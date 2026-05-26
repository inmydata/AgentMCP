# Personal Access Token (PAT) Support

## Overview

This MCP server supports both JWT tokens and Personal Access Tokens (PATs) for authentication. When a PAT is detected (non-JWT format), the server automatically performs token introspection to validate it and retrieve the necessary claims.

## How It Works

1. **JWT Authentication (Default)**: When a valid JWT is provided in the `Authorization` header, it's validated directly using the JWKS from the auth server.

2. **PAT Authentication (Fallback with Caching)**: When a non-JWT token (PAT) is provided:
   - The server attempts JWT validation first
   - If JWT validation fails, it checks the introspection cache
   - **Positive Cache Hit**: If the PAT was recently introspected and active, the cached result is used (no network request)
   - **Negative Cache Hit**: If the PAT was recently introspected and reported `active: false`, the request is rejected immediately without re-contacting the introspection endpoint
   - **Cache Miss**: If not cached or expired, it performs token introspection
   - The introspection endpoint validates the PAT and returns the token claims
   - Active results are cached briefly; inactive results are cached briefly with jitter
   - Transient introspection failures (network/HTTP errors) are **not** cached
   - If the token is active and valid, the request proceeds with the introspected claims

### Caching Details

- **Cache Key**: SHA-256 hash of the token (full tokens are never stored)
- **Positive Cache TTL**: Capped at `INMYDATA_PAT_CACHE_MAX_TTL` seconds (default: **60**), regardless of any far-future `exp` returned by the IdP. If the upstream `exp` is sooner, that wins.
- **Negative Cache TTL**: `INMYDATA_PAT_CACHE_NEGATIVE_TTL` seconds (default: **10**) with ±20% jitter, applied to `active: false` introspection responses. This prevents an attacker from using invalid PATs to amplify load onto the introspection endpoint.
- **Bounded Size**: At most `INMYDATA_PAT_CACHE_MAX_ENTRIES` entries (default: **1024**), evicted strict LRU.
- **Lazy Expiry**: Expired entries are dropped on read; there is no eager periodic sweep.
- **Failure Handling**: HTTP errors, non-200 responses and malformed bodies are reported as transient failures and are **not** cached — a brief IdP outage will not poison the cache.

### Revocation Lag

Because positive entries are capped at `INMYDATA_PAT_CACHE_MAX_TTL`, revoking a PAT at the IdP will be reflected by this server within that many seconds (default: 60). Set the cap lower if you need faster revocation; raise it to reduce introspection load (at the cost of revocation lag).

## Configuration

Add the following to your `.env` file:

```env
# Auth Server Configuration
INMYDATA_AUTH_SERVER=https://auth.inmydata.com
INMYDATA_MCP_HOST=mcp.inmydata.com

# Token Introspection Configuration (for PAT support)
INMYDATA_INTROSPECTION_CLIENT_ID=your_client_id_here
INMYDATA_INTROSPECTION_CLIENT_SECRET=your_client_secret_here

# PAT introspection cache (all optional — defaults shown).
# Positive (active) entries are capped at this many seconds, regardless of
# the upstream `exp`. Lower = faster revocation, higher = fewer IdP calls.
INMYDATA_PAT_CACHE_MAX_TTL=60

# Negative (active: false) entries are kept this long, with ±20% jitter, so
# a flood of invalid PATs cannot reflect load onto the introspection endpoint.
INMYDATA_PAT_CACHE_NEGATIVE_TTL=10

# LRU bound on total cache entries.
INMYDATA_PAT_CACHE_MAX_ENTRIES=1024

# DEPRECATED: honored as a fallback for INMYDATA_PAT_CACHE_MAX_TTL only.
# Will be removed in a future release.
# INMYDATA_TOKEN_CACHE_TTL=300
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
2. Checks if hash exists in cache and isn't expired
3. If cached, returns the stored `AccessToken` immediately
4. If not cached, sends a POST request to the introspection endpoint
5. Includes the token and client credentials
6. Validates the `active` flag in the response
7. Extracts required fields from the introspection result:
   - `client_id`: From `client_id` or `azp` claim (defaults to "unknown")
   - `scopes`: From `scope` claim (space-separated string or array)
   - `exp`: Token expiration timestamp
   - All other claims are stored in the `claims` dictionary
8. Creates an `AccessToken` object with the required fields
9. Caches the result using token hash as key
10. Sets expiry to `min(now + INMYDATA_PAT_CACHE_MAX_TTL, upstream_exp)`

If the introspection response is `active: false`, the token hash is recorded in a short negative cache (with jittered TTL) so a repeat of the same invalid token does not trigger another introspection request. Transient HTTP/network failures are not cached.

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

- Positive entries expire at `min(now + INMYDATA_PAT_CACHE_MAX_TTL, upstream_exp)`
- Negative entries expire after `INMYDATA_PAT_CACHE_NEGATIVE_TTL` ± 20% jitter
- Total entries are bounded by `INMYDATA_PAT_CACHE_MAX_ENTRIES` (strict LRU eviction)
- Expired entries are dropped lazily on read — there is no eager periodic sweep
- Transient introspection failures are not cached, so a brief IdP outage does not block valid PATs once the IdP recovers

## Security Considerations

- Introspection requires valid client credentials (`INMYDATA_INTROSPECTION_CLIENT_ID` and `INMYDATA_INTROSPECTION_CLIENT_SECRET`)
- Tokens must be marked as "active" in the introspection response
- The introspection endpoint must be properly secured and only accept authenticated requests
- Failed introspection attempts are logged but don't expose sensitive information
- **Cache Security**: Full tokens are never stored in the cache - only SHA-256 hashes are used as keys
- **Positive Cache TTL**: Capped at `INMYDATA_PAT_CACHE_MAX_TTL` (default 60 seconds). This is the maximum window between a PAT being revoked at the IdP and this server treating it as invalid.
- **Negative Cache**: Definitive `active: false` responses are cached briefly (default 10 seconds, jittered) so an attacker cannot use the MCP server as a reflection amplifier against the introspection endpoint.
- **Bounded Memory**: The cache is hard-capped at `INMYDATA_PAT_CACHE_MAX_ENTRIES` entries and evicted strict LRU.
- **Token Expiry**: Cache entries are also capped at the token's upstream `exp` if that is sooner than the configured TTL cap.

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
- Increase `INMYDATA_PAT_CACHE_MAX_TTL` to reduce introspection requests (at the cost of slower revocation propagation)
- Increase `INMYDATA_PAT_CACHE_MAX_ENTRIES` if your active PAT population is larger than 1024
- Monitor cache effectiveness through DEBUG log messages ("PAT introspection cache hit")
- Balance cache TTL against the need for timely revocation detection

