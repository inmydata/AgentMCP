"""
Custom RemoteAuthProvider that supports both JWTs and Personal Access Tokens (PATs).
When a PAT is detected (non-JWT), performs token introspection to get a valid JWT.
Caches introspection results to avoid repeated requests for the same PAT.
"""
import collections
import hashlib
import httpx
import os
import random
import time
from typing import Optional, Tuple
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier, AccessToken
from pydantic import AnyHttpUrl


class _BoundedTTLCache:
    """
    A bounded LRU cache with TTL support for PAT introspection results.

    Supports positive (active token) and negative (inactive token) entries.
    Uses an OrderedDict for O(1) LRU eviction.  Expired entries are lazily
    removed on read; a periodic sweep runs when the cache size exceeds
    half the configured maximum.

    Cache entry types
    -----------------
    Positive entry : (AccessToken, expiry_timestamp)
    Negative entry : (_NEGATIVE_SENTINEL, expiry_timestamp)
    """

    _NEGATIVE_SENTINEL = object()

    def __init__(self, max_entries: int, max_positive_ttl: int, negative_ttl: int) -> None:
        self._max_entries = max_entries
        self._max_positive_ttl = max_positive_ttl
        self._negative_ttl = negative_ttl
        self._cache: collections.OrderedDict = collections.OrderedDict()
        self._sweep_threshold = max(1, max_entries // 2)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get(self, key: str) -> Tuple[str, Optional[AccessToken]]:
        """
        Look up *key* in the cache.

        Returns one of:
          ``("hit_positive", access_token)`` – valid, active cached result.
          ``("hit_negative", None)``          – cached negative (inactive) result.
          ``("miss", None)``                  – not found or expired.
        """
        if key not in self._cache:
            return ("miss", None)

        value, expiry = self._cache[key]

        if time.time() >= expiry:
            del self._cache[key]
            return ("miss", None)

        # Promote to most-recently-used position
        self._cache.move_to_end(key)

        if value is self._NEGATIVE_SENTINEL:
            return ("hit_negative", None)
        return ("hit_positive", value)

    def set_positive(self, key: str, access_token: AccessToken, upstream_exp: Optional[float]) -> None:
        """
        Cache a positive introspection result.

        The effective TTL is ``min(now + max_positive_ttl, upstream_exp)`` so
        that the local cache never outlives the token's actual expiry and is
        also capped to prevent revocation lag on far-future ``exp`` values.
        """
        now = time.time()
        effective_exp = now + self._max_positive_ttl
        if upstream_exp is not None:
            effective_exp = min(effective_exp, upstream_exp)

        self._cache[key] = (access_token, effective_exp)
        self._cache.move_to_end(key)
        self._evict_if_needed()

    def set_negative(self, key: str) -> None:
        """
        Cache a negative introspection result with a short TTL plus or minus 20 %
        random jitter to avoid synchronised expiry across entries.
        """
        jitter = self._negative_ttl * 0.4 * (random.random() - 0.5)
        expiry = time.time() + self._negative_ttl + jitter
        self._cache[key] = (self._NEGATIVE_SENTINEL, expiry)
        self._cache.move_to_end(key)
        self._evict_if_needed()

    def __len__(self) -> int:
        return len(self._cache)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evict_if_needed(self) -> None:
        """Enforce max-size via LRU eviction, then optionally sweep expired entries."""
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)

        if len(self._cache) >= self._sweep_threshold:
            self._sweep_expired()

    def _sweep_expired(self) -> None:
        """Remove all expired entries in a single pass."""
        now = time.time()
        expired_keys = [k for k, (_, exp) in self._cache.items() if now >= exp]
        for k in expired_keys:
            del self._cache[k]
        if expired_keys:
            print(f"Cache sweep: removed {len(expired_keys)} expired entries")


class PATAwareJWTVerifier(JWTVerifier):
    """
    Custom JWT verifier that handles both JWTs and Personal Access Tokens.
    If the token is not a valid JWT, it performs token introspection.
    Caches introspection results to avoid repeated requests.
    """

    def __init__(
        self,
        jwks_uri: str,
        issuer: str,
        audience: str,
        introspection_endpoint: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        cache_ttl_seconds: int = 300,  # kept for backward compatibility; see PAT_CACHE_MAX_TTL
    ):
        super().__init__(jwks_uri=jwks_uri, issuer=issuer, audience=audience)
        self.introspection_endpoint = introspection_endpoint
        self.client_id = client_id
        self.client_secret = client_secret
        self.cache_ttl_seconds = cache_ttl_seconds

        max_positive_ttl = int(os.environ.get("PAT_CACHE_MAX_TTL", "3600"))
        negative_ttl = int(os.environ.get("PAT_CACHE_NEGATIVE_TTL", "10"))
        max_entries = int(os.environ.get("PAT_CACHE_MAX_ENTRIES", "1024"))

        self._introspection_cache = _BoundedTTLCache(
            max_entries=max_entries,
            max_positive_ttl=max_positive_ttl,
            negative_ttl=negative_ttl,
        )

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        """
        Verify a token. First tries JWT verification, then falls back to introspection.
        Caches introspection results to avoid repeated requests.

        Args:
            token: The bearer token to verify (JWT or PAT)

        Returns:
            AccessToken if valid, None otherwise
        """
        # First, try standard JWT verification
        try:
            access_token = await super().verify_token(token)
            if access_token is not None:
                return access_token
        except Exception as e:
            # JWT verification failed, might be a PAT
            print(f"JWT verification failed: {e}. Attempting token introspection...")

        # If JWT verification failed and we have introspection configured, try introspection
        if self.introspection_endpoint:
            # SHA-256 is used as a cache-key fingerprint only (not password storage).
            # The raw token is never stored; only its hash is used as the dict key.
            token_hash = hashlib.sha256(token.encode()).hexdigest()  # nosec B324
            cache_status, cached_value = self._introspection_cache.get(token_hash)

            if cache_status == "hit_positive":
                print("Using cached introspection result")
                return cached_value

            if cache_status == "hit_negative":
                print("Token previously found inactive (negative cache hit); skipping introspection")
                return None

            # Cache miss – perform introspection
            introspected_token = await self._introspect_token(token)
            if introspected_token is not None:
                upstream_exp = introspected_token.claims.get("exp")
                if not isinstance(upstream_exp, (int, float)):
                    upstream_exp = None
                self._introspection_cache.set_positive(token_hash, introspected_token, upstream_exp)
            else:
                self._introspection_cache.set_negative(token_hash)
            return introspected_token

        return None
    
    async def _introspect_token(self, token: str) -> Optional[AccessToken]:
        """
        Perform token introspection to validate a PAT and get JWT claims.
        
        Args:
            token: The PAT to introspect
            
        Returns:
            AccessToken if introspection succeeds and token is active, None otherwise
        """
        if not self.introspection_endpoint:
            print("No introspection endpoint configured")
            return None
        
        try:
            async with httpx.AsyncClient() as client:
                # Prepare introspection request
                data = {
                    "token": token,
                }
                
                # Add client credentials if configured
                auth = None
                if self.client_id and self.client_secret:
                    auth = (self.client_id, self.client_secret)
                
                headers = {
                    "Content-Type": "application/x-www-form-urlencoded"
                }
                
                # Make introspection request
                response = await client.post(
                    self.introspection_endpoint,
                    data=data,
                    headers=headers,
                    auth=auth,
                    timeout=10.0
                )
                
                if response.status_code != 200:
                    print(f"Introspection failed with status {response.status_code}: {response.text}")
                    return None
                
                introspection_result = response.json()
                
                # Check if token is active
                if not introspection_result.get("active", False):
                    print("Token is not active according to introspection")
                    return None
                
                # Convert introspection result to AccessToken format
                # Extract required fields from introspection response
                client_id = introspection_result.get("client_id", introspection_result.get("azp", "unknown"))
                
                # Extract scopes - handle both space-separated string and array formats
                scopes = introspection_result.get("scope", "")
                if isinstance(scopes, str):
                    scopes = scopes.split() if scopes else []
                elif not isinstance(scopes, list):
                    scopes = []
                
                # Extract expiration
                expires_at = introspection_result.get("exp")
                
                # Create AccessToken with required fields
                access_token = AccessToken(
                    token=token,
                    client_id=client_id,
                    scopes=scopes,
                    expires_at=expires_at,
                    claims=introspection_result
                )
                
                return access_token
                
        except httpx.HTTPError as e:
            print(f"HTTP error during token introspection: {e}")
            return None
        except Exception as e:
            print(f"Error during token introspection: {e}")
            return None


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
