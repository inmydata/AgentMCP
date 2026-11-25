"""
Tests for the tool methods in the second half of mcp_utils: get_top_n error
handling, query_results, get_answer, get_schema (and its dashboard hints),
get_financial_periods and get_calendar_period_date_range.

The inmydata SDK drivers talk to a remote service, so they are replaced with
small fakes. DuckDB and the JSON handling run for real.

Run with:  python -m pytest tests/test_mcp_utils_tools.py -v
"""
import asyncio
import json
import sys
import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inmydata.CalendarAssistant import FinancialPeriodDetails  # noqa: E402
from mcp_utils import _tenant_namespace, mcp_utils  # noqa: E402

TEST_TENANT = "test"
SECRET_TEXT = "SECRET-internal-detail"


# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------

@pytest.fixture
def utils_instance():
    return mcp_utils(
        api_key="test",
        tenant=TEST_TENANT,
        calendar="test",
        user="test",
        session_id="test",
        server=None,
    )


@pytest.fixture
def no_tenant_instance():
    return mcp_utils(
        api_key="test", tenant="", calendar="test",
        user="test", session_id="test", server=None,
    )


@pytest.fixture
def no_calendar_instance():
    return mcp_utils(
        api_key="test", tenant=TEST_TENANT, calendar="",
        user="test", session_id="test", server=None,
    )


@pytest.fixture
def duckdb_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_DUCKDB_LOCATION", str(tmp_path))
    return tmp_path


def _create_instance(duckdb_dir, sql="CREATE TABLE my_table AS SELECT * FROM range(3) t(a)"):
    """Create a real DuckDB file for the test tenant and return its instance id."""
    instance_id = str(uuid.uuid4())
    path = duckdb_dir / _tenant_namespace(TEST_TENANT) / f"{instance_id}.duckdb"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute(sql)
    finally:
        con.close()
    return instance_id


NOT_FOUND = (
    "No saved result was found for this instance_id. Run get_rows_fast or "
    "get_top_n_fast again to get a new one."
)


class FakeContext:
    """Records progress reports and errors the way an MCP Context would receive them."""

    def __init__(self):
        self.progress = []
        self.errors = []

    async def report_progress(self, progress, message=None):
        self.progress.append((progress, message))

    async def error(self, message):
        self.errors.append(message)


class FakeConversationalDriver:
    """Stands in for ConversationalDataDriver; configure via class attributes in each test."""

    answer = SimpleNamespace(answer="42 stores", subject="Sales")
    raises = None
    updates = ()

    def __init__(self, *args, **kwargs):
        self._callbacks = {}

    def on(self, event_name, callback):
        self._callbacks[event_name] = callback

    async def get_answer(self, question):
        cb = self._callbacks.get("ai_question_update")
        for message in self.updates:
            # The real SDK reports status from a worker thread.
            await asyncio.to_thread(cb, self, message)
        # Let the loop run the progress coroutines scheduled from the thread.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if self.raises is not None:
            raise self.raises
        return self.answer


def _install_conversational_driver(monkeypatch, *, answer=None, raises=None, updates=()):
    class Driver(FakeConversationalDriver):
        pass

    if answer is not None:
        Driver.answer = answer
    Driver.raises = raises
    Driver.updates = updates
    monkeypatch.setattr("inmydata.ConversationalData.ConversationalDataDriver", Driver)


def _install_structured_driver(monkeypatch, *, schema=None, raises=None, rows=None):
    class Driver:
        def __init__(self, *args, **kwargs):
            pass

        def get_schema(self, caller):
            if raises is not None:
                raise raises
            return schema

        def get_data(self, *args, **kwargs):
            if raises is not None:
                raise raises
            return rows

    monkeypatch.setattr("mcp_utils.StructuredDataDriver", Driver)


def _install_calendar_assistant(monkeypatch, *, periods=None, date_range=None, raises=None):
    """Replace CalendarAssistant. `date_range` is a (start, end) tuple or None."""
    class Assistant:
        def __init__(self, *args, **kwargs):
            pass

        def get_financial_periods(self, input_date):
            if raises is not None:
                raise raises
            return periods

        def get_calendar_period_date_range(self, year, period_number, period_type):
            if raises is not None:
                raise raises
            if date_range is None:
                return None
            return SimpleNamespace(StartDate=date_range[0], EndDate=date_range[1])

    monkeypatch.setattr("inmydata.CalendarAssistant.CalendarAssistant", Assistant)


# ---------------------------------------------------------------------------
# get_top_n: caller mistakes
# ---------------------------------------------------------------------------

class TestGetTopNCallerMistakes:
    async def test_reports_a_filter_with_no_field_to_the_caller(self, utils_instance, monkeypatch):
        # Arrange
        _install_structured_driver(monkeypatch, rows=None)

        # Act
        result = json.loads(await utils_instance.get_top_n(
            "Sales", "Region", "Sales Value", 5, where=[{"op": "equals", "value": "x"}],
        ))

        # Assert
        assert result == {"error": "Filter at index 0 is missing 'field'"}

    async def test_hides_upstream_error_details_from_the_caller(self, utils_instance, monkeypatch):
        # Arrange
        _install_structured_driver(monkeypatch, raises=RuntimeError(SECRET_TEXT))

        # Act
        raw = await utils_instance.get_top_n("Sales", "Region", "Sales Value", 5)
        result = json.loads(raw)

        # Assert
        assert result["error"] == "internal error"
        assert result["correlation_id"]
        assert SECRET_TEXT not in raw


# ---------------------------------------------------------------------------
# query_results
# ---------------------------------------------------------------------------

class TestQueryResults:
    async def test_returns_rows_columns_and_instance_id_for_a_valid_query(self, utils_instance, duckdb_dir):
        # Arrange
        instance_id = _create_instance(duckdb_dir)

        # Act
        result = json.loads(await utils_instance.query_results(
            instance_id, "SELECT a FROM my_table ORDER BY a",
        ))

        # Assert
        assert result == {
            "row_count": 3,
            "columns": ["a"],
            "data": [{"a": 0}, {"a": 1}, {"a": 2}],
            "instance_id": instance_id,
        }

    async def test_returns_an_empty_data_list_when_nothing_matches(self, utils_instance, duckdb_dir):
        # Arrange
        instance_id = _create_instance(duckdb_dir)

        # Act
        result = json.loads(await utils_instance.query_results(
            instance_id, "SELECT a FROM my_table WHERE a > 100",
        ))

        # Assert
        assert result["row_count"] == 0
        assert result["data"] == []

    async def test_converts_nulls_and_dates_to_json_safe_values(self, utils_instance, duckdb_dir):
        # Arrange
        instance_id = _create_instance(duckdb_dir)

        # Act
        raw = await utils_instance.query_results(
            instance_id, "SELECT NULL AS missing, DATE '2024-03-05' AS day",
        )
        result = json.loads(raw)

        # Assert
        row = result["data"][0]
        assert row["missing"] is None
        assert "2024-03-05" in str(row["day"])

    async def test_rejects_an_invalid_instance_id_with_a_correlation_id(self, utils_instance, duckdb_dir):
        # Act
        result = json.loads(await utils_instance.query_results("../etc/passwd", "SELECT 1"))

        # Assert
        assert result["error"] == "invalid request"
        assert result["correlation_id"]

    async def test_rejects_unsafe_sql_with_a_correlation_id(self, utils_instance, duckdb_dir):
        # Arrange
        instance_id = _create_instance(duckdb_dir)

        # Act
        result = json.loads(await utils_instance.query_results(instance_id, "COPY my_table TO 'x.csv'"))

        # Assert
        assert result["error"] == "invalid request"
        assert result["correlation_id"]

    async def test_rejects_a_blank_tenant(self, no_tenant_instance, duckdb_dir):
        # Act
        result = json.loads(await no_tenant_instance.query_results(str(uuid.uuid4()), "SELECT 1"))

        # Assert
        assert result["error"] == "invalid request"

    async def test_reports_instance_not_found_for_an_unknown_id(self, utils_instance, duckdb_dir):
        # Act
        result = json.loads(await utils_instance.query_results(str(uuid.uuid4()), "SELECT 1"))

        # Assert
        assert result["error"] == NOT_FOUND
        assert result["correlation_id"]

    async def test_does_not_find_an_instance_that_belongs_to_another_tenant(self, duckdb_dir):
        # Arrange
        instance_id = _create_instance(duckdb_dir)
        other = mcp_utils(
            api_key="k", tenant="other-tenant", calendar="c",
            user="u", session_id="s", server=None,
        )

        # Act
        result = json.loads(await other.query_results(instance_id, "SELECT * FROM my_table"))

        # Assert: the same message as for an id that does not exist, so the
        # reply does not reveal that the instance exists for another tenant.
        assert result["error"] == NOT_FOUND

    @pytest.mark.parametrize("sql", [
        pytest.param("SELECT * FROM no_such_table", id="unknown-table"),
        pytest.param('SELECT SUM("no_such_column") FROM my_table', id="unknown-column"),
        pytest.param("SELEC a FROM my_table", id="syntax-error"),
        pytest.param("SELECT CAST('x' AS INTEGER) FROM my_table", id="bad-conversion"),
    ])
    async def test_sql_mistake_lists_the_columns_but_not_duckdbs_message(self, utils_instance, duckdb_dir, sql):
        # Arrange
        instance_id = _create_instance(
            duckdb_dir, 'CREATE TABLE my_table AS SELECT 1 AS "Store", 2.5 AS "Sales Value"',
        )

        # Act
        raw = await utils_instance.query_results(instance_id, sql)
        result = json.loads(raw)

        # Assert
        assert result["error"] == (
            "The SQL could not be run against my_table. Check that it is valid DuckDB SQL "
            'and uses only these columns, in double quotes: "Store", "Sales Value".'
        )
        assert result["correlation_id"]
        for duckdb_text in ("no_such", "Binder", "Parser", "Catalog", "Conversion", "SELEC "):
            assert duckdb_text not in raw

    async def test_sql_mistake_after_my_table_was_dropped(self, utils_instance, duckdb_dir):
        # Arrange
        instance_id = _create_instance(duckdb_dir)
        await utils_instance.query_results(instance_id, "DROP TABLE my_table")

        # Act
        result = json.loads(await utils_instance.query_results(instance_id, "SELECT * FROM my_table"))

        # Assert
        assert result["error"].startswith("The SQL could not be run, and this result no longer contains my_table.")

    async def test_long_column_lists_are_cut_short(self, utils_instance, duckdb_dir):
        # Arrange
        columns = ", ".join(f'{i} AS "c{i}"' for i in range(80))
        instance_id = _create_instance(duckdb_dir, f"CREATE TABLE my_table AS SELECT {columns}")

        # Act
        result = json.loads(await utils_instance.query_results(instance_id, "SELECT nope FROM my_table"))

        # Assert
        assert '"c59", ...' in result["error"]
        assert '"c60"' not in result["error"]

    async def test_duckdb_engine_failure_is_still_an_internal_error(self, utils_instance, duckdb_dir, monkeypatch):
        # Arrange: an I/O failure is not the caller's mistake
        instance_id = _create_instance(duckdb_dir)
        import mcp_utils as mcp_utils_module

        def broken_connection(path, read_only=False):
            raise duckdb.IOException("Could not set lock on file C:/secret/path.duckdb")
        monkeypatch.setattr(mcp_utils_module, "_open_sandboxed_connection", broken_connection)

        # Act
        raw = await utils_instance.query_results(instance_id, "SELECT * FROM my_table")

        # Assert
        assert json.loads(raw)["error"] == "internal error"
        assert "secret" not in raw

    async def test_blocks_file_access_from_inside_a_valid_query(self, utils_instance, duckdb_dir, tmp_path):
        # Arrange
        instance_id = _create_instance(duckdb_dir)
        outside = tmp_path / "outside.csv"
        outside.write_text("a\n1\n")

        # Act
        result = json.loads(await utils_instance.query_results(
            instance_id, f"SELECT * FROM read_csv_auto('{outside.as_posix()}')",
        ))

        # Assert
        assert result["error"] == "internal error"


# ---------------------------------------------------------------------------
# get_answer
# ---------------------------------------------------------------------------

class TestGetAnswer:
    async def test_returns_answer_subject_and_question(self, utils_instance, monkeypatch):
        # Arrange
        _install_conversational_driver(monkeypatch)

        # Act
        result = json.loads(await utils_instance.get_answer("How many stores?"))

        # Assert
        assert result == {"answer": "42 stores", "subject": "Sales", "question": "How many stores?"}

    async def test_reports_a_missing_tenant(self, no_tenant_instance, monkeypatch):
        # Arrange
        _install_conversational_driver(monkeypatch)

        # Act
        result = json.loads(await no_tenant_instance.get_answer("anything"))

        # Assert
        assert result == {"error": "Tenant not set"}

    async def test_turns_a_missing_answer_key_into_a_helpful_message(self, utils_instance, monkeypatch):
        # Arrange
        _install_conversational_driver(monkeypatch, raises=KeyError("answer"))

        # Act
        result = json.loads(await utils_instance.get_answer("How many stores?"))

        # Assert
        assert "question service returned an error" in result["error"]
        assert "get_rows_fast" in result["error"]
        assert "correlation_id" not in result

    async def test_hides_unexpected_error_details_from_the_caller(self, utils_instance, monkeypatch):
        # Arrange
        _install_conversational_driver(monkeypatch, raises=RuntimeError(SECRET_TEXT))

        # Act
        raw = await utils_instance.get_answer("How many stores?")
        result = json.loads(raw)

        # Assert
        assert result["error"] == "internal error"
        assert result["correlation_id"]
        assert SECRET_TEXT not in raw

    async def test_reports_start_and_completion_progress_to_the_context(self, utils_instance, monkeypatch):
        # Arrange
        ctx = FakeContext()
        _install_conversational_driver(monkeypatch)

        # Act
        await utils_instance.get_answer("How many stores?", ctx=ctx)

        # Assert
        assert ctx.progress[0] == (0, "Starting conversational query: How many stores?")
        assert ctx.progress[-1] == (1, "Query completed. Subject used: Sales")

    async def test_forwards_status_updates_from_the_sdk_as_progress(self, utils_instance, monkeypatch):
        # Arrange
        ctx = FakeContext()
        _install_conversational_driver(monkeypatch, updates=("Thinking", "Fetching data"))

        # Act
        await utils_instance.get_answer("How many stores?", ctx=ctx)

        # Assert
        assert (1, "Thinking") in ctx.progress
        assert (2, "Fetching data") in ctx.progress
        assert ctx.progress[-1][0] == 3

    async def test_status_updates_do_not_need_a_context(self, utils_instance, monkeypatch):
        # Arrange
        _install_conversational_driver(monkeypatch, updates=("Thinking",))

        # Act
        result = json.loads(await utils_instance.get_answer("How many stores?"))

        # Assert
        assert result["answer"] == "42 stores"

    async def test_tells_the_context_when_the_service_gives_no_answer(self, utils_instance, monkeypatch):
        # Arrange
        ctx = FakeContext()
        _install_conversational_driver(monkeypatch, raises=KeyError("answer"))

        # Act
        result = json.loads(await utils_instance.get_answer("How many stores?", ctx=ctx))

        # Assert
        assert ctx.errors == [result["error"]]

    async def test_tells_the_context_only_the_correlation_id_for_unexpected_errors(self, utils_instance, monkeypatch):
        # Arrange
        ctx = FakeContext()
        _install_conversational_driver(monkeypatch, raises=RuntimeError(SECRET_TEXT))

        # Act
        result = json.loads(await utils_instance.get_answer("How many stores?", ctx=ctx))

        # Assert
        assert ctx.errors == [f"internal error (correlation_id: {result['correlation_id']})"]
        assert SECRET_TEXT not in ctx.errors[0]


# ---------------------------------------------------------------------------
# get_schema
# ---------------------------------------------------------------------------

class TestGetSchema:
    async def test_reports_a_missing_tenant(self, no_tenant_instance, monkeypatch):
        # Arrange
        _install_structured_driver(monkeypatch, schema="{}")

        # Act
        result = json.loads(no_tenant_instance.get_schema())

        # Assert
        assert result == {"error": "Tenant not set"}

    async def test_reports_when_the_service_returns_no_schema(self, utils_instance, monkeypatch):
        # Arrange
        _install_structured_driver(monkeypatch, schema=None)

        # Act
        result = json.loads(utils_instance.get_schema())

        # Assert
        assert result == {"error": "No schema returned from get_schema"}

    async def test_adds_dashboard_hints_to_every_subject(self, utils_instance, monkeypatch):
        # Arrange
        schema = {"subjects": [
            {"name": "Sales", "factFieldTypes": {"Sale Date": {}}, "metricFieldTypes": {"Sales Value": {}}},
            {"name": "Stock", "factFieldTypes": {"Store": {}}, "metricFieldTypes": {"Quantity": {}}},
        ]}
        _install_structured_driver(monkeypatch, schema=json.dumps(schema))

        # Act
        result = json.loads(utils_instance.get_schema())

        # Assert
        assert [s["name"] for s in result["subjects"]] == ["Sales", "Stock"]
        assert all("dashboardHints" in s for s in result["subjects"])
        assert result["subjects"][0]["dashboardHints"]["recommendedTimeDimension"] == "Sale Date"

    async def test_returns_a_schema_without_subjects_unchanged(self, utils_instance, monkeypatch):
        # Arrange
        _install_structured_driver(monkeypatch, schema='{"schemaVersion": 2}')

        # Act
        result = json.loads(utils_instance.get_schema())

        # Assert
        assert result == {"schemaVersion": 2}

    async def test_returns_non_json_schema_text_as_it_was_received(self, utils_instance, monkeypatch):
        # Arrange
        _install_structured_driver(monkeypatch, schema="this is not json")

        # Act
        result = utils_instance.get_schema()

        # Assert
        assert result == "this is not json"

    async def test_hides_upstream_error_details_from_the_caller(self, utils_instance, monkeypatch):
        # Arrange
        _install_structured_driver(monkeypatch, raises=RuntimeError(SECRET_TEXT))

        # Act
        raw = utils_instance.get_schema()
        result = json.loads(raw)

        # Assert
        assert result["error"] == "internal error"
        assert result["correlation_id"]
        assert SECRET_TEXT not in raw


# ---------------------------------------------------------------------------
# _add_dashboard_hints
# ---------------------------------------------------------------------------

def _hints_for(utils, facts=(), metrics=()):
    subject = {
        "name": "Test",
        "factFieldTypes": {name: {} for name in facts},
        "metricFieldTypes": {name: {} for name in metrics},
    }
    utils._add_dashboard_hints(subject)
    return subject


class TestAddDashboardHints:
    def test_groups_fact_fields_by_meaning(self, utils_instance):
        # Act
        subject = _hints_for(utils_instance, facts=[
            "Sale Date", "Region", "Product Name", "Customer Type", "Order Number",
        ])

        # Assert
        assert subject["fieldGroups"] == {
            "timeFields": ["Sale Date"],
            "locationFields": ["Region"],
            "productFields": ["Product Name"],
            "categoryFields": ["Customer Type"],
            "identifierFields": ["Order Number"],
        }

    def test_matches_field_names_regardless_of_case(self, utils_instance):
        # Act
        subject = _hints_for(utils_instance, facts=["SALE DATE"])

        # Assert
        assert subject["fieldGroups"]["timeFields"] == ["SALE DATE"]

    def test_puts_a_field_in_the_first_matching_group_only(self, utils_instance):
        # Act: "Product Category" matches both product and category keywords.
        subject = _hints_for(utils_instance, facts=["Product Category"])

        # Assert
        assert subject["fieldGroups"] == {"productFields": ["Product Category"]}

    def test_leaves_out_groups_that_have_no_fields(self, utils_instance):
        # Act
        subject = _hints_for(utils_instance, facts=["Sale Date"])

        # Assert
        assert list(subject["fieldGroups"]) == ["timeFields"]

    def test_ignores_fact_fields_that_match_no_group(self, utils_instance):
        # Act
        subject = _hints_for(utils_instance, facts=["Colour", "Notes"])

        # Assert
        assert "fieldGroups" not in subject
        assert "fastQueryFields" not in subject["dashboardHints"]

    def test_gives_only_the_fixed_hints_for_a_subject_with_no_fields(self, utils_instance):
        # Act
        subject = _hints_for(utils_instance)

        # Assert
        assert subject["dashboardHints"] == {"topNSupported": True, "maxRowsRecommended": 5000}
        assert "fieldGroups" not in subject

    def test_copes_with_a_subject_that_has_no_field_sections(self, utils_instance):
        # Arrange
        subject = {"name": "Bare"}

        # Act
        utils_instance._add_dashboard_hints(subject)

        # Assert
        assert subject["dashboardHints"] == {"topNSupported": True, "maxRowsRecommended": 5000}

    @pytest.mark.parametrize("fields, expected", [
        pytest.param(["Sale Month", "Sale Date", "Sale Year"], "Sale Date", id="date-beats-month-and-year"),
        pytest.param(["Sale Year", "Sale Month"], "Sale Year", id="year-beats-month"),
        pytest.param(["Sale Quarter", "Sale Month"], "Sale Month", id="month-beats-quarter"),
        pytest.param(["Day Name", "Time Slot"], "Day Name", id="first-field-when-no-preferred-word"),
    ])
    def test_recommends_the_most_useful_time_dimension(self, utils_instance, fields, expected):
        # Act
        subject = _hints_for(utils_instance, facts=fields)

        # Assert
        assert subject["dashboardHints"]["recommendedTimeDimension"] == expected

    def test_recommends_no_time_dimension_when_there_are_no_time_fields(self, utils_instance):
        # Act
        subject = _hints_for(utils_instance, facts=["Region"])

        # Assert
        assert "recommendedTimeDimension" not in subject["dashboardHints"]

    def test_recommends_business_metrics_ahead_of_other_metrics(self, utils_instance):
        # Act
        subject = _hints_for(utils_instance, metrics=["Footfall", "Basket Size", "Sales Value"])

        # Assert
        assert subject["dashboardHints"]["recommendedMetrics"] == ["Sales Value", "Footfall", "Basket Size"]

    def test_tops_up_to_three_metrics_when_few_have_priority_words(self, utils_instance):
        # Act
        subject = _hints_for(utils_instance, metrics=["A", "B", "C", "D", "Revenue"])

        # Assert
        assert subject["dashboardHints"]["recommendedMetrics"] == ["Revenue", "A", "B"]

    def test_limits_recommended_metrics_to_five(self, utils_instance):
        # Act
        subject = _hints_for(utils_instance, metrics=[f"Sales {i}" for i in range(8)])

        # Assert
        assert subject["dashboardHints"]["recommendedMetrics"] == [f"Sales {i}" for i in range(5)]

    def test_recommends_every_metric_when_there_are_fewer_than_three(self, utils_instance):
        # Act
        subject = _hints_for(utils_instance, metrics=["Footfall"])

        # Assert
        assert subject["dashboardHints"]["recommendedMetrics"] == ["Footfall"]

    def test_omits_recommended_metrics_when_there_are_no_metrics(self, utils_instance):
        # Act
        subject = _hints_for(utils_instance, facts=["Sale Date"])

        # Assert
        assert "recommendedMetrics" not in subject["dashboardHints"]

    def test_limits_fast_query_fields_per_group_in_priority_order(self, utils_instance):
        # Arrange
        facts = (
            ["Order Date", "Ship Date", "Due Date", "Delivery Date"]
            + ["Region", "Country", "City", "State"]
            + ["Product Name", "Item Name", "Brand Name"]
        )

        # Act
        subject = _hints_for(utils_instance, facts=facts)

        # Assert
        assert subject["dashboardHints"]["fastQueryFields"] == [
            "Order Date", "Ship Date", "Due Date",
            "Region", "Country", "City",
            "Product Name", "Item Name",
        ]


# ---------------------------------------------------------------------------
# get_financial_periods
# ---------------------------------------------------------------------------

class TestGetFinancialPeriods:
    async def test_reports_a_missing_tenant(self, no_tenant_instance, monkeypatch):
        # Arrange
        _install_calendar_assistant(monkeypatch, periods={})

        # Act
        result = json.loads(await no_tenant_instance.get_financial_periods("2024-03-05"))

        # Assert
        assert result == {"error": "Tenant and calendar must be set"}

    async def test_reports_a_missing_calendar(self, no_calendar_instance, monkeypatch):
        # Arrange
        _install_calendar_assistant(monkeypatch, periods={})

        # Act
        result = json.loads(await no_calendar_instance.get_financial_periods("2024-03-05"))

        # Assert
        assert result == {"error": "Tenant and calendar must be set"}

    async def test_returns_the_periods_for_the_requested_date(self, utils_instance, monkeypatch):
        # Arrange
        periods = {"year": 2024, "month": 3, "week": 10, "quarter": 1}
        _install_calendar_assistant(monkeypatch, periods=periods)

        # Act
        result = json.loads(await utils_instance.get_financial_periods("2024-03-05"))

        # Assert
        assert result["date"] == "2024-03-05"
        assert json.loads(result["periods"]) == periods

    async def test_converts_the_sdk_period_object_to_json(self, utils_instance, monkeypatch):
        # Arrange
        _install_calendar_assistant(
            monkeypatch, periods=FinancialPeriodDetails(year=2025, month=6, week=24, quarter=2),
        )

        # Act
        result = json.loads(await utils_instance.get_financial_periods("2025-06-15"))

        # Assert
        assert json.loads(result["periods"]) == {"year": 2025, "month": 6, "week": 24, "quarter": 2}

    async def test_uses_todays_date_when_none_is_given(self, utils_instance, monkeypatch):
        # Arrange
        _install_calendar_assistant(monkeypatch, periods={"year": 2024})
        before = date.today()

        # Act
        result = json.loads(await utils_instance.get_financial_periods())

        # Assert
        after = date.today()
        assert before <= date.fromisoformat(result["date"]) <= after

    async def test_describes_periods_that_cannot_be_serialised_as_text(self, utils_instance, monkeypatch):
        # Arrange
        _install_calendar_assistant(
            monkeypatch, periods=FinancialPeriodDetails(year=2024, month=3, week=10, quarter=1),
        )

        # Act
        result = json.loads(await utils_instance.get_financial_periods("2024-03-05"))

        # Assert
        assert "2024" in result["periods"]
        assert result["date"] == "2024-03-05"

    @pytest.mark.parametrize("bad_date", ["not-a-date", "15/06/2025", "2025-13-45"])
    async def test_explains_the_date_format_for_a_malformed_date(self, utils_instance, monkeypatch, bad_date):
        # Arrange
        _install_calendar_assistant(monkeypatch, periods={})

        # Act
        raw = await utils_instance.get_financial_periods(bad_date)

        # Assert
        assert json.loads(raw) == {
            "error": "target_date must be a date in YYYY-MM-DD format, for example 2025-06-15."
        }

    async def test_hides_upstream_error_details_from_the_caller(self, utils_instance, monkeypatch):
        # Arrange
        _install_calendar_assistant(monkeypatch, raises=RuntimeError(SECRET_TEXT))

        # Act
        raw = await utils_instance.get_financial_periods("2024-03-05")
        result = json.loads(raw)

        # Assert
        assert result["error"] == "internal error"
        assert SECRET_TEXT not in raw


# ---------------------------------------------------------------------------
# get_calendar_period_date_range: explicit parameters
# ---------------------------------------------------------------------------

JAN = (date(2024, 1, 1), date(2024, 1, 31))


class TestGetCalendarPeriodDateRangeExplicit:
    async def test_returns_the_start_and_end_dates_for_the_period(self, utils_instance, monkeypatch):
        # Arrange
        _install_calendar_assistant(monkeypatch, date_range=JAN)

        # Act
        result = json.loads(await utils_instance.get_calendar_period_date_range(2024, 1, "month"))

        # Assert
        assert result == {
            "start_date": "2024-01-01",
            "end_date": "2024-01-31",
            "financial_year": 2024,
            "period_number": 1,
            "period_type": "month",
        }

    @pytest.mark.parametrize("period_type", [
        pytest.param("year", id="year"),
        pytest.param("month", id="month"),
        pytest.param("quarter", id="quarter"),
        pytest.param("week", id="week"),
        pytest.param("MONTH", id="upper-case"),
    ])
    async def test_accepts_each_period_type(self, utils_instance, monkeypatch, period_type):
        # Arrange
        _install_calendar_assistant(monkeypatch, date_range=JAN)

        # Act
        result = json.loads(await utils_instance.get_calendar_period_date_range(2024, 1, period_type))

        # Assert
        assert result["start_date"] == "2024-01-01"
        assert result["period_type"] == period_type.lower()

    async def test_rejects_an_unknown_period_type(self, utils_instance, monkeypatch):
        # Arrange
        _install_calendar_assistant(monkeypatch, date_range=JAN)

        # Act
        result = json.loads(await utils_instance.get_calendar_period_date_range(2024, 1, "fortnight"))

        # Assert
        assert result["error"].startswith("Invalid period_type: fortnight")

    async def test_reports_when_no_date_range_exists_for_the_period(self, utils_instance, monkeypatch):
        # Arrange
        _install_calendar_assistant(monkeypatch, date_range=None)

        # Act
        result = json.loads(await utils_instance.get_calendar_period_date_range(2024, 99, "month"))

        # Assert
        assert result == {"error": "No date range found for the specified period"}

    @pytest.mark.parametrize("instance_fixture", [
        pytest.param("no_tenant_instance", id="no-tenant"),
        pytest.param("no_calendar_instance", id="no-calendar"),
    ])
    async def test_reports_a_missing_tenant_or_calendar(self, request, monkeypatch, instance_fixture):
        # Arrange
        instance = request.getfixturevalue(instance_fixture)
        _install_calendar_assistant(monkeypatch, date_range=JAN)

        # Act
        result = json.loads(await instance.get_calendar_period_date_range(2024, 1, "month"))

        # Assert
        assert result == {"error": "Tenant and Calendar variables must be set"}

    async def test_rejects_a_financial_year_of_zero(self, utils_instance, monkeypatch):
        # Arrange
        _install_calendar_assistant(monkeypatch, date_range=JAN)

        # Act
        result = json.loads(await utils_instance.get_calendar_period_date_range(0, 1, "month"))

        # Assert
        assert result == {"error": "Could not determine financial_year"}

    async def test_rejects_a_period_number_of_zero(self, utils_instance, monkeypatch):
        # Arrange
        _install_calendar_assistant(monkeypatch, date_range=JAN)

        # Act
        result = json.loads(await utils_instance.get_calendar_period_date_range(2024, 0, "month"))

        # Assert
        assert result == {"error": "Could not determine period_number"}

    async def test_rejects_an_empty_period_type(self, utils_instance, monkeypatch):
        # Arrange
        _install_calendar_assistant(monkeypatch, date_range=JAN)

        # Act
        result = json.loads(await utils_instance.get_calendar_period_date_range(2024, 1, ""))

        # Assert
        assert result == {"error": "Could not determine period_type"}

    async def test_hides_upstream_error_details_from_the_caller(self, utils_instance, monkeypatch):
        # Arrange
        _install_calendar_assistant(monkeypatch, raises=RuntimeError(SECRET_TEXT))

        # Act
        raw = await utils_instance.get_calendar_period_date_range(2024, 1, "month")
        result = json.loads(raw)

        # Assert
        assert result["error"] == "internal error"
        assert result["correlation_id"]
        assert SECRET_TEXT not in raw


# ---------------------------------------------------------------------------
# get_calendar_period_date_range: defaults taken from the current period
# ---------------------------------------------------------------------------

CURRENT_PERIODS = {"year": 2025, "month": 3, "quarter": 1, "week": 10}


class TestGetCalendarPeriodDateRangeDefaults:
    async def test_defaults_to_the_current_month(self, utils_instance, monkeypatch):
        # Arrange
        _install_calendar_assistant(monkeypatch, periods=CURRENT_PERIODS, date_range=JAN)

        # Act
        result = json.loads(await utils_instance.get_calendar_period_date_range())

        # Assert
        assert result["financial_year"] == 2025
        assert result["period_number"] == 3
        assert result["period_type"] == "month"

    @pytest.mark.parametrize("period_type, expected_number", [
        pytest.param("quarter", 1, id="quarter"),
        pytest.param("week", 10, id="week"),
        pytest.param("year", 1, id="year"),
        pytest.param("month", 3, id="month"),
        pytest.param("Quarter", 1, id="mixed-case-quarter"),
    ])
    async def test_uses_the_current_period_number_for_the_requested_type(
        self, utils_instance, monkeypatch, period_type, expected_number,
    ):
        # Arrange
        _install_calendar_assistant(monkeypatch, periods=CURRENT_PERIODS, date_range=JAN)

        # Act
        result = json.loads(await utils_instance.get_calendar_period_date_range(period_type=period_type))

        # Assert
        assert result["period_number"] == expected_number
        assert result["financial_year"] == 2025

    async def test_keeps_values_the_caller_supplied(self, utils_instance, monkeypatch):
        # Arrange
        _install_calendar_assistant(monkeypatch, periods=CURRENT_PERIODS, date_range=JAN)

        # Act
        result = json.loads(await utils_instance.get_calendar_period_date_range(
            financial_year=2020, period_type="month",
        ))

        # Assert
        assert result["financial_year"] == 2020
        assert result["period_number"] == 3

    async def test_looks_up_only_the_year_when_number_and_type_are_supplied(self, utils_instance, monkeypatch):
        # Arrange
        _install_calendar_assistant(monkeypatch, periods=CURRENT_PERIODS, date_range=JAN)

        # Act
        result = json.loads(await utils_instance.get_calendar_period_date_range(
            period_number=7, period_type="quarter",
        ))

        # Assert
        assert result["financial_year"] == 2025
        assert result["period_number"] == 7
        assert result["period_type"] == "quarter"

    async def test_rejects_an_unknown_period_type_when_the_number_is_missing(self, utils_instance, monkeypatch):
        # Arrange
        _install_calendar_assistant(monkeypatch, periods=CURRENT_PERIODS, date_range=JAN)

        # Act
        result = json.loads(await utils_instance.get_calendar_period_date_range(period_type="fortnight"))

        # Assert
        assert result["error"].startswith("Invalid period_type: fortnight")

    async def test_passes_on_an_error_from_looking_up_the_current_period(self, utils_instance, monkeypatch):
        # Arrange
        _install_calendar_assistant(monkeypatch, raises=RuntimeError(SECRET_TEXT))

        # Act
        raw = await utils_instance.get_calendar_period_date_range()
        result = json.loads(raw)

        # Assert
        assert result["error"] == "internal error"
        assert SECRET_TEXT not in raw

    async def test_reports_when_the_current_year_cannot_be_found(self, utils_instance, monkeypatch):
        # Arrange
        _install_calendar_assistant(monkeypatch, periods={}, date_range=JAN)

        # Act
        result = json.loads(await utils_instance.get_calendar_period_date_range())

        # Assert
        assert result == {"error": "Could not determine financial_year"}

    async def test_defaults_to_the_current_month_with_the_real_period_object(self, utils_instance, monkeypatch):
        # Arrange
        _install_calendar_assistant(
            monkeypatch,
            periods=FinancialPeriodDetails(year=2025, month=3, week=10, quarter=1),
            date_range=JAN,
        )

        # Act
        result = json.loads(await utils_instance.get_calendar_period_date_range())

        # Assert
        assert result["financial_year"] == 2025
        assert result["period_number"] == 3
        assert result["period_type"] == "month"
