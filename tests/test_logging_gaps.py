"""Extra tests for ``mcp_logging`` and ``request_logging``.

These fill gaps left by ``test_logging_safety.py`` and ``test_request_logging.py``:
logger configuration, tuple redaction, malformed request bodies and tokens,
client address handling, and the middleware's behaviour when its own logging
or body capture fails. In this file the log output is the behaviour being
tested, so assertions look at the log lines directly.
"""
import asyncio
import base64
import io
import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_logging import configure_logging, logger as agentmcp_logger, redact  # noqa: E402
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


@pytest.fixture
def pristine_logger(monkeypatch):
    """The ``agentmcp`` logger with no handlers, restored afterwards."""
    monkeypatch.delenv("AGENTMCP_LOG_LEVEL", raising=False)
    saved_handlers = list(agentmcp_logger.handlers)
    saved_level = agentmcp_logger.level
    saved_propagate = agentmcp_logger.propagate
    agentmcp_logger.handlers = []
    try:
        yield agentmcp_logger
    finally:
        agentmcp_logger.handlers = saved_handlers
        agentmcp_logger.setLevel(saved_level)
        agentmcp_logger.propagate = saved_propagate


# --- configure_logging -----------------------------------------------------

def test_configure_logging_defaults_to_info(pristine_logger):
    configure_logging()

    assert pristine_logger.level == logging.INFO


def test_configure_logging_uses_the_supplied_default_level(pristine_logger):
    configure_logging("WARNING")

    assert pristine_logger.level == logging.WARNING


def test_configure_logging_prefers_the_environment_over_the_default(pristine_logger, monkeypatch):
    monkeypatch.setenv("AGENTMCP_LOG_LEVEL", "DEBUG")

    configure_logging("ERROR")

    assert pristine_logger.level == logging.DEBUG


def test_configure_logging_accepts_a_lower_case_level_name(pristine_logger, monkeypatch):
    monkeypatch.setenv("AGENTMCP_LOG_LEVEL", "error")

    configure_logging()

    assert pristine_logger.level == logging.ERROR


def test_configure_logging_falls_back_to_info_for_an_unknown_level(pristine_logger, monkeypatch):
    monkeypatch.setenv("AGENTMCP_LOG_LEVEL", "LOUD")

    configure_logging()

    assert pristine_logger.level == logging.INFO


def test_configure_logging_attaches_one_handler_and_stops_propagation(pristine_logger):
    configure_logging()

    assert len(pristine_logger.handlers) == 1
    assert pristine_logger.propagate is False


def test_configure_logging_called_twice_does_not_duplicate_the_handler(pristine_logger):
    configure_logging()
    configure_logging()

    assert len(pristine_logger.handlers) == 1


def test_configure_logging_called_again_still_updates_the_level(pristine_logger, monkeypatch):
    configure_logging()
    monkeypatch.setenv("AGENTMCP_LOG_LEVEL", "DEBUG")

    configure_logging()

    assert pristine_logger.level == logging.DEBUG


def test_configured_handler_writes_time_level_logger_name_and_message(pristine_logger):
    configure_logging()
    stream = io.StringIO()
    pristine_logger.handlers[0].setStream(stream)

    pristine_logger.info("hello there")

    line = stream.getvalue().strip()
    assert line.endswith("INFO agentmcp: hello there")
    assert line[:4].isdigit()  # starts with the year of the timestamp


def test_configure_logging_respects_the_level_when_writing(pristine_logger, monkeypatch):
    monkeypatch.setenv("AGENTMCP_LOG_LEVEL", "ERROR")
    configure_logging()
    stream = io.StringIO()
    pristine_logger.handlers[0].setStream(stream)

    pristine_logger.info("quiet")
    pristine_logger.error("loud")

    assert "quiet" not in stream.getvalue()
    assert "loud" in stream.getvalue()


# --- redact ----------------------------------------------------------------

def test_redact_keeps_tuples_as_tuples_and_redacts_inside_them():
    value = ("keep", {"access_token": "secret-value", "name": "x"})

    result = redact(value)

    assert result == ("keep", {"access_token": "***", "name": "x"})
    assert isinstance(result, tuple)


def test_redact_reaches_tuples_nested_in_lists_and_dicts():
    value = {"items": [({"password": "hunter2"},)]}

    result = redact(value)

    assert result == {"items": [({"password": "***"},)]}


# --- request logging helpers ----------------------------------------------

def _jwt_with_payload(payload_bytes: bytes) -> str:
    def enc(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f"{enc(b'{}')}.{enc(payload_bytes)}.c2ln"


def _rpc(*methods) -> bytes:
    return json.dumps([{"jsonrpc": "2.0", "id": i, "method": m} for i, m in enumerate(methods)]).encode()


INIT_BODY = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode()


def _scope(*, method="POST", path="/mcp", headers=None, client=("10.0.0.1", 1234)):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return {
        "type": "http", "method": method, "path": path, "query_string": b"",
        "headers": raw, "client": client,
    }


async def _run(app, *, scope=None, receive=None, oauth=True):
    """Drive the middleware once and return the messages the client would see."""
    sent = []

    async def default_receive():
        return {"type": "http.request", "body": INIT_BODY, "more_body": False}

    async def send(message):
        sent.append(message)

    middleware = MCPRequestLogMiddleware(app, oauth=oauth)
    await middleware(scope or _scope(), receive or default_receive, send)
    return sent


def _responding(status: int, *, read_body: bool = True):
    async def app(scope, receive, send):
        if read_body:
            await receive()
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})
    return app


def _body_receive(body: bytes):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    return receive


# --- JWT and body parsing --------------------------------------------------

@pytest.mark.parametrize(
    "token",
    [
        pytest.param("opaque-token-with-no-dots", id="no_dots"),
        pytest.param("only.two", id="two_parts"),
        pytest.param("a.b.c.d", id="four_parts"),
    ],
)
async def test_token_that_is_not_a_three_part_jwt_logs_no_identity(logs, token):
    await _run(_responding(401), scope=_scope(headers={"authorization": f"Bearer {token}"}))

    assert "client_id=- tenant=-" in logs[0]
    assert "claims=unverified" not in logs[0]


async def test_jwt_whose_payload_is_not_base64_logs_no_identity(logs):
    token = "header.%%%not-base64%%%.sig"

    await _run(_responding(401), scope=_scope(headers={"authorization": f"Bearer {token}"}))

    assert "client_id=- tenant=-" in logs[0]


async def test_jwt_whose_payload_is_not_json_logs_no_identity(logs):
    token = _jwt_with_payload(b"this is not json")

    await _run(_responding(401), scope=_scope(headers={"authorization": f"Bearer {token}"}))

    assert "client_id=- tenant=-" in logs[0]


async def test_jwt_whose_payload_is_a_json_list_logs_no_identity(logs):
    token = _jwt_with_payload(b'["client_id", "acme"]')

    await _run(_responding(401), scope=_scope(headers={"authorization": f"Bearer {token}"}))

    assert "client_id=- tenant=-" in logs[0]
    assert "claims=unverified" not in logs[0]


async def test_empty_post_body_logs_no_rpc(logs):
    await _run(_responding(200), receive=_body_receive(b""))

    assert "rpc=-" in logs[0]


async def test_post_body_that_is_not_json_logs_no_rpc(logs):
    await _run(_responding(200), receive=_body_receive(b"this is not json"))

    assert "rpc=-" in logs[0]


async def test_batched_request_logs_every_method_in_order(logs):
    await _run(_responding(200), receive=_body_receive(_rpc("initialize", "tools/list")))

    assert "rpc=initialize,tools/list" in logs[0]


async def test_batch_entries_that_are_not_objects_are_skipped(logs):
    body = json.dumps(["junk", 7, None, {"method": "tools/list"}]).encode()

    await _run(_responding(200), receive=_body_receive(body))

    assert "rpc=tools/list" in logs[0]


async def test_batch_entries_with_a_non_text_method_are_skipped(logs):
    body = json.dumps([{"method": 42}, {"method": None}, {"method": "ping"}]).encode()

    await _run(_responding(200), receive=_body_receive(body))

    assert "rpc=ping" in logs[0]


async def test_body_with_no_usable_method_logs_no_rpc(logs):
    body = json.dumps({"jsonrpc": "2.0", "id": 1}).encode()

    await _run(_responding(200), receive=_body_receive(body))

    assert "rpc=-" in logs[0]


async def test_tool_call_without_a_text_name_logs_just_the_method(logs):
    body = json.dumps({"method": "tools/call", "params": {"name": 5}}).encode()

    await _run(_responding(200), receive=_body_receive(body))

    assert "rpc=tools/call " in logs[0]


async def test_get_request_is_logged_without_reading_a_body(logs):
    async def never_called():
        raise AssertionError("receive must not be wrapped for GET")

    await _run(_responding(200, read_body=False), scope=_scope(method="GET"), receive=never_called)

    assert "MCP request" in logs[0] and "GET /mcp" in logs[0] and "rpc=-" in logs[0]


# --- client address --------------------------------------------------------

async def test_first_forwarded_for_address_is_logged_as_the_client_ip(logs):
    headers = {"x-forwarded-for": "203.0.113.9, 10.1.1.1, 10.2.2.2"}

    await _run(_responding(401), scope=_scope(headers=headers))

    assert "ip=203.0.113.9 " in logs[0]


async def test_connection_address_is_logged_when_there_is_no_forwarded_for_header(logs):
    await _run(_responding(401), scope=_scope(client=("198.51.100.4", 5555)))

    assert "ip=198.51.100.4 " in logs[0]


async def test_ip_is_a_dash_when_the_client_address_is_unknown(logs):
    await _run(_responding(401), scope=_scope(client=None))

    assert "ip=- " in logs[0]


async def test_forwarded_for_value_is_escaped_in_the_log_line(logs):
    headers = {"x-forwarded-for": 'evil"\\inject'}

    await _run(_responding(401), scope=_scope(headers=headers))

    assert len(logs) == 1
    assert "\n" not in logs[0]
    assert 'ip=evil\\"\\\\inject ' in logs[0]


# --- body capture before a 401 --------------------------------------------

def _rejecting_app_that_never_reads():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 401, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})
    return app


async def test_empty_body_streamed_in_chunks_before_a_401_logs_no_rpc(logs):
    messages = [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive():
        return messages[0]

    await _run(_rejecting_app_that_never_reads(), receive=receive)

    assert "status=401" in logs[0] and "rpc=-" in logs[0]


async def test_capture_gives_up_when_another_reader_holds_the_body_stream(logs, monkeypatch):
    monkeypatch.setattr("request_logging._EARLY_REJECTION_CAPTURE_TIMEOUT_SECONDS", 0.01)
    release = asyncio.Event()
    body_message = {"type": "http.request", "body": INIT_BODY, "more_body": False}
    delivered = []

    async def receive():
        await release.wait()
        return body_message

    async def app(scope, app_receive, send):
        reader = asyncio.create_task(app_receive())
        await asyncio.sleep(0)  # let the reader start waiting on the body
        await send({"type": "http.response.start", "status": 401, "headers": []})
        release.set()
        delivered.append(await reader)

    sent = await asyncio.wait_for(_run(app, receive=receive), timeout=2)

    assert "status=401" in logs[0] and "rpc=-" in logs[0]
    assert delivered == [body_message]
    assert sent[0]["status"] == 401


async def test_capture_with_no_time_allowed_reads_nothing_and_loses_no_data(logs, monkeypatch):
    monkeypatch.setattr("request_logging._EARLY_REJECTION_CAPTURE_TIMEOUT_SECONDS", 0)
    body_message = {"type": "http.request", "body": INIT_BODY, "more_body": False}
    replayed = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 401, "headers": []})
        replayed.append(await receive())

    await _run(app, receive=_body_receive(INIT_BODY))

    assert "status=401" in logs[0] and "rpc=-" in logs[0]
    assert replayed == [body_message]


async def test_capture_stops_after_a_fixed_number_of_events_and_replays_them_all(logs):
    chunk = {"type": "http.request", "body": b"a", "more_body": True}
    replayed = []

    async def receive():
        return chunk

    async def app(scope, app_receive, send):
        await send({"type": "http.response.start", "status": 401, "headers": []})
        for _ in range(128):
            replayed.append(await app_receive())

    await _run(app, receive=receive)

    assert "status=401" in logs[0] and "rpc=-" in logs[0]
    assert replayed == [chunk] * 128


async def test_failure_while_reading_the_body_before_a_401_does_not_stop_the_response(logs):
    async def receive():
        raise ConnectionResetError("client went away")

    sent = await _run(_rejecting_app_that_never_reads(), receive=receive)

    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    request_line = next(line for line in logs if line.startswith("MCP request without token"))
    assert "status=401" in request_line and "rpc=-" in request_line
    assert "MCP request body capture before 401 failed" in logs


async def test_malformed_message_while_capturing_does_not_stop_the_response(logs):
    async def receive():
        return {}  # no "type" key

    sent = await _run(_rejecting_app_that_never_reads(), receive=receive)

    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    request_line = next(line for line in logs if line.startswith("MCP request without token"))
    assert "status=401" in request_line
    assert "MCP request body capture before 401 failed" in logs


# --- logging must never break a request -----------------------------------

class _UnprintableMethod:
    """A request method whose text form fails, which makes the log line fail."""

    def __eq__(self, other):
        return False

    def __str__(self):
        raise RuntimeError("cannot render")


async def test_failure_to_write_the_log_line_does_not_break_the_response(logs):
    scope = _scope()
    scope["method"] = _UnprintableMethod()

    sent = await _run(_responding(200, read_body=False), scope=scope)

    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert "MCP request logging failed" in logs


async def test_failure_to_write_the_log_line_does_not_hide_an_application_error(logs):
    scope = _scope()
    scope["method"] = _UnprintableMethod()

    async def app(scope, receive, send):
        raise ValueError("app broke")

    with pytest.raises(ValueError, match="app broke"):
        await _run(app, scope=scope)

    assert "MCP request logging failed" in logs


async def test_application_error_after_the_response_started_is_reraised_without_a_second_log_line(logs):
    async def app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("died mid-stream")

    with pytest.raises(RuntimeError, match="died mid-stream"):
        await _run(app)

    assert len(logs) == 1
    assert "status=200" in logs[0]
