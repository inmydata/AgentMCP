"""Agentic RAG MCP tool registration.

Call ``register(mcp, tenant_resolver=None)`` on a FastMCP instance to add the
``agentic_rag_query`` tool. Three signature variants are chosen at
registration time (see plan-agentic-rag-tool.md section 5):

  A. tenant_resolver provided          -> query(query, session_id?)
     external_id is derived from the request (OAuth JWT) inside the tool.
  B. AGENTIC_RAG_DEFAULT_EXTERNAL_ID   -> query(query, session_id?)
     external_id is the configured default.
  C. neither                           -> query(external_id, query, session_id?)
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
from typing import Annotated, Any, Callable, Optional

from pydantic import Field

from agentic_rag_client import (
    AgenticRagClient,
    AgenticRagError,
    KeyLimitExhausted,
)


_log = logging.getLogger("agentic_rag")


_DESCRIPTION = (
    "Query the tenant's Agentic RAG knowledge base with a natural-language "
    "question. Returns an answer grounded in the ingested documents plus the "
    "source chunks used. Use this when the user asks about information that "
    "would live in the tenant's knowledge base. Do not use for live data, "
    "general world knowledge, or anything outside the ingested corpus."
)


QueryArg = Annotated[
    str,
    Field(
        min_length=1,
        max_length=10000,
        description="The natural-language question to answer from the tenant's knowledge base.",
    ),
]


def _format(result: dict) -> str:
    response_text = result.get("response", "")
    lines = [f"<answer>\n{response_text}\n</answer>"]
    sources = result.get("sources") or []
    if sources:
        lines.append("<sources>")
        for s in sources:
            section = s.get("section_path") or "-"
            score = s.get("score")
            chunk = s.get("chunk_id", "")
            try:
                score_str = f"{float(score):.3f}"
            except (TypeError, ValueError):
                score_str = str(score)
            lines.append(f'- score={score_str} section="{section}" chunk={chunk}')
        lines.append("</sources>")
    session_id = result.get("session_id")
    if session_id:
        lines.append(f"<session_id>{session_id}</session_id>")
    query_id = result.get("query_id")
    if query_id:
        lines.append(f"<query_id>{query_id}</query_id>")
    return "\n".join(lines)


async def _resolve_tenant(resolver: Callable[[], Any]) -> str:
    result = resolver()
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, str) or not result:
        raise AgenticRagError("could not resolve tenant from request")
    return result


def register(mcp, tenant_resolver: Optional[Callable[[], Any]] = None) -> None:
    """Attach ``agentic_rag_query`` to the given FastMCP instance.

    If ``tenant_resolver`` is provided, every call derives ``external_id`` from
    it at request time (OAuth mode) and the LLM cannot override.
    """
    base_url = os.environ.get("AGENTIC_RAG_BASE_URL", "")
    psk = os.environ.get("AGENTIC_RAG_PSK", "")
    default_eid = os.environ.get("AGENTIC_RAG_DEFAULT_EXTERNAL_ID", "") or None
    cache_file = os.environ.get("AGENTIC_RAG_CACHE_FILE", "./agentic_rag_cache.json")

    use_oauth = tenant_resolver is not None
    if use_oauth and default_eid:
        _log.warning(
            "AGENTIC_RAG_DEFAULT_EXTERNAL_ID is ignored in OAuth mode; tenant derived from JWT"
        )
        default_eid = None
    if not use_oauth and os.environ.get("AGENTIC_RAG_TENANT_PREFIX"):
        _log.warning(
            "AGENTIC_RAG_TENANT_PREFIX is ignored when OAuth is disabled"
        )

    if not base_url or not psk:
        _log.warning(
            "agentic_rag not configured (missing AGENTIC_RAG_BASE_URL and/or AGENTIC_RAG_PSK); "
            "tool will be registered but will return an error on every call"
        )
        err_msg = (
            "error: agentic_rag not configured "
            "(missing AGENTIC_RAG_BASE_URL and/or AGENTIC_RAG_PSK)"
        )
        _register_stub(mcp, err_msg, use_oauth=use_oauth, has_default=bool(default_eid))
        return

    client = AgenticRagClient(base_url=base_url, psk=psk, cache_file=cache_file)

    async def _run(external_id: str, query_text: str, session_id: Optional[str]) -> str:
        try:
            result = await client.query(external_id, query_text, session_id)
        except KeyLimitExhausted as exc:
            msg = f"error: {exc}"
            _log.error(
                "agentic_rag key-limit exhausted for external_id=%s", external_id
            )
            return msg
        except AgenticRagError as exc:
            _log.error("agentic_rag error for external_id=%s: %s", external_id, exc)
            return f"error: {exc}"
        except Exception as exc:
            _log.exception("agentic_rag unexpected error for external_id=%s", external_id)
            return f"error: {exc}"
        return _format(result)

    if tenant_resolver is not None:
        @mcp.tool()
        async def agentic_rag_query(
            query: QueryArg,
            session_id: Optional[str] = None,
        ) -> str:
            """Query the tenant's Agentic RAG knowledge base with a natural-language question.

            Returns an answer grounded in the ingested documents plus the source
            chunks used. Use this when the user asks about information that would
            live in the tenant's knowledge base. Do not use for live data,
            general world knowledge, or anything outside the ingested corpus.
            """
            try:
                external_id = await _resolve_tenant(tenant_resolver)
            except Exception as exc:
                _log.error("agentic_rag tenant resolution failed: %s", exc)
                return f"error: {exc}"
            return await _run(external_id, query, session_id)
    elif default_eid:
        resolved_eid = default_eid

        @mcp.tool()
        async def agentic_rag_query(
            query: QueryArg,
            session_id: Optional[str] = None,
        ) -> str:
            """Query the tenant's Agentic RAG knowledge base with a natural-language question.

            Returns an answer grounded in the ingested documents plus the source
            chunks used. Use this when the user asks about information that would
            live in the tenant's knowledge base. Do not use for live data,
            general world knowledge, or anything outside the ingested corpus.
            """
            return await _run(resolved_eid, query, session_id)
    else:
        @mcp.tool()
        async def agentic_rag_query(
            external_id: str,
            query: QueryArg,
            session_id: Optional[str] = None,
        ) -> str:
            """Query the tenant's Agentic RAG knowledge base with a natural-language question.

            Returns an answer grounded in the ingested documents plus the source
            chunks used. Use this when the user asks about information that would
            live in the tenant's knowledge base. Do not use for live data,
            general world knowledge, or anything outside the ingested corpus.

            Args:
                external_id: Stable identifier for the tenant whose knowledge base should be queried.
            """
            if not external_id:
                return "error: external_id is required"
            return await _run(external_id, query, session_id)


def _register_stub(mcp, err_msg: str, *, use_oauth: bool, has_default: bool) -> None:
    """Register a placeholder that always returns a misconfiguration error."""
    if use_oauth or has_default:
        @mcp.tool()
        async def agentic_rag_query(
            query: QueryArg,
            session_id: Optional[str] = None,
        ) -> str:
            """Query the tenant's Agentic RAG knowledge base (currently unavailable: see server logs)."""
            return err_msg
    else:
        @mcp.tool()
        async def agentic_rag_query(
            external_id: str,
            query: QueryArg,
            session_id: Optional[str] = None,
        ) -> str:
            """Query the tenant's Agentic RAG knowledge base (currently unavailable: see server logs)."""
            return err_msg
