import sys
import asyncio

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import os
import json
from datetime import date, datetime
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Context

load_dotenv()

mcp = FastMCP("inmydata-agent-server")


def _to_primitive(obj, _seen=None):
    """
    Recursively convert an SDK/domain object into JSON-serializable primitives.
    - dates/datetimes -> ISO strings
    - lists/tuples -> lists
    - dicts -> dicts
    - other objects -> map of public attrs -> primitive
    This is defensive: if attribute access raises, it falls back to str().
    """
    if _seen is None:
        _seen = set()

    # primitives
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj

    # dates and datetimes
    try:
        from datetime import date, datetime
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
    except Exception:
        pass

    # containers
    if isinstance(obj, (list, tuple, set)):
        return [_to_primitive(o, _seen) for o in obj]
    if isinstance(obj, dict):
        return {str(k): _to_primitive(v, _seen) for k, v in obj.items()}

    # avoid recursion
    obj_id = id(obj)
    if obj_id in _seen:
        return str(obj)
    _seen.add(obj_id)

    # try dataclass / __dict__ style
    try:
        if hasattr(obj, '__dict__') and obj.__dict__:
            result = {}
            for k, v in vars(obj).items():
                if k.startswith('_'):
                    continue
                try:
                    result[k] = _to_primitive(v, _seen)
                except Exception:
                    result[k] = str(v)
            return result
    except Exception:
        pass

    # fallback: inspect public attributes
    result = {}
    try:
        for attr in [a for a in dir(obj) if not a.startswith('_')]:
            try:
                val = getattr(obj, attr)
            except Exception:
                continue
            if callable(val):
                continue
            try:
                result[attr] = _to_primitive(val, _seen)
            except Exception:
                result[attr] = str(val)
        if result:
            return result
    except Exception:
        pass

    # final fallback
    try:
        return str(obj)
    except Exception:
        return None



@mcp.tool()
async def get_data_simple(
    subject: str,
    fields: List[str],
    filters: List[Dict[str, str]],
    case_sensitive: bool = False,
    top_n_options: Optional[Dict[str, Dict[str, Any]]] = None,
    ctx: Optional[Context] = None
) -> str:
    """
    Retrieve structured data from inmydata using simple equality filters.
    
    Args:
        subject: Name of the inmydata subject to query (e.g., "Inmystore Sales")
        fields: List of field names to retrieve (e.g., ["Sales Person", "Sales Value"])
        filters: List of simple equality filters, each with 'field' and 'value' keys
        case_sensitive: Whether filters are case sensitive (default: False)
        top_n_options: Optional dict mapping field names to TopN config with 'order_by_field' and 'n' (positive for top, negative for bottom)
    
    Returns:
        JSON string containing the query results as a pandas DataFrame in JSON format
    """
    from inmydata.StructuredData import StructuredDataDriver, AIDataSimpleFilter, TopNOption
    
    try:
        tenant = os.environ.get('INMYDATA_TENANT')
        if not tenant:
            return json.dumps({"error": "INMYDATA_TENANT environment variable not set"})
        
        driver = StructuredDataDriver(tenant)
        user = os.environ.get('INMYDATA_USER', 'mcp-agent')
        session_id = os.environ.get('INMYDATA_SESSION_ID', 'mcp-session')
        driver.user = user
        driver.session_id = session_id
        
        simple_filters = []
        for f in filters:
            simple_filters.append(AIDataSimpleFilter(f['field'], f['value']))
        
        top_n_opts = {}
        if top_n_options:
            for field_name, config in top_n_options.items():
                top_n_opts[field_name] = TopNOption(config['order_by_field'], config['n'])
        
        df = driver.get_data_simple(subject, fields, simple_filters, case_sensitive, top_n_opts)
        if df is None:
            return json.dumps({"error": "no data returned"})
        return df.to_json(orient='records', date_format='iso')
    
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_data(
    subject: str,
    fields: List[str],
    filters: List[Dict[str, Any]],
    top_n_options: Optional[Dict[str, Dict[str, Any]]] = None,
    ctx: Optional[Context] = None
) -> str:
    """
    Retrieve structured data from inmydata using advanced filters (supports OR, bracketing, non-equality).
    
    Args:
        subject: Name of the inmydata subject to query
        fields: List of field names to retrieve
        filters: List of advanced filters, each with:
            - field: Field name
            - operator: Condition operator (equals, not_equals, contains, not_contains, starts_with, not_starts_with, like, not_like, greater_than, less_than, greater_than_or_equal, less_than_or_equal)
            - logical: Logical operator (and, or)
            - value: Filter value
            - brackets_before: Number of opening brackets before this condition (default: 0)
            - brackets_after: Number of closing brackets after this condition (default: 0)
            - case_sensitive: Whether filter is case sensitive (default: False)
        top_n_options: Optional dict mapping field names to TopN config
    
    Returns:
        JSON string containing the query results
    """
    from inmydata.StructuredData import StructuredDataDriver, AIDataFilter, LogicalOperator, ConditionOperator, TopNOption
    
    try:
        tenant = os.environ.get('INMYDATA_TENANT')
        if not tenant:
            return json.dumps({"error": "INMYDATA_TENANT environment variable not set"})
        
        driver = StructuredDataDriver(tenant)
        user = os.environ.get('INMYDATA_USER', 'mcp-agent')
        session_id = os.environ.get('INMYDATA_SESSION_ID', 'mcp-session')
        driver.user = user
        driver.session_id = session_id
        
        operator_map = {
            'equals': ConditionOperator.Equals,
            'not_equals': ConditionOperator.NotEquals,
            'contains': ConditionOperator.Contains,
            'not_contains': ConditionOperator.NotContains,
            'starts_with': ConditionOperator.StartsWith,
            'not_starts_with': ConditionOperator.NotStartsWith,
            'like': ConditionOperator.Like,
            'not_like': ConditionOperator.NotLike,
            'greater_than': ConditionOperator.GreaterThan,
            'less_than': ConditionOperator.LessThan,
            'greater_than_or_equal': ConditionOperator.GreaterThanOrEqualTo,
            'less_than_or_equal': ConditionOperator.LessThanOrEqualTo,
        }
        
        logical_map = {
            'and': LogicalOperator.And,
            'or': LogicalOperator.Or,
        }
        
        advanced_filters = []
        for f in filters:
            operator = operator_map.get(f.get('operator', 'equals').lower(), ConditionOperator.Equals)
            logical = logical_map.get(f.get('logical', 'and').lower(), LogicalOperator.And)
            
            advanced_filters.append(AIDataFilter(
                f['field'],
                operator,
                logical,
                f['value'],
                f.get('brackets_before', 0),
                f.get('brackets_after', 0),
                f.get('case_sensitive', False)
            ))
        
        top_n_opts = {}
        if top_n_options:
            for field_name, config in top_n_options.items():
                top_n_opts[field_name] = TopNOption(config['order_by_field'], config['n'])
        
        df = driver.get_data(subject, fields, advanced_filters, top_n_opts)
        if df is None:
            return json.dumps({"error": "no data returned"})
        return df.to_json(orient='records', date_format='iso')
    
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_chart(
    subject: str,
    row_fields: List[str],
    column_fields: List[str],
    value_fields: List[str],
    chart_type: str,
    title: str,
    filters: Optional[List[Dict[str, Any]]] = None,
    top_n_options: Optional[Dict[str, Dict[str, Any]]] = None,
    ctx: Optional[Context] = None
) -> str:
    """
    Generate a chart from inmydata and return the chart ID.
    
    Args:
        subject: Name of the inmydata subject to query
        row_fields: List of fields for chart rows
        column_fields: List of fields for chart columns
        value_fields: List of fields for chart values
        chart_type: Type of chart (bar, pie, area, column, scatter, bubble, grid)
        title: Chart title
        filters: Optional list of filters (same format as get_data)
        top_n_options: Optional dict mapping field names to TopN config
    
    Returns:
        JSON string with chart_id
    """
    from inmydata.StructuredData import StructuredDataDriver, AIDataFilter, LogicalOperator, ConditionOperator, TopNOption, ChartType
    
    try:
        tenant = os.environ.get('INMYDATA_TENANT')
        if not tenant:
            return json.dumps({"error": "INMYDATA_TENANT environment variable not set"})
        
        driver = StructuredDataDriver(tenant)
        user = os.environ.get('INMYDATA_USER', 'mcp-agent')
        session_id = os.environ.get('INMYDATA_SESSION_ID', 'mcp-session')
        driver.user = user
        driver.session_id = session_id
        
        chart_type_map = {
            'bar': ChartType.Bar,
            'pie': ChartType.Pie,
            'area': ChartType.Area,
            'column': ChartType.Column,
            'scatter': ChartType.Scatter,
            'bubble': ChartType.Bubble,
            'grid': ChartType.Grid,
        }
        
        chart_type_enum = chart_type_map.get(chart_type.lower(), ChartType.Bar)
        
        advanced_filters = []
        if filters:
            operator_map = {
                'equals': ConditionOperator.Equals,
                'not_equals': ConditionOperator.NotEquals,
                'contains': ConditionOperator.Contains,
                'not_contains': ConditionOperator.NotContains,
                'starts_with': ConditionOperator.StartsWith,
                'not_starts_with': ConditionOperator.NotStartsWith,
                'like': ConditionOperator.Like,
                'not_like': ConditionOperator.NotLike,
                'greater_than': ConditionOperator.GreaterThan,
                'less_than': ConditionOperator.LessThan,
                'greater_than_or_equal': ConditionOperator.GreaterThanOrEqualTo,
                'less_than_or_equal': ConditionOperator.LessThanOrEqualTo,
            }
            
            logical_map = {
                'and': LogicalOperator.And,
                'or': LogicalOperator.Or,
            }
            
            for f in filters:
                operator = operator_map.get(f.get('operator', 'equals').lower(), ConditionOperator.Equals)
                logical = logical_map.get(f.get('logical', 'and').lower(), LogicalOperator.And)
                
                advanced_filters.append(AIDataFilter(
                    f['field'],
                    operator,
                    logical,
                    f['value'],
                    f.get('brackets_before', 0),
                    f.get('brackets_after', 0),
                    f.get('case_sensitive', False)
                ))
        
        top_n_opts = {}
        if top_n_options:
            for field_name, config in top_n_options.items():
                top_n_opts[field_name] = TopNOption(config['order_by_field'], config['n'])
        
        chart_id = driver.get_chart(
            subject,
            row_fields,
            column_fields,
            value_fields,
            advanced_filters,
            chart_type_enum,
            title,
            top_n_opts
        )
        
        return json.dumps({"chart_id": chart_id})
    
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
        tenant = os.environ.get('INMYDATA_TENANT')
        if not tenant:
            return json.dumps({"error": "INMYDATA_TENANT environment variable not set"})
        
        driver = ConversationalDataDriver(tenant)
        
        progress_counter = 0
        
        def on_ai_question_update(caller, message):
            nonlocal progress_counter
            progress_counter += 1
            if ctx:
                import asyncio
                asyncio.create_task(ctx.report_progress(
                    progress=progress_counter,
                    message=message
                ))
        
        driver.on("ai_question_update", on_ai_question_update)
        
        if ctx:
            await ctx.info(f"Starting conversational query: {question}")
        
        answer = await driver.get_answer(question)
        
        if ctx:
            await ctx.info(f"Query completed. Subject used: {answer.subject}")
        
        return json.dumps({
            "answer": answer.answer,
            "subject": answer.subject,
            "question": question
        })
    
    except Exception as e:
        if ctx:
            await ctx.error(f"Error in get_answer: {str(e)}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_financial_year(
    target_date: Optional[str] = None,
    ctx: Optional[Context] = None
) -> str:
    """
    Get the financial year for a given date from the inmydata calendar.
    
    Args:
        target_date: Date in ISO format (YYYY-MM-DD). If not provided, uses today's date.
    
    Returns:
        JSON string with the financial year
    """
    from inmydata.CalendarAssistant import CalendarAssistant
    
    try:
        tenant = os.environ.get('INMYDATA_TENANT')
        calendar = os.environ.get('INMYDATA_CALENDAR')
        if not tenant or not calendar:
            return json.dumps({"error": "INMYDATA_TENANT and INMYDATA_CALENDAR environment variables must be set"})
        
        assistant = CalendarAssistant(tenant, calendar)
        
        if target_date:
            dt = datetime.fromisoformat(target_date).date()
        else:
            dt = date.today()
        
        financial_year = assistant.get_financial_year(dt)
        
        return json.dumps({"financial_year": financial_year, "date": dt.isoformat()})
    
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_quarter(
    target_date: Optional[str] = None,
    ctx: Optional[Context] = None
) -> str:
    """
    Get the financial quarter for a given date from the inmydata calendar.
    
    Args:
        target_date: Date in ISO format (YYYY-MM-DD). If not provided, uses today's date.
    
    Returns:
        JSON string with the quarter
    """
    from inmydata.CalendarAssistant import CalendarAssistant
    
    try:
        tenant = os.environ.get('INMYDATA_TENANT')
        calendar = os.environ.get('INMYDATA_CALENDAR')
        if not tenant or not calendar:
            return json.dumps({"error": "INMYDATA_TENANT and INMYDATA_CALENDAR environment variables must be set"})
        
        assistant = CalendarAssistant(tenant, calendar)
        
        if target_date:
            dt = datetime.fromisoformat(target_date).date()
        else:
            dt = date.today()
        
        quarter = assistant.get_quarter(dt)
        
        return json.dumps({"quarter": quarter, "date": dt.isoformat()})
    
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_month(
    target_date: Optional[str] = None,
    ctx: Optional[Context] = None
) -> str:
    """
    Get the financial month for a given date from the inmydata calendar.
    
    Args:
        target_date: Date in ISO format (YYYY-MM-DD). If not provided, uses today's date.
    
    Returns:
        JSON string with the month
    """
    from inmydata.CalendarAssistant import CalendarAssistant
    
    try:
        tenant = os.environ.get('INMYDATA_TENANT')
        calendar = os.environ.get('INMYDATA_CALENDAR')
        if not tenant or not calendar:
            return json.dumps({"error": "INMYDATA_TENANT and INMYDATA_CALENDAR environment variables must be set"})
        
        assistant = CalendarAssistant(tenant, calendar)
        
        if target_date:
            dt = datetime.fromisoformat(target_date).date()
        else:
            dt = date.today()
        
        month = assistant.get_month(dt)
        
        return json.dumps({"month": month, "date": dt.isoformat()})
    
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_week_number(
    target_date: Optional[str] = None,
    ctx: Optional[Context] = None
) -> str:
    """
    Get the financial week number for a given date from the inmydata calendar.
    
    Args:
        target_date: Date in ISO format (YYYY-MM-DD). If not provided, uses today's date.
    
    Returns:
        JSON string with the week number
    """
    from inmydata.CalendarAssistant import CalendarAssistant
    
    try:
        tenant = os.environ.get('INMYDATA_TENANT')
        calendar = os.environ.get('INMYDATA_CALENDAR')
        if not tenant or not calendar:
            return json.dumps({"error": "INMYDATA_TENANT and INMYDATA_CALENDAR environment variables must be set"})
        
        assistant = CalendarAssistant(tenant, calendar)
        
        if target_date:
            dt = datetime.fromisoformat(target_date).date()
        else:
            dt = date.today()
        
        week_number = assistant.get_week_number(dt)
        
        return json.dumps({"week_number": week_number, "date": dt.isoformat()})
    
    except Exception as e:
        return json.dumps({"error": str(e)})


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
    from inmydata.CalendarAssistant import CalendarAssistant
    
    try:
        tenant = os.environ.get('INMYDATA_TENANT')
        calendar = os.environ.get('INMYDATA_CALENDAR')
        if not tenant or not calendar:
            return json.dumps({"error": "INMYDATA_TENANT and INMYDATA_CALENDAR environment variables must be set"})
        
        assistant = CalendarAssistant(tenant, calendar)
        
        if target_date:
            dt = datetime.fromisoformat(target_date).date()
        else:
            dt = date.today()
        
        periods = assistant.get_financial_periods(dt)

        # Convert SDK/domain objects to JSON-serializable primitives
        try:
            serializable = _to_primitive(periods)
        except Exception:
            serializable = str(periods)

        return json.dumps({"periods": serializable, "date": dt.isoformat()})
    
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
    from inmydata.CalendarAssistant import CalendarAssistant, CalendarPeriodType
    
    try:
        tenant = os.environ.get('INMYDATA_TENANT')
        calendar = os.environ.get('INMYDATA_CALENDAR')
        if not tenant or not calendar:
            return json.dumps({"error": "INMYDATA_TENANT and INMYDATA_CALENDAR environment variables must be set"})
        
        assistant = CalendarAssistant(tenant, calendar)
        
        period_type_map = {
            'year': CalendarPeriodType.year,
            'month': CalendarPeriodType.month,
            'quarter': CalendarPeriodType.quarter,
            'week': CalendarPeriodType.week,
        }
        
        period_type_enum = period_type_map.get(period_type.lower())
        if not period_type_enum:
            return json.dumps({"error": f"Invalid period_type: {period_type}. Must be one of: year, month, quarter, week"})
        
        response = assistant.get_calendar_period_date_range(financial_year, period_number, period_type_enum)
        
        if response is None:
            return json.dumps({"error": "No date range found for the specified period"})
        
        return json.dumps({
            "start_date": response.StartDate.isoformat(),
            "end_date": response.EndDate.isoformat(),
            "financial_year": financial_year,
            "period_number": period_number,
            "period_type": period_type
        })
    
    except Exception as e:
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    mcp.run()  # starts STDIO transport and blocks