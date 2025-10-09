import os
from dotenv import load_dotenv
import asyncio
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport

load_dotenv(".env", override=True)

# With custom headers for authentication
transport = StreamableHttpTransport(
    url="http://localhost:8000/mcp",
    headers={
        "x-inmydata-api-key": os.environ.get('INMYDATA_API_KEY', ""),
        "x-inmydata-tenant": os.environ.get('INMYDATA_TENANT', ""),
        "x-inmydata-server": os.environ.get('INMYDATA_SERVER',"inmydata.com"),
        "x-inmydata-calendar": os.environ.get('INMYDATA_CALENDAR',"default"),
        "x-inmydata-user": os.environ.get('INMYDATA_USER', 'mcp-agent'),
        "x-inmydata-session-id": os.environ.get('INMYDATA_SESSION_ID', 'mcp-session')
    }
)
client = Client(transport)


async def main():
    async with client:

        # Basic server interaction
        await client.ping()
        
        # List available operations
        tools = await client.list_tools()
        resources = await client.list_resources()
        prompts = await client.list_prompts()
        #print("Available tools:", tools)


        # Execute operations
        #result = await client.call_tool("get_schema", {})
        #print(result)      
        #result = await client.call_tool("get_financial_periods", {})
        #print(result)
        #result = await client.call_tool("get_calendar_period_date_range", {"financial_year":2023, "period_number":3, "period_type":"month"})
        #print(result)
       
        async def get_answer_progress_handler(progress: float, total: float | None, message: str | None):
            print(f"Progress: {message}")
        result = await client.call_tool(
            "get_answer", 
            {"question":"Give me the top 10 stores based on sales of furniture last year?"},
            progress_handler=get_answer_progress_handler
        )
        print(result)


asyncio.run(main())
