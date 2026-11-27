# inmydata MCP Server

## Overview

This is a Python web application that exposes the [inmydata agents SDK](https://github.com/inmydata/agents) as a Model Context Protocol (MCP) Server. The MCP server enables AI agents to access inmydata's powerful data querying capabilities through a standardized interface.

## Project Architecture

### Technology Stack

- **Python 3.11** - Runtime environment
- **FastMCP** - High-level MCP server framework  
- **inmydata SDK** - Provides structured data, conversational data, and calendar tools
- **pandas** - Data handling for SDK responses
- **python-dotenv** - Environment variable management

### MCP Tools Exposed

#### Data Query Tools

- `get_rows_fast` - **FAST PATH (recommended)** - Query data with specific fields and simple filters. Returns clean JSON format optimized for LLMs.
- `get_top_n_fast` - **FAST PATH for rankings** - Get top/bottom N results by a metric. Much faster than conversational queries.
- `get_answer_slow` - **SLOW/EXPENSIVE (fallback)** - Natural language queries using conversational AI (supports streaming progress updates via MCP progress notifications)
- `get_schema` - Get available schema with AI-enhanced dashboard hints and field categorization
- `query_results_fast` - Queries results with SQL fetched with the get_rows_fast and get_top_n_fast tools and stored in a DuckDB database

#### Calendar Tools

- `get_financial_periods` - Get all financial periods (year, quarter, month, week) for a date
- `get_calendar_period_date_range` - Get start/end dates for a calendar period. **Now supports smart defaults** - call with no parameters to get current month's date range

#### Knowledge Base Tool

- `agentic_rag_query` - Query a tenant's knowledge base in Agentic RAG with a natural-language question. Returns a grounded answer plus source chunk references. See [Agentic RAG query tool](#agentic-rag-query-tool) below.

## Configuration

Required environment variables (see `.env.example`):

- `INMYDATA_API_KEY` - Your inmydata API key
- `INMYDATA_TENANT` - Your tenant name
- `INMYDATA_CALENDAR` - Your calendar name
- `INMYDATA_USER` (optional) - User for chart events (default: mcp-agent)
- `INMYDATA_SESSION_ID` (optional) - Session ID for chart events (default: mcp-session)
- `MCP_DUCKDB_LOCATION` - Location to use for the DuckDB database
- `MCP_DEBUG` - For local use only. 0 (default) has no effect. 1 enables debugging to be connected from Visual Studio Code

### Remote Server Additional Configuration

- `INMYDATA_USE_OAUTH` - Set to `true` to enable OAuth authentication. Deployed servers must set this. If it is `false` or unset, the server uses legacy API key authentication and refuses to start unless `INMYDATA_ALLOW_LEGACY_AUTH` is also `true`
- `INMYDATA_ALLOW_LEGACY_AUTH` (local testing only) - Set to `true` to let the server start with OAuth off. Never set it in a deployed environment; see [Legacy API Key Authentication](#legacy-api-key-authentication-inmydata_use_oauthfalse-local-testing-only)
- `INMYDATA_MCP_HOST` (optional) - MCP server host (default: mcp.inmydata.ai)
- `INMYDATA_AUTH_SERVER` (optional) - OAuth authorization server URL (default: https://auth.inmydata.com)
- `INMYDATA_SERVER` (optional) - inmydata server (default: inmydata.com)
- `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` (optional, local development only) - CA bundle for outgoing HTTPS calls. Needed when the auth server or the inmydata API runs locally with an mkcert certificate; see [Using a local auth server](#using-a-local-auth-server-mkcert-certificates)

## Usage

### Local Server (stdio transport)

For local MCP client connections:

```bash
python server.py
```

The server communicates via standard input/output following the MCP protocol. Environment variables are read from `.env` file.

Note: Both servers use the `mcp_utils` helper class which handles all SDK interactions,
including proper JSON serialization of responses. This ensures consistent handling
of SDK objects (dates, calendar periods, etc.) across both local and remote modes.

### Remote Server (SSE/HTTP transport)

For remote deployment on AWS, Google Cloud, Azure, etc:

```bash
python server_remote.py sse 8000
# or
python server_remote.py streamable-http 8000
```

The remote server:

- Exposes HTTP endpoints for remote MCP client connections
- Accepts inmydata credentials securely via HTTP headers (not environment variables)
- Supports both SSE and Streamable HTTP transports
- Can be deployed on any cloud platform (AWS, GCP, Azure, Render, Railway, etc.)

#### Authentication Options for Remote Server

The remote server supports two authentication modes, controlled by the `INMYDATA_USE_OAUTH` environment variable:

##### OAuth Authentication (INMYDATA_USE_OAUTH=true)

When OAuth is enabled, the server uses bearer token authentication:

- `Authorization: Bearer <token>` - OAuth access token
- `x-inmydata-tenant` (optional) - Overrides tenant extracted from token
- `x-inmydata-calendar` (optional) - Calendar name (default: Default)
- `x-inmydata-user` (optional) - User for events (default: mcp-agent)
- `x-inmydata-session-id` (optional) - Session ID (default: mcp-session)
- `x-inmydata-server` (optional) - Server override

The tenant is automatically extracted from the token's `client_imd_tenant` or `imd_tenant` claim.

##### Using a local auth server (mkcert certificates)

If `INMYDATA_AUTH_SERVER` or the inmydata API points at a server running on your own machine (for example `auth-dev.inmydata.com` or `dev.inmydata.com` mapped to `127.0.0.1` in your `hosts` file), that server will usually use a certificate made by [mkcert](https://github.com/FiloSottile/mkcert).

`mkcert -install` makes Windows and your browsers trust that certificate, but Python does not. Python's HTTP libraries check certificates against their own `certifi` bundle, which does not contain the mkcert root CA. Without the fix below:

- `POST /connect/token` returns **500**, because the token proxy cannot connect to the auth server (`CERTIFICATE_VERIFY_FAILED` in the server log). MCP clients such as ChatGPT fail to sign in.
- Token checks on `/mcp` fail in the same way, because the signing keys (JWKS) are also downloaded from the auth server.
- Tool calls fail with `requests.exceptions.SSLError ... CERTIFICATE_VERIFY_FAILED`, because the inmydata SDK cannot connect to the local API.

To fix it, give Python a CA bundle that contains both the normal `certifi` certificates and your mkcert root CA:

```bash
# Git Bash; run from the repository root
mkdir -p "$USERPROFILE/.certs"
B="$USERPROFILE/.certs/certifi-plus-mkcert.pem"
cat "$(.venv/Scripts/python -c 'import certifi; print(certifi.where())')" > "$B"
echo >> "$B"
cat "$(mkcert -CAROOT)/rootCA.pem" >> "$B"
```

Then add both of these to `.env`, pointing at the same file:

```
SSL_CERT_FILE=C:\Users\<you>\.certs\certifi-plus-mkcert.pem
REQUESTS_CA_BUNDLE=C:\Users\<you>\.certs\certifi-plus-mkcert.pem
```

Two settings are needed because two HTTP libraries are in use. `httpx` (the token proxy and JWKS downloads) reads `SSL_CERT_FILE`. `requests` (the inmydata SDK) ignores `SSL_CERT_FILE` and reads `REQUESTS_CA_BUNDLE`.

Notes:

- The bundle must contain both sets of certificates. A file with only the mkcert root CA would stop calls to real servers, such as `auth.inmydata.com`, from working.
- Keep the file outside the repository.
- It is a copy, so rebuild it if you upgrade `certifi` or regenerate your mkcert CA.
- This is for local development only. Do not set either setting in deployed environments.

##### Legacy API Key Authentication (INMYDATA_USE_OAUTH=false, local testing only)

Do not deploy the server in this mode. Without OAuth, each caller chooses the tenant and the inmydata server, and can make the server use an API key from its own environment (see "Environment Variable Lookup" below). The server refuses to start this way unless `INMYDATA_ALLOW_LEGACY_AUTH=true` is also set.

When OAuth is disabled, the server uses traditional API key authentication:

**Headers:**
- `x-inmydata-api-key` - Your inmydata API key
- `x-inmydata-tenant` - Your tenant name
- `x-inmydata-calendar` (optional) - Calendar name (default: Default)
- `x-inmydata-user` (optional) - User for events (default: mcp-agent)
- `x-inmydata-session-id` (optional) - Session ID (default: mcp-session)
- `x-inmydata-server` (optional) - Server override

**Query Parameters (takes precedence over headers):**
- `?tenant=your-tenant-name` - Overrides `x-inmydata-tenant` header if provided

**Environment Variable Lookup:**
- API key can be auto-detected from environment variable `{TENANT}_API_KEY` (e.g., `ACME_API_KEY` for tenant "acme")
- Falls back to `x-inmydata-api-key` header if env var not found

See `deployment-guide.md` for detailed deployment instructions.

### Claude Desktop (stdio) integration

Claude Desktop can run local tools over stdio.

Steps:
Enter the following in C:\Users\\\[USERNAME]\AppData\Roaming\Claude\claude_desktop_config.json

```json
{
  "mcpServers": {
    "inmydata": {
      "command": "[PATH TO PYTHON EXECUTABLE]\\python.exe",
      "args": [
        "[PATH TO MCP SERVER SRC]\\server.py"
      ],
      "env": {
        "MCP_DEBUG":"0",
        "INMYDATA_API_KEY":"[API-KEY]",
        "INMYDATA_TENANT": "[TENANT]",
        "INMYDATA_CALENDAR": "[CALENDAR]",
        "INMYDATA_USER": "[INMYDATA-USER]",
        "INMYDATA_SESSION_ID": "[SESSION-ID]"
      }
    }
  }
}
```

## Agentic RAG query tool

The `agentic_rag_query` tool lets an LLM query a tenant's knowledge base in Agentic RAG through this server. The MCP server holds a platform service key (`psk_live_...`), provisions or mints a per-tenant API key (`rak_live_...`) on first use, and caches it.

### Environment variables

| Var | Required | Purpose |
|---|---|---|
| `AGENTIC_RAG_BASE_URL` | Yes | Base URL of the Agentic RAG service, e.g. `https://rag.example.com`. |
| `AGENTIC_RAG_PSK` | Yes | Platform service key. Never logged or surfaced to the LLM. |
| `AGENTIC_RAG_DEFAULT_EXTERNAL_ID` | No | **Non-OAuth only.** If set, `external_id` is dropped from the tool schema. Ignored (with a warning) when `INMYDATA_USE_OAUTH=true`. |
| `AGENTIC_RAG_TENANT_PREFIX` | No | **OAuth only.** Prepended to the JWT tenant claim when forming the `external_id`. Default: empty (1:1 mapping). Ignored when OAuth is off. |
| `AGENTIC_RAG_CACHE_FILE` | No | Path to the persisted key cache. Default: `./agentic_rag_cache.json`. Written with mode `0600` on POSIX. |

### Tenant binding

- **OAuth mode (`INMYDATA_USE_OAUTH=true`)**: `external_id` is derived from the same JWT claim (`client_imd_tenant` / `imd_tenant`) the other tools use, optionally prefixed by `AGENTIC_RAG_TENANT_PREFIX`. The LLM cannot pass or override it.
- **STDIO / legacy mode**: if `AGENTIC_RAG_DEFAULT_EXTERNAL_ID` is set, the LLM does not see the parameter; otherwise the LLM must pass `external_id`.

### Cache file security

The cache file contains plaintext tenant API keys so the server does not burn one of the 20-active-keys-per-tenant slots on every restart. On POSIX it is created with mode `0600`. On Windows there is no equivalent automatic ACL — place the file inside a directory that is ACL-restricted to the service account. The default path (`./agentic_rag_cache.json`) and its `.tmp` / `.corrupt.*` siblings are listed in `.gitignore`.

If the file is deleted or a new replica starts without access to it, the first call per tenant re-mints a key. Over time that can exhaust the 20-key limit; on exhaustion the tool returns an operator-actionable error and an admin must revoke stale keys via the admin API.

## Testing

```bash
uv sync --extra test
uv run pytest
```

This runs the unit tests, which replace the inmydata API with fakes.

### Live integration tests

`tests/integration/` calls the API through both MCP servers: `server.py` over stdio, and `server_remote.py` over streamable HTTP. The tests are skipped unless you pass `--integration`. Copy `.env.test.example` to `.env.test` (git-ignored) and configure the separate local and test settings and API keys:

- **Local (default):** Set `LOCAL_INMYDATA_API_KEY` to a key for the dev tenant. Start the local API and add `127.0.0.1 dev.inmydata.com` to your hosts file. The tests refuse to run if any resolved address is not loopback.
  Run `uv run pytest --integration -m integration`.
- **Test server:** Set `TEST_INMYDATA_API_KEY` to a key for the test tenant. Run `uv run pytest --integration -m integration --integration-target=test`. Test mode only accepts `test.test-inmydata.com` and refuses loopback addresses.

Both sets of settings can remain in `.env.test`; never commit it. Existing unprefixed `INMYDATA_*` settings still work for local mode if no `LOCAL_INMYDATA_*` settings are present. Both modes use the same fixed dev dataset and calendar assertions. If the test server has different data, those assertions may fail.

The local site's certificate is trusted by Windows but not by Python's own certificate list. The servers are started through `tests/integration/_launch_server.py`, which uses `truststore` so that Python checks certificates against the Windows store. Certificate checking stays on.

`tests/integration/test_error_reporting.py` checks the exact error messages for common mistakes (wrong subject, field or calendar name, bad API key, badly formed date, SQL mistakes). Like the data tests, it uses the `Inmystore Sales` subject, which both the local dev tenant and the test tenant have.

Every tool error includes a correlation ID. The full detail is in the server's log under that ID. For the remote server in these tests, the log is under pytest's temporary folder: `%TEMP%\pytest-of-<your user>\pytest-current\mcp-server-cwd0\server_remote.log` on Windows.

## Error reporting

Tool errors are JSON: `{"error": "<message>", "correlation_id": "<id>"}`. The message depends on the cause (see `errors.py`):

| Cause | Message |
|---|---|
| Missing or invalid tool argument | A specific message from the tool, for example `subject parameter is required` (no correlation ID) |
| inmydata API 400, 404, 409 and other 4xx | `The inmydata API rejected the request (HTTP <status>): <the API's message>`, without the request URL, on one line, at most 300 characters. For `get_rows_fast` / `get_top_n_fast` it adds a hint to call `get_schema`. |
| inmydata API 401 | Fixed text: the credentials were not accepted |
| inmydata API 403 | Fixed text: the subject may not exist, may not be enabled for API access, or the user may lack access |
| inmydata API 5xx | Fixed text, plus the platform's support reference if the response has one. The response body is not passed on. |
| Cannot connect, or no response in time | Fixed text |
| `query_results_fast`: mistake in the caller's SQL (syntax, unknown column or table, bad conversion) | Fixed text plus the column names of `my_table`, which the caller already has. DuckDB's own message stays in the log. |
| `query_results_fast`: malformed `instance_id` | Fixed text saying the `instance_id` is not in the expected format and to use it exactly as `get_rows_fast` / `get_top_n_fast` returned it. The value sent is not repeated back. |
| `query_results_fast`: blocked SQL statement (`ATTACH`, `COPY`, `SET`, ...) | Fixed text saying this type of statement is not allowed and to query `my_table`. Does not name the keyword that was blocked. |
| `query_results_fast`: no tenant set on the server | `invalid request` |
| `query_results_fast`: `instance_id` not found, or belongs to another tenant | Fixed text saying to run `get_rows_fast` / `get_top_n_fast` again. The same in both cases. |
| Anything else, including a bug in this server or a DuckDB engine failure | `internal error` |

Only API messages from 4xx responses are passed on, because they describe the caller's own request. Other exception text stays in the log, as required by GHSA-xv3j-j949-g8wx.

## Logging

### How logging works

All of the server's own diagnostic output goes through one Python logger called `agentmcp`, set up in `mcp_logging.py`. The server code does not use `print()`, apart from a few debugger messages that `server.py` writes to stderr. A test (`tests/test_logging_safety.py`) fails if `print(` is added to a server module, or if a log call puts something that looks like a credential (`api_key`, `access_token`, `Bearer `, `Authorization:`) into its message.

- **Where logs go:** standard error (stderr). In Docker that is `docker logs`; on AWS ECS it is CloudWatch Logs.
- **Line format:** `<date and time> <level> <logger name>: <message>`, for example
  `2026-11-27 10:15:02,123 INFO agentmcp: get_rows called (subject=Inmystore Sales, fields=3, where_terms=1)`.
  The agentic RAG tool logs under `agentmcp.agentic_rag`.
- **Log level:** set with `AGENTMCP_LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING` or `ERROR`; default `INFO`). Keep production at `INFO`. `DEBUG` adds the full filters and SQL that callers sent, and redacted introspection error responses.
- **Values from the request** on the per-request lines (tenant, client ID, user agent, the RPC method name and so on) are escaped before they are logged, so a newline sent by a caller cannot start a fake log line.

### What is never logged

These rules come from security advisory GHSA-3x42-7cvw-r9jv:

- **Tokens and API keys.** Where a log line needs to say which credential was used, it shows `token=<8 characters>` instead. This is a *fingerprint*: the first 8 hexadecimal characters of the SHA-256 hash of the credential (see `token_fingerprint` in `mcp_logging.py`). The credential is whatever the caller sent in the `Authorization` header, minus the `Bearer ` prefix: an OAuth access token (JWT), a Personal Access Token (PAT), or, in legacy mode, an inmydata API key.
  - The fingerprint cannot be turned back into the token, so it is safe to share.
  - The same token always gives the same fingerprint, so you can use it to find every line for one caller's token.
  - If you have a token, you can work out its fingerprint yourself and search for it:
    `python -c "import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:8])" <token>`
  - `token=-` means no token was sent.
- **Credential fields in data structures.** Before a structure (such as an introspection error response) is logged, `redact()` replaces any field whose name contains `token`, `secret`, `password`, `authorization` or `api_key` with `***`. These structures are only logged at `DEBUG`.

### What to expect in the logs

**One line per request to `/mcp`** (remote server only, from `request_logging.py`). This is written for every request, including the `initialize` handshake and requests that are refused with 401 before any tool runs:

```
MCP request: POST /mcp rpc=tools/call:get_rows_fast status=200 client_id=claude-connector tenant=acme token=3f9a1c2e
```

| Field | Meaning |
|---|---|
| `rpc` | The JSON-RPC method, plus the tool name for tool calls (`tools/call:get_schema`). `-` if it could not be read, for example on a GET. |
| `status` | The HTTP status the server sent back. 401 means the token was missing, invalid or expired. |
| `client_id` | The OAuth client the token was issued to (for example, the connector a customer is using). `-` in legacy mode. |
| `tenant` | The inmydata tenant. With OAuth it comes from the token's `client_imd_tenant` or `imd_tenant` claim; in legacy mode from the `tenant` query parameter or `x-inmydata-tenant` header. |
| `token` | The fingerprint of the caller's token, as described above. |
| `claims=unverified` | Only appears when the token was not accepted. The client ID and tenant were read from the token without checking its signature, so treat them as a guide, not proof. |

Requests that arrive with no `Authorization` header at all are logged separately, as `MCP request without token: ... ip=<address> user_agent=<agent>`. These are normally the first step of a connector's OAuth sign-in, or internet scanners, and are kept apart so they do not mix with real traffic.

**Tool activity** (both servers, from `mcp_utils.py`):

- `get_rows called (...)` / `get_rows completed (subject=..., row_count=..., sample_rows=..., instance_id=...)`, and the same for `get_top_n`. The `instance_id` is the saved DuckDB result, if the result was large enough to be saved.
- `query_results called [<cid>] (instance_id=..., sql_chars=...)` and `query_results succeeded [<cid>] (... row_count=...)`. The SQL itself is only logged at `DEBUG`.

**Sign-in checks** (remote server with OAuth, from `pat_jwt_auth.py`). These show why a token was refused:

- `Introspection reports token inactive (token=<fp>)` (INFO): the PAT has expired or been revoked.
- `Introspection failed (token=<fp>, status=<status>)` (WARNING): the auth server refused the introspection request.
- `HTTP error during token introspection` / `Error during token introspection` (WARNING): the auth server could not be reached.
- At `DEBUG` only: `JWT verification failed (token=<fp>, reason=<exception type>)`, followed by an introspection attempt, plus cache hits.

**Tool errors** (both servers, from `errors.py`). Each one has a correlation ID (`cid`), the same 8-character ID that the MCP client receives in `correlation_id`:

- `WARNING agentmcp: tool error in get_rows_fast (cid=1a2b3c4d): <the API's error>`: an inmydata API 4xx error, which the caller can fix (wrong field name, bad filter and so on). No traceback.
- `ERROR agentmcp: tool error in get_rows_fast (cid=1a2b3c4d)` followed by a full traceback: everything else (API 401/403/5xx, timeouts, bugs in this server). The last line of the traceback holds the exception text, including the API's response body.
- `query_results ... [<cid>]` warnings: SQL that was blocked, an unknown `instance_id`, or SQL that DuckDB rejected (with DuckDB's message).
- `tool error in agentic_rag_query (cid=...)`: errors from the agentic RAG service, with its detail.

### Finding the log entries for a customer's problem

1. **The customer has a correlation ID** (every tool error shows one): search the logs for `cid=<id>` or `[<id>]`. That gives the tool, the full error and, for errors, the traceback.
2. **No correlation ID:** search for the tenant (`tenant=<name>`) around the time of the problem. The `MCP request:` lines show which tools were called and their HTTP status. The `token=` fingerprint on those lines then lets you follow that one caller's token through the sign-in checks and other requests.
3. **Sign-in problems:** look for `status=401` on the `MCP request:` lines, then for `Introspection ...` lines with the same `token=` fingerprint.

### Cross-referencing with the inmydata API logs

The correlation ID is created by this server and is **not sent to the inmydata API**, so it will not appear in the API's logs. To match an entry in this server's log with one in the API's logs, use:

- **The support reference (API 5xx errors).** When the API fails with a 5xx, its response ends `Contact support with reference <hex>`. The customer's error message includes this as `quoting reference <hex>`, and the full response is in this server's ERROR traceback under the correlation ID. Search the API logs for that reference. This is the most reliable link.
- **Tenant and time.** Every API call goes to `https://<tenant>.<server>/api/developer/v1/...`, so the tenant in this server's log matches the tenant on the API side. Match on the time of the tool call (the timestamp of the `get_rows called`, `get_top_n called` or `tool error` line) and the endpoint. Allow for the API call starting a moment after the `called` line.
- **The caller's identity.** With OAuth, this server passes the caller's own access token straight on to the API, so the API sees the same user (the token's `sub` claim) and OAuth client as the `client_id` in this server's log. In legacy mode, the API sees the API key, and the tenant is the best link.
- **The API's error message (4xx).** For 4xx errors, the WARNING line contains the API's response, and the customer saw a shortened, one-line version of the same text. Search the API logs for that message.
- **The token fingerprint.** If the API logs a SHA-256 fingerprint of the bearer token in the same way (first 8 hex characters), it will match `token=` here. If it does not, but you have the token, work out the fingerprint with the command above.

## Deployment

### Docker Deployment

```bash
docker build -t inmydata-mcp-server .
docker run -p 8000:8000 inmydata-mcp-server
```

Or using docker-compose:

```bash
docker-compose up -d
```

### Cloud Platforms

- **AWS**: ECS, App Runner, or Lambda
- **Google Cloud**: Cloud Run
- **Azure**: Container Apps
- **Render/Railway/Fly.io**: Direct GitHub deployment

See `deployment-guide.md` for platform-specific instructions and `client-config-example.json` for client configuration.

Requirements note: a `requirements.txt` is included for quick installs and
adds `uvicorn` for the remote server. Install with:

```powershell
python -m pip install -r requirements.txt
```

## Recent Changes
- **2026-11: Logging and error reporting.** One log line per MCP request with client, tenant and token fingerprint; credentials never logged (GHSA-3x42-7cvw-r9jv); tool errors give the caller a safe message and a correlation ID, with the detail in the log (GHSA-xv3j-j949-g8wx). See [Logging](#logging) and [Error reporting](#error-reporting).

- **2025-12-05: Added support for larger datasets by saving results in DuckDB database and adding a tool to query that.**

- **2025-10-29: Optional OAuth Authentication**
  - **🔐 Configurable Auth Modes**: New `INMYDATA_USE_OAUTH` environment variable enables switching between OAuth and legacy API key authentication
  - **🔄 Backward Compatible**: Defaults to legacy authentication (false) - existing deployments unaffected
  - **🎯 Token-Based Auth**: When enabled, automatically extracts tenant from JWT claims (`client_imd_tenant` or `imd_tenant`)
  - **🔑 Flexible Credentials**: Legacy mode supports environment variable lookup (`{TENANT}_API_KEY`), header-based API keys, and query parameter tenant override

- **2025-10-27: Major LLM & Developer Experience Improvements**
  - **🔧 Flexible Parameters**: All tool parameters now optional with smart defaults - eliminates crashes from empty `{}` calls
  - **📊 Simplified JSON**: `get_rows_fast` and `get_top_n_fast` return clean, flat JSON (40-60% smaller payloads)
  - **🤖 AI Schema Hints**: Auto-categorized fields (time/location/product) with dashboard recommendations
  - **📅 Smart Calendar Defaults**: `get_calendar_period_date_range()` with no args returns current month
  - **🔐 Query Parameter Auth**: `?tenant=name` support alongside headers
  - **🔍 Enhanced Filtering**: Added `not_contains` operator for text filtering

- 2025-10-08: Improved architecture and progress updates
  - Unified SDK interaction via `mcp_utils` helper class
  - Consistent JSON serialization across both servers
  - Documented MCP progress notification API (`session.add_notification_handler('progress', handler)`)
  - Added `requirements.txt` with uvicorn for remote server deployment
  
- 2025-10-02: Remote deployment support & example client
  - Added `server_remote.py` with SSE/HTTP transport for remote hosting
  - Implemented secure credential passing via HTTP headers
  - Created Docker deployment configuration
  - Added comprehensive deployment guide for AWS, GCP, Azure
  - Created `example_client.py` demonstrating FastMCP Client usage for both local and remote servers

## Key Features

### 🚀 LLM-Optimized Design

- **Graceful Error Handling**: Empty `{}` parameters return helpful errors instead of crashes
- **Token-Efficient Responses**: Simplified JSON format reduces token usage by 40-60%
- **Smart Defaults**: Common operations (like "current month") work with minimal parameters
- **Enhanced Filtering**: Support for `equals`, `contains`, `not_contains`, `starts_with`, `gt`, `gte`, `lt`, `lte` operators

### 🤖 AI-Enhanced Schema

- **Auto-Categorization**: Fields automatically grouped by semantic meaning (time, location, product, etc.)
- **Dashboard Hints**: AI-generated recommendations for time dimensions, key metrics, and fast query fields
- **Field Groups**: Pre-categorized field collections for smarter UI generation

### 📊 Performance Tiers

- **FAST PATH** (`get_rows_fast`, `get_top_n_fast`): Direct warehouse queries - seconds, not minutes
- **SLOW PATH** (`get_answer_slow`): Conversational AI with natural language - up to 1 minute with progress updates

### 🔄 Streaming Progress Updates

The `get_answer_slow` tool implements streaming progress notifications. As the inmydata SDK processes natural language queries (which can take up to a minute), progress updates are forwarded from the SDK's `ai_question_update` events to MCP progress notifications via `ctx.report_progress()`. MCP clients can receive these updates in real-time by registering a notification handler:

```python
session.add_notification_handler('progress', handler)
```

The handler receives progress events with:

- progress: Counter value
- message: Human-readable progress message

This enables clients to provide real-time feedback during long-running operations.
