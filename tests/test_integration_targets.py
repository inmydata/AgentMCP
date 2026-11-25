"""Tests for the live integration suite's API target safeguards."""

import socket
from types import SimpleNamespace

import pytest

from tests.integration import conftest
from tests.integration.conftest import _assert_api_target


def _addresses(*addresses: str) -> list[tuple]:
    return [(None, None, None, None, (address, 443)) for address in addresses]


@pytest.mark.parametrize(
    ("mode", "host", "addresses"),
    [
        ("local", "dev.inmydata.com", ("127.0.0.1", "::1")),
        ("test", "test.test-inmydata.com", ("192.0.2.1",)),
    ],
)
def test_integration_mode_accepts_its_intended_target(monkeypatch, mode, host, addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: _addresses(*addresses))

    _assert_api_target(host, mode)


@pytest.mark.parametrize(
    ("mode", "host", "addresses"),
    [
        ("local", "dev.inmydata.com", ("127.0.0.1", "192.0.2.1")),
        ("test", "dev.inmydata.com", ("192.0.2.1",)),
        ("test", "test.test-inmydata.com.attacker.example", ("192.0.2.1",)),
        ("test", "test.test-inmydata.com", ("127.0.0.1",)),
        ("test", "test.test-inmydata.com", ("192.0.2.1", "::1")),
    ],
)
def test_integration_mode_rejects_an_unintended_target(monkeypatch, mode, host, addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: _addresses(*addresses))

    with pytest.raises(pytest.fail.Exception):
        _assert_api_target(host, mode)


def test_integration_mode_reports_dns_failure(monkeypatch):
    def fail_resolution(*args, **kwargs):
        raise socket.gaierror("DNS failed")

    monkeypatch.setattr(socket, "getaddrinfo", fail_resolution)

    with pytest.raises(pytest.fail.Exception, match="does not resolve"):
        _assert_api_target("test.test-inmydata.com", "test")


@pytest.mark.parametrize(
    ("mode", "host", "key"),
    [
        ("local", "dev.inmydata.com", "local-key"),
        ("test", "test.test-inmydata.com", "test-key"),
    ],
)
def test_fixture_uses_only_selected_mode_settings(monkeypatch, tmp_path, mode, host, key):
    env_file = tmp_path / ".env.test"
    env_file.write_text(
        "LOCAL_INMYDATA_API_KEY=local-key\n"
        "LOCAL_INMYDATA_TENANT=dev\n"
        "LOCAL_INMYDATA_SERVER=inmydata.com\n"
        "LOCAL_INMYDATA_CALENDAR=LocalCalendar\n"
        "TEST_INMYDATA_API_KEY=test-key\n"
        "TEST_INMYDATA_TENANT=test\n"
        "TEST_INMYDATA_SERVER=test-inmydata.com\n"
        "TEST_INMYDATA_CALENDAR=TestCalendar\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(conftest, "ENV_FILE", env_file)
    address = "127.0.0.1" if mode == "local" else "192.0.2.1"
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: _addresses(address))
    request = SimpleNamespace(config=SimpleNamespace(getoption=lambda option: mode))

    settings = conftest.dev_api.__wrapped__(request)

    assert settings.host == host
    assert settings.api_key == key
    assert settings.calendar == ("LocalCalendar" if mode == "local" else "TestCalendar")


@pytest.mark.parametrize("mode", ["local", "test"])
def test_missing_mode_key_does_not_use_other_mode_key(monkeypatch, tmp_path, mode):
    env_file = tmp_path / ".env.test"
    other_mode = "TEST" if mode == "local" else "LOCAL"
    env_file.write_text(
        f"INMYDATA_API_KEY=legacy-key\n{other_mode}_INMYDATA_API_KEY=other-key\n"
        f"{mode.upper()}_INMYDATA_TENANT=dev\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(conftest, "ENV_FILE", env_file)
    request = SimpleNamespace(config=SimpleNamespace(getoption=lambda option: mode))

    with pytest.raises(pytest.fail.Exception, match=f"{mode.upper()}_INMYDATA_API_KEY"):
        conftest.dev_api.__wrapped__(request)


def test_local_mode_supports_legacy_unprefixed_settings(monkeypatch, tmp_path):
    env_file = tmp_path / ".env.test"
    env_file.write_text(
        "INMYDATA_API_KEY=legacy-key\nINMYDATA_TENANT=dev\nINMYDATA_SERVER=inmydata.com\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(conftest, "ENV_FILE", env_file)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: _addresses("127.0.0.1"))
    request = SimpleNamespace(config=SimpleNamespace(getoption=lambda option: "local"))

    settings = conftest.dev_api.__wrapped__(request)

    assert settings.host == "dev.inmydata.com"
    assert settings.api_key == "legacy-key"
