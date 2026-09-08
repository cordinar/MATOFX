"""Top-level entry point for the ``matofx-api`` process.

Wired into ``[project.scripts]`` in ``pyproject.toml`` as
``matofx-api = matofx.api.daemon:main``.

Boots uvicorn on **loopback**, with the redacting JSON-Lines log
formatter installed and a single :class:`Settings` instance built from
disk + env-var overrides at process startup.

The bind address is not configurable, and deliberately so: the
supported production shape puts a reverse proxy on the same host in
front of this process, which makes a loopback bind the correct one —
the app is then unreachable except through the proxy. ``run_api``
takes host / port only so tests can bind elsewhere; nothing reads them
from ``matofx.toml``. Serving MATOFX from a container, or putting the
proxy on a *different* host, would need that config key to exist
first.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn

from matofx.api.app import make_app
from matofx.config import load_settings
from matofx.obs import configure as configure_obs

_log = logging.getLogger(__name__)

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000


def main() -> int:
    """Sync entry point installed under the ``matofx-api`` console script."""
    try:
        asyncio.run(run_api())
    except KeyboardInterrupt:
        return 130
    except Exception:
        _log.exception("matofx-api failed to start")
        return 1
    return 0


async def run_api(host: str = _DEFAULT_HOST, port: int = _DEFAULT_PORT) -> None:
    """Build the app and serve it via uvicorn until the process exits."""
    settings = load_settings()
    configure_obs(settings.log)
    # Single process serves both /api/* (REST) and /ui/* (NiceGUI shell).
    app = make_app(settings, mount_ui_shell=True)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=None,  # let our matofx.obs setup own logging end-to-end
        access_log=False,  # we record audited mutations explicitly; access log is noise
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    sys.exit(main())
