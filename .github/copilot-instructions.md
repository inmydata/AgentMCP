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

# inmydata MCP Server — Copilot instructions

Keep this short and practical. The goal: make an AI coding agent productive immediately by surfacing repo-specific architecture, conventions, and examples.

Key files
- `server.py` — local stdio MCP server. Reads credentials from environment (`.env`) and registers tools with `@mcp.tool()`.
- `server_remote.py` — remote HTTP/SSE entrypoint. Extracts credentials from request headers via `get_credentials_from_context` and exposes `sse` and `streamable-http` transports.
- `example_client.py` — canonical client usage (stdio and SSE). Use this when adding or changing tool signatures.
- `scripts/claude-launch.ps1` — helper for launching the stdio server with `.env` (Claude/Desktop style).
- `deployment-guide.md`, `client-config-example.json`, `pyproject.toml` — deployment and dependency references.

Architecture & conventions (what matters)
- MCP tools return JSON-encoded strings (often `df.to_json(orient='records')` or `json.dumps({...})`). Error responses are JSON objects with top-level `error`.
- Long-running operations must stream progress via MCP context: subscribe to SDK events and call `ctx.report_progress(...)` (see `get_answer` in both `server.py` and `server_remote.py`). Use `asyncio.create_task` if events are synchronous callbacks.
- Credentials:
  - Local (`server.py`): uses environment variables `INMYDATA_API_KEY`, `INMYDATA_TENANT`, `INMYDATA_CALENDAR`, optional `INMYDATA_USER`, `INMYDATA_SESSION_ID`.
  - Remote (`server_remote.py`): expects headers `x-inmydata-api-key`, `x-inmydata-tenant`, `x-inmydata-calendar`, optional `x-inmydata-user`, `x-inmydata-session-id`.
- Structured Data patterns:
  - `get_data_simple` expects simple equality filters [{"field":..., "value":...}] and optional `top_n_options` mapping to `TopNOption`.
  - `get_data` expects advanced filters with keys: `field`, `operator` (e.g. `equals`, `greater_than`), `logical` (`and`/`or`), `value`, optional `brackets_before`/`after`, `case_sensitive`.
- Chart creation: `get_chart` maps `chart_type` strings to `ChartType` enums. Return payload: {"chart_id": "..."}.

How to add or change a tool (checklist)
1. Add an async function decorated with `@mcp.tool()` in `server.py` (local) and mirror in `server_remote.py` if remote header-based credentials are needed.
2. Use typed args and document the argument shapes in the docstring (clients rely on these shapes).
3. Return JSON strings. On error, return `json.dumps({"error": "..."})`.
4. If the tool is long-running, subscribe to SDK events and forward progress using `ctx.report_progress`.
5. Update `example_client.py` to include a quick usage example for the new/changed tool.

Run & debug
- Local: `python server.py` (reads `.env` via python-dotenv). Use `example_client.py local` to exercise flows.
- Remote: `python server_remote.py sse 8000` or `python server_remote.py streamable-http 8000`. Provide credentials via headers (see `example_client.py` for header structure).
- Docker: `docker build -t inmydata-mcp-server .` and `docker run -p 8000:8000 inmydata-mcp-server` or `docker-compose up -d`.

Tests & quick checks
- There are no unit tests in the repo by default. Use `example_client.py` as an integration smoke test (local or remote).
- When editing, run `python server.py` and `python example_client.py local` to validate tool discovery and call shapes quickly.

Extra notes
- Prefer changing both `server.py` and `server_remote.py` for public tool signatures. `server_remote.py` uses header extraction helper `get_credentials_from_context`.
- Keep return shapes stable: clients parse JSON arrays or objects — changing shapes breaks downstream users.
- Check `pyproject.toml` for dependency versions (Python >= 3.11, inmydata, mcp, pandas, python-dotenv).

If anything in this file is unclear or you want more detail about a particular tool shape or event sequence (for example exact ai_question_update messages), tell me which area to expand and I'll update this file.
