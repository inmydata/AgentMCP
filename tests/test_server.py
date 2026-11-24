"""Tests for the local (stdio) MCP server's tool handlers in ``server.py``.

Importing ``server`` has side effects: it loads ``.env`` over the environment,
configures logging, can start a debugger when ``MCP_DEBUG=1``, and registers
the RAG tool. The ``server`` fixture below neutralises all of that, imports a
fresh copy, and removes it from ``sys.modules`` afterwards.

The data layer (``mcp_utils``) is replaced by a fake, because it talks to the
inmydata service. What is tested is the handlers' own behaviour: argument
validation, parameter defaulting, and how failures are reported to the caller.
"""
import importlib
import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dotenv  # noqa: E402
import mcp_logging  # noqa: E402
from errors import UserFacingError  # noqa: E402
from mcp_logging import logger as agentmcp_logger  # noqa: E402

CONFIG_ENV_VARS = (
    "MCP_DEBUG",
    "INMYDATA_API_KEY",
    "INMYDATA_TENANT",
    "INMYDATA_SERVER",
    "INMYDATA_CALENDAR",
    "INMYDATA_USER",
    "INMYDATA_SESSION_ID",
    "AGENTIC_RAG_BASE_URL",
    "AGENTIC_RAG_PSK",
    "AGENTIC_RAG_DEFAULT_EXTERNAL_ID",
    "AGENTIC_RAG_CACHE_FILE",
    "AGENTIC_RAG_TENANT_PREFIX",
)


@pytest.fixture
def server(monkeypatch):
    """A freshly imported ``server`` module with no import-time side effects."""
    for name in CONFIG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Stop the module reading the developer's real .env or touching log handlers.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(mcp_logging, "configure_logging", lambda *a, **k: None)
    sys.modules.pop("server", None)
    module = importlib.import_module("server")
    try:
        yield module
    finally:
        sys.modules.pop("server", None)


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


class FakeUtils:
    """Stands in for ``mcp_utils``. Each method returns ``results[name]`` or raises it."""

    def __init__(self, **results):
        self.results = results
        self.calls: list[tuple] = []

    def _answer(self, name, *args):
        self.calls.append((name, *args))
        result = self.results.get(name, "{}")
        if isinstance(result, BaseException):
            raise result
        return result

    async def get_rows(self, *args):
        return self._answer("get_rows", *args)

    async def get_top_n(self, *args):
        return self._answer("get_top_n", *args)

    async def query_results(self, *args):
        return self._answer("query_results", *args)

    async def get_answer(self, *args):
        return self._answer("get_answer", *args)

    def get_schema(self, *args):
        return self._answer("get_schema", *args)

    async def get_financial_periods(self, *args):
        return self._answer("get_financial_periods", *args)

    async def get_calendar_period_date_range(self, *args):
        return self._answer("get_calendar_period_date_range", *args)

    def last_call(self, name):
        return [c for c in self.calls if c[0] == name][-1][1:]


def _use(server, monkeypatch, **results) -> FakeUtils:
    fake = FakeUtils(**results)
    monkeypatch.setattr(server, "utils", lambda: fake)
    return fake


class FakeContext:
    def __init__(self):
        self.errors: list[str] = []

    async def error(self, message):
        self.errors.append(message)


def _assert_sanitised_error(response: str, secret: str):
    payload = json.loads(response)
    assert payload["error"] == "internal error"
    assert len(payload["correlation_id"]) == 8
    assert secret not in response


# --- import and registration ----------------------------------------------

async def test_all_tools_are_registered_on_the_server(server):
    tools = await server.mcp.list_tools()

    assert {t.name for t in tools} == {
        "get_rows_fast",
        "get_top_n_fast",
        "query_results_fast",
        "get_answer_slow",
        "get_schema",
        "get_financial_periods",
        "get_calendar_period_date_range",
        "agentic_rag_query",
    }


async def test_rag_tool_reports_missing_configuration_when_the_environment_is_empty(server):
    content, _ = await server.mcp.call_tool("agentic_rag_query", {"external_id": "x", "query": "q"})

    assert content[0].text.startswith("error: agentic_rag not configured")


async def test_importing_the_server_does_not_change_the_environment(monkeypatch):
    # The env-reading side of import is covered by the fixture; here we confirm
    # nothing is leaked into os.environ by an import.
    import os

    for name in CONFIG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(mcp_logging, "configure_logging", lambda *a, **k: None)
    sys.modules.pop("server", None)
    before = dict(os.environ)

    importlib.import_module("server")
    sys.modules.pop("server", None)

    assert dict(os.environ) == before


# --- utils() ---------------------------------------------------------------

def test_utils_builds_the_data_layer_from_the_environment(server, monkeypatch):
    monkeypatch.setenv("INMYDATA_API_KEY", "key-123")
    monkeypatch.setenv("INMYDATA_TENANT", "acme")
    monkeypatch.setenv("INMYDATA_SERVER", "eu.example.test")
    monkeypatch.setenv("INMYDATA_CALENDAR", "fiscal")
    monkeypatch.setenv("INMYDATA_USER", "alice")
    monkeypatch.setenv("INMYDATA_SESSION_ID", "sess-1")

    built = server.utils()

    assert (built.api_key, built.tenant, built.server) == ("key-123", "acme", "eu.example.test")
    assert (built.calendar, built.user, built.session_id) == ("fiscal", "alice", "sess-1")


def test_utils_falls_back_to_defaults_when_the_environment_is_empty(server):
    built = server.utils()

    assert built.api_key == ""
    assert built.tenant == ""
    assert built.server == "inmydata.com"
    assert built.calendar == "default"
    assert built.user == "mcp-agent"
    assert built.session_id == "mcp-session"


def test_utils_wraps_a_construction_failure_in_a_runtime_error(server, monkeypatch):
    def broken(*args):
        raise ValueError("bad config")

    monkeypatch.setattr(server, "mcp_utils", broken)

    with pytest.raises(RuntimeError, match="Error initializing mcp_utils") as excinfo:
        server.utils()

    assert isinstance(excinfo.value.__cause__, ValueError)


# --- get_rows_fast ---------------------------------------------------------

async def test_get_rows_fast_returns_what_the_data_layer_returns(server, monkeypatch):
    fake = _use(server, monkeypatch, get_rows='{"row_count": 2, "data": []}')

    result = await server.get_rows_fast("Sales", ["Region"], [{"field": "Year", "op": "equals", "value": 2025}])

    assert result == '{"row_count": 2, "data": []}'
    assert fake.last_call("get_rows") == (
        "Sales", ["Region"], [{"field": "Year", "op": "equals", "value": 2025}],
    )


async def test_get_rows_fast_without_a_filter_passes_an_empty_filter_list(server, monkeypatch):
    fake = _use(server, monkeypatch)

    await server.get_rows_fast("Sales", ["Region"])

    assert fake.last_call("get_rows")[2] == []


@pytest.mark.parametrize(
    "arguments, expected",
    [
        pytest.param({"select": ["Region"]}, "subject parameter is required", id="no_subject"),
        pytest.param({"subject": "Sales"}, "select parameter is required", id="no_select"),
        pytest.param({"subject": "", "select": ["Region"]}, "subject parameter is required", id="blank_subject"),
        pytest.param({"subject": "Sales", "select": []}, "select parameter is required", id="empty_select"),
    ],
)
async def test_get_rows_fast_rejects_missing_arguments(server, monkeypatch, arguments, expected):
    fake = _use(server, monkeypatch)

    result = await server.get_rows_fast(**arguments)

    assert expected in json.loads(result)["error"]
    assert fake.calls == []


async def test_get_rows_fast_reports_a_user_facing_error_verbatim(server, monkeypatch):
    _use(server, monkeypatch, get_rows=UserFacingError("Unknown subject 'Foo'"))

    result = await server.get_rows_fast("Foo", ["Region"])

    assert json.loads(result) == {"error": "Unknown subject 'Foo'"}


async def test_get_rows_fast_hides_unexpected_error_detail(server, monkeypatch):
    _use(server, monkeypatch, get_rows=RuntimeError("password=hunter2 at /srv/db"))

    result = await server.get_rows_fast("Sales", ["Region"])

    _assert_sanitised_error(result, "hunter2")


async def test_get_rows_fast_logs_the_full_error_for_operators(server, monkeypatch, logs):
    _use(server, monkeypatch, get_rows=RuntimeError("boom"))

    result = await server.get_rows_fast("Sales", ["Region"])

    cid = json.loads(result)["correlation_id"]
    assert any(f"cid={cid}" in line and "get_rows_fast" in line for line in logs)


# --- get_top_n_fast --------------------------------------------------------

async def test_get_top_n_fast_returns_what_the_data_layer_returns(server, monkeypatch):
    fake = _use(server, monkeypatch, get_top_n='{"row_count": 3}')

    result = await server.get_top_n_fast("Sales", "Region", "Profit", 3, [])

    assert result == '{"row_count": 3}'
    assert fake.last_call("get_top_n") == ("Sales", "Region", "Profit", 3, [])


async def test_get_top_n_fast_defaults_to_ten_rows(server, monkeypatch):
    fake = _use(server, monkeypatch)

    await server.get_top_n_fast("Sales", "Region", "Profit")

    assert fake.last_call("get_top_n")[3] == 10


@pytest.mark.parametrize(
    "arguments, expected",
    [
        pytest.param({"group_by": "Region", "order_by": "Profit"}, "subject parameter is required", id="no_subject"),
        pytest.param({"subject": "Sales", "order_by": "Profit"}, "group_by parameter is required", id="no_group_by"),
        pytest.param({"subject": "Sales", "group_by": "Region"}, "order_by parameter is required", id="no_order_by"),
    ],
)
async def test_get_top_n_fast_rejects_missing_arguments(server, monkeypatch, arguments, expected):
    fake = _use(server, monkeypatch)

    result = await server.get_top_n_fast(**arguments)

    assert json.loads(result) == {"error": expected}
    assert fake.calls == []


async def test_get_top_n_fast_reports_a_user_facing_error_verbatim(server, monkeypatch):
    _use(server, monkeypatch, get_top_n=UserFacingError("n must be positive"))

    result = await server.get_top_n_fast("Sales", "Region", "Profit", -1)

    assert json.loads(result) == {"error": "n must be positive"}


async def test_get_top_n_fast_hides_unexpected_error_detail(server, monkeypatch):
    _use(server, monkeypatch, get_top_n=KeyError("internal_column_xyz"))

    result = await server.get_top_n_fast("Sales", "Region", "Profit")

    _assert_sanitised_error(result, "internal_column_xyz")


# --- query_results_fast ----------------------------------------------------

async def test_query_results_fast_returns_what_the_data_layer_returns(server, monkeypatch):
    fake = _use(server, monkeypatch, query_results='{"rows": [1]}')

    result = await server.query_results_fast("abc123", "SELECT 1 FROM my_table")

    assert result == '{"rows": [1]}'
    assert fake.last_call("query_results") == ("abc123", "SELECT 1 FROM my_table")


@pytest.mark.parametrize(
    "arguments, expected",
    [
        pytest.param({"sql": "SELECT 1"}, "instance_id parameter is required", id="no_instance_id"),
        pytest.param({"instance_id": "abc"}, "sql parameter is required", id="no_sql"),
    ],
)
async def test_query_results_fast_rejects_missing_arguments(server, monkeypatch, arguments, expected):
    fake = _use(server, monkeypatch)

    result = await server.query_results_fast(**arguments)

    assert json.loads(result) == {"error": expected}
    assert fake.calls == []


async def test_query_results_fast_reports_a_user_facing_error_verbatim(server, monkeypatch):
    _use(server, monkeypatch, query_results=UserFacingError("Only SELECT statements are allowed"))

    result = await server.query_results_fast("abc", "DROP TABLE my_table")

    assert json.loads(result) == {"error": "Only SELECT statements are allowed"}


async def test_query_results_fast_hides_unexpected_error_detail(server, monkeypatch):
    _use(server, monkeypatch, query_results=OSError("C:\\secret\\path\\results.duckdb locked"))

    result = await server.query_results_fast("abc", "SELECT 1")

    _assert_sanitised_error(result, "secret")


# --- get_answer_slow -------------------------------------------------------

async def test_get_answer_slow_returns_the_answer_and_passes_the_context_on(server, monkeypatch):
    fake = _use(server, monkeypatch, get_answer='{"answer": "because"}')
    ctx = FakeContext()

    result = await server.get_answer_slow("Why did the north underperform?", ctx)

    assert result == '{"answer": "because"}'
    assert fake.last_call("get_answer") == ("Why did the north underperform?", ctx)


async def test_get_answer_slow_rejects_an_empty_question(server, monkeypatch):
    fake = _use(server, monkeypatch)

    result = await server.get_answer_slow("")

    assert json.loads(result) == {"error": "question parameter is required"}
    assert fake.calls == []


async def test_get_answer_slow_reports_a_user_facing_error_to_the_caller_and_the_context(server, monkeypatch):
    _use(server, monkeypatch, get_answer=UserFacingError("tenant not set"))
    ctx = FakeContext()

    result = await server.get_answer_slow("hello", ctx)

    assert json.loads(result) == {"error": "tenant not set"}
    assert ctx.errors == ["tenant not set"]


async def test_get_answer_slow_works_without_a_context_when_a_user_facing_error_occurs(server, monkeypatch):
    _use(server, monkeypatch, get_answer=UserFacingError("tenant not set"))

    result = await server.get_answer_slow("hello")

    assert json.loads(result) == {"error": "tenant not set"}


async def test_get_answer_slow_hides_unexpected_error_detail_from_caller_and_context(server, monkeypatch):
    _use(server, monkeypatch, get_answer=RuntimeError("token abc-secret rejected by upstream"))
    ctx = FakeContext()

    result = await server.get_answer_slow("hello", ctx)

    _assert_sanitised_error(result, "abc-secret")
    cid = json.loads(result)["correlation_id"]
    assert ctx.errors == [f"internal error (correlation_id: {cid})"]


async def test_get_answer_slow_works_without_a_context_when_an_unexpected_error_occurs(server, monkeypatch):
    _use(server, monkeypatch, get_answer=RuntimeError("boom"))

    result = await server.get_answer_slow("hello")

    assert json.loads(result)["error"] == "internal error"


# --- get_schema ------------------------------------------------------------

def test_get_schema_returns_the_schema_json(server, monkeypatch):
    _use(server, monkeypatch, get_schema='{"subjectsCount": 1}')

    result = server.get_schema()

    assert result == '{"subjectsCount": 1}'


def test_get_schema_reports_a_user_facing_error_verbatim(server, monkeypatch):
    _use(server, monkeypatch, get_schema=UserFacingError("tenant not set"))

    result = server.get_schema()

    assert json.loads(result) == {"error": "tenant not set"}


def test_get_schema_hides_unexpected_error_detail(server, monkeypatch):
    _use(server, monkeypatch, get_schema=ConnectionError("db.internal:5432 refused"))

    result = server.get_schema()

    _assert_sanitised_error(result, "db.internal")


# --- get_financial_periods -------------------------------------------------

async def test_get_financial_periods_passes_the_date_through(server, monkeypatch):
    fake = _use(server, monkeypatch, get_financial_periods='{"periods": "{}"}')

    result = await server.get_financial_periods("2025-03-01")

    assert result == '{"periods": "{}"}'
    assert fake.last_call("get_financial_periods") == ("2025-03-01",)


async def test_get_financial_periods_without_a_date_asks_for_today(server, monkeypatch):
    fake = _use(server, monkeypatch)

    await server.get_financial_periods()

    assert fake.last_call("get_financial_periods") == (None,)


async def test_get_financial_periods_reports_a_user_facing_error_verbatim(server, monkeypatch):
    _use(server, monkeypatch, get_financial_periods=UserFacingError("Invalid date"))

    result = await server.get_financial_periods("not-a-date")

    assert json.loads(result) == {"error": "Invalid date"}


async def test_get_financial_periods_hides_unexpected_error_detail(server, monkeypatch):
    _use(server, monkeypatch, get_financial_periods=RuntimeError("stack detail leak"))

    result = await server.get_financial_periods()

    _assert_sanitised_error(result, "stack detail leak")


# --- get_calendar_period_date_range ---------------------------------------

def _periods(**values) -> str:
    """The shape the data layer returns: periods as a JSON string inside JSON."""
    return json.dumps({"periods": json.dumps(values)})


async def test_date_range_with_all_arguments_does_not_look_up_current_periods(server, monkeypatch):
    fake = _use(server, monkeypatch, get_calendar_period_date_range='{"start_date": "a"}')

    result = await server.get_calendar_period_date_range(2025, 3, "quarter")

    assert result == '{"start_date": "a"}'
    assert fake.last_call("get_calendar_period_date_range") == (2025, 3, "quarter")
    assert [c[0] for c in fake.calls] == ["get_calendar_period_date_range"]


async def test_date_range_with_no_arguments_uses_the_current_financial_year_and_month(server, monkeypatch):
    fake = _use(
        server, monkeypatch,
        get_financial_periods=_periods(FinancialYear=2025, Month=7),
        get_calendar_period_date_range='{"ok": true}',
    )

    await server.get_calendar_period_date_range()

    assert fake.last_call("get_calendar_period_date_range") == (2025, 7, "month")


async def test_date_range_fills_only_the_missing_arguments(server, monkeypatch):
    fake = _use(
        server, monkeypatch,
        get_financial_periods=_periods(FinancialYear=2025, Month=7),
        get_calendar_period_date_range='{"ok": true}',
    )

    await server.get_calendar_period_date_range(financial_year=2020, period_type="week")

    assert fake.last_call("get_calendar_period_date_range") == (2020, 7, "week")


async def test_date_range_with_only_the_period_number_given_fills_year_and_type(server, monkeypatch):
    fake = _use(
        server, monkeypatch,
        get_financial_periods=_periods(FinancialYear=2025, Month=7),
        get_calendar_period_date_range='{"ok": true}',
    )

    await server.get_calendar_period_date_range(period_number=3)

    assert fake.last_call("get_calendar_period_date_range") == (2025, 3, "month")


async def test_date_range_accepts_alternative_period_key_names(server, monkeypatch):
    fake = _use(
        server, monkeypatch,
        get_financial_periods=_periods(Year=2024, Period=4),
        get_calendar_period_date_range='{"ok": true}',
    )

    await server.get_calendar_period_date_range()

    assert fake.last_call("get_calendar_period_date_range") == (2024, 4, "month")


async def test_date_range_accepts_periods_that_are_already_a_json_object(server, monkeypatch):
    fake = _use(
        server, monkeypatch,
        get_financial_periods=json.dumps({"periods": {"FinancialYear": 2023, "Month": 2}}),
        get_calendar_period_date_range='{"ok": true}',
    )

    await server.get_calendar_period_date_range()

    assert fake.last_call("get_calendar_period_date_range") == (2023, 2, "month")


async def test_date_range_defaults_the_period_number_to_one_when_the_periods_lack_it(server, monkeypatch):
    fake = _use(
        server, monkeypatch,
        get_financial_periods=_periods(FinancialYear=2025),
        get_calendar_period_date_range='{"ok": true}',
    )

    await server.get_calendar_period_date_range()

    assert fake.last_call("get_calendar_period_date_range") == (2025, 1, "month")


async def test_date_range_returns_the_lookup_error_when_current_periods_cannot_be_fetched(server, monkeypatch):
    fake = _use(server, monkeypatch, get_financial_periods='{"error": "calendar not found"}')

    result = await server.get_calendar_period_date_range()

    assert json.loads(result) == {"error": "calendar not found"}
    assert [c[0] for c in fake.calls] == ["get_financial_periods"]


async def test_date_range_reports_when_the_financial_year_cannot_be_determined(server, monkeypatch):
    _use(server, monkeypatch, get_financial_periods=_periods())

    result = await server.get_calendar_period_date_range()

    assert json.loads(result) == {"error": "Could not determine financial_year"}


async def test_date_range_reports_when_the_periods_text_is_not_valid_json(server, monkeypatch):
    _use(server, monkeypatch, get_financial_periods=json.dumps({"periods": "not json"}))

    result = await server.get_calendar_period_date_range()

    assert json.loads(result) == {"error": "Could not determine financial_year"}


async def test_date_range_reports_when_the_period_number_is_zero(server, monkeypatch):
    fake = _use(server, monkeypatch)

    result = await server.get_calendar_period_date_range(2025, 0, "month")

    assert json.loads(result) == {"error": "Could not determine period_number"}
    assert fake.calls == []


async def test_date_range_reports_when_the_financial_year_is_zero(server, monkeypatch):
    fake = _use(server, monkeypatch)

    result = await server.get_calendar_period_date_range(0, 1, "month")

    assert json.loads(result) == {"error": "Could not determine financial_year"}
    assert fake.calls == []


async def test_date_range_rejects_a_blank_period_type(server, monkeypatch):
    fake = _use(server, monkeypatch)

    result = await server.get_calendar_period_date_range(2025, 1, "")

    assert json.loads(result) == {"error": "period_type parameter is required"}
    assert fake.calls == []


async def test_date_range_reports_a_user_facing_error_verbatim(server, monkeypatch):
    _use(server, monkeypatch, get_calendar_period_date_range=UserFacingError("Unknown period type 'decade'"))

    result = await server.get_calendar_period_date_range(2025, 1, "decade")

    assert json.loads(result) == {"error": "Unknown period type 'decade'"}


async def test_date_range_hides_unexpected_error_detail(server, monkeypatch):
    _use(server, monkeypatch, get_calendar_period_date_range=RuntimeError("sql: SELECT secret_col"))

    result = await server.get_calendar_period_date_range(2025, 1, "month")

    _assert_sanitised_error(result, "secret_col")


async def test_date_range_hides_detail_when_the_current_periods_lookup_crashes(server, monkeypatch):
    _use(server, monkeypatch, get_financial_periods=RuntimeError("upstream exploded"))

    result = await server.get_calendar_period_date_range()

    _assert_sanitised_error(result, "upstream exploded")
