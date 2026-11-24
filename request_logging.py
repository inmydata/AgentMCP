"""Per-request logging for the MCP endpoint.

Wraps the ASGI app and writes one log line for every request to ``/mcp``,
including the ``initialize`` handshake and requests that FastMCP rejects with
401 before any tool code runs. Each line names who the request came from
(OAuth client id and/or inmydata tenant) so a failure can be traced back to a
customer without asking them for details.

Requests that arrive with no ``Authorization`` header at all are logged under a
separate message (``MCP request without token``). These are normally either
the first step of a real connector's OAuth handshake or internet scanners, and
keeping them apart stops them being mixed in with real, authenticated traffic.

Identity is worked out without any network calls:

* JWTs: the payload is base64-decoded (not verified) to read ``client_id`` and
  the tenant claim. On a 2xx response FastMCP has already verified the token;
  on any other status the claims are marked ``claims=unverified`` because
  anyone can put anything in an unsigned or forged token.
* PATs (opaque tokens): the verifier's introspection cache is consulted. FastMCP
  has just verified the same token, so a valid PAT is normally in the cache.
* Legacy (non-OAuth) mode: the tenant comes from the ``tenant`` query parameter
  or the ``x-inmydata-tenant`` header, as in ``server_remote.utils``.

The raw token is never logged; ``token_fingerprint`` is used instead.
"""
from __future__ import annotations

import asyncio
import base64
import json
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import parse_qs

from starlette.datastructures import Headers

from mcp_logging import logger, token_fingerprint

# The logger captures at most this many bytes while the app consumes the body.
_MAX_BODY_TO_PARSE = 64 * 1024
_EARLY_REJECTION_CAPTURE_TIMEOUT_SECONDS = 0.1
_MAX_EARLY_REJECTION_CAPTURE_EVENTS = 128

ASGIApp = Callable[..., Awaitable[None]]


@dataclass
class RequestIdentity:
    """Who a request appears to come from. Any field may be ``None``."""

    client_id: Optional[str] = None
    tenant: Optional[str] = None


def _decode_jwt_claims(token: str) -> Optional[Dict[str, Any]]:
    """Return the payload of a JWT without verifying it, or ``None``."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return None
    return claims if isinstance(claims, dict) else None


def _identity_from_claims(claims: Dict[str, Any]) -> RequestIdentity:
    return RequestIdentity(
        client_id=claims.get("client_id") or claims.get("azp"),
        tenant=claims.get("client_imd_tenant") or claims.get("imd_tenant"),
    )


def _rpc_summary(body: bytes) -> Optional[str]:
    """Return the JSON-RPC method(s) in a POST body, e.g. ``tools/call:get_schema``."""
    if not body or len(body) > _MAX_BODY_TO_PARSE:
        return None
    try:
        message = json.loads(body)
    except Exception:
        return None
    messages = message if isinstance(message, list) else [message]
    names: List[str] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        method = item.get("method")
        if not isinstance(method, str):
            continue
        params = item.get("params")
        if method == "tools/call" and isinstance(params, dict) and isinstance(params.get("name"), str):
            method = f"{method}:{params['name']}"
        names.append(method)
    return ",".join(names) if names else None


def _client_ip(headers: Dict[str, str], scope: Dict[str, Any]) -> str:
    # Behind the load balancer the real client is the first X-Forwarded-For entry.
    forwarded = headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "-"


class MCPRequestLogMiddleware:
    """ASGI middleware that logs each request to the MCP endpoint.

    Args:
        app: The ASGI app to wrap.
        oauth: True when the server runs with OAuth (identity from the token);
            False for legacy mode (identity from the tenant query param/header).
        token_verifier: The ``PATAwareJWTVerifier`` in OAuth mode, used only to
            look up PATs in its cache. Optional.
        path_prefix: Requests whose path is this, or starts with it plus ``/``,
            are logged. Everything else is passed straight through.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        oauth: bool,
        token_verifier: Any = None,
        path_prefix: str = "/mcp",
    ) -> None:
        self.app = app
        self.oauth = oauth
        self.token_verifier = token_verifier
        self.path_prefix = path_prefix.rstrip("/")

    def _is_mcp_path(self, path: str) -> bool:
        if path != self.path_prefix and not path.startswith(self.path_prefix + "/"):
            return False
        # Discovery documents under /mcp/.well-known are not MCP calls.
        return "/.well-known/" not in path

    def _identity(self, token: str, scope: Dict[str, Any], headers: Dict[str, str]) -> RequestIdentity:
        if not self.oauth:
            query = parse_qs(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
            tenant = (query.get("tenant") or [""])[-1] or headers.get("x-inmydata-tenant", "")
            return RequestIdentity(tenant=tenant or None)

        claims = _decode_jwt_claims(token)
        if claims is not None:
            return _identity_from_claims(claims)

        peek = getattr(self.token_verifier, "peek_cached", None)
        if peek is not None:
            cached = peek(token)
            if cached is not None:
                identity = _identity_from_claims(cached.claims or {})
                identity.client_id = identity.client_id or cached.client_id
                return identity
        return RequestIdentity()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self._is_mcp_path(scope.get("path", "")):
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        # OAuth authentication uses Starlette's first-value header lookup;
        # legacy mode uses FastMCP's last-value header dictionary.
        auth_header = (
            Headers(raw=scope.get("headers", [])).get("authorization", "")
            if self.oauth else headers.get("authorization", "")
        ).strip()
        token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else auth_header

        request_body = bytearray()
        request_body_size = 0
        request_body_complete = False
        request_body_too_large = False
        pending_messages = deque()
        original_receive = receive

        if scope.get("method") == "POST":
            receive_lock = asyncio.Lock()

            def record_request_message(message):
                nonlocal request_body_size, request_body_complete, request_body_too_large
                if message["type"] != "http.request":
                    return
                chunk = message.get("body", b"")
                if not request_body_too_large:
                    if request_body_size + len(chunk) > _MAX_BODY_TO_PARSE:
                        request_body.clear()
                        request_body_too_large = True
                    elif chunk:
                        request_body.extend(chunk)
                        request_body_size += len(chunk)
                if not message.get("more_body", False):
                    request_body_complete = True

            async def receive_wrapper():
                async with receive_lock:
                    if pending_messages:
                        return pending_messages.popleft()
                    message = await original_receive()
                    record_request_message(message)
                    return message

            receive = receive_wrapper

            async def capture_body_before_rejection():
                if request_body_complete or request_body_too_large:
                    return

                loop = asyncio.get_running_loop()
                deadline = loop.time() + _EARLY_REJECTION_CAPTURE_TIMEOUT_SECONDS
                captured = []
                try:
                    await asyncio.wait_for(
                        receive_lock.acquire(),
                        timeout=max(0, deadline - loop.time()),
                    )
                except asyncio.TimeoutError:
                    return

                try:
                    for _ in range(_MAX_EARLY_REJECTION_CAPTURE_EVENTS):
                        if request_body_complete or request_body_too_large:
                            break
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            break
                        try:
                            message = await asyncio.wait_for(original_receive(), timeout=remaining)
                        except asyncio.TimeoutError:
                            break
                        except Exception:
                            logger.debug("MCP request body capture before 401 failed", exc_info=True)
                            break

                        captured.append(message)
                        record_request_message(message)
                        if message["type"] != "http.request" or request_body_complete or request_body_too_large:
                            break
                finally:
                    pending_messages.extend(captured)
                    receive_lock.release()

        def rpc_summary() -> Optional[str]:
            if not request_body_complete or request_body_too_large:
                return None
            return _rpc_summary(bytes(request_body))

        logged = False

        async def send_wrapper(message):
            nonlocal logged
            if message["type"] == "http.response.start" and not logged:
                if scope.get("method") == "POST" and message.get("status") == 401:
                    try:
                        await capture_body_before_rejection()
                    except Exception:
                        logger.debug("MCP request body capture before 401 failed", exc_info=True)
                logged = True
                try:
                    self._log(scope, headers, token, rpc_summary(), message.get("status", 0))
                except Exception:
                    # Logging must never break a request.
                    logger.debug("MCP request logging failed", exc_info=True)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            if not logged:
                try:
                    self._log(scope, headers, token, rpc_summary(), 500)
                except Exception:
                    logger.debug("MCP request logging failed", exc_info=True)
            raise

    def _log(self, scope, headers, token: str, rpc: Optional[str], status: int) -> None:
        def log_field(value: Any) -> str:
            return json.dumps(str(value), ensure_ascii=True)[1:-1]

        method = log_field(scope.get("method", "-"))
        path = log_field(scope.get("path", "-"))
        session = log_field(headers.get("mcp-session-id", "-"))
        rpc_field = log_field(rpc or "-")

        if not token:
            tenant = self._identity(token, scope, headers).tenant if not self.oauth else None
            logger.info(
                "MCP request without token: %s %s rpc=%s status=%d tenant=%s ip=%s user_agent=%s",
                method, path, rpc_field, status, log_field(tenant or "-"),
                log_field(_client_ip(headers, scope)), log_field(headers.get("user-agent", "-")),
            )
            return

        identity = self._identity(token, scope, headers)
        unverified = self.oauth and not (200 <= status < 300) and (identity.client_id or identity.tenant)
        logger.info(
            "MCP request: %s %s rpc=%s status=%d client_id=%s tenant=%s token=%s session=%s%s",
            method, path, rpc_field, status,
            log_field(identity.client_id or "-"), log_field(identity.tenant or "-"),
            token_fingerprint(token), session,
            " claims=unverified" if unverified else "",
        )
