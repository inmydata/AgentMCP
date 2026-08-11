from decimal import Decimal
import hashlib
import os
import re
import tempfile
import uuid
from pathlib import Path
import duckdb
import pandas as pd
import numpy as np
from datetime import date, datetime
import json
from inmydata.StructuredData import StructuredDataDriver, AIDataFilter, LogicalOperator, ConditionOperator, TopNOption
from typing import Optional, List, Dict, Any, Tuple
from mcp.server.fastmcp import Context
import asyncio

from mcp_logging import logger
from errors import UserFacingError, tool_error_response


class InvalidInstanceId(Exception):
    """Raised when instance_id is not a well-formed UUID or resolves outside the configured DuckDB directory."""


class InvalidTenant(Exception):
    """Raised when a tenant identifier is missing/blank, so no per-tenant DuckDB namespace can be derived."""


class UnsafeSql(Exception):
    """Raised when caller-supplied SQL begins with a statement keyword that can reach outside the sandbox (file/network/extensions/config)."""


_INSTANCE_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_BLOCKED_LEAD_KEYWORDS = frozenset({
    "ATTACH", "DETACH",
    "COPY",
    "INSTALL", "LOAD",
    "PRAGMA",
    "EXPORT", "IMPORT",
    "USE",
    "CHECKPOINT",
    "SET", "RESET",
})

_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _get_duckdb_base_location() -> Path:
    """Resolved base directory for per-instance DuckDB files."""
    return Path(os.environ.get("MCP_DUCKDB_LOCATION", tempfile.gettempdir())).resolve()


def _validate_instance_id(instance_id: str) -> str:
    if not isinstance(instance_id, str) or not _INSTANCE_ID_RE.match(instance_id):
        raise InvalidInstanceId("instance_id is not a valid UUID")
    try:
        uuid.UUID(instance_id)
    except (ValueError, AttributeError, TypeError) as e:
        raise InvalidInstanceId("instance_id is not a valid UUID") from e
    return instance_id


def _tenant_namespace(tenant: str) -> str:
    """Filesystem-safe, collision-free directory name for a tenant.

    Tenant names are treated case-insensitively (matching the rest of the
    server, e.g. the ``{TENANT}_API_KEY`` env lookup). The sanitized slug keeps
    the name recognisable for operators; the digest suffix guarantees two
    distinct tenants can never share a namespace even if their slugs collide.
    """
    if not isinstance(tenant, str) or not tenant.strip():
        raise InvalidTenant("tenant is not set")
    raw = tenant.strip().lower()
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^a-z0-9_-]", "-", raw)[:40].strip("-") or "tenant"
    return f"{slug}--{digest}"


def _resolve_duckdb_path(tenant: str, instance_id: str) -> Path:
    base = _get_duckdb_base_location()
    namespace = (base / _tenant_namespace(tenant)).resolve()
    try:
        namespace.relative_to(base)
    except ValueError as e:
        # The slug is sanitized, so this only trips if the namespace directory
        # itself was replaced with a symlink pointing outside the base.
        raise InvalidTenant("tenant namespace resolves outside the configured DuckDB directory") from e
    candidate = (namespace / f"{instance_id}.duckdb").resolve()
    try:
        candidate.relative_to(namespace)
    except ValueError as e:
        raise InvalidInstanceId("instance_id resolves outside the tenant's DuckDB directory") from e
    return candidate


def _open_sandboxed_connection(path, read_only: bool = False):
    """Open a DuckDB connection with file/network access disabled and configuration locked so caller SQL cannot re-enable them."""
    con = duckdb.connect(str(path), read_only=read_only)
    try:
        con.execute("SET enable_external_access=false;")
        con.execute("SET allow_unsigned_extensions=false;")
        con.execute("SET lock_configuration=true;")
    except Exception:
        con.close()
        raise
    return con


def _strip_sql_comments(sql: str) -> str:
    sql = _BLOCK_COMMENT_RE.sub(" ", sql)
    sql = _LINE_COMMENT_RE.sub(" ", sql)
    return sql


def _split_statements(sql: str) -> List[str]:
    """Split SQL on top-level ';' while ignoring ';' inside '...' strings or "..." quoted identifiers (with doubled-quote escaping)."""
    out: List[str] = []
    buf: List[str] = []
    i = 0
    n = len(sql)
    quote: Optional[str] = None
    while i < n:
        ch = sql[i]
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
                buf.append(ch)
            elif ch == ";":
                out.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
        else:
            buf.append(ch)
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:
                    buf.append(sql[i + 1])
                    i += 1
                else:
                    quote = None
        i += 1
    if buf:
        out.append("".join(buf))
    return out


def _assert_sql_safe(sql: str) -> None:
    """Reject SQL whose statements begin with a keyword that lets DuckDB step outside the sandboxed database file."""
    if not isinstance(sql, str):
        raise UnsafeSql("sql must be a string")
    cleaned = _strip_sql_comments(sql)
    for stmt in _split_statements(cleaned):
        stripped = stmt.strip()
        if not stripped:
            continue
        m = re.match(r"[A-Za-z_]+", stripped)
        if not m:
            continue
        first = m.group(0).upper()
        if first in _BLOCKED_LEAD_KEYWORDS:
            raise UnsafeSql(f"statement starts with disallowed keyword: {first}")


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

        logger.debug(
            "Initialized mcp_utils (tenant=%s, calendar=%s, server=%s)",
            tenant, calendar, server,
        )
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

    # --- Operator normalization ---
    _LOGICAL_ALIASES = {
        # AND
        "AND": LogicalOperator.And,
        "and": LogicalOperator.And,
        # OR
        "OR": LogicalOperator.Or,
        "or": LogicalOperator.Or
    }

    def _normalize_condition_operator(self, op_raw: Optional[str]) -> ConditionOperator:
        if not op_raw:
            return ConditionOperator.Equals
        key = str(op_raw).strip().lower()
        if key not in self._OP_ALIASES:
            raise UserFacingError(f"Unsupported operator: {op_raw!r}")
        return self._OP_ALIASES[key]

    def _normalize_logical_operator(self, logic_raw: Optional[str]) -> LogicalOperator:
       if not logic_raw:
           return LogicalOperator.And
       key = str(logic_raw).strip().upper()
       if key not in self._LOGICAL_ALIASES:
           raise UserFacingError(f"Unsupported logical operator: {logic_raw!r}")
       return self._LOGICAL_ALIASES[key]
    
    def is_int(self, s: str) -> bool:
        try:
            int(s)
            return True
        except (ValueError, TypeError):
            return False

    def parse_where(
        self,
        where: Optional[List[Dict[str, Any]]]
    ) -> List[AIDataFilter]:
        """
        Convert `where` items like:
          {"field":"Region","op":"equals","value":"North","logical":"AND"},
          {"field":"Sales Value","op":"gte","value":1000,"logical":"AND"}

        into AIDataFilter instances with explicit defaults.
        """
        if not where:
            return []

        filters: List[AIDataFilter] = []

        for i, item in enumerate(where):
            # Accept a few common synonyms for keys
            field = item.get("field") or item.get("column") or item.get("name")
            if not field:
                raise UserFacingError(f"Filter at index {i} is missing 'field'")

            op = self._normalize_condition_operator(item.get("op"))
            logic = self._normalize_logical_operator(item.get("logic") or item.get("logical"))

            # Value rules:
            # - require presence (can be falsy like 0/""/False)
            if "value" not in item:
                raise UserFacingError(f"Filter for field {field!r} requires 'value'")
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
    
    def save_to_duckdb(
        self, 
        rows: pd.DataFrame, 
        total_rows: int, 
        default_limit: int = 10
    ) -> Tuple[pd.DataFrame, str]:
        """
        Saves a DataFrame to a DuckDB database if it exceeds a row limit and returns a truncated sample.

        Args:
            rows (pd.DataFrame): The DataFrame to process.
            total_rows (int): Total number of rows in the DataFrame.
            default_limit (int, optional): Default row limit. Defaults to 10.

        Returns:
            Tuple[pd.DataFrame, str, str]: (truncated DataFrame, path to DuckDB file or empty string if not saved, instance_id for DuckDB file or empty string if not saved)
        """
        strlimit = os.environ.get("MCP_SAMPLE_ROWS", str(default_limit))
        limit = int(strlimit) if self.is_int(strlimit) else default_limit

        duckdb_path = ""
        instance_id = ""

        if total_rows > limit:
            instance_id = str(uuid.uuid4())
            logger.warning("total_rows=%d exceeds threshold; data may be truncated.", total_rows)

            resolved_path = _resolve_duckdb_path(self.tenant, instance_id)
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            duckdb_path = str(resolved_path)
            con = _open_sandboxed_connection(resolved_path, read_only=False)
            try:
                con.register("rows", rows)
                con.execute("CREATE OR REPLACE TABLE my_table AS SELECT * FROM rows")
            finally:
                con.close()

            rows = rows.head(limit)
        return rows, duckdb_path, instance_id
    
    async def get_rows(
        self,
        subject: str,
        select: List[str],
        where: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """
        Retrieve rows with a simple AND-only filter list.
        where: [{"field":"Region","op":"equals","value":"North","logical":"AND"}, {"field":"Sales Value","op":"gte","value":1000,"logical":"AND"}]
        Allowed ops: equals, contains, not_contains, starts_with, gt, lt, gte, lte
        Allows logical:  AND, OR (default is AND)        
        Returns records (<= limit) and total_count if available.
        """
        try:
            if not self.tenant:
                return json.dumps({"error": "Tenant not set"})

            logger.info(
                "get_rows called (subject=%s, fields=%d, where_terms=%d)",
                subject, len(select or []), len(where or []),
            )
            driver = StructuredDataDriver(self.tenant, self.server, self.user, self.session_id, self.api_key)
            logger.debug("Calling get_rows (subject=%s, fields=%s, where=%s)", subject, select, where)
            rows = driver.get_data(subject, select, self.parse_where(where), None)
            if rows is None:
                return json.dumps({"error": "No data returned from get_data"})
            
            # Convert DataFrame to simple, LLM-friendly JSON format
            total_rows = len(rows)

            rows, duckdb_file, instanceid = self.save_to_duckdb(rows=rows, total_rows=total_rows)
            if duckdb_file != "":
                logger.debug("DuckDB database saved to: %s", duckdb_file)
            else:
                logger.debug("Data did not exceed row limit; no DuckDB file created.")
                instanceid = ""
            
            # Convert each cell to JSON-safe types
            records = [
                {str(col): self._to_json_safe(val) for col, val in row.items()}
                for row in rows.to_dict(orient="records")
            ]
            
            result = {
                "subject": subject,
                "row_count": total_rows,
                "columns": list(map(str, rows.columns)),
                "data": records,
                "instance_id": instanceid
            }

            logger.info(
                "get_rows completed (subject=%s, row_count=%d, sample_rows=%d, instance_id=%s)",
                subject, total_rows, len(records), instanceid or "-",
            )
            return json.dumps(result, ensure_ascii=False)
        except UserFacingError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return tool_error_response(exc, context="get_rows")

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

           logger.info(
               "get_top_n called (subject=%s, group_by=%s, order_by=%s, n=%s, where_terms=%d)",
               subject, group_by, order_by, n, len(where or []),
           )
           driver = StructuredDataDriver(self.tenant, self.server, self.user, self.session_id, self.api_key)
           logger.debug(
               "Calling get_top_n (subject=%s, group_by=%s, order_by=%s, n=%s, where=%s)",
               subject, group_by, order_by, n, where,
           )

           # Build a TopN filter to only show the Top 10 Sales People based on Sales Value
           TopN = TopNOption(order_by, n) # Field to order by and number of records to return (Positive for TopN, negative for BottomN)
           TopNOptions = {}
           TopNOptions[group_by] = TopN # Apply the Top N option to the group_by field

           rows = driver.get_data(subject, [group_by, order_by], self.parse_where(where), TopNOptions)
           if rows is None:
               return json.dumps({"error": "No data returned from get_top_n"})
           
           # Convert DataFrame to simple, LLM-friendly JSON format
           total_rows = len(rows)
           rows, duckdb_file, instanceid = self.save_to_duckdb(rows=rows, total_rows=total_rows)
           
           if duckdb_file != "":
               logger.debug("DuckDB database saved to: %s", duckdb_file)
           else:
               logger.debug("Data did not exceed row limit; no DuckDB file created.")
               instanceid = ""
           
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
               "data": records,
               "instance_id": instanceid
           }

           logger.info(
               "get_top_n completed (subject=%s, row_count=%d, sample_rows=%d, instance_id=%s)",
               subject, total_rows, len(records), instanceid or "-",
           )
           return json.dumps(result, ensure_ascii=False)
       except UserFacingError as exc:
           return json.dumps({"error": str(exc)})
       except Exception as exc:
           return tool_error_response(exc, context="get_top_n")

    async def query_results(
        self,
        instance_id: str,
        sql: str
    ) -> str:
        """
        Queries data in a DuckDB database fetching and loaded into that database
        by a previous tool call.
        instance_id: is the instance id of the dataset returned by the tool that created the data
        this is unique per call to the tool.
        sql: Is the sql that should be executed against the duckdb database which has a single table
        call my_table in it.
        """
        correlation_id = uuid.uuid4().hex[:8]
        try:
            validated_id = _validate_instance_id(instance_id)
            db_path = _resolve_duckdb_path(self.tenant, validated_id)
            _assert_sql_safe(sql)
        except (InvalidInstanceId, InvalidTenant, UnsafeSql) as e:
            logger.warning(
                "query_results rejected [%s]: %s (instance_id=%r)",
                correlation_id, e, instance_id,
            )
            return json.dumps({"error": "invalid request", "correlation_id": correlation_id})

        if not db_path.exists():
            logger.warning(
                "query_results instance not found [%s] (instance_id=%s)",
                correlation_id, validated_id,
            )
            return json.dumps({"error": "instance not found", "correlation_id": correlation_id})

        logger.info(
            "query_results called [%s] (instance_id=%s, sql_chars=%d)",
            correlation_id, validated_id, len(sql),
        )
        logger.debug("query_results sql [%s]: %s", correlation_id, sql)

        try:
            con = _open_sandboxed_connection(db_path, read_only=False)
            try:
                rows = con.execute(sql).df()
            finally:
                con.close()

            records = [
                {str(col): self._to_json_safe(val) for col, val in row.items()}
                for row in rows.to_dict(orient="records")
            ]
            logger.info(
                "query_results succeeded [%s] (instance_id=%s, row_count=%d)",
                correlation_id, validated_id, len(rows),
            )
            return json.dumps({
                "row_count": len(rows),
                "columns": list(map(str, rows.columns)),
                "data": records,
                "instance_id": validated_id,
            }, ensure_ascii=False)
        except Exception:
            logger.exception("query_results failed [%s] for instance_id=%s", correlation_id, validated_id)
            return json.dumps({"error": "internal error", "correlation_id": correlation_id})

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

        except UserFacingError as exc:
            if ctx:
                await ctx.error(str(exc))
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            response = tool_error_response(exc, context="get_answer")
            if ctx:
                cid = json.loads(response).get("correlation_id", "-")
                await ctx.error(f"internal error (correlation_id: {cid})")
            return response


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

        except UserFacingError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return tool_error_response(exc, context="get_schema")

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

        except UserFacingError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return tool_error_response(exc, context="get_financial_periods")


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

        except UserFacingError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return tool_error_response(exc, context="get_calendar_period_date_range")

