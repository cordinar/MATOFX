"""Post-run working-directory cleanup.

After a successful run, a single descriptor **keep-list**
(``retain_files``) decides what survives in the job's working
directory. The model is *keep-list, not delete-list*: you list the
files worth keeping and everything else the tool wrote is removed.

**The retention floor — always kept, never listed.** A generous set
of paths is protected regardless of the keep-list, so an author who
lists nothing extra never loses anything load-bearing:

- ``inputs/`` in full — the job's exact submission record (small,
  reproducible, audit-relevant). The keep sweep never descends into
  it.
- ``logs/`` and ``heartbeat/`` — audit- and triage-relevant runtime
  records. Never scrubbed by descriptor request.
- ``launcher.py`` — how the job was invoked; reproducibility.
- ``inputs/inputs.json`` and ``outputs/outputs.json`` — the I/O
  contract between MATOFX and the tool's main script (ADR-0003,
  2026-05-20).
- Every **declared output** — the runner passes each descriptor
  ``type: file`` output (and any time-resolved ``field`` series) as
  ``extra_protected`` so a tool's promised results survive even when
  the author lists nothing. Declared outputs never need listing.

**What the keep-list actually governs:** undeclared files the tool
left behind — top-level scratch and any *undeclared* files inside
``outputs/``. When the keep-list is non-empty, those are deleted
unless they match a pattern (or sit in the floor). When it is
**empty, nothing is swept** — the whole working directory is kept.
Patterns are globs relative to the working directory, so keep an
output-side scratch file with e.g. ``outputs/*.vtk``.

``delete_io_files`` is a **legacy** deny-list retained only so tool
descriptors authored before the unified model keep working; new
descriptors leave it empty and it is a no-op. It scrubs its glob
matches from ``inputs/`` and ``outputs/``, always minus the floor.

On a failed run the cleanup is *skipped*: keeping the full state
makes triage possible. The runner emits an audit event noting that
cleanup was deferred.
"""

from __future__ import annotations

import logging
import shutil
import stat
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from matofx.tools.exec.staging import (
    HEARTBEAT_DIRNAME,
    INPUTS_DIRNAME,
    LAUNCHER_FILENAME,
    LOGS_DIRNAME,
    OUTPUTS_DIRNAME,
)

_log = logging.getLogger(__name__)

_PROTECTED_TOP_LEVEL: frozenset[str] = frozenset(
    {INPUTS_DIRNAME, LOGS_DIRNAME, HEARTBEAT_DIRNAME, LAUNCHER_FILENAME}
)
"""Top-level entries the keep sweep never touches.

``inputs/`` (the submission record), the two runtime directories,
and the launcher script are kept wholesale — operators rely on them
for debugging and auditing long after the job finishes. Note that
``outputs/`` is *not* here: its declared results are floored, but its
*undeclared* contents are subject to the keep-list (see
:func:`_sweep_outputs`)."""

_CONTRACT_RETAINED: tuple[str, ...] = (
    f"{INPUTS_DIRNAME}/inputs.json",
    f"{OUTPUTS_DIRNAME}/outputs.json",
)
"""Always-on retention floor for every job (ADR-0003, 2026-05-20).

The two I/O documents are the integration contract between MATOFX
and tool main scripts. They are needed for the parametric-studies
durable per-child results store, per-child drill-down, and post-hoc
debugging — so they are retained on every job, overriding both the
keep-list default *and* any legacy ``delete_io_files`` listing that
targets them."""


def apply_cleanup(
    *,
    working_dir: Path,
    retain_files: Iterable[str],
    delete_io_files: Iterable[str] = (),
    extra_protected: Iterable[str] = (),
) -> None:
    """Apply descriptor cleanup rules to a finished working directory.

    Two passes, in order:

    1. **delete_io_files** (legacy) — every glob pattern is matched
       under ``inputs/`` and ``outputs/``; matching files are
       deleted, *except* anything in the retention floor. New
       descriptors leave this empty, making the pass a no-op.
    2. **retain_files** (the keep-list) — when non-empty, undeclared
       top-level entries and undeclared ``outputs/`` files that don't
       match a keep pattern (and aren't floored) are deleted. When
       empty, nothing is swept.

    Retention floor (ADR-0003): the platform-wide
    :data:`_CONTRACT_RETAINED` paths plus any caller-supplied
    ``extra_protected`` paths are *never* deleted. The runner passes
    every descriptor-declared file output as ``extra_protected`` so a
    tool's promised results survive even when the keep-list is empty.

    Args:
        working_dir: The job's absolute working directory.
        retain_files: Keep-list glob patterns (relative to
            ``working_dir``; use e.g. ``outputs/*.vtk`` to keep an
            output-side file). When empty, nothing is swept.
        delete_io_files: Legacy deny-list scrubbed from ``inputs/``
            and ``outputs/``. Empty for new descriptors.
        extra_protected: Working-dir-relative paths the caller adds
            to the retention floor (declared outputs, ``field``
            series, etc.).

    The function never raises on a missing path — cleanup of an
    already-clean directory is idempotent. OSErrors during delete
    propagate so the runner can surface them.
    """
    if not working_dir.exists():
        return

    floor = _resolve_floor(working_dir, extra_protected)
    _delete_io(working_dir, delete_io_files, floor)
    _apply_keep_list(working_dir, retain_files, floor)


def _resolve_floor(working_dir: Path, extra_protected: Iterable[str]) -> frozenset[Path]:
    """Resolve the contract floor + caller extras into absolute paths."""
    paths: list[Path] = []
    for rel in [*_CONTRACT_RETAINED, *list(extra_protected)]:
        if not rel:
            continue
        try:
            paths.append((working_dir / rel).resolve())
        except OSError:  # pragma: no cover - path resolution failure
            continue
    return frozenset(paths)


def _delete_io(
    working_dir: Path,
    delete_io_files: Iterable[str],
    floor: frozenset[Path],
) -> None:
    """Legacy deny-list scrub of ``inputs/`` and ``outputs/``."""
    patterns = list(delete_io_files)
    if not patterns:
        return
    for sub in (INPUTS_DIRNAME, OUTPUTS_DIRNAME):
        sub_path = working_dir / sub
        if not sub_path.exists():
            continue
        for pat in patterns:
            for match in sub_path.glob(pat):
                if _in_floor(match, floor):
                    continue
                _safe_delete(match)


def _apply_keep_list(
    working_dir: Path,
    retain_files: Iterable[str],
    floor: frozenset[Path],
) -> None:
    """Keep-list sweep across the top level and ``outputs/``.

    Non-protected top-level entries and undeclared ``outputs/`` files
    that neither match a keep pattern nor sit in the floor are
    deleted. An empty keep-list keeps everything.
    """
    patterns = list(retain_files)
    if not patterns:
        return
    keepers = _expand_keepers(working_dir, patterns)
    for entry in working_dir.iterdir():
        if entry.name in _PROTECTED_TOP_LEVEL:
            continue
        if entry.name == OUTPUTS_DIRNAME and entry.is_dir():
            _sweep_outputs(entry, keepers, floor)
            continue
        if _is_kept(entry, keepers, floor):
            continue
        _safe_delete(entry)


def _sweep_outputs(
    outputs_dir: Path,
    keepers: frozenset[Path],
    floor: frozenset[Path],
) -> None:
    """Delete undeclared, unlisted ``outputs/`` files; keep the dir.

    Declared outputs and ``outputs/outputs.json`` are in the floor,
    so the tool's promised results always survive.
    """
    for entry in outputs_dir.iterdir():
        if _is_kept(entry, keepers, floor):
            continue
        _safe_delete(entry)


def _expand_keepers(working_dir: Path, patterns: Iterable[str]) -> frozenset[Path]:
    """Resolve every keep-pattern match into an absolute path set."""
    keepers: set[Path] = set()
    for pat in patterns:
        for match in working_dir.glob(pat):
            try:
                keepers.add(match.resolve())
            except OSError:  # pragma: no cover - path resolution failure
                keepers.add(match)
    return frozenset(keepers)


def _is_kept(entry: Path, keepers: frozenset[Path], floor: frozenset[Path]) -> bool:
    """True if ``entry`` matches a keep pattern or sits in the floor."""
    try:
        resolved = entry.resolve()
    except OSError:  # pragma: no cover - path resolution failure
        resolved = entry
    return resolved in keepers or resolved in floor


def _in_floor(match: Path, floor: frozenset[Path]) -> bool:
    try:
        return match.resolve() in floor
    except OSError:  # pragma: no cover - path resolution failure
        return False


def _on_rm_error(func: Any, path: Any, _exc: BaseException) -> None:
    """``shutil.rmtree`` error handler: clear read-only + retry, else skip.

    Best-effort cleanup of an *ephemeral* working directory must never wedge
    the runner loop. A stray read-only or transiently-locked entry — e.g. a
    ``__pycache__`` ``.pyc`` an imported sibling module wrote, held by OneDrive
    sync or an AV scanner on Windows — is cleared-and-retried, then logged and
    skipped if it still won't go.
    """
    try:
        Path(path).chmod(stat.S_IWRITE)
        func(path)
    except OSError as exc:
        _log.warning("cleanup.skip_undeletable", extra={"path": str(path), "error": str(exc)})


def _safe_delete(path: Path) -> None:
    """Delete a file or directory, best-effort.

    Missing paths are ignored. A locked / read-only entry (common on Windows
    under OneDrive or an antivirus scanner) is logged and skipped rather than
    raised, so post-run cleanup can never crash and wedge the runner loop.
    """
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path, onexc=_on_rm_error)
        return
    try:
        path.chmod(stat.S_IWRITE)
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        _log.warning("cleanup.skip_undeletable", extra={"path": str(path), "error": str(exc)})
