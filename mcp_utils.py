from decimal import Decimal
import pandas as pd
import numpy as np
from datetime import date, datetime
import json
from inmydata.StructuredData import StructuredDataDriver, AIDataFilter, LogicalOperator, ConditionOperator, TopNOption
from typing import Optional, List, Dict, Any
from mcp.server.fastmcp import Context
import asyncio



class mcp_utils:
    def __init__(
            self, 
            api_key: str,
            tenant: str,
            calendar: str,
            user: str,
            session_id: str,
            server: Optional[str]):
        self.api_key = api_key
        self.tenant = tenant
        self.calendar = calendar
        self.user = user
        self.session_id = session_id
        if not server:
            self.server = "inmydata.com"
        else:
            self.server = server

        print(f"Initialized mcp_utils with tenant={tenant}, calendar={calendar}, server={server}, user={user}, session_id={session_id}")
        pass


    def _to_json_safe(self, value):
        # Normalize types Claude will see
        if pd.isna(value):
            return None
        if isinstance(value, (np.integer,)):
            return int(value)
        if isinstance(value, (np.floating,)):
            # Keep floats as floats; if you use Decimal, convert to str (below)
            return float(value)
        if isinstance(value, (np.bool_,)):
            return bool(value)
        if isinstance(value, (datetime,)):
            # ISO 8601 (assume naive are UTC; tweak if you have TZ info)
            if value.tzinfo is None:
                return value.isoformat() + "Z"
            return value.isoformat()
        if isinstance(value, (date,)):
            return value.isoformat()
        if isinstance(value, Decimal):
            # Avoid float rounding; LLMs handle numeric strings fine
            return str(value)
        return value

    def dataframe_to_LLM_string(
        self,
        df: pd.DataFrame,
        *,
        max_rows: int = 1000,
        max_chars: int = 200_000,
        include_schema: bool = True,
        markdown_preview_rows: int = 50,
    ) -> str:
        """
        Serialize a DataFrame into a JSON string that's LLM-friendly.
        - Caps rows to avoid blowing context.
        - Converts NaN -> null, datetimes -> ISO 8601, numpy types -> Python scalars.
        - Includes schema & dtypes so the model understands columns.
        - Adds a small markdown preview (as a string field) for quick glance.
        """
        total_rows = int(len(df))
        df_out = df.head(max_rows).copy()

        # Build schema & dtypes
        schema = [{"name": str(c), "dtype": str(df[c].dtype)} for c in df.columns]

        # Convert each cell to JSON-safe types
        records = [
            {str(col): self._to_json_safe(val) for col, val in row.items()}
            for row in df_out.to_dict(orient="records")
        ]

        payload = {
            "type": "dataframe",
            "row_count": total_rows,
            "returned_rows": len(df_out),
            "truncated": total_rows > len(df_out),
            "columns": list(map(str, df.columns)),
            "data": records,
        }

        if include_schema:
            payload["schema"] = schema

        # Optional small markdown preview for humans (kept inside JSON)
        try:
            preview_rows = min(markdown_preview_rows, len(df_out))
            if preview_rows > 0:
                payload["markdown_preview"] = df_out.head(preview_rows).to_markdown(index=False)
        except Exception:
            # .to_markdown requires tabulate; safe to ignore if unavailable
            pass

        s = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        # Hard cap by characters—if still too large, fall back to CSV snippet
        if len(s) > max_chars:
            csv_sample = df_out.to_csv(index=False)
            s = json.dumps({
                "type": "dataframe",
                "row_count": total_rows,
                "returned_rows": len(df_out),
                "truncated": True,
                "columns": list(map(str, df.columns)),
                "data_format": "csv",
                "csv_sample": csv_sample[: max_chars // 2]  # keep it sane
            }, ensure_ascii=False, separators=(",", ":"))

        return s

    # --- Operator normalization ---
    _OP_ALIASES = {
        # equals
        "equals": ConditionOperator.Equals,
        "eq": ConditionOperator.Equals,
        "=": ConditionOperator.Equals,
        # not equals
        "not_equals": ConditionOperator.NotEquals,
        "neq": ConditionOperator.NotEquals,
        "!=": ConditionOperator.NotEquals,
        "<>": ConditionOperator.NotEquals,
        # gt/gte/lt/lte
        "gt": ConditionOperator.GreaterThan,
        ">": ConditionOperator.GreaterThan,
        "gte": ConditionOperator.GreaterThanOrEqualTo,
        ">=": ConditionOperator.GreaterThanOrEqualTo,
        "lt": ConditionOperator.LessThan,
        "<": ConditionOperator.LessThan,
        "lte": ConditionOperator.LessThanOrEqualTo,
        "<=": ConditionOperator.LessThanOrEqualTo,
        # string-ish
        "contains": ConditionOperator.Like,
        "not_contains": ConditionOperator.NotLike,
        "starts_with": ConditionOperator.StartsWith
    }


    def _normalize_condition_operator(self, op_raw: Optional[str]) -> ConditionOperator:
        if not op_raw:
            return ConditionOperator.Equals
        key = str(op_raw).strip().lower()
        if key not in self._OP_ALIASES:
            raise ValueError(f"Unsupported operator: {op_raw!r}")
        return self._OP_ALIASES[key]


    def _normalize_logical_operator(self, logic_raw: Optional[str]) -> LogicalOperator:
        if not logic_raw:
            return LogicalOperator.And
        key = str(logic_raw).strip().upper()
        if key not in (LogicalOperator.And.value, LogicalOperator.Or.value):
            raise ValueError(f"Unsupported logical operator: {logic_raw!r}")
        return LogicalOperator[key]


    def parse_where(
        self,
        where: Optional[List[Dict[str, Any]]]
    ) -> List[AIDataFilter]:
        """
        Convert `where` items like:
          {"field":"Region","op":"equals","value":"North"}
          {"field":"Sales Value","op":"gte","value":1000}

        into AIDataFilter instances with explicit defaults.
        """
        if not where:
            return []

        filters: List[AIDataFilter] = []

        for i, item in enumerate(where):
            # Accept a few common synonyms for keys
            field = item.get("field") or item.get("column") or item.get("name")
            if not field:
                raise ValueError(f"Filter at index {i} is missing 'field'")

            op = self._normalize_condition_operator(item.get("op"))
            logic = self._normalize_logical_operator(item.get("logic") or item.get("logical"))

            # Value rules:
            # - require presence (can be falsy like 0/""/False)
            if "value" not in item:
                raise ValueError(f"Filter for field {field!r} requires 'value'")
            value = item.get("value")

            # Grouping and case-sensitivity
            start_group = int(item.get("start_group", 0))
            end_group = int(item.get("end_group", 0))
            case_insensitive = bool(item.get("case_insensitive", True))

            filters.append(
                AIDataFilter(
                    Field=field,
                    ConditionOperator=op,
                    LogicalOperator=logic,
                    Value=value,
                    StartGroup=start_group,
                    EndGroup=end_group,
                    CaseInsensitive=case_insensitive,
                )
            )

        return filters
    
    async def get_rows(
        self,
        subject: str,
        select: List[str],
        where: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """
        Retrieve rows with a simple AND-only filter list.
        where: [{"field":"Region","op":"equals","value":"North"}, {"field":"Sales Value","op":"gte","value":1000}]
        Allowed ops: equals, contains, not_contains, starts_with, gt, lt, gte, lte
        Returns records (<= limit) and total_count if available.
        """
        try:
            if not self.tenant:
                return json.dumps({"error": "Tenant not set"})

            driver = StructuredDataDriver(self.tenant, self.server, self.user, self.session_id, self.api_key)
            print(f"Calling get_rows with subject={subject}, fields={select}, where={where}")
            rows = driver.get_data(subject, select, self.parse_where(where), None)
            if rows is None:
                return json.dumps({"error": "No data returned from get_data"})
            
            # Convert DataFrame to simple, LLM-friendly JSON format
            total_rows = len(rows)
            
            # Convert each cell to JSON-safe types
            records = [
                {str(col): self._to_json_safe(val) for col, val in row.items()}
                for row in rows.to_dict(orient="records")
            ]
            
            result = {
                "subject": subject,
                "row_count": total_rows,
                "columns": list(map(str, rows.columns)),
                "data": records
            }
            
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def get_top_n(
        self,
        subject: str,
        group_by: str,
        order_by: str,
        n: int,
        where: Optional[List[Dict[str, Any]]] = None
    ) -> str:
       """
        Return top/bottom N groups by a metric.
        n>0 => top N, n<0 => bottom N.
        where uses the same shape as get_rows.
        """
       try:
           if not self.tenant:
               return json.dumps({"error": "Tenant not set"})

           driver = StructuredDataDriver(self.tenant, self.server, self.user, self.session_id, self.api_key)
           print(f"Calling get_top_n with subject={subject}, group_by={group_by}, order_by={order_by}, n={n}, where={where}")

           # Build a TopN filter to only show the Top 10 Sales People based on Sales Value
           TopN = TopNOption(order_by, n) # Field to order by and number of records to return (Positive for TopN, negative for BottomN)
           TopNOptions = {}
           TopNOptions[group_by] = TopN # Apply the Top N option to the group_by field

           rows = driver.get_data(subject, [group_by, order_by], self.parse_where(where), TopNOptions)
           if rows is None:
               return json.dumps({"error": "No data returned from get_top_n"})
           
           # Convert DataFrame to simple, LLM-friendly JSON format
           total_rows = len(rows)
           
           # Convert each cell to JSON-safe types
           records = [
               {str(col): self._to_json_safe(val) for col, val in row.items()}
               for row in rows.to_dict(orient="records")
           ]
           
           result = {
               "subject": subject,
               "ranking_type": "top" if n > 0 else "bottom",
               "n": abs(n),
               "group_by": group_by,
               "order_by": order_by,
               "row_count": total_rows,
               "columns": list(map(str, rows.columns)),
               "data": records
           }
           
           return json.dumps(result, ensure_ascii=False)
       except Exception as e:
           return json.dumps({"error": str(e)}) 

    async def get_answer(
        self,
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
            if not self.tenant:
                return json.dumps({"error": "Tenant not set"})

            driver = ConversationalDataDriver(self.tenant, self.server, self.user, self.session_id, self.api_key)

            # capture the loop that owns ctx before you register the callback
            loop = asyncio.get_running_loop()
            progress_counter = 0

            def on_ai_question_update(caller, message):
                nonlocal progress_counter
                progress_counter += 1
                if ctx:
                    # schedule the coroutine onto the captured loop, thread-safely
                    fut = asyncio.run_coroutine_threadsafe(
                        ctx.report_progress(progress=progress_counter, message=message),
                        loop
                    )

            driver.on("ai_question_update", on_ai_question_update)

            if ctx:
                await ctx.report_progress(progress=0, message=f"Starting conversational query: {question}")

            answer = await driver.get_answer(question)

            if ctx:
                await ctx.report_progress(progress=progress_counter + 1, message=f"Query completed. Subject used: {answer.subject}")


            return json.dumps({
                "answer": answer.answer,
                "subject": answer.subject,
                "question": question
            })

        except Exception as e:
            if ctx:
                await ctx.error(f"Error in get_answer: {str(e)}")
            return json.dumps({"error": str(e)})


    def get_schema(self) -> str:
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
                numMetrics: int,
                dashboardHints: {
                  recommendedTimeDimension: str,
                  recommendedMetrics: [str],
                  fastQueryFields: [str],
                  topNSupported: bool,
                  maxRowsRecommended: int
                },
                fieldGroups: {
                  timeFields: [str],
                  locationFields: [str],
                  productFields: [str],
                  ... other semantic groups
                }
              }, ...
            ]
        """
        try:
            if not self.tenant:
               return json.dumps({"error": "Tenant not set"})

            driver = StructuredDataDriver(self.tenant, self.server, self.user, self.session_id, self.api_key)
            schema_json = driver.get_schema("inmydata.MCP.Server")
            if schema_json is None:
                return json.dumps({"error": "No schema returned from get_schema"})
            
            # Parse the schema and enhance it with dashboard hints
            try:
                schema = json.loads(schema_json)
                
                # Enhance each subject with dashboard hints and field groups
                if "subjects" in schema:
                    for subject in schema["subjects"]:
                        self._add_dashboard_hints(subject)
                
                return json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
            except json.JSONDecodeError:
                # If schema is not valid JSON, return as-is
                return schema_json

        except Exception as e:
            # Mirror your C# error string style
            return f"Error retrieving subjects: {e}"

    def _add_dashboard_hints(self, subject: Dict[str, Any]) -> None:
        """
        Add dashboard hints and field groups to a subject based on field analysis.
        Modifies the subject dict in-place.
        """
        fact_fields = subject.get("factFieldTypes", {})
        metric_fields = subject.get("metricFieldTypes", {})
        
        # Categorize fields into semantic groups
        time_fields = []
        location_fields = []
        product_fields = []
        category_fields = []
        identifier_fields = []
        
        # Common time field patterns
        time_keywords = ["date", "time", "year", "month", "week", "quarter", "period", "day"]
        # Common location field patterns
        location_keywords = ["region", "country", "state", "city", "location", "store", "branch", "site", "territory"]
        # Common product field patterns
        product_keywords = ["product", "item", "sku", "article", "goods", "category", "brand"]
        # Common category field patterns
        category_keywords = ["type", "class", "group", "category", "segment", "division"]
        # Common identifier patterns
        id_keywords = ["id", "code", "number", "ref"]
        
        for field_name, field_info in fact_fields.items():
            name_lower = field_name.lower()
            
            # Categorize by semantic meaning
            if any(keyword in name_lower for keyword in time_keywords):
                time_fields.append(field_name)
            elif any(keyword in name_lower for keyword in location_keywords):
                location_fields.append(field_name)
            elif any(keyword in name_lower for keyword in product_keywords):
                product_fields.append(field_name)
            elif any(keyword in name_lower for keyword in category_keywords):
                category_fields.append(field_name)
            elif any(keyword in name_lower for keyword in id_keywords):
                identifier_fields.append(field_name)
        
        # Build field groups (only include non-empty groups)
        field_groups = {}
        if time_fields:
            field_groups["timeFields"] = time_fields
        if location_fields:
            field_groups["locationFields"] = location_fields
        if product_fields:
            field_groups["productFields"] = product_fields
        if category_fields:
            field_groups["categoryFields"] = category_fields
        if identifier_fields:
            field_groups["identifierFields"] = identifier_fields
        
        # Determine recommended time dimension (prefer Date > Week > Year > Month > Quarter)
        time_priority = ["date", "week", "year", "month", "quarter", "period"]
        recommended_time_dim = None
        for priority_word in time_priority:
            for field in time_fields:
                if priority_word in field.lower():
                    recommended_time_dim = field
                    break
            if recommended_time_dim:
                break
        
        # If no specific match, just use first time field
        if not recommended_time_dim and time_fields:
            recommended_time_dim = time_fields[0]
        
        # Select recommended metrics (prioritize common business metrics)
        metric_priority_keywords = [
            "value", "amount", "revenue", "sales", "profit", "margin", "cost",
            "quantity", "count", "total", "average", "sum"
        ]
        recommended_metrics = []
        
        # First pass: add metrics with priority keywords
        for metric_name in metric_fields.keys():
            metric_lower = metric_name.lower()
            if any(keyword in metric_lower for keyword in metric_priority_keywords):
                recommended_metrics.append(metric_name)
                if len(recommended_metrics) >= 5:  # Limit to top 5
                    break
        
        # If we don't have enough, add remaining metrics
        if len(recommended_metrics) < 3:
            for metric_name in metric_fields.keys():
                if metric_name not in recommended_metrics:
                    recommended_metrics.append(metric_name)
                    if len(recommended_metrics) >= 3:
                        break
        
        # Determine fast query fields (dimensions that are likely to be indexed/commonly used)
        fast_query_fields = []
        # Prefer time fields, then location, then product, then categories
        fast_query_fields.extend(time_fields[:3])  # Top 3 time fields
        fast_query_fields.extend(location_fields[:3])  # Top 3 location fields
        fast_query_fields.extend(product_fields[:2])  # Top 2 product fields
        
        # Build dashboard hints
        dashboard_hints = {
            "topNSupported": True,  # All subjects support top N queries
            "maxRowsRecommended": 5000
        }
        
        if recommended_time_dim:
            dashboard_hints["recommendedTimeDimension"] = recommended_time_dim
        
        if recommended_metrics:
            dashboard_hints["recommendedMetrics"] = recommended_metrics
        
        if fast_query_fields:
            dashboard_hints["fastQueryFields"] = fast_query_fields
        
        # Add to subject
        subject["dashboardHints"] = dashboard_hints
        if field_groups:
            subject["fieldGroups"] = field_groups

    async def get_financial_periods(
        self,
        target_date: Optional[str] = None
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
            if not self.tenant or not self.calendar:
                return json.dumps({"error": "Tenant and calendar must be set"})

            print("Getting financial periods. API key =", self.api_key)
            assistant = CalendarAssistant(self.tenant, self.calendar, self.server, self.api_key)

            if target_date:
                dt = datetime.fromisoformat(target_date).date()
            else:
                dt = date.today()

            periods = assistant.get_financial_periods(dt)

            # Convert SDK/domain objects to JSON-serializable primitives
            try:
                serializable = json.dumps(periods)
            except Exception:
                serializable = str(periods)

            return json.dumps({"periods": serializable, "date": dt.isoformat()})

        except Exception as e:
            return json.dumps({"error": str(e)})


    async def get_calendar_period_date_range(
        self,
        financial_year: Optional[int] = None,
        period_number: Optional[int] = None,
        period_type: Optional[str] = None
    ) -> str:
        """
        Get the start and end dates for a calendar period.

        If no parameters provided, returns date range for the current period
        (defaults to current month).

        Args:
            financial_year: The financial year (optional, defaults to current year)
            period_number: The period number (optional, defaults to current period)
            period_type: Type of period - 'year', 'month', 'quarter', 'week'
                        (optional, defaults to 'month')

        Returns:
            JSON string with start_date, end_date, and period info
        """
        from inmydata.CalendarAssistant import CalendarAssistant, CalendarPeriodType

        try:
            if not self.tenant or not self.calendar:
                return json.dumps({"error": "Tenant and Calendar variables must be set"})

            # If any parameter is missing, use current financial period
            if financial_year is None or period_number is None or period_type is None:
                # Get current date's financial period info
                current_periods_result = await self.get_financial_periods(None)
                current_periods_data = json.loads(current_periods_result)
                
                if "error" in current_periods_data:
                    return current_periods_result
                
                # Parse the periods JSON
                periods_str = current_periods_data.get("periods", "{}")
                try:
                    periods = json.loads(periods_str) if isinstance(periods_str, str) else periods_str
                except:
                    periods = {}
                
                # Set defaults based on current periods
                if financial_year is None:
                    financial_year = periods.get("FinancialYear", periods.get("Year", 0))
                
                if period_type is None:
                    period_type = "month"  # Default to month
                
                if period_number is None:
                    # Use current period number based on period_type
                    if period_type == "month":
                        period_number = periods.get("Month", periods.get("Period", 1))
                    elif period_type == "quarter":
                        period_number = periods.get("Quarter", 1)
                    elif period_type == "week":
                        period_number = periods.get("Week", 1)
                    elif period_type == "year":
                        period_number = 1  # Year period number is typically 1
                    else:
                        return json.dumps({"error": f"Invalid period_type: {period_type}. Must be one of: year, month, quarter, week"})

            # Validate we have all required values
            if not financial_year:
                return json.dumps({"error": "Could not determine financial_year"})
            if not period_number:
                return json.dumps({"error": "Could not determine period_number"})
            if not period_type:
                return json.dumps({"error": "Could not determine period_type"})

            assistant = CalendarAssistant(self.tenant, self.calendar, self.server, self.api_key)

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

