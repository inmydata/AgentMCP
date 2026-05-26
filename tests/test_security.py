"""
Tests for the DuckDB sandbox and instance_id validation added to mcp_utils.
Covers GHSA-8g22-5f3w-j68p (issue #4).

Run with:  python -m pytest tests/ -v
"""
import json
import os
import sys
import uuid
from pathlib import Path

import duckdb
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_utils import (  # noqa: E402
    InvalidInstanceId,
    UnsafeSql,
    _assert_sql_safe,
    _open_sandboxed_connection,
    _resolve_duckdb_path,
    _split_statements,
    _strip_sql_comments,
    _validate_instance_id,
    mcp_utils,
)


@pytest.fixture
def duckdb_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_DUCKDB_LOCATION", str(tmp_path))
    return tmp_path


@pytest.fixture
def populated_instance(duckdb_dir):
    """Create a real DuckDB file at {duckdb_dir}/{uuid}.duckdb with a my_table."""
    instance_id = str(uuid.uuid4())
    path = duckdb_dir / f"{instance_id}.duckdb"
    con = duckdb.connect(str(path))
    try:
        con.execute("CREATE TABLE my_table AS SELECT 1 AS a, 'hello' AS b")
    finally:
        con.close()
    return instance_id


@pytest.fixture
def utils_instance():
    return mcp_utils(
        api_key="test",
        tenant="test",
        calendar="test",
        user="test",
        session_id="test",
        server=None,
    )


# ---------------------------------------------------------------------------
# _validate_instance_id
# ---------------------------------------------------------------------------

class TestValidateInstanceId:
    def test_accepts_valid_uuid(self):
        valid = str(uuid.uuid4())
        assert _validate_instance_id(valid) == valid

    def test_accepts_uppercase_uuid(self):
        valid = str(uuid.uuid4()).upper()
        assert _validate_instance_id(valid) == valid

    @pytest.mark.parametrize("bad", [
        "",
        "not-a-uuid",
        "../somethingelse",
        "../" + str(uuid.uuid4()),
        "/etc/passwd",
        "C:\\Windows\\System32\\config\\SAM",
        "..%2F" + str(uuid.uuid4()),
        str(uuid.uuid4()) + "/../other",
        str(uuid.uuid4()) + "\x00",
        "00000000-0000-0000-0000-00000000000",   # 35 chars
        "00000000-0000-0000-0000-0000000000000",  # 37 chars
        "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz",  # non-hex
    ])
    def test_rejects_invalid(self, bad):
        with pytest.raises(InvalidInstanceId):
            _validate_instance_id(bad)

    @pytest.mark.parametrize("bad", [None, 123, [], {}, b"00000000-0000-0000-0000-000000000000"])
    def test_rejects_non_string(self, bad):
        with pytest.raises(InvalidInstanceId):
            _validate_instance_id(bad)


# ---------------------------------------------------------------------------
# _resolve_duckdb_path
# ---------------------------------------------------------------------------

class TestResolveDuckdbPath:
    def test_resolves_inside_base(self, duckdb_dir):
        instance_id = str(uuid.uuid4())
        resolved = _resolve_duckdb_path(instance_id)
        assert resolved.parent == duckdb_dir.resolve()
        assert resolved.name == f"{instance_id}.duckdb"

    def test_rejects_traversal_after_format_check(self, duckdb_dir):
        with pytest.raises(InvalidInstanceId):
            _resolve_duckdb_path("../escape")

    def test_symlink_escape_is_blocked(self, tmp_path, monkeypatch):
        """If the configured base directory contains a symlink to elsewhere,
        the resolved path must still live inside the realpath of the base."""
        base = tmp_path / "base"
        base.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        instance_id = str(uuid.uuid4())
        link = base / f"{instance_id}.duckdb"
        target = outside / f"{instance_id}.duckdb"
        target.write_text("")
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform/account")
        monkeypatch.setenv("MCP_DUCKDB_LOCATION", str(base))
        with pytest.raises(InvalidInstanceId):
            _resolve_duckdb_path(instance_id)


# ---------------------------------------------------------------------------
# SQL comment stripping and statement splitting
# ---------------------------------------------------------------------------

class TestSqlParsingHelpers:
    def test_strips_line_comments(self):
        assert "ATTACH" not in _strip_sql_comments("SELECT 1 -- ATTACH 'x'\n FROM my_table").upper().split()[-1:]

    def test_strips_block_comments(self):
        assert "ATTACH" not in _strip_sql_comments("/* ATTACH 'x' */ SELECT 1")

    def test_split_ignores_semicolons_in_strings(self):
        stmts = _split_statements("SELECT 'a;b' AS x; SELECT 2")
        assert len(stmts) == 2
        assert "a;b" in stmts[0]

    def test_split_handles_doubled_quote_escape(self):
        stmts = _split_statements("SELECT 'It''s; fine' AS x; SELECT 2")
        assert len(stmts) == 2

    def test_split_ignores_semicolons_in_quoted_identifiers(self):
        stmts = _split_statements('SELECT "a;b" FROM my_table; SELECT 2')
        assert len(stmts) == 2


# ---------------------------------------------------------------------------
# _assert_sql_safe
# ---------------------------------------------------------------------------

class TestAssertSqlSafe:
    @pytest.mark.parametrize("sql", [
        "SELECT * FROM my_table",
        "SELECT 1",
        "  select 1  ",
        "WITH cte AS (SELECT 1) SELECT * FROM cte",
        "SELECT * FROM my_table WHERE col = 'attach this'",
        'SELECT "attach" FROM my_table',
        "SELECT * FROM read_csv_auto('/etc/passwd')",  # blocked at runtime by sandbox, not by keyword filter
    ])
    def test_allows_safe_sql(self, sql):
        _assert_sql_safe(sql)  # should not raise

    @pytest.mark.parametrize("sql", [
        "ATTACH 'other.duckdb'",
        "attach 'other.duckdb'",
        "  ATTACH 'other.duckdb'",
        "COPY (SELECT 1) TO '/tmp/pwn.csv'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "PRAGMA database_list",
        "SET enable_external_access=true",
        "RESET enable_external_access",
        "DETACH other",
        "USE other_db",
        "CHECKPOINT",
        "EXPORT DATABASE '/tmp/leak'",
        "IMPORT DATABASE '/tmp/leak'",
    ])
    def test_blocks_disallowed_lead_keyword(self, sql):
        with pytest.raises(UnsafeSql):
            _assert_sql_safe(sql)

    def test_blocks_second_statement(self):
        with pytest.raises(UnsafeSql):
            _assert_sql_safe("SELECT 1; ATTACH 'other.duckdb'")

    def test_blocks_keyword_hidden_behind_line_comment(self):
        with pytest.raises(UnsafeSql):
            _assert_sql_safe("-- benign\nATTACH 'other.duckdb'")

    def test_blocks_keyword_hidden_behind_block_comment(self):
        with pytest.raises(UnsafeSql):
            _assert_sql_safe("/* benign */ ATTACH 'other.duckdb'")

    def test_does_not_flag_keyword_inside_string(self):
        _assert_sql_safe("SELECT 'ATTACH something' AS msg")

    def test_does_not_flag_keyword_as_column_name(self):
        _assert_sql_safe('SELECT "attach" FROM my_table')

    def test_rejects_non_string(self):
        with pytest.raises(UnsafeSql):
            _assert_sql_safe(None)


# ---------------------------------------------------------------------------
# _open_sandboxed_connection — functional test: external access really blocked
# ---------------------------------------------------------------------------

class TestSandboxedConnection:
    def test_blocks_read_csv_auto_of_external_file(self, tmp_path, duckdb_dir, populated_instance):
        external = tmp_path / "outside.csv"
        external.write_text("a,b\n1,2\n")
        db_path = duckdb_dir / f"{populated_instance}.duckdb"
        con = _open_sandboxed_connection(db_path, read_only=False)
        try:
            with pytest.raises(Exception):
                con.execute(f"SELECT * FROM read_csv_auto('{external.as_posix()}')").df()
        finally:
            con.close()

    def test_blocks_copy_to_external_file(self, tmp_path, duckdb_dir, populated_instance):
        out = tmp_path / "leak.csv"
        db_path = duckdb_dir / f"{populated_instance}.duckdb"
        con = _open_sandboxed_connection(db_path, read_only=False)
        try:
            with pytest.raises(Exception):
                con.execute(f"COPY (SELECT 1) TO '{out.as_posix()}'")
        finally:
            con.close()
        assert not out.exists()

    def test_blocks_runtime_unlock_attempt(self, duckdb_dir, populated_instance):
        db_path = duckdb_dir / f"{populated_instance}.duckdb"
        con = _open_sandboxed_connection(db_path, read_only=False)
        try:
            with pytest.raises(Exception):
                con.execute("SET enable_external_access=true")
        finally:
            con.close()

    def test_normal_select_still_works(self, duckdb_dir, populated_instance):
        db_path = duckdb_dir / f"{populated_instance}.duckdb"
        con = _open_sandboxed_connection(db_path, read_only=False)
        try:
            df = con.execute("SELECT * FROM my_table").df()
        finally:
            con.close()
        assert len(df) == 1
        assert df.iloc[0]["a"] == 1


# ---------------------------------------------------------------------------
# query_results end-to-end
# ---------------------------------------------------------------------------

class TestQueryResults:
    @pytest.mark.asyncio
    async def test_returns_rows_for_legitimate_select(self, utils_instance, duckdb_dir, populated_instance):
        result_json = await utils_instance.query_results(populated_instance, "SELECT * FROM my_table")
        result = json.loads(result_json)
        assert result.get("row_count") == 1
        assert result.get("instance_id") == populated_instance
        assert result.get("data")[0]["a"] == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_id", [
        "../escape",
        "/etc/passwd",
        "not-a-uuid",
        "",
        str(uuid.uuid4()) + "/../other",
    ])
    async def test_rejects_bad_instance_id_with_generic_error(self, utils_instance, duckdb_dir, bad_id):
        result_json = await utils_instance.query_results(bad_id, "SELECT 1")
        result = json.loads(result_json)
        assert result.get("error") == "invalid request"
        assert "correlation_id" in result
        # Must not leak the reason
        assert "traversal" not in result_json.lower()
        assert "uuid" not in result_json.lower()
        if bad_id:
            assert bad_id not in result_json

    @pytest.mark.asyncio
    async def test_rejects_unsafe_sql_with_generic_error(self, utils_instance, duckdb_dir, populated_instance):
        result_json = await utils_instance.query_results(
            populated_instance, "ATTACH 'other.duckdb'"
        )
        result = json.loads(result_json)
        assert result.get("error") == "invalid request"
        assert "ATTACH" not in result_json
        assert "keyword" not in result_json.lower()

    @pytest.mark.asyncio
    async def test_external_access_blocked_end_to_end(
        self, utils_instance, duckdb_dir, populated_instance, tmp_path
    ):
        external = tmp_path / "outside.csv"
        external.write_text("a,b\n9,9\n")
        sql = f"SELECT * FROM read_csv_auto('{external.as_posix()}')"
        result_json = await utils_instance.query_results(populated_instance, sql)
        result = json.loads(result_json)
        # The keyword filter doesn't block read_csv_auto (it's a function, not a leading keyword);
        # the sandbox setting must block it at execution time.
        assert "error" in result
        # The legitimate row from the external file must NOT appear in any successful result.
        assert "data" not in result or all(
            row.get("a") != 9 for row in result.get("data", [])
        )
