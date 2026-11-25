"""Start an MCP server script with the operating system's certificate store.

Usage: ``python _launch_server.py <path to server script> [server args...]``

The local dev API at ``https://dev.inmydata.com`` uses a certificate that the
operating system trusts but Python's bundled certificate list (certifi) does
not. ``truststore`` makes Python's ``ssl`` module, and so ``requests`` and the
inmydata SDK, check certificates against the OS store instead. Certificate
checking stays on.

This runs only in the server process started by the integration tests, so the
servers' own code is unchanged.
"""
import runpy
import sys
from pathlib import Path

import truststore


def main() -> None:
    """Inject the OS trust store, then run the given script as ``__main__``."""
    truststore.inject_into_ssl()
    script = Path(sys.argv[1]).resolve()
    sys.argv = [str(script), *sys.argv[2:]]
    sys.path.insert(0, str(script.parent))
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
