"""
Tests for the bounded PAT introspection cache (GHSA-m387-5xpr-wpqx / issue #6).

Covers:
  * positive cache hit within TTL
  * upstream `exp` greater than the configured cap is clamped
  * upstream `exp` sooner than the cap wins
  * negative cache short-circuits a second invalid-token lookup
  * LRU eviction removes the oldest entry when full
  * recently-touched entries survive eviction
  * revocation is reflected after the cap even when upstream `exp` is far future
  * transient introspection failures are not cached
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pat_jwt_auth  # noqa: E402
from pat_jwt_auth import (  # noqa: E402
    AccessToken,
    PATAwareJWTVerifier,
    _BoundedTTLCache,
)


def _make_token(token: str = "tok", exp: int | None = None) -> AccessToken:
    claims: dict = {}
    if exp is not None:
        claims["exp"] = exp
    return AccessToken(
        token=token,
        client_id="cid",
        scopes=[],
        expires_at=exp,
        claims=claims,
    )


# ---------------------------------------------------------------------------
# _BoundedTTLCache unit tests
# ---------------------------------------------------------------------------


class TestBoundedTTLCache:
    def _cache(self, **overrides) -> _BoundedTTLCache:
        kwargs = dict(max_entries=4, max_positive_ttl=60, negative_ttl=10)
        kwargs.update(overrides)
        return _BoundedTTLCache(**kwargs)

    def test_miss_when_empty(self):
        c = self._cache()
        assert c.get("k") == ("miss", None)

    def test_positive_hit_within_ttl(self):
        c = self._cache()
        t = _make_token()
        c.set_positive("k", t, upstream_exp=None)
        kind, value = c.get("k")
        assert kind == "hit_positive"
        assert value is t

    def test_positive_expires_after_max_ttl(self, monkeypatch):
        c = self._cache(max_positive_ttl=60)
        now = [1000.0]
        monkeypatch.setattr(pat_jwt_auth.time, "time", lambda: now[0])
        c.set_positive("k", _make_token(), upstream_exp=None)
        now[0] += 61
        assert c.get("k") == ("miss", None)

    def test_positive_clamps_far_future_upstream_exp(self, monkeypatch):
        """An upstream `exp` far in the future must not extend the cache
        entry beyond the configured cap."""
        c = self._cache(max_positive_ttl=60)
        now = [1000.0]
        monkeypatch.setattr(pat_jwt_auth.time, "time", lambda: now[0])
        c.set_positive("k", _make_token(), upstream_exp=now[0] + 10_000)
        # 61s later we're past the 60s cap but nowhere near upstream exp
        now[0] += 61
        assert c.get("k") == ("miss", None)

    def test_positive_uses_sooner_upstream_exp(self, monkeypatch):
        """If upstream `exp` is sooner than `now + max_positive_ttl`, the
        upstream value must be honored."""
        c = self._cache(max_positive_ttl=60)
        now = [1000.0]
        monkeypatch.setattr(pat_jwt_auth.time, "time", lambda: now[0])
        c.set_positive("k", _make_token(), upstream_exp=now[0] + 10)
        now[0] += 11
        assert c.get("k") == ("miss", None)

    def test_positive_refuses_already_expired_upstream(self, monkeypatch):
        c = self._cache()
        now = [1000.0]
        monkeypatch.setattr(pat_jwt_auth.time, "time", lambda: now[0])
        c.set_positive("k", _make_token(), upstream_exp=now[0] - 1)
        assert c.get("k") == ("miss", None)

    def test_negative_hit(self):
        c = self._cache()
        c.set_negative("k")
        kind, value = c.get("k")
        assert kind == "hit_negative"
        assert value is None

    def test_negative_expires_past_jitter_ceiling(self, monkeypatch):
        """Worst-case jittered TTL is negative_ttl * 1.2 — advance past it."""
        c = self._cache(negative_ttl=10)
        now = [1000.0]
        monkeypatch.setattr(pat_jwt_auth.time, "time", lambda: now[0])
        c.set_negative("k")
        now[0] += 13  # > 10 * 1.2
        assert c.get("k") == ("miss", None)

    def test_lru_evicts_oldest_on_overflow(self):
        c = self._cache(max_entries=2)
        c.set_negative("a")
        c.set_negative("b")
        c.set_negative("c")  # should push "a" out
        assert c.get("a") == ("miss", None)
        assert c.get("b")[0] == "hit_negative"
        assert c.get("c")[0] == "hit_negative"

    def test_lru_touch_on_get_protects_entry(self):
        c = self._cache(max_entries=2)
        c.set_negative("a")
        c.set_negative("b")
        # Touch "a" so it becomes most-recently-used.
        c.get("a")
        c.set_negative("c")  # should evict "b" now, not "a"
        assert c.get("a")[0] == "hit_negative"
        assert c.get("b") == ("miss", None)
        assert c.get("c")[0] == "hit_negative"


# ---------------------------------------------------------------------------
# Integration: PATAwareJWTVerifier with the new cache
# ---------------------------------------------------------------------------


def _make_verifier(**overrides) -> PATAwareJWTVerifier:
    kwargs = dict(
        jwks_uri="https://test.example.com/jwks",
        issuer="https://test.example.com",
        audience="https://test.example.com/mcp",
        introspection_endpoint="https://test.example.com/introspect",
        client_id="cid",
        client_secret="sec",
        cache_max_positive_ttl=60,
        cache_negative_ttl=10,
        cache_max_entries=8,
    )
    kwargs.update(overrides)
    return PATAwareJWTVerifier(**kwargs)


@pytest.fixture
def force_jwt_failure(monkeypatch):
    """Make the parent JWTVerifier.verify_token always raise so every call
    falls through to the PAT introspection path."""
    async def _fail(self, token):  # noqa: ARG001
        raise ValueError("not a jwt")
    monkeypatch.setattr(
        pat_jwt_auth.JWTVerifier, "verify_token", _fail
    )


class TestVerifierIntegration:
    @pytest.mark.asyncio
    async def test_negative_cache_short_circuits_introspect(
        self, force_jwt_failure, monkeypatch
    ):
        v = _make_verifier()
        introspect = AsyncMock(return_value=("inactive", None))
        monkeypatch.setattr(v, "_introspect_token", introspect)

        r1 = await v.verify_token("bad-token")
        r2 = await v.verify_token("bad-token")
        assert r1 is None and r2 is None
        # The whole point of the negative cache: second call must not
        # re-introspect.
        assert introspect.await_count == 1

    @pytest.mark.asyncio
    async def test_failure_is_not_cached(self, force_jwt_failure, monkeypatch):
        v = _make_verifier()
        introspect = AsyncMock(return_value=("failure", None))
        monkeypatch.setattr(v, "_introspect_token", introspect)

        await v.verify_token("tok")
        await v.verify_token("tok")
        # A transient failure must not poison subsequent lookups — both calls
        # must re-introspect.
        assert introspect.await_count == 2

    @pytest.mark.asyncio
    async def test_positive_cache_hit_reuses_result(
        self, force_jwt_failure, monkeypatch
    ):
        v = _make_verifier()
        token_obj = _make_token(exp=int(time.time()) + 10_000)
        introspect = AsyncMock(return_value=("active", token_obj))
        monkeypatch.setattr(v, "_introspect_token", introspect)

        r1 = await v.verify_token("tok")
        r2 = await v.verify_token("tok")
        assert r1 is token_obj
        assert r2 is token_obj
        assert introspect.await_count == 1

    @pytest.mark.asyncio
    async def test_revocation_reflected_after_max_ttl(
        self, force_jwt_failure, monkeypatch
    ):
        """A PAT with a far-future upstream `exp` must still be re-checked
        once the configured cap (60s here) elapses."""
        v = _make_verifier(cache_max_positive_ttl=60)
        now = [time.time()]
        monkeypatch.setattr(pat_jwt_auth.time, "time", lambda: now[0])

        good = _make_token(exp=int(now[0]) + 100_000)
        introspect = AsyncMock(side_effect=[
            ("active", good),
            ("inactive", None),
        ])
        monkeypatch.setattr(v, "_introspect_token", introspect)

        r1 = await v.verify_token("tok")
        assert r1 is good

        # Jump past the configured cap. The cache entry must be discarded
        # despite the upstream exp still being far in the future.
        now[0] += 61
        r2 = await v.verify_token("tok")
        assert r2 is None
        assert introspect.await_count == 2
