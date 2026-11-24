"""
Tests for the parts of pat_jwt_auth that test_pat_cache.py does not reach:

  * reading the cache settings from environment variables (including the
    deprecated INMYDATA_TOKEN_CACHE_TTL alias and bad values)
  * the real introspection call: what is sent to the identity provider and
    how each kind of reply is turned into an access token, a "not valid"
    answer, or a failure that must not be cached
  * the JWT fast path and the "no introspection endpoint" path
  * the PATSupportingRemoteAuthProvider wrapper

The identity provider is replaced with an in-process httpx.MockTransport, so
nothing leaves the machine. The parent JWT verification is replaced where a
test needs a known outcome, because it would otherwise fetch a JWKS.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pat_jwt_auth  # noqa: E402
from mcp_logging import logger as agentmcp_logger  # noqa: E402
from pat_jwt_auth import (  # noqa: E402
    AccessToken,
    AnyHttpUrl,
    PATAwareJWTVerifier,
    PATSupportingRemoteAuthProvider,
    _BoundedTTLCache,
    _read_positive_int_env,
)

INTROSPECTION_URL = "https://auth.test.example/connect/introspect"
PAT = "pat-opaque-value-123"
CLOCK_START = 1_000_000.0


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def logs():
    """Collect agentmcp log lines (at DEBUG level) for the duration of a test."""
    records: list[str] = []

    class _Buffer(logging.Handler):
        def emit(self, record):
            records.append(f"{record.levelname} {record.getMessage()}")

    handler = _Buffer(level=logging.DEBUG)
    previous = agentmcp_logger.level
    agentmcp_logger.setLevel(logging.DEBUG)
    agentmcp_logger.addHandler(handler)
    try:
        yield records
    finally:
        agentmcp_logger.removeHandler(handler)
        agentmcp_logger.setLevel(previous)


@pytest.fixture
def clock(monkeypatch):
    """A controllable wall clock for the cache. Advance with ``clock[0] += n``."""
    now = [CLOCK_START]
    monkeypatch.setattr(pat_jwt_auth.time, "time", lambda: now[0])
    return now


@pytest.fixture
def clean_cache_env(monkeypatch):
    """Make sure no cache setting leaks in from the developer's machine."""
    for name in (
        "INMYDATA_PAT_CACHE_MAX_TTL",
        "INMYDATA_PAT_CACHE_NEGATIVE_TTL",
        "INMYDATA_PAT_CACHE_MAX_ENTRIES",
        "INMYDATA_TOKEN_CACHE_TTL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def force_jwt_failure(monkeypatch):
    """Make JWT validation reject every token, so each call tries introspection."""

    async def _fail(self, token):  # noqa: ARG001
        raise ValueError("not a jwt")

    monkeypatch.setattr(pat_jwt_auth.JWTVerifier, "verify_token", _fail)


class FakeIdentityProvider:
    """Stands in for the introspection endpoint. Records every request."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.reply = lambda request: httpx.Response(200, json={"active": False})

    def respond_with(self, reply):
        self.reply = reply


@pytest.fixture
def idp(monkeypatch):
    fake = FakeIdentityProvider()
    real_client = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        fake.requests.append(request)
        return fake.reply(request)

    def client_factory(*args, **kwargs):  # noqa: ARG001
        return real_client(transport=httpx.MockTransport(handle))

    monkeypatch.setattr(pat_jwt_auth.httpx, "AsyncClient", client_factory)
    return fake


def _json_reply(body: dict, status: int = 200):
    return lambda request: httpx.Response(status, json=body)  # noqa: ARG005


def _verifier(**overrides) -> PATAwareJWTVerifier:
    kwargs = dict(
        jwks_uri="https://auth.test.example/jwks",
        issuer="https://auth.test.example",
        audience="https://mcp.test.example/mcp",
        introspection_endpoint=INTROSPECTION_URL,
        client_id="introspector",
        client_secret="introspector-secret",
        cache_max_positive_ttl=60,
        cache_negative_ttl=10,
        cache_max_entries=8,
    )
    kwargs.update(overrides)
    return PATAwareJWTVerifier(**kwargs)


def _form(request: httpx.Request) -> dict[str, str]:
    return dict(pair.split("=", 1) for pair in request.content.decode().split("&"))


# ---------------------------------------------------------------------------
# _read_positive_int_env
# ---------------------------------------------------------------------------


class TestReadPositiveIntEnv:
    VAR = "TEST_PAT_SETTING"
    ALIAS = "TEST_PAT_SETTING_OLD"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param(None, 60, id="unset"),
            pytest.param("", 60, id="empty"),
            pytest.param("   ", 60, id="blank"),
            pytest.param("30", 30, id="valid"),
            pytest.param(" 45 ", 45, id="valid-with-spaces"),
        ],
    )
    def test_returns_the_value_or_the_default(self, monkeypatch, raw, expected):
        # Arrange
        monkeypatch.delenv(self.VAR, raising=False)
        if raw is not None:
            monkeypatch.setenv(self.VAR, raw)

        # Act
        value = _read_positive_int_env(self.VAR, 60)

        # Assert
        assert value == expected

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("abc", id="text"),
            pytest.param("1.5", id="decimal"),
            pytest.param("0", id="zero"),
            pytest.param("-5", id="negative"),
        ],
    )
    def test_bad_values_fall_back_to_the_default(self, monkeypatch, raw):
        # Arrange
        monkeypatch.setenv(self.VAR, raw)

        # Act
        value = _read_positive_int_env(self.VAR, 60)

        # Assert
        assert value == 60

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("abc", id="text"),
            pytest.param("-5", id="negative"),
        ],
    )
    def test_bad_values_are_reported_in_the_log(self, monkeypatch, logs, raw):
        # Arrange
        monkeypatch.setenv(self.VAR, raw)

        # Act
        _read_positive_int_env(self.VAR, 60)

        # Assert
        assert any(line.startswith("WARNING") and self.VAR in line for line in logs)

    def test_deprecated_alias_is_used_when_the_main_setting_is_missing(
        self, monkeypatch, logs
    ):
        # Arrange
        monkeypatch.delenv(self.VAR, raising=False)
        monkeypatch.setenv(self.ALIAS, "25")

        # Act
        value = _read_positive_int_env(self.VAR, 60, fallback_name=self.ALIAS)

        # Assert
        assert value == 25
        assert any("deprecated" in line and self.ALIAS in line for line in logs)

    def test_main_setting_wins_over_the_deprecated_alias(self, monkeypatch, logs):
        # Arrange
        monkeypatch.setenv(self.VAR, "40")
        monkeypatch.setenv(self.ALIAS, "25")

        # Act
        value = _read_positive_int_env(self.VAR, 60, fallback_name=self.ALIAS)

        # Assert
        assert value == 40
        assert not any("deprecated" in line for line in logs)

    def test_blank_alias_is_ignored(self, monkeypatch):
        # Arrange
        monkeypatch.delenv(self.VAR, raising=False)
        monkeypatch.setenv(self.ALIAS, "  ")

        # Act
        value = _read_positive_int_env(self.VAR, 60, fallback_name=self.ALIAS)

        # Assert
        assert value == 60

    def test_invalid_alias_value_falls_back_to_the_default(self, monkeypatch):
        # Arrange
        monkeypatch.delenv(self.VAR, raising=False)
        monkeypatch.setenv(self.ALIAS, "soon")

        # Act
        value = _read_positive_int_env(self.VAR, 60, fallback_name=self.ALIAS)

        # Assert
        assert value == 60


# ---------------------------------------------------------------------------
# Cache settings read from the environment by the verifier
# ---------------------------------------------------------------------------


class TestVerifierCacheSettingsFromEnvironment:
    @pytest.fixture(autouse=True)
    def _use_fakes(self, clean_cache_env, force_jwt_failure, clock):
        """Every test here needs a clean environment, a failing JWT check and a fixed clock."""

    @staticmethod
    def _env_verifier() -> PATAwareJWTVerifier:
        """A verifier that takes its cache settings from the environment."""
        return _verifier(
            cache_max_positive_ttl=None, cache_negative_ttl=None, cache_max_entries=None
        )

    async def test_default_positive_ttl_is_sixty_seconds(self, idp, clock):
        # Arrange
        idp.respond_with(_json_reply({"active": True, "client_id": "c"}))
        verifier = self._env_verifier()
        await verifier.verify_token(PAT)

        # Act
        clock[0] += 59
        await verifier.verify_token(PAT)
        within_ttl_requests = len(idp.requests)
        clock[0] += 2
        await verifier.verify_token(PAT)

        # Assert
        assert within_ttl_requests == 1
        assert len(idp.requests) == 2

    async def test_positive_ttl_can_be_set_from_the_environment(
        self, idp, clock, monkeypatch
    ):
        # Arrange
        monkeypatch.setenv("INMYDATA_PAT_CACHE_MAX_TTL", "5")
        idp.respond_with(_json_reply({"active": True, "client_id": "c"}))
        verifier = self._env_verifier()
        await verifier.verify_token(PAT)

        # Act
        clock[0] += 6
        await verifier.verify_token(PAT)

        # Assert
        assert len(idp.requests) == 2

    async def test_deprecated_token_cache_ttl_still_sets_the_positive_ttl(
        self, idp, clock, monkeypatch
    ):
        # Arrange
        monkeypatch.setenv("INMYDATA_TOKEN_CACHE_TTL", "5")
        idp.respond_with(_json_reply({"active": True, "client_id": "c"}))
        verifier = self._env_verifier()
        await verifier.verify_token(PAT)

        # Act
        clock[0] += 6
        await verifier.verify_token(PAT)

        # Assert
        assert len(idp.requests) == 2

    async def test_invalid_positive_ttl_setting_keeps_the_sixty_second_default(
        self, idp, clock, monkeypatch
    ):
        # Arrange
        monkeypatch.setenv("INMYDATA_PAT_CACHE_MAX_TTL", "-1")
        idp.respond_with(_json_reply({"active": True, "client_id": "c"}))
        verifier = self._env_verifier()
        await verifier.verify_token(PAT)

        # Act
        clock[0] += 30
        await verifier.verify_token(PAT)

        # Assert
        assert len(idp.requests) == 1

    async def test_negative_ttl_can_be_set_from_the_environment(
        self, idp, clock, monkeypatch
    ):
        # Arrange
        monkeypatch.setenv("INMYDATA_PAT_CACHE_NEGATIVE_TTL", "100")
        idp.respond_with(_json_reply({"active": False}))
        verifier = self._env_verifier()
        await verifier.verify_token(PAT)

        # Act: 50s is well past the 10s default but inside the configured 100s
        clock[0] += 50
        result = await verifier.verify_token(PAT)

        # Assert
        assert result is None
        assert len(idp.requests) == 1

    async def test_max_entries_can_be_set_from_the_environment(
        self, idp, monkeypatch
    ):
        # Arrange
        monkeypatch.setenv("INMYDATA_PAT_CACHE_MAX_ENTRIES", "1")
        idp.respond_with(_json_reply({"active": True, "client_id": "c"}))
        verifier = self._env_verifier()
        await verifier.verify_token("first-pat")
        await verifier.verify_token("second-pat")  # evicts "first-pat"

        # Act
        await verifier.verify_token("first-pat")

        # Assert: first-pat had to be checked again, so three lookups in all
        assert len(idp.requests) == 3


# ---------------------------------------------------------------------------
# The JWT fast path and the missing-endpoint path
# ---------------------------------------------------------------------------


class TestJwtFastPathAndMissingEndpoint:
    async def test_valid_jwt_is_returned_without_contacting_the_identity_provider(
        self, idp, monkeypatch
    ):
        # Arrange
        jwt_result = AccessToken(
            token="a.b.c", client_id="jwt-client", scopes=[], claims={"sub": "u1"}
        )

        async def _accept(self, token):  # noqa: ARG001
            return jwt_result

        monkeypatch.setattr(pat_jwt_auth.JWTVerifier, "verify_token", _accept)
        verifier = _verifier()

        # Act
        result = await verifier.verify_token("a.b.c")

        # Assert
        assert result is jwt_result
        assert idp.requests == []

    async def test_token_rejected_by_jwt_check_without_an_endpoint_is_refused(
        self, idp, force_jwt_failure
    ):
        # Arrange
        verifier = _verifier(introspection_endpoint=None)

        # Act
        result = await verifier.verify_token(PAT)

        # Assert
        assert result is None
        assert idp.requests == []

    async def test_jwt_check_returning_nothing_falls_through_to_introspection(
        self, idp, monkeypatch
    ):
        # Arrange
        async def _reject(self, token):  # noqa: ARG001
            return None

        monkeypatch.setattr(pat_jwt_auth.JWTVerifier, "verify_token", _reject)
        idp.respond_with(_json_reply({"active": True, "client_id": "pat-client"}))
        verifier = _verifier()

        # Act
        result = await verifier.verify_token(PAT)

        # Assert
        assert result is not None and result.client_id == "pat-client"

    async def test_jwt_failure_is_logged_without_the_token(
        self, idp, force_jwt_failure, logs
    ):
        # Arrange
        verifier = _verifier()

        # Act
        await verifier.verify_token(PAT)

        # Assert
        assert any("JWT verification failed" in line for line in logs)
        assert not any(PAT in line for line in logs)


# ---------------------------------------------------------------------------
# What is sent to the introspection endpoint
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("force_jwt_failure")
class TestIntrospectionRequest:
    async def test_posts_the_token_as_a_form_to_the_configured_endpoint(self, idp):
        # Arrange
        verifier = _verifier()

        # Act
        await verifier.verify_token(PAT)

        # Assert
        request, = idp.requests
        assert request.method == "POST"
        assert str(request.url) == INTROSPECTION_URL
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        assert _form(request) == {"token": PAT}

    async def test_authenticates_to_the_endpoint_with_the_client_credentials(self, idp):
        # Arrange
        verifier = _verifier(client_id="introspector", client_secret="s3cret")

        # Act
        await verifier.verify_token(PAT)

        # Assert
        request, = idp.requests
        assert request.headers["authorization"].startswith("Basic ")

    @pytest.mark.parametrize(
        ("client_id", "client_secret"),
        [
            pytest.param("introspector", None, id="no-secret"),
            pytest.param(None, "s3cret", id="no-client-id"),
            pytest.param("", "", id="both-empty"),
        ],
    )
    async def test_sends_no_credentials_unless_both_are_configured(
        self, idp, client_id, client_secret
    ):
        # Arrange
        verifier = _verifier(client_id=client_id, client_secret=client_secret)

        # Act
        await verifier.verify_token(PAT)

        # Assert
        request, = idp.requests
        assert "authorization" not in request.headers


# ---------------------------------------------------------------------------
# How introspection replies are interpreted
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("force_jwt_failure")
class TestIntrospectionReplies:
    async def test_active_reply_becomes_an_access_token_carrying_the_claims(self, idp):
        # Arrange
        reply = {
            "active": True,
            "client_id": "claude-connector",
            "scope": "openid profile inmydata.RAG",
            "exp": 4_102_444_800,
            "sub": "user-1",
            "imd_tenant": "acme",
        }
        idp.respond_with(_json_reply(reply))
        verifier = _verifier()

        # Act
        result = await verifier.verify_token(PAT)

        # Assert
        assert result is not None
        assert result.token == PAT
        assert result.client_id == "claude-connector"
        assert result.scopes == ["openid", "profile", "inmydata.RAG"]
        assert result.expires_at == 4_102_444_800
        assert result.claims["imd_tenant"] == "acme"
        assert result.claims["sub"] == "user-1"

    @pytest.mark.parametrize(
        ("reply", "expected_client_id"),
        [
            pytest.param({"active": True, "client_id": "cid", "azp": "other"}, "cid", id="client-id-wins"),
            pytest.param({"active": True, "azp": "authorised-party"}, "authorised-party", id="azp-fallback"),
            pytest.param({"active": True}, "unknown", id="neither"),
        ],
    )
    async def test_client_id_comes_from_client_id_then_azp_then_unknown(
        self, idp, reply, expected_client_id
    ):
        # Arrange
        idp.respond_with(_json_reply(reply))
        verifier = _verifier()

        # Act
        result = await verifier.verify_token(PAT)

        # Assert
        assert result is not None and result.client_id == expected_client_id

    @pytest.mark.parametrize(
        ("scope", "expected"),
        [
            pytest.param("a b  c", ["a", "b", "c"], id="space-separated-string"),
            pytest.param("", [], id="empty-string"),
            pytest.param(["a", "b"], ["a", "b"], id="list"),
            pytest.param(None, [], id="null"),
            pytest.param(42, [], id="number"),
            pytest.param({"a": 1}, [], id="object"),
        ],
    )
    async def test_scope_is_normalised_to_a_list_of_strings(self, idp, scope, expected):
        # Arrange
        idp.respond_with(_json_reply({"active": True, "client_id": "c", "scope": scope}))
        verifier = _verifier()

        # Act
        result = await verifier.verify_token(PAT)

        # Assert
        assert result is not None and result.scopes == expected

    async def test_missing_scope_gives_an_empty_list(self, idp):
        # Arrange
        idp.respond_with(_json_reply({"active": True, "client_id": "c"}))
        verifier = _verifier()

        # Act
        result = await verifier.verify_token(PAT)

        # Assert
        assert result is not None and result.scopes == []

    @pytest.mark.parametrize(
        "reply",
        [
            pytest.param({"active": False}, id="active-false"),
            pytest.param({}, id="active-missing"),
            pytest.param({"active": None}, id="active-null"),
        ],
    )
    async def test_inactive_reply_refuses_the_token(self, idp, reply):
        # Arrange
        idp.respond_with(_json_reply(reply))
        verifier = _verifier()

        # Act
        result = await verifier.verify_token(PAT)

        # Assert
        assert result is None

    async def test_inactive_reply_is_logged_without_the_token(self, idp, logs):
        # Arrange
        idp.respond_with(_json_reply({"active": False}))
        verifier = _verifier()

        # Act
        await verifier.verify_token(PAT)

        # Assert
        assert any("inactive" in line for line in logs)
        assert not any(PAT in line for line in logs)

    @pytest.mark.parametrize("status", [400, 401, 500, 503], ids=["400", "401", "500", "503"])
    async def test_error_status_refuses_the_token(self, idp, status):
        # Arrange
        idp.respond_with(_json_reply({"error": "server_error"}, status=status))
        verifier = _verifier()

        # Act
        result = await verifier.verify_token(PAT)

        # Assert
        assert result is None

    async def test_error_status_is_not_cached_so_the_next_call_asks_again(self, idp):
        # Arrange
        idp.respond_with(_json_reply({"error": "server_error"}, status=503))
        verifier = _verifier()
        await verifier.verify_token(PAT)
        idp.respond_with(_json_reply({"active": True, "client_id": "c"}))

        # Act
        result = await verifier.verify_token(PAT)

        # Assert
        assert result is not None
        assert len(idp.requests) == 2

    async def test_error_status_logs_the_status_but_not_the_token(self, idp, logs):
        # Arrange
        idp.respond_with(_json_reply({"error": "server_error"}, status=503))
        verifier = _verifier()

        # Act
        await verifier.verify_token(PAT)

        # Assert
        assert any("status=503" in line for line in logs)
        assert not any(PAT in line for line in logs)

    async def test_error_body_is_redacted_in_debug_logs(self, idp, logs):
        # Arrange
        body = {"error": "invalid_client", "client_secret": "leaky-secret-value"}
        idp.respond_with(_json_reply(body, status=401))
        verifier = _verifier()

        # Act
        await verifier.verify_token(PAT)

        # Assert
        assert any("invalid_client" in line for line in logs)
        assert not any("leaky-secret-value" in line for line in logs)

    async def test_non_json_error_body_is_not_written_to_the_logs(self, idp, logs):
        # Arrange
        idp.respond_with(
            lambda request: httpx.Response(502, text="<html>secret-gateway-detail</html>")  # noqa: ARG005
        )
        verifier = _verifier()

        # Act
        await verifier.verify_token(PAT)

        # Assert
        assert any("non-json body suppressed" in line for line in logs)
        assert not any("secret-gateway-detail" in line for line in logs)

    async def test_malformed_success_body_refuses_the_token_without_caching(self, idp):
        # Arrange
        idp.respond_with(lambda request: httpx.Response(200, text="not json"))  # noqa: ARG005
        verifier = _verifier()
        await verifier.verify_token(PAT)

        # Act
        result = await verifier.verify_token(PAT)

        # Assert
        assert result is None
        assert len(idp.requests) == 2

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(httpx.ConnectError("connection refused"), id="connect-error"),
            pytest.param(httpx.ReadTimeout("timed out"), id="timeout"),
            pytest.param(RuntimeError("unexpected"), id="unexpected-error"),
        ],
    )
    async def test_network_failure_refuses_the_token_and_is_not_cached(self, idp, error):
        # Arrange
        def _fail(request):  # noqa: ARG001
            raise error

        idp.respond_with(_fail)
        verifier = _verifier()
        first = await verifier.verify_token(PAT)
        idp.respond_with(_json_reply({"active": True, "client_id": "c"}))

        # Act
        second = await verifier.verify_token(PAT)

        # Assert
        assert first is None
        assert second is not None
        assert len(idp.requests) == 2

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(httpx.ConnectError("connection refused"), id="http-error"),
            pytest.param(RuntimeError("unexpected"), id="unexpected-error"),
        ],
    )
    async def test_network_failure_is_logged_without_the_token(self, idp, logs, error):
        # Arrange
        def _fail(request):  # noqa: ARG001
            raise error

        idp.respond_with(_fail)
        verifier = _verifier()

        # Act
        await verifier.verify_token(PAT)

        # Assert
        assert any(line.startswith("WARNING") and type(error).__name__ in line for line in logs)
        assert not any(PAT in line for line in logs)


# ---------------------------------------------------------------------------
# Caching behaviour seen through the real introspection call
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("force_jwt_failure")
class TestCachingOfIntrospectionResults:
    async def test_active_reply_is_reused_for_the_same_token(self, idp):
        # Arrange
        idp.respond_with(_json_reply({"active": True, "client_id": "c"}))
        verifier = _verifier()
        first = await verifier.verify_token(PAT)

        # Act
        second = await verifier.verify_token(PAT)

        # Assert
        assert second is first
        assert len(idp.requests) == 1

    @pytest.mark.parametrize(
        "claims",
        [
            pytest.param({"exp": None}, id="null-exp"),
            pytest.param({}, id="no-exp"),
        ],
    )
    async def test_reply_without_a_usable_exp_is_cached_for_the_normal_cap(
        self, idp, clock, claims
    ):
        # Arrange
        idp.respond_with(_json_reply({"active": True, "client_id": "c", **claims}))
        verifier = _verifier(cache_max_positive_ttl=60)
        await verifier.verify_token(PAT)

        # Act
        clock[0] += 30
        await verifier.verify_token(PAT)
        requests_inside_cap = len(idp.requests)
        clock[0] += 31
        await verifier.verify_token(PAT)

        # Assert
        assert requests_inside_cap == 1
        assert len(idp.requests) == 2

    @pytest.mark.parametrize(
        "exp",
        [
            pytest.param("soon", id="text"),
            pytest.param([1], id="list"),
        ],
    )
    async def test_reply_with_a_non_numeric_exp_is_refused_and_not_cached(self, idp, exp):
        # Arrange
        idp.respond_with(_json_reply({"active": True, "client_id": "c", "exp": exp}))
        verifier = _verifier()
        first = await verifier.verify_token(PAT)

        # Act
        await verifier.verify_token(PAT)

        # Assert
        assert first is None
        assert len(idp.requests) == 2

    async def test_integer_exp_sooner_than_the_cap_limits_the_cache_time(self, idp, clock):
        # Arrange
        exp = int(CLOCK_START) + 10
        idp.respond_with(_json_reply({"active": True, "client_id": "c", "exp": exp}))
        verifier = _verifier(cache_max_positive_ttl=60)
        await verifier.verify_token(PAT)

        # Act
        clock[0] += 11
        await verifier.verify_token(PAT)

        # Assert
        assert len(idp.requests) == 2

    async def test_token_already_expired_upstream_is_returned_but_not_cached(
        self, idp, clock
    ):
        # Arrange
        exp = int(CLOCK_START) - 5
        idp.respond_with(_json_reply({"active": True, "client_id": "c", "exp": exp}))
        verifier = _verifier()
        first = await verifier.verify_token(PAT)

        # Act
        await verifier.verify_token(PAT)

        # Assert
        assert first is not None
        assert len(idp.requests) == 2

    async def test_inactive_reply_is_remembered_so_repeats_do_not_reach_the_provider(
        self, idp
    ):
        # Arrange
        idp.respond_with(_json_reply({"active": False}))
        verifier = _verifier()
        await verifier.verify_token("revoked-pat")

        # Act
        result = await verifier.verify_token("revoked-pat")

        # Assert
        assert result is None
        assert len(idp.requests) == 1

    async def test_different_tokens_never_share_a_cached_result(self, idp):
        # Arrange: the provider answers differently depending on the token sent
        def _by_token(request):
            token = _form(request)["token"]
            tenant = "tenant-a" if token == "pat-a" else "tenant-b"
            return httpx.Response(
                200, json={"active": True, "client_id": "c", "imd_tenant": tenant}
            )

        idp.respond_with(_by_token)
        verifier = _verifier()
        await verifier.verify_token("pat-a")

        # Act
        result_b = await verifier.verify_token("pat-b")
        result_a_again = await verifier.verify_token("pat-a")

        # Assert
        assert result_b is not None and result_b.claims["imd_tenant"] == "tenant-b"
        assert result_a_again is not None and result_a_again.claims["imd_tenant"] == "tenant-a"

    async def test_an_invalid_token_does_not_cause_a_valid_one_to_be_refused(self, idp):
        # Arrange
        def _by_token(request):
            active = _form(request)["token"] == "good-pat"
            return httpx.Response(200, json={"active": active, "client_id": "c"})

        idp.respond_with(_by_token)
        verifier = _verifier()
        await verifier.verify_token("bad-pat")

        # Act
        result = await verifier.verify_token("good-pat")

        # Assert
        assert result is not None


# ---------------------------------------------------------------------------
# _BoundedTTLCache size
# ---------------------------------------------------------------------------


class TestBoundedTTLCacheSize:
    def test_length_counts_the_stored_entries(self):
        # Arrange
        cache = _BoundedTTLCache(max_entries=5, max_positive_ttl=60, negative_ttl=10)

        # Act
        cache.set_negative("a")
        cache.set_negative("b")

        # Assert
        assert len(cache) == 2

    def test_length_never_exceeds_the_maximum(self):
        # Arrange
        cache = _BoundedTTLCache(max_entries=2, max_positive_ttl=60, negative_ttl=10)

        # Act
        for key in ("a", "b", "c", "d"):
            cache.set_negative(key)

        # Assert
        assert len(cache) == 2

    def test_cache_key_is_a_hash_so_the_token_is_not_stored_in_clear(self):
        # Arrange
        token = "very-secret-pat"

        # Act
        key = PATAwareJWTVerifier._cache_key(token)

        # Assert
        assert key == hashlib.sha256(token.encode()).hexdigest()
        assert token not in key


# ---------------------------------------------------------------------------
# PATSupportingRemoteAuthProvider
# ---------------------------------------------------------------------------


class TestPATSupportingRemoteAuthProvider:
    def _provider(self):
        verifier = _verifier()
        provider = PATSupportingRemoteAuthProvider(
            token_verifier=verifier,
            authorization_servers=[AnyHttpUrl("https://auth.test.example")],
            base_url="https://mcp.test.example",
        )
        return provider, verifier

    def test_uses_the_pat_aware_verifier_it_was_given(self):
        # Arrange / Act
        provider, verifier = self._provider()

        # Assert
        assert provider.token_verifier is verifier

    def test_advertises_the_configured_authorisation_server(self):
        # Arrange / Act
        provider, _ = self._provider()

        # Assert
        assert [str(url) for url in provider.authorization_servers] == ["https://auth.test.example/"]

    def test_publishes_protected_resource_metadata_route(self):
        # Arrange
        provider, _ = self._provider()

        # Act
        paths = [route.path for route in provider.get_routes("/mcp")]

        # Assert
        assert "/.well-known/oauth-protected-resource" in paths

    async def test_verifies_pats_through_the_wrapped_verifier(self, idp, force_jwt_failure):
        # Arrange
        idp.respond_with(_json_reply({"active": True, "client_id": "pat-client"}))
        provider, _ = self._provider()

        # Act
        result = await provider.token_verifier.verify_token(PAT)

        # Assert
        assert result is not None and result.client_id == "pat-client"


# Silence the unused-import check for names kept for readability of the module.
assert json and time
