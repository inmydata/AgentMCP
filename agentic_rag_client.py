"""Agentic RAG HTTP client.

Holds the in-memory + file-backed tenant-key cache and performs
provision / mint / query calls against an Agentic RAG service.

Pure I/O — no FastMCP imports.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass, asdict
from typing import Optional

import httpx


_log = logging.getLogger("agentic_rag")


class AgenticRagError(Exception):
    """Raised when a call cannot be completed. The message is safe to surface to the LLM."""


class KeyLimitExhausted(AgenticRagError):
    """Raised when a tenant already has the maximum number of active API keys."""


@dataclass
class _TenantCreds:
    tenant_id: str
    api_key: str


def _slugify(external_id: str) -> str:
    """Derive a slug matching ^[a-z][a-z0-9_]{1,48}$ from an external_id."""
    s = re.sub(r"[^a-z0-9_]+", "_", external_id.lower()).strip("_")
    if not s or not s[0].isalpha():
        s = "t_" + s
    if len(s) == 1:
        s = s + "_x"
    return s[:49]


def _is_key_limit_error(detail: str) -> bool:
    if not detail:
        return False
    d = detail.lower()
    return ("20" in d and "key" in d) or "limit" in d and "key" in d


class AgenticRagClient:
    """Async client with cache + single-replica persistence."""

    def __init__(
        self,
        base_url: str,
        psk: str,
        cache_file: str,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._psk = psk
        self._cache_file = cache_file
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        self._creds: dict[str, _TenantCreds] = {}
        # Lock for mutating the lock-map and the cache file write path.
        self._meta_lock = asyncio.Lock()
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._load_cache_from_disk()

    async def aclose(self) -> None:
        await self._http.aclose()

    def _load_cache_from_disk(self) -> None:
        if not os.path.exists(self._cache_file):
            return
        try:
            with open(self._cache_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("cache file is not a JSON object")
            self._creds = {
                eid: _TenantCreds(tenant_id=v["tenant_id"], api_key=v["api_key"])
                for eid, v in raw.items()
                if isinstance(v, dict) and "tenant_id" in v and "api_key" in v
            }
        except Exception as exc:
            ts = int(time.time())
            bad = f"{self._cache_file}.corrupt.{ts}"
            try:
                os.replace(self._cache_file, bad)
            except OSError:
                pass
            _log.warning(
                "agentic_rag cache file unreadable (%s); renamed to %s and starting empty",
                exc, bad,
            )
            self._creds = {}

    async def _persist(self) -> None:
        payload = {eid: asdict(c) for eid, c in self._creds.items()}
        tmp = f"{self._cache_file}.tmp"
        dirpath = os.path.dirname(os.path.abspath(self._cache_file)) or "."
        os.makedirs(dirpath, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, self._cache_file)
        if sys.platform != "win32":
            try:
                os.chmod(self._cache_file, 0o600)
            except OSError:
                pass

    async def _lock_for(self, external_id: str) -> asyncio.Lock:
        async with self._meta_lock:
            lock = self._key_locks.get(external_id)
            if lock is None:
                lock = asyncio.Lock()
                self._key_locks[external_id] = lock
            return lock

    async def _creds_for(self, external_id: str) -> _TenantCreds:
        cached = self._creds.get(external_id)
        if cached is not None:
            return cached

        lock = await self._lock_for(external_id)
        async with lock:
            cached = self._creds.get(external_id)
            if cached is not None:
                return cached

            slug = _slugify(external_id)
            prov = await self._post_admin(
                "/admin/tenants/provision",
                {"external_id": external_id, "name": external_id[:200], "slug": slug},
            )
            tenant_id = prov["tenant_id"]
            api_key = prov.get("api_key")
            if api_key is None:
                mint = await self._post_admin(
                    f"/admin/tenants/{tenant_id}/api-keys",
                    {"label": "mcp-server"},
                )
                api_key = mint["api_key"]

            creds = _TenantCreds(tenant_id=tenant_id, api_key=api_key)
            self._creds[external_id] = creds
            await self._persist()
            return creds

    async def _invalidate(self, external_id: str) -> None:
        lock = await self._lock_for(external_id)
        async with lock:
            if external_id in self._creds:
                self._creds.pop(external_id, None)
                await self._persist()

    async def query(
        self,
        external_id: str,
        query_text: str,
        session_id: Optional[str],
    ) -> dict:
        if session_id == "":
            session_id = None

        creds = await self._creds_for(external_id)
        resp = await self._post_query(creds.api_key, query_text, session_id)

        if resp.status_code == 401:
            await self._invalidate(external_id)
            try:
                creds = await self._creds_for(external_id)
            except KeyLimitExhausted:
                raise
            resp = await self._post_query(creds.api_key, query_text, session_id)

        return self._handle_query_response(resp)

    async def _post_query(
        self,
        api_key: str,
        query_text: str,
        session_id: Optional[str],
    ) -> httpx.Response:
        body = {"query": query_text, "session_id": session_id, "stream": False}
        last_exc: Optional[Exception] = None
        for attempt in range(2):  # one retry on 5xx / transport error
            try:
                resp = await self._http.post(
                    f"{self._base}/v1/query",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == 0:
                    await asyncio.sleep(random.uniform(0.2, 0.5))
                    continue
                raise AgenticRagError(f"transport error: {exc}") from exc

            if 500 <= resp.status_code < 600 and attempt == 0:
                await asyncio.sleep(random.uniform(0.2, 0.5))
                continue
            return resp
        assert last_exc is not None
        raise AgenticRagError(f"transport error: {last_exc}")

    def _handle_query_response(self, resp: httpx.Response) -> dict:
        status = resp.status_code
        if status == 200:
            return resp.json()

        detail = self._extract_detail(resp)

        if status == 401:
            raise AgenticRagError("authentication failed (re-mint attempted)")
        if status == 429:
            retry_after = resp.headers.get("Retry-After", "?")
            try:
                retry_after = str(int(retry_after))
            except (TypeError, ValueError):
                retry_after = "?"
            raise AgenticRagError(f"rate limited; retry in {retry_after}s")
        if status == 403:
            raise AgenticRagError(detail or "forbidden")
        if status == 422:
            _log.warning("agentic_rag /v1/query 422 detail: %s", detail)
            raise AgenticRagError("request validation failed - see server log")
        if 400 <= status < 500:
            raise AgenticRagError(detail or f"HTTP {status}")
        raise AgenticRagError(detail or f"HTTP {status}")

    async def _post_admin(self, path: str, body: dict) -> dict:
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            try:
                resp = await self._http.post(
                    f"{self._base}{path}",
                    headers={
                        "Authorization": f"Bearer {self._psk}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt == 0:
                    await asyncio.sleep(random.uniform(0.2, 0.5))
                    continue
                raise AgenticRagError(f"transport error: {exc}") from exc

            if 500 <= resp.status_code < 600 and attempt == 0:
                await asyncio.sleep(random.uniform(0.2, 0.5))
                continue

            status = resp.status_code
            if status == 200 or status == 201:
                return resp.json()

            detail = self._extract_detail(resp)

            if status == 429:
                retry_after = resp.headers.get("Retry-After", "?")
                try:
                    retry_after = str(int(retry_after))
                except (TypeError, ValueError):
                    retry_after = "?"
                raise AgenticRagError(f"rate limited; retry in {retry_after}s")
            if status == 409 or (status == 400 and _is_key_limit_error(detail)):
                raise KeyLimitExhausted(
                    "tenant key pool exhausted (20 active keys). "
                    "An operator must revoke stale keys via the admin API "
                    "before this tool can recover."
                )
            if _is_key_limit_error(detail):
                raise KeyLimitExhausted(
                    "tenant key pool exhausted (20 active keys). "
                    "An operator must revoke stale keys via the admin API "
                    "before this tool can recover."
                )
            if status == 422:
                _log.warning("agentic_rag admin %s 422 detail: %s", path, detail)
                raise AgenticRagError("admin request validation failed - see server log")
            if 400 <= status < 500:
                raise AgenticRagError(detail or f"HTTP {status}")
            raise AgenticRagError(detail or f"HTTP {status}")
        assert last_exc is not None
        raise AgenticRagError(f"transport error: {last_exc}")

    @staticmethod
    def _extract_detail(resp: httpx.Response) -> str:
        try:
            data = resp.json()
        except ValueError:
            return resp.text or ""
        if isinstance(data, dict):
            d = data.get("detail")
            if isinstance(d, str):
                return d
            if d is not None:
                return json.dumps(d)
        return ""
