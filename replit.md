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

#### StructuredData Tools
- `get_data_simple` - Query inmydata subjects with simple equality filters
- `get_data` - Advanced queries with OR logic, bracketing, and complex conditions
- `get_chart` - Generate charts from inmydata and return chart IDs

#### ConversationalData Tool  
- `get_answer` - Natural language queries to inmydata (supports streaming progress updates via MCP progress notifications)

#### CalendarAssistant Tools
- `get_financial_year` - Get financial year for a date
- `get_quarter` - Get financial quarter for a date  
- `get_month` - Get financial month for a date
- `get_week_number` - Get financial week number for a date
- `get_financial_periods` - Get all financial periods for a date
- `get_calendar_period_date_range` - Get start/end dates for a specific calendar period

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

#### Required Headers for Remote Server

Clients must include these headers when connecting:
- `x-inmydata-api-key`: Your inmydata API key
- `x-inmydata-tenant`: Your tenant name
- `x-inmydata-calendar`: Your calendar name
- `x-inmydata-user` (optional): User for events (default: mcp-agent)
- `x-inmydata-session-id` (optional): Session ID (default: mcp-session)

See `deployment-guide.md` for detailed deployment instructions.

### Example Client

The `example_client.py` script demonstrates how to connect to and use the MCP server:

```bash
# Connect to local server (stdio)
python example_client.py local

# Connect to remote server (SSE)
python example_client.py remote
```

The example client shows:
- Connecting to both local (stdio) and remote (SSE) servers
- Calling all MCP tools (StructuredData, ConversationalData, CalendarAssistant)
- Handling streaming progress notifications from long-running queries
- Proper error handling and credential management

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

## Recent Changes

- 2025-10-02: Remote deployment support & example client
  - Added `server_remote.py` with SSE/HTTP transport for remote hosting
  - Implemented secure credential passing via HTTP headers
  - Created Docker deployment configuration
  - Added comprehensive deployment guide for AWS, GCP, Azure
  - Created `example_client.py` demonstrating FastMCP Client usage for both local and remote servers
  
- 2025-10-01: Initial implementation
  - Created MCP server with FastMCP framework
  - Implemented all inmydata SDK tools as MCP tools
  - Added streaming progress support for conversational queries
  - Set up environment configuration and .gitignore

## Features

### Streaming Progress Updates

The `get_answer` tool implements streaming progress notifications. As the inmydata SDK processes natural language queries (which can take up to a minute), progress updates are forwarded from the SDK's `ai_question_update` events to MCP progress notifications, allowing clients to display real-time feedback to users.
