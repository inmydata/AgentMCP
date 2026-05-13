"""
Custom RemoteAuthProvider that supports both JWTs and Personal Access Tokens (PATs).
When a PAT is detected (non-JWT), performs token introspection to get a valid JWT.
Caches introspection results to avoid repeated requests for the same PAT.
"""
import httpx
import os
import time
from typing import Optional, Dict, Tuple
from fastmcp.server.auth import RemoteAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier, AccessToken
from pydantic import AnyHttpUrl


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
        cache_ttl_seconds: int = 300  # Default 5 minutes cache
    ):
        super().__init__(jwks_uri=jwks_uri, issuer=issuer, audience=audience)
        self.introspection_endpoint = introspection_endpoint
        self.client_id = client_id
        self.client_secret = client_secret
        self.cache_ttl_seconds = cache_ttl_seconds
        
        # Cache format: {token_hash: (AccessToken, expiry_timestamp)}
        self._introspection_cache: Dict[str, Tuple[AccessToken, float]] = {}
    
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
            # Check cache first
            cached_token = self._get_cached_token(token)
            if cached_token is not None:
                print("Using cached introspection result")
                return cached_token
            
            # Cache miss, perform introspection
            introspected_token = await self._introspect_token(token)
            if introspected_token is not None:
                self._cache_token(token, introspected_token)
            return introspected_token
        
        return None
    
    def _get_cached_token(self, token: str) -> Optional[AccessToken]:
        """
        Retrieve a cached introspection result if not expired.
        
        Args:
            token: The token to look up
            
        Returns:
            Cached AccessToken if valid and not expired, None otherwise
        """
        # Use hash of token as cache key to avoid storing full token in memory
        import hashlib
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        
        if token_hash in self._introspection_cache:
            cached_token, expiry = self._introspection_cache[token_hash]
            
            # Check if cache entry has expired
            if time.time() < expiry:
                return cached_token
            else:
                # Remove expired entry
                del self._introspection_cache[token_hash]
        
        return None
    
    def _cache_token(self, token: str, access_token: AccessToken) -> None:
        """
        Cache an introspection result.
        
        Args:
            token: The original token
            access_token: The AccessToken to cache
        """
        import hashlib
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        
        # Determine expiry time - use token's exp claim if available, otherwise use cache TTL
        expiry_timestamp = time.time() + self.cache_ttl_seconds
        
        if "exp" in access_token.claims:
            # Use the token's expiration if available
            token_exp = access_token.claims["exp"]
            if isinstance(token_exp, (int, float)):
                # Don't cache beyond the token's actual expiration
                expiry_timestamp = min(expiry_timestamp, token_exp)
        
        self._introspection_cache[token_hash] = (access_token, expiry_timestamp)
        
        # Simple cache cleanup: remove expired entries periodically
        self._cleanup_expired_cache()
    
    def _cleanup_expired_cache(self) -> None:
        """
        Remove expired entries from the cache.
        Called periodically during cache updates.
        """
        current_time = time.time()
        expired_keys = [
            key for key, (_, expiry) in self._introspection_cache.items()
            if current_time >= expiry
        ]
        
        for key in expired_keys:
            del self._introspection_cache[key]
        
        if expired_keys:
            print(f"Cleaned up {len(expired_keys)} expired cache entries")
    
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
