"""Safe error reporting for MCP tool handlers.

See GHSA-xv3j-j949-g8wx (issue #7). Tool handlers must not return raw
exception strings to the caller. Exception messages can carry filesystem
paths, internal class names, SQL fragments, upstream HTTP response bodies,
and PII -- all of which leak to the LLM and any MCP client able to read the
tool response, and the upstream-body case also opens an indirect
prompt-injection channel.

Two pieces:

  * ``UserFacingError`` -- marker base class for messages that the handler
    has deliberately constructed to be safe (e.g. argument validation,
    "tenant not set"). These propagate to the caller verbatim.
  * ``tool_error_response`` / ``log_tool_error`` -- for any other exception.
    Generate a short correlation id, log the full traceback under it, and
    return only ``{"error": "internal error", "correlation_id": "<id>"}``
    (or the bare id, for tools whose response is plain text). The caller
    quotes the id to an operator, who looks up the full detail in the log.
"""
from __future__ import annotations

import json
import uuid

from mcp_logging import logger


class UserFacingError(Exception):
    """An exception whose ``str()`` is safe to surface to the MCP caller.

    Raise this for argument validation and other handler-constructed
    messages. Anything else should propagate to ``tool_error_response``
    so the original exception text stays on the server side.
    """


def log_tool_error(exc: BaseException, *, context: str) -> str:
    """Log ``exc`` with a fresh correlation id and return the id.

    Use this when the caller needs to embed the id in a non-JSON response
    shape. The full traceback is recorded at ERROR level against the
    ``agentmcp`` logger; the returned id is the only thing safe to send back.
    """
    cid = uuid.uuid4().hex[:8]
    logger.error("tool error in %s (cid=%s)", context, cid, exc_info=exc)
    return cid


def tool_error_response(exc: BaseException, *, context: str) -> str:
    """Log ``exc`` and return a sanitised JSON error payload.

    Standard use from a tool handler::

        except UserFacingError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return tool_error_response(exc, context="get_rows")
    """
    cid = log_tool_error(exc, context=context)
    return json.dumps({"error": "internal error", "correlation_id": cid})
