"""
Shared logging utilities for AgentMCP.

Provides:
- A module-level logger for all agentmcp code.
- token_fingerprint(): safely represent a token in logs (SHA-256 prefix, never full value).
- redact(): strip sensitive fields from a dict before logging.
"""
import hashlib
import logging
import re
from typing import Any

logger = logging.getLogger("agentmcp")

# Fields whose values must never appear in logs.
_REDACT_KEYS = re.compile(r"token|secret|password|authorization|api_key|bearer", re.I)
_REDACTED = "<redacted>"


def token_fingerprint(token: str) -> str:
    """Return the first 8 hex chars of the SHA-256 hash of *token*.

    Safe to include in log messages – it uniquely identifies the token for
    correlation purposes without exposing the value itself.

    Note: SHA-256 is used here for *fingerprinting / correlation*, not for
    secure password storage.  The goal is a short, collision-resistant
    identifier that lets operators correlate log lines without leaking the
    full credential.
    """
    return hashlib.sha256(token.encode()).hexdigest()[:8]  # nosec B324 – fingerprinting, not password hashing


def redact(d: Any) -> Any:
    """Recursively redact sensitive fields from a dict (or list of dicts).

    Any key matching *token*, *secret*, *password*, or *authorization*
    (case-insensitive) has its value replaced with "<redacted>".
    Other types are returned unchanged.
    """
    if isinstance(d, dict):
        return {
            k: _REDACTED if _REDACT_KEYS.search(k) else redact(v)
            for k, v in d.items()
        }
    if isinstance(d, list):
        return [redact(item) for item in d]
    return d
