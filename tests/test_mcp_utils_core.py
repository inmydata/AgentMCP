"""
Tests for the first half of mcp_utils: path/connection helpers, value
serialisation, filter parsing, DuckDB spill-over and the get_rows / get_top_n
tool methods.

The inmydata StructuredDataDriver is the only thing replaced (it calls the
real API). DuckDB and pandas run for real under tmp_path.
"""
import json
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_utils as mcp_utils_module  # noqa: E402
from inmydata.StructuredData import ConditionOperator, LogicalOperator  # noqa: E402
from errors import UserFacingError  # noqa: E402
from mcp_utils import (  # noqa: E402
    InvalidTenant,
    UnsafeSql,
    _assert_sql_safe,
    _open_sandboxed_connection,
    _resolve_duckdb_path,
    _tenant_namespace,
    mcp_utils,
)

TEST_TENANT = "test"


@pytest.fixture
def duckdb_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_DUCKDB_LOCATION", str(tmp_path))
    return tmp_path


@pytest.fixture
def utils_instance():
    return mcp_utils(
        api_key="test", tenant=TEST_TENANT, calendar="test",
        user="test", session_id="test", server=None,
    )


class FakeDriver:
    """Stands in for StructuredDataDriver; returns whatever the test sets."""
    result = None
    error = None
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    def get_data(self, subject, fields, filters, top_n):
        FakeDriver.calls.append((subject, fields, filters, top_n))
        if FakeDriver.error:
            raise FakeDriver.error
        return FakeDriver.result


@pytest.fixture
def fake_driver(monkeypatch):
    FakeDriver.result = None
    FakeDriver.error = None
    FakeDriver.calls = []
    monkeypatch.setattr(mcp_utils_module, "StructuredDataDriver", FakeDriver)
    return FakeDriver


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------

class TestConstructor:
    def test_defaults_server_when_none_given(self, utils_instance):
        assert utils_instance.server == "inmydata.com"

    def test_keeps_explicit_server(self):
        u = mcp_utils("k", "t", "c", "u", "s", "example.test")

        assert u.server == "example.test"


# ---------------------------------------------------------------------------
# _resolve_duckdb_path
# ---------------------------------------------------------------------------

class TestResolveDuckdbPath:
    def test_rejects_tenant_namespace_that_is_a_symlink_outside_base(self, duckdb_dir, tmp_path_factory):
        outside = tmp_path_factory.mktemp("outside")
        link = duckdb_dir / _tenant_namespace(TEST_TENANT)
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not permitted on this machine")

        with pytest.raises(InvalidTenant):
            _resolve_duckdb_path(TEST_TENANT, str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# _open_sandboxed_connection
# ---------------------------------------------------------------------------

class TestOpenSandboxedConnectionFailure:
    def test_closes_connection_and_reraises_when_locking_down_fails(self, monkeypatch):
        class BrokenConnection:
            closed = False

            def execute(self, sql):
                raise duckdb.Error("cannot set option")

            def close(self):
                BrokenConnection.closed = True

        monkeypatch.setattr(mcp_utils_module.duckdb, "connect", lambda *a, **k: BrokenConnection())

        with pytest.raises(duckdb.Error):
            _open_sandboxed_connection(":memory:")

        assert BrokenConnection.closed is True


# ---------------------------------------------------------------------------
# _assert_sql_safe edge cases
# ---------------------------------------------------------------------------

class TestAssertSqlSafeEdges:
    @pytest.mark.parametrize("sql", [
        pytest.param("SELECT 1;;  ;", id="empty-statements"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param("(SELECT 1)", id="starts-with-bracket"),
        pytest.param("-- just a comment", id="comment-only"),
    ])
    def test_allows_statements_with_no_leading_keyword(self, sql):
        _assert_sql_safe(sql)  # must not raise

    def test_rejects_non_string_input(self):
        with pytest.raises(UnsafeSql):
            _assert_sql_safe(None)


# ---------------------------------------------------------------------------
# _to_json_safe
# ---------------------------------------------------------------------------

class TestToJsonSafe:
    @pytest.mark.parametrize("value", [
        pytest.param(None, id="none"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(pd.NaT, id="nat"),
        pytest.param(np.nan, id="numpy-nan"),
    ])
    def test_missing_values_become_none(self, utils_instance, value):
        assert utils_instance._to_json_safe(value) is None

    def test_numpy_integer_becomes_python_int(self, utils_instance):
        result = utils_instance._to_json_safe(np.int64(7))

        assert result == 7 and type(result) is int

    def test_numpy_float_becomes_python_float(self, utils_instance):
        result = utils_instance._to_json_safe(np.float32(1.5))

        assert result == 1.5 and type(result) is float

    def test_numpy_bool_becomes_python_bool(self, utils_instance):
        result = utils_instance._to_json_safe(np.bool_(True))

        assert result is True

    def test_naive_datetime_is_marked_as_utc(self, utils_instance):
        result = utils_instance._to_json_safe(datetime(2024, 3, 1, 12, 30, 0))

        assert result == "2024-03-01T12:30:00Z"

    def test_aware_datetime_keeps_its_offset(self, utils_instance):
        result = utils_instance._to_json_safe(datetime(2024, 3, 1, 12, 30, tzinfo=timezone.utc))

        assert result == "2024-03-01T12:30:00+00:00"

    def test_date_becomes_iso_string(self, utils_instance):
        assert utils_instance._to_json_safe(date(2024, 3, 1)) == "2024-03-01"

    def test_decimal_becomes_exact_string(self, utils_instance):
        assert utils_instance._to_json_safe(Decimal("12.340")) == "12.340"

    def test_plain_values_pass_through(self, utils_instance):
        assert utils_instance._to_json_safe("hello") == "hello"


# ---------------------------------------------------------------------------
# dataframe_to_LLM_string
# ---------------------------------------------------------------------------

class TestDataframeToLlmString:
    def test_describes_rows_columns_and_schema(self, utils_instance):
        df = pd.DataFrame({"a": [1, 2], "b": ["x", None]})

        out = json.loads(utils_instance.dataframe_to_LLM_string(df))

        assert out["row_count"] == 2
        assert out["returned_rows"] == 2
        assert out["truncated"] is False
        assert out["columns"] == ["a", "b"]
        assert out["data"] == [{"a": 1, "b": "x"}, {"a": 2, "b": None}]
        assert out["schema"][0] == {"name": "a", "dtype": "int64"}

    def test_includes_markdown_preview_when_there_are_rows(self, utils_instance):
        pytest.importorskip("tabulate")
        df = pd.DataFrame({"a": [1]})

        out = json.loads(utils_instance.dataframe_to_LLM_string(df))

        assert "a" in out["markdown_preview"]

    def test_omits_schema_when_not_requested(self, utils_instance):
        df = pd.DataFrame({"a": [1]})

        out = json.loads(utils_instance.dataframe_to_LLM_string(df, include_schema=False))

        assert "schema" not in out

    def test_caps_rows_and_reports_truncation(self, utils_instance):
        df = pd.DataFrame({"a": range(10)})

        out = json.loads(utils_instance.dataframe_to_LLM_string(df, max_rows=3))

        assert out["row_count"] == 10
        assert out["returned_rows"] == 3
        assert out["truncated"] is True
        assert len(out["data"]) == 3

    def test_empty_dataframe_gives_no_rows_and_no_preview(self, utils_instance):
        df = pd.DataFrame({"a": []})

        out = json.loads(utils_instance.dataframe_to_LLM_string(df))

        assert out["row_count"] == 0
        assert out["data"] == []
        assert "markdown_preview" not in out

    def test_falls_back_to_csv_sample_when_output_too_large(self, utils_instance):
        df = pd.DataFrame({"a": ["x" * 50] * 20})

        s = utils_instance.dataframe_to_LLM_string(df, max_chars=500)
        out = json.loads(s)

        assert out["data_format"] == "csv"
        assert out["truncated"] is True
        assert "data" not in out
        assert len(out["csv_sample"]) <= 250
        assert out["csv_sample"].splitlines()[0] == "a"  # line ending depends on the OS


# ---------------------------------------------------------------------------
# Operator normalisation
# ---------------------------------------------------------------------------

class TestNormalizeConditionOperator:
    @pytest.mark.parametrize("raw, expected", [
        pytest.param(None, ConditionOperator.Equals, id="none-defaults-to-equals"),
        pytest.param("", ConditionOperator.Equals, id="empty-defaults-to-equals"),
        pytest.param("eq", ConditionOperator.Equals, id="eq"),
        pytest.param("!=", ConditionOperator.NotEquals, id="not-equal-symbol"),
        pytest.param("  GTE ", ConditionOperator.GreaterThanOrEqualTo, id="padded-upper-case"),
        pytest.param("contains", ConditionOperator.Like, id="contains"),
        pytest.param("starts_with", ConditionOperator.StartsWith, id="starts-with"),
    ])
    def test_maps_aliases(self, utils_instance, raw, expected):
        assert utils_instance._normalize_condition_operator(raw) == expected

    def test_rejects_unknown_operator_with_user_facing_error(self, utils_instance):
        with pytest.raises(UserFacingError, match="Unsupported operator"):
            utils_instance._normalize_condition_operator("approx")


class TestNormalizeLogicalOperator:
    @pytest.mark.parametrize("raw, expected", [
        pytest.param(None, LogicalOperator.And, id="none-defaults-to-and"),
        pytest.param("or", LogicalOperator.Or, id="lower-or"),
        pytest.param(" Or ", LogicalOperator.Or, id="padded-mixed-case-or"),
        pytest.param("AND", LogicalOperator.And, id="upper-and"),
    ])
    def test_maps_values(self, utils_instance, raw, expected):
        assert utils_instance._normalize_logical_operator(raw) == expected

    def test_rejects_unknown_logical_operator(self, utils_instance):
        with pytest.raises(UserFacingError, match="Unsupported logical operator"):
            utils_instance._normalize_logical_operator("xor")


class TestIsInt:
    @pytest.mark.parametrize("value, expected", [
        pytest.param("12", True, id="digits"),
        pytest.param("-3", True, id="negative"),
        pytest.param("1.5", False, id="decimal"),
        pytest.param("abc", False, id="text"),
        pytest.param(None, False, id="none"),
    ])
    def test_detects_integers(self, utils_instance, value, expected):
        assert utils_instance.is_int(value) is expected


# ---------------------------------------------------------------------------
# parse_where
# ---------------------------------------------------------------------------

class TestParseWhere:
    @pytest.mark.parametrize("where", [
        pytest.param(None, id="none"),
        pytest.param([], id="empty-list"),
    ])
    def test_no_filters_gives_empty_list(self, utils_instance, where):
        assert utils_instance.parse_where(where) == []

    def test_builds_filter_with_defaults(self, utils_instance):
        result = utils_instance.parse_where([{"field": "Region", "value": "North"}])

        f = result[0]
        assert f.Field == "Region"
        assert f.ConditionOperator == ConditionOperator.Equals
        assert f.LogicalOperator == LogicalOperator.And
        assert f.Value == "North"
        assert f.StartGroup == 0 and f.EndGroup == 0
        assert f.CaseInsensitive is True

    def test_honours_explicit_options(self, utils_instance):
        result = utils_instance.parse_where([{
            "field": "Sales", "op": "gte", "value": 1000, "logical": "or",
            "start_group": 1, "end_group": 2, "case_insensitive": False,
        }])

        f = result[0]
        assert f.ConditionOperator == ConditionOperator.GreaterThanOrEqualTo
        assert f.LogicalOperator == LogicalOperator.Or
        assert f.StartGroup == 1 and f.EndGroup == 2
        assert f.CaseInsensitive is False

    @pytest.mark.parametrize("key", [
        pytest.param("column", id="column-synonym"),
        pytest.param("name", id="name-synonym"),
    ])
    def test_accepts_field_name_synonyms(self, utils_instance, key):
        result = utils_instance.parse_where([{key: "Region", "value": "x"}])

        assert result[0].Field == "Region"

    def test_accepts_logic_as_synonym_for_logical(self, utils_instance):
        result = utils_instance.parse_where([{"field": "a", "value": 1, "logic": "OR"}])

        assert result[0].LogicalOperator == LogicalOperator.Or

    def test_keeps_falsy_values(self, utils_instance):
        result = utils_instance.parse_where([{"field": "a", "value": 0}, {"field": "b", "value": ""}])

        assert [f.Value for f in result] == [0, ""]

    def test_rejects_filter_without_field(self, utils_instance):
        with pytest.raises(UserFacingError, match="index 1.*'field'"):
            utils_instance.parse_where([{"field": "a", "value": 1}, {"value": 2}])

    def test_rejects_filter_without_value(self, utils_instance):
        with pytest.raises(UserFacingError, match="requires 'value'"):
            utils_instance.parse_where([{"field": "Region"}])

    def test_rejects_filter_with_unknown_operator(self, utils_instance):
        with pytest.raises(UserFacingError, match="Unsupported operator"):
            utils_instance.parse_where([{"field": "a", "op": "bogus", "value": 1}])


# ---------------------------------------------------------------------------
# save_to_duckdb
# ---------------------------------------------------------------------------

class TestSaveToDuckdb:
    def test_small_result_is_returned_whole_and_nothing_is_written(self, utils_instance, duckdb_dir):
        df = pd.DataFrame({"a": [1, 2, 3]})

        rows, path, instance_id = utils_instance.save_to_duckdb(df, total_rows=3, default_limit=10)

        assert len(rows) == 3
        assert path == "" and instance_id == ""
        assert list(duckdb_dir.iterdir()) == []

    def test_large_result_is_saved_in_full_and_sample_returned(self, utils_instance, duckdb_dir):
        df = pd.DataFrame({"a": range(25)})

        rows, path, instance_id = utils_instance.save_to_duckdb(df, total_rows=25, default_limit=5)

        assert len(rows) == 5
        assert Path(path).parent.parent == duckdb_dir.resolve()
        assert Path(path).name == f"{instance_id}.duckdb"
        con = duckdb.connect(path, read_only=True)
        try:
            assert con.execute("SELECT count(*) FROM my_table").fetchone()[0] == 25
        finally:
            con.close()

    def test_result_exactly_at_limit_is_not_saved(self, utils_instance, duckdb_dir):
        df = pd.DataFrame({"a": range(5)})

        _, path, _ = utils_instance.save_to_duckdb(df, total_rows=5, default_limit=5)

        assert path == ""

    def test_env_var_overrides_default_limit(self, utils_instance, duckdb_dir, monkeypatch):
        monkeypatch.setenv("MCP_SAMPLE_ROWS", "2")
        df = pd.DataFrame({"a": range(4)})

        rows, path, _ = utils_instance.save_to_duckdb(df, total_rows=4, default_limit=10)

        assert len(rows) == 2
        assert path != ""

    def test_non_numeric_env_var_falls_back_to_default_limit(self, utils_instance, duckdb_dir, monkeypatch):
        monkeypatch.setenv("MCP_SAMPLE_ROWS", "lots")
        df = pd.DataFrame({"a": range(4)})

        rows, path, _ = utils_instance.save_to_duckdb(df, total_rows=4, default_limit=3)

        assert len(rows) == 3
        assert path != ""

    def test_blank_tenant_refuses_to_write(self, duckdb_dir):
        u = mcp_utils("k", "  ", "c", "u", "s", None)
        df = pd.DataFrame({"a": range(20)})

        with pytest.raises(InvalidTenant):
            u.save_to_duckdb(df, total_rows=20, default_limit=5)

        assert list(duckdb_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# get_rows
# ---------------------------------------------------------------------------

class TestGetRows:
    async def test_returns_records_with_json_safe_values(self, utils_instance, fake_driver, duckdb_dir):
        fake_driver.result = pd.DataFrame({
            "Region": ["North", "South"],
            "Sales": [np.float64(1.5), np.nan],
            "When": [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")],
        })

        out = json.loads(await utils_instance.get_rows("Sales", ["Region", "Sales", "When"]))

        assert out["subject"] == "Sales"
        assert out["row_count"] == 2
        assert out["columns"] == ["Region", "Sales", "When"]
        assert out["data"][0] == {"Region": "North", "Sales": 1.5, "When": "2024-01-02T00:00:00Z"}
        assert out["data"][1]["Sales"] is None
        assert out["instance_id"] == ""

    async def test_passes_parsed_filters_and_no_top_n_to_driver(self, utils_instance, fake_driver, duckdb_dir):
        fake_driver.result = pd.DataFrame({"a": [1]})

        await utils_instance.get_rows("S", ["a"], [{"field": "a", "op": "gt", "value": 0}])

        subject, fields, filters, top_n = fake_driver.calls[0]
        assert (subject, fields, top_n) == ("S", ["a"], None)
        assert filters[0].ConditionOperator == ConditionOperator.GreaterThan

    async def test_large_result_is_sampled_and_saved_with_instance_id(self, utils_instance, fake_driver, duckdb_dir, monkeypatch):
        monkeypatch.setenv("MCP_SAMPLE_ROWS", "3")
        fake_driver.result = pd.DataFrame({"a": range(8)})

        out = json.loads(await utils_instance.get_rows("S", ["a"]))

        assert out["row_count"] == 8
        assert len(out["data"]) == 3
        assert uuid.UUID(out["instance_id"])  # a real uuid
        assert list(duckdb_dir.glob("*/" + out["instance_id"] + ".duckdb"))

    async def test_missing_tenant_returns_error_without_calling_driver(self, fake_driver):
        u = mcp_utils("k", "", "c", "u", "s", None)

        out = json.loads(await u.get_rows("S", ["a"]))

        assert out == {"error": "Tenant not set"}
        assert fake_driver.calls == []

    async def test_no_data_from_driver_returns_error(self, utils_instance, fake_driver):
        fake_driver.result = None

        out = json.loads(await utils_instance.get_rows("S", ["a"]))

        assert out == {"error": "No data returned from get_data"}

    async def test_bad_filter_returns_its_message_to_caller(self, utils_instance, fake_driver):
        out = json.loads(await utils_instance.get_rows("S", ["a"], [{"field": "a"}]))

        assert "requires 'value'" in out["error"]

    async def test_upstream_failure_hides_internal_detail(self, utils_instance, fake_driver):
        fake_driver.error = RuntimeError("secret-host.internal refused connection")

        out = json.loads(await utils_instance.get_rows("S", ["a"]))

        assert out["error"] == "internal error"
        assert out["correlation_id"]
        assert "secret-host" not in json.dumps(out)


# ---------------------------------------------------------------------------
# get_top_n
# ---------------------------------------------------------------------------

class TestGetTopN:
    async def test_top_n_returns_ranking_metadata_and_rows(self, utils_instance, fake_driver, duckdb_dir):
        fake_driver.result = pd.DataFrame({"Person": ["A", "B"], "Sales": [10, 5]})

        out = json.loads(await utils_instance.get_top_n("S", "Person", "Sales", 2))

        assert out["ranking_type"] == "top"
        assert out["n"] == 2
        assert out["group_by"] == "Person" and out["order_by"] == "Sales"
        assert out["row_count"] == 2
        assert out["data"] == [{"Person": "A", "Sales": 10}, {"Person": "B", "Sales": 5}]
        assert out["instance_id"] == ""

    async def test_negative_n_is_reported_as_bottom_with_positive_count(self, utils_instance, fake_driver, duckdb_dir):
        fake_driver.result = pd.DataFrame({"Person": ["A"], "Sales": [1]})

        out = json.loads(await utils_instance.get_top_n("S", "Person", "Sales", -3))

        assert out["ranking_type"] == "bottom"
        assert out["n"] == 3

    async def test_asks_driver_for_group_and_metric_with_top_n_option(self, utils_instance, fake_driver, duckdb_dir):
        fake_driver.result = pd.DataFrame({"Person": ["A"], "Sales": [1]})

        await utils_instance.get_top_n("S", "Person", "Sales", 5)

        _, fields, filters, top_n = fake_driver.calls[0]
        assert fields == ["Person", "Sales"]
        assert filters == []
        assert list(top_n.keys()) == ["Person"]

    async def test_large_result_is_sampled_and_saved(self, utils_instance, fake_driver, duckdb_dir, monkeypatch):
        monkeypatch.setenv("MCP_SAMPLE_ROWS", "2")
        fake_driver.result = pd.DataFrame({"Person": list("abcdef"), "Sales": range(6)})

        out = json.loads(await utils_instance.get_top_n("S", "Person", "Sales", 6))

        assert out["row_count"] == 6
        assert len(out["data"]) == 2
        assert list(duckdb_dir.glob("*/" + out["instance_id"] + ".duckdb"))

    async def test_missing_tenant_returns_error(self, fake_driver):
        u = mcp_utils("k", "", "c", "u", "s", None)

        out = json.loads(await u.get_top_n("S", "g", "o", 1))

        assert out == {"error": "Tenant not set"}

    async def test_no_data_from_driver_returns_error(self, utils_instance, fake_driver):
        fake_driver.result = None

        out = json.loads(await utils_instance.get_top_n("S", "g", "o", 1))

        assert out == {"error": "No data returned from get_top_n"}

    async def test_bad_filter_returns_its_message_to_caller(self, utils_instance, fake_driver):
        out = json.loads(await utils_instance.get_top_n("S", "g", "o", 1, [{"value": 1}]))

        assert "missing 'field'" in out["error"]

    async def test_upstream_failure_hides_internal_detail(self, utils_instance, fake_driver):
        fake_driver.error = RuntimeError("disk-path-leak exploded")

        out = json.loads(await utils_instance.get_top_n("S", "g", "o", 1))

        assert out["error"] == "internal error"
        assert "disk-path-leak" not in json.dumps(out)
