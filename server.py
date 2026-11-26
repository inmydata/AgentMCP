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
import tool_descriptions as docs
from tool_descriptions import (
    FinancialYear, GroupBy, InstanceId, OrderBy, PeriodNumber, PeriodType, Question,
    SelectFields, Sql, Subject, TargetDate, TopN, Where, where_as_dicts,
)

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

mcp = FastMCP("inmydata-agent-server", instructions=docs.SERVER_INSTRUCTIONS)

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

# Tool descriptions and parameter types live in tool_descriptions.py, shared
# with server_remote.py, so both servers describe the tools the same way.

@mcp.tool(description=docs.GET_ROWS_FAST)
async def get_rows_fast(
    subject: Subject,
    select: SelectFields,
    where: Where = [],
    ctx: Optional[Context] = None
) -> str:
    """Handle get_rows_fast: check the arguments and fetch totals from mcp_utils.get_rows."""
    try:
        if not subject:
            return json.dumps({"error": "subject parameter is required"})
        if not select:
            return json.dumps({"error": "select parameter is required (list of field names)"})
        return await utils().get_rows(subject, select, where_as_dicts(where))
    except UserFacingError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        return tool_error_response(exc, context="get_rows_fast")

@mcp.tool(description=docs.GET_TOP_N_FAST)
async def get_top_n_fast(
    subject: Subject,
    group_by: GroupBy,
    order_by: OrderBy,
    n: TopN = 10,
    where: Where = [],
    ctx: Optional[Context] = None
) -> str:
   """Handle get_top_n_fast: check the arguments and fetch a ranking from mcp_utils.get_top_n."""
   try:
       if not subject:
           return json.dumps({"error": "subject parameter is required"})
       if not group_by:
           return json.dumps({"error": "group_by parameter is required"})
       if not order_by:
           return json.dumps({"error": "order_by parameter is required"})
       return await utils().get_top_n(subject, group_by, order_by, n, where_as_dicts(where))
   except UserFacingError as exc:
       return json.dumps({"error": str(exc)})
   except Exception as exc:
       return tool_error_response(exc, context="get_top_n_fast")
   
@mcp.tool(description=docs.QUERY_RESULTS_FAST)
async def query_results_fast(
    instance_id: InstanceId,
    sql: Sql,
    ctx: Optional[Context] = None
) -> str:
   """Handle query_results_fast: check the arguments and run the SQL via mcp_utils.query_results."""
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

@mcp.tool(description=docs.GET_ANSWER_SLOW)
async def get_answer_slow(
    question: Question,
    ctx: Optional[Context] = None
) -> str:
    """Handle get_answer_slow: check the arguments and ask mcp_utils.get_answer, reporting progress."""
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

@mcp.tool(description=docs.GET_SCHEMA)
def get_schema() -> str:
    """Handle get_schema: return the tenant's subjects and fields from mcp_utils.get_schema."""
    try:
        return utils().get_schema()

    except UserFacingError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        return tool_error_response(exc, context="get_schema")

@mcp.tool(description=docs.GET_FINANCIAL_PERIODS)
async def get_financial_periods(
    target_date: TargetDate = None,
    ctx: Optional[Context] = None
) -> str:
    """Handle get_financial_periods: look up a date's financial periods via mcp_utils."""
    try:
        return await utils().get_financial_periods(target_date)

    except UserFacingError as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        return tool_error_response(exc, context="get_financial_periods")


@mcp.tool(description=docs.GET_CALENDAR_PERIOD_DATE_RANGE)
async def get_calendar_period_date_range(
    financial_year: FinancialYear = None,
    period_number: PeriodNumber = None,
    period_type: PeriodType = None,
    ctx: Optional[Context] = None
) -> str:
    """Handle get_calendar_period_date_range: look up a financial period's dates via mcp_utils."""
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
