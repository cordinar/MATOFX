"""On-disk byte storage for governed library-file versions (ADR-0006).

A library version's bytes live on the active :class:`StorageLocation`
mount under a dedicated ``library/`` namespace, kept visually separate
from the ``jobs/`` working-dir layout and the ``drafts/`` upload area
(:mod:`matofx.tools.exec.uploads`)::

    <mount>/library/<env>/file_<id>/v<ordinal>/<filename>

- ``library/`` namespace marks these as the curated, long-lived store
  (never swept by job cleanup, unlike a working directory).
- ``<env>`` (``Settings.environment``) follows the same segmentation the
  staged working dirs and draft uploads use, so a single mount serving
  dev / staging / prod doesn't interleave library files at the top level.
- ``file_<id>/v<ordinal>/`` makes every immutable version its own
  directory — appending a version never touches an older version's bytes.

Versions are immutable: :func:`store_version_bytes` writes once into a
fresh ``v<ordinal>`` directory and never overwrites (unlike the draft
area's replacement semantics). The sha-256 ``content_hash`` is computed
here so the service can persist it alongside the path for integrity and
the "these two versions are byte-identical" signal.

Filenames are sanitised to a single POSIX-safe segment and the resolved
target is checked to stay below the version directory — the same
defence :mod:`matofx.tools.exec.uploads` applies to forged uploads.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

LIBRARY_DIRNAME: str = "library"
"""Top-level segment under the storage mount for governed library bytes."""

_FIRST_PRINTABLE_ORD = 32
"""ASCII control characters (ord < 32) are scrubbed from filenames."""


class LibraryStorageError(RuntimeError):
    """Raised when library bytes cannot be persisted or read.

    Message is plain-English and surfaced verbatim to the user (the API
    layer converts it to a friendly error body).
    """


@dataclass(frozen=True)
class StoredVersion:
    """The on-disk result of writing one library version's bytes."""

    stored_path: str
    """Absolute path the bytes landed at (persisted on the version row)."""

    original_filename: str
    """Sanitised leaf filename actually written."""

    content_hash: str
    """sha-256 hex digest of the bytes — integrity + dedup signal."""

    size_bytes: int
    extension: str
    """Lower-case, no leading dot (empty when the filename has none)."""


def _safe_filename_segment(value: str) -> str:
    """Sanitise a filename to a single POSIX-safe segment.

    Mirrors :func:`matofx.tools.exec.uploads._safe_filename_segment`:
    strips any path components, rejects NUL bytes, keeps only the leaf,
    and replaces filesystem-hostile characters with ``_``. An empty
    result raises :class:`LibraryStorageError`.
    """
    if not value or "\x00" in value:
        raise LibraryStorageError("That filename can't be empty or contain NUL bytes.")
    leaf = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not leaf or leaf in (".", ".."):
        raise LibraryStorageError("That filename is not valid.")
    cleaned: list[str] = []
    for ch in leaf:
        if ord(ch) < _FIRST_PRINTABLE_ORD or ch in {"<", ">", ":", '"', "|", "?", "*"}:
            cleaned.append("_")
        else:
            cleaned.append(ch)
    result = "".join(cleaned).strip()
    if not result:
        raise LibraryStorageError("That filename is not valid.")
    return result


def _safe_env_segment(environment: str | None) -> str:
    """Mirror of :func:`matofx.tools.exec.uploads._safe_env_segment`."""
    if not environment:
        return ""
    return "".join(c for c in environment.lower() if c.isalnum() or c in "-_")


def extension_of(filename: str) -> str:
    """Return the lower-case extension (no dot) of ``filename``, or ``""``."""
    suffix = Path(filename).suffix
    return suffix[1:].lower() if suffix else ""


def library_root_for(*, mount_path: str, environment: str | None, file_id: int) -> Path:
    """Return the absolute directory holding ``file_id``'s version subdirs."""
    env_segment = _safe_env_segment(environment)
    base = Path(mount_path) / LIBRARY_DIRNAME
    if env_segment:
        base = base / env_segment
    return base / f"file_{file_id:07d}"


def store_version_bytes(
    *,
    mount_path: str,
    environment: str | None,
    file_id: int,
    ordinal: int,
    filename: str,
    content: bytes,
) -> StoredVersion:
    """Write ``content`` into a fresh ``v<ordinal>`` directory (write-once).

    The version directory must not already hold the target file — a
    re-used ``(file_id, ordinal)`` is a programming error (ordinals are
    monotonic per file) and raises rather than overwriting an immutable
    version's bytes.

    Args:
        mount_path: Root of the active :class:`StorageLocation`.
        environment: ``Settings.environment`` for the path segment;
            ``None`` falls back to ``library/file_<id>/...`` (tests).
        file_id: The owning ``library_file`` id.
        ordinal: This version's 1-based ordinal.
        filename: Client-supplied original filename (sanitised here).
        content: Raw bytes to write.

    Returns:
        :class:`StoredVersion` with the absolute path, sha-256 hash,
        size, and extension for the version row.

    Raises:
        LibraryStorageError: sanitisation fails, the target already
            exists, or any underlying ``OSError`` surfaces. Always
            plain-English, ready to surface to the user.
    """
    safe_filename = _safe_filename_segment(filename)
    version_dir = (
        library_root_for(mount_path=mount_path, environment=environment, file_id=file_id)
        / f"v{ordinal}"
    )
    target = version_dir / safe_filename

    try:
        resolved_dir = version_dir.resolve(strict=False)
        resolved_target = target.resolve(strict=False)
        resolved_target.relative_to(resolved_dir)
    except (OSError, ValueError) as exc:
        raise LibraryStorageError(
            "Couldn't determine a safe location for that file — try a simpler filename."
        ) from exc

    if target.exists():
        raise LibraryStorageError(
            "A library version already exists at that location. Library versions are "
            "immutable and are never overwritten."
        )

    try:
        version_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    except OSError as exc:
        raise LibraryStorageError(
            f"Couldn't save the library file to disk: {exc}. "
            "Check that the storage mount is reachable and writable."
        ) from exc

    return StoredVersion(
        stored_path=str(target),
        original_filename=safe_filename,
        content_hash=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        extension=extension_of(safe_filename),
    )


def read_version_bytes(stored_path: str) -> bytes:
    """Read a stored version's bytes back from disk.

    Raises:
        LibraryStorageError: the file is missing or unreadable —
            plain-English, ready to surface to the user (e.g. the mount
            is offline or the bytes were removed out-of-band).
    """
    try:
        return Path(stored_path).read_bytes()
    except OSError as exc:
        raise LibraryStorageError(
            f"Couldn't read that library file from disk: {exc}. "
            "The storage mount may be offline, or the file may have been moved."
        ) from exc
