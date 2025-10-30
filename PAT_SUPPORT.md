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
- **Cache Duration**: Configurable via `INMYDATA_TOKEN_CACHE_TTL` (default: 300 seconds / 5 minutes)
- **Cache Expiry**: Respects the token's `exp` claim if present, won't cache beyond actual expiration
- **Automatic Cleanup**: Expired cache entries are automatically removed during cache operations
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

# Token Cache TTL (in seconds) - how long to cache introspected PAT results
# Default: 300 seconds (5 minutes)
# Increase for better performance if PATs are long-lived
INMYDATA_TOKEN_CACHE_TTL=300
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
10. Sets expiry based on token's `exp` claim or configured TTL (whichever is sooner)

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

- Cache entries automatically expire based on TTL or token expiration
- Expired entries are cleaned up during cache operations
- No manual cache invalidation needed
- Each PAT is only introspected once per cache TTL period

## Security Considerations

- Introspection requires valid client credentials (`INMYDATA_INTROSPECTION_CLIENT_ID` and `INMYDATA_INTROSPECTION_CLIENT_SECRET`)
- Tokens must be marked as "active" in the introspection response
- The introspection endpoint must be properly secured and only accept authenticated requests
- Failed introspection attempts are logged but don't expose sensitive information
- **Cache Security**: Full tokens are never stored in the cache - only SHA-256 hashes are used as keys
- **Cache TTL**: Keep cache TTL reasonable to balance performance vs. security (default 5 minutes)
- **Token Expiry**: Cache entries respect the token's actual expiration time

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

### Performance Optimization

For long-lived PATs in high-traffic scenarios:
- Increase `INMYDATA_TOKEN_CACHE_TTL` to reduce introspection requests
- Monitor cache effectiveness through log messages ("Using cached introspection result")
- Balance cache TTL against the need for timely revocation detection
