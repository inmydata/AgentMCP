#!/usr/bin/env python3
"""Smoke test for inmydata MCP Server (local stdio transport).

This script expects the following environment variables to be set in the process
that runs it (or provided by the PowerShell wrapper):
  INMYDATA_API_KEY
  INMYDATA_TENANT
  INMYDATA_CALENDAR

It will:
  - Launch `server.py` as a subprocess using the current Python executable
  - Connect to it via the MCP stdio client
  - List available tools and call `get_financial_year` as a lightweight smoke test

This script never writes secrets to disk.
"""

import asyncio
import json
import os
import sys
from datetime import date

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run_smoke_test():
    required = [
        "INMYDATA_API_KEY",
        "INMYDATA_TENANT",
        "INMYDATA_CALENDAR",
    ]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print("Missing required environment variables:", missing)
        print("Set them in your shell (preferred) or use the provided PowerShell wrapper scripts/run-smoke-test.ps1")
        sys.exit(2)

    # Use the same environment when launching the server so the server inherits keys
    env = os.environ.copy()

    server_params = StdioServerParameters(command=sys.executable, args=["server.py"], env=env)

    print("Starting local server and connecting via MCP stdio client...")
    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                print("Connected. Listing tools...")
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                print("Available tools:", names)

                # Call a simple calendar tool to validate requests/responses
                today = date.today().isoformat()
                print(f"Calling get_financial_year for {today}...")
                result = await session.call_tool("get_financial_year", arguments={"target_date": today})

                # Print tool output (the MCP contents are in result.content)
                for content in result.content:
                    # Avoid direct attribute access on typed MCP content objects
                    # Use getattr to safely read 'text' and check it's a string.
                    text = getattr(content, 'text', None)
                    if isinstance(text, str):
                        try:
                            data = json.loads(text)
                            print(json.dumps(data, indent=2))
                        except Exception:
                            print(text)
                    else:
                        # Fallback: print a safe representation
                        try:
                            print(str(content))
                        except Exception:
                            print(repr(content))

                print("Smoke test completed successfully.")

    except Exception as e:
        print("Smoke test failed:", e)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(run_smoke_test())
    except KeyboardInterrupt:
        print("Interrupted by user")
    except SystemExit:
        raise
    except Exception:
        raise
