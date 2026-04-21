# Adding an Agentic RAG Query Tool to an Existing MCP Server

> **Audience:** an engineer (or Claude Code) adding a new tool to an *existing* MCP server. The tool lets an LLM query a tenant's knowledge base in Agentic RAG via the `/v1/query` endpoint. The MCP server holds a platform service key and uses it to provision / look up tenant API keys, which it then caches for subsequent queries.

This document is self-contained — you should not need to read the Agentic RAG source to implement the tool. For the complementary upload flow, see `third-party-upload.md` (shares the same auth model).

---

## 1. What you are building

A single MCP tool — conventionally named `agentic_rag_query` — that:

1. Accepts a **tenant identifier** (your stable `external_id`) and a natural-language **query**.
2. Looks up the tenant's API key from an in-memory cache, or provisions/mints one on first use.
3. Calls `POST /v1/query` against Agentic RAG with that tenant key.
4. Returns the answer text plus source references to the calling LLM.

You are **not** building an upload path, an admin UI, or a multi-tool suite. Keep the tool surface narrow — one tool, one job.

---

## 2. Auth model (read this once, then never mix them up)

| Credential | Prefix | Scope | Held by |
|---|---|---|---|
| **Platform service key** | `psk_live_` | Platform-wide (admin) | MCP server process, from config |
| **Tenant API key** | `rak_live_` | One tenant | MCP server in-memory/secret cache, per tenant |

Rules:

1. **The platform service key never leaves the MCP server process.** Do not expose it as a tool input, do not log it, do not pass it to the LLM.
2. The MCP server is the only party that calls `/admin/*`. The LLM (through the tool) only ever causes `/v1/*` calls.
3. Tenant plaintext keys are returned **only once** at creation. Persist immediately (secret store or encrypted cache) — the server stores only a SHA-256 hash and cannot recover them.
4. There is no tenant header — tenant is derived from the API key.

Authorization header format in both cases:

```
Authorization: Bearer psk_live_<hex>     # admin calls
Authorization: Bearer rak_live_<hex>     # /v1/query
```

---

## 3. Flow

```
┌─────────┐          ┌────────────────────┐          ┌──────────────┐
│   LLM   │          │     MCP server     │          │ Agentic RAG  │
│         │          │ (tool: rag_query)  │          │              │
│         │──────────►  tool call:        │          │              │
│         │          │  {external_id,     │          │              │
│         │          │   query}           │          │              │
│         │          │                    │          │              │
│         │          │  cache miss?       │          │              │
│         │          │  ─────── PSK ─────►│  POST /admin/tenants/   │
│         │          │                    │         provision       │
│         │          │                    │  ──► tenant_id,         │
│         │          │  cache api_key ◄───│      rak_live_…         │
│         │          │                    │                         │
│         │          │  ─── rak_live_… ──►│  POST /v1/query         │
│         │          │                    │  ──► { response,        │
│         │          │  format result ◄───│         sources, ... }  │
│         │◄─────────│  return to LLM     │                         │
└─────────┘          └────────────────────┘          └──────────────┘
```

---

## 4. Configuration the MCP server needs

Set these via the MCP server's existing config/env mechanism. Do **not** hardcode.

| Env var | Example | Required |
|---|---|---|
| `AGENTIC_RAG_BASE_URL` | `https://rag.example.com` or `http://localhost:8000` | Yes |
| `AGENTIC_RAG_PSK` | `psk_live_…` | Yes |
| `AGENTIC_RAG_DEFAULT_EXTERNAL_ID` | `acme-prod` | Optional — makes the tool easier for the LLM if the server is effectively single-tenant |

The tenant API key cache is **runtime state**. It can be:

- An in-process dict (simplest — acceptable for single-replica MCP servers).
- A shared store (Redis / disk) if the server is replicated and you want to avoid minting a fresh key on every cold start.

---

## 5. MCP tool definition

Use whichever MCP SDK the existing server already uses. Do **not** introduce a second SDK.

**Tool name:** `agentic_rag_query`

**Description (shown to the LLM):**

> Query the tenant's Agentic RAG knowledge base with a natural-language question. Returns an answer grounded in the ingested documents plus the source chunks used. Use this when the user asks about information that would live in the tenant's knowledge base. Do not use for live data, general world knowledge, or anything outside the ingested corpus.

**Input schema (JSON Schema):**

```json
{
  "type": "object",
  "properties": {
    "external_id": {
      "type": "string",
      "description": "Stable identifier for the tenant whose knowledge base should be queried. Required unless the server has a default tenant configured."
    },
    "query": {
      "type": "string",
      "minLength": 1,
      "maxLength": 10000,
      "description": "The natural-language question to answer from the tenant's knowledge base."
    },
    "session_id": {
      "type": "string",
      "description": "Optional. Pass the session_id returned by a previous call to continue a multi-turn conversation."
    }
  },
  "required": ["query"]
}
```

If the server is single-tenant (has `AGENTIC_RAG_DEFAULT_EXTERNAL_ID` set), drop `external_id` from the schema so the LLM cannot pass the wrong one.

**Output (what the tool returns to the LLM):**

Return a compact text block, not a giant JSON dump — LLMs consume text better. Suggested shape:

```
<answer>
{response_text}
</answer>
<sources>
- score=0.87 section="Chapter 3.2.1" chunk=<chunk_id>
- score=0.72 section="Appendix A"   chunk=<chunk_id>
</sources>
<session_id>{session_id}</session_id>
```

Include the `session_id` so the LLM can pass it back on the next call for multi-turn.

---

## 6. Agentic RAG endpoints the tool uses

### 6.1 `POST /admin/tenants/provision` (idempotent)

Called by the MCP server with the **platform service key** when there is no cached tenant API key for the given `external_id`.

**Request:**

```http
POST /admin/tenants/provision
Authorization: Bearer psk_live_<hex>
Content-Type: application/json

{
  "external_id": "acme-42",
  "name": "ACME Corp",
  "slug": "acme_42"
}
```

- `external_id`: required, ≥1 char. Your stable tenant identifier.
- `name`: required, 1–200 chars.
- `slug`: required, regex `^[a-z][a-z0-9_]{1,48}$`.

**Response (first time, tenant created):**

```json
{
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "slug": "acme_42",
  "api_key": "rak_live_<hex>",
  "created": true
}
```

**Response (already existed):**

```json
{
  "tenant_id": "550e8400-…",
  "slug": "acme_42",
  "api_key": null,
  "created": false
}
```

If `created: false` and the MCP server doesn't already have an API key cached, mint one with the next endpoint.

### 6.2 `POST /admin/tenants/{tenant_id}/api-keys` (mint a new tenant key)

```http
POST /admin/tenants/{tenant_id}/api-keys
Authorization: Bearer psk_live_<hex>
Content-Type: application/json

{ "label": "mcp-server-prod" }
```

Response:

```json
{ "key_id": "…", "api_key": "rak_live_<hex>", "label": "mcp-server-prod" }
```

Limit: 20 active keys per tenant — revoke old ones before minting when rotating.

### 6.3 `POST /v1/query` (the actual query)

The only data-plane call the tool makes.

**Request:**

```http
POST /v1/query
Authorization: Bearer rak_live_<hex>
Content-Type: application/json

{
  "query": "What does the handbook say about travel expenses?",
  "session_id": null,
  "stream": false
}
```

| Field | Type | Rules |
|---|---|---|
| `query` | string | Required. 1–10000 chars. |
| `session_id` | string \| null | Optional. Pass the `session_id` from a previous response to continue a multi-turn thread. |
| `stream` | bool | Default `false`. Leave as `false` for MCP tools — see §7. |

**Response (200 OK):**

```json
{
  "query_id": "uuid",
  "response": "The handbook says…",
  "sources": [
    { "section_path": "Chapter 3.2.1", "score": 0.87, "chunk_id": "chunk-uuid" },
    { "section_path": "Appendix A",     "score": 0.72, "chunk_id": "chunk-uuid" }
  ],
  "session_id": "uuid-or-null"
}
```

- `response` is the final grounded answer. This is the text the LLM needs.
- `sources[].score` is a fused Reciprocal Rank Fusion (RRF) score — small-valued (typically ~0.01–0.05). **Do not interpret it as a cosine similarity or probability.** Treat it as a relative ranking within this one result set. Do not threshold on it in the tool.
- `sources[].section_path` is `null` for sources that do not have a heading path.
- `session_id` may be `null` if the request did not pass one and the server did not allocate one. If non-null, pass it back on the next call to continue the conversation.

### 6.4 Errors (shared by all three endpoints)

All errors return:

```json
{ "detail": "Human-readable error message" }
```

| Status | Meaning | Tool behaviour |
|---|---|---|
| 400 | Bad input (missing/malformed field) | Surface `detail` to the LLM as a tool error. |
| 401 | API key missing/invalid/revoked | Invalidate cached key, re-provision once, retry once, then fail. |
| 403 | Wrong auth type (e.g. PSK used on `/v1/query`) | Bug in the tool — do not retry. |
| 404 | Tenant / job not found | Surface as tool error. |
| 422 | Pydantic validation | Log `detail` array verbatim; surface a short summary. |
| 429 | Rate limited | See §8. |
| 5xx | Server error | Retry once with jitter; then surface. |

---

## 7. Do not enable streaming from the tool

`/v1/query` supports SSE streaming when `stream: true`. **The MCP tool should pass `stream: false`** (or omit it — `false` is default).

Reasons:

- MCP tool responses are single-shot; the LLM waits for the full response before continuing. Streaming gains nothing.
- Your MCP transport may not forward SSE cleanly.
- Token-by-token streaming makes it harder to attach the `sources` array to the response.

If you later build a UI that benefits from streaming, add it as a separate code path — not through the MCP tool.

---

## 8. Rate limits

Per-tenant, fixed-window:

- `/v1/query`: default **60 requests/minute** per tenant.
- `/admin/*` calls: default **300 requests/minute** per tenant (these are rare — provisioning and key minting only).

On `429`:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 45
```

The tool should:

1. Read `Retry-After` (seconds).
2. Surface a short error to the LLM (`"rate limited; retry in Ns"`) rather than blocking the MCP server thread for 45 seconds.
3. Let the LLM decide whether to retry on the next turn.

Do not implement blind retries inside the tool — they will stack up against the single per-tenant bucket.

---

## 9. Caching strategy for tenant API keys

**Minimum viable cache** (single-replica server):

```text
in-memory dict:  external_id  ─►  { tenant_id, api_key }
```

- Populate on first tool call for a given `external_id` via provision-then-mint-if-needed.
- Never serialise this dict to a log or telemetry.
- On `401` from `/v1/query`, evict the entry, re-provision/mint **once**, retry **once**, then fail.

**Multi-replica / restart-resilient:**

- Store `external_id → tenant_id` in a shared database (this is cheap — it's derivable via `/admin/tenants/provision` which is idempotent).
- Store `api_key` in a secret manager (AWS Secrets Manager, Vault, etc.) — not the shared DB.
- On cold start, pull both from the shared stores before accepting tool calls.

**Key rotation:** mint a new key via `/admin/tenants/{tenant_id}/api-keys`, swap it in the cache, then revoke the old one via the admin revoke endpoint (out of scope for this tool).

---

## 10. Reference implementation (Python — MCP Python SDK)

Sketch. Adapt to the shape of the existing server — do **not** copy-paste if the existing server uses a different handler registration pattern.

```python
"""Agentic RAG query tool.

Registers one MCP tool, `agentic_rag_query`, that queries a tenant's
knowledge base in Agentic RAG.

Env:
  AGENTIC_RAG_BASE_URL          required
  AGENTIC_RAG_PSK               required (platform service key)
  AGENTIC_RAG_DEFAULT_EXTERNAL_ID  optional (single-tenant servers)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from threading import Lock

import httpx

# Match the import style the existing MCP server already uses.
# This example assumes the official `mcp` Python SDK; adjust as needed.
from mcp.server import Server
from mcp.types import TextContent, Tool


@dataclass
class _TenantCreds:
    tenant_id: str
    api_key: str


class _AgenticRagClient:
    def __init__(self, base_url: str, psk: str) -> None:
        self._base = base_url.rstrip("/")
        self._psk = psk
        self._http = httpx.Client(timeout=60.0)
        self._creds: dict[str, _TenantCreds] = {}
        self._lock = Lock()

    def creds_for(self, external_id: str) -> _TenantCreds:
        with self._lock:
            if external_id in self._creds:
                return self._creds[external_id]

        # Provision (idempotent). We derive name/slug from external_id;
        # override with real values if you have them.
        slug = _slugify(external_id)
        prov = self._post_admin(
            "/admin/tenants/provision",
            {"external_id": external_id, "name": external_id, "slug": slug},
        )
        tenant_id = prov["tenant_id"]
        api_key = prov.get("api_key")
        if api_key is None:
            # Tenant already existed; mint a fresh key.
            mint = self._post_admin(
                f"/admin/tenants/{tenant_id}/api-keys",
                {"label": "mcp-server"},
            )
            api_key = mint["api_key"]

        creds = _TenantCreds(tenant_id=tenant_id, api_key=api_key)
        with self._lock:
            self._creds[external_id] = creds
        return creds

    def invalidate(self, external_id: str) -> None:
        with self._lock:
            self._creds.pop(external_id, None)

    def query(
        self, external_id: str, query: str, session_id: str | None
    ) -> dict:
        creds = self.creds_for(external_id)
        r = self._http.post(
            f"{self._base}/v1/query",
            headers={"Authorization": f"Bearer {creds.api_key}"},
            json={"query": query, "session_id": session_id, "stream": False},
        )
        if r.status_code == 401:
            self.invalidate(external_id)
            creds = self.creds_for(external_id)  # re-provision once
            r = self._http.post(
                f"{self._base}/v1/query",
                headers={"Authorization": f"Bearer {creds.api_key}"},
                json={"query": query, "session_id": session_id, "stream": False},
            )
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After", "?")
            raise RuntimeError(f"Rate limited; retry in {retry_after}s")
        r.raise_for_status()
        return r.json()

    def _post_admin(self, path: str, body: dict) -> dict:
        r = self._http.post(
            f"{self._base}{path}",
            headers={
                "Authorization": f"Bearer {self._psk}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        r.raise_for_status()
        return r.json()


def _slugify(external_id: str) -> str:
    import re

    s = re.sub(r"[^a-z0-9_]+", "_", external_id.lower()).strip("_")
    if not s or not s[0].isalpha():
        s = "t_" + s
    return s[:49]


# --- MCP registration ---------------------------------------------------

_BASE_URL = os.environ["AGENTIC_RAG_BASE_URL"]
_PSK = os.environ["AGENTIC_RAG_PSK"]
_DEFAULT_EID = os.environ.get("AGENTIC_RAG_DEFAULT_EXTERNAL_ID")

_client = _AgenticRagClient(_BASE_URL, _PSK)


def register(server: Server) -> None:
    """Register the tool on the existing MCP server."""

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="agentic_rag_query",
                description=(
                    "Query the tenant's Agentic RAG knowledge base with a "
                    "natural-language question. Returns an answer grounded "
                    "in the ingested documents plus the source chunks used. "
                    "Use when the user asks about information that would "
                    "live in the tenant's knowledge base."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "external_id": {
                            "type": "string",
                            "description": (
                                "Stable tenant identifier. "
                                + ("Optional — defaults to the server's configured tenant."
                                   if _DEFAULT_EID else "Required.")
                            ),
                        },
                        "query": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 10000,
                            "description": "The question to answer.",
                        },
                        "session_id": {
                            "type": "string",
                            "description": (
                                "Optional. Pass the session_id returned by a "
                                "previous call to continue a multi-turn thread."
                            ),
                        },
                    },
                    "required": ["query"],
                },
            )
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name != "agentic_rag_query":
            raise ValueError(f"Unknown tool: {name}")

        external_id = arguments.get("external_id") or _DEFAULT_EID
        if not external_id:
            return [TextContent(
                type="text",
                text="error: external_id is required (no default configured).",
            )]

        query = arguments["query"]
        session_id = arguments.get("session_id")

        try:
            result = _client.query(external_id, query, session_id)
        except Exception as exc:
            return [TextContent(type="text", text=f"error: {exc}")]

        return [TextContent(type="text", text=_format(result))]


def _format(result: dict) -> str:
    lines = [f"<answer>\n{result['response']}\n</answer>"]
    if result.get("sources"):
        lines.append("<sources>")
        for s in result["sources"]:
            section = s.get("section_path") or "—"
            lines.append(
                f"- score={s['score']:.3f} section=\"{section}\" chunk={s['chunk_id']}"
            )
        lines.append("</sources>")
    if result.get("session_id"):
        lines.append(f"<session_id>{result['session_id']}</session_id>")
    return "\n".join(lines)
```

---

## 11. Reference implementation (TypeScript — `@modelcontextprotocol/sdk`)

Sketch. Adapt to how the existing server registers tools.

```ts
/**
 * Agentic RAG query tool.
 *
 * Env:
 *   AGENTIC_RAG_BASE_URL          required
 *   AGENTIC_RAG_PSK               required
 *   AGENTIC_RAG_DEFAULT_EXTERNAL_ID  optional
 */
import type { Server } from "@modelcontextprotocol/sdk/server/index.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

interface TenantCreds {
  tenantId: string;
  apiKey: string;
}

interface QueryResponse {
  query_id: string;
  response: string;
  sources: { section_path: string | null; score: number; chunk_id: string }[];
  session_id: string | null;
}

class AgenticRagClient {
  private readonly base: string;
  private readonly creds = new Map<string, TenantCreds>();

  constructor(baseUrl: string, private readonly psk: string) {
    this.base = baseUrl.replace(/\/+$/, "");
  }

  async credsFor(externalId: string): Promise<TenantCreds> {
    const cached = this.creds.get(externalId);
    if (cached) return cached;

    const slug = slugify(externalId);
    const prov = await this.postAdmin<{
      tenant_id: string;
      api_key: string | null;
      created: boolean;
    }>("/admin/tenants/provision", {
      external_id: externalId,
      name: externalId,
      slug,
    });

    let apiKey = prov.api_key;
    if (apiKey === null) {
      const mint = await this.postAdmin<{ api_key: string }>(
        `/admin/tenants/${prov.tenant_id}/api-keys`,
        { label: "mcp-server" },
      );
      apiKey = mint.api_key;
    }

    const creds = { tenantId: prov.tenant_id, apiKey };
    this.creds.set(externalId, creds);
    return creds;
  }

  invalidate(externalId: string): void {
    this.creds.delete(externalId);
  }

  async query(
    externalId: string,
    query: string,
    sessionId?: string,
  ): Promise<QueryResponse> {
    let creds = await this.credsFor(externalId);
    let r = await this.callQuery(creds.apiKey, query, sessionId);

    if (r.status === 401) {
      this.invalidate(externalId);
      creds = await this.credsFor(externalId);
      r = await this.callQuery(creds.apiKey, query, sessionId);
    }

    if (r.status === 429) {
      throw new Error(
        `Rate limited; retry in ${r.headers.get("Retry-After") ?? "?"}s`,
      );
    }
    if (!r.ok) {
      const body = await r.text();
      throw new Error(`query failed: ${r.status} ${body}`);
    }
    return (await r.json()) as QueryResponse;
  }

  private callQuery(
    apiKey: string,
    query: string,
    sessionId?: string,
  ): Promise<Response> {
    return fetch(`${this.base}/v1/query`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        query,
        session_id: sessionId ?? null,
        stream: false,
      }),
    });
  }

  private async postAdmin<T>(path: string, body: unknown): Promise<T> {
    const r = await fetch(`${this.base}${path}`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${this.psk}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      throw new Error(`admin call failed: ${r.status} ${await r.text()}`);
    }
    return (await r.json()) as T;
  }
}

function slugify(externalId: string): string {
  let s = externalId.toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
  if (!s || !/^[a-z]/.test(s)) s = "t_" + s;
  return s.slice(0, 49);
}

// --- Registration ----------------------------------------------------

const BASE_URL = process.env.AGENTIC_RAG_BASE_URL!;
const PSK = process.env.AGENTIC_RAG_PSK!;
const DEFAULT_EID = process.env.AGENTIC_RAG_DEFAULT_EXTERNAL_ID;

const client = new AgenticRagClient(BASE_URL, PSK);

export function registerAgenticRagQueryTool(server: Server): void {
  // If the existing server already has list/call handlers, MERGE the
  // tool into them rather than overwriting. This example assumes a
  // fresh server.
  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: [
      {
        name: "agentic_rag_query",
        description:
          "Query the tenant's Agentic RAG knowledge base with a natural-language question. " +
          "Returns an answer grounded in the ingested documents plus the source chunks used.",
        inputSchema: {
          type: "object",
          properties: {
            external_id: {
              type: "string",
              description: DEFAULT_EID
                ? "Stable tenant identifier. Optional — defaults to the server's configured tenant."
                : "Stable tenant identifier. Required.",
            },
            query: {
              type: "string",
              minLength: 1,
              maxLength: 10000,
              description: "The question to answer.",
            },
            session_id: {
              type: "string",
              description: "Optional. Pass to continue a multi-turn thread.",
            },
          },
          required: ["query"],
        },
      },
    ],
  }));

  server.setRequestHandler(CallToolRequestSchema, async (req) => {
    if (req.params.name !== "agentic_rag_query") {
      throw new Error(`Unknown tool: ${req.params.name}`);
    }
    const args = req.params.arguments ?? {};
    const externalId = (args.external_id as string | undefined) ?? DEFAULT_EID;
    if (!externalId) {
      return {
        content: [
          { type: "text", text: "error: external_id is required (no default configured)." },
        ],
      };
    }
    const query = args.query as string;
    const sessionId = args.session_id as string | undefined;

    try {
      const result = await client.query(externalId, query, sessionId);
      return { content: [{ type: "text", text: format(result) }] };
    } catch (err) {
      return {
        content: [{ type: "text", text: `error: ${(err as Error).message}` }],
      };
    }
  });
}

function format(result: QueryResponse): string {
  const lines = [`<answer>\n${result.response}\n</answer>`];
  if (result.sources.length > 0) {
    lines.push("<sources>");
    for (const s of result.sources) {
      const section = s.section_path ?? "—";
      lines.push(
        `- score=${s.score.toFixed(3)} section="${section}" chunk=${s.chunk_id}`,
      );
    }
    lines.push("</sources>");
  }
  if (result.session_id) {
    lines.push(`<session_id>${result.session_id}</session_id>`);
  }
  return lines.join("\n");
}
```

---

## 12. Implementation checklist

- [ ] `AGENTIC_RAG_BASE_URL` and `AGENTIC_RAG_PSK` are read from env, not hardcoded.
- [ ] The platform service key is used **only** for `/admin/*` calls and never appears in tool inputs, outputs, logs, or telemetry.
- [ ] Tenant API keys are cached by `external_id` for the lifetime of the process (or persisted in a secret store for multi-replica deployments).
- [ ] First-time provisioning of an unknown `external_id` goes: `provision` → mint key if `api_key` was `null` (tenant pre-existed).
- [ ] The tool passes `stream: false` to `/v1/query`.
- [ ] `401` on `/v1/query` triggers **one** cache invalidation + re-provision + retry, then fails cleanly.
- [ ] `429` responses are surfaced to the LLM with the `Retry-After` value; the tool does not block-sleep.
- [ ] The tool registration is **additive** — it merges into the existing server's list/call handlers rather than replacing them.
- [ ] Tool description tells the LLM when to use and when not to use (in-corpus questions only).
- [ ] Response format is compact text with `<answer>`, `<sources>`, and `<session_id>` sections — not raw JSON.
- [ ] No change introduces a new SDK or HTTP client if the existing server already uses one (`httpx`, `requests`, `node-fetch`, etc.) — reuse it.
