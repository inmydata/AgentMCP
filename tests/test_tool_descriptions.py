"""Tests that the tool descriptions and schemas both MCP servers publish stay accurate.

The descriptions in ``tool_descriptions.py`` name tools and parameters. These
tests fail when one of those names stops existing, which is how descriptions
usually go stale (a tool or parameter is renamed and the text is not). They
also check that each server publishes the shared descriptions, marks required
parameters as required, and lists the allowed filter operators in the schema.
Output examples in the descriptions are compared with what mcp_utils really
returns, using fakes for the inmydata SDK.

Both servers are loaded over the MCP protocol with an in-memory client, so
what is checked is what a real client sees.
"""
import importlib
import json
import re
import sys
from pathlib import Path

import pandas as pd
import pytest
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dotenv  # noqa: E402
import mcp_logging  # noqa: E402
import mcp_utils as mcp_utils_module  # noqa: E402
import tool_descriptions as docs  # noqa: E402
from inmydata.CalendarAssistant import FinancialPeriodDetails  # noqa: E402
from mcp_utils import mcp_utils  # noqa: E402
from tool_descriptions import WhereCondition, where_as_dicts  # noqa: E402

DESCRIPTIONS = {
    "get_rows_fast": docs.GET_ROWS_FAST,
    "get_top_n_fast": docs.GET_TOP_N_FAST,
    "query_results_fast": docs.QUERY_RESULTS_FAST,
    "get_answer_slow": docs.GET_ANSWER_SLOW,
    "get_schema": docs.GET_SCHEMA,
    "get_financial_periods": docs.GET_FINANCIAL_PERIODS,
    "get_calendar_period_date_range": docs.GET_CALENDAR_PERIOD_DATE_RANGE,
}

REQUIRED = {
    "get_rows_fast": {"subject", "select"},
    "get_top_n_fast": {"subject", "group_by", "order_by"},
    "query_results_fast": {"instance_id", "sql"},
    "get_answer_slow": {"question"},
}

# Anything that looks like one of this server's tool names.
TOOL_NAME_RE = re.compile(r"\b(?:get_[a-z_]+|query_results_fast|agentic_rag_query)\b")
# A keyword argument in an example, such as subject="Sales" or n=10.
EXAMPLE_ARG_RE = re.compile(r"\b([a-z_]+)=")
# An output example: a line starting "Example result", then indented JSON lines.
EXAMPLE_RESULT_RE = re.compile(r"^Example result[^\n]*:\n((?:  [^\n]*\n?)+)", re.MULTILINE)


def _example_result(text: str) -> dict:
    """Parse the one output example in a description."""
    blocks = EXAMPLE_RESULT_RE.findall(text)
    assert len(blocks) == 1, f"expected one output example, found {len(blocks)}"
    return json.loads(blocks[0])


@pytest.fixture(params=["server", "server_remote"])
async def published_tools(request, monkeypatch):
    """The tools (name -> mcp Tool) and instructions one server publishes."""
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(mcp_logging, "configure_logging", lambda *a, **k: None)
    monkeypatch.setenv("INMYDATA_USE_OAUTH", "false")
    for name in ("AGENTIC_RAG_BASE_URL", "AGENTIC_RAG_PSK", "AGENTIC_RAG_DEFAULT_EXTERNAL_ID"):
        monkeypatch.delenv(name, raising=False)
    sys.modules.pop(request.param, None)
    module = importlib.import_module(request.param)
    try:
        async with Client(module.mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
            yield tools, client.initialize_result.instructions
    finally:
        sys.modules.pop(request.param, None)


async def test_each_tool_publishes_the_shared_description(published_tools):
    tools, _ = published_tools

    for name, text in DESCRIPTIONS.items():
        assert tools[name].description == text, name


async def test_the_server_publishes_the_shared_instructions(published_tools):
    _, instructions = published_tools

    assert instructions == docs.SERVER_INSTRUCTIONS


async def test_every_tool_named_in_the_text_exists(published_tools):
    tools, instructions = published_tools
    texts = [*DESCRIPTIONS.values(), instructions, tools["agentic_rag_query"].description]

    named = {m for text in texts for m in TOOL_NAME_RE.findall(text)}

    assert named <= set(tools), f"descriptions name tools that do not exist: {named - set(tools)}"


async def test_every_argument_in_an_example_is_a_parameter_of_that_tool(published_tools):
    tools, _ = published_tools

    for name, text in DESCRIPTIONS.items():
        params = set(tools[name].inputSchema.get("properties", {}))
        used = set(EXAMPLE_ARG_RE.findall(text))
        assert used <= params, f"{name} description uses unknown parameters {used - params}"


async def test_required_parameters_are_marked_required(published_tools):
    tools, _ = published_tools

    for name, required in REQUIRED.items():
        assert set(tools[name].inputSchema.get("required", [])) == required, name


async def test_filter_operators_and_period_types_are_listed_in_the_schema(published_tools):
    tools, _ = published_tools

    condition = tools["get_rows_fast"].inputSchema["$defs"]["WhereCondition"]["properties"]
    assert condition["op"]["enum"] == [
        "equals", "not_equals", "contains", "not_contains", "starts_with", "gt", "gte", "lt", "lte"
    ]
    assert condition["logical"]["enum"] == ["AND", "OR"]
    period = tools["get_calendar_period_date_range"].inputSchema["properties"]["period_type"]
    assert {"enum": ["year", "quarter", "month", "week"], "type": "string"} in period["anyOf"]


def test_every_documented_operator_is_one_mcp_utils_accepts():
    from mcp_utils import mcp_utils

    for op in WhereCondition.model_fields["op"].annotation.__args__:
        assert op in mcp_utils._OP_ALIASES, op


def test_where_as_dicts_keeps_only_the_keys_the_caller_gave():
    conditions = [
        WhereCondition.model_validate({"field": "Region", "value": "Wales", "start_group": 1}),
        WhereCondition.model_validate({"field": "Year", "value": 2024, "case_insensitive": False}),
    ]

    assert where_as_dicts(conditions) == [
        {"field": "Region", "value": "Wales", "start_group": 1},
        {"field": "Year", "value": 2024, "case_insensitive": False},
    ]


def test_where_as_dicts_passes_plain_dicts_through():
    where = [{"field": "Region", "op": "equals", "value": "North"}]

    assert where_as_dicts(where) == where
    assert where_as_dicts(None) == []


# --- Output examples ----------------------------------------------------------

class _FakeDriver:
    """Stands in for StructuredDataDriver; returns ``result`` for every query."""

    result: pd.DataFrame | None = None

    def __init__(self, *args, **kwargs):
        pass

    def get_data(self, *args):
        return _FakeDriver.result


@pytest.fixture
def real_utils(tmp_path, monkeypatch):
    """An mcp_utils with the SDK faked, the default sample size and DuckDB under tmp_path."""
    monkeypatch.setenv("MCP_DUCKDB_LOCATION", str(tmp_path))
    monkeypatch.delenv("MCP_SAMPLE_ROWS", raising=False)
    monkeypatch.setattr(mcp_utils_module, "StructuredDataDriver", _FakeDriver)
    return mcp_utils("key", "test", "Default", "user", "session", None)


def test_every_output_example_is_valid_json():
    for name, text in DESCRIPTIONS.items():
        for block in EXAMPLE_RESULT_RE.findall(text):
            json.loads(block)  # raises, naming the bad character, if not


async def test_get_rows_example_matches_a_real_sampled_result(real_utils):
    example = _example_result(docs.GET_ROWS_FAST)
    _FakeDriver.result = pd.DataFrame({"Store": [f"s{i}" for i in range(25)], "Sales Value": range(25)})

    real = json.loads(await real_utils.get_rows("Sales", ["Store", "Sales Value"]))

    assert set(real) == set(example)
    assert real["columns"] == example["columns"]
    assert real["row_count"] == example["row_count"]
    assert real["instance_id"] and example["instance_id"]
    assert real["note"].startswith(example["note"].removesuffix(" ..."))


async def test_get_top_n_example_matches_a_real_complete_result(real_utils):
    example = _example_result(docs.GET_TOP_N_FAST)
    _FakeDriver.result = pd.DataFrame({"Store": [f"s{i}" for i in range(10)], "Sales Value": range(10)})

    real = json.loads(await real_utils.get_top_n("Sales", "Store", "Sales Value", 10))

    assert set(real) == set(example)
    for key in ("ranking_type", "n", "group_by", "order_by", "row_count", "columns", "instance_id"):
        assert real[key] == example[key], key


async def test_get_financial_periods_example_is_exactly_what_is_returned(real_utils, monkeypatch):
    example = _example_result(docs.GET_FINANCIAL_PERIODS)

    class _Assistant:
        def __init__(self, *args, **kwargs):
            pass

        def get_financial_periods(self, _date):
            return FinancialPeriodDetails(year=2025, month=6, week=24, quarter=2)

    monkeypatch.setattr("inmydata.CalendarAssistant.CalendarAssistant", _Assistant)

    real = json.loads(await real_utils.get_financial_periods("2025-06-15"))

    assert real == example
