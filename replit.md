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

### Running the Server

The server runs via stdio transport for local MCP client connections:
```bash
python server.py
```

### Connecting an MCP Client

MCP clients can connect to this server using stdio transport with:
```bash
python server.py
```

The server will communicate via standard input/output following the MCP protocol.

## Recent Changes

- 2025-10-01: Initial implementation
  - Created MCP server with FastMCP framework
  - Implemented all inmydata SDK tools as MCP tools
  - Added streaming progress support for conversational queries
  - Set up environment configuration and .gitignore

## Features

### Streaming Progress Updates

The `get_answer` tool implements streaming progress notifications. As the inmydata SDK processes natural language queries (which can take up to a minute), progress updates are forwarded from the SDK's `ai_question_update` events to MCP progress notifications, allowing clients to display real-time feedback to users.
