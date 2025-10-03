#!/usr/bin/env python3
"""
Example MCP Client for inmydata MCP Server

This script demonstrates how to use FastMCP Client to connect to the inmydata
MCP server in both local (stdio) and remote (SSE/HTTP) modes.

Requirements:
    pip install mcp

Usage:
    python example_client.py local
    python example_client.py remote
"""

import asyncio
import json
import sys
from datetime import date
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client


async def example_local_server():
    """
    Example: Connect to local MCP server via stdio transport.
    
    The local server reads credentials from .env file.
    """
    print("\n=== Connecting to Local MCP Server (stdio) ===\n")
    
    server_params = StdioServerParameters(
        command="python",
        args=["server.py"],
        env=None
    )
    
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            
            print("✓ Connected to local server\n")
            
            await list_available_tools(session)
            await example_structured_data_simple(session)
            await example_structured_data_advanced(session)
            await example_chart_generation(session)
            await example_conversational_data(session)
            await example_calendar_assistant(session)
            
            print("\n✓ Local server examples completed\n")


async def example_remote_server():
    """
    Example: Connect to remote MCP server via SSE transport.
    
    The remote server requires credentials via HTTP headers.
    You must start the remote server first:
        python server_remote.py sse 8000
    """
    print("\n=== Connecting to Remote MCP Server (SSE) ===\n")
    
    print("⚠️  Make sure you've started the remote server:")
    print("    python server_remote.py sse 8000\n")
    print("⚠️  Update the headers below with your actual credentials\n")
    
    headers = {
        "x-inmydata-api-key": "your-api-key-here",
        "x-inmydata-tenant": "your-tenant-name",
        "x-inmydata-calendar": "your-calendar-name",
        "x-inmydata-user": "example-client",
        "x-inmydata-session-id": "example-session"
    }
    
    try:
        async with sse_client("http://localhost:8000/sse", headers=headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                
                print("✓ Connected to remote server\n")
                
                await list_available_tools(session)
                await example_structured_data_simple(session)
                await example_structured_data_advanced(session)
                await example_chart_generation(session)
                await example_conversational_data(session)
                await example_calendar_assistant(session)
                
                print("\n✓ Remote server examples completed\n")
    except Exception as e:
        print(f"❌ Error connecting to remote server: {e}")
        print("\nMake sure:")
        print("  1. Remote server is running: python server_remote.py sse 8000")
        print("  2. Headers contain valid inmydata credentials")


async def list_available_tools(session: ClientSession):
    """List all available tools from the MCP server."""
    print("--- Available Tools ---")
    
    tools = await session.list_tools()
    for tool in tools.tools:
        print(f"  • {tool.name}")
    
    print()


async def example_structured_data_simple(session: ClientSession):
    """
    Example: Query inmydata with simple equality filters.
    
    This demonstrates get_data_simple which uses simple field=value filters.
    
    Note: Update 'Sales' to match your actual inmydata subject name,
    and update fields/filters to match your data schema.
    """
    print("--- Example: get_data_simple ---")
    print("Note: This example uses placeholder values - update to match your inmydata subjects\n")
    
    try:
        # README example: TopN for Sales Person based on Sales Value in Edinburgh
        result = await session.call_tool(
            "get_data_simple",
            arguments={
                "subject": "Inmystore Sales",
                "filters": [
                    {"field": "Store", "value": "Edinburgh"}
                ],
                "fields": ["Sales Person", "Sales Value"],
                "case_sensitive": False,
                "top_n_options": {"Sales Person": {"order_by_field": "Sales Value", "n": 10}}
            }
        )
        print("Query: Top 10 Sales People in Edinburgh by Sales Value")
        print_tool_result(result)
    except Exception as e:
        print(f"⚠️  Tool call failed (expected with placeholder data): {e}")
        print("   Update the subject and filters to match your actual inmydata configuration\n")


async def example_structured_data_advanced(session: ClientSession):
    """
    Example: Query inmydata with advanced filters.
    
    This demonstrates get_data with complex conditions, OR logic, and bracketing.
    
    Note: Update to match your actual inmydata subject and fields.
    """
    print("--- Example: get_data (advanced) ---")
    print("Note: This example uses placeholder values - update to match your inmydata subjects\n")
    
    try:
        # README advanced example: use OR between Store=Edinburgh and Store=London
        result = await session.call_tool(
            "get_data",
            arguments={
                "subject": "Inmystore Sales",
                "filters": [
                    {"field": "Store", "operator": "equals", "logical": "and", "value": "Edinburgh", "brackets_before": 0, "brackets_after": 0, "case_sensitive": False},
                    {"field": "Store", "operator": "equals", "logical": "or", "value": "London", "brackets_before": 0, "brackets_after": 0, "case_sensitive": False}
                ],
                "fields": ["Financial Year", "Store", "Sales Value"],
                "top_n_options": {}
            }
        )
        print("Query: Financial Year, Store, Sales Value for Edinburgh or London")
        print_tool_result(result)
    except Exception as e:
        print(f"⚠️  Tool call failed (expected with placeholder data): {e}")
        print("   Update the subject, fields, and conditions to match your actual inmydata configuration\n")


async def example_conversational_data(session: ClientSession):
    """
    Example: Natural language query with streaming progress.
    
    This demonstrates get_answer which sends progress notifications as the
    query is processed by inmydata's AI.
    
    Note: Update the question to match your actual data.
    
    Progress Notifications: The server sends progress updates via MCP protocol
    during long-running queries. These are visible in MCP-compliant clients
    like Claude Desktop. The basic Python ClientSession in this example doesn't
    display them, but they are being sent by the server.
    """
    print("--- Example: get_answer (conversational) ---")
    print("Note: This example uses a placeholder question - update to query your actual data")
    print("Query: 'Give me the top 10 stores this year'")
    print("(This may take 30-60 seconds)")
    print("(Progress notifications are sent by server but not displayed in this basic example)\n")
    
    try:
        # README conversational example
        result = await session.call_tool(
            "get_answer",
            arguments={
                "question": "Give me the top 10 stores this year"
            }
        )
        
        print_tool_result(result)
    except Exception as e:
        print(f"⚠️  Tool call failed (expected with placeholder data): {e}")
        print("   Update the question to query your actual inmydata data\n")


async def example_calendar_assistant(session: ClientSession):
    """
    Example: Calendar/financial period queries.
    
    This demonstrates various calendar helper tools.
    """
    print("--- Example: Calendar Assistant ---")
    
    today = date.today().isoformat()
    
    result_fy = await session.call_tool(
        "get_financial_year",
        arguments={"target_date": today}
    )
    print(f"Financial year for {today}:")
    print_tool_result(result_fy)
    
    result_quarter = await session.call_tool(
        "get_quarter",
        arguments={"target_date": today}
    )
    print(f"Financial quarter for {today}:")
    print_tool_result(result_quarter)
    
    result_periods = await session.call_tool(
        "get_financial_periods",
        arguments={"target_date": today}
    )
    print(f"All financial periods for {today}:")
    print_tool_result(result_periods)


def print_tool_result(result):
    """Pretty print tool call results."""
    for content in result.content:
        if hasattr(content, 'text'):
            try:
                data = json.loads(content.text)
                print(json.dumps(data, indent=2))
            except json.JSONDecodeError:
                print(content.text)
        else:
            print(content)
    print()


async def example_chart_generation(session: ClientSession):
    """
    Example: Generate a chart and get the chart ID.
    
    Charts are created in inmydata and can be viewed in the platform.
    
    Note: Update subject, filters, axes, and chart_type to match your data.
    """
    print("--- Example: get_chart ---")
    print("Note: This example uses placeholder values - update to match your inmydata subjects\n")
    
    try:
        result = await session.call_tool(
            "get_chart",
            arguments={
                "subject": "Inmystore Sales",
                "filters": [
                    {"field": "Financial Year", "operator": "equals", "logical": "and", "value": "2024"}
                ],
                "row_fields": ["Sales Person"],
                "column_fields": [],
                "value_fields": ["Sales Value"],
                "chart_type": "bar",
                "title": "2024 Revenue by Product",
                "top_n_options": {"Sales Person": {"order_by_field": "Sales Value", "n": 10}}
            }
        )
        
        print("Chart created - view it in your inmydata platform using the chart_id from the response")
        print_tool_result(result)
    except Exception as e:
        print(f"⚠️  Tool call failed (expected with placeholder data): {e}")
        print("   Update the subject, filters, and axes to match your actual inmydata configuration\n")


async def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python example_client.py [local|remote]")
        print()
        print("Examples:")
        print("  python example_client.py local   # Connect to local stdio server")
        print("  python example_client.py remote  # Connect to remote SSE server")
        sys.exit(1)
    
    mode = sys.argv[1].lower()
    
    if mode == "local":
        await example_local_server()
    elif mode == "remote":
        await example_remote_server()
    else:
        print(f"Unknown mode: {mode}")
        print("Use 'local' or 'remote'")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
