"""Safe error reporting for MCP tool handlers.

See GHSA-xv3j-j949-g8wx (issue #7). Tool handlers must not return raw
exception strings to the caller. Exception messages can carry filesystem
paths, internal class names, SQL fragments, upstream HTTP response bodies,
and PII -- all of which leak to the LLM and any MCP client able to read the
tool response, and the upstream-body case also opens an indirect
prompt-injection channel.

Three pieces:

  * ``UserFacingError`` -- marker base class for messages that the handler
    has deliberately constructed to be safe (e.g. argument validation,
    "tenant not set"). These propagate to the caller verbatim.
  * ``describe_error`` -- turns the errors the caller can act on into a safe
    message: inmydata API errors (by HTTP status) and failures to reach the
    API. Only a 4xx client-error message written by the API is passed on, and
    then without the request URL, flattened to one line and capped in length;
    every other message is fixed text written here.
  * ``tool_error_response`` / ``log_tool_error`` -- for any exception. Generate
    a short correlation id and log the detail under it. The caller gets
    ``{"error": <message>, "correlation_id": "<id>"}``, where the message comes
    from ``describe_error``, or is ``"internal error"`` for anything it does not
    recognise (or just the bare id, for tools whose response is plain text).
    The caller quotes the id to an operator, who looks up the full detail in
    the log.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Optional

import requests
from inmydata.exceptions import (
    InmydataAccessDeniedError,
    InmydataAPIError,
    InmydataAuthenticationError,
    InmydataResponseError,
    InmydataServerError,
)

from mcp_logging import logger


INTERNAL_ERROR = "internal error"

# Longest API-written message passed on to the caller.
MAX_API_MESSAGE_CHARS = 300

# Tools whose 400 responses are almost always a wrong subject or field name.
_SCHEMA_HINT_CONTEXTS = frozenset({"get_rows", "get_top_n", "get_rows_fast", "get_top_n_fast"})
_SCHEMA_HINT = (
    " Call get_schema to see the subjects and field names available; names must "
    "match the schema exactly, including case and spaces."
)

# The SDK's message is "<url> returned <status>: <response body>".
_SDK_MESSAGE_RE = re.compile(r"^\S+ returned \d{3}: (.*)\Z", re.DOTALL)
# The platform's 5xx bodies end "Contact support with reference <hex>."
_SUPPORT_REFERENCE_RE = re.compile(r"\breference ([0-9a-fA-F]{8,64})\b")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")


class UserFacingError(Exception):
    """An exception whose ``str()`` is safe to surface to the MCP caller.

    Raise this for argument validation and other handler-constructed
    messages. Anything else should propagate to ``tool_error_response``
    so the original exception text stays on the server side.
    """


def _response_body(exc: InmydataAPIError) -> str:
    """The upstream response body from an SDK exception, without the URL."""
    match = _SDK_MESSAGE_RE.match(str(exc))
    return match.group(1) if match else ""


def _api_message(body: str) -> str:
    """The caller-facing text in an API error body, made safe to pass on.

    Handles plain text and RFC 9457 problem details (``detail``, else
    ``errors``, else ``title``). HTML error pages give "". The result is one
    line with no control characters, at most ``MAX_API_MESSAGE_CHARS`` long.
    """
    text = body.strip()
    if text.startswith("<"):
        return ""
    if text.startswith("{"):
        try:
            problem = json.loads(text)
        except ValueError:
            return ""
        if isinstance(problem, dict):
            errors = problem.get("errors")
            if problem.get("detail"):
                text = str(problem["detail"])
            elif isinstance(errors, dict) and errors:
                text = "; ".join(
                    f"{field}: {' '.join(map(str, msgs)) if isinstance(msgs, list) else msgs}"
                    for field, msgs in errors.items()
                )
            else:
                text = str(problem.get("title") or "")
    text = " ".join(_CONTROL_CHARS_RE.sub(" ", text).split())
    if len(text) > MAX_API_MESSAGE_CHARS:
        text = text[: MAX_API_MESSAGE_CHARS - 3].rstrip() + "..."
    return text


def describe_error(exc: BaseException, *, context: str) -> Optional[str]:
    """A safe, actionable message for ``exc``, or None if it is not recognised.

    Recognised: inmydata API errors (``InmydataAPIError`` and subclasses) and
    ``requests`` connection failures and timeouts. Anything else returns None
    and should be reported as ``INTERNAL_ERROR``.
    """
    if isinstance(exc, InmydataAuthenticationError):
        return (
            "The inmydata API did not accept the credentials (HTTP 401): the API key "
            "or sign-in is missing, invalid or expired. Sign in again or check the API key."
        )
    if isinstance(exc, InmydataAccessDeniedError):
        return (
            "The inmydata API refused the request (HTTP 403). The subject may not exist, "
            "may not be enabled for API access in tenant administration, or this user may "
            "not have access to it. Call get_schema to see the subjects available."
        )
    if isinstance(exc, InmydataServerError):
        message = (
            f"The inmydata service failed to process the request (HTTP {exc.status_code}). "
            "Try again; if it keeps happening, contact support"
        )
        reference = _SUPPORT_REFERENCE_RE.search(_response_body(exc))
        if reference:
            return f"{message} quoting reference {reference.group(1)}."
        return message + "."
    if isinstance(exc, InmydataResponseError):
        return (
            "The inmydata service returned a response that could not be read. "
            "Try again; if it keeps happening, contact support."
        )
    if isinstance(exc, InmydataAPIError):
        # Other 4xx (400, 404, 409, 422, ...): the API's own message, if any.
        status = exc.status_code
        api_text = _api_message(_response_body(exc))
        if api_text:
            message = f"The inmydata API rejected the request (HTTP {status}): {api_text}"
        else:
            message = f"The inmydata API rejected the request (HTTP {status})."
        if status == 400 and context in _SCHEMA_HINT_CONTEXTS:
            message += _SCHEMA_HINT
        return message
    if isinstance(exc, requests.Timeout):
        return "The inmydata service did not respond in time. Try again."
    if isinstance(exc, requests.ConnectionError):
        return (
            "Could not connect to the inmydata service. Check the server setting "
            "and that the service is reachable."
        )
    return None


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
    """Log ``exc`` and return a safe JSON error payload with a correlation id.

    Errors ``describe_error`` recognises get its message; anything else gets
    ``"internal error"``. Client errors (4xx, which the caller can fix) are
    logged at WARNING without a traceback; everything else at ERROR with one.

    Standard use from a tool handler::

        except UserFacingError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            return tool_error_response(exc, context="get_rows")
    """
    message = describe_error(exc, context=context)
    status = getattr(exc, "status_code", None)
    if message is not None and isinstance(status, int) and 400 <= status < 500:
        cid = uuid.uuid4().hex[:8]
        logger.warning("tool error in %s (cid=%s): %s", context, cid, exc)
    else:
        cid = log_tool_error(exc, context=context)
    return json.dumps({"error": message or INTERNAL_ERROR, "correlation_id": cid})


def tool_error_message(response: str) -> str:
    """The text for an MCP ``ctx.error`` notification from a ``tool_error_response`` payload."""
    payload = json.loads(response)
    return f"{payload.get('error', INTERNAL_ERROR)} (correlation_id: {payload.get('correlation_id', '-')})"
