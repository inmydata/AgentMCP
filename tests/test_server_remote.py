"""
Tests for server_remote.py: the remote MCP server (OAuth and legacy modes).

server_remote builds its app when it is imported and reads its settings from
the environment, so each test loads a fresh copy through the ``load_server``
fixture. That fixture:

  * stops ``.env`` being read (it would switch OAuth on and leak into the
    process environment),
  * stops the module changing the shared ``agentmcp`` logger,
  * sets every INMYDATA_* / AGENTIC_RAG_* variable explicitly, and
  * removes the module from ``sys.modules`` afterwards.

Nothing binds a port. The MCP app is driven in-process through
``httpx.ASGITransport`` using real JSON-RPC over the streamable HTTP
transport, so the real authentication layer, ``utils()`` and the tool
handlers all run. Only things outside the process are replaced:

  * the token verifier's ``verify_token`` (it would fetch a JWKS and call
    the identity provider), and
  * ``mcp_utils`` (it would call the inmydata platform). The fake records
    what it was built with and echoes the arguments it receives, so tests can
    see which credentials, tenant and arguments reached the platform layer.

The ``if __name__ == "__main__"`` block starts a real server and is not tested.
"""
from __future__ import annotations

import importlib
import json
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import dotenv
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agentic_rag_client  # noqa: E402
import mcp_logging  # noqa: E402
from errors import UserFacingError  # noqa: E402
from fastmcp.server.auth.providers.jwt import AccessToken  # noqa: E402

MCP_HOST = "mcp.example.test"
SERVER = "platform.example.test"
AUTH_SERVER = "auth.platform.example.test"
ISSUER = f"https://{AUTH_SERVER}"

_MANAGED_ENV = (
    "_API_KEY",
    "ACME_API_KEY",
    "RIVAL_API_KEY",
    "TENANT-A_API_KEY",
    "TENANT-B_API_KEY",
    "INMYDATA_MCP_HOST",
    "INMYDATA_SERVER",
    "INMYDATA_AUTH_SERVER",
    "INMYDATA_USE_OAUTH",
    "INMYDATA_INTROSPECTION_CLIENT_ID",
    "INMYDATA_INTROSPECTION_CLIENT_SECRET",
    "INMYDATA_PAT_CACHE_MAX_TTL",
    "INMYDATA_PAT_CACHE_NEGATIVE_TTL",
    "INMYDATA_PAT_CACHE_MAX_ENTRIES",
    "INMYDATA_TOKEN_CACHE_TTL",
    "AGENTIC_RAG_BASE_URL",
    "AGENTIC_RAG_PSK",
    "AGENTIC_RAG_DEFAULT_EXTERNAL_ID",
    "AGENTIC_RAG_TENANT_PREFIX",
    "AGENTIC_RAG_CACHE_FILE",
)


# ---------------------------------------------------------------------------
# Fake platform layer
# ---------------------------------------------------------------------------


def _make_fake_utils():
    """Build a stand-in for mcp_utils that echoes what it was given."""

    class FakeUtils:
        failure: BaseException | None = None

        def __init__(self, api_key, tenant, calendar, user, session_id, server):
            self.built_with = {
                "api_key": api_key,
                "tenant": tenant,
                "calendar": calendar,
                "user": user,
                "session_id": session_id,
                "server": server,
            }

        def _echo(self, method, **args):
            if type(self).failure is not None:
                raise type(self).failure
            return json.dumps({"method": method, "built_with": self.built_with, "args": args})

        async def get_rows(self, subject, select, where):
            return self._echo("get_rows", subject=subject, select=select, where=where)

        async def get_top_n(self, subject, group_by, order_by, n, where):
            return self._echo(
                "get_top_n", subject=subject, group_by=group_by, order_by=order_by, n=n, where=where
            )

        async def query_results(self, instance_id, sql):
            return self._echo("query_results", instance_id=instance_id, sql=sql)

        async def get_answer(self, question, ctx):
            return self._echo("get_answer", question=question)

        def get_schema(self):
            return self._echo("get_schema")

        async def get_financial_periods(self, target_date):
            if type(self).failure is not None:
                raise type(self).failure
            return self._echo("get_financial_periods", target_date=target_date)

        async def get_calendar_period_date_range(self, financial_year, period_number, period_type):
            return self._echo(
                "get_calendar_period_date_range",
                financial_year=financial_year,
                period_number=period_number,
                period_type=period_type,
            )

    return FakeUtils


# ---------------------------------------------------------------------------
# Talking to the app over JSON-RPC
# ---------------------------------------------------------------------------


@dataclass
class Reply:
    """The outcome of one tool call: the HTTP status and the tool's text, if any."""

    status: int
    text: str | None = None
    client_log: tuple = ()  # messages the server logged to the client during the call

    @property
    def json(self):
        assert self.text is not None, f"no tool output (HTTP {self.status})"
        return json.loads(self.text)


def _sse_messages(body: str) -> list[dict]:
    return [
        json.loads(line[len("data:"):].strip())
        for line in body.splitlines()
        if line.startswith("data:")
    ]


class Caller:
    """Calls MCP tools over HTTP. Each call performs its own MCP handshake."""

    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def __call__(
        self,
        tool: str,
        arguments: dict | None = None,
        *,
        token: str | None = None,
        headers: dict | None = None,
        query: str = "",
    ) -> Reply:
        request_headers = {"Accept": "application/json, text/event-stream"}
        if token is not None:
            request_headers["Authorization"] = f"Bearer {token}"
        request_headers.update(headers or {})
        url = "/mcp" + (f"?{query}" if query else "")

        init = await self._client.post(
            url,
            headers=request_headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "tests", "version": "1"},
                },
            },
        )
        if init.status_code != 200:
            return Reply(init.status_code)
        request_headers["mcp-session-id"] = init.headers["mcp-session-id"]
        await self._client.post(
            url,
            headers=request_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        response = await self._client.post(
            url,
            headers=request_headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments or {}},
            },
        )
        if response.status_code != 200:
            return Reply(response.status_code)
        messages = _sse_messages(response.text)
        result = next(m["result"] for m in messages if m.get("id") == 2)
        client_log = tuple(
            m["params"]["data"]["msg"]
            for m in messages
            if m.get("method") == "notifications/message"
        )
        return Reply(200, result["content"][0]["text"], client_log)


class LoadedServer:
    """A freshly imported server_remote plus the fakes wired into it."""

    def __init__(self, module, app, fake_utils, tokens: dict):
        self.module = module
        self.app = app
        self.utils = fake_utils
        self.tokens = tokens

    def accept(self, token: str, **claims) -> None:
        """Make the (faked) token verifier accept ``token`` with these claims."""
        self.tokens[token] = AccessToken(
            token=token, client_id=claims.get("client_id", "test-client"), scopes=[], claims=claims
        )

    @asynccontextmanager
    async def running(self):
        """Run the app's lifespan and yield a Caller for MCP tool calls."""
        async with self.app.router.lifespan_context(self.app):
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                yield Caller(client)

    @asynccontextmanager
    async def plain_client(self, raise_app_exceptions: bool = True):
        """A bare HTTP client for the ordinary (non-MCP) routes."""
        transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=raise_app_exceptions)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
def load_server(monkeypatch, tmp_path):
    """Return a function that imports a fresh server_remote with a controlled environment."""
    # Import-time side effects: do not read .env, do not reconfigure logging.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(mcp_logging, "configure_logging", lambda *args, **kwargs: None)

    def _load(*, oauth: bool, env: dict | None = None) -> LoadedServer:
        for name in _MANAGED_ENV:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("INMYDATA_MCP_HOST", MCP_HOST)
        monkeypatch.setenv("INMYDATA_SERVER", SERVER)
        monkeypatch.setenv("INMYDATA_AUTH_SERVER", AUTH_SERVER)
        monkeypatch.setenv("INMYDATA_USE_OAUTH", "true" if oauth else "false")
        monkeypatch.setenv("AGENTIC_RAG_CACHE_FILE", str(tmp_path / "rag_cache.json"))
        for name, value in (env or {}).items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)

        sys.modules.pop("server_remote", None)
        module = importlib.import_module("server_remote")

        fake_utils = _make_fake_utils()
        monkeypatch.setattr(module, "mcp_utils", fake_utils)

        tokens: dict = {}

        async def _verify(token):
            return tokens.get(token)

        monkeypatch.setattr(module.token_verifier, "verify_token", _verify)

        app = module.app if oauth else module.mcp.http_app()
        return LoadedServer(module, app, fake_utils, tokens)

    yield _load
    sys.modules.pop("server_remote", None)


@pytest.fixture
def oauth_server(load_server) -> LoadedServer:
    return load_server(oauth=True)


@pytest.fixture
def legacy_server(load_server) -> LoadedServer:
    return load_server(oauth=False)


# ---------------------------------------------------------------------------
# MCPPathRewriteMiddleware
# ---------------------------------------------------------------------------


class TestMCPPathRewriteMiddleware:
    @staticmethod
    async def _run(module, scope):
        seen = {}

        async def inner(scope, receive, send):
            seen["scope"] = scope

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            pass

        await module.MCPPathRewriteMiddleware(inner)(scope, receive, send)
        return seen["scope"]

    async def test_bare_mcp_path_is_rewritten_with_a_trailing_slash(self, oauth_server):
        # Arrange
        scope = {"type": "http", "path": "/mcp", "raw_path": b"/mcp"}

        # Act
        seen = await self._run(oauth_server.module, scope)

        # Assert
        assert seen["path"] == "/mcp/"
        assert seen["raw_path"] == b"/mcp/"

    async def test_original_scope_is_left_untouched(self, oauth_server):
        # Arrange
        scope = {"type": "http", "path": "/mcp", "raw_path": b"/mcp"}

        # Act
        await self._run(oauth_server.module, scope)

        # Assert
        assert scope["path"] == "/mcp"

    @pytest.mark.parametrize(
        "path",
        [
            pytest.param("/mcp/", id="already-slashed"),
            pytest.param("/", id="root"),
            pytest.param("/mcp/tools", id="sub-path"),
            pytest.param("/mcpx", id="similar-prefix"),
        ],
    )
    async def test_other_paths_are_passed_through_unchanged(self, oauth_server, path):
        # Arrange
        scope = {"type": "http", "path": path}

        # Act
        seen = await self._run(oauth_server.module, scope)

        # Assert
        assert seen["path"] == path

    async def test_non_http_scopes_are_passed_through_unchanged(self, oauth_server):
        # Arrange
        scope = {"type": "lifespan", "path": "/mcp"}

        # Act
        seen = await self._run(oauth_server.module, scope)

        # Assert
        assert seen["path"] == "/mcp"


# ---------------------------------------------------------------------------
# OAuth mode: plain routes
# ---------------------------------------------------------------------------


class TestOAuthDiscoveryRoutes:
    async def test_root_reports_ok(self, oauth_server):
        # Arrange / Act
        async with oauth_server.plain_client() as client:
            response = await client.get("/")

        # Assert
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_well_known_mcp_document_is_served(self, oauth_server):
        # Arrange / Act
        async with oauth_server.plain_client() as client:
            response = await client.get("/.well-known/mcp.json")

        # Assert
        assert response.status_code == 200
        assert "url" in response.json()["mcp_server"]

    @pytest.mark.parametrize(
        "path",
        [
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
            "/mcp/.well-known/oauth-protected-resource",
        ],
        ids=["root", "root-mcp", "under-mcp"],
    )
    async def test_protected_resource_metadata_names_this_server_and_the_auth_server(
        self, oauth_server, path
    ):
        # Arrange / Act
        async with oauth_server.plain_client() as client:
            response = await client.get(path)

        # Assert
        body = response.json()
        assert body["resource"] == f"https://{MCP_HOST}/mcp"
        assert body["authorization_servers"] == [ISSUER]
        assert body["bearer_methods_supported"] == ["header"]

    @pytest.mark.parametrize(
        "path",
        [
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-authorization-server/mcp",
            "/.well-known/openid-configuration",
            "/.well-known/openid-configuration/mcp",
        ],
        ids=["oauth-as", "oauth-as-mcp", "openid", "openid-mcp"],
    )
    async def test_authorisation_server_metadata_is_served_on_every_discovery_path(
        self, oauth_server, path
    ):
        # Arrange / Act
        async with oauth_server.plain_client() as client:
            response = await client.get(path)

        # Assert
        body = response.json()
        assert response.status_code == 200
        assert body["authorization_endpoint"] == f"{ISSUER}/connect/authorize"
        assert body["token_endpoint"] == f"https://{MCP_HOST}/connect/token"
        assert body["code_challenge_methods_supported"] == ["S256"]
        assert body["response_types_supported"] == ["code"]

    async def test_advertised_issuer_has_no_trailing_slash(self, oauth_server):
        # Arrange / Act
        async with oauth_server.plain_client() as client:
            body = (await client.get("/.well-known/oauth-authorization-server")).json()

        # Assert: clients compare the issuer as an exact string
        assert body["issuer"] == ISSUER
        assert not body["issuer"].endswith("/")

    async def test_issuer_defaults_to_the_auth_subdomain_of_the_platform_server(
        self, load_server
    ):
        # Arrange / Act
        server = load_server(
            oauth=True, env={"INMYDATA_AUTH_SERVER": None, "INMYDATA_SERVER": "example.org"}
        )
        async with server.plain_client() as client:
            body = (await client.get("/.well-known/oauth-authorization-server")).json()

        # Assert
        assert body["issuer"] == "https://auth.example.org"


# ---------------------------------------------------------------------------
# OAuth mode: the /connect/token proxy
# ---------------------------------------------------------------------------


@pytest.fixture
def upstream_token_endpoint(monkeypatch):
    """Replace the auth server's token endpoint with an in-process fake."""

    class Upstream:
        def __init__(self):
            self.requests: list[httpx.Request] = []
            self.reply = lambda request: httpx.Response(200, json={"access_token": "issued"})

    upstream = Upstream()
    real_client = httpx.AsyncClient

    def handle(request):
        upstream.requests.append(request)
        return upstream.reply(request)

    def factory(*args, **kwargs):  # noqa: ARG001
        return real_client(transport=httpx.MockTransport(handle))

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    # The caller's own client is built before this patch is used, see _post_token.
    upstream.real_client = real_client
    return upstream


async def _post_token(server: LoadedServer, upstream, data: dict):
    transport = httpx.ASGITransport(app=server.app, raise_app_exceptions=False)
    async with upstream.real_client(transport=transport, base_url="http://test") as client:
        return await client.post("/connect/token", data=data)


class TestTokenEndpointProxy:
    async def test_form_fields_are_forwarded_to_the_auth_servers_token_endpoint(
        self, oauth_server, upstream_token_endpoint
    ):
        # Arrange
        form = {"grant_type": "authorization_code", "code": "abc", "client_id": "claude"}

        # Act
        await _post_token(oauth_server, upstream_token_endpoint, form)

        # Assert
        request, = upstream_token_endpoint.requests
        assert str(request.url) == f"https://{AUTH_SERVER}/connect/token"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        sent = dict(pair.split("=", 1) for pair in request.content.decode().split("&"))
        assert sent == form

    async def test_successful_upstream_reply_is_relayed(
        self, oauth_server, upstream_token_endpoint
    ):
        # Arrange
        upstream_token_endpoint.reply = lambda request: httpx.Response(  # noqa: ARG005
            200, json={"access_token": "issued", "token_type": "Bearer"}
        )

        # Act
        response = await _post_token(oauth_server, upstream_token_endpoint, {"grant_type": "x"})

        # Assert
        assert response.status_code == 200
        assert response.json() == {"access_token": "issued", "token_type": "Bearer"}

    async def test_upstream_rejection_keeps_its_status_and_error_body(
        self, oauth_server, upstream_token_endpoint
    ):
        # Arrange
        upstream_token_endpoint.reply = lambda request: httpx.Response(  # noqa: ARG005
            400, json={"error": "invalid_grant"}
        )

        # Act
        response = await _post_token(oauth_server, upstream_token_endpoint, {"grant_type": "x"})

        # Assert
        assert response.status_code == 400
        assert response.json() == {"error": "invalid_grant"}

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "The proxy copies the upstream headers verbatim, including Content-Length, "
            "but re-serialises the body. When the upstream JSON is formatted differently "
            "the length no longer matches the body, which breaks real HTTP clients."
        ),
    )
    async def test_relayed_content_length_matches_the_relayed_body(
        self, oauth_server, upstream_token_endpoint
    ):
        # Arrange: upstream sends pretty-printed JSON, so its length differs from compact JSON
        pretty = json.dumps({"access_token": "issued"}, indent=4)
        upstream_token_endpoint.reply = lambda request: httpx.Response(  # noqa: ARG005
            200, content=pretty.encode(), headers={"content-type": "application/json"}
        )

        # Act
        response = await _post_token(oauth_server, upstream_token_endpoint, {"grant_type": "x"})

        # Assert
        assert int(response.headers["content-length"]) == len(response.content)

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "An upstream error that is not JSON (for example an HTML 502 from a gateway) "
            "makes response.json() raise, so the proxy answers 500 instead of a controlled "
            "gateway error."
        ),
    )
    async def test_non_json_upstream_failure_is_reported_as_a_gateway_error(
        self, oauth_server, upstream_token_endpoint
    ):
        # Arrange
        upstream_token_endpoint.reply = lambda request: httpx.Response(  # noqa: ARG005
            502, text="<html>Bad gateway</html>"
        )

        # Act
        response = await _post_token(oauth_server, upstream_token_endpoint, {"grant_type": "x"})

        # Assert
        assert response.status_code in (502, 503, 504)


# ---------------------------------------------------------------------------
# OAuth mode: authentication and tenant isolation on /mcp
# ---------------------------------------------------------------------------


class TestOAuthAuthentication:
    async def test_request_without_a_token_is_rejected(self, oauth_server):
        # Arrange / Act
        async with oauth_server.running() as call:
            reply = await call("get_schema")

        # Assert
        assert reply.status == 401

    async def test_unknown_token_is_rejected(self, oauth_server):
        # Arrange / Act
        async with oauth_server.running() as call:
            reply = await call("get_schema", token="not-a-known-token")

        # Assert
        assert reply.status == 401

    async def test_authorisation_header_that_is_not_a_bearer_token_is_rejected(
        self, oauth_server
    ):
        # Arrange
        oauth_server.accept("valid", imd_tenant="acme", sub="u1")

        # Act
        async with oauth_server.running() as call:
            reply = await call("get_schema", headers={"Authorization": "Basic dmFsaWQ6eA=="})

        # Assert
        assert reply.status == 401

    async def test_rejected_request_does_not_reach_the_platform_layer(self, oauth_server):
        # Arrange: if a tool ran, this would make the fake raise and change the reply
        oauth_server.utils.failure = AssertionError("platform layer must not be reached")

        # Act
        async with oauth_server.running() as call:
            reply = await call("get_schema", token="not-a-known-token")

        # Assert
        assert reply.status == 401
        assert reply.text is None

    async def test_valid_token_can_call_a_tool_on_the_path_without_a_trailing_slash(
        self, oauth_server
    ):
        # Arrange
        oauth_server.accept("valid", imd_tenant="acme", sub="u1")

        # Act
        async with oauth_server.running() as call:
            reply = await call("get_schema", token="valid")

        # Assert
        assert reply.status == 200
        assert reply.json["method"] == "get_schema"


class TestOAuthTenantAndIdentity:
    async def test_tenant_user_and_credential_come_from_the_verified_token(self, oauth_server):
        # Arrange
        oauth_server.accept("tok-acme", imd_tenant="acme", sub="user-1")

        # Act
        async with oauth_server.running() as call:
            reply = await call("get_schema", token="tok-acme")

        # Assert
        built = reply.json["built_with"]
        assert built["tenant"] == "acme"
        assert built["user"] == "user-1"
        assert built["api_key"] == "tok-acme"

    async def test_defaults_are_used_when_no_optional_headers_are_sent(self, oauth_server):
        # Arrange
        oauth_server.accept("tok", imd_tenant="acme", sub="u1")

        # Act
        async with oauth_server.running() as call:
            reply = await call("get_schema", token="tok")

        # Assert
        built = reply.json["built_with"]
        assert built["calendar"] == "Default"
        assert built["session_id"] == "mcp-session"
        assert built["server"] == SERVER

    async def test_optional_headers_set_the_server_calendar_and_session(self, oauth_server):
        # Arrange
        oauth_server.accept("tok", imd_tenant="acme", sub="u1")
        headers = {
            "x-inmydata-server": "other.example.test",
            "x-inmydata-calendar": "Fiscal",
            "x-inmydata-session-id": "session-42",
        }

        # Act
        async with oauth_server.running() as call:
            reply = await call("get_schema", token="tok", headers=headers)

        # Assert
        built = reply.json["built_with"]
        assert built["server"] == "other.example.test"
        assert built["calendar"] == "Fiscal"
        assert built["session_id"] == "session-42"

    async def test_client_tenant_claim_is_preferred_over_the_user_tenant_claim(
        self, oauth_server
    ):
        # Arrange
        oauth_server.accept(
            "tok", client_imd_tenant="client-tenant", imd_tenant="user-tenant", sub="u1"
        )

        # Act
        async with oauth_server.running() as call:
            reply = await call("get_schema", token="tok")

        # Assert
        assert reply.json["built_with"]["tenant"] == "client-tenant"

    async def test_two_tokens_each_get_their_own_tenant(self, oauth_server):
        # Arrange
        oauth_server.accept("tok-a", imd_tenant="tenant-a", sub="user-a")
        oauth_server.accept("tok-b", imd_tenant="tenant-b", sub="user-b")

        # Act
        async with oauth_server.running() as call:
            reply_a = await call("get_schema", token="tok-a")
            reply_b = await call("get_schema", token="tok-b")
            reply_a_again = await call("get_schema", token="tok-a")

        # Assert
        assert reply_a.json["built_with"]["tenant"] == "tenant-a"
        assert reply_b.json["built_with"]["tenant"] == "tenant-b"
        assert reply_b.json["built_with"]["user"] == "user-b"
        assert reply_a_again.json["built_with"]["tenant"] == "tenant-a"

    async def test_tenant_header_cannot_override_the_tenant_in_the_token(self, oauth_server):
        # Arrange
        oauth_server.accept("tok-a", imd_tenant="tenant-a", sub="u1")

        # Act
        async with oauth_server.running() as call:
            reply = await call(
                "get_schema", token="tok-a", headers={"x-inmydata-tenant": "tenant-b"}
            )

        # Assert
        assert reply.json["built_with"]["tenant"] == "tenant-a"

    async def test_tenant_query_parameter_cannot_override_the_tenant_in_the_token(
        self, oauth_server
    ):
        # Arrange
        oauth_server.accept("tok-a", imd_tenant="tenant-a", sub="u1")

        # Act
        async with oauth_server.running() as call:
            reply = await call("get_schema", token="tok-a", query="tenant=tenant-b")

        # Assert
        assert reply.json["built_with"]["tenant"] == "tenant-a"

    async def test_user_header_cannot_override_the_user_in_the_token(self, oauth_server):
        # Arrange
        oauth_server.accept("tok", imd_tenant="acme", sub="real-user")

        # Act
        async with oauth_server.running() as call:
            reply = await call("get_schema", token="tok", headers={"x-inmydata-user": "someone-else"})

        # Assert
        assert reply.json["built_with"]["user"] == "real-user"

    async def test_environment_api_key_is_not_used_in_oauth_mode(
        self, oauth_server, monkeypatch
    ):
        # Arrange
        monkeypatch.setenv("ACME_API_KEY", "env-key-for-acme")
        oauth_server.accept("tok", imd_tenant="acme", sub="u1")

        # Act
        async with oauth_server.running() as call:
            reply = await call("get_schema", token="tok")

        # Assert
        assert reply.json["built_with"]["api_key"] == "tok"

    @pytest.mark.parametrize(
        "claims",
        [
            pytest.param({"sub": "u1"}, id="no-tenant-claim"),
            pytest.param({"sub": "u1", "imd_tenant": ""}, id="empty-tenant-claim"),
            pytest.param({"imd_tenant": "acme"}, id="no-subject-claim"),
            pytest.param({"imd_tenant": "acme", "sub": ""}, id="empty-subject-claim"),
        ],
    )
    async def test_token_missing_tenant_or_subject_gets_a_sanitised_error(
        self, oauth_server, claims
    ):
        # Arrange
        oauth_server.accept("tok", **claims)

        # Act
        async with oauth_server.running() as call:
            reply = await call("get_schema", token="tok")

        # Assert
        body = reply.json
        assert body["error"] == "internal error"
        assert body["correlation_id"]
        assert "method" not in body

    async def test_missing_claim_detail_is_not_leaked_to_the_caller(self, oauth_server):
        # Arrange
        oauth_server.accept("tok", sub="u1")

        # Act
        async with oauth_server.running() as call:
            reply = await call("get_schema", token="tok")

        # Assert
        assert "tenant" not in reply.text.lower()


# ---------------------------------------------------------------------------
# get_user and get_tenant
# ---------------------------------------------------------------------------


class TestGetUserAndGetTenant:
    async def test_user_is_the_subject_claim(self, oauth_server):
        # Arrange
        oauth_server.accept("tok", sub="user-9", imd_tenant="acme")

        # Act
        user = await oauth_server.module.get_user("tok")

        # Assert
        assert user == "user-9"

    async def test_unknown_token_has_no_user(self, oauth_server):
        # Arrange / Act / Assert
        with pytest.raises(RuntimeError, match="Invalid token"):
            await oauth_server.module.get_user("unknown")

    async def test_token_without_subject_has_no_user(self, oauth_server):
        # Arrange
        oauth_server.accept("tok", imd_tenant="acme")

        # Act / Assert
        with pytest.raises(RuntimeError, match="No user information"):
            await oauth_server.module.get_user("tok")

    async def test_tenant_prefers_the_client_tenant_claim(self, oauth_server):
        # Arrange
        oauth_server.accept("tok", client_imd_tenant="client-tenant", imd_tenant="user-tenant")

        # Act
        tenant = await oauth_server.module.get_tenant("tok")

        # Assert
        assert tenant == "client-tenant"

    async def test_tenant_falls_back_to_the_user_tenant_claim(self, oauth_server):
        # Arrange
        oauth_server.accept("tok", imd_tenant="user-tenant")

        # Act
        tenant = await oauth_server.module.get_tenant("tok")

        # Assert
        assert tenant == "user-tenant"

    async def test_unknown_token_has_no_tenant(self, oauth_server):
        # Arrange / Act / Assert
        with pytest.raises(RuntimeError, match="Invalid token"):
            await oauth_server.module.get_tenant("unknown")

    async def test_token_without_tenant_is_refused(self, oauth_server):
        # Arrange
        oauth_server.accept("tok", sub="u1")

        # Act / Assert
        with pytest.raises(RuntimeError, match="No tenant information"):
            await oauth_server.module.get_tenant("tok")

    async def test_empty_token_is_refused(self, oauth_server):
        # Arrange / Act / Assert
        with pytest.raises(RuntimeError, match="Invalid token"):
            await oauth_server.module.get_tenant("")


# ---------------------------------------------------------------------------
# Legacy mode: API key and tenant from headers, query string or environment
# ---------------------------------------------------------------------------


class TestLegacyCredentials:
    async def test_defaults_are_used_when_no_headers_are_sent(self, legacy_server):
        # Arrange / Act
        async with legacy_server.running() as call:
            reply = await call("get_schema")

        # Assert
        assert reply.json["built_with"] == {
            "api_key": "",
            "tenant": "",
            "calendar": "Default",
            "user": "mcp-agent",
            "session_id": "mcp-session",
            "server": SERVER,
        }

    async def test_requests_are_accepted_without_a_bearer_token(self, legacy_server):
        # Arrange / Act
        async with legacy_server.running() as call:
            reply = await call("get_schema", headers={"x-inmydata-tenant": "acme"})

        # Assert
        assert reply.status == 200

    async def test_tenant_and_api_key_come_from_the_headers(self, legacy_server):
        # Arrange
        headers = {"x-inmydata-tenant": "acme"}

        # Act
        async with legacy_server.running() as call:
            reply = await call("get_schema", token="header-key", headers=headers)

        # Assert
        built = reply.json["built_with"]
        assert built["tenant"] == "acme"
        assert built["api_key"] == "header-key"

    async def test_tenant_query_parameter_beats_the_tenant_header(self, legacy_server):
        # Arrange
        headers = {"x-inmydata-tenant": "header-tenant"}

        # Act
        async with legacy_server.running() as call:
            reply = await call("get_schema", headers=headers, query="tenant=query-tenant")

        # Assert
        assert reply.json["built_with"]["tenant"] == "query-tenant"

    async def test_environment_key_for_the_tenant_beats_the_header_key(
        self, legacy_server, monkeypatch
    ):
        # Arrange
        monkeypatch.setenv("ACME_API_KEY", "env-key")

        # Act
        async with legacy_server.running() as call:
            reply = await call(
                "get_schema", token="header-key", headers={"x-inmydata-tenant": "acme"}
            )

        # Assert
        assert reply.json["built_with"]["api_key"] == "env-key"

    async def test_environment_key_is_not_used_for_a_different_tenant(
        self, legacy_server, monkeypatch
    ):
        # Arrange
        monkeypatch.setenv("ACME_API_KEY", "acme-env-key")

        # Act
        async with legacy_server.running() as call:
            reply = await call(
                "get_schema", token="rival-header-key", headers={"x-inmydata-tenant": "rival"}
            )

        # Assert
        built = reply.json["built_with"]
        assert built["tenant"] == "rival"
        assert built["api_key"] == "rival-header-key"

    async def test_optional_headers_set_server_calendar_user_and_session(self, legacy_server):
        # Arrange
        headers = {
            "x-inmydata-tenant": "acme",
            "x-inmydata-server": "other.example.test",
            "x-inmydata-calendar": "Fiscal",
            "x-inmydata-user": "alice",
            "x-inmydata-session-id": "session-7",
        }

        # Act
        async with legacy_server.running() as call:
            reply = await call("get_schema", token="k", headers=headers)

        # Assert
        built = reply.json["built_with"]
        assert built["server"] == "other.example.test"
        assert built["calendar"] == "Fiscal"
        assert built["user"] == "alice"
        assert built["session_id"] == "session-7"

    async def test_server_default_follows_the_environment_at_call_time(
        self, legacy_server, monkeypatch
    ):
        # Arrange
        monkeypatch.setenv("INMYDATA_SERVER", "changed.example.test")

        # Act
        async with legacy_server.running() as call:
            reply = await call("get_schema")

        # Assert
        assert reply.json["built_with"]["server"] == "changed.example.test"

    async def test_credentials_fall_back_to_the_defaults_outside_an_http_request(
        self, legacy_server, monkeypatch
    ):
        # Arrange: calling utils() directly, as a non-HTTP transport would, has no request
        monkeypatch.setenv("INMYDATA_SERVER", SERVER)

        # Act
        platform = await legacy_server.module.utils()

        # Assert
        assert platform.built_with == {
            "api_key": "",
            "tenant": "",
            "calendar": "Default",
            "user": "mcp-agent",
            "session_id": "mcp-session",
            "server": SERVER,
        }

    async def test_two_tenants_in_a_row_do_not_share_credentials(self, legacy_server):
        # Arrange / Act
        async with legacy_server.running() as call:
            first = await call(
                "get_schema", token="key-a", headers={"x-inmydata-tenant": "tenant-a"}
            )
            second = await call(
                "get_schema", token="key-b", headers={"x-inmydata-tenant": "tenant-b"}
            )

        # Assert
        assert (first.json["built_with"]["tenant"], first.json["built_with"]["api_key"]) == (
            "tenant-a",
            "key-a",
        )
        assert (second.json["built_with"]["tenant"], second.json["built_with"]["api_key"]) == (
            "tenant-b",
            "key-b",
        )


# ---------------------------------------------------------------------------
# Tool argument checks (no platform call is made)
# ---------------------------------------------------------------------------


class TestToolArgumentChecks:
    @pytest.mark.parametrize(
        ("tool", "arguments", "expected_error"),
        [
            pytest.param("get_rows_fast", {}, "subject parameter is required", id="rows-no-subject"),
            pytest.param(
                "get_rows_fast", {"subject": "Sales"}, "select parameter is required (list of field names)", id="rows-no-select"
            ),
            pytest.param(
                "get_top_n_fast", {}, "subject parameter is required", id="top-n-no-subject"
            ),
            pytest.param(
                "get_top_n_fast",
                {"subject": "Sales"},
                "group_by parameter is required",
                id="top-n-no-group-by",
            ),
            pytest.param(
                "get_top_n_fast",
                {"subject": "Sales", "group_by": "Region"},
                "order_by parameter is required",
                id="top-n-no-order-by",
            ),
            pytest.param(
                "query_results_fast", {}, "instance_id parameter is required", id="query-no-instance"
            ),
            pytest.param(
                "query_results_fast",
                {"instance_id": "abc"},
                "sql parameter is required",
                id="query-no-sql",
            ),
            pytest.param(
                "get_answer_slow", {}, "question parameter is required", id="answer-no-question"
            ),
        ],
    )
    async def test_missing_required_argument_gets_a_clear_error(
        self, legacy_server, tool, arguments, expected_error
    ):
        # Arrange: any platform call would show up as a different reply
        legacy_server.utils.failure = AssertionError("platform layer must not be reached")

        # Act
        async with legacy_server.running() as call:
            reply = await call(tool, arguments, headers={"x-inmydata-tenant": "acme"})

        # Assert
        assert reply.json == {"error": expected_error}

    async def test_get_answer_without_an_mcp_context_asks_for_one(self, legacy_server):
        # Arrange
        tool = legacy_server.module.get_answer_slow

        # Act
        reply = await tool.fn(question="Why did sales fall?", ctx=None)

        # Assert
        assert json.loads(reply) == {"error": "context parameter is required"}


# ---------------------------------------------------------------------------
# Tools pass their arguments to the platform layer
# ---------------------------------------------------------------------------


class TestToolsDelegateToThePlatformLayer:
    TENANT = {"x-inmydata-tenant": "acme"}

    async def test_get_rows_fast_passes_subject_fields_and_filters(self, legacy_server):
        # Arrange
        where = [{"field": "Region", "op": "equals", "value": "North", "logical": "AND"}]
        arguments = {"subject": "Sales", "select": ["Region", "Revenue"], "where": where}

        # Act
        async with legacy_server.running() as call:
            reply = await call("get_rows_fast", arguments, headers=self.TENANT)

        # Assert
        assert reply.json["method"] == "get_rows"
        assert reply.json["args"] == {
            "subject": "Sales",
            "select": ["Region", "Revenue"],
            "where": where,
        }

    async def test_get_top_n_fast_defaults_to_ten_rows(self, legacy_server):
        # Arrange
        arguments = {"subject": "Sales", "group_by": "Region", "order_by": "Revenue"}

        # Act
        async with legacy_server.running() as call:
            reply = await call("get_top_n_fast", arguments, headers=self.TENANT)

        # Assert
        assert reply.json["method"] == "get_top_n"
        assert reply.json["args"]["n"] == 10
        assert reply.json["args"]["where"] == []

    async def test_get_top_n_fast_passes_the_requested_row_count(self, legacy_server):
        # Arrange
        arguments = {"subject": "Sales", "group_by": "Region", "order_by": "Revenue", "n": 3}

        # Act
        async with legacy_server.running() as call:
            reply = await call("get_top_n_fast", arguments, headers=self.TENANT)

        # Assert
        assert reply.json["args"]["n"] == 3

    async def test_query_results_fast_passes_the_instance_and_sql(self, legacy_server):
        # Arrange
        arguments = {"instance_id": "abc-123", "sql": "SELECT 1 FROM my_table"}

        # Act
        async with legacy_server.running() as call:
            reply = await call("query_results_fast", arguments, headers=self.TENANT)

        # Assert
        assert reply.json["method"] == "query_results"
        assert reply.json["args"] == arguments

    async def test_get_answer_slow_passes_the_question(self, legacy_server):
        # Arrange
        arguments = {"question": "Why did sales fall?"}

        # Act
        async with legacy_server.running() as call:
            reply = await call("get_answer_slow", arguments, headers=self.TENANT)

        # Assert
        assert reply.json["method"] == "get_answer"
        assert reply.json["args"] == {"question": "Why did sales fall?"}

    async def test_get_schema_returns_the_schema(self, legacy_server):
        # Arrange / Act
        async with legacy_server.running() as call:
            reply = await call("get_schema", headers=self.TENANT)

        # Assert
        assert reply.json["method"] == "get_schema"

    async def test_get_financial_periods_passes_the_target_date(self, legacy_server):
        # Arrange / Act
        async with legacy_server.running() as call:
            reply = await call(
                "get_financial_periods", {"target_date": "2025-03-14"}, headers=self.TENANT
            )

        # Assert
        assert reply.json["method"] == "get_financial_periods"
        assert reply.json["args"] == {"target_date": "2025-03-14"}

    @pytest.mark.parametrize(
        "arguments",
        [
            pytest.param(
                {"financial_year": 2024, "period_number": 7, "period_type": "quarter"},
                id="all-given",
            ),
            pytest.param({}, id="none-given"),
            pytest.param({"financial_year": 2020, "period_type": "week"}, id="period-number-missing"),
            pytest.param(
                {"financial_year": 2025, "period_number": 0, "period_type": ""},
                id="invalid-values-left-to-mcp_utils",
            ),
        ],
    )
    async def test_calendar_range_passes_its_arguments_through_unchanged(
        self, legacy_server, arguments
    ):
        # Missing arguments are filled from today's financial period inside
        # mcp_utils (tested in test_mcp_utils_tools.py), so the server must
        # pass them on unchanged, None included.
        expected = {"financial_year": None, "period_number": None, "period_type": None, **arguments}

        # Act
        async with legacy_server.running() as call:
            reply = await call("get_calendar_period_date_range", arguments, headers=self.TENANT)

        # Assert
        assert reply.json["method"] == "get_calendar_period_date_range"
        assert reply.json["args"] == expected


# ---------------------------------------------------------------------------
# Tool failures are reported safely
# ---------------------------------------------------------------------------

_TOOL_CALLS = [
    pytest.param("get_rows_fast", {"subject": "S", "select": ["A"]}, id="get-rows-fast"),
    pytest.param(
        "get_top_n_fast", {"subject": "S", "group_by": "A", "order_by": "B"}, id="get-top-n-fast"
    ),
    pytest.param("query_results_fast", {"instance_id": "i", "sql": "SELECT 1"}, id="query-results-fast"),
    pytest.param("get_answer_slow", {"question": "why?"}, id="get-answer-slow"),
    pytest.param("get_schema", {}, id="get-schema"),
    pytest.param("get_financial_periods", {}, id="get-financial-periods"),
    pytest.param(
        "get_calendar_period_date_range",
        {"financial_year": 2025, "period_number": 1, "period_type": "month"},
        id="get-calendar-period-date-range",
    ),
]


class TestToolFailuresAreReportedSafely:
    HEADERS = {"x-inmydata-tenant": "acme"}

    @pytest.mark.parametrize(("tool", "arguments"), _TOOL_CALLS)
    async def test_deliberate_user_message_is_passed_to_the_caller(
        self, legacy_server, tool, arguments
    ):
        # Arrange
        legacy_server.utils.failure = UserFacingError("Unknown subject 'Nonsense'")

        # Act
        async with legacy_server.running() as call:
            reply = await call(tool, arguments, headers=self.HEADERS)

        # Assert
        assert reply.json == {"error": "Unknown subject 'Nonsense'"}

    @pytest.mark.parametrize(("tool", "arguments"), _TOOL_CALLS)
    async def test_unexpected_error_detail_is_not_leaked_to_the_caller(
        self, legacy_server, tool, arguments
    ):
        # Arrange
        legacy_server.utils.failure = RuntimeError("secret C:\\internal\\path and SQL fragment")

        # Act
        async with legacy_server.running() as call:
            reply = await call(tool, arguments, headers=self.HEADERS)

        # Assert
        body = reply.json
        assert body["error"] == "internal error"
        assert body["correlation_id"]
        assert "secret" not in reply.text


class TestGetAnswerReportsFailuresToTheClient:
    HEADERS = {"x-inmydata-tenant": "acme"}

    async def test_deliberate_user_message_is_also_sent_to_the_client_log(self, legacy_server):
        # Arrange
        legacy_server.utils.failure = UserFacingError("Question was too vague")

        # Act
        async with legacy_server.running() as call:
            reply = await call("get_answer_slow", {"question": "?"}, headers=self.HEADERS)

        # Assert
        assert reply.client_log == ("Question was too vague",)

    async def test_unexpected_error_sends_only_the_correlation_id_to_the_client_log(
        self, legacy_server
    ):
        # Arrange
        legacy_server.utils.failure = RuntimeError("secret internal detail")

        # Act
        async with legacy_server.running() as call:
            reply = await call("get_answer_slow", {"question": "?"}, headers=self.HEADERS)

        # Assert
        correlation_id = reply.json["correlation_id"]
        assert reply.client_log == (f"internal error (correlation_id: {correlation_id})",)


# ---------------------------------------------------------------------------
# Agentic RAG tenant resolution in OAuth mode
# ---------------------------------------------------------------------------


@pytest.fixture
def rag_queries(monkeypatch):
    """Record the external ids sent to the (faked) Agentic RAG service."""
    seen: list[str] = []

    async def _query(self, external_id, query, session_id=None):  # noqa: ARG001
        seen.append(external_id)
        return {"response": "grounded answer"}

    monkeypatch.setattr(agentic_rag_client.AgenticRagClient, "query", _query)
    return seen


class TestAgenticRagInOAuthMode:
    RAG_ENV = {"AGENTIC_RAG_BASE_URL": "https://rag.example.test", "AGENTIC_RAG_PSK": "psk"}

    async def test_unauthenticated_request_cannot_query_the_knowledge_base(
        self, load_server, rag_queries
    ):
        # Arrange
        server = load_server(oauth=True, env=self.RAG_ENV)

        # Act
        async with server.running() as call:
            reply = await call("agentic_rag_query", {"query": "what is our refund policy?"})

        # Assert
        assert reply.status == 401
        assert rag_queries == []

    async def test_rag_tool_is_unavailable_with_a_clear_message_when_not_configured(
        self, oauth_server
    ):
        # Arrange
        oauth_server.accept("tok", imd_tenant="acme", sub="u1")

        # Act
        async with oauth_server.running() as call:
            reply = await call("agentic_rag_query", {"query": "hello"}, token="tok")

        # Assert
        assert reply.text.startswith("error: agentic_rag not configured")

    async def test_authenticated_rag_query_uses_the_tenant_and_app_from_the_request(
        self, load_server, rag_queries
    ):
        # Arrange
        server = load_server(oauth=True, env=self.RAG_ENV)
        server.accept("tok", imd_tenant="acme", sub="u1")

        # Act
        async with server.running() as call:
            reply = await call(
                "agentic_rag_query",
                {"query": "what is our refund policy?"},
                token="tok",
                headers={"x-inmydata-rag-app": "support"},
            )

        # Assert
        assert "grounded answer" in reply.text
        assert rag_queries == ["acme:support"]


# ---------------------------------------------------------------------------
# Environment settings read at import time
# ---------------------------------------------------------------------------


class TestImportTimeSettings:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param("true", True, id="lower-true"),
            pytest.param("TRUE", True, id="upper-true"),
            pytest.param("false", False, id="false"),
            pytest.param("yes", False, id="yes-is-not-true"),
            pytest.param("1", False, id="one-is-not-true"),
        ],
    )
    def test_oauth_is_switched_on_only_by_the_word_true(self, load_server, value, expected):
        # Arrange / Act
        server = load_server(oauth=False, env={"INMYDATA_USE_OAUTH": value})

        # Assert
        assert server.module.INMYDATA_USE_OAUTH is expected

    def test_audience_and_issuer_are_built_from_the_configured_hosts(self, oauth_server):
        # Arrange / Act
        verifier = oauth_server.module.token_verifier

        # Assert
        assert verifier.audience == f"https://{MCP_HOST}/mcp"
        assert verifier.issuer == ISSUER

    def test_introspection_uses_the_auth_servers_endpoint_and_configured_client(
        self, load_server
    ):
        # Arrange / Act
        server = load_server(
            oauth=True,
            env={
                "INMYDATA_INTROSPECTION_CLIENT_ID": "introspector",
                "INMYDATA_INTROSPECTION_CLIENT_SECRET": "shh",
            },
        )
        verifier = server.module.token_verifier

        # Assert
        assert verifier.introspection_endpoint == f"https://{AUTH_SERVER}/connect/introspect"
        assert verifier.client_id == "introspector"
        assert verifier.client_secret == "shh"

    def test_legacy_mode_builds_a_server_without_authentication(self, legacy_server):
        # Arrange / Act
        auth = legacy_server.module.mcp.auth

        # Assert
        assert auth is None

    def test_oauth_mode_builds_a_server_with_authentication(self, oauth_server):
        # Arrange / Act
        auth = oauth_server.module.mcp.auth

        # Assert
        assert auth is oauth_server.module.auth
