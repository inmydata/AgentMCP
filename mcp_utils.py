from decimal import Decimal
import pandas as pd
import numpy as np
from datetime import date, datetime
import httpx
import json
from inmydata.StructuredData import StructuredDataDriver, AIDataFilter, LogicalOperator, ConditionOperator, TopNOption
from typing import Optional, List, Dict, Any
from mcp.server.fastmcp import Context



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
            server = "inmydata.com"
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
        "contains": ConditionOperator.Contains,
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
        Allowed ops: equals, contains, starts_with, gt, lt, gte, lte
        Returns records (<= limit) and total_count if available.
        """
        try:
            if not self.tenant:
                return json.dumps({"error": "Tenant not set"})

            driver = driver = StructuredDataDriver(self.tenant, self.server, self.user, self.session_id, self.api_key)
            print(f"Calling get_rows with subject={subject}, fields={select}, where={where}")
            rows = driver.get_data(subject, select, self.parse_where(where), None)
            if rows is None:
                return json.dumps({"error": "No data returned from get_data"})
            result_str = self.dataframe_to_LLM_string(rows)
            return result_str
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
           result_str = self.dataframe_to_LLM_string(rows)
           return result_str
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
                numMetrics: int
              }, ...
            ]
        """
        try:
            if not self.tenant:
               return json.dumps({"error": "Tenant not set"})

            driver = StructuredDataDriver(self.tenant, self.server, self.user, self.session_id, self.api_key)
            schema = driver.get_schema("inmydata.MCP.Server")
            if schema is None:
                return json.dumps({"error": "No schema returned from get_schema"})
            return schema

        except Exception as e:
            # Mirror your C# error string style
            return f"Error retrieving subjects: {e}"

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

            assistant = CalendarAssistant(self.tenant, self.calendar, self.server)

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
        financial_year: int,
        period_number: int,
        period_type: str
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
            if not self.tenant or not self.calendar:
                return json.dumps({"error": "Tenant and Calendar variables must be set"})

            assistant = CalendarAssistant(self.tenant, self.calendar, self.server)

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

