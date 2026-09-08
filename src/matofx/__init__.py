"""MATOFX SPDM — Funis Consulting Ltd."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

try:
    # Single source of truth is [project].version in pyproject.toml — the
    # installed distribution's metadata. A hardcoded string here drifted
    # once (CLI reported 0.0.0 while the install was 0.0.2).
    __version__ = _dist_version("matofx")
except PackageNotFoundError:  # running from a raw source tree, not installed
    __version__ = "0.0.0+uninstalled"
