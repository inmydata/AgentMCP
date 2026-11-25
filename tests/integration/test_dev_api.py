"""Live tests of both MCP servers against the local dev inmydata API.

Each test runs once per server (``local-stdio`` and ``remote-http``, see the
``mcp_client`` fixture) and calls the tools through the MCP protocol, so the
whole path is covered: MCP transport, tool handler, ``mcp_utils``, the
inmydata SDK and the API.

Run with: ``uv run pytest --integration -m integration``
"""
import json
from datetime import date
from typing import Any

import pytest
from fastmcp import Client

pytestmark = pytest.mark.integration

EXPECTED_TOOLS = {
    "get_schema",
    "get_rows_fast",
    "get_top_n_fast",
    "query_results_fast",
    "get_financial_periods",
    "get_calendar_period_date_range",
}


async def call_json(client: Client, tool: str, args: dict[str, Any] | None = None) -> Any:
    """Call ``tool``, check it did not fail, and return its JSON-decoded text."""
    result = await client.call_tool(tool, args or {})
    assert result.content, f"{tool} returned no content"
    text = result.content[0].text
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        pytest.fail(f"{tool} did not return JSON: {text[:500]}")
    if isinstance(payload, dict):
        assert "error" not in payload, f"{tool} returned an error: {payload}"
    return payload


async def test_server_lists_the_inmydata_tools(mcp_client: Client) -> None:
    async with mcp_client as client:
        names = {tool.name for tool in await client.list_tools()}
    assert EXPECTED_TOOLS <= names


async def test_get_schema_returns_subjects(mcp_client: Client) -> None:
    async with mcp_client as client:
        schema = await call_json(client, "get_schema")

    subjects = schema["subjects"]
    assert subjects, "dev tenant has no subjects"
    for subject in subjects:
        assert subject["name"]
        assert isinstance(subject["factFieldTypes"], dict)
        assert isinstance(subject["metricFieldTypes"], dict)
        # Added by mcp_utils on top of the API response.
        assert "dashboardHints" in subject
        assert "fieldGroups" in subject


async def test_get_financial_periods_for_a_fixed_date(mcp_client: Client) -> None:
    async with mcp_client as client:
        result = await call_json(client, "get_financial_periods", {"target_date": "2025-06-15"})

    assert result["date"] == "2025-06-15"
    periods = json.loads(result["periods"])
    assert periods == {"year": 2025, "month": 6, "week": 24, "quarter": 2}


async def test_get_calendar_period_date_range_for_a_fixed_period(mcp_client: Client) -> None:
    async with mcp_client as client:
        result = await call_json(
            client,
            "get_calendar_period_date_range",
            {"financial_year": 2025, "period_number": 1, "period_type": "month"},
        )

    assert result == {
        "start_date": "2024-12-30",
        "end_date": "2025-01-26",
        "financial_year": 2025,
        "period_number": 1,
        "period_type": "month",
    }


async def test_get_calendar_period_date_range_defaults_to_the_current_month(
    mcp_client: Client,
) -> None:
    async with mcp_client as client:
        today = json.loads((await call_json(client, "get_financial_periods"))["periods"])
        result = await call_json(client, "get_calendar_period_date_range")

    assert result["financial_year"] == today["year"]
    assert result["period_number"] == today["month"]
    assert result["period_type"] == "month"
    assert result["start_date"] <= date.today().isoformat() <= result["end_date"]


# --- Data tools ---------------------------------------------------------------
# Expected figures are for financial year 2024 of the dev tenant's
# "Inmystore Sales" subject. It is a closed year, so they should not change.
# If the dev data is reloaded, update them here.

SUBJECT = "Inmystore Sales"
FY_2024 = [{"field": "Financial Year", "op": "equals", "value": 2024}]
TOTAL_SALES_2024 = 75_646_793.67


async def test_get_rows_fast_returns_sales_by_region(mcp_client: Client) -> None:
    async with mcp_client as client:
        result = await call_json(client, "get_rows_fast", {
            "subject": SUBJECT,
            "select": ["Region", "Sales Value", "Transactions"],
            "where": FY_2024,
        })

    # 4 rows is within the 10-row sample limit, so all rows come back inline.
    assert result["row_count"] == 4
    assert result["instance_id"] == ""
    by_region = {row["Region"]: row for row in result["data"]}
    assert by_region == {
        "England": {"Region": "England", "Sales Value": pytest.approx(58_567_630.60, abs=0.01), "Transactions": 59_175},
        "Northern Ireland": {"Region": "Northern Ireland", "Sales Value": pytest.approx(1_685_288.04, abs=0.01), "Transactions": 2_044},
        "Scotland": {"Region": "Scotland", "Sales Value": pytest.approx(7_067_964.24, abs=0.01), "Transactions": 5_342},
        "Wales": {"Region": "Wales", "Sales Value": pytest.approx(8_325_910.79, abs=0.01), "Transactions": 7_130},
    }


async def test_get_top_n_fast_returns_top_stores(mcp_client: Client) -> None:
    async with mcp_client as client:
        result = await call_json(client, "get_top_n_fast", {
            "subject": SUBJECT, "group_by": "Store", "order_by": "Sales Value", "n": 3, "where": FY_2024,
        })

    assert result["ranking_type"] == "top"
    assert [(r["Store"], r["Sales Value"]) for r in result["data"]] == [
        ("Liverpool", pytest.approx(11_269_759.63, abs=0.01)),
        ("Birmingham", pytest.approx(5_053_474.08, abs=0.01)),
        ("London", pytest.approx(4_393_452.56, abs=0.01)),
    ]


async def test_get_top_n_fast_returns_bottom_stores(mcp_client: Client) -> None:
    async with mcp_client as client:
        result = await call_json(client, "get_top_n_fast", {
            "subject": SUBJECT, "group_by": "Store", "order_by": "Sales Value", "n": -3, "where": FY_2024,
        })

    assert result["ranking_type"] == "bottom"
    assert {r["Store"]: r["Sales Value"] for r in result["data"]} == {
        "Derby": pytest.approx(1_429_013.29, abs=0.01),
        "Plymouth": pytest.approx(1_149_342.10, abs=0.01),
        "Southampton": pytest.approx(10_378.37, abs=0.01),
    }


async def test_large_result_is_saved_to_duckdb_and_queryable(mcp_client: Client) -> None:
    async with mcp_client as client:
        rows = await call_json(client, "get_rows_fast", {
            "subject": SUBJECT, "select": ["Store", "Sales Value"], "where": FY_2024,
        })
        # 25 stores is over the 10-row sample limit: a sample comes back
        # inline and the full result is saved to DuckDB.
        assert rows["row_count"] == 25
        assert len(rows["data"]) == 10
        assert rows["instance_id"]

        totals = await call_json(client, "query_results_fast", {
            "instance_id": rows["instance_id"],
            "sql": 'SELECT COUNT(*) AS n, SUM("Sales Value") AS total FROM my_table',
        })

    assert totals["data"] == [{"n": 25, "total": pytest.approx(TOTAL_SALES_2024, abs=0.01)}]
