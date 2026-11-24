"""Tests for the Agentic RAG HTTP client (tenant provisioning, key cache, queries).

Only the HTTP boundary is faked (an ``httpx.MockTransport`` standing in for the
Agentic RAG service). The client's own logic runs for real: slug derivation,
the in-memory and file-backed key cache, re-minting after a 401, retries and
error translation.
"""
import asyncio
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agentic_rag_client  # noqa: E402
from agentic_rag_client import (  # noqa: E402
    AgenticRagClient,
    AgenticRagError,
    KeyLimitExhausted,
    _is_key_limit_error,
    _slugify,
)

PSK = "admin-psk-value"
BASE = "https://rag.example.test"
SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,48}$")


def _reply(status=200, body=None, text=None, headers=None):
    """Describe a canned HTTP response."""
    return {"status": status, "body": body, "text": text, "headers": headers or {}}


class FakeRagService:
    """Scripted stand-in for the Agentic RAG service.

    Each route (``provision``, ``mint``, ``query``) has a queue of canned
    replies. The last reply in a queue is reused for any further calls. A reply
    may also be an exception instance, which is raised as a transport failure.
    """

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.script = {
            "provision": [_reply(200, {"tenant_id": "tid-1", "api_key": "key-1"})],
            "mint": [_reply(201, {"api_key": "minted-key"})],
            "query": [_reply(200, {"response": "an answer"})],
        }

    async def handler(self, request: httpx.Request) -> httpx.Response:
        # Yield once, as real network I/O would, so concurrent callers interleave.
        await asyncio.sleep(0)
        if request.url.path == "/admin/tenants/provision":
            route = "provision"
        elif request.url.path.endswith("/api-keys"):
            route = "mint"
        elif request.url.path == "/v1/query":
            route = "query"
        else:
            return httpx.Response(404, json={"detail": "unknown route"})
        self.requests.append(request)

        queue = self.script[route]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return httpx.Response(
            item["status"],
            json=item["body"] if item["body"] is not None else None,
            text=item["text"],
            headers=item["headers"],
        )

    def to(self, route: str) -> list[httpx.Request]:
        paths = {
            "provision": lambda r: r.url.path == "/admin/tenants/provision",
            "mint": lambda r: r.url.path.endswith("/api-keys"),
            "query": lambda r: r.url.path == "/v1/query",
        }
        return [r for r in self.requests if paths[route](r)]


@pytest.fixture
def service(monkeypatch):
    svc = FakeRagService()
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(svc.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    # Retries wait a random fraction of a second; make that instant.
    monkeypatch.setattr(agentic_rag_client.random, "uniform", lambda a, b: 0)
    return svc


@pytest.fixture
def cache_file(tmp_path):
    return tmp_path / "rag_cache.json"


@pytest.fixture
async def client(service, cache_file):
    c = AgenticRagClient(BASE, PSK, str(cache_file))
    yield c
    await c.aclose()


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content)


# --- slug derivation -------------------------------------------------------

@pytest.mark.parametrize(
    "external_id",
    [
        pytest.param("acme", id="simple"),
        pytest.param("Acme Corp!", id="mixed_case_and_punctuation"),
        pytest.param("123numeric", id="starts_with_digit"),
        pytest.param("", id="empty"),
        pytest.param("x" * 300, id="very_long"),
        pytest.param("tenant/with-odd.chars@example.com", id="email_like"),
    ],
)
def test_slug_always_matches_the_upstream_pattern(external_id):
    slug = _slugify(external_id)

    assert SLUG_PATTERN.match(slug)


def test_slug_for_a_known_id_is_stable():
    # Must stay identical to LangChainMCPChat's slug, so the value is pinned.
    slug = _slugify("acme")

    assert slug == "acme_822b33ad87"


def test_slug_lower_cases_and_replaces_unsafe_characters():
    slug = _slugify("Acme Corp!")

    assert slug == "acme_corp__2532325676"


def test_slug_for_an_id_starting_with_a_digit_gets_a_letter_prefix():
    slug = _slugify("1abc")

    assert slug.startswith("t_1abc_")


def test_slug_for_different_ids_differs_even_when_the_readable_part_matches():
    first = _slugify("Acme Corp")
    second = _slugify("acme corp")

    assert first != second


def test_slug_is_never_longer_than_49_characters():
    slug = _slugify("a" * 500)

    assert len(slug) <= 49


# --- key limit detection ---------------------------------------------------

@pytest.mark.parametrize(
    "detail, expected",
    [
        pytest.param("", False, id="empty"),
        pytest.param("Tenant already has 20 active keys", True, id="twenty_keys"),
        pytest.param("API key limit reached", True, id="key_limit"),
        pytest.param("Invalid payload", False, id="unrelated"),
        pytest.param("rate limit exceeded", False, id="limit_without_key"),
    ],
)
def test_key_limit_detection(detail, expected):
    result = _is_key_limit_error(detail)

    assert result is expected


# --- cache loading ---------------------------------------------------------

async def test_cached_credentials_from_disk_are_used_without_provisioning(service, cache_file):
    cache_file.write_text(
        json.dumps({"acme": {"tenant_id": "t-9", "api_key": "disk-key"}}),
        encoding="utf-8",
    )
    c = AgenticRagClient(BASE, PSK, str(cache_file))

    await c.query("acme", "hello", None)
    await c.aclose()

    assert service.to("provision") == []
    assert service.to("query")[0].headers["Authorization"] == "Bearer disk-key"


async def test_corrupt_cache_file_is_set_aside_and_the_client_starts_empty(service, cache_file):
    cache_file.write_text("{not valid json", encoding="utf-8")

    c = AgenticRagClient(BASE, PSK, str(cache_file))
    await c.query("acme", "hello", None)
    await c.aclose()

    set_aside = list(cache_file.parent.glob("rag_cache.json.corrupt.*"))
    assert len(set_aside) == 1
    assert set_aside[0].read_text(encoding="utf-8") == "{not valid json"
    assert len(service.to("provision")) == 1


async def test_cache_file_holding_a_json_list_is_treated_as_corrupt(service, cache_file):
    cache_file.write_text("[1, 2, 3]", encoding="utf-8")

    c = AgenticRagClient(BASE, PSK, str(cache_file))
    await c.query("acme", "hello", None)
    await c.aclose()

    assert len(list(cache_file.parent.glob("rag_cache.json.corrupt.*"))) == 1


async def test_cache_entries_missing_a_field_are_ignored(service, cache_file):
    cache_file.write_text(
        json.dumps({
            "good": {"tenant_id": "t-1", "api_key": "good-key"},
            "no_key": {"tenant_id": "t-2"},
            "not_a_dict": "oops",
        }),
        encoding="utf-8",
    )
    c = AgenticRagClient(BASE, PSK, str(cache_file))

    await c.query("good", "q", None)
    await c.query("no_key", "q", None)
    await c.aclose()

    provisioned = [_body(r)["external_id"] for r in service.to("provision")]
    assert provisioned == ["no_key"]


async def test_missing_cache_file_is_not_an_error(service, cache_file):
    assert not cache_file.exists()

    c = AgenticRagClient(BASE, PSK, str(cache_file))
    result = await c.query("acme", "hello", None)
    await c.aclose()

    assert result == {"response": "an answer"}


# --- provisioning and caching ---------------------------------------------

async def test_first_query_provisions_the_tenant_with_the_admin_psk(client, service):
    await client.query("Acme Corp", "what is x?", None)

    provision = service.to("provision")[0]
    assert provision.headers["Authorization"] == f"Bearer {PSK}"
    assert _body(provision) == {
        "external_id": "Acme Corp",
        "name": "Acme Corp",
        "slug": _slugify("Acme Corp"),
    }


async def test_query_uses_the_tenant_api_key_not_the_admin_psk(client, service):
    await client.query("acme", "what is x?", None)

    query = service.to("query")[0]
    assert query.headers["Authorization"] == "Bearer key-1"


async def test_query_body_carries_the_question_and_disables_streaming(client, service):
    await client.query("acme", "what is x?", "sess-1")

    assert _body(service.to("query")[0]) == {
        "query": "what is x?",
        "session_id": "sess-1",
        "stream": False,
    }


async def test_empty_session_id_is_sent_as_null(client, service):
    await client.query("acme", "what is x?", "")

    assert _body(service.to("query")[0])["session_id"] is None


async def test_query_returns_the_service_response(client, service):
    service.script["query"] = [_reply(200, {"response": "42", "sources": []})]

    result = await client.query("acme", "meaning?", None)

    assert result == {"response": "42", "sources": []}


async def test_api_key_is_minted_when_provisioning_does_not_return_one(client, service):
    service.script["provision"] = [_reply(200, {"tenant_id": "tid-7"})]

    await client.query("acme", "q", None)

    mint = service.to("mint")[0]
    assert mint.url.path == "/admin/tenants/tid-7/api-keys"
    assert mint.headers["Authorization"] == f"Bearer {PSK}"
    assert _body(mint) == {"label": "mcp-server"}
    assert service.to("query")[0].headers["Authorization"] == "Bearer minted-key"


async def test_credentials_are_reused_for_later_queries_of_the_same_tenant(client, service):
    await client.query("acme", "first", None)
    await client.query("acme", "second", None)

    assert len(service.to("provision")) == 1
    assert len(service.to("query")) == 2


async def test_each_tenant_is_provisioned_separately(client, service):
    await client.query("acme", "q", None)
    await client.query("globex", "q", None)

    provisioned = [_body(r)["external_id"] for r in service.to("provision")]
    assert provisioned == ["acme", "globex"]


async def test_simultaneous_first_queries_for_one_tenant_provision_it_once(client, service):
    await asyncio.gather(*(client.query("acme", f"q{i}", None) for i in range(5)))

    assert len(service.to("provision")) == 1


async def test_new_credentials_are_written_to_the_cache_file(client, cache_file):
    await client.query("acme", "q", None)

    stored = json.loads(cache_file.read_text(encoding="utf-8"))
    assert stored == {"acme": {"tenant_id": "tid-1", "api_key": "key-1"}}


async def test_writing_the_cache_leaves_no_temporary_file_behind(client, cache_file):
    await client.query("acme", "q", None)

    assert [p.name for p in cache_file.parent.iterdir()] == [cache_file.name]


async def test_cache_file_in_a_missing_directory_is_created(service, tmp_path):
    nested = tmp_path / "deep" / "er" / "cache.json"
    c = AgenticRagClient(BASE, PSK, str(nested))

    await c.query("acme", "q", None)
    await c.aclose()

    assert nested.exists()


async def test_a_new_client_reuses_credentials_saved_by_an_earlier_one(service, cache_file):
    first = AgenticRagClient(BASE, PSK, str(cache_file))
    await first.query("acme", "q", None)
    await first.aclose()

    second = AgenticRagClient(BASE, PSK, str(cache_file))
    await second.query("acme", "q", None)
    await second.aclose()

    assert len(service.to("provision")) == 1


async def test_trailing_slash_on_the_base_url_does_not_double_up(service, cache_file):
    c = AgenticRagClient(BASE + "/", PSK, str(cache_file))

    await c.query("acme", "q", None)
    await c.aclose()

    assert str(service.to("query")[0].url) == f"{BASE}/v1/query"


# --- rejected credentials (401) -------------------------------------------

async def test_rejected_api_key_triggers_a_fresh_provision_and_a_retry(client, service):
    service.script["provision"] = [
        _reply(200, {"tenant_id": "tid-1", "api_key": "stale"}),
        _reply(200, {"tenant_id": "tid-1", "api_key": "fresh"}),
    ]
    service.script["query"] = [
        _reply(401, {"detail": "bad key"}),
        _reply(200, {"response": "now works"}),
    ]

    result = await client.query("acme", "q", None)

    assert result == {"response": "now works"}
    keys_used = [r.headers["Authorization"] for r in service.to("query")]
    assert keys_used == ["Bearer stale", "Bearer fresh"]


async def test_replacement_credentials_after_a_401_are_saved_to_the_cache(client, service, cache_file):
    service.script["provision"] = [
        _reply(200, {"tenant_id": "tid-1", "api_key": "stale"}),
        _reply(200, {"tenant_id": "tid-1", "api_key": "fresh"}),
    ]
    service.script["query"] = [_reply(401), _reply(200, {"response": "ok"})]

    await client.query("acme", "q", None)

    stored = json.loads(cache_file.read_text(encoding="utf-8"))
    assert stored["acme"]["api_key"] == "fresh"


async def test_a_second_401_is_reported_as_an_authentication_failure(client, service):
    service.script["query"] = [_reply(401, {"detail": "nope"})]

    with pytest.raises(AgenticRagError, match="authentication failed"):
        await client.query("acme", "q", None)


async def test_key_limit_hit_while_re_minting_after_a_401_is_reported_as_such(client, service):
    service.script["query"] = [_reply(401)]
    service.script["provision"] = [
        _reply(200, {"tenant_id": "tid-1", "api_key": "stale"}),
        _reply(409, {"detail": "too many keys"}),
    ]

    with pytest.raises(KeyLimitExhausted):
        await client.query("acme", "q", None)


# --- query errors ----------------------------------------------------------

async def test_server_error_on_the_first_attempt_is_retried_once(client, service):
    service.script["query"] = [_reply(503), _reply(200, {"response": "recovered"})]

    result = await client.query("acme", "q", None)

    assert result == {"response": "recovered"}
    assert len(service.to("query")) == 2


async def test_persistent_server_error_is_reported_with_its_status(client, service):
    service.script["query"] = [_reply(500, text="")]

    with pytest.raises(AgenticRagError, match="HTTP 500"):
        await client.query("acme", "q", None)


async def test_persistent_server_error_is_tried_exactly_twice(client, service):
    service.script["query"] = [_reply(502, text="")]

    with pytest.raises(AgenticRagError):
        await client.query("acme", "q", None)

    assert len(service.to("query")) == 2


async def test_transport_error_on_the_first_attempt_is_retried(client, service):
    service.script["query"] = [
        httpx.ConnectError("connection refused"),
        _reply(200, {"response": "back"}),
    ]

    result = await client.query("acme", "q", None)

    assert result == {"response": "back"}


async def test_persistent_transport_error_is_reported_as_a_transport_error(client, service):
    service.script["query"] = [httpx.ConnectTimeout("timed out")]

    with pytest.raises(AgenticRagError, match="transport error"):
        await client.query("acme", "q", None)


@pytest.mark.parametrize(
    "headers, expected",
    [
        pytest.param({"Retry-After": "30"}, "retry in 30s", id="numeric"),
        pytest.param({"Retry-After": "soon"}, "retry in ?s", id="not_a_number"),
        pytest.param({}, "retry in ?s", id="header_missing"),
    ],
)
async def test_rate_limited_query_tells_the_caller_when_to_retry(client, service, headers, expected):
    service.script["query"] = [_reply(429, headers=headers)]

    with pytest.raises(AgenticRagError, match=re.escape(expected)):
        await client.query("acme", "q", None)


async def test_forbidden_query_reports_the_service_detail(client, service):
    service.script["query"] = [_reply(403, {"detail": "tenant suspended"})]

    with pytest.raises(AgenticRagError, match="tenant suspended"):
        await client.query("acme", "q", None)


async def test_forbidden_query_without_detail_says_forbidden(client, service):
    service.script["query"] = [_reply(403, text="")]

    with pytest.raises(AgenticRagError, match="forbidden"):
        await client.query("acme", "q", None)


async def test_validation_failure_hides_the_service_detail_from_the_caller(client, service):
    service.script["query"] = [_reply(422, {"detail": [{"loc": ["body", "query"], "msg": "internal-field"}]})]

    with pytest.raises(AgenticRagError) as excinfo:
        await client.query("acme", "q", None)

    assert "internal-field" not in str(excinfo.value)
    assert "validation failed" in str(excinfo.value)


async def test_other_client_error_reports_the_service_detail(client, service):
    service.script["query"] = [_reply(404, {"detail": "no such index"})]

    with pytest.raises(AgenticRagError, match="no such index"):
        await client.query("acme", "q", None)


async def test_client_error_with_a_non_json_body_reports_the_body_text(client, service):
    service.script["query"] = [_reply(400, text="plain text failure")]

    with pytest.raises(AgenticRagError, match="plain text failure"):
        await client.query("acme", "q", None)


async def test_structured_error_detail_is_reported_as_json_text(client, service):
    service.script["query"] = [_reply(400, {"detail": {"code": "bad_thing"}})]

    with pytest.raises(AgenticRagError, match="bad_thing"):
        await client.query("acme", "q", None)


async def test_client_error_with_no_usable_detail_falls_back_to_the_status(client, service):
    service.script["query"] = [_reply(418, {"unexpected": "shape"})]

    with pytest.raises(AgenticRagError, match="HTTP 418"):
        await client.query("acme", "q", None)


# --- admin (provision / mint) errors ---------------------------------------

async def test_provisioning_accepts_a_201_created(client, service):
    service.script["provision"] = [_reply(201, {"tenant_id": "tid-1", "api_key": "k"})]

    result = await client.query("acme", "q", None)

    assert result == {"response": "an answer"}


async def test_provisioning_server_error_is_retried_once(client, service):
    service.script["provision"] = [
        _reply(500),
        _reply(200, {"tenant_id": "tid-1", "api_key": "k"}),
    ]

    await client.query("acme", "q", None)

    assert len(service.to("provision")) == 2


async def test_provisioning_transport_error_is_retried_once(client, service):
    service.script["provision"] = [
        httpx.ReadError("dropped"),
        _reply(200, {"tenant_id": "tid-1", "api_key": "k"}),
    ]

    await client.query("acme", "q", None)

    assert len(service.to("provision")) == 2


async def test_persistent_provisioning_transport_error_is_reported(client, service):
    service.script["provision"] = [httpx.ConnectError("down")]

    with pytest.raises(AgenticRagError, match="transport error"):
        await client.query("acme", "q", None)


async def test_persistent_provisioning_server_error_is_reported_with_its_status(client, service):
    service.script["provision"] = [_reply(500, text="")]

    with pytest.raises(AgenticRagError, match="HTTP 500"):
        await client.query("acme", "q", None)


async def test_failed_provisioning_is_not_cached(client, service, cache_file):
    service.script["provision"] = [_reply(500, text=""), _reply(500, text=""), _reply(200, {"tenant_id": "t", "api_key": "k"})]

    with pytest.raises(AgenticRagError):
        await client.query("acme", "q", None)

    assert not cache_file.exists()


async def test_provisioning_rate_limit_tells_the_caller_when_to_retry(client, service):
    service.script["provision"] = [_reply(429, headers={"Retry-After": "12"})]

    with pytest.raises(AgenticRagError, match="retry in 12s"):
        await client.query("acme", "q", None)


async def test_provisioning_rate_limit_with_an_unreadable_retry_after_says_unknown(client, service):
    service.script["provision"] = [_reply(429, headers={"Retry-After": "tomorrow"})]

    with pytest.raises(AgenticRagError, match=re.escape("retry in ?s")):
        await client.query("acme", "q", None)


async def test_client_error_with_a_json_list_body_falls_back_to_the_status(client, service):
    service.script["query"] = [_reply(400, [1, 2, 3])]

    with pytest.raises(AgenticRagError, match="HTTP 400"):
        await client.query("acme", "q", None)


async def test_provisioning_conflict_means_the_key_pool_is_exhausted(client, service):
    service.script["provision"] = [_reply(409, {"detail": "conflict"})]

    with pytest.raises(KeyLimitExhausted, match="20 active keys"):
        await client.query("acme", "q", None)


async def test_bad_request_naming_the_key_limit_means_the_key_pool_is_exhausted(client, service):
    service.script["provision"] = [_reply(400, {"detail": "tenant already has 20 active keys"})]

    with pytest.raises(KeyLimitExhausted):
        await client.query("acme", "q", None)


async def test_key_limit_detail_on_other_client_errors_is_also_recognised(client, service):
    service.script["provision"] = [_reply(403, {"detail": "API key limit reached"})]

    with pytest.raises(KeyLimitExhausted):
        await client.query("acme", "q", None)


async def test_unrelated_bad_request_while_provisioning_reports_the_detail(client, service):
    service.script["provision"] = [_reply(400, {"detail": "name too long"})]

    with pytest.raises(AgenticRagError, match="name too long") as excinfo:
        await client.query("acme", "q", None)

    assert not isinstance(excinfo.value, KeyLimitExhausted)


async def test_provisioning_validation_failure_hides_the_service_detail(client, service):
    service.script["provision"] = [_reply(422, {"detail": "secret-internal-detail"})]

    with pytest.raises(AgenticRagError) as excinfo:
        await client.query("acme", "q", None)

    assert "secret-internal-detail" not in str(excinfo.value)
    assert "admin request validation failed" in str(excinfo.value)


async def test_minting_failure_is_reported_and_nothing_is_cached(client, service, cache_file):
    service.script["provision"] = [_reply(200, {"tenant_id": "tid-1"})]
    service.script["mint"] = [_reply(409, {"detail": "full"})]

    with pytest.raises(KeyLimitExhausted):
        await client.query("acme", "q", None)

    assert not cache_file.exists()


def test_key_limit_exhaustion_is_a_kind_of_rag_error():
    assert issubclass(KeyLimitExhausted, AgenticRagError)
