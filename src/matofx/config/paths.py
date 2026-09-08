r"""Default config-file location lookup.

The config file path is resolved in this order, taking the first that exists:

1. Path passed explicitly to :func:`matofx.config.loader.load_settings`.
2. ``MATOFX_CONFIG`` environment variable.
3. OS-appropriate system path:
   - Linux/macOS: ``/etc/matofx/matofx.toml``
   - Windows: ``%PROGRAMDATA%\matofx\matofx.toml``
4. User-local fallback for dev:
   - Linux/macOS: ``~/.config/matofx/matofx.toml``
   - Windows: ``%APPDATA%\matofx\matofx.toml``

If no file is found, ``load_settings`` returns defaults; env-var
overrides still apply. Operators who want a hard error on missing
config can pass an explicit path.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_CONFIG_FILENAME = "matofx.toml"
_ENV_VAR = "MATOFX_CONFIG"


def env_path() -> Path | None:
    """Return the path from ``MATOFX_CONFIG`` if set, else ``None``."""
    value = os.environ.get(_ENV_VAR)
    return Path(value) if value else None


def system_path() -> Path:
    """Return the OS-appropriate system-wide config path (may not exist)."""
    if sys.platform == "win32":
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return Path(program_data) / "matofx" / _CONFIG_FILENAME
    return Path("/etc/matofx") / _CONFIG_FILENAME


def user_path() -> Path:
    """Return the per-user fallback config path (may not exist)."""
    if sys.platform == "win32":
        app_data = os.environ.get("APPDATA")
        if app_data:
            return Path(app_data) / "matofx" / _CONFIG_FILENAME
        return Path.home() / "AppData" / "Roaming" / "matofx" / _CONFIG_FILENAME
    return Path.home() / ".config" / "matofx" / _CONFIG_FILENAME


def candidate_paths() -> list[Path]:
    """Return the ordered list of paths the loader will try.

    Excludes the explicit-argument case (which the loader handles itself);
    the env-var path is included only when the env var is set.
    """
    paths: list[Path] = []
    env = env_path()
    if env is not None:
        paths.append(env)
    paths.append(system_path())
    paths.append(user_path())
    return paths
