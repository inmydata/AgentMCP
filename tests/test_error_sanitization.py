"""Regression tests for GHSA-xv3j-j949-g8wx (issue #7).

Tool handlers must not return raw exception strings to the MCP caller. Two
guarantees are checked here:

  1. The ``errors`` helpers behave as advertised: unexpected exceptions are
     logged (with a correlation id) and replaced with a generic payload;
     ``UserFacingError`` propagates its message verbatim.
  2. Representative tool handlers in ``mcp_utils`` actually use the helpers
     end-to-end -- forcing an unexpected exception inside a tool returns
     ``"internal error"`` plus a correlation id, and crucially does *not*
     contain the original exception message.
"""
import json
import logging
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from errors import UserFacingError, log_tool_error, tool_error_response  # noqa: E402
from mcp_logging import logger as agentmcp_logger  # noqa: E402
from mcp_utils import mcp_utils  # noqa: E402


CID_RE = re.compile(r"^[0-9a-f]{8}$")


@pytest.fixture
def utils_instance():
    return mcp_utils(
        api_key="test",
        tenant="test-tenant",
        calendar="test-cal",
        user="test-user",
        session_id="test-session",
        server=None,
    )


@pytest.fixture
def captured_agentmcp_logs():
    """The `agentmcp` logger has `propagate=False`, so caplog can't observe it
    by default. This fixture attaches a buffer handler, restores it, and
    returns the captured log records."""
    records: list[logging.LogRecord] = []

    class _Buffer(logging.Handler):
        def emit(self, record):  # noqa: D401 -- handler hook
            records.append(record)

    handler = _Buffer(level=logging.DEBUG)
    previous_level = agentmcp_logger.level
    agentmcp_logger.setLevel(logging.DEBUG)
    agentmcp_logger.addHandler(handler)
    try:
        yield records
    finally:
        agentmcp_logger.removeHandler(handler)
        agentmcp_logger.setLevel(previous_level)


def _joined_log_text(records):
    return "\n".join(rec.getMessage() + "\n" + (rec.exc_text or "") for rec in records)


# ---------------------------------------------------------------------------
# errors module -- unit tests
# ---------------------------------------------------------------------------

class TestToolErrorResponse:
    def test_returns_generic_payload_with_correlation_id(self):
        out = tool_error_response(RuntimeError("secret-path /etc/foo"), context="unit-test")
        parsed = json.loads(out)
        assert parsed["error"] == "internal error"
        assert CID_RE.match(parsed["correlation_id"])

    def test_does_not_leak_original_message(self):
        secret = "SECRET-MARKER-/etc/passwd:DROP TABLE users"
        out = tool_error_response(RuntimeError(secret), context="unit-test")
        assert secret not in out
        assert "RuntimeError" not in out
        assert "/etc/passwd" not in out

    def test_does_not_leak_repr_or_classname(self):
        class CustomException(Exception):
            pass

        out = tool_error_response(CustomException("nope"), context="unit-test")
        assert "CustomException" not in out
        assert "nope" not in out

    def test_each_call_returns_fresh_correlation_id(self):
        exc = RuntimeError("x")
        first = json.loads(tool_error_response(exc, context="a"))
        second = json.loads(tool_error_response(exc, context="b"))
        assert first["correlation_id"] != second["correlation_id"]

    def test_logs_exception_with_correlation_id_and_context(self, captured_agentmcp_logs):
        out = tool_error_response(RuntimeError("internal-only"), context="my_tool")
        cid = json.loads(out)["correlation_id"]
        all_log_text = _joined_log_text(captured_agentmcp_logs)
        assert cid in all_log_text
        assert "my_tool" in all_log_text
        # The full exception detail must reach the server log (just not the caller).
        assert "internal-only" in all_log_text


class TestLogToolError:
    def test_returns_bare_correlation_id(self):
        cid = log_tool_error(RuntimeError("anything"), context="bare-text-tool")
        assert CID_RE.match(cid)

    def test_logs_exception(self, captured_agentmcp_logs):
        log_tool_error(RuntimeError("traceback-content"), context="bare-text-tool")
        all_log_text = _joined_log_text(captured_agentmcp_logs)
        assert "traceback-content" in all_log_text


class TestUserFacingError:
    def test_is_exception_subclass(self):
        assert issubclass(UserFacingError, Exception)

    def test_str_preserves_message(self):
        assert str(UserFacingError("user-visible reason")) == "user-visible reason"


# ---------------------------------------------------------------------------
# end-to-end: a forced unexpected exception in a real tool handler must be
# sanitised, while a UserFacingError raised inside the same try block must
# pass through verbatim.
# ---------------------------------------------------------------------------

class TestMcpUtilsToolHandlers:
    @pytest.mark.asyncio
    async def test_get_rows_sanitises_unexpected_exception(self, utils_instance, monkeypatch):
        secret = "DB-PATH=/var/lib/inmydata/SECRETDB and password=hunter2"

        def boom(*_args, **_kwargs):
            raise RuntimeError(secret)

        monkeypatch.setattr("mcp_utils.StructuredDataDriver", boom)

        result_json = await utils_instance.get_rows(
            subject="Sales", select=["Region"], where=None
        )
        result = json.loads(result_json)

        assert result["error"] == "internal error"
        assert CID_RE.match(result["correlation_id"])
        assert secret not in result_json
        assert "SECRETDB" not in result_json
        assert "hunter2" not in result_json
        assert "RuntimeError" not in result_json

    @pytest.mark.asyncio
    async def test_get_rows_preserves_user_facing_validation_error(self, utils_instance):
        # parse_where raises UserFacingError("Filter at index 0 is missing 'field'")
        # -- the message names what the caller did wrong and is safe to surface.
        result_json = await utils_instance.get_rows(
            subject="Sales",
            select=["Region"],
            where=[{"op": "equals", "value": 1}],  # no "field" key
        )
        result = json.loads(result_json)
        assert "missing 'field'" in result["error"]
        assert "correlation_id" not in result

    @pytest.mark.asyncio
    async def test_get_top_n_sanitises_unexpected_exception(self, utils_instance, monkeypatch):
        secret = "STACKTRACE-LEAK<<<"

        def boom(*_args, **_kwargs):
            raise RuntimeError(secret)

        monkeypatch.setattr("mcp_utils.StructuredDataDriver", boom)

        result_json = await utils_instance.get_top_n(
            subject="Sales", group_by="Region", order_by="Sales", n=5, where=None
        )
        result = json.loads(result_json)

        assert result["error"] == "internal error"
        assert CID_RE.match(result["correlation_id"])
        assert secret not in result_json

    def test_get_schema_sanitises_unexpected_exception(self, utils_instance, monkeypatch):
        secret = "schema-internal-state-LEAKED"

        def boom(*_args, **_kwargs):
            raise RuntimeError(secret)

        monkeypatch.setattr("mcp_utils.StructuredDataDriver", boom)

        result_json = utils_instance.get_schema()
        result = json.loads(result_json)

        # get_schema previously returned a bare "Error retrieving subjects: ..." string;
        # it must now return JSON with the generic shape.
        assert result["error"] == "internal error"
        assert CID_RE.match(result["correlation_id"])
        assert secret not in result_json

    @pytest.mark.asyncio
    async def test_get_financial_periods_sanitises_unexpected_exception(
        self, utils_instance, monkeypatch
    ):
        secret = "calendar-internal-LEAKED"

        # CalendarAssistant is imported lazily inside the method; patch the source.
        class FakeCalendarModule:
            class CalendarAssistant:
                def __init__(self, *_args, **_kwargs):
                    raise RuntimeError(secret)

        monkeypatch.setitem(sys.modules, "inmydata.CalendarAssistant", FakeCalendarModule)

        result_json = await utils_instance.get_financial_periods(target_date=None)
        result = json.loads(result_json)

        assert result["error"] == "internal error"
        assert CID_RE.match(result["correlation_id"])
        assert secret not in result_json


# ---------------------------------------------------------------------------
# Static guard: every tool-handler `except Exception` in the project should
# route through tool_error_response / log_tool_error, not return str(exc).
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent

# Files in scope for the static sweep. agentic_rag_client.py is upstream-IO
# and intentionally constructs AgenticRagError messages from response detail;
# it is exempt because the tool layer (agentic_rag_tool.py) catches those
# separately as user-facing.
TOOL_HANDLER_FILES = [
    REPO_ROOT / "server.py",
    REPO_ROOT / "server_remote.py",
    REPO_ROOT / "mcp_utils.py",
    REPO_ROOT / "agentic_rag_tool.py",
]


def _scan_after_except_exception(source: str):
    """Yield the textual body of every `except Exception ...:` block (best-effort)."""
    import ast

    tree = ast.parse(source)
    lines = source.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        exc_type = node.type
        # Only flag broad catches: `except Exception as ...:`. `except UserFacingError`
        # and `except AgenticRagError` are deliberately user-facing.
        if not (isinstance(exc_type, ast.Name) and exc_type.id == "Exception"):
            continue
        end_line = getattr(node, "end_lineno", None) or node.lineno
        body = "\n".join(lines[node.lineno - 1 : end_line])
        yield node.lineno, body


@pytest.mark.parametrize("path", TOOL_HANDLER_FILES, ids=lambda p: p.name)
def test_no_raw_exception_string_in_tool_handlers(path):
    """`except Exception` blocks in tool-handler modules must not return raw
    exception text. The replacement helpers are tool_error_response (for
    JSON-returning tools) and log_tool_error (for text-returning tools)."""
    source = path.read_text(encoding="utf-8")
    offenders = []
    for lineno, body in _scan_after_except_exception(source):
        # Patterns the advisory calls out. The `{e}` / `{exc}` interpolation
        # forms cover plain-text returns, JSON returns, and ctx.error() sends
        # alike -- anywhere the original exception variable reaches the wire.
        bad_patterns = [
            'str(e)',
            'str(exc)',
            '{e}',
            '{exc}',
            '"error": str(',
            '"errorX"',
        ]
        for needle in bad_patterns:
            if needle in body:
                offenders.append(f"{path.name}:{lineno}: contains {needle!r}")
    assert not offenders, (
        "Tool handler `except Exception` blocks must use tool_error_response / "
        "log_tool_error instead of returning raw exception text. Offenders:\n  "
        + "\n  ".join(offenders)
    )
