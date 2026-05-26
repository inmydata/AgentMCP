"""
Regression tests for GHSA-3x42-7cvw-r9jv (issue #5).

Two things are guarded here:

1. No `print()` call appears in any production module — diagnostics must go
   through the project logger so log level / destination is controllable.
2. No logging or print call passes a clearly token-shaped argument (`api_key`,
   `access_token`, `bearer`, full `Authorization` header, etc.) as a positional
   argument outside the redaction helpers themselves.

Both checks are AST-based so string literals that legitimately mention these
words (docstrings, comment blocks, header-name lookups) don't trigger a false
positive — only what actually reaches a logger / print call does.
"""
import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent

# Modules where `print` is allowed. test-client.py is a manual demo client;
# tests obviously may print; mcp_logging.py is the redaction helper itself
# and is allowed to log raw values internally for its own purposes.
PRINT_ALLOWLIST = {
    REPO_ROOT / "test-client.py",
}

# Files exempt from the sensitive-argument check (they implement the
# fingerprinting / redaction themselves and reference these names by design).
SENSITIVE_ALLOWLIST = {
    REPO_ROOT / "mcp_logging.py",
}

# Substrings that must never appear verbatim in a positional argument passed
# to a logging call or print call.
SENSITIVE_SUBSTRINGS = (
    "api_key",
    "access_token",
    "bearer ",
    "authorization:",
)

# stderr-only `print(..., file=sys.stderr)` calls in server.py are debugger
# banners and don't reach container log shippers. They're allowed.
STDERR_PRINT_ALLOWED_FILES = {
    REPO_ROOT / "server.py",
}


def _iter_python_sources():
    """Yield (path, source) for every first-party .py file we want to scan."""
    for path in REPO_ROOT.glob("*.py"):
        yield path, path.read_text(encoding="utf-8")


def _is_print_call(node: ast.Call) -> bool:
    func = node.func
    return isinstance(func, ast.Name) and func.id == "print"


def _is_logger_call(node: ast.Call) -> bool:
    """Roughly: foo.debug/info/warning/error/exception/critical/log(...)."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    return func.attr in {"debug", "info", "warning", "error", "exception", "critical", "log"}


def _print_goes_to_stderr(node: ast.Call) -> bool:
    for kw in node.keywords:
        if (
            kw.arg == "file"
            and isinstance(kw.value, ast.Attribute)
            and isinstance(kw.value.value, ast.Name)
            and kw.value.value.id == "sys"
            and kw.value.attr == "stderr"
        ):
            return True
    return False


def _interpolated_literals_in_call(node: ast.Call):
    """Yield literal-string fragments that are accompanied by interpolation.

    A bare literal like `logger.info("here is some Authorization help text")`
    is just documentation and is not flagged. But `logger.info("api_key=%s",
    key)` (printf-style with extra positional args) and `logger.debug(f"token=
    {token}")` (f-string with at least one FormattedValue) are flagged because
    they're the shape a real leak takes.
    """
    extra_positional = max(0, len(node.args) - 1)
    for i, arg in enumerate(node.args):
        # printf-style: only flag the format string when something is being
        # interpolated into it.
        if (
            isinstance(arg, ast.Constant)
            and isinstance(arg.value, str)
            and i == 0
            and extra_positional > 0
            and "%" in arg.value
        ):
            yield arg.value
        # f-string: only flag if it actually interpolates a variable.
        elif isinstance(arg, ast.JoinedStr) and any(
            isinstance(part, ast.FormattedValue) for part in arg.values
        ):
            for part in arg.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    yield part.value


def test_no_print_calls_outside_allowlist():
    """No production module may call `print()`. The only escape hatches are
    `test-client.py` and stderr-only debugger banners."""
    offenders: list[str] = []
    for path, source in _iter_python_sources():
        if path in PRINT_ALLOWLIST:
            continue
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_print_call(node):
                if path in STDERR_PRINT_ALLOWED_FILES and _print_goes_to_stderr(node):
                    continue
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, (
        "print() must not be used in production modules — route diagnostics "
        "through the agentmcp logger instead. Offenders:\n  "
        + "\n  ".join(offenders)
    )


def test_no_sensitive_literals_in_log_or_print_calls():
    """No log/print call may pass a credential-shaped string literal as an
    argument. Catches regressions like `logger.info("api_key=%s", api_key)`."""
    offenders: list[str] = []
    for path, source in _iter_python_sources():
        if path in SENSITIVE_ALLOWLIST:
            continue
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (_is_print_call(node) or _is_logger_call(node)):
                continue
            for literal in _interpolated_literals_in_call(node):
                lowered = literal.lower()
                for needle in SENSITIVE_SUBSTRINGS:
                    if needle in lowered:
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                            f"contains {needle!r} in a log/print argument"
                        )
                        break
    assert not offenders, (
        "Log/print arguments mention credential-shaped names — use "
        "token_fingerprint() or redact() instead. Offenders:\n  "
        + "\n  ".join(offenders)
    )


# -----------------------------------------------------------------------------
# Helper function unit tests
# -----------------------------------------------------------------------------

class TestTokenFingerprint:
    def test_returns_eight_hex_chars(self):
        from mcp_logging import token_fingerprint
        fp = token_fingerprint("eyJhbGciOiJSUzI1NiJ9.test.signature")
        assert len(fp) == 8
        assert all(c in "0123456789abcdef" for c in fp)

    def test_is_deterministic(self):
        from mcp_logging import token_fingerprint
        assert token_fingerprint("same") == token_fingerprint("same")

    def test_distinguishes_tokens(self):
        from mcp_logging import token_fingerprint
        assert token_fingerprint("a") != token_fingerprint("b")

    def test_does_not_contain_input(self):
        from mcp_logging import token_fingerprint
        secret = "supersecretbearer"
        assert secret not in token_fingerprint(secret)

    @pytest.mark.parametrize("empty", [None, "", 0])
    def test_handles_empty(self, empty):
        from mcp_logging import token_fingerprint
        assert token_fingerprint(empty) == "-"


class TestRedact:
    def test_redacts_top_level_token_key(self):
        from mcp_logging import redact
        out = redact({"token": "secret", "user": "alice"})
        assert out == {"token": "***", "user": "alice"}

    def test_redacts_nested_secret(self):
        from mcp_logging import redact
        out = redact({"outer": {"client_secret": "x", "ok": 1}})
        assert out == {"outer": {"client_secret": "***", "ok": 1}}

    def test_redacts_inside_list(self):
        from mcp_logging import redact
        out = redact([{"api_key": "x"}, {"id": 2}])
        assert out == [{"api_key": "***"}, {"id": 2}]

    def test_is_case_insensitive(self):
        from mcp_logging import redact
        out = redact({"Authorization": "Bearer abc"})
        assert out == {"Authorization": "***"}

    def test_passes_through_non_sensitive(self):
        from mcp_logging import redact
        payload = {"active": True, "exp": 1234567890, "scope": "read write"}
        assert redact(payload) == payload

    def test_returns_string_unchanged(self):
        """`redact` must not be used to scrub raw strings — callers should
        pass parsed structures. We confirm the function does not silently
        swallow a raw token (which would hide misuse)."""
        from mcp_logging import redact
        assert redact("a-raw-token") == "a-raw-token"
