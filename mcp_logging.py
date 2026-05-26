"""
Shared logging utilities for AgentMCP.

Provides a project-wide logger plus two helpers used anywhere bearer tokens,
PATs, JWTs, or introspection payloads might otherwise reach a log sink:

  * `token_fingerprint(token)` — short, non-reversible identifier safe to log.
  * `redact(value)` — recursively strips values whose key name looks like a
    credential before the structure is logged.

See GHSA-3x42-7cvw-r9jv (issue #5).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any, Mapping


logger = logging.getLogger("agentmcp")


_REDACT_KEY_RE = re.compile(r"token|secret|password|authorization|api[_-]?key", re.IGNORECASE)
_REDACTED = "***"


def configure_logging(default_level: str = "INFO") -> None:
    """Configure the `agentmcp` logger from `AGENTMCP_LOG_LEVEL` (or the supplied default).

    Safe to call more than once: handlers are only attached the first time so
    repeated imports during tests don't duplicate output.
    """
    level_name = os.environ.get("AGENTMCP_LOG_LEVEL", default_level).upper()
    level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        logger.propagate = False


def token_fingerprint(token: str | None) -> str:
    """Return the first 8 hex chars of sha256(token), or `"-"` for empty input.

    Used in log lines where token identity matters for correlation but the
    raw token must never appear.
    """
    if not token:
        return "-"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def redact(value: Any) -> Any:
    """Return a copy of `value` with credential-like fields replaced by `"***"`.

    Recurses into dicts and lists/tuples. Anything else (including raw strings)
    is returned unchanged — callers must not pass a bare token here.
    """
    if isinstance(value, Mapping):
        return {
            k: (_REDACTED if isinstance(k, str) and _REDACT_KEY_RE.search(k) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value
