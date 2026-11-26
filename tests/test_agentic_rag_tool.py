"""Tests for the ``agentic_rag_query`` MCP tool registration.

A real FastMCP instance is used, and tools are called through its public
``call_tool`` / ``list_tools`` API. Only the HTTP boundary to the Agentic RAG
service is faked (``httpx.MockTransport``); the registration logic, the client
and the response formatting all run for real.
"""
import json
import logging
import sys
from pathlib import Path

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agentic_rag_client  # noqa: E402
import agentic_rag_tool  # noqa: E402
from mcp_logging import logger as agentmcp_logger  # noqa: E402

BASE = "https://rag.example.test"
PSK = "admin-psk-value"
RAG_ENV_VARS = (
    "AGENTIC_RAG_BASE_URL",
    "AGENTIC_RAG_PSK",
    "AGENTIC_RAG_DEFAULT_EXTERNAL_ID",
    "AGENTIC_RAG_CACHE_FILE",
    "AGENTIC_RAG_TENANT_PREFIX",
)


class FakeRagService:
    """Scripted stand-in for the Agentic RAG service."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.query_replies = [(200, {"response": "an answer"})]
        self.provision_reply = (200, {"tenant_id": "tid-1", "api_key": "key-1"})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/admin/tenants/provision":
            status, body = self.provision_reply
            return httpx.Response(status, json=body)
        replies = self.query_replies
        status, body = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    def provisioned_ids(self) -> list[str]:
        return [
            json.loads(r.content)["external_id"]
            for r in self.requests
            if r.url.path == "/admin/tenants/provision"
        ]

    def query_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/v1/query"]


@pytest.fixture(autouse=True)
def clean_rag_env(monkeypatch):
    for name in RAG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def service(monkeypatch):
    svc = FakeRagService()
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(svc.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(agentic_rag_client.random, "uniform", lambda a, b: 0)
    return svc


@pytest.fixture
def configured(monkeypatch, tmp_path):
    """Environment for a fully configured service, with the key cache in tmp_path."""
    monkeypatch.setenv("AGENTIC_RAG_BASE_URL", BASE)
    monkeypatch.setenv("AGENTIC_RAG_PSK", PSK)
    monkeypatch.setenv("AGENTIC_RAG_CACHE_FILE", str(tmp_path / "cache.json"))


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


def _server(tenant_resolver=None) -> FastMCP:
    mcp = FastMCP("test-server")
    agentic_rag_tool.register(mcp, tenant_resolver)
    return mcp


async def _call(mcp: FastMCP, **arguments) -> str:
    content, _ = await mcp.call_tool("agentic_rag_query", arguments)
    return content[0].text


async def _parameters(mcp: FastMCP) -> set[str]:
    tools = await mcp.list_tools()
    assert [t.name for t in tools] == ["agentic_rag_query"]
    return set(tools[0].inputSchema["properties"])


# --- response formatting ---------------------------------------------------

def test_format_wraps_the_answer_text():
    text = agentic_rag_tool._format({"response": "Paris"})

    assert text == "<answer>\nParis\n</answer>"


def test_format_lists_sources_with_three_decimal_scores():
    result = {
        "response": "x",
        "sources": [{"section_path": "Guide > Setup", "score": 0.87654, "chunk_id": "c1"}],
    }

    text = agentic_rag_tool._format(result)

    assert '- score=0.877 section="Guide > Setup" chunk=c1' in text
    assert "<sources>" in text and "</sources>" in text


def test_format_shows_a_dash_for_a_source_without_a_section():
    text = agentic_rag_tool._format(
        {"response": "x", "sources": [{"score": 0.5, "chunk_id": "c1"}]}
    )

    assert 'section="-"' in text


@pytest.mark.parametrize(
    "score, expected",
    [
        pytest.param(None, "score=None", id="missing"),
        pytest.param("high", "score=high", id="text"),
        pytest.param("0.5", "score=0.500", id="numeric_string"),
    ],
)
def test_format_copes_with_unusual_scores(score, expected):
    text = agentic_rag_tool._format(
        {"response": "x", "sources": [{"score": score, "chunk_id": "c"}]}
    )

    assert expected in text


def test_format_omits_the_sources_block_when_there_are_none():
    text = agentic_rag_tool._format({"response": "x", "sources": []})

    assert "<sources>" not in text


def test_format_includes_session_and_query_ids_when_present():
    text = agentic_rag_tool._format(
        {"response": "x", "session_id": "s-1", "query_id": "q-1"}
    )

    assert "<session_id>s-1</session_id>" in text
    assert "<query_id>q-1</query_id>" in text


def test_format_leaves_out_session_and_query_ids_when_absent():
    text = agentic_rag_tool._format({"response": "x"})

    assert "session_id" not in text and "query_id" not in text


def test_format_of_a_result_with_no_response_gives_an_empty_answer():
    text = agentic_rag_tool._format({})

    assert text == "<answer>\n\n</answer>"


# --- not configured --------------------------------------------------------

async def test_unconfigured_tool_asks_for_external_id_in_the_plain_variant():
    mcp = _server()

    parameters = await _parameters(mcp)

    assert parameters == {"external_id", "query", "session_id"}


async def test_unconfigured_tool_returns_a_configuration_error():
    mcp = _server()

    result = await _call(mcp, external_id="acme", query="hello")

    assert result.startswith("error: agentic_rag not configured")


async def test_unconfigured_tool_does_not_name_the_missing_settings_to_the_client():
    mcp = _server()

    result = await _call(mcp, external_id="acme", query="hello")

    assert "AGENTIC_RAG" not in result


async def test_unconfigured_tool_with_a_default_tenant_does_not_ask_for_external_id(monkeypatch):
    monkeypatch.setenv("AGENTIC_RAG_DEFAULT_EXTERNAL_ID", "acme")
    mcp = _server()

    parameters = await _parameters(mcp)

    assert parameters == {"query", "session_id"}


async def test_unconfigured_tool_with_a_default_tenant_returns_a_configuration_error(monkeypatch):
    monkeypatch.setenv("AGENTIC_RAG_DEFAULT_EXTERNAL_ID", "acme")
    mcp = _server()

    result = await _call(mcp, query="hello")

    assert result.startswith("error: agentic_rag not configured")


async def test_unconfigured_oauth_tool_does_not_ask_for_external_id():
    mcp = _server(tenant_resolver=lambda: "acme")

    parameters = await _parameters(mcp)

    assert parameters == {"query", "session_id"}


async def test_unconfigured_oauth_tool_returns_a_configuration_error():
    mcp = _server(tenant_resolver=lambda: "acme")

    result = await _call(mcp, query="hello")

    assert result.startswith("error: agentic_rag not configured")


@pytest.mark.parametrize(
    "present",
    [
        pytest.param("AGENTIC_RAG_BASE_URL", id="only_base_url"),
        pytest.param("AGENTIC_RAG_PSK", id="only_psk"),
    ],
)
async def test_tool_with_half_the_configuration_returns_a_configuration_error(monkeypatch, present):
    monkeypatch.setenv(present, "value")
    mcp = _server()

    result = await _call(mcp, external_id="acme", query="hello")

    assert result.startswith("error: agentic_rag not configured")


# --- configured: caller supplies the tenant (variant C) --------------------

async def test_configured_plain_tool_asks_for_external_id(configured, service):
    mcp = _server()

    parameters = await _parameters(mcp)

    assert parameters == {"external_id", "query", "session_id"}


async def test_configured_plain_tool_returns_the_formatted_answer(configured, service):
    service.query_replies = [(200, {
        "response": "Paris",
        "sources": [{"section_path": "Geo", "score": 0.9, "chunk_id": "c7"}],
        "session_id": "s-1",
        "query_id": "q-1",
    })]
    mcp = _server()

    result = await _call(mcp, external_id="acme", query="Capital of France?")

    assert result == (
        "<answer>\nParis\n</answer>\n"
        "<sources>\n"
        '- score=0.900 section="Geo" chunk=c7\n'
        "</sources>\n"
        "<session_id>s-1</session_id>\n"
        "<query_id>q-1</query_id>"
    )


async def test_configured_plain_tool_queries_the_tenant_the_caller_names(configured, service):
    mcp = _server()

    await _call(mcp, external_id="globex", query="hello")

    assert service.provisioned_ids() == ["globex"]


async def test_session_id_is_passed_on_to_the_service(configured, service):
    mcp = _server()

    await _call(mcp, external_id="acme", query="hello", session_id="sess-9")

    assert json.loads(service.query_requests()[0].content)["session_id"] == "sess-9"


async def test_plain_tool_rejects_an_empty_external_id(configured, service):
    mcp = _server()

    result = await _call(mcp, external_id="", query="hello")

    assert result == "error: external_id is required"
    assert service.requests == []


async def test_empty_query_is_rejected_before_reaching_the_service(configured, service):
    mcp = _server()

    with pytest.raises(ToolError):
        await _call(mcp, external_id="acme", query="")

    assert service.requests == []


async def test_over_long_query_is_rejected_before_reaching_the_service(configured, service):
    mcp = _server()

    with pytest.raises(ToolError):
        await _call(mcp, external_id="acme", query="x" * 10001)

    assert service.requests == []


async def test_query_at_the_maximum_length_is_accepted(configured, service):
    mcp = _server()

    result = await _call(mcp, external_id="acme", query="x" * 10000)

    assert result.startswith("<answer>")


# --- configured: default tenant (variant B) --------------------------------

async def test_default_tenant_tool_does_not_ask_for_external_id(configured, monkeypatch, service):
    monkeypatch.setenv("AGENTIC_RAG_DEFAULT_EXTERNAL_ID", "default-co")
    mcp = _server()

    parameters = await _parameters(mcp)

    assert parameters == {"query", "session_id"}


async def test_default_tenant_tool_queries_the_default_tenant(configured, monkeypatch, service):
    monkeypatch.setenv("AGENTIC_RAG_DEFAULT_EXTERNAL_ID", "default-co")
    mcp = _server()

    await _call(mcp, query="hello")

    assert service.provisioned_ids() == ["default-co"]


# --- configured: tenant from the request (OAuth, variant A) ----------------

async def test_oauth_tool_does_not_ask_for_external_id(configured, service):
    mcp = _server(tenant_resolver=lambda: "from-jwt")

    parameters = await _parameters(mcp)

    assert parameters == {"query", "session_id"}


async def test_oauth_tool_queries_the_tenant_from_the_resolver(configured, service):
    mcp = _server(tenant_resolver=lambda: "from-jwt")

    await _call(mcp, query="hello")

    assert service.provisioned_ids() == ["from-jwt"]


async def test_oauth_tool_accepts_an_async_resolver(configured, service):
    async def resolver():
        return "async-tenant"

    mcp = _server(tenant_resolver=resolver)

    await _call(mcp, query="hello")

    assert service.provisioned_ids() == ["async-tenant"]


async def test_resolver_is_asked_again_on_every_call(configured, service):
    tenants = iter(["first", "second"])
    mcp = _server(tenant_resolver=lambda: next(tenants))

    await _call(mcp, query="one")
    await _call(mcp, query="two")

    assert service.provisioned_ids() == ["first", "second"]


async def test_oauth_mode_ignores_the_default_tenant_setting(configured, monkeypatch, service):
    monkeypatch.setenv("AGENTIC_RAG_DEFAULT_EXTERNAL_ID", "default-co")
    mcp = _server(tenant_resolver=lambda: "from-jwt")

    await _call(mcp, query="hello")

    assert service.provisioned_ids() == ["from-jwt"]


@pytest.mark.parametrize(
    "resolved",
    [
        pytest.param("", id="empty_string"),
        pytest.param(None, id="none"),
        pytest.param(123, id="not_a_string"),
    ],
)
async def test_oauth_tool_refuses_when_the_tenant_cannot_be_resolved(configured, service, resolved):
    mcp = _server(tenant_resolver=lambda: resolved)

    result = await _call(mcp, query="hello")

    assert result == "error: could not resolve tenant from request"
    assert service.requests == []


async def test_oauth_tool_hides_the_detail_when_the_resolver_crashes(configured, service):
    def resolver():
        raise RuntimeError("secret database password in message")

    mcp = _server(tenant_resolver=resolver)

    result = await _call(mcp, query="hello")

    assert result.startswith("error: internal error (correlation_id: ")
    assert "secret" not in result
    assert service.requests == []


# --- upstream failures -----------------------------------------------------

async def test_rate_limit_from_the_service_is_returned_as_an_error_string(configured, service):
    service.query_replies = [(429, {"detail": "slow down"})]
    mcp = _server()

    result = await _call(mcp, external_id="acme", query="hello")

    assert result.startswith("error: rate limited")


async def test_exhausted_key_pool_is_returned_as_an_error_string(configured, service):
    service.provision_reply = (409, {"detail": "full"})
    mcp = _server()

    result = await _call(mcp, external_id="acme", query="hello")

    assert result.startswith("error: the knowledge base is not available at the moment")
    assert "keys" not in result


async def test_service_outage_is_returned_as_an_error_string(configured, service):
    service.query_replies = [(503, "")]
    mcp = _server()

    result = await _call(mcp, external_id="acme", query="hello")

    assert result.startswith(
        "error: the knowledge base service failed to process the request (HTTP 503). "
        "Try again (correlation_id: "
    )


async def test_upstream_error_detail_goes_to_the_log_not_the_client(configured, service, logs):
    service.query_replies = [(403, {"detail": "tenant suspended by internal-billing"})]
    mcp = _server()

    result = await _call(mcp, external_id="acme", query="hello")

    assert "internal-billing" not in result
    correlation_id = result.rsplit(": ", 1)[1].rstrip(")")
    assert any(
        f"cid={correlation_id}" in line and "internal-billing" in line for line in logs
    )


async def test_unreadable_service_reply_is_reported_without_leaking_detail(configured, service):
    service.query_replies = [(200, "<html>not json, internal-host-name</html>")]
    mcp = _server()

    result = await _call(mcp, external_id="acme", query="hello")

    assert result.startswith("error: internal error (correlation_id: ")
    assert "internal-host-name" not in result


async def test_correlation_id_in_an_internal_error_matches_the_log(configured, service, logs):
    service.query_replies = [(200, "garbage")]
    mcp = _server()

    result = await _call(mcp, external_id="acme", query="hello")

    correlation_id = result.rsplit(": ", 1)[1].rstrip(")")
    assert any(f"cid={correlation_id}" in line for line in logs)
