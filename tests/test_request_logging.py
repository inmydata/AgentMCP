"""Tests for per-request MCP logging and the get_answer KeyError handling."""
import asyncio
import base64
import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastmcp.server.auth.providers.jwt import AccessToken  # noqa: E402
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware  # noqa: E402
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend  # noqa: E402
from starlette.datastructures import Headers  # noqa: E402
from starlette.middleware.authentication import AuthenticationMiddleware  # noqa: E402
from mcp_logging import logger as agentmcp_logger, token_fingerprint  # noqa: E402
from mcp_utils import mcp_utils  # noqa: E402
from request_logging import _MAX_BODY_TO_PARSE, MCPRequestLogMiddleware  # noqa: E402


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


async def _call_with_receive(middleware, receive, *, headers=None):
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http", "method": "POST", "path": "/mcp", "query_string": b"",
        "headers": raw_headers, "client": ("10.0.0.1", 1234),
    }
    sent = []

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
async def test_401_without_reading_body_still_logs_rpc(logs):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 401, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw = MCPRequestLogMiddleware(app, oauth=True)
    await _call(mw, body=INIT_BODY)

    assert "rpc=initialize" in logs[0]


@pytest.mark.asyncio
async def test_401_capture_replays_complete_body_to_app(logs):
    messages = [
        {"type": "http.request", "body": INIT_BODY[:16], "more_body": True},
        {"type": "http.request", "body": INIT_BODY[16:], "more_body": False},
    ]
    replayed = []
    index = 0

    async def receive():
        nonlocal index
        message = messages[index]
        index += 1
        return message

    async def app(scope, app_receive, send):
        await send({"type": "http.response.start", "status": 401, "headers": []})
        replayed.extend([await app_receive(), await app_receive()])
        await send({"type": "http.response.body", "body": b"{}"})

    mw = MCPRequestLogMiddleware(app, oauth=True)
    await _call_with_receive(mw, receive)

    assert "rpc=initialize" in logs[0]
    assert replayed == messages


@pytest.mark.asyncio
async def test_401_capture_stops_at_body_limit_and_replays_remaining_stream(logs):
    messages = [
        {"type": "http.request", "body": b"x" * (_MAX_BODY_TO_PARSE + 1), "more_body": True},
        {"type": "http.request", "body": b"tail", "more_body": False},
    ]
    replayed = []
    index = 0

    async def receive():
        nonlocal index
        message = messages[index]
        index += 1
        return message

    async def app(scope, app_receive, send):
        await send({"type": "http.response.start", "status": 401, "headers": []})
        replayed.extend([await app_receive(), await app_receive()])

    mw = MCPRequestLogMiddleware(app, oauth=True)
    await _call_with_receive(mw, receive)

    assert "rpc=-" in logs[0]
    assert replayed == messages


@pytest.mark.asyncio
async def test_401_capture_replays_disconnect_without_completing_body(logs):
    messages = [
        {"type": "http.request", "body": INIT_BODY, "more_body": True},
        {"type": "http.disconnect"},
    ]
    replayed = []
    index = 0

    async def receive():
        nonlocal index
        message = messages[index]
        index += 1
        return message

    async def app(scope, app_receive, send):
        await send({"type": "http.response.start", "status": 401, "headers": []})
        replayed.extend([await app_receive(), await app_receive()])

    mw = MCPRequestLogMiddleware(app, oauth=True)
    await _call_with_receive(mw, receive)

    assert "rpc=-" in logs[0]
    assert replayed == messages


@pytest.mark.asyncio
async def test_401_capture_times_out_and_replays_partial_body(logs, monkeypatch):
    monkeypatch.setattr("request_logging._EARLY_REJECTION_CAPTURE_TIMEOUT_SECONDS", 0.01)
    first = {"type": "http.request", "body": INIT_BODY[:-1], "more_body": True}
    replayed = []
    receive_calls = 0
    stalled = asyncio.Event()

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls == 1:
            return first
        await stalled.wait()

    async def app(scope, app_receive, send):
        await send({"type": "http.response.start", "status": 401, "headers": []})
        replayed.append(await app_receive())

    mw = MCPRequestLogMiddleware(app, oauth=True)
    await asyncio.wait_for(_call_with_receive(mw, receive), timeout=1)

    assert receive_calls == 2
    assert replayed == [first]
    assert "status=401" in logs[0] and "rpc=-" in logs[0]


@pytest.mark.asyncio
async def test_handshake_logs_client_and_tenant_and_replays_body(logs):
    app, seen = _app_returning(200)
    mw = MCPRequestLogMiddleware(app, oauth=True)
    token = _jwt({"client_id": "claude-connector", "client_imd_tenant": "acme"})
    await _call(mw, headers={"authorization": f"Bearer {token}"}, body=INIT_BODY)
    assert seen["body"] == INIT_BODY
    line = logs[0]
    assert "rpc=initialize" in line and "client_id=claude-connector" in line and "tenant=acme" in line
    assert "claims=unverified" in line  # The stub app never authenticated the request.
    assert token not in line and token_fingerprint(token) in line


@pytest.mark.asyncio
@pytest.mark.parametrize("oauth", [True, False])
async def test_duplicate_authorization_logs_the_credential_used_by_app(logs, oauth):
    first = _jwt({"client_id": "first-client", "client_imd_tenant": "first-tenant"})
    second = _jwt({"client_id": "second-client", "client_imd_tenant": "second-tenant"})
    raw_headers = [
        (b"authorization", f"Bearer {first}".encode()),
        (b"authorization", f"Bearer {second}".encode()),
    ]
    scope = {
        "type": "http", "method": "POST", "path": "/mcp", "query_string": b"tenant=legacy",
        "headers": raw_headers, "client": ("10.0.0.1", 1234),
    }

    async def app(scope, receive, send):
        headers = Headers(scope=scope)
        used = headers.get("authorization") if oauth else dict(headers.items())["authorization"]
        expected = first if oauth else second
        assert used == f"Bearer {expected}"
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def receive():
        return {"type": "http.request", "body": INIT_BODY, "more_body": False}

    async def send(message):
        pass

    await MCPRequestLogMiddleware(app, oauth=oauth)(scope, receive, send)

    expected = first if oauth else second
    assert f"token={token_fingerprint(expected)}" in logs[0]
    if oauth:
        assert "client_id=first-client" in logs[0]
        assert "tenant=first-tenant" in logs[0]
        assert "second-client" not in logs[0]


@pytest.mark.asyncio
async def test_401_with_jwt_marks_claims_unverified(logs):
    app, _ = _app_returning(401)
    mw = MCPRequestLogMiddleware(app, oauth=True)
    token = _jwt({"azp": "chatgpt", "imd_tenant": "acme"})
    await _call(mw, headers={"authorization": f"Bearer {token}"}, body=INIT_BODY)
    assert "status=401" in logs[0] and "client_id=chatgpt" in logs[0] and "claims=unverified" in logs[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token",
    ["opaque-pat", _jwt({"client_id": "payload-client", "imd_tenant": "payload-tenant"})],
    ids=["opaque-pat", "jwt"],
)
async def test_request_logs_verified_identity_not_decoded_claims(logs, token):
    class _Verifier:
        async def verify_token(self, token):
            return AccessToken(token=token, client_id="pat-client", scopes=[], claims={"imd_tenant": "acme"})

    app, _ = _app_returning(200)
    authenticated = AuthenticationMiddleware(AuthContextMiddleware(app), backend=BearerAuthBackend(_Verifier()))
    mw = MCPRequestLogMiddleware(authenticated, oauth=True)
    body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_schema"}}).encode()
    await _call(mw, headers={"authorization": f"Bearer {token}", "mcp-session-id": "private-session-id"}, body=body)
    assert "client_id=pat-client" in logs[0] and "tenant=acme" in logs[0]
    assert "claims=unverified" not in logs[0] and "private-session-id" not in logs[0]
    assert "rpc=tools/call:get_schema" in logs[0]


@pytest.mark.asyncio
async def test_legacy_mode_logs_tenant_from_query(logs):
    app, _ = _app_returning(200)
    mw = MCPRequestLogMiddleware(app, oauth=False)
    await _call(mw, headers={"authorization": "Bearer key"}, body=INIT_BODY, query=b"tenant=acme")
    assert "tenant=acme" in logs[0]


@pytest.mark.asyncio
async def test_legacy_no_token_logs_tenant_from_query_or_header(logs):
    app, _ = _app_returning(200)
    mw = MCPRequestLogMiddleware(app, oauth=False)

    await _call(mw, body=INIT_BODY, query=b"tenant=query-tenant")
    await _call(mw, headers={"x-inmydata-tenant": "header-tenant"}, body=INIT_BODY)

    assert "tenant=query-tenant" in logs[0]
    assert "tenant=header-tenant" in logs[1]


@pytest.mark.asyncio
async def test_request_body_is_streamed_and_capture_is_bounded(logs):
    first_body = INIT_BODY
    oversized_body = b" " * (_MAX_BODY_TO_PARSE + 1)
    messages = [
        {"type": "http.request", "body": first_body, "more_body": True},
        {"type": "http.request", "body": oversized_body, "more_body": False},
    ]
    received = []
    receive_count = 0

    async def receive():
        nonlocal receive_count
        message = messages[receive_count]
        receive_count += 1
        return message

    async def app(scope, app_receive, send):
        assert receive_count == 0
        for _ in messages:
            received.append(await app_receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(message):
        pass

    mw = MCPRequestLogMiddleware(app, oauth=True)
    scope = {
        "type": "http", "method": "POST", "path": "/mcp", "query_string": b"",
        "headers": [], "client": ("10.0.0.1", 1234),
    }

    await mw(scope, receive, send)

    assert received == messages
    assert "rpc=-" in logs[0]


@pytest.mark.asyncio
async def test_disconnect_is_passed_through_without_completing_truncated_body(logs):
    messages = [
        {"type": "http.request", "body": INIT_BODY, "more_body": True},
        {"type": "http.disconnect"},
    ]
    received = []
    index = 0

    async def receive():
        nonlocal index
        message = messages[index]
        index += 1
        return message

    async def app(scope, app_receive, send):
        received.extend([await app_receive(), await app_receive()])
        await send({"type": "http.response.start", "status": 400, "headers": []})

    async def send(message):
        pass

    mw = MCPRequestLogMiddleware(app, oauth=True)
    scope = {
        "type": "http", "method": "POST", "path": "/mcp", "query_string": b"",
        "headers": [], "client": ("10.0.0.1", 1234),
    }

    await mw(scope, receive, send)

    assert received == messages
    assert "rpc=-" in logs[0]


@pytest.mark.asyncio
async def test_exception_before_response_start_logs_fallback_500_and_reraises(logs):
    async def app(scope, receive, send):
        raise RuntimeError("downstream failure")

    mw = MCPRequestLogMiddleware(app, oauth=True)

    with pytest.raises(RuntimeError, match="downstream failure"):
        await _call(mw, body=INIT_BODY)

    assert len(logs) == 1
    assert "status=500" in logs[0]


@pytest.mark.asyncio
async def test_request_controlled_log_fields_are_escaped(logs):
    app, _ = _app_returning(401)
    mw = MCPRequestLogMiddleware(app, oauth=True)
    token = _jwt({
        "client_id": "untrusted\nclient",
        "client_imd_tenant": "untrusted\r\ntenant",
    })
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize\nforged",
    }).encode()

    await _call(
        mw,
        headers={"authorization": f"Bearer {token}"},
        body=body,
    )

    assert "\n" not in logs[0] and "\r" not in logs[0]
    assert "\\n" in logs[0] and "\\r" in logs[0]

    legacy_app, _ = _app_returning(200)
    legacy_mw = MCPRequestLogMiddleware(legacy_app, oauth=False)
    await _call(
        legacy_mw,
        headers={"x-inmydata-tenant": "untrusted\r\ntenant", "user-agent": "agent\nforged"},
        body=INIT_BODY,
    )

    assert "\n" not in logs[1] and "\r" not in logs[1]
    assert "\\n" in logs[1] and "\\r" in logs[1]


@pytest.mark.asyncio
async def test_other_paths_not_logged(logs):
    app, _ = _app_returning(200)
    mw = MCPRequestLogMiddleware(app, oauth=True)
    await _call(mw, path="/", method="GET")
    await _call(mw, path="/mcp/.well-known/oauth-protected-resource", method="GET")
    assert logs == []


@pytest.mark.asyncio
async def test_get_answer_missing_answer_key_returns_clear_error(logs):
    utils = mcp_utils(api_key="k", tenant="acme\r\nbranch", calendar="c", user="u", session_id="s", server="example.com")

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
    warning, = (line for line in logs if "question API returned no answer" in line)
    assert r"tenant=acme\r\nbranch" in warning and len(warning.splitlines()) == 1
