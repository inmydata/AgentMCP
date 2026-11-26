"""Tool descriptions, parameter types and server instructions shared by both MCP servers.

Responsibilities:
  - Hold the description text MCP clients see for each inmydata tool.
  - Define typed, documented tool parameters (subject, fields, filters,
    period type) so clients see allowed values in the tool's JSON schema.
  - Hold the server-level instructions describing the usual order of calls.

Consumers:
  - server.py (local stdio server)
  - server_remote.py (remote HTTP server)
  - tests/test_tool_descriptions.py

Notes:
  - The descriptions are written for a model calling the tools for the first
    time. Each fact in them is there because, without it, a caller would make
    a failing or silently wrong call. Behaviour described here was checked
    against the inmydata API; when the API changes, update the text and the
    tests in tests/test_tool_descriptions.py.
  - Name other tools and parameters only where a caller needs them. The tests
    check that every tool and parameter named here exists.
  - Output examples appear only where a caller must plan a next step from the
    output's layout. Each follows a line starting "Example result", must be
    valid JSON, and uses <placeholder> values so they are not mistaken for
    data. The tests check its keys against what mcp_utils really returns.
  - The MCP library checks each call's arguments against the published JSON
    schema before the tool runs, so only the spellings in the schema are
    accepted (for example "equals", not "=" or "eq"). Pydantic aliases or
    "before" validators here would never be reached.
"""
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Server instructions
# ---------------------------------------------------------------------------

SERVER_INSTRUCTIONS = """\
Tools for querying a tenant's inmydata data.

Usual order of calls:
1. get_schema, once per conversation, to find the subject and field names.
2. For relative or named periods ("this month", "last quarter", "financial year 2025"),
   get_financial_periods or get_calendar_period_date_range. Periods follow the tenant's
   financial calendar, which may not match calendar months.
3. get_rows_fast for figures broken down by any fields, or get_top_n_fast for rankings.
4. If that result has a non-blank instance_id, its data holds only a sample. Use
   query_results_fast with that instance_id to work with every row.
Use get_answer_slow only for questions that need an explanation rather than figures.

The data and calendar tools return JSON. If one fails, the result is {"error": "..."},
sometimes with a correlation_id. agentic_rag_query returns plain text, and its errors start
with "error:". A call whose arguments do not match the tool's schema is refused before the
tool runs, with a message naming the problem. Correct argument-validation errors and retry.
For other errors, follow the message: authentication or configuration may require user or
operator action. If only "internal error" and a correlation_id are returned, report that id
instead of guessing argument changes or repeatedly retrying.
"""

# ---------------------------------------------------------------------------
# Parameter types
# ---------------------------------------------------------------------------

FilterOp = Literal[
    "equals", "not_equals", "contains", "not_contains", "starts_with", "gt", "gte", "lt", "lte"
]


class WhereCondition(BaseModel):
    """One filter condition in the ``where`` list of get_rows_fast and get_top_n_fast.

    Unknown keys are refused (the published schema has additionalProperties
    false). mcp_utils.parse_where also reads older keys such as "logic" and
    "column", and "logic" would override "logical"; refusing extras keeps
    callers to the keys documented here.
    """

    model_config = ConfigDict(extra="forbid")

    field: str = Field(
        description="Field name, copied exactly from get_schema. May be a dimension or a metric.",
    )
    op: FilterOp = Field(
        default="equals",
        description=(
            "Comparison. contains and not_contains match text anywhere in the value, and "
            "starts_with matches the beginning of it (no wildcards needed). For dates, use "
            "gte and lt, not equals or lte (see the tool description)."
        ),
    )
    value: Union[bool, int, float, str] = Field(
        description=(
            "A single value to compare with. Dates as YYYY-MM-DD. Not a list: for several "
            "values, use one condition per value joined with OR, in brackets."
        ),
    )
    case_insensitive: bool = Field(
        default=True,
        description="Text comparisons ignore case unless this is false.",
    )
    logical: Literal["AND", "OR"] = Field(
        default="AND",
        description="How this condition joins the one before it. Ignored on the first condition.",
    )
    start_group: int = Field(
        default=0,
        description="Set to 1 to open a bracket before this condition.",
    )
    end_group: int = Field(
        default=0,
        description="Set to 1 to close a bracket after this condition.",
    )


def where_as_dicts(where: Optional[List[Any]]) -> List[Dict[str, Any]]:
    """Turn validated filter conditions back into the dicts mcp_utils.parse_where reads.

    Args:
        where: WhereCondition objects (from an MCP call) or plain dicts (from a
            direct Python call, which skips validation).

    Returns:
        One dict per condition, holding only the keys the caller gave.
    """
    return [c.model_dump(exclude_unset=True) if isinstance(c, BaseModel) else c for c in where or []]


Subject = Annotated[str, Field(
    description="Subject name, copied exactly from a subjects[].name in get_schema.",
)]
SelectFields = Annotated[List[str], Field(
    description=(
        "Fields to return: dimensions and/or metrics, each copied exactly from the subject's "
        "factFieldTypes or metricFieldTypes keys in get_schema. At least one."
    ),
)]
Where = Annotated[List[WhereCondition], Field(
    description="Filter conditions, all applied together. Leave empty for no filter.",
)]
GroupBy = Annotated[str, Field(
    description="The dimension to rank: a key of the subject's factFieldTypes in get_schema.",
)]
OrderBy = Annotated[str, Field(
    description="The metric to rank by: a key of the subject's metricFieldTypes in get_schema.",
)]
TopN = Annotated[int, Field(
    description="How many to return. Positive for the top n (highest), negative for the bottom n (lowest). Not 0.",
)]
InstanceId = Annotated[str, Field(
    description="The instance_id from a get_rows_fast or get_top_n_fast result, copied exactly.",
)]
Sql = Annotated[str, Field(
    description='DuckDB SQL reading the table my_table, with column names in double quotes.',
)]
Question = Annotated[str, Field(
    description="The full question in plain English, including any period or filter.",
)]
TargetDate = Annotated[Optional[str], Field(
    description="Date as YYYY-MM-DD, for example 2025-06-15. Leave out for today.",
)]
FinancialYear = Annotated[Optional[int], Field(
    description="Financial year, for example 2025. Leave out for the current financial year.",
)]
PeriodNumber = Annotated[Optional[int], Field(
    description=(
        "Number of the quarter, month or week within the financial year (1 for a whole year). "
        "Leave out for the current one."
    ),
)]
PeriodType = Annotated[
    Optional[Literal["year", "quarter", "month", "week"]],
    Field(description='Kind of period. Leave out for "month".'),
]

# ---------------------------------------------------------------------------
# Tool descriptions
# ---------------------------------------------------------------------------

GET_ROWS_FAST = """\
Fetch figures from one subject, totalled by the dimensions you select. Use this for most data
questions: it is much faster and cheaper than get_answer_slow.

Call get_schema first. subject and every field name must be copied exactly from it,
including case and spaces.

What comes back:
- One row for each combination of the selected dimensions (factFieldTypes), with each selected
  metric (metricFieldTypes) totalled for that combination. select=["Region", "Sales Value"]
  gives one row per region.
- Select only metrics to get one grand-total row. Select only dimensions to list their
  distinct values (useful for finding the exact values to filter on).
- Fields used in where do not need to be in select.

Filtering (where):
- A condition on a metric is applied to individual records before they are totalled, not to
  the totals. "Sales Value gte 1000" keeps only records worth 1000 or more; it does not find
  regions whose total is 1000 or more. To filter on totals, fetch the totals and filter the
  rows yourself, or with query_results_fast.
- AND and OR are not bracketed for you: A AND B OR C means (A AND B) OR C. Whenever you
  combine OR with other conditions, put the OR conditions in brackets with start_group and
  end_group. Financial year 2024 in Wales or Scotland:
    [{"field": "Financial Year", "op": "equals", "value": 2024},
     {"field": "Region", "op": "equals", "value": "Wales", "start_group": 1},
     {"field": "Region", "op": "equals", "value": "Scotland", "logical": "OR", "end_group": 1}]
- Dates are YYYY-MM-DD. To filter a day or a range of days, use gte the first day and lt the
  day after the last day; June 2024 is gte 2024-06-01 and lt 2024-07-01. This works for every
  date field. Some date fields also hold times, and a date on its own means midnight, so
  equals or lte with a date would leave out the rest of that day without any error.
- For relative periods (this month, last quarter), get the dates or period numbers from
  get_calendar_period_date_range or get_financial_periods first.
- Select only the fields you need. Results whose export would exceed 50 MB are refused, and
  selecting a detailed field (such as a transaction id) can do that. If it happens, add
  filters or select fewer fields.

Result: JSON with row_count (total rows), columns, data and instance_id.
- If instance_id is blank, data holds every row.
- If instance_id is not blank, data holds only the first few rows and a note says how many.
  The full result is saved: use query_results_fast with that instance_id to read or summarise
  every row. Do not answer from the sample alone.

Example result when only a sample is returned (values in <> are placeholders; data shortened):
  {"subject": "Sales", "row_count": 25, "columns": ["Store", "Sales Value"],
   "data": [{"Store": "<store>", "Sales Value": "<number>"}],
   "instance_id": "<uuid>",
   "note": "data holds only the first 10 of 25 rows. Use query_results_fast with this instance_id ..."}

Example (names are illustrative; use the names from get_schema). "Sales and profit by region in
financial year 2025":
  subject="Sales", select=["Region", "Sales Value", "Profit"],
  where=[{"field": "Financial Year", "op": "equals", "value": 2025}]
"""

GET_TOP_N_FAST = """\
Rank the values of one dimension by one metric and return the top or bottom n, for example the
10 stores with the highest sales. Much faster and cheaper than get_answer_slow.

Call get_schema first and copy names exactly from it, including case and spaces:
- group_by must be a dimension: a key of the subject's factFieldTypes (such as a store or
  product field).
- order_by must be a metric: a key of the subject's metricFieldTypes. A dimension here makes the
  request fail.

What comes back: exactly two columns, group_by and order_by, with the metric totalled for each
value of group_by. If you need more columns for the ranked items, use get_rows_fast with
group_by and all the metrics you need, then sort the rows.

where works exactly as in get_rows_fast. In particular, a condition on a metric applies to
individual records before they are totalled, and OR conditions combined with other conditions
must be put in brackets with start_group and end_group.

Result: JSON with ranking_type ("top" or "bottom"), n, row_count, columns, data and
instance_id. If instance_id is not blank, data holds only a sample and a note says how many
rows: use query_results_fast with that instance_id to read every row.

Example result (values in <> are placeholders; data shortened):
  {"subject": "Sales", "ranking_type": "top", "n": 10, "group_by": "Store",
   "order_by": "Sales Value", "row_count": 10, "columns": ["Store", "Sales Value"],
   "data": [{"Store": "<store>", "Sales Value": "<number>"}],
   "instance_id": ""}

Example (names are illustrative; use the names from get_schema). "Top 10 stores by sales in
financial year 2025":
  subject="Sales", group_by="Store", order_by="Sales Value", n=10,
  where=[{"field": "Financial Year", "op": "equals", "value": 2025}]
"""

QUERY_RESULTS_FAST = """\
Run SQL over the full result saved by get_rows_fast or get_top_n_fast. Use it whenever one of
those returned a non-blank instance_id: its data then holds only a sample, and this is the only
way to see or summarise every row.

- instance_id: copy it exactly from that result. If the reply says no saved result was found,
  run the original tool again to get a new instance_id.
- sql: DuckDB SQL. The only table is my_table. Its columns are the ones listed in that result's
  columns, with the same names. Put every column name in double quotes, because most contain
  spaces or capital letters: SUM("Sales Value"), not SUM(Sales Value) or SUM(sales_value).
- Only read from my_table. Statements that reach outside it (ATTACH, COPY, PRAGMA, INSTALL,
  LOAD, EXPORT, IMPORT, USE) are refused.
- Every row the query returns is sent back, with no limit. Summarise with GROUP BY or
  aggregates, or add a LIMIT, rather than SELECT * on a large result.

Result: JSON with row_count, columns, data and instance_id.

Example: total sales for each region, highest first:
  instance_id="<from the earlier result>",
  sql='SELECT "Region", SUM("Sales Value") AS total FROM my_table GROUP BY "Region" ORDER BY total DESC'
"""

GET_ANSWER_SLOW = """\
Ask a question in plain English and get a written answer from inmydata's conversational AI.
It is slow (it can take up to a minute) and expensive, so use it only when the question needs
an explanation or reasoning, for example "Why did the North region underperform in 2025?".

For figures (totals, breakdowns, rankings, filters by period or region), use get_rows_fast or
get_top_n_fast instead, even when the user asks in plain English.

It does not need get_schema: write the question in ordinary words, including any period or
filter, for example "Why did sales in Wales fall in financial year 2025?".

Result: JSON with answer (text), subject (the subject it used) and question.
"""

GET_SCHEMA = """\
List the subjects (data sets) you can query, with their dimensions and metrics. Call it once at
the start of a conversation, before get_rows_fast or get_top_n_fast, and reuse the result: those
tools need subject and field names copied exactly from here, including case and spaces.

For each entry in subjects:
- name: the value to pass as subject.
- factFieldTypes: the dimensions, which you group or filter by (such as Region or Date). The
  keys are the field names.
- metricFieldTypes: the metrics, which are totalled (such as Sales Value). The keys are the
  field names.
- aiDescription (on subjects and fields, when present): what the subject or field means. Use it
  to choose the right field.
- type: the field's data type, such as System.String, System.Int32 or System.DateTime. Filter
  values must suit it: numbers for Int32 fields, YYYY-MM-DD for DateTime fields.

dashboardHints and fieldGroups are suggestions guessed from the field names. The field lists
above are the authority.
"""

GET_FINANCIAL_PERIODS = """\
Find the financial year, quarter, month and week that a date falls in, in the tenant's financial
calendar. Financial periods may not match calendar months (financial month 1 of 2025 may start
in December 2024). Use it to turn "today" or a specific date into period numbers before
filtering on financial year or period fields from get_schema. For the start and end dates of a
period, use get_calendar_period_date_range.

Result: JSON with periods and date. periods is itself JSON text, so decode it a second time.
It decodes to the financial year (year) and the quarter, month and week numbers within it.

Example result for target_date="2025-06-15":
  {"periods": "{\\"year\\": 2025, \\"month\\": 6, \\"week\\": 24, \\"quarter\\": 2}",
   "date": "2025-06-15"}
"""

GET_CALENDAR_PERIOD_DATE_RANGE = """\
Get the first and last dates of a year, quarter, month or week in the tenant's financial
calendar. Despite the name, periods follow the financial calendar, not calendar months:
financial month 1 of 2025 may run from 2024-12-30 to 2025-01-26. To filter a date field to the
period, use gte start_date and lt the day after end_date (see get_rows_fast).

Arguments:
- For a specific period, pass all three: financial_year, period_number and period_type.
- For a whole financial year, pass financial_year and period_type="year".
- With no arguments, you get the current financial month.
- Any other missing argument is filled from today: financial_year with the current financial
  year, period_type with "month", period_number with the current number of that period type.
  So financial_year=2023, period_type="month" with no period_number gives the month of 2023
  with the same number as the current month, not the whole of 2023.

Result: JSON with start_date and end_date (YYYY-MM-DD, both included in the period), and the
financial_year, period_number and period_type that were used.
"""
