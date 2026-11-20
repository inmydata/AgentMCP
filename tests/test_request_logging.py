"""Tests for per-request MCP logging and the get_answer KeyError handling."""
import base64
import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastmcp.server.auth.providers.jwt import AccessToken  # noqa: E402
from mcp_logging import logger as agentmcp_logger, token_fingerprint  # noqa: E402
from mcp_utils import mcp_utils  # noqa: E402
from request_logging import MCPRequestLogMiddleware  # noqa: E402


@pytest.fixture
def logs():
    records: list[str] = []

    class _Buffer(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = _Buffer(level=logging.DEBUG)
    previous = agentmcp_logger.level
    agentmcp_logger.setLevel(logging.DEBUG)
    agentmcp_logger.addHandler(handler)
    try:
        yield records
    finally:
        agentmcp_logger.removeHandler(handler)
        agentmcp_logger.setLevel(previous)


def _jwt(claims: dict) -> str:
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.c2ln"


def _app_returning(status: int):
    seen = {}

    async def app(scope, receive, send):
        message = await receive()
        seen["body"] = message.get("body", b"")
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    return app, seen


async def _call(middleware, *, path="/mcp", method="POST", headers=None, body=b"", query=b""):
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http", "method": method, "path": path, "query_string": query,
        "headers": raw_headers, "client": ("10.0.0.1", 1234),
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)
    return sent


INIT_BODY = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()


@pytest.mark.asyncio
async def test_no_token_gets_its_own_line(logs):
    app, _ = _app_returning(401)
    mw = MCPRequestLogMiddleware(app, oauth=True)
    await _call(mw, headers={"user-agent": "zgrab/0.x"}, body=INIT_BODY)
    assert len(logs) == 1
    assert logs[0].startswith("MCP request without token:")
    assert "rpc=initialize" in logs[0] and "status=401" in logs[0] and "zgrab" in logs[0]


@pytest.mark.asyncio
async def test_handshake_logs_client_and_tenant_and_replays_body(logs):
    app, seen = _app_returning(200)
    mw = MCPRequestLogMiddleware(app, oauth=True)
    token = _jwt({"client_id": "claude-connector", "client_imd_tenant": "acme"})
    await _call(mw, headers={"authorization": f"Bearer {token}"}, body=INIT_BODY)
    assert seen["body"] == INIT_BODY
    line = logs[0]
    assert "rpc=initialize" in line and "client_id=claude-connector" in line and "tenant=acme" in line
    assert "unverified" not in line
    assert token not in line and token_fingerprint(token) in line


@pytest.mark.asyncio
async def test_401_with_jwt_marks_claims_unverified(logs):
    app, _ = _app_returning(401)
    mw = MCPRequestLogMiddleware(app, oauth=True)
    token = _jwt({"azp": "chatgpt", "imd_tenant": "acme"})
    await _call(mw, headers={"authorization": f"Bearer {token}"}, body=INIT_BODY)
    assert "status=401" in logs[0] and "client_id=chatgpt" in logs[0] and "claims=unverified" in logs[0]


@pytest.mark.asyncio
async def test_pat_identity_from_verifier_cache(logs):
    class _Verifier:
        def peek_cached(self, token):
            return AccessToken(token=token, client_id="pat-client", scopes=[], claims={"imd_tenant": "acme"})

    app, _ = _app_returning(200)
    mw = MCPRequestLogMiddleware(app, oauth=True, token_verifier=_Verifier())
    body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_schema"}}).encode()
    await _call(mw, headers={"authorization": "Bearer opaque-pat"}, body=body)
    assert "client_id=pat-client" in logs[0] and "tenant=acme" in logs[0]
    assert "rpc=tools/call:get_schema" in logs[0]


@pytest.mark.asyncio
async def test_legacy_mode_logs_tenant_from_query(logs):
    app, _ = _app_returning(200)
    mw = MCPRequestLogMiddleware(app, oauth=False)
    await _call(mw, headers={"authorization": "Bearer key"}, body=INIT_BODY, query=b"tenant=acme")
    assert "tenant=acme" in logs[0]


@pytest.mark.asyncio
async def test_other_paths_not_logged(logs):
    app, _ = _app_returning(200)
    mw = MCPRequestLogMiddleware(app, oauth=True)
    await _call(mw, path="/", method="GET")
    await _call(mw, path="/mcp/.well-known/oauth-protected-resource", method="GET")
    assert logs == []


@pytest.mark.asyncio
async def test_get_answer_missing_answer_key_returns_clear_error():
    utils = mcp_utils(api_key="k", tenant="acme", calendar="c", user="u", session_id="s", server="example.com")

    class _BrokenDriver:
        """Stands in for the SDK driver when the question API returns an error body."""

        def __init__(self, *args, **kwargs):
            pass

        def on(self, *args):
            pass

        async def get_answer(self, question):
            raise KeyError("answer")

    with patch("inmydata.ConversationalData.ConversationalDataDriver", _BrokenDriver):
        result = json.loads(await utils.get_answer("top stores"))

    assert "question service returned an error" in result["error"]
    assert "correlation_id" not in result
