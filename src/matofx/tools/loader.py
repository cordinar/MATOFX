"""Descriptor loaders: load + validate from a bundle on disk.

Centralises the descriptor file-reading logic so install / update
paths agree on:

- Which filenames to look for (``descriptor.toml`` or
  ``descriptor.json``, in that order).
- File-permission warnings (matches the config-file pattern in
  :mod:`matofx.config`).
- The single error class surfaced when the bundle is malformed.

Tool authors don't need to reach into this module — the standard
flow is ``ToolService.install`` (descriptor already in memory) or
:func:`install_from_path` (loader runs first, then install).
"""

from __future__ import annotations

import json
import sys
import tomllib
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

from matofx.tools.descriptor import ToolDescriptor

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.tools.orm import Tool
    from matofx.tools.service import ToolService


_DESCRIPTOR_FILENAMES: tuple[str, ...] = ("descriptor.toml", "descriptor.json")


class DescriptorLoadError(Exception):
    """The bundle is missing, unreadable, or doesn't contain a valid descriptor."""


def load_descriptor(bundle_dir: str | Path) -> ToolDescriptor:
    """Read and validate the descriptor for a tool bundle on disk.

    Looks for ``descriptor.toml`` first, then ``descriptor.json``.
    Raises :class:`DescriptorLoadError` when neither is present, the
    file is unreadable, or the contents fail Pydantic validation.

    On POSIX hosts, emits a warning when the descriptor file is
    world- or group-readable — same pattern as the config-file
    permission warning.
    """
    bundle = Path(bundle_dir)
    if not bundle.is_dir():
        raise DescriptorLoadError(f"Bundle path {str(bundle)!r} isn't a directory.")

    descriptor_path: Path | None = None
    for filename in _DESCRIPTOR_FILENAMES:
        candidate = bundle / filename
        if candidate.is_file():
            descriptor_path = candidate
            break
    if descriptor_path is None:
        raise DescriptorLoadError(
            f"No descriptor file found in {str(bundle)!r}. "
            f"Expected one of: {', '.join(_DESCRIPTOR_FILENAMES)}."
        )

    _warn_if_loose_permissions(descriptor_path)

    raw_text = descriptor_path.read_text(encoding="utf-8")
    try:
        if descriptor_path.suffix == ".toml":
            payload = tomllib.loads(raw_text)
        else:
            payload = json.loads(raw_text)
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        raise DescriptorLoadError(f"Couldn't parse {descriptor_path.name}: {exc}") from exc

    try:
        return ToolDescriptor.model_validate(payload)
    except ValueError as exc:
        raise DescriptorLoadError(
            f"Descriptor in {descriptor_path.name} failed validation: {exc}"
        ) from exc


async def install_from_path(
    session: AsyncSession,
    tool_service: ToolService,
    *,
    actor_user_id: int,
    bundle_dir: str | Path,
) -> Tool:
    """Load the descriptor from ``bundle_dir`` and install it.

    Convenience wrapper: ``load_descriptor`` + ``ToolService.install``
    in one call. The bundle directory becomes the tool's ``bundle_path``.
    """
    descriptor = load_descriptor(bundle_dir)
    return await tool_service.install(
        session,
        actor_user_id=actor_user_id,
        descriptor=descriptor,
        bundle_path=str(Path(bundle_dir).resolve()),
    )


def _warn_if_loose_permissions(path: Path) -> None:
    """Emit a warning when the descriptor file is readable beyond its owner.

    Matches the config-file permission-warning pattern. POSIX-only;
    Windows has a different ACL model and no analogous bit-flag
    concept. Honours the ``MATOFX_SUPPRESS_PERM_WARNING`` env var so
    test/CI hosts can opt out.
    """
    if sys.platform == "win32":
        return
    import os  # noqa: PLC0415  (lazy: only on POSIX path)

    if os.environ.get("MATOFX_SUPPRESS_PERM_WARNING") == "1":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        warnings.warn(
            f"Descriptor file {str(path)!r} is readable by group or others "
            f"(mode {oct(mode & 0o777)}). Consider tightening to 0600.",
            stacklevel=2,
        )
