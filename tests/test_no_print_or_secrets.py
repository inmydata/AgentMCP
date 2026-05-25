"""
Security regression tests – ensure no print() calls or raw sensitive strings
appear in production source modules.

These tests scan the Python source files in the project root and fail if any
forbidden patterns are found.  test-client.py is excluded because it is a
developer tool, not production code.
"""
import ast
import pathlib
import re

# Project root is the parent of the `tests/` directory.
PROJECT_ROOT = pathlib.Path(__file__).parent.parent

# Production modules to check – all *.py files in the project root except
# test-client.py and anything inside `tests/`.
_EXCLUDED_FILES = {"test-client.py"}

# The module that is allowed to reference sensitive names (the redaction helper).
_REDACTION_MODULE = "mcp_logging.py"


def _production_sources():
    sources = []
    for path in PROJECT_ROOT.glob("*.py"):
        if path.name not in _EXCLUDED_FILES:
            sources.append(path)
    return sorted(sources)


def test_no_print_calls_in_production_code():
    """No production module may call print()."""
    offenders = []
    for path in _production_sources():
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            # Let other tests catch syntax errors; skip here.
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, (
        "print() calls found in production modules – use the logger instead:\n"
        + "\n".join(f"  {o}" for o in offenders)
    )


def _extract_string_literals(node: ast.expr) -> list[str]:
    """Recursively collect all string constants within an AST expression."""
    strings = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        strings.append(node.value)
    elif isinstance(node, ast.JoinedStr):
        # f-string: walk its values
        for value in node.values:
            strings.extend(_extract_string_literals(value))
    for child in ast.iter_child_nodes(node):
        strings.extend(_extract_string_literals(child))
    return strings


def _is_log_or_print_call(node: ast.Call) -> bool:
    """Return True if *node* is a logging.* call or a print() call."""
    func = node.func
    if isinstance(func, ast.Name) and func.id == "print":
        return True
    if isinstance(func, ast.Attribute):
        # logger.debug(...) / logger.info(...) / logging.warning(...)
        if func.attr in ("debug", "info", "warning", "error", "critical", "exception"):
            return True
    return False


def test_no_raw_sensitive_strings_in_log_arguments():
    """Log/print string arguments must not contain literal 'api_key', 'access_token', or 'bearer'.

    These three patterns were specifically called out in the security issue as the
    identifiers most likely to reveal credential values in logs.  The broader
    ``_REDACT_KEYS`` pattern in ``mcp_logging.py`` (which also covers 'token',
    'secret', 'password', 'authorization') is intentionally wider – it is applied
    at runtime to strip fields from response dictionaries, whereas this compile-time
    check focuses on the narrow set of names that historically appeared verbatim in
    log strings.

    The only place allowed to reference these strings is mcp_logging.py itself
    (the redaction helper).
    """
    # Matches the three specific identifiers called out in the security issue.
    _SENSITIVE = re.compile(r"\b(api_key|access_token|bearer)\b", re.I)

    offenders = []
    for path in _production_sources():
        if path.name == _REDACTION_MODULE:
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_log_or_print_call(node)):
                continue
            # Check every string literal passed as a positional argument.
            for arg in node.args:
                for literal in _extract_string_literals(arg):
                    if _SENSITIVE.search(literal):
                        offenders.append(
                            f"{path.name}:{node.lineno}: sensitive string in log argument: {literal!r}"
                        )

    assert not offenders, (
        "Sensitive strings (api_key / access_token / bearer) found in log/print "
        "arguments outside the redaction helper:\n"
        + "\n".join(f"  {o}" for o in offenders)
    )
