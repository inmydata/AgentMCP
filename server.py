import os
import json
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
from inmydata.StructuredData import StructuredDataDriver, AIDataFilter, LogicalOperator, ConditionOperator, TopNOption
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Context
from mcp_utils import mcp_utils
from mcp_logging import configure_logging
from errors import UserFacingError, tool_error_message, tool_error_response
import agentic_rag_tool

load_dotenv(".env", override=True)
configure_logging()

# Enable debug mode only when manually set
if os.getenv("MCP_DEBUG", "0") == "1":
    import debugpy
    debugpy.listen(("localhost", 5678))
    # Debugger banners go to stderr (sys.stderr) so they don't corrupt the
    # stdio MCP transport.
    import sys
    print("MCP server waiting for VS Code debugger on port 5678...", file=sys.stderr)
    debugpy.wait_for_client()
    print("Debugger attached. Continuing execution.", file=sys.stderr)

mcp = FastMCP("inmydata-agent-server")

def utils():
    try:
        api_key = os.environ.get('INMYDATA_API_KEY', "")
        tenant = os.environ.get('INMYDATA_TENANT', "")
        server = os.environ.get('INMYDATA_SERVER',"inmydata.com")
        calendar = os.environ.get('INMYDATA_CALENDAR',"default")
        user = os.environ.get('INMYDATA_USER', 'mcp-agent')
        session_id = os.environ.get('INMYDATA_SESSION_ID', 'mcp-session')
        return mcp_utils(api_key, tenant, calendar, user, session_id, server)
    except Exception as e:
        raise RuntimeError("Error initializing mcp_utils") from e

@mcp.tool()
async def get_rows_fast(
    subject: str = "",
    select: List[str] = [],
    where: List[Dict[str, Any]] = [],
    ctx: Optional[Context] = None
) -> str:
    """
    FAST PATH (recommended) for fetching rows of data.
    Use when the request names specific fields and simple filters (no free-form reasoning).
    Far faster and cheaper than get_answer_slow.

    Before calling, call get_schema and take the subject and field names from it:
    - subject must be one of the subject names in get_schema.
    - Every field in select and where must be a key of that subject's factFieldTypes
      (dimensions) or metricFieldTypes (metrics), spelt exactly as in the schema,
      including case and spaces.
    - If the result is an error naming a subject or field, correct the name from
      get_schema. Do not guess other spellings.

    Results:
    - row_count is the total number of rows. If instance_id is blank, data holds them all.
    - If instance_id is not blank, data holds only a sample and the full result is in a
      table called my_table. You MUST use query_results_fast with that instance_id and SQL
      to read it; this is the only way to access larger results.

    where is a list of conditions, for example:
      [{"field": "Region", "op": "equals", "value": "North", "logical": "AND"},
       {"field": "Sales Value", "op": "gte", "value": 1000, "logical": "AND"}]
    Allowed op values: equals, contains, not_contains, starts_with, gt, lt, gte, lte.
    Allowed logical values: AND, OR (default AND), joining a condition to the previous one.

    Example (the names are illustrative; use the names in get_schema):
    "Sales and profit by region in financial year 2025"
      -> get_rows_fast(subject="Sales", select=["Region", "Sales Value", "Profit"],
                       where=[{"field": "Financial Year", "op": "equals", "value": 2025}])
    """
    try:
        if not subject:
            return json.dumps({"error": "subject parameter is required"})
        if not select:
            return json.dumps({"error": "select parameter is required (list of field names)"})
        return await utils().get_rows(subject, select, where)
    except UserFacingError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        return tool_error_response(exc, context="get_rows_fast")

@mcp.tool()
async def get_top_n_fast(
    subject: str = "",
    group_by: str = "",
    order_by: str = "",
    n: int = 10,
    where: List[Dict[str, Any]] = [],
    ctx: Optional[Context] = None
) -> str:
   """
    FAST PATH for rankings: the top or bottom n values of one field, ranked by a metric.
    Use when the user asks for "top/bottom N" by a metric (no free-form reasoning).
    Far faster and cheaper than get_answer_slow.

    Before calling, call get_schema and take the subject and field names from it:
    - subject must be one of the subject names in get_schema.
    - group_by is the field to rank, a key of the subject's factFieldTypes (for example
      a store or product field). order_by is the metric to rank by, a key of its
      metricFieldTypes. Names must match the schema exactly, including case and spaces.
    - If the result is an error naming a subject or field, correct the name from
      get_schema. Do not guess other spellings.
    - n > 0 gives the top n; n < 0 gives the bottom n.
    - where uses the same format as get_rows_fast.

    Results are in the same format as get_rows_fast, including instance_id and
    query_results_fast for results larger than the sample.

    Example (the names are illustrative; use the names in get_schema):
    "Top 10 stores by sales in financial year 2025"
      -> get_top_n_fast(subject="Sales", group_by="Store", order_by="Sales Value", n=10,
                        where=[{"field": "Financial Year", "op": "equals", "value": 2025}])
    """
   try:
       if not subject:
           return json.dumps({"error": "subject parameter is required"})
       if not group_by:
           return json.dumps({"error": "group_by parameter is required"})
       if not order_by:
           return json.dumps({"error": "order_by parameter is required"})
       return await utils().get_top_n(subject, group_by, order_by, n, where)
   except UserFacingError as exc:
       return json.dumps({"error": str(exc)})
   except Exception as exc:
       return tool_error_response(exc, context="get_top_n_fast")
   
@mcp.tool()
async def query_results_fast(
    instance_id: str = "",
    sql: str = "",
    ctx: Optional[Context] = None
) -> str:
   """
    FAST PATH for querying a result data set produced by other tools.
    Use when the data set returned by another tool is over a specific number of rows.
    This gives direct SQL access to the DuckDB database file saved on disk
    allowing fast and cheap querying of the data.
    Can be used to access larger datasets without needs further querying to get to an answer.
    The only table in the database will be named my_table.
    The columns will be the same as those returned by the tool that produced the dataset.
    You will have a sample of the data in the data property of the output json from the tool that
    produced the dataset.

    Example:
    - "Find the biggest difference between credit limit and balance"
      -> query_results_fast(dataset_id="", instance_id="", sql="SELECT MAX(CreditLimit - Balance) AS MaxDifference FROM my_table;")
    """
   try:       
       if not instance_id:
           return json.dumps({"error": "instance_id parameter is required"})
       if not sql:
           return json.dumps({"error": "sql parameter is required"})
       return await utils().query_results(instance_id, sql)
   except UserFacingError as exc:
       return json.dumps({"error": str(exc)})
   except Exception as exc:
       return tool_error_response(exc, context="query_results_fast")

@mcp.tool()
async def get_answer_slow(
    question: str = "",
    ctx: Optional[Context] = None
) -> str:
    """
    SLOW / EXPENSIVE (fallback).
    Use ONLY when a request cannot be expressed with get_rows or get_top_n.
    If the request names explicit fields, filters, years, or dimensions, prefer the fast tools above.

    Example good uses: "Why did region X underperform in 2025?" (requires explanation)
    Example bad uses:  "Avg transaction value by region in 2025" (should use get_rows)
    
    Args:
        question: Natural language question to ask (e.g., "Give me the top 10 stores this year")
    
    Returns:
        JSON string containing the answer, subject used, and any additional metadata
    """
    from inmydata.ConversationalData import ConversationalDataDriver
    
    try:
        if not question:
            return json.dumps({"error": "question parameter is required"})
        return await utils().get_answer(question, ctx)

    except UserFacingError as exc:
        if ctx:
            await ctx.error(str(exc))
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        response = tool_error_response(exc, context="get_answer_slow")
        if ctx:
            await ctx.error(tool_error_message(response))
        return response

@mcp.tool()
def get_schema() -> str:
    """
    Call this before get_rows_fast or get_top_n_fast: their subject and field names must match this schema exactly.
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
        return utils().get_schema()

    except UserFacingError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        return tool_error_response(exc, context="get_schema")

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
        return await utils().get_financial_periods(target_date)

    except UserFacingError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        return tool_error_response(exc, context="get_financial_periods")


@mcp.tool()
async def get_calendar_period_date_range(
    financial_year: Optional[int] = None,
    period_number: Optional[int] = None,
    period_type: Optional[str] = None,
    ctx: Optional[Context] = None
) -> str:
    """
    Get the start and end dates for a specific calendar period.
    
    Args:
        financial_year: The financial year (use null/None to automatically use current financial year)
        period_number: The period number (e.g., month number, quarter number; use null/None to automatically use current period)
        period_type: Type of period (year, month, quarter, week; use null/None to automatically use month)
    
    Note:
        If financial_year, period_number, or period_type are null/None (defaults), this tool will automatically
        fetch the current financial periods and use the appropriate values for today's date.
    
    Returns:
        JSON string with start_date and end_date
    """
    try:
        # mcp_utils fills any missing argument from today's financial period,
        # choosing the period number that matches period_type.
        return await utils().get_calendar_period_date_range(financial_year, period_number, period_type)

    except UserFacingError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        return tool_error_response(exc, context="get_calendar_period_date_range")


agentic_rag_tool.register(mcp)


if __name__ == "__main__":
    mcp.run()  # starts STDIO transport and blocks
