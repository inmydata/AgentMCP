"""Live tests of the error messages both MCP servers return for common mistakes.

Each test runs once per server (``local-stdio`` and ``remote-http``) and makes
a mistake a model or user might make: a wrong field, subject or calendar name,
or a bad API key. It checks the client gets a message it can act on, with a
correlation id, rather than "internal error".

The subject and field names are taken from ``get_schema``, so these tests do
not depend on any particular tenant's data.

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


async def first_subject(client: Client) -> tuple[str, str]:
    """The name and one dimension field of the first subject in the schema."""
    result = await client.call_tool("get_schema", {})
    subject = json.loads(result.content[0].text)["subjects"][0]
    return subject["name"], next(iter(subject["factFieldTypes"]))


async def test_wrong_select_field_returns_the_api_message(mcp_client: Client) -> None:
    async with mcp_client as client:
        subject, _ = await first_subject(client)
        payload = await call_error(client, "get_rows_fast", {"subject": subject, "select": ["No Such Field"]})

    assert payload["error"].startswith("The inmydata API rejected the request (HTTP 400): ")
    assert "No Such Field" in payload["error"]
    assert "Call get_schema" in payload["error"]
    assert "https://" not in payload["error"]


async def test_wrong_where_field_returns_the_api_message(mcp_client: Client) -> None:
    async with mcp_client as client:
        subject, field = await first_subject(client)
        payload = await call_error(client, "get_rows_fast", {
            "subject": subject,
            "select": [field],
            "where": [{"field": "No Such Field", "op": "equals", "value": 1}],
        })

    assert payload["error"].startswith("The inmydata API rejected the request (HTTP 400): ")
    assert "No Such Field" in payload["error"]


async def test_wrong_top_n_field_returns_the_api_message(mcp_client: Client) -> None:
    async with mcp_client as client:
        subject, _ = await first_subject(client)
        payload = await call_error(client, "get_top_n_fast", {
            "subject": subject, "group_by": "No Such Field", "order_by": "No Such Metric", "n": 3,
        })

    assert payload["error"].startswith("The inmydata API rejected the request (HTTP 400): ")
    assert "Call get_schema" in payload["error"]


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


MAX_SUBJECTS_TRIED = 5


async def saved_result(client: Client) -> dict | None:
    """A get_rows_fast result large enough to be saved for query_results_fast, or None.

    Tries the first few subjects in the schema, selecting each one's first
    three dimensions: together they give enough combinations for more rows
    than the inline sample. Subjects that return an error (for example one
    not enabled for the API) are passed over.
    """
    schema = json.loads((await client.call_tool("get_schema", {})).content[0].text)
    for subject in schema["subjects"][:MAX_SUBJECTS_TRIED]:
        fields = list(subject["factFieldTypes"])[:3]
        if not fields:
            continue
        rows = json.loads((await client.call_tool(
            "get_rows_fast", {"subject": subject["name"], "select": fields},
        )).content[0].text)
        if rows.get("instance_id"):
            return rows
    return None


async def test_sql_mistake_lists_the_result_columns(mcp_client: Client) -> None:
    async with mcp_client as client:
        rows = await saved_result(client)
        if rows is None:
            pytest.skip(f"none of the first {MAX_SUBJECTS_TRIED} subjects returned enough rows to be saved")
        payload = await call_error(client, "query_results_fast", {
            "instance_id": rows["instance_id"], "sql": 'SELECT SUM("No Such Column") FROM my_table',
        })

    assert payload["error"].startswith("The SQL could not be run against my_table.")
    assert f'"{rows["columns"][0]}"' in payload["error"]
    assert "Binder" not in payload["error"]
