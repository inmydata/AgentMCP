"""Shared pytest configuration for the whole test suite.

Adds the ``--integration`` command-line switch. Tests marked
``@pytest.mark.integration`` call a real inmydata API (see
``tests/integration/``), so they are skipped unless the switch is given:

    uv run pytest --integration -m integration
"""
import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the ``--integration`` switch that enables live API tests."""
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run tests marked 'integration', which call the local dev inmydata API.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip integration tests unless ``--integration`` was passed."""
    if config.getoption("--integration"):
        return
    skip = pytest.mark.skip(reason="live API test; run with --integration")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
