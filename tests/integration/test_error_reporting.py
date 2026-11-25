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
