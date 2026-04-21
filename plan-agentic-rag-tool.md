# Plan: Add `agentic_rag_query` tool to AgentMCP

> Implementation plan for the feature described in [mcp-query-tool.md](mcp-query-tool.md). The goal is one new MCP tool that lets an LLM query a tenant's knowledge base in Agentic RAG through this server. **No code is written yet** — this document is review-only.
>
> **Revised after review** to (1) bind `external_id` to the authenticated JWT tenant in OAuth mode instead of accepting it as a free-form LLM parameter, (2) persist the tenant-key cache to disk so restarts don't exhaust the per-tenant 20-key limit, and (3) fold in a few adjacent fixes (slugify edge case, schema-level length bounds, the latent `httpx` import bug).

---

## 1. Context snapshot

**AgentMCP (this repo)**
- Python 3.11+, uses `fastmcp==2.12.4` (both `mcp.server.fastmcp.FastMCP` in [server.py](server.py) and `fastmcp.FastMCP` in [server_remote.py](server_remote.py)).
- Two entry points share the same tool surface:
  - [server.py](server.py) — STDIO transport, local/dev. No per-request auth; tenant comes from env.
  - [server_remote.py](server_remote.py) — HTTP transport mounted on FastAPI with OAuth/PAT-aware JWT auth. **Every existing tool is tenant-scoped to the authenticated caller** — see [server_remote.py:149-213](server_remote.py#L149-L213) where tenant is derived from the `client_imd_tenant` / `imd_tenant` JWT claim or the `x-inmydata-tenant` header.
- Tools are registered with the `@mcp.tool()` decorator; the docstring is the description shown to the LLM. Representative examples: [server.py:34-75](server.py#L34-L75), [server.py:175-201](server.py#L175-L201).
- Existing async tools accept `ctx: Optional[Context]` and call `ctx.error(...)` for server-side logging — see [server.py:171-172](server.py#L171-L172).
- Config is read from `.env` via `python-dotenv` at module load — see [server.py:10-32](server.py#L10-L32) and [server_remote.py:14-24](server_remote.py#L14-L24).
- `httpx` is used in the tree (transitively via `fastmcp`/`mcp`, and directly inside [pat_jwt_auth.py](pat_jwt_auth.py)) but is **not** a declared direct dependency. `import httpx` in [server_remote.py](server_remote.py) is inside the `if __name__ == "__main__":` block at [server_remote.py:465](server_remote.py#L465), but the route handler at [server_remote.py:121-144](server_remote.py#L121-L144) references `httpx` at module scope — any non-`__main__` entry (e.g. `uvicorn server_remote:app`) will NameError on first `/connect/token` request. We will (a) add `httpx` as an explicit direct dependency and (b) move `import httpx` to the top of the module, in its own commit, as part of this change.
- Dependencies managed in [pyproject.toml](pyproject.toml) / [requirements.txt](requirements.txt).
- Existing tools return JSON-stringified errors: `json.dumps({"error": str(e)})` — see [server.py:75](server.py#L75). The new tool deliberately returns plain-text errors (`error: <detail>`), because spec §5 prescribes a compact text response shape. This is an intentional divergence, called out here and in §6.10 so it isn't later "fixed" by a reviewer.

**Agentic RAG (sibling repo, for reference only — do not import from it)**
- FastAPI service, default base URL `http://localhost:8000`, health at `GET /health`.
- Endpoints and contracts in [mcp-query-tool.md](mcp-query-tool.md) were verified against the actual implementation (`/admin/tenants/provision`, `/admin/tenants/{id}/api-keys`, `/v1/query`). The spec is accurate — no deviations to plan for.
- `Retry-After` is set on 429 responses.
- 20 active keys per tenant limit is enforced.

**Auth model we must preserve** (from the spec, §2)
- `psk_live_…` — platform service key, stays on the MCP server, used only for `/admin/*`.
- `rak_live_…` — tenant API key, cached per `external_id`, used only for `/v1/query`.
- Plaintext tenant keys are returned *once* at creation/mint — they must be captured and cached immediately or they are lost.

**Tenancy model we must preserve** (specific to this repo)
- In OAuth mode the caller's tenant is **established by the JWT**, not chosen by the LLM. Every other tool obeys this. The RAG tool must too, or an authenticated caller for tenant A can direct its LLM to query tenant B's corpus.

---

## 2. Scope

**In scope**
- One new MCP tool `agentic_rag_query(query, session_id?)` — with `external_id` present *only* in deployments where neither OAuth nor a server-wide default tenant can provide it (see §5).
- Tenant-key provisioning + caching (platform service key → tenant `rak_live_` key).
- **File-backed cache** for `{external_id: {tenant_id, api_key}}` — survives restart so we don't burn one of the 20 per-tenant key slots on every restart. Acceptable for single-replica deployments (spec §9 allows this tier). Multi-replica using a shared volume gets the same benefit for free; true multi-region/HA still needs a secret manager (out of scope).
- Cross-tenant isolation in OAuth mode: `external_id` derived from the same JWT claim used by other tools (`client_imd_tenant` / `imd_tenant`), optionally namespaced via `AGENTIC_RAG_TENANT_PREFIX` so the AgentMCP tenant namespace and the Agentic-RAG tenant namespace don't collide.
- Registration in both [server.py](server.py) and [server_remote.py](server_remote.py).
- `httpx` upgrade from transitive to direct dependency + move the `import httpx` in [server_remote.py](server_remote.py) to module scope (separate commit — see §9).
- Environment-based config.

**Out of scope** (do not add — the spec §1 is explicit)
- Upload/ingestion flow.
- Admin UI or admin tools for tenant management.
- Streaming (`stream: false` is mandated — spec §7).
- Key *rotation* tooling beyond the forced re-mint on 401 and the operator-actionable error path in §6.4 for the 20-key-exhaustion case.
- A full shared secret-manager integration for multi-replica deployments — the file cache covers single-replica and shared-volume multi-replica; true HA stays deferred.

---

## 3. Files to change / add

| Path | Change |
|---|---|
| `agentic_rag_client.py` (new) | `AgenticRagClient` class: admin provision/mint + query, with 401-triggered re-mint and 429 surfacing. Holds the in-memory cache and manages file-backed persistence. Pure I/O module; no FastMCP imports. |
| `agentic_rag_tool.py` (new) | `register(mcp, tenant_resolver=None)` function that attaches the `agentic_rag_query` tool to a `FastMCP` instance. Picks one of three signature variants based on `tenant_resolver` / default-tenant config (§5). Holds env parsing + text formatter. |
| [server.py](server.py) | Import `agentic_rag_tool` at the top, and call `agentic_rag_tool.register(mcp)` (no `tenant_resolver` — STDIO mode has no per-request auth) after the last existing `@mcp.tool()`, just before `if __name__ == "__main__":` at [server.py:286](server.py#L286). Do **not** touch any existing `@mcp.tool()` definitions. |
| [server_remote.py](server_remote.py) | Two changes: (a) **move** `import httpx` from [server_remote.py:465](server_remote.py#L465) to the top of the file (fixes a latent NameError when loaded by uvicorn without `__main__`); (b) after the last `@mcp.tool()`, call `agentic_rag_tool.register(mcp, tenant_resolver=...)` **once**. The `tenant_resolver` argument is `None` when `INMYDATA_USE_OAUTH` is false and a callable that reads the current request's JWT/headers (mirroring [server_remote.py:149-163](server_remote.py#L149-L163)) when it is true. `mcp` is bound in both branches of the OAuth `if/else`, so registering once outside covers both modes. |
| [requirements.txt](requirements.txt) / [pyproject.toml](pyproject.toml) | Add `httpx` as an explicit direct dependency. |
| [client-config-example.json](client-config-example.json) | Add example values for the new env vars. |
| [readme.md](readme.md) | Short section documenting the new tool, its env vars, and the OAuth-mode tenant binding. |
| [.gitignore](.gitignore) | Add the default cache-file path (`agentic_rag_cache.json`) — plaintext tenant keys must not be committed. |
| `.env` (user-managed, not committed) | Add `AGENTIC_RAG_BASE_URL`, `AGENTIC_RAG_PSK`, optional `AGENTIC_RAG_DEFAULT_EXTERNAL_ID` (non-OAuth only), optional `AGENTIC_RAG_TENANT_PREFIX` (OAuth only), optional `AGENTIC_RAG_CACHE_FILE`. |
| `agentic_rag_cache.json` (runtime, gitignored) | File-backed cache. Created on first successful provision. |

**Why a separate module, not inline in `server.py`:**
1. Both [server.py](server.py) and [server_remote.py](server_remote.py) need the same tool — a shared module avoids copy/paste drift.
2. The client logic (provision → cache → query → retry-on-401, file persistence) is testable in isolation.
3. Mirrors the existing separation of concerns: tool wiring in `server*.py`, backend logic in [mcp_utils.py](mcp_utils.py).

---

## 4. Configuration

New env vars (read via `os.environ.get`, consistent with [server.py:24-29](server.py#L24-L29)):

| Var | Required | Purpose |
|---|---|---|
| `AGENTIC_RAG_BASE_URL` | Yes | e.g. `http://localhost:8000` or `https://rag.example.com`. |
| `AGENTIC_RAG_PSK` | Yes | Platform service key (`psk_live_…`). Never logged, never surfaced to the LLM. |
| `AGENTIC_RAG_DEFAULT_EXTERNAL_ID` | No | **Only honored when OAuth is off.** If set, `external_id` is dropped from the tool schema (see §5 variant B). Ignored — with a warning — when `INMYDATA_USE_OAUTH=true`; OAuth mode always derives tenant from the JWT. |
| `AGENTIC_RAG_TENANT_PREFIX` | No | **Only used when OAuth is on.** Prepended to the JWT tenant claim to form the `external_id` passed to Agentic RAG. Default: empty. Use this when the Agentic-RAG tenant namespace is distinct from the inmydata tenant namespace (e.g. `rag-prod-`). Ignored — with a warning — when OAuth is off. |
| `AGENTIC_RAG_CACHE_FILE` | No | Path to the persisted key cache. Default: `./agentic_rag_cache.json`. File is written with mode `0600` on POSIX. On Windows the caller is responsible for securing the parent directory via ACLs; the code does not attempt to set ACLs directly. |

**Missing-config behaviour:** if either required var is missing at server start-up, `register(mcp)` **still registers the tool**, but the tool returns `error: agentic_rag not configured (missing AGENTIC_RAG_BASE_URL and/or AGENTIC_RAG_PSK)` on every call. It also logs one warning at registration time. Rationale: silently dropping the tool is worse than surfacing the misconfiguration — an operator who set the env vars wrong will see the error in their logs the first time the LLM tries to use the tool, instead of wondering why the LLM never calls it.

**Misconfig combinations explicitly handled at startup (one warning each, then proceed):**
- `AGENTIC_RAG_DEFAULT_EXTERNAL_ID` set **and** OAuth on → `"AGENTIC_RAG_DEFAULT_EXTERNAL_ID is ignored in OAuth mode; tenant derived from JWT"`.
- `AGENTIC_RAG_TENANT_PREFIX` set **and** OAuth off → `"AGENTIC_RAG_TENANT_PREFIX is ignored when OAuth is disabled"`.

---

## 5. Tool contract

Matches spec §5, adapted for the three-variant registration below.

- **Name:** `agentic_rag_query`
- **Description** (docstring shown to the LLM): the wording from spec §5, including the "do not use for live data, general world knowledge, or anything outside the ingested corpus" guidance.
- **Input schema** varies by variant:
  - `query` — string, 1–10000 chars, **required**, always present.
  - `session_id` — string, optional, always present. Pass back the value returned by a prior call.
  - `external_id` — string, **present only in variant C below**.
- **Output:** compact text block (not JSON) matching spec §5, plus a trailing `<query_id>` block for support/debugging:
  ```
  <answer>{response}</answer>
  <sources>
  - score=0.087 section="Chapter 3.2.1" chunk=<chunk_id>
  ...
  </sources>
  <session_id>{session_id}</session_id>
  <query_id>{query_id}</query_id>
  ```
  `<sources>` and `<session_id>` blocks are omitted when empty/null. `<query_id>` is always included when present — cheap to emit and invaluable when a user reports a bad answer and we need to grep Agentic RAG logs.

**Three variants — one is selected at `register()` time:**

| Variant | When selected | Tool signature |
|---|---|---|
| A. OAuth-bound | `tenant_resolver` callable passed to `register()` (OAuth mode) | `agentic_rag_query(query, session_id=None, ctx=None)` — `external_id` is derived at call time from the JWT via `tenant_resolver()`. The LLM cannot override. |
| B. Default-tenant | No `tenant_resolver`, `AGENTIC_RAG_DEFAULT_EXTERNAL_ID` set | `agentic_rag_query(query, session_id=None, ctx=None)` — `external_id` is the configured default. |
| C. Explicit | No `tenant_resolver`, no default | `agentic_rag_query(external_id, query, session_id=None, ctx=None)` — `external_id` required. |

Variant A is used by `server_remote.py` when `INMYDATA_USE_OAUTH=true`. Variant B or C is used by `server.py` (STDIO) and by `server_remote.py` when OAuth is off. A third variant is necessary because `fastmcp` derives the JSON schema from the function signature — a single function with a "sometimes ignored" parameter would mislead the LLM and, worse, leave a backdoor the LLM might use to pass the wrong tenant.

**Schema-level length bounds on `query`.** Use `Annotated` + Pydantic `Field` so `fastmcp` emits `minLength: 1, maxLength: 10000` in the tool's JSON schema (spec §5 requires this). This lets the bounds reject invalid inputs at the MCP layer rather than round-tripping to Agentic RAG:

```python
from typing import Annotated, Optional
from pydantic import Field
from mcp.server.fastmcp import Context  # or fastmcp.Context for server_remote.py

QueryArg = Annotated[str, Field(
    min_length=1, max_length=10000,
    description="The question to answer from the tenant's knowledge base.",
)]

def register(mcp, tenant_resolver=None):
    if tenant_resolver is not None:
        @mcp.tool()
        async def agentic_rag_query(
            query: QueryArg,
            session_id: Optional[str] = None,
            ctx: Optional[Context] = None,
        ) -> str:
            """<spec §5 description>"""
            external_id = await _resolve_tenant(tenant_resolver)
            return await _run(external_id, query, session_id, ctx)
    elif _DEFAULT_EID:
        @mcp.tool()
        async def agentic_rag_query(
            query: QueryArg,
            session_id: Optional[str] = None,
            ctx: Optional[Context] = None,
        ) -> str:
            """<spec §5 description>"""
            return await _run(_DEFAULT_EID, query, session_id, ctx)
    else:
        @mcp.tool()
        async def agentic_rag_query(
            external_id: str,
            query: QueryArg,
            session_id: Optional[str] = None,
            ctx: Optional[Context] = None,
        ) -> str:
            """<spec §5 description>"""
            return await _run(external_id, query, session_id, ctx)
```

`ctx: Optional[Context]` matches the pattern used by other async tools ([server.py:34-75](server.py#L34-L75)); when present it's used for `ctx.error(...)` logging on failure, not passed through to Agentic RAG. `_resolve_tenant` awaits the resolver if it's async (the OAuth resolver is — see §6.9).

---

## 6. Client behaviour (the tricky bits)

### 6.1 Key acquisition (first call for an `external_id`)
1. Look up in in-process `dict[str, {tenant_id, api_key}]`, populated from the on-disk cache at startup (see §6.8). The dict and the per-key locks live on a single `_AgenticRagClient` instance.
2. On miss, acquire a **per-`external_id` `asyncio.Lock`** (lazily created under a small global `asyncio.Lock` that only guards the lock-map itself). Re-check the cache inside the per-key lock before proceeding (standard double-checked-locking pattern). This prevents two concurrent first-ever calls for the same `external_id` from both minting a key and wasting one of the 20 active keys per tenant.
3. `POST /admin/tenants/provision` with PSK, body `{external_id, name=external_id, slug=_slugify(external_id)}` (see §6.7 for slug rules).
4. If response `created: true`, use the returned `api_key` as the tenant key.
5. If response `created: false` and `api_key` is null (tenant pre-existed — either from a prior deployment or from an MCP instance that lost its cache), `POST /admin/tenants/{tenant_id}/api-keys` with `{label: "mcp-server"}` to mint a fresh key.
6. Cache `{tenant_id, api_key}` keyed by `external_id` **and persist the updated cache to the file** (§6.8), release the per-key lock.

### 6.2 Query call
- `POST /v1/query` with `Authorization: Bearer <rak_live_…>`, body `{query, session_id, stream: false}`. Spec §7 explicitly forbids streaming.
- If the caller passes `session_id=""` (empty string), normalize to `None` before sending — empty strings aren't valid session IDs and would confuse Agentic RAG.

### 6.3 Error handling (spec §6.4)

**On `/v1/query`:**
- **401:** evict cache entry (memory + file), re-run §6.1 **once**, retry the query **once**, then fail with a clean message to the LLM. Do not loop. If the re-mint step itself fails with the 20-key-limit error, surface the exhaustion message from §6.4 without further retry.
- **429:** read `Retry-After`. Per RFC 7231 the value can be either an integer (seconds) or an HTTP date. Agentic RAG sends integer seconds per spec §8, so parse as int and fall back to `"?"` on anything else — do not implement full HTTP-date parsing. Return `error: rate limited; retry in Ns` to the LLM. **Never** block-sleep — spec §8 is explicit that blind retries stack up against the per-tenant bucket (60 req/min on `/v1/query`).
- **403:** treat as a bug (means we sent PSK to `/v1/query` or vice-versa). Return the `detail` field unmodified; do not retry.
- **422:** log the `detail` array verbatim (it's structured Pydantic output); return a short summary `error: request validation failed — see server log`.
- **Other 4xx (400, 404, …):** surface `detail` as `error: <detail>`.
- **5xx:** one retry with jitter (200–500 ms). Keep the single retry — LLMs are not good at driving retry-after-N-seconds control loops, and a transient 503 shouldn't surface as a hard tool error when one retry would fix it.
- All errors return as text content (`error: …`). When `ctx` is non-null, also `await ctx.error(msg)` for server-side observability.

**On `/admin/*` calls (provision, mint):**
- **429:** admin bucket is 300 req/min per tenant (spec §8). Surface the same `rate limited; retry in Ns` error as for query — no retry, no block-sleep.
- **409 / key-limit error on mint:** specific handling — see §6.4.
- **5xx:** one retry with jitter, then surface.
- **422:** almost always a slug validation failure — log `detail` and return a short error; the slug helper in §6.7 should make this unreachable in practice.
- **Other 4xx:** surface `detail`.

### 6.4 20-key-limit exhaustion
Agentic RAG refuses mint when a tenant already has 20 active keys. Our file cache makes this unlikely (no key burn on restart), but it can still occur if:
- Operators run multiple MCP instances without sharing the cache file (each mints its own key).
- The cache file is lost (disk failure, manual deletion, non-persistent container volume).
- The 401 path re-mints during an incident.

**Design choice: surface the error, do not auto-revoke.** Auto-revoke would need a key-listing endpoint plus heuristics to decide which of the 20 keys is "safe" to drop — we could orphan an active peer key or a human-labelled one. Instead, the tool returns:

```
error: tenant key pool exhausted (20 active keys). An operator must revoke stale keys via the admin API before this tool can recover for external_id=<eid>.
```

Rotating keys is a rare operator concern, not an LLM concern, and the spec (§9) explicitly marks revocation tooling as out of scope. This is the minimum-surprise path. If operator-side revocation becomes a frequent need, revisit in a follow-up.

### 6.5 Concurrency
- Use **`asyncio.Lock`**, not `threading.Lock`. The module is fully async (`httpx.AsyncClient`, `async def` tool body), and any lock held across an `await` must cooperate with the event loop. Mixing `threading.Lock` into async code is a latent bug — don't.
- Per-`external_id` locks as described in §6.1. This matters more than it looks: even in a single-tenant deployment, an LLM can easily fire concurrent tool calls during its own reasoning, and the first run of those would otherwise double-mint.
- Create one long-lived `httpx.AsyncClient` on the `_AgenticRagClient` instance (not per-call, not per-request), built inside `register(mcp)` — see §6.8. Configure `timeout=httpx.Timeout(60.0, connect=10.0)` to bound slow RAG responses without being trigger-happy on the connect phase.
- The cache-file write is serialized behind the per-external-id lock for that entry plus a small write-lock on the file handle itself. Read-full-file → mutate → write-to-temp → `os.replace` gives atomic replacement. Fine for a file that tops out at a few KB.

### 6.6 Logging & secrets hygiene (spec §2)
- Never log `AGENTIC_RAG_PSK` or `api_key` values, even at DEBUG.
- When logging errors from admin calls, redact headers.
- Do not include PSK or tenant key in any tool output.
- The cache file holds plaintext `api_key` values — `chmod 0600` on POSIX immediately after write; on Windows, document in [readme.md](readme.md) that the parent directory must be ACL-restricted. File is added to [.gitignore](.gitignore).
- Smoke-test verification in §8.

### 6.7 Slug / name derivation
Spec §6.1 requires `name` (1–200 chars) and `slug` (regex `^[a-z][a-z0-9_]{1,48}$` — note this requires a minimum of **2** total chars, not 1; the `{1,48}` is the count of characters *after* the leading alpha). We derive both from `external_id`:

- `name = external_id` (truncated to 200 chars if ever needed — external_ids are typically short anyway).
- `slug = _slugify(external_id)`:
  - lowercase, replace every non-`[a-z0-9_]` run with `_`, strip leading/trailing `_`;
  - if empty or doesn't start with `[a-z]`, prefix `t_` (covers `"42"`, `""`, `"-acme"`);
  - **if the result is only 1 character long, pad with `_x`** to satisfy the regex's 2-char minimum (covers single-letter external_ids like `"a"`);
  - truncate to 49 chars (regex max is 48 after the leading alpha → 49 total).

Operators who want human-friendly tenant names should pre-provision out-of-band via direct admin API calls — the MCP tool is driven by LLMs, not humans, so derived values are fine here.

### 6.8 Client lifetime & cache file
Do **not** create the `_AgenticRagClient` or the `httpx.AsyncClient` at module import. Create them inside `register(mcp)`, after checking that `AGENTIC_RAG_BASE_URL` and `AGENTIC_RAG_PSK` are present. This keeps module import side-effect-free (matches the style of [server.py](server.py)'s `utils()` factory) and means the tool's "not configured" error path doesn't need a half-initialised client hanging around.

**Cache file lifecycle:**
- On `_AgenticRagClient.__init__`, attempt to load `AGENTIC_RAG_CACHE_FILE` (JSON object keyed by `external_id`, values `{tenant_id, api_key}`). On file-not-found, start with an empty dict. On parse error, log a warning, rename the bad file to `<path>.corrupt.<timestamp>`, and start empty.
- After every successful provision/mint that mutates the dict, persist (write-to-temp + `os.replace`, then `chmod 0600` on POSIX). On 401-triggered eviction, persist the deletion too.
- **No explicit shutdown hook.** `fastmcp` doesn't currently expose a clean lifecycle callback that fires reliably for both STDIO and streamable-HTTP. Since every mutation is persisted inline, process death doesn't lose data. The `httpx.AsyncClient` connection pool leaks at shutdown, but that's a process-exit issue, not a correctness one — acceptable until `fastmcp` surfaces a shutdown hook.

The reference impl in spec §10 at [mcp-query-tool.md:453-457](mcp-query-tool.md#L453-L457) uses `os.environ[...]` at import and would crash on import if vars are missing — **do not copy that pattern**.

### 6.9 OAuth-mode tenant resolution
In `server_remote.py` when `INMYDATA_USE_OAUTH=true`, define an `async` tenant-resolver callable that returns the current request's tenant as an Agentic-RAG `external_id`:

```python
async def _rag_tenant_resolver() -> str:
    headers = get_http_headers()
    token = headers.get('authorization', '').replace('Bearer ', '')
    tenant = headers.get('x-inmydata-tenant') or await get_tenant(token)
    prefix = os.environ.get('AGENTIC_RAG_TENANT_PREFIX', '')
    return f"{prefix}{tenant}"
```

This mirrors the existing `utils()` flow at [server_remote.py:167-176](server_remote.py#L167-L176). `get_tenant` already raises on missing/invalid token; we don't re-implement that check. The resolver is called **inside** the tool body (per request), not at registration time, so it always sees the current call's auth context. `agentic_rag_tool._resolve_tenant` `await`s the resolver when it is a coroutine function (detected via `asyncio.iscoroutinefunction`) and calls it directly otherwise — keeps the public `register()` API permissive.

**Why the prefix option.** The AgentMCP tenant namespace and the Agentic-RAG tenant namespace are independent. Some operators will run Agentic-RAG as a separate product with its own tenant IDs; others will mirror inmydata tenants 1:1. Defaulting to empty prefix means "1:1 mapping"; setting a prefix like `inmydata-` keeps the namespaces visibly distinct in Agentic-RAG admin UIs. See §10.10 — this default is the one question remaining open for the reviewer.

### 6.10 Output format rationale (deliberate divergence)
Every other tool in this repo returns JSON strings, including errors (`json.dumps({"error": str(e)})` — [server.py:75](server.py#L75)). The new tool returns plain-text `<answer>…</answer>` blocks and plain-text `error: …` strings. This is a deliberate divergence:
- Spec §5 explicitly prescribes the text shape (LLMs consume text better than nested JSON strings).
- Mixing the two error conventions inside one tool would be strictly worse than each tool being internally consistent.

Flagged here so a future reviewer doesn't "normalize" the new tool's errors to JSON and accidentally break the spec contract.

---

## 7. Registration — the "additive" requirement (spec §12)

Both server entry points already register tools with `@mcp.tool()` directly on the `FastMCP` instance. The registration plan:

1. Define the tool inside `agentic_rag_tool.register(mcp, tenant_resolver=None)` using the same `@mcp.tool()` decorator style as the rest of the codebase. The variant chosen (A/B/C) is based on whether `tenant_resolver` was passed and on `AGENTIC_RAG_DEFAULT_EXTERNAL_ID` — see §5.
2. Call `register(...)` **once** per entry point, after the existing tools are declared:
   - [server.py](server.py): add `import agentic_rag_tool` near [server.py:8](server.py#L8), and call `agentic_rag_tool.register(mcp)` (no resolver) just before `if __name__ == "__main__":` on [server.py:286](server.py#L286).
   - [server_remote.py](server_remote.py): a **single** `agentic_rag_tool.register(mcp, tenant_resolver=_rag_tenant_resolver if INMYDATA_USE_OAUTH else None)` call placed after the last existing `@mcp.tool()` (around line 459), before `if __name__ == "__main__":` at [server_remote.py:462](server_remote.py#L462). `mcp` is bound in both arms of the OAuth `if/else` (lines 64 and 147), so we don't duplicate the call per branch. Define `_rag_tenant_resolver` at module scope (it needs `get_http_headers`, `get_tenant`, and env access — all already available where other module-scope helpers live).
3. Do **not** replace or re-define any existing tools.

---

## 8. Testing plan

Manual, in this order:

1. **Unit-ish smoke** — mock `httpx.AsyncClient.post` and assert:
   - cache miss → provision → cache hit on second call (no second admin call);
   - `created: false, api_key: null` → triggers mint call;
   - 401 on query → one invalidate + re-mint + retry;
   - 401 → re-mint hits 20-key-limit → returns the exhaustion error without looping;
   - 429 on query surfaces `Retry-After` without sleeping;
   - 429 on admin also surfaces without sleeping;
   - two concurrent first-ever calls for the same `external_id` produce **exactly one** provision + at most one mint (per-key lock works);
   - `_slugify("42")`, `_slugify("")`, `_slugify("-Acme Corp!")`, `_slugify("a")`, `_slugify("_")` all return valid slugs matching `^[a-z][a-z0-9_]{1,48}$` — specifically, the 1-char inputs must be padded to ≥2 chars.
2. **Cache persistence** — point `AGENTIC_RAG_CACHE_FILE` at a temp path. First call provisions and writes the file. Kill the process, restart. Second call reads the file, skips provision (verified by asserting no admin HTTP calls were made via the mock). Assert the file is mode `0600` on POSIX.
3. **Cache corruption recovery** — hand-edit the cache file to invalid JSON. Start the server. Assert one warning is logged, the file is renamed with a `.corrupt.<timestamp>` suffix, and the tool works (re-provisions on first call).
4. **End-to-end against a local Agentic RAG** — run `agentic-rag` on `http://localhost:8000`, set `AGENTIC_RAG_PSK`, start AgentMCP via [server.py](server.py), invoke the tool through [test-client.py](test-client.py) with a pre-ingested corpus. Confirm the answer text, at least one source line, a `query_id`, and a non-null `session_id`.
5. **Session continuity** — call twice, passing the first response's `session_id` into the second call. Confirm Agentic RAG logs show the same session. Also pass `session_id=""` and confirm it's normalized to null (no error).
6. **Secrets-leak grep** — capture stdout/stderr of the smoke run to a file and `grep -E 'psk_live_|rak_live_'`. Expected: zero matches. Separately confirm the cache file **does** contain `rak_live_` (that's its whole purpose) but has permission `0600` on POSIX / is inside an ACL-restricted directory on Windows.
7. **Default-tenant mode (variant B) and explicit mode (variant C)** — OAuth off, set `AGENTIC_RAG_DEFAULT_EXTERNAL_ID`, restart, and confirm via `list_tools` that the `agentic_rag_query` input schema does **not** include an `external_id` property. Unset, restart, confirm the property reappears and is required. Also confirm the `query` property carries `minLength: 1` and `maxLength: 10000` in the emitted schema in both modes.
8. **OAuth-mode tenant binding (variant A — the security-critical test)** — start [server_remote.py](server_remote.py) with `INMYDATA_USE_OAUTH=true`. Authenticate as tenant A, call the tool; confirm Agentic-RAG received the call with tenant A's `external_id`. Attempt to pass `external_id=tenant_B` in the tool's arguments — confirm the schema does not list the property and (even if forced through a crafted client) the tool call still targets tenant A (because variant A's signature doesn't bind the extra argument). Switch auth to tenant B, confirm tenant B's corpus is queried. Set `AGENTIC_RAG_TENANT_PREFIX=test-` and confirm calls go to `test-<tenant>`.
9. **Misconfig paths** —
   - `AGENTIC_RAG_PSK` unset: confirm the tool is still listed, calling it returns `error: agentic_rag not configured …`, and a single warning line was logged at start-up.
   - `AGENTIC_RAG_DEFAULT_EXTERNAL_ID` set AND OAuth on: confirm the startup warning about it being ignored, and confirm JWT-derived tenant is used.
   - `AGENTIC_RAG_TENANT_PREFIX` set AND OAuth off: confirm the startup warning about it being ignored.
10. **Regression** — run each existing tool (`get_rows_fast`, `get_top_n_fast`, `query_results_fast`, `get_answer_slow`, `get_schema`, `get_financial_periods`, `get_calendar_period_date_range`) to confirm no side effects from the new module's import, the moved `httpx` import, or the `register()` call.
11. **httpx-import regression** — start [server_remote.py](server_remote.py) via `uvicorn server_remote:app` (i.e. *not* via `python server_remote.py`), hit the `/connect/token` endpoint, and confirm it no longer NameErrors. This verifies the preliminary `httpx` move.

No automated test suite exists in this repo today (there is a single [test-client.py](test-client.py) integration script). We will not introduce one as part of this change — matches the existing convention. The mocked "unit-ish" cases in step 1 can live in a scratch script under the feature branch and do not need to be committed.

---

## 9. Rollout

1. Implement on a feature branch.
2. **First commit: the `httpx` import fix** in [server_remote.py](server_remote.py) — move `import httpx` to the top of the file. Reviewable independently of the feature; it's a latent-bug fix that stands on its own.
3. Subsequent commits one-per-file for the feature (`agentic_rag_client.py`, `agentic_rag_tool.py`, server wiring in both entry points, dependency updates, `.gitignore`, docs).
4. Local smoke + end-to-end test against a running `agentic-rag`, including the OAuth-mode isolation test (§8.8).
5. Dockerfile / `docker-compose.yml` — no code change needed; env vars flow through the existing mechanism. **However:** confirm the cache-file path is under a persisted volume if running containerized. If the container is ephemeral, the file-cache benefit is lost and every restart burns a new key — which brings us back to the 20-key-limit problem §6.4 calls out.
6. Open a PR. Do **not** include `.env`, any real PSK, or the cache file in commits.

---

## 10. Open questions for the reviewer before coding starts

Most of the original and review-raised questions are resolved in the body above. Remaining items:

1. ~~**Default-tenant fallback.**~~ **Resolved** (§4, §6.9): `AGENTIC_RAG_DEFAULT_EXTERNAL_ID` applies only when OAuth is off; OAuth mode binds `external_id` to the JWT tenant (optionally prefixed).
2. ~~**Cache durability.**~~ **Resolved** (§2, §6.8): file-backed JSON cache at `AGENTIC_RAG_CACHE_FILE` (default `./agentic_rag_cache.json`). Handles single-replica restarts and shared-volume multi-replica. Full secret-manager integration remains out of scope.
3. ~~**Slug derivation.**~~ **Resolved** (§6.7), including the 1-char-input edge case.
4. ~~**Retry policy for 5xx.**~~ **Resolved** (§6.3): single jittered retry on both `/v1/query` and `/admin/*`.
5. ~~**Missing-config behaviour.**~~ **Resolved** (§4).
6. ~~**Cross-tenant isolation in OAuth mode.**~~ **Resolved** (§2, §5 variant A, §6.9): `external_id` is derived from the JWT, not accepted from the LLM, in OAuth deployments.
7. ~~**20-key-limit exhaustion.**~~ **Resolved** (§6.4): surface a clear operator-actionable error; do not auto-revoke.
8. ~~**Schema-level length bounds.**~~ **Resolved** (§5): `Annotated[str, Field(min_length=1, max_length=10000)]`.
9. ~~**Output-format divergence from existing tools.**~~ **Resolved** (§6.10): deliberate, documented.
10. **Default for `AGENTIC_RAG_TENANT_PREFIX`** — currently empty (1:1 mapping of inmydata tenant → Agentic-RAG `external_id`). Open question: should we default to a non-empty prefix (e.g. `inmydata-`) to make the namespaces visibly distinct and prevent accidental cross-product collisions? A 1:1 default is operator-friendly; a prefixed default is collision-safe. This is a product-side call — confirm before implementation.

Confirm §10.10 and implementation can proceed.
