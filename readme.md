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

#### Calendar Tools

- `get_financial_periods` - Get all financial periods (year, quarter, month, week) for a date
- `get_calendar_period_date_range` - Get start/end dates for a calendar period. **Now supports smart defaults** - call with no parameters to get current month's date range

## Configuration

Required environment variables (see `.env.example`):

- `INMYDATA_API_KEY` - Your inmydata API key
- `INMYDATA_TENANT` - Your tenant name
- `INMYDATA_CALENDAR` - Your calendar name
- `INMYDATA_USER` (optional) - User for chart events (default: mcp-agent)
- `INMYDATA_SESSION_ID` (optional) - Session ID for chart events (default: mcp-session)

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

**Headers (traditional method):**
- `x-inmydata-api-key`: Your inmydata API key
- `x-inmydata-tenant`: Your tenant name
- `x-inmydata-calendar`: Your calendar name
- `x-inmydata-user` (optional): User for events (default: mcp-agent)
- `x-inmydata-session-id` (optional): Session ID (default: mcp-session)

**Query Parameters (new - takes precedence):**
- `?tenant=your-tenant-name` - Overrides `x-inmydata-tenant` header if provided
- API key can be auto-detected from environment variable `{TENANT}_API_KEY`

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
