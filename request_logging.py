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

import base64
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import parse_qs

from mcp_logging import logger, token_fingerprint

# Only this many bytes of a POST body are parsed to find the JSON-RPC method.
# The body is always passed on to the app in full.
_MAX_BODY_TO_PARSE = 64 * 1024

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
            query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            tenant = (query.get("tenant") or [""])[0] or headers.get("x-inmydata-tenant", "")
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
        auth_header = headers.get("authorization", "").strip()
        token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else auth_header

        # Buffer a POST body so the JSON-RPC method can be logged, then replay it.
        rpc = None
        if scope.get("method") == "POST":
            chunks: List[bytes] = []
            more = True
            while more:
                message = await receive()
                if message["type"] != "http.request":
                    # Client went away mid-body; hand the app what we have plus this.
                    pending = [{"type": "http.request", "body": b"".join(chunks), "more_body": False}, message]
                    receive = _replay(pending, receive)
                    break
                chunks.append(message.get("body", b""))
                more = message.get("more_body", False)
            else:
                body = b"".join(chunks)
                rpc = _rpc_summary(body)
                receive = _replay([{"type": "http.request", "body": body, "more_body": False}], receive)

        logged = False

        async def send_wrapper(message):
            nonlocal logged
            if message["type"] == "http.response.start" and not logged:
                logged = True
                try:
                    self._log(scope, headers, token, rpc, message.get("status", 0))
                except Exception:
                    # Logging must never break a request.
                    logger.debug("MCP request logging failed", exc_info=True)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            if not logged:
                try:
                    self._log(scope, headers, token, rpc, 500)
                except Exception:
                    logger.debug("MCP request logging failed", exc_info=True)
            raise

    def _log(self, scope, headers, token: str, rpc: Optional[str], status: int) -> None:
        method = scope.get("method", "-")
        path = scope.get("path", "-")
        session = headers.get("mcp-session-id", "-")

        if not token:
            logger.info(
                "MCP request without token: %s %s rpc=%s status=%d ip=%s user_agent=%r",
                method, path, rpc or "-", status, _client_ip(headers, scope),
                headers.get("user-agent", "-"),
            )
            return

        identity = self._identity(token, scope, headers)
        unverified = self.oauth and not (200 <= status < 300) and (identity.client_id or identity.tenant)
        logger.info(
            "MCP request: %s %s rpc=%s status=%d client_id=%s tenant=%s token=%s session=%s%s",
            method, path, rpc or "-", status,
            identity.client_id or "-", identity.tenant or "-",
            token_fingerprint(token), session,
            " claims=unverified" if unverified else "",
        )


def _replay(messages: List[Dict[str, Any]], receive):
    """Return a ``receive`` callable that yields ``messages`` first, then defers to ``receive``."""
    queue = list(messages)

    async def replay_receive():
        if queue:
            return queue.pop(0)
        return await receive()

    return replay_receive
