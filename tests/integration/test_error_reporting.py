"""Live tests of the error messages both MCP servers return for common mistakes.

Each test runs once per server (``local-stdio`` and ``remote-http``) and makes
a mistake a model or user might make: a wrong field, subject or calendar name,
or a bad API key. It checks the client gets a message it can act on, with a
correlation id, rather than "internal error".

Like ``test_dev_api.py``, these tests use the "Inmystore Sales" subject, which
both the local dev tenant and the test tenant have.

Run with: ``uv run pytest --integration -m integration``
"""
import json
import re
from typing import Any, Callable

import pytest
from fastmcp import Client

pytestmark = pytest.mark.integration

CID_RE = re.compile(r"^[0-9a-f]{8}$")


async def call_error(client: Client, tool: str, args: dict[str, Any] | None = None) -> dict:
    """Call ``tool``, check it returned an error payload, and return the payload."""
    result = await client.call_tool(tool, args or {})
    payload = json.loads(result.content[0].text)
    assert "error" in payload, f"{tool} did not return an error: {payload}"
    assert payload["error"] != "internal error", f"{tool} gave no detail: {payload}"
    assert CID_RE.match(payload.get("correlation_id", "")), payload
    return payload


SUBJECT = "Inmystore Sales"
FY_2024 = [{"field": "Financial Year", "op": "equals", "value": 2024}]
REJECTED = "The inmydata API rejected the request (HTTP 400): "
SCHEMA_HINT = (
    " Call get_schema to see the subjects and field names available; names must "
    "match the schema exactly, including case and spaces."
)


async def test_wrong_select_field_returns_the_api_message(mcp_client: Client) -> None:
    async with mcp_client as client:
        payload = await call_error(client, "get_rows_fast", {
            "subject": SUBJECT, "select": ["Invoice Date", "Sales Value"],
        })

    assert payload["error"] == (
        REJECTED + "Field 'Invoice Date' not found in subject 'Inmystore Sales'. "
        "Use a field from the subject schema or remove it from Fields." + SCHEMA_HINT
    )


async def test_wrong_where_field_returns_the_api_message(mcp_client: Client) -> None:
    async with mcp_client as client:
        payload = await call_error(client, "get_rows_fast", {
            "subject": SUBJECT,
            "select": ["Region"],
            "where": [{"field": "Invoice Year", "op": "equals", "value": 2024}],
        })

    assert payload["error"] == (
        REJECTED + "Filter field 'Invoice Year' not found in subject 'Inmystore Sales'. "
        "Use a field from the subject schema or remove the filter." + SCHEMA_HINT
    )


async def test_wrong_top_n_field_returns_the_api_message(mcp_client: Client) -> None:
    async with mcp_client as client:
        payload = await call_error(client, "get_top_n_fast", {
            "subject": SUBJECT, "group_by": "Shop", "order_by": "Sales Value", "n": 3, "where": FY_2024,
        })

    assert payload["error"] == (
        REJECTED + "Field 'Shop' not found in subject 'Inmystore Sales'. "
        "Use a field from the subject schema or remove it from Fields." + SCHEMA_HINT
    )


async def test_wrong_subject_explains_the_likely_causes(mcp_client: Client) -> None:
    async with mcp_client as client:
        payload = await call_error(client, "get_rows_fast", {"subject": "No Such Subject", "select": ["Region"]})

    assert payload["error"].startswith("The inmydata API refused the request (HTTP 403).")
    assert "get_schema" in payload["error"]


async def test_wrong_calendar_returns_the_api_message(
    mcp_client_factory: Callable[..., Client],
) -> None:
    async with mcp_client_factory(calendar="No Such Calendar") as client:
        payload = await call_error(client, "get_financial_periods", {"target_date": "2025-06-15"})

    assert payload["error"].startswith("The inmydata API rejected the request (HTTP 400): ")
    assert "alendar" in payload["error"]
    assert "get_schema" not in payload["error"]


async def test_bad_api_key_says_the_credentials_were_not_accepted(
    mcp_client_factory: Callable[..., Client],
) -> None:
    async with mcp_client_factory(api_key="not-a-real-key") as client:
        payload = await call_error(client, "get_schema")

    assert payload["error"].startswith("The inmydata API did not accept the credentials (HTTP 401)")


async def test_badly_formed_date_explains_the_format(mcp_client: Client) -> None:
    async with mcp_client as client:
        result = await client.call_tool("get_financial_periods", {"target_date": "15/06/2025"})

    assert json.loads(result.content[0].text) == {
        "error": "target_date must be a date in YYYY-MM-DD format, for example 2025-06-15."
    }


async def test_sql_mistake_lists_the_result_columns(mcp_client: Client) -> None:
    async with mcp_client as client:
        # 25 stores in FY2024: more than the 10-row sample, so the result is saved.
        rows = json.loads((await client.call_tool("get_rows_fast", {
            "subject": SUBJECT, "select": ["Store", "Sales Value"], "where": FY_2024,
        })).content[0].text)
        assert rows["instance_id"], rows
        payload = await call_error(client, "query_results_fast", {
            "instance_id": rows["instance_id"], "sql": 'SELECT SUM("Sales") FROM my_table',
        })

    assert payload["error"] == (
        "The SQL could not be run against my_table. Check that it is valid DuckDB SQL "
        'and uses only these columns, in double quotes: "Store", "Sales Value".'
    )
