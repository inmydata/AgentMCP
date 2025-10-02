## Purpose

Short, focused instructions for an AI coding agent to be immediately productive in this repository (inmydata MCP Server).

## Big picture (what this repo is)
- A small Python 3.11 MCP server exposing the inmydata SDK via FastMCP.
- `server.py` = local stdio MCP server (reads credentials from environment variables).
- `server_remote.py` = HTTP remote server (SSE or streamable-HTTP) intended for cloud deployment; it reads inmydata credentials from HTTP headers.
- `example_client.py` = example FastMCP client showing how to call the tools (local and remote).
- Key dependency list: see `pyproject.toml` (inmydata, mcp, pandas, python-dotenv).

## Architecture & dataflow (concise)
- FastMCP registers async functions decorated with `@mcp.tool()` (see `server.py` and `server_remote.py`). Each tool receives typed args and an optional `ctx: Context`.
- StructuredData and ConversationalData use the inmydata SDK drivers (`StructuredDataDriver`, `ConversationalDataDriver`, `CalendarAssistant`). Drivers return pandas DataFrames or domain objects; code converts them to JSON strings before returning.
- Local flow: `server.py` reads `INMYDATA_*` env vars and calls drivers directly.
- Remote flow: `server_remote.py` extracts credentials from headers (see `get_credentials_from_context`) and sets `driver.api_key`, `driver.user`, `driver.session_id` accordingly.

## Key conventions and patterns (project-specific)
- Tool return type: Tools return JSON-encoded strings (often from `df.to_json(orient='records')` or `json.dumps({...})`). Error paths return `{"error": "..."}` JSON strings.
- Credentials: Local uses environment variables (INMYDATA_API_KEY, INMYDATA_TENANT, INMYDATA_CALENDAR, optional INMYDATA_USER/INMYDATA_SESSION_ID). Remote uses HTTP headers: `x-inmydata-api-key`, `x-inmydata-tenant`, `x-inmydata-calendar`, `x-inmydata-user`, `x-inmydata-session-id`.
- Progress streaming: `get_answer` wires the SDK's `ai_question_update` events to MCP progress via `ctx.report_progress(...)`. When editing or adding long-running tools, follow the same pattern: subscribe to SDK events and call `ctx.report_progress` (create asyncio tasks if necessary).
- Filters & shapes (examples):
  - get_data_simple filters: `[{"field": "Region", "value": "North"}]`
  - get_data (advanced) expects list of dicts with keys: `field`, `operator` (e.g. `equals`, `greater_than`), `logical` (`and`/`or`), `value`, optional `brackets_before`, `brackets_after`, `case_sensitive`.
  - top_n_options: `{ "Sales": {"order_by_field": "Revenue", "n": 10} }` (n positive for top, negative for bottom).
  - chart_type strings: `bar`, `pie`, `area`, `column`, `scatter`, `bubble`, `grid`.
- Dates: Use ISO format `YYYY-MM-DD` for calendar functions (see `get_financial_year`, `get_calendar_period_date_range`).

## Running & developer workflows (commands)
Use Python 3.11.

Local (stdio) server (reads `.env` via python-dotenv):

```powershell
python server.py
```

Remote server (SSE or streamable-http, uses uvicorn):

```powershell
# SSE transport on port 8000
python server_remote.py sse 8000

# Streamable HTTP transport on port 8000
python server_remote.py streamable-http 8000
```

Example client usage (examples in `example_client.py`):

```powershell
# Local stdio example
python example_client.py local

# Remote example (make sure server_remote.py is running and update headers)
python example_client.py remote
```

Docker (build + run):

```powershell
docker build -t inmydata-mcp-server .
docker run -p 8000:8000 inmydata-mcp-server
# or docker-compose up -d (file present)
```

Installing dependencies (quick):

```powershell
python -m pip install --upgrade pip
python -m pip install inmydata mcp pandas python-dotenv
```

## Files to inspect for common edits
- `server.py` — local behavior, env-based credentials, progress streaming pattern.
- `server_remote.py` — header parsing, credential mapping, uvicorn startup and transport choices.
- `example_client.py` — canonical client calls, argument shapes and how progress is consumed.
- `pyproject.toml` — declared runtime dependencies and Python version requirement.
- `client-config-example.json` and `deployment-guide.md` — client-side config and deployment notes.

## Common small tasks and gotchas for AI edits
- If adding a new tool, register with `@mcp.tool()` and follow return-conventions (JSON string or df.to_json). Add docstring describing arguments and return payloads.
- For long-running operations, wire SDK progress events to `ctx.report_progress` to keep clients informed (see `get_answer`). Use `asyncio.create_task` to call `ctx.report_progress` from non-async SDK callbacks.
- Keep error responses consistent: return JSON with top-level `error` key.
- For remote server changes, update `get_credentials_from_context` and ensure `driver.api_key` is set before calling SDK methods.
- When changing public tool signatures, update `example_client.py` examples so maintainers can manually test quickly.

## Where to look for more context
- `readme.md` — overview and usage examples.
- `deployment-guide.md` — cloud deployment notes and environment/headers guidance.

---
If anything here is unclear or you want more detail about a specific tool/flow (for example full JSON shapes of advanced filters or the exact sequence of progress events), tell me which area to expand and I'll update this file accordingly.
