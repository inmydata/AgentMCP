# inmydata MCP Server

## Overview

This project is a Python web application that exposes the inmydata agents SDK as a Model Context Protocol (MCP) Server. It enables AI agents to query structured data, generate charts, ask conversational questions, and work with financial calendar periods through the inmydata platform.

The server operates in two modes:
- **Local mode** (`server.py`) - stdio transport for local MCP clients, credentials from environment variables
- **Remote mode** (`server_remote.py`) - HTTP/SSE transport for cloud deployment, credentials from HTTP headers

## User Preferences

Preferred communication style: Simple, everyday language.

## System Architecture

### Core Technologies

**Runtime & Framework**
- Python 3.11 as the runtime environment
- FastMCP framework for building MCP servers with decorator-based tool registration
- Async/await pattern throughout for non-blocking I/O operations

**Primary Dependencies**
- `inmydata` SDK (v0.0.16+) - provides StructuredDataDriver, ConversationalDataDriver, and CalendarAssistant
- `mcp` (v1.15.0+) - Model Context Protocol implementation
- `pandas` (v2.3.3+) - data manipulation for SDK responses
- `python-dotenv` (v1.1.1+) - environment variable management
- `uvicorn` - ASGI server for remote deployment

### Transport Architecture

**Dual Transport Design**
The system supports two independent transport mechanisms:

1. **stdio transport** - Local development and direct client connections. The server reads credentials from `.env` file and communicates via standard input/output streams.

2. **SSE/HTTP transport** - Cloud deployment with credentials passed securely via HTTP headers (`x-inmydata-api-key`, `x-inmydata-tenant`, `x-inmydata-calendar`, etc.). Supports Server-Sent Events for streaming responses.

This separation allows the same tool implementations to work in both local and remote contexts by extracting credentials from different sources (environment variables vs. HTTP headers).

### Tool Architecture

**Tool Registration Pattern**
All tools use FastMCP's `@mcp.tool()` decorator for automatic registration. Each tool:
- Receives typed parameters for validation
- Accepts optional `Context` parameter for request metadata
- Returns JSON-encoded strings (either data records or error objects)
- Handles SDK exceptions and converts them to `{"error": "..."}` responses

**Tool Categories**

1. **StructuredData Tools**
   - `get_data_simple` - Simple equality-based filtering on inmydata subjects
   - `get_data` - Advanced querying with OR logic, bracketing, case sensitivity, and complex condition operators
   - `get_chart` - Chart generation from data queries, returns chart ID for visualization

2. **ConversationalData Tool**
   - `get_answer` - Natural language queries with streaming progress updates via MCP progress notifications

3. **CalendarAssistant Tools**
   - Financial calendar utilities (`get_financial_year`, `get_quarter`, `get_month`, `get_week_number`)
   - Period analysis (`get_financial_periods`, `get_calendar_period_date_range`)

### Data Flow Architecture

**Local Mode Flow**
1. Client initiates stdio connection to `server.py`
2. Server loads credentials from environment variables via `python-dotenv`
3. Tool invocation creates SDK driver instances with loaded credentials
4. SDK returns pandas DataFrames or domain objects
5. Helper function `_to_primitive()` recursively converts SDK objects to JSON-serializable primitives
6. Tool returns JSON string to client via stdout

**Remote Mode Flow**
1. Client connects via HTTP/SSE to `server_remote.py` running under uvicorn
2. `get_credentials_from_context()` extracts inmydata credentials from request headers
3. SDK driver instances are configured with extracted credentials
4. Same data processing as local mode
5. Response returned via HTTP response body or SSE stream

**Progress Streaming Design**
Long-running operations (e.g., `get_answer`) subscribe to SDK progress events and translate them to MCP progress notifications via `ctx.report_progress()`. This enables real-time feedback to clients during complex queries.

### Data Serialization Strategy

**Primitive Conversion**
The `_to_primitive()` helper function recursively converts SDK domain objects to JSON-serializable types:
- `date`/`datetime` objects → ISO 8601 strings
- pandas DataFrames → list of record dicts via `.to_json(orient='records')`
- Custom objects → dictionary of public attributes
- Circular reference protection via `_seen` set

This defensive conversion handles SDK response diversity without requiring extensive type mapping.

### Error Handling Pattern

**Consistent Error Response**
All tools follow a uniform error response pattern:
```python
return json.dumps({"error": "description"})
```

This ensures clients can reliably detect failures by checking for the presence of an "error" key in the JSON response, regardless of which tool was invoked.

### Configuration Design

**Environment-based Configuration (Local)**
- `INMYDATA_API_KEY` - API authentication token
- `INMYDATA_TENANT` - Tenant namespace
- `INMYDATA_CALENDAR` - Calendar configuration
- `INMYDATA_USER` (optional, default: "mcp-agent")
- `INMYDATA_SESSION_ID` (optional, default: "mcp-session")

**Header-based Configuration (Remote)**
Same credentials passed via HTTP headers with `x-inmydata-` prefix for security in multi-tenant cloud deployments.

## External Dependencies

### inmydata SDK Integration

**Core SDK Drivers**
- `StructuredDataDriver` - handles subject queries, filtering, aggregation, and chart generation
- `ConversationalDataDriver` - processes natural language questions and streams progress events
- `CalendarAssistant` - financial calendar calculations and period lookups

**SDK Response Handling**
The SDK returns pandas DataFrames for tabular data and custom domain objects for calendar/chart operations. All responses are normalized to JSON strings before returning to MCP clients.

### Model Context Protocol (MCP)

**Client-Server Communication**
The MCP framework provides:
- Tool discovery and invocation protocol
- Typed parameter validation
- Progress notification system for streaming updates
- Context propagation for request metadata

**Transport Implementations**
- `stdio_client` / `StdioServerParameters` for local connections
- `sse_client` for remote Server-Sent Events connections
- Support for custom headers in remote mode

### Deployment Infrastructure

**Container Support**
Docker and docker-compose configurations provided for containerized deployments. Dockerfile builds Python 3.11 image with all dependencies.

**Cloud Platform Support**
Deployment guide includes patterns for:
- AWS ECS (Elastic Container Service) with ECR image registry
- AWS App Runner for simplified container deployment
- AWS Lambda + API Gateway for serverless deployment
- Generic instructions applicable to Google Cloud, Azure, and other platforms

**Web Server**
uvicorn ASGI server runs the remote FastMCP application on configurable ports (default: 8000) with SSE or streamable-HTTP transport modes.

### Data Processing

**pandas Integration**
Used exclusively for SDK response handling:
- Converting DataFrames to JSON records
- No direct data manipulation (SDK handles all business logic)
- Serves as interchange format between SDK and JSON responses