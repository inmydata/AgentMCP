"""
Custom RemoteAuthProvider that supports both JWTs and Personal Access Tokens (PATs).
When a PAT is detected (non-JWT), performs token introspection to get a valid JWT.
Caches introspection results to avoid repeated requests for the same PAT.

The introspection cache is a small in-process LRU with these guarantees
(GHSA-m387-5xpr-wpqx / issue #6):

* Positive (active) entries are capped at a configurable short TTL regardless
  of any far-future ``exp`` returned by the IdP, so revocation propagates
  within ``INMYDATA_PAT_CACHE_MAX_TTL`` seconds.
* Negative (``active: false``) responses are cached for a short, jittered
  window so a flood of invalid PATs cannot be used to amplify load onto the
  introspection endpoint.
* The cache is bounded by entry count and evicts strict LRU; transient
  introspection failures are never cached so a brief IdP outage does not
  poison the cache.
"""
import hashlib
import httpx
import logging
import os
import random
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier, AccessToken
from pydantic import AnyHttpUrl

from mcp_logging import logger, redact, token_fingerprint


_DEFAULT_MAX_POSITIVE_TTL = 60
_DEFAULT_NEGATIVE_TTL = 10
_DEFAULT_MAX_ENTRIES = 1024


def _read_positive_int_env(name: str, default: int, fallback_name: Optional[str] = None) -> int:
    """Read a positive int from the environment, falling back to ``default``.

    A second ``fallback_name`` may be supplied for a deprecated alias; if the
    primary variable is unset but the alias is set, the alias is used and a
    one-time deprecation warning is logged.
    """
    raw = os.environ.get(name)
    if (raw is None or raw.strip() == "") and fallback_name is not None:
        alias = os.environ.get(fallback_name)
        if alias is not None and alias.strip() != "":
            logger.warning(
                "%s is deprecated; please set %s instead.", fallback_name, name,
            )
            raw = alias
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using default %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("%s=%d must be positive; using default %d", name, value, default)
        return default
    return value


class _BoundedTTLCache:
    """In-process LRU cache for PAT introspection results.

    Entries are tagged ``"pos"`` (an active introspection result) or ``"neg"``
    (a definitive ``active: false`` response). Expiry is checked lazily on
    read; eviction is strict LRU once ``max_entries`` is exceeded.

    All time values use ``time.time()`` (wall clock); tests monkey-patch
    ``pat_jwt_auth.time.time`` to advance the clock.
    """

    _Entry = Tuple[str, Optional[AccessToken], float]

    def __init__(
        self,
        *,
        max_entries: int,
        max_positive_ttl: int,
        negative_ttl: int,
    ) -> None:
        self._max_entries = max(1, int(max_entries))
        self._max_positive_ttl = max(1, int(max_positive_ttl))
        self._negative_ttl = max(1, int(negative_ttl))
        self._entries: "OrderedDict[str, _BoundedTTLCache._Entry]" = OrderedDict()

    def get(self, key: str) -> Tuple[str, Optional[AccessToken]]:
        """Return ``(kind, value)`` where ``kind`` is one of
        ``"hit_positive"``, ``"hit_negative"``, ``"miss"``."""
        entry = self._entries.get(key)
        if entry is None:
            return ("miss", None)
        kind, value, expiry = entry
        if time.time() >= expiry:
            del self._entries[key]
            return ("miss", None)
        self._entries.move_to_end(key)
        if kind == "pos":
            return ("hit_positive", value)
        return ("hit_negative", None)

    def set_positive(
        self,
        key: str,
        value: AccessToken,
        upstream_exp: Optional[float],
    ) -> None:
        """Cache a positive introspection result.

        ``upstream_exp`` is the ``exp`` claim from the introspection response
        (epoch seconds). The effective expiry is
        ``min(now + max_positive_ttl, upstream_exp)``. If ``upstream_exp`` is
        in the past the entry is not stored.
        """
        now = time.time()
        expiry = now + self._max_positive_ttl
        if upstream_exp is not None:
            if upstream_exp <= now:
                # Already expired upstream — refuse to cache so the next call
                # re-checks immediately.
                return
            expiry = min(expiry, float(upstream_exp))
        self._entries[key] = ("pos", value, expiry)
        self._entries.move_to_end(key)
        self._evict_if_full()

    def set_negative(self, key: str) -> None:
        """Cache a definitive ``active: false`` response with jittered TTL.

        Jitter (±20%) is applied so a synchronised wave of identical invalid
        tokens does not produce a synchronised re-introspection burst when
        the entries expire.
        """
        now = time.time()
        jitter = random.uniform(0.8, 1.2)
        self._entries[key] = ("neg", None, now + (self._negative_ttl * jitter))
        self._entries.move_to_end(key)
        self._evict_if_full()

    def _evict_if_full(self) -> None:
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)


class PATAwareJWTVerifier(JWTVerifier):
    """JWT verifier that falls back to token introspection for PATs.

    Successful introspection results are cached in a bounded LRU; definitive
    ``active: false`` responses are also cached briefly so invalid tokens
    cannot be used to reflect load onto the introspection endpoint.
    """

    def __init__(
        self,
        jwks_uri: str,
        issuer: str,
        audience: str,
        introspection_endpoint: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        cache_max_positive_ttl: Optional[int] = None,
        cache_negative_ttl: Optional[int] = None,
        cache_max_entries: Optional[int] = None,
    ) -> None:
        super().__init__(jwks_uri=jwks_uri, issuer=issuer, audience=audience)
        self.introspection_endpoint = introspection_endpoint
        self.client_id = client_id
        self.client_secret = client_secret

        max_positive_ttl = (
            cache_max_positive_ttl
            if cache_max_positive_ttl is not None
            else _read_positive_int_env(
                "INMYDATA_PAT_CACHE_MAX_TTL",
                _DEFAULT_MAX_POSITIVE_TTL,
                fallback_name="INMYDATA_TOKEN_CACHE_TTL",
            )
        )
        negative_ttl = (
            cache_negative_ttl
            if cache_negative_ttl is not None
            else _read_positive_int_env(
                "INMYDATA_PAT_CACHE_NEGATIVE_TTL", _DEFAULT_NEGATIVE_TTL,
            )
        )
        max_entries = (
            cache_max_entries
            if cache_max_entries is not None
            else _read_positive_int_env(
                "INMYDATA_PAT_CACHE_MAX_ENTRIES", _DEFAULT_MAX_ENTRIES,
            )
        )

        self._cache = _BoundedTTLCache(
            max_entries=max_entries,
            max_positive_ttl=max_positive_ttl,
            negative_ttl=negative_ttl,
        )

    @staticmethod
    def _cache_key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        """Verify a token: try JWT validation first, then fall back to
        cached PAT introspection."""
        try:
            access_token = await super().verify_token(token)
            if access_token is not None:
                return access_token
        except Exception as e:
            # Some JWT libraries embed the offending token in str(e); log only
            # the exception type and a fingerprint of the token.
            logger.debug(
                "JWT verification failed (token=%s, reason=%s); attempting introspection.",
                token_fingerprint(token),
                type(e).__name__,
            )

        if not self.introspection_endpoint:
            return None

        key = self._cache_key(token)
        kind, cached = self._cache.get(key)
        if kind == "hit_positive":
            logger.debug(
                "PAT introspection cache hit (token=%s)", token_fingerprint(token),
            )
            return cached
        if kind == "hit_negative":
            logger.debug(
                "PAT introspection negative cache hit; skipping introspection (token=%s)",
                token_fingerprint(token),
            )
            return None

        status, result = await self._introspect_token(token)
        if status == "active" and result is not None:
            upstream_exp = self._extract_exp(result)
            self._cache.set_positive(key, result, upstream_exp)
            return result
        if status == "inactive":
            self._cache.set_negative(key)
            return None
        # status == "failure": do not cache, so a transient IdP outage does
        # not poison subsequent lookups.
        return None

    @staticmethod
    def _extract_exp(access_token: AccessToken) -> Optional[float]:
        exp = access_token.claims.get("exp") if isinstance(access_token.claims, dict) else None
        if isinstance(exp, (int, float)):
            return float(exp)
        return None

    async def _introspect_token(
        self, token: str,
    ) -> Tuple[str, Optional[AccessToken]]:
        """Call the introspection endpoint.

        Returns one of:
          * ``("active", AccessToken)`` — token is valid.
          * ``("inactive", None)``      — IdP responded that the token is not active.
          * ``("failure", None)``       — network/HTTP error or malformed response;
                                          caller must not cache this result.
        """
        if not self.introspection_endpoint:
            logger.warning("No introspection endpoint configured; cannot validate PAT.")
            return ("failure", None)

        try:
            async with httpx.AsyncClient() as client:
                data = {"token": token}
                auth = None
                if self.client_id and self.client_secret:
                    auth = (self.client_id, self.client_secret)
                headers = {"Content-Type": "application/x-www-form-urlencoded"}

                response = await client.post(
                    self.introspection_endpoint,
                    data=data,
                    headers=headers,
                    auth=auth,
                    timeout=10.0,
                )

                if response.status_code != 200:
                    logger.warning(
                        "Introspection failed (token=%s, status=%d)",
                        token_fingerprint(token),
                        response.status_code,
                    )
                    if logger.isEnabledFor(logging.DEBUG):
                        try:
                            body: Any = redact(response.json())
                        except ValueError:
                            body = "<non-json body suppressed>"
                        logger.debug(
                            "Introspection error body (token=%s): %r",
                            token_fingerprint(token), body,
                        )
                    return ("failure", None)

                introspection_result = response.json()

                if not introspection_result.get("active", False):
                    logger.info(
                        "Introspection reports token inactive (token=%s)",
                        token_fingerprint(token),
                    )
                    return ("inactive", None)

                client_id = introspection_result.get(
                    "client_id", introspection_result.get("azp", "unknown"),
                )

                scopes = introspection_result.get("scope", "")
                if isinstance(scopes, str):
                    scopes = scopes.split() if scopes else []
                elif not isinstance(scopes, list):
                    scopes = []

                expires_at = introspection_result.get("exp")

                access_token = AccessToken(
                    token=token,
                    client_id=client_id,
                    scopes=scopes,
                    expires_at=expires_at,
                    claims=introspection_result,
                )
                return ("active", access_token)

        except httpx.HTTPError as e:
            logger.warning(
                "HTTP error during token introspection (token=%s, reason=%s)",
                token_fingerprint(token),
                type(e).__name__,
            )
            return ("failure", None)
        except Exception as e:
            logger.warning(
                "Error during token introspection (token=%s, reason=%s)",
                token_fingerprint(token),
                type(e).__name__,
            )
            return ("failure", None)


class PATSupportingRemoteAuthProvider(RemoteAuthProvider):
    """
    Custom RemoteAuthProvider that supports both JWTs and Personal Access Tokens.
    """

    def __init__(
        self,
        token_verifier: PATAwareJWTVerifier,
        authorization_servers: list[AnyHttpUrl],
        base_url: str
    ):
        """
        Initialize the PAT-supporting auth provider.

        Args:
            token_verifier: A PATSupportingJWTVerifier instance
            authorization_servers: List of authorization server URLs
            base_url: Base URL of this MCP server
        """
        super().__init__(
            token_verifier=token_verifier,
            authorization_servers=authorization_servers,
            base_url=base_url
        )
