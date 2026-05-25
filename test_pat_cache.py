"""
Tests for the _BoundedTTLCache used by PATAwareJWTVerifier.

Covers:
- cache hit returns within TTL
- upstream exp greater than MAX_POSITIVE_TTL is clamped
- negative cache short-circuits a second invalid-token lookup
- LRU eviction removes the oldest entry when full
- revocation is reflected after MAX_POSITIVE_TTL even when upstream exp is far in the future
"""
import time
import unittest
from unittest.mock import MagicMock

from pat_jwt_auth import _BoundedTTLCache


def _make_token(client_id="test-client"):
    """Return a minimal AccessToken-like mock."""
    token = MagicMock()
    token.client_id = client_id
    return token


class TestBoundedTTLCacheCacheHit(unittest.TestCase):
    """cache hit returns within TTL."""

    def test_positive_hit_within_ttl(self):
        cache = _BoundedTTLCache(max_entries=10, max_positive_ttl=60, negative_ttl=10)
        tok = _make_token()
        cache.set_positive("key1", tok, upstream_exp=time.time() + 3600)

        status, value = cache.get("key1")
        self.assertEqual(status, "hit_positive")
        self.assertIs(value, tok)

    def test_positive_miss_after_expiry(self):
        cache = _BoundedTTLCache(max_entries=10, max_positive_ttl=60, negative_ttl=10)
        tok = _make_token()
        # Set expiry 1 second in the past
        past_exp = time.time() - 1
        cache._cache["key1"] = (tok, past_exp)

        status, value = cache.get("key1")
        self.assertEqual(status, "miss")
        self.assertIsNone(value)

    def test_miss_for_unknown_key(self):
        cache = _BoundedTTLCache(max_entries=10, max_positive_ttl=60, negative_ttl=10)
        status, value = cache.get("nonexistent")
        self.assertEqual(status, "miss")
        self.assertIsNone(value)


class TestTTLClamping(unittest.TestCase):
    """upstream exp greater than MAX_POSITIVE_TTL is clamped."""

    def test_far_future_exp_is_clamped(self):
        max_ttl = 3600  # 1 hour
        cache = _BoundedTTLCache(max_entries=10, max_positive_ttl=max_ttl, negative_ttl=10)
        tok = _make_token()
        far_future = time.time() + 86400  # 24 hours

        cache.set_positive("key1", tok, upstream_exp=far_future)

        _, expiry = cache._cache["key1"]
        now = time.time()
        # Effective expiry must be ≤ now + max_ttl (with a small tolerance)
        self.assertLessEqual(expiry, now + max_ttl + 1)
        self.assertGreater(expiry, now + max_ttl - 5)

    def test_near_exp_is_not_extended(self):
        max_ttl = 3600
        cache = _BoundedTTLCache(max_entries=10, max_positive_ttl=max_ttl, negative_ttl=10)
        tok = _make_token()
        near_exp = time.time() + 30  # only 30 s left

        cache.set_positive("key1", tok, upstream_exp=near_exp)

        _, expiry = cache._cache["key1"]
        # Effective expiry must be the near_exp, not the max_ttl
        self.assertAlmostEqual(expiry, near_exp, delta=1)

    def test_none_upstream_exp_uses_max_ttl(self):
        max_ttl = 3600
        cache = _BoundedTTLCache(max_entries=10, max_positive_ttl=max_ttl, negative_ttl=10)
        tok = _make_token()

        cache.set_positive("key1", tok, upstream_exp=None)

        _, expiry = cache._cache["key1"]
        now = time.time()
        self.assertAlmostEqual(expiry, now + max_ttl, delta=2)


class TestNegativeCache(unittest.TestCase):
    """negative cache short-circuits a second invalid-token lookup."""

    def test_negative_cache_hit(self):
        cache = _BoundedTTLCache(max_entries=10, max_positive_ttl=60, negative_ttl=10)
        cache.set_negative("bad-token-hash")

        status, value = cache.get("bad-token-hash")
        self.assertEqual(status, "hit_negative")
        self.assertIsNone(value)

    def test_negative_cache_respects_ttl(self):
        cache = _BoundedTTLCache(max_entries=10, max_positive_ttl=60, negative_ttl=10)
        # Manually insert an already-expired negative entry
        cache._cache["bad-key"] = (_BoundedTTLCache._NEGATIVE_SENTINEL, time.time() - 1)

        status, value = cache.get("bad-key")
        self.assertEqual(status, "miss")
        self.assertIsNone(value)

    def test_negative_ttl_has_jitter(self):
        """Negative TTL entries should not all share the exact same expiry (jitter applied)."""
        cache = _BoundedTTLCache(max_entries=100, max_positive_ttl=60, negative_ttl=10)
        expiries = set()
        for i in range(20):
            key = f"key-{i}"
            cache.set_negative(key)
            _, expiry = cache._cache[key]
            # Round to 3 decimal places to check for variance
            expiries.add(round(expiry, 3))
        # With 20 entries and ±20 % jitter the expiries should not all be identical
        self.assertGreater(len(expiries), 1)


class TestLRUEviction(unittest.TestCase):
    """LRU eviction removes the oldest entry when full."""

    def test_oldest_entry_evicted_when_full(self):
        cache = _BoundedTTLCache(max_entries=3, max_positive_ttl=60, negative_ttl=10)
        tok = _make_token()
        far = time.time() + 3600

        cache.set_positive("first", tok, upstream_exp=far)
        cache.set_positive("second", tok, upstream_exp=far)
        cache.set_positive("third", tok, upstream_exp=far)
        # Adding a 4th entry should evict "first" (LRU)
        cache.set_positive("fourth", tok, upstream_exp=far)

        self.assertEqual(len(cache), 3)
        status, _ = cache.get("first")
        self.assertEqual(status, "miss")  # evicted

    def test_accessed_entry_is_not_evicted(self):
        cache = _BoundedTTLCache(max_entries=3, max_positive_ttl=60, negative_ttl=10)
        tok = _make_token()
        far = time.time() + 3600

        cache.set_positive("first", tok, upstream_exp=far)
        cache.set_positive("second", tok, upstream_exp=far)
        cache.set_positive("third", tok, upstream_exp=far)

        # Access "first" to promote it to MRU position
        cache.get("first")

        # Adding a 4th entry should evict "second" (now the LRU)
        cache.set_positive("fourth", tok, upstream_exp=far)

        self.assertEqual(len(cache), 3)
        status_first, _ = cache.get("first")
        self.assertEqual(status_first, "hit_positive")  # still present

        status_second, _ = cache.get("second")
        self.assertEqual(status_second, "miss")  # evicted


class TestRevocationReflection(unittest.TestCase):
    """Revocation is reflected after MAX_POSITIVE_TTL even when upstream exp is far in future."""

    def test_positive_entry_expires_at_capped_ttl(self):
        max_ttl = 5  # 5 seconds for test speed
        cache = _BoundedTTLCache(max_entries=10, max_positive_ttl=max_ttl, negative_ttl=10)
        tok = _make_token()
        far_future = time.time() + 86400  # 24 hours upstream exp

        cache.set_positive("revoked-token", tok, upstream_exp=far_future)

        # Entry must be present immediately
        status, _ = cache.get("revoked-token")
        self.assertEqual(status, "hit_positive")

        # Simulate time passing beyond the capped TTL
        _, expiry = cache._cache["revoked-token"]
        # Manually backdate the expiry to simulate passage of max_ttl
        cache._cache["revoked-token"] = (tok, time.time() - 0.001)

        status_after, _ = cache.get("revoked-token")
        self.assertEqual(status_after, "miss")


if __name__ == "__main__":
    unittest.main()
