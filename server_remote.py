import json
import os
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
from fastapi.responses import JSONResponse
from fastmcp import FastMCP, Context
from fastapi import FastAPI
from mcp_utils import mcp_utils
from fastmcp.server.dependencies import get_http_headers, get_http_request
from pydantic import AnyHttpUrl
from pat_jwt_auth import PATAwareJWTVerifier, PATSupportingRemoteAuthProvider

#get environment variables from .env file if available
load_dotenv(".env", override=True)

#get the following from environment variables:
INMYDATA_MCP_HOST = os.environ.get('INMYDATA_MCP_HOST', 'mcp.inmydata.com')
INMYDATA_SERVER = os.environ.get('INMYDATA_SERVER', 'inmydata.com')
INMYDATA_AUTH_SERVER = os.environ.get('INMYDATA_AUTH_SERVER', 'https://auth.inmydata.com')
INMYDATA_USE_OAUTH = os.environ.get('INMYDATA_USE_OAUTH', 'false').lower() == 'true'
INMYDATA_INTROSPECTION_CLIENT_ID = os.environ.get('INMYDATA_INTROSPECTION_CLIENT_ID', '')
INMYDATA_INTROSPECTION_CLIENT_SECRET = os.environ.get('INMYDATA_INTROSPECTION_CLIENT_SECRET', '')
INMYDATA_TOKEN_CACHE_TTL = int(os.environ.get('INMYDATA_TOKEN_CACHE_TTL', '300'))  # Default 5 minutes

# Configure token validation for your identity provider with PAT support
token_verifier = PATAwareJWTVerifier(
    jwks_uri=f"https://{INMYDATA_AUTH_SERVER}/.well-known/openid-configuration/jwks",
    issuer=f"https://{INMYDATA_AUTH_SERVER}",
    audience=f"https://{INMYDATA_MCP_HOST}/mcp",
    introspection_endpoint=f"https://{INMYDATA_AUTH_SERVER}/connect/introspect",
    client_id=INMYDATA_INTROSPECTION_CLIENT_ID,
    client_secret=INMYDATA_INTROSPECTION_CLIENT_SECRET,
    cache_ttl_seconds=INMYDATA_TOKEN_CACHE_TTL
)

# Define the auth server that the auth provider will use
auth_servers = [AnyHttpUrl(f"https://{INMYDATA_AUTH_SERVER}")]

# Create the remote auth provider
auth = PATSupportingRemoteAuthProvider(
    token_verifier=token_verifier,
    authorization_servers=auth_servers,
    base_url=f"https://{INMYDATA_MCP_HOST}"  # Your server base URL
    # Optional: customize allowed client redirect URIs (defaults to localhost only)
    #allowed_client_redirect_uris=["http://localhost:*", "http://127.0.0.1:*","https://chatgpt.com/connector_platform_oauth_redirect", "https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback"]
)


if INMYDATA_USE_OAUTH:
    # Initialise FastMCP, and mount to FastAPI app that provides custom auth endpoints
    mcp = FastMCP(name="inmydata-agent-server", auth=auth)
    mcp_app = mcp.http_app("/")
    app = FastAPI(lifespan=mcp_app.lifespan)
    app.mount("/mcp", mcp_app)

    #--- Custom OAuth endpoints ---
    @app.get("/.well-known/oauth-protected-resource/mcp")
    @app.get("/.well-known/oauth-protected-resource")
    def oauth_protected_resource():
        return {"resource": f"https://{INMYDATA_MCP_HOST}/mcp", "authorization_servers": [f"https://{INMYDATA_AUTH_SERVER}/"], "scopes_supported": ["openid", "profile", "inmydata.Developer.AI"], "bearer_methods_supported": ["header"]}
    # Connectors are failing to go to the correct endpoints when we only offer /.well-known/oauth-protected-resource.  Serving the endpoint metadata here allows us to fix this.
    @app.get("/.well-known/oauth-authorization-server")
    @app.get("/.well-known/oauth-authorization-server/mcp")
    @app.get("/.well-known/openid-configuration")
    @app.get("/.well-known/openid-configuration/mcp")
    @app.get("/mcp/.well-known/openid-configuration")
    async def oauth_metadata():
        return {
            "issuer": f"https://{INMYDATA_AUTH_SERVER}/",
            "authorization_endpoint": f"https://{INMYDATA_AUTH_SERVER}/connect/authorize",
            "token_endpoint": f"https://{INMYDATA_MCP_HOST}/connect/token",
            "registration_endpoint": f"https://{INMYDATA_AUTH_SERVER}/register",
            "grant_types_supported": ["authorization_code", "refresh_token"],
              "scopes_supported": [
                "openid",
                "profile",
                "inmydata.Developer.AI"
            ],
            "response_types_supported": ["code"],
            "token_endpoint_auth_methods_supported": ["client_secret_post"],
             "revocation_endpoint": f"https://{INMYDATA_MCP_HOST}/revoke",
             "revocation_endpoint_auth_methods_supported": [
                "client_secret_post"
            ],
             "code_challenge_methods_supported": [
                "S256"
            ]
        }

    # For reasons I don't understand, connectors fail when going to the auth server's token endpoint directly.  This proxy seems to fix it.
    @app.post("/connect/token")
    async def token_endpoint_post(request: Request):
        """Handle POST requests to the token endpoint"""
        # Get form data from the request body
        form_data = await request.form()

        # Prepare headers for form data
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded'
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://{INMYDATA_AUTH_SERVER}/connect/token",
                data=dict(form_data),
                headers=headers

            )

            return JSONResponse(
                content=response.json(),
                status_code=response.status_code,
                headers=dict(response.headers)
            )    
else:
    # Initialise FastMCP without auth, and mount to FastAPI app
    mcp = FastMCP(name="inmydata-agent-server")
    app = mcp.streamable_http_app()

async def get_tenant(token: str) -> str:
    access_token = await token_verifier.verify_token(token)

    if access_token is None:
        raise RuntimeError("Invalid token")

    tenant = access_token.claims.get("client_imd_tenant")
    # Fallback to imd_tenant if client_imd_tenant is not present
    if not tenant:
        tenant = access_token.claims.get("imd_tenant")
    
    if not tenant:
        raise RuntimeError("No tenant information found in token")
    
    return tenant

async def utils() -> mcp_utils:
    try:
        if INMYDATA_USE_OAUTH:
            # OAuth flow - use bearer token and extract tenant from token
            headers = get_http_headers()
            api_key = headers.get('authorization', '').replace('Bearer ', '')
            tenant = headers.get('x-inmydata-tenant', await get_tenant(api_key))
            server = headers.get('x-inmydata-server', "")
            calendar = headers.get('x-inmydata-calendar', 'Default')
            user = headers.get('x-inmydata-user', 'mcp-agent')
            session_id = headers.get('x-inmydata-session-id', 'mcp-session')
            return mcp_utils(api_key, tenant, calendar, user, session_id, server)
        else:
            # Legacy flow - use API key from headers or environment variables
            # Fetch headers and request (if available). Preference: query parameter 'tenant' > header 'x-inmydata-tenant'
            headers = get_http_headers()
            tenant = ''
            try:
                req = get_http_request()
                if req is not None:
                    tenant = req.query_params.get('tenant', '')
            except Exception:
                # If get_http_request isn't available or fails, ignore and fall back to headers
                tenant = ''

            # Only use header tenant if query param not provided
            if not tenant:
                tenant = headers.get('x-inmydata-tenant', '')

            # Check if we can pick up the api key for this tenant from env first, otherwise look for header
            api_key = ""
            if tenant.upper() + "_API_KEY" in os.environ:
                api_key = os.environ.get(tenant.upper() + "_API_KEY", "")
            else:
                api_key = headers.get('authorization', '').replace('Bearer ', '')
        

            server = headers.get('x-inmydata-server', '')
            calendar = headers.get('x-inmydata-calendar', '')
            if not calendar:
                calendar = 'Default'
            user = headers.get('x-inmydata-user', 'mcp-agent')
            session_id = headers.get('x-inmydata-session-id', 'mcp-session')

            return mcp_utils(api_key, tenant, calendar, user, session_id, server)
    except Exception as e:
        raise RuntimeError(f"Error initializing mcp_utils: {e}")

@mcp.tool()
async def get_rows_fast(
    subject: str = "",
    select: List[str] = [],
    where: List[Dict[str, Any]] = []
) -> str:
    """
    FAST PATH (recommended).
    Use when the request names specific fields and simple filters (no free-form reasoning).
    Returns rows immediately from the warehouse; far faster and cheaper than get_answer.

    Examples:
    - "Give me the specific average transaction value and profit margin percentage for each region in 2025"
      -> get_rows(
           subject="Sales",
           select=["Region", "Average Transaction Value", "Profit Margin %"],
           where=[{"field":"Financial Year","op":"equals","value":2025}]
         )

    where items: [{"field":"Region","op":"equals","value":"North"}, {"field":"Sales Value","op":"gte","value":1000}]
    Allowed ops: equals, contains, not_contains, starts_with, gt, lt, gte, lte
    """
    try:
        if not subject:
            return json.dumps({"error": "subject parameter is required"})
        if not select:
            return json.dumps({"error": "select parameter is required (list of field names)"})
        return await (await utils()).get_rows(subject, select, where)
    except Exception as e:
        return json.dumps({"error": str(e)})

@mcp.tool()
async def get_top_n_fast(
    subject: str = "",
    group_by: str = "",
    order_by: str = "",
    n: int = 10,
    where: List[Dict[str, Any]] = []
) -> str:
   """
    FAST PATH for rankings and leaderboards.
    Use when the user asks for "top/bottom N" by a metric (no free-form reasoning).
    Much faster and cheaper than get_answer.

    Example:
    - "Top 10 regions by profit margin in 2025"
      -> get_top_n(subject="Sales", group_by="Region", order_by="Profit Margin %", n=10,
                   where=[{"field":"Financial Year","op":"equals","value":2025}])
    """
   try:
       if not subject:
           return json.dumps({"error": "subject parameter is required"})
       if not group_by:
           return json.dumps({"error": "group_by parameter is required"})
       if not order_by:
           return json.dumps({"error": "order_by parameter is required"})
       return await (await utils()).get_top_n(subject, group_by, order_by, n, where)
   except Exception as e:
       return json.dumps({"error": str(e)}) 

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
        if not ctx:
            return json.dumps({"error": "context parameter is required"})
        return await (await utils()).get_answer(question, ctx) # type: ignore
    
    except Exception as e:
        if ctx:
            await ctx.error(f"Error in get_answer: {str(e)}")
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_schema() -> str:
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
        return (await utils()).get_schema()

    except Exception as e:
        # Mirror your C# error string style
        return f"Error retrieving subjects: {e}"

@mcp.tool()
async def get_financial_periods(
    target_date: Optional[str] = None
) -> str:
    """
    Get all financial periods (year, quarter, month, week) for a given date.
    
    Args:
        target_date: Date in ISO format (YYYY-MM-DD). If not provided, uses today's date.
    
    Returns:
        JSON string with all financial periods
    """
    try:
        return await (await utils()).get_financial_periods(target_date)
    
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def get_calendar_period_date_range(
    financial_year: Optional[int] = None,
    period_number: Optional[int] = None,
    period_type: Optional[str] = None
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
        # If any parameter is None, fetch current financial periods
        if financial_year is None or period_number is None or period_type is None:
            periods_result = await (await utils()).get_financial_periods(None)
            periods_data = json.loads(periods_result)
            
            if "error" in periods_data:
                return periods_result
            
            # Parse the periods JSON
            periods_str = periods_data.get("periods", "{}")
            try:
                periods = json.loads(periods_str) if isinstance(periods_str, str) else periods_str
            except:
                periods = {}
            
            # Auto-fill missing parameters from current periods
            if financial_year is None:
                financial_year = periods.get("FinancialYear", periods.get("Year", 0))
            
            if period_number is None:
                # Default to current month if not specified
                period_number = periods.get("Month", periods.get("Period", 1))
            
            if period_type is None:
                period_type = "month"  # Default to month
        
        if not financial_year:
            return json.dumps({"error": "Could not determine financial_year"})
        if not period_number:
            return json.dumps({"error": "Could not determine period_number"})
        if not period_type:
            return json.dumps({"error": "period_type parameter is required"})
            
        return await (await utils()).get_calendar_period_date_range(financial_year, period_number, period_type)
    
    except Exception as e:
        return json.dumps({"error": str(e)})

#--- Custom OAuth endpoints ---
@app.get("/.well-known/oauth-protected-resource/mcp")
@app.get("/.well-known/oauth-protected-resource")
def oauth_protected_resource():
    return {"resource": f"https://{INMYDATA_MCP_HOST}/mcp", "authorization_servers": [f"https://{INMYDATA_AUTH_SERVER}/"], "scopes_supported": ["openid", "profile", "inmydata.Developer.AI"], "bearer_methods_supported": ["header"]}

# Connectors are failing to go to the correct endpoints when we only offer /.well-known/oauth-protected-resource.  Serving the endpoint metadata here allows us to fix this.
@app.get("/.well-known/oauth-authorization-server")
@app.get("/.well-known/oauth-authorization-server/mcp")
@app.get("/.well-known/openid-configuration")
@app.get("/.well-known/openid-configuration/mcp")
@app.get("/mcp/.well-known/openid-configuration")
async def oauth_metadata():
    return {
        "issuer": f"https://{INMYDATA_AUTH_SERVER}/",
        "authorization_endpoint": f"{INMYDATA_AUTH_SERVER}/connect/authorize",
        "token_endpoint": f"https://{INMYDATA_MCP_HOST}/connect/token",
        "registration_endpoint": f"https://{INMYDATA_AUTH_SERVER}/register",
        "grant_types_supported": ["authorization_code", "refresh_token"],
          "scopes_supported": [
            "openid",
            "profile",
            "inmydata.Developer.AI"
        ],
        "response_types_supported": ["code"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
         "revocation_endpoint": f"https://{INMYDATA_MCP_HOST}/revoke",
         "revocation_endpoint_auth_methods_supported": [
            "client_secret_post"
        ],
         "code_challenge_methods_supported": [
            "S256"
        ]
    }


from starlette.requests import Request

# For reasons I don't understand, connectors fail when going to the auth server's token endpoint directly.  This proxy seems to fix it.
@app.post("/connect/token")
async def token_endpoint_post(request: Request):
    """Handle POST requests to the token endpoint"""
    # Get form data from the request body
    form_data = await request.form()
    
    # Prepare headers for form data
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://{INMYDATA_AUTH_SERVER}/connect/token",
            data=dict(form_data),
            headers=headers
        
        )
        
        return JSONResponse(
            content=response.json(),
            status_code=response.status_code,
            headers=dict(response.headers)
        )





if __name__ == "__main__":
    import sys
    import uvicorn
    import httpx
    
    port = 8000
    
    if len(sys.argv) > 1:
        if sys.argv[1] in ["sse", "streamable-http"]:
            transport = sys.argv[1]
        if len(sys.argv) > 2:
            port = int(sys.argv[2])
    
    if INMYDATA_USE_OAUTH:
        print(f"Starting MCP server with OAuth and streamable-http transport on port {port}")
        print("Connectors should use OAuth to authenticate via the /mcp endpoint.")
    else:
        app = mcp.streamable_http_app()
        print(f"Starting MCP server with streamable-http transport on port {port}")
        print("Credentials should be passed via headers:")
        print("  Authorization: Your API key, prefixed with 'Bearer '")
        print("  x-inmydata-tenant: Your tenant name")
        print("  x-inmydata-server: Server name (optional, default: inmydata.com)")
        print("  x-inmydata-calendar: Your calendar name")
        print("  x-inmydata-user: User for events (optional, default: mcp-agent)")
        print("  x-inmydata-session-id: Session ID (optional, default: mcp-session)")
    
    uvicorn.run(app, host="0.0.0.0", port=port, ws="none")

    print(f"Starting MCP server with {transport} transport on port {port}")
    print("Credentials should be passed via headers:")
    print("  Authorization: Your API key, prefixed with 'Bearer '.  Leave unset to trigger interactive login")
    print("  x-inmydata-tenant: Your tenant name")
    print("  x-inmydata-server: Server name (optional, default: inmydata.com)")
    print("  x-inmydata-calendar: Your calendar name")
    print("  x-inmydata-user: User for events (optional, default: mcp-agent)")
    print("  x-inmydata-session-id: Session ID (optional, default: mcp-session)")

    
    if transport == "sse":
        #app = mcp.sse_app()
        uvicorn.run(app, host="0.0.0.0", port=port, ws="none")
    elif transport == "streamable-http":
        #app = mcp.streamable_http_app()
        uvicorn.run(app, host="0.0.0.0", port=port, ws="none")
    else:
        mcp.run(transport="stdio")

