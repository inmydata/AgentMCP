import json
from typing import Optional, List, Dict, Any
from mcp.server.fastmcp import FastMCP, Context
from mcp_utils import mcp_utils
mcp = FastMCP("inmydata-agent-server")


def utils(ctx: Optional[Context]) -> mcp_utils:
    try:
        if not ctx or not hasattr(ctx, 'request_context'):
            raise RuntimeError(f"Missing context")

        request = ctx.request_context
        headers = getattr(request, 'headers', {})
        api_key = headers.get('x-inmydata-api-key', '')
        tenant = headers.get('x-inmydata-tenant', '')
        server = headers.get('x-inmydata-server', '')
        calendar = headers.get('x-inmydata-calendar', '')
        user = headers.get('x-inmydata-user', 'mcp-agent')
        session_id = headers.get('x-inmydata-session-id', 'mcp-session')
        
        return mcp_utils(api_key, tenant, calendar, user, session_id, server)
    except Exception as e:
        raise RuntimeError(f"Error initializing mcp_utils: {e}")

@mcp.tool()
async def get_rows(
    subject: str,
    select: List[str],
    where: Optional[List[Dict[str, Any]]] = None,
    ctx: Optional[Context] = None
) -> str:
    """
    Retrieve rows with a simple AND-only filter list.
    where: [{"field":"Region","op":"equals","value":"North"}, {"field":"Sales Value","op":"gte","value":1000}]
    Allowed ops: equals, contains, starts_with, gt, lt, gte, lte
    Returns records (<= limit) and total_count if available.
    """
    try:
        return await utils(ctx).get_rows(subject, select, where)
    except Exception as e:
        return json.dumps({"error": str(e)})

@mcp.tool()
async def get_top_n(
    subject: str,
    group_by: str,
    order_by: str,
    n: int,
    where: Optional[List[Dict[str, Any]]] = None,
    ctx: Optional[Context] = None
) -> str:
   """
    Return top/bottom N groups by a metric.
    n>0 => top N, n<0 => bottom N.
    where uses the same shape as get_rows.
    """
   try:
       return await utils(ctx).get_top_n(subject, group_by, order_by, n, where)
   except Exception as e:
       return json.dumps({"error": str(e)}) 

@mcp.tool()
async def get_answer(
    question: str,
    ctx: Optional[Context] = None
) -> str:
    """
    Get an answer to a natural language question about inmydata using conversational AI.
    This operation may take up to a minute and will send progress updates.
    
    Args:
        question: Natural language question to ask (e.g., "Give me the top 10 stores this year")
    
    Returns:
        JSON string containing the answer, subject used, and any additional metadata
    """
    from inmydata.ConversationalData import ConversationalDataDriver
    
    try:
        return await utils(ctx).get_answer(question, ctx)
    
    except Exception as e:
        if ctx:
            await ctx.error(f"Error in get_answer: {str(e)}")
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_schema(ctx: Optional[Context] = None) -> str:
    """
    Get the available schema. Returns a JSON object that defines the available subjects (tables) and their columns.

    Returns a JSON string with:
      - schemaVersion: int
      - generatedAt: ISO 8601 UTC timestamp
      - source: string identifying this server
      - subjectsCount: int
      - subjects: [
          {
            name: str,
            aiDescription: Optional[str],
            factFieldTypes: { fieldName: { name, type, aiDescription } },
            metricFieldTypes: { metricName: { name, type, dimensionsUsed, aiDescription } },
            numDimensions: int,
            numMetrics: int
          }, ...
        ]
    """
    try:
        return utils(ctx).get_schema()

    except Exception as e:
        # Mirror your C# error string style
        return f"Error retrieving subjects: {e}"

@mcp.tool()
async def get_financial_periods(
    target_date: Optional[str] = None,
    ctx: Optional[Context] = None
) -> str:
    """
    Get all financial periods (year, quarter, month, week) for a given date.
    
    Args:
        target_date: Date in ISO format (YYYY-MM-DD). If not provided, uses today's date.
    
    Returns:
        JSON string with all financial periods
    """
    try:
        return await utils(ctx).get_financial_periods(target_date)
    
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_calendar_period_date_range(
    financial_year: int,
    period_number: int,
    period_type: str,
    ctx: Optional[Context] = None
) -> str:
    """
    Get the start and end dates for a specific calendar period.
    
    Args:
        financial_year: The financial year
        period_number: The period number (e.g., month number, quarter number)
        period_type: Type of period (year, month, quarter, week)
    
    Returns:
        JSON string with start_date and end_date
    """
    try:
        return await utils(ctx).get_calendar_period_date_range(financial_year, period_number, period_type)
    
    except Exception as e:
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    import sys
    import uvicorn
    
    transport = "sse"
    port = 8000
    
    if len(sys.argv) > 1:
        if sys.argv[1] in ["sse", "streamable-http"]:
            transport = sys.argv[1]
        if len(sys.argv) > 2:
            port = int(sys.argv[2])
    
    print(f"Starting MCP server with {transport} transport on port {port}")
    print("Credentials should be passed via headers:")
    print("  x-inmydata-api-key: Your API key")
    print("  x-inmydata-tenant: Your tenant name")
    print("  x-inmydata-server: Server name (optional, default: inmydata.com)")
    print("  x-inmydata-calendar: Your calendar name")
    print("  x-inmydata-user: User for events (optional, default: mcp-agent)")
    print("  x-inmydata-session-id: Session ID (optional, default: mcp-session)")
    
    if transport == "sse":
        app = mcp.sse_app()
        uvicorn.run(app, host="0.0.0.0", port=port)
    elif transport == "streamable-http":
        app = mcp.streamable_http_app()
        uvicorn.run(app, host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")
