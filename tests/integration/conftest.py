"""Fixtures for live tests of the MCP servers against local or test APIs.

Each test module here sets ``pytestmark = pytest.mark.integration``, so its
tests are skipped unless pytest is run with ``--integration`` (see
``tests/conftest.py``).

Settings come from ``.env.test`` in the repo root (copy ``.env.test.example``).
They are read into a dict, not into ``os.environ``, and passed only to the
server processes.

Both servers run as separate processes, started from an empty temporary
folder. Each server calls ``load_dotenv(".env", override=True)`` on start-up,
so running them from the repo root would let a developer's own ``.env``
override the test settings.

- ``local-stdio``: ``server.py`` over stdio, as Claude Desktop runs it, with
  its configuration in environment variables.
- ``remote-http``: ``server_remote.py streamable-http`` with OAuth off, with
  its configuration sent in the ``Authorization`` and ``x-inmydata-*`` headers.
"""
import ipaddress
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest
from dotenv import dotenv_values
from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = Path(__file__).resolve().parent / "_launch_server.py"
ENV_FILE = REPO_ROOT / ".env.test"

SERVER_START_TIMEOUT_S = 60


@dataclass(frozen=True)
class DevApiSettings:
    """Connection settings for the API, read from ``.env.test``."""

    api_key: str
    tenant: str
    server: str
    calendar: str

    @property
    def host(self) -> str:
        """The hostname the SDK calls: ``<tenant>.<server>``."""
        return f"{self.tenant}.{self.server}"


def _assert_api_target(host: str, target: str) -> None:
    """Check that the configured host is safe for the selected target."""
    if target not in ("local", "test"):
        pytest.fail(f"Unknown integration target: {target}")
    if target == "test" and host != "test.test-inmydata.com":
        pytest.fail("Test mode requires TEST_INMYDATA_TENANT=test and TEST_INMYDATA_SERVER=test-inmydata.com")
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        pytest.fail(f"{host} does not resolve ({exc})")
    addresses = {info[4][0] for info in infos}
    if not addresses:
        pytest.fail(f"{host} did not resolve to any addresses")
    if target == "local" and any(not ipaddress.ip_address(a).is_loopback for a in addresses):
        pytest.fail(
            f"{host} does not resolve exclusively to this machine. "
            f"For local mode, add a hosts file entry '127.0.0.1 {host}'."
        )
    if target == "test" and any(ipaddress.ip_address(a).is_loopback for a in addresses):
        pytest.fail(f"{host} resolves to this machine; test mode requires the test server")


@pytest.fixture(scope="session")
def dev_api(request: pytest.FixtureRequest) -> DevApiSettings:
    """Load ``.env.test`` and verify the host matches the selected target."""
    if not ENV_FILE.exists():
        pytest.fail(f"{ENV_FILE.name} not found; copy .env.test.example and add the API key")
    values = dotenv_values(ENV_FILE)
    target = request.config.getoption("--integration-target")
    prefix = f"{target.upper()}_INMYDATA_"
    if target == "local" and not any(key.startswith(prefix) for key in values):
        prefix = "INMYDATA_"
    api_key = (values.get(f"{prefix}API_KEY") or "").strip()
    if not api_key or api_key == "your_dev_api_key_here":
        pytest.fail(f"{prefix}API_KEY is not set in {ENV_FILE.name}")
    settings = DevApiSettings(
        api_key=api_key,
        tenant=(values.get(f"{prefix}TENANT") or ("dev" if target == "local" else "test")).strip(),
        server=(values.get(f"{prefix}SERVER") or (
            "inmydata.com" if target == "local" else "test-inmydata.com"
        )).strip(),
        calendar=(values.get(f"{prefix}CALENDAR") or "Default").strip(),
    )
    _assert_api_target(settings.host, target)
    return settings


def _clean_env() -> dict[str, str]:
    """Copy of this process's environment without any inmydata settings.

    Removes ``INMYDATA_*``, ``AGENTIC_RAG_*``, ``MCP_DEBUG`` and any
    ``<TENANT>_API_KEY`` (the remote server prefers that over the header), so
    only the test settings reach the servers.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith(("INMYDATA_", "AGENTIC_RAG_"))
        and not k.upper().endswith("_API_KEY")
        and k.upper() != "MCP_DEBUG"
    }


@pytest.fixture(scope="session")
def server_workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Empty folder the servers start in, so no ``.env`` is loaded."""
    return tmp_path_factory.mktemp("mcp-server-cwd")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def remote_server_url(dev_api: DevApiSettings, server_workdir: Path) -> Iterator[str]:
    """Start ``server_remote.py`` over streamable HTTP and yield its MCP URL."""
    port = _free_port()
    env = _clean_env()
    env["INMYDATA_USE_OAUTH"] = "false"
    env["MCP_DUCKDB_LOCATION"] = str(server_workdir / "duckdb")
    log_path = server_workdir / "server_remote.log"
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            [sys.executable, str(LAUNCHER), str(REPO_ROOT / "server_remote.py"),
             "streamable-http", str(port)],
            cwd=server_workdir, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + SERVER_START_TIMEOUT_S
            while True:
                if proc.poll() is not None:
                    pytest.fail(f"server_remote.py exited early:\n{log_path.read_text(errors='replace')}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=1):
                        break
                except OSError:
                    if time.monotonic() > deadline:
                        pytest.fail(f"server_remote.py did not start within {SERVER_START_TIMEOUT_S}s")
                    time.sleep(0.25)
            yield f"http://127.0.0.1:{port}/mcp"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


@pytest.fixture(params=["local-stdio", "remote-http"])
def mcp_client(
    request: pytest.FixtureRequest, dev_api: DevApiSettings, server_workdir: Path
) -> Client:
    """An MCP client (not yet connected) for each server. Use ``async with``."""
    if request.param == "local-stdio":
        env = _clean_env()
        env.update(
            INMYDATA_API_KEY=dev_api.api_key,
            INMYDATA_TENANT=dev_api.tenant,
            INMYDATA_SERVER=dev_api.server,
            INMYDATA_CALENDAR=dev_api.calendar,
            MCP_DUCKDB_LOCATION=str(server_workdir / "duckdb"),
        )
        transport = StdioTransport(
            command=sys.executable,
            args=[str(LAUNCHER), str(REPO_ROOT / "server.py")],
            env=env,
            cwd=str(server_workdir),
        )
    else:
        url = request.getfixturevalue("remote_server_url")
        transport = StreamableHttpTransport(
            url=url,
            headers={
                "Authorization": f"Bearer {dev_api.api_key}",
                "x-inmydata-tenant": dev_api.tenant,
                "x-inmydata-server": dev_api.server,
                "x-inmydata-calendar": dev_api.calendar,
            },
        )
    return Client(transport, timeout=120)
