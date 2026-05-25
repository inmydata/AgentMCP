# Personal Access Token (PAT) Support

## Overview

This MCP server supports both JWT tokens and Personal Access Tokens (PATs) for authentication. When a PAT is detected (non-JWT format), the server automatically performs token introspection to validate it and retrieve the necessary claims.

## How It Works

1. **JWT Authentication (Default)**: When a valid JWT is provided in the `Authorization` header, it's validated directly using the JWKS from the auth server.

2. **PAT Authentication (Fallback with Caching)**: When a non-JWT token (PAT) is provided:
   - The server attempts JWT validation first
   - If JWT validation fails, it checks the introspection cache
   - **Cache Hit**: If the PAT was recently introspected, the cached result is used (no network request)
   - **Cache Miss**: If not cached or expired, it performs token introspection
   - The introspection endpoint validates the PAT and returns the token claims
   - The result is cached for future requests
   - If the token is active and valid, the request proceeds with the introspected claims

### Caching Details

- **Cache Key**: SHA-256 hash of the token (for security - full tokens aren't stored)
- **Positive TTL cap**: Active token results are cached for at most `PAT_CACHE_MAX_TTL` seconds (default **3600 s / 1 hour**), regardless of the upstream `exp` claim. The effective expiry is `min(now + PAT_CACHE_MAX_TTL, upstream_exp)`.
- **Negative cache**: Inactive (`active: false`) responses are cached for `PAT_CACHE_NEGATIVE_TTL` seconds (default **10 s**) with ±20 % jitter to avoid synchronised expiry. This prevents repeated introspection calls for invalid tokens.
- **Bounded size**: The cache holds at most `PAT_CACHE_MAX_ENTRIES` entries (default **1024**). When full, the least-recently-used entry is evicted.
- **Lazy expiry**: Entries are checked for expiry on read; a periodic sweep of all entries runs when the cache size exceeds half the maximum.
- **Memory Efficient**: Only stores hash → (AccessToken, expiry_timestamp) pairs

## Configuration

Add the following to your `.env` file:

```env
# Auth Server Configuration
INMYDATA_AUTH_SERVER=https://auth.inmydata.com
INMYDATA_MCP_HOST=mcp.inmydata.com

# Token Introspection Configuration (for PAT support)
INMYDATA_INTROSPECTION_CLIENT_ID=your_client_id_here
INMYDATA_INTROSPECTION_CLIENT_SECRET=your_client_secret_here

# ── PAT Introspection Cache ──────────────────────────────────────────────────
# Maximum TTL (seconds) for a *positive* (active) cache entry.
# The effective expiry is min(now + PAT_CACHE_MAX_TTL, upstream_exp).
# Default: 3600 (1 hour)
PAT_CACHE_MAX_TTL=3600

# TTL (seconds) for a *negative* (inactive / revoked) cache entry.
# A ±20 % jitter is applied automatically to avoid synchronised expiry.
# Default: 10
PAT_CACHE_NEGATIVE_TTL=10

# Maximum number of entries in the in-process LRU cache.
# The oldest (least-recently-used) entry is evicted when the limit is reached.
# Default: 1024
PAT_CACHE_MAX_ENTRIES=1024
```

The introspection client credentials are used to authenticate with the auth server when validating PATs.

## Usage

### With JWT
```bash
curl -H "Authorization: Bearer eyJhbGc..." https://mcp.inmydata.com/mcp
```

### With Personal Access Token
```bash
curl -H "Authorization: Bearer imd_pat_..." https://mcp.inmydata.com/mcp
```

Both methods work seamlessly - the server automatically detects the token type and handles validation appropriately.

## Implementation Details

The PAT support is implemented through two custom classes:

- **`PATSupportingJWTVerifier`**: Extends the standard `JWTVerifier` to add token introspection capability with caching
- **`PATSupportingRemoteAuthProvider`**: Uses the custom verifier with `RemoteAuthProvider`

### Introspection Flow

When introspection is performed, the server:
1. Computes SHA-256 hash of the token
2. Checks the bounded LRU cache:
   - **Positive hit**: returns the stored `AccessToken` immediately (no network request)
   - **Negative hit**: returns `None` immediately (no network request) – short-circuits repeated invalid-token probes
   - **Miss**: proceeds to the introspection endpoint
3. Sends a POST request to the introspection endpoint with the token and client credentials
4. Validates the `active` flag in the response
5. On `active: true` – extracts required fields and caches the result with effective expiry `min(now + PAT_CACHE_MAX_TTL, upstream_exp)`
6. On `active: false` / error – caches a negative entry for `PAT_CACHE_NEGATIVE_TTL` seconds (±20 % jitter)
7. Extracts required fields from the introspection result:
   - `client_id`: From `client_id` or `azp` claim (defaults to "unknown")
   - `scopes`: From `scope` claim (space-separated string or array)
   - `exp`: Token expiration timestamp
   - All other claims are stored in the `claims` dictionary
8. Creates an `AccessToken` object with the required fields

### Expected Introspection Response Format

Your introspection endpoint should return a response like:
```json
{
  "active": true,
  "client_id": "your-client-id",
  "scope": "openid profile inmydata.Developer.AI",
  "exp": 1730000000,
  "iat": 1729900000,
  "sub": "user-id",
  "imd_tenant": "tenant-name",
  "client_imd_tenant": "tenant-name"
  // ... other claims
}
```

Required fields:
- `active`: Must be `true` for the token to be accepted
- `client_id` (or `azp`): Client identifier
- `scope`: Space-separated string or array of scopes
- `exp`: Expiration timestamp (optional but recommended)

### Cache Management

- Cache entries automatically expire based on `PAT_CACHE_MAX_TTL` (positive) or `PAT_CACHE_NEGATIVE_TTL` (negative), always bounded by the token's own `exp` claim
- The positive TTL cap is `min(now + PAT_CACHE_MAX_TTL, upstream_exp)` to limit revocation lag
- Expired entries are lazily removed on lookup; a periodic sweep removes all stale entries when the cache size exceeds half the maximum
- The cache is bounded to `PAT_CACHE_MAX_ENTRIES` entries; the least-recently-used entry is evicted when the limit is reached
- No manual cache invalidation is needed

## Security Considerations

- Introspection requires valid client credentials (`INMYDATA_INTROSPECTION_CLIENT_ID` and `INMYDATA_INTROSPECTION_CLIENT_SECRET`)
- Tokens must be marked as "active" in the introspection response
- The introspection endpoint must be properly secured and only accept authenticated requests
- Failed introspection attempts are logged but don't expose sensitive information
- **Cache Security**: Full tokens are never stored in the cache - only SHA-256 hashes are used as keys
- **Positive TTL cap**: `PAT_CACHE_MAX_TTL` (default 3600 s) bounds how long a revoked token can remain valid locally
- **Negative cache**: `PAT_CACHE_NEGATIVE_TTL` (default 10 s) prevents an attacker from amplifying load on the IdP using unique invalid tokens
- **Bounded memory**: `PAT_CACHE_MAX_ENTRIES` (default 1024) prevents unbounded memory growth; LRU eviction ensures the most recent tokens are retained

## Troubleshooting

If PAT authentication isn't working:

1. Verify introspection credentials are correct
2. Check that the introspection endpoint is accessible
3. Review server logs for introspection error messages
4. Ensure the PAT is valid and active
5. Verify the auth server supports the introspection endpoint
6. Check cache TTL settings if tokens seem stale
7. Monitor cache hit/miss logs to verify caching is working
8. **Verify introspection response format**: Ensure the response includes:
   - `active: true`
   - `client_id` (or `azp`)
   - `scope` (space-separated string or array)
   - `exp` (recommended for proper cache expiry)
  
Error: "Invalid Target"
   - Did you specify the correct URL (e.g. https://mcp.inmydata.com/mcp)?
   - Ensure '/mcp' is on the end of the URL
   - Try with and without a trailing slash.  
   - Don't try to pass query string parameters

### Performance Optimization

For long-lived PATs in high-traffic scenarios:
- `PAT_CACHE_MAX_TTL` controls the positive cache duration (default 3600 s = 1 hour)
- Monitor cache effectiveness through log messages ("Using cached introspection result")
- Tune `PAT_CACHE_MAX_ENTRIES` to trade memory for reduced introspection round-trips
- Monitor negative-cache hits ("Token previously found inactive") to detect attack patterns

