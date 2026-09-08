"""Semver bump rules + descriptor-diff helpers for tool snapshots.

Pure functions that the service uses to compute the new version + the
structured `changes` map for a snapshot. Kept out of
``matofx.tools.service`` so the rules are testable in isolation —
they're conceptually lightweight but the edge cases (1.x.y → 2.0.0,
0.x.y → 1.0.0 first release) deserve their own unit tests.

Bump policy (per the user's spec):

- New tool installs at ``"0.1.0"`` regardless of what the operator
  uploaded — :func:`initial_version`.
- Metadata edits (anything except files / bundle) → patch bump,
  :func:`bump_patch`. Example: 0.1.0 → 0.1.1.
- In-UI file edits (Session C) → minor bump, :func:`bump_minor`.
  Example: 0.1.7 → 0.2.0.
- Bundle replacements → major bump, :func:`bump_major`.
  Example: 0.5.3 → 1.0.0.
- First promotion to ``released`` from a ``0.x.y`` version snaps to
  ``"1.0.0"`` — :func:`first_release_snap`.

Descriptor diffing (:func:`diff_descriptor`) walks the metadata fields
the form exposes and emits a ``{field: {"old": ..., "new": ...}}``
map for the snapshot's ``changes`` column. Long string values are
truncated so a description rewrite doesn't bloat the row.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Final

INITIAL_VERSION: Final[str] = "0.1.0"
"""What every brand-new tool starts at, regardless of what the
operator uploaded in the descriptor JSON."""

FIRST_RELEASE_VERSION: Final[str] = "1.0.0"
"""Where a ``0.x.y`` tool snaps on its first promotion to ``released``."""

# Descriptor fields that drive *patch* bumps when changed via
# update_descriptor. ``main_script`` is excluded — that's a bundle-level
# change and triggers the major flow. The bottom block (``inputs``,
# ``outputs``, etc.) is the wizard-owned set: editing the input form's
# structure via the Edit Tool wizard is a metadata-tier change because
# it doesn't touch the bundle's executable code, just the descriptor.
METADATA_FIELDS: Final[tuple[str, ...]] = (
    "name",
    "short_description",
    "long_description",
    "teams",
    "downstream_tools",
    "approvals_policy",
    "approver_usernames",
    "email_notifications",
    "resources",
    "licences_required",
    "duration_class",
    "inputs",
    "input_groups",
    "input_column_layout",
    "outputs",
    "output_groups",
    "output_column_layout",
    "retain_files",
    "delete_io_files",
    "execution_backend",
    "input_groups_start_collapsed",
    "require_all_groups_opened",
    # §16.16 conditional rules + §F30 cross-field constraints. Wizard-
    # owned like inputs/outputs; must be tracked or a dependency-only
    # edit diffs to nothing and update_descriptor short-circuits as a
    # no-op, silently discarding the rules.
    "dependencies",
    "cross_field_constraints",
    # Tool-author presets, authored on the wizard's Presets tab. Same
    # trap as `dependencies` above and missed the same way: accepted by
    # `AdminToolDescriptorUpdate`, persisted in the descriptor, but not
    # tracked here — so an edit that changed *only* the presets diffed
    # to nothing and `update_descriptor` short-circuited as a no-op,
    # silently discarding the author's work with a success message.
    "presets",
)

_VALUE_TRUNCATE_AT = 50_000
"""Per-field old / new value truncation in the structured ``changes``
map. Bounded high enough that every model-bounded metadata field
(``long_description`` is capped at 2048 chars, ``name`` at 128, etc.) round-trips
intact. The cap stays in place as a sanity bound — a corrupted descriptor
that smuggles a megabyte string shouldn't be able to bloat the snapshot
row indefinitely — but in normal use the change-log modal shows the full
old / new content of every metadata field."""

_SEMVER_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def initial_version() -> str:
    """Return the initial version every new tool gets (``"0.1.0"``)."""
    return INITIAL_VERSION


def _parse(version: str) -> tuple[int, int, int]:
    """Parse a version string into a ``(major, minor, patch)`` tuple.

    Trailing pre-release / build suffixes (``-rc1``, ``+build.5``) are
    dropped — bumps reset them. A version with fewer than three parts
    is padded with zeros (``"1"`` → ``(1, 0, 0)``, ``"1.2"`` →
    ``(1, 2, 0)``). Anything that doesn't match the grammar at all
    falls back to the initial-version triple so a corrupted descriptor
    doesn't break a save.
    """
    match = _SEMVER_RE.match(version.strip())
    if match is None:
        return (0, 1, 0)
    major = int(match.group(1) or 0)
    minor = int(match.group(2) or 0)
    patch = int(match.group(3) or 0)
    return (major, minor, patch)


def _format(triple: tuple[int, int, int]) -> str:
    return f"{triple[0]}.{triple[1]}.{triple[2]}"


def bump_patch(current: str) -> str:
    """Return the patch-bumped version (``X.Y.Z → X.Y.Z+1``)."""
    major, minor, patch = _parse(current)
    return _format((major, minor, patch + 1))


def bump_minor(current: str) -> str:
    """Return the minor-bumped version (``X.Y.Z → X.Y+1.0``)."""
    major, minor, _ = _parse(current)
    return _format((major, minor + 1, 0))


def bump_major(current: str) -> str:
    """Return the major-bumped version (``X.Y.Z → X+1.0.0``)."""
    major, _, _ = _parse(current)
    return _format((major + 1, 0, 0))


def first_release_snap(current: str) -> str | None:
    """Return ``"1.0.0"`` if ``current`` is in the ``0.x.y`` track, else ``None``.

    Used by :func:`promote_state` on first promotion to ``released``
    to anchor the version at ``1.0.0``. Once the tool is past 1.x.y
    further state changes don't touch the version (returns ``None``).
    """
    major, _, _ = _parse(current)
    if major == 0:
        return FIRST_RELEASE_VERSION
    return None


def _truncate(value: Any) -> Any:
    """Truncate long strings in-place; pass other types through.

    Lists / dicts pass through unchanged — they're already small in
    this codebase (teams, approver lists, resource dicts, etc.).
    """
    if isinstance(value, str) and len(value) > _VALUE_TRUNCATE_AT:
        return value[: _VALUE_TRUNCATE_AT - 1].rstrip() + "…"
    return value


def diff_descriptor(
    old: dict[str, Any], new: dict[str, Any], *, fields: tuple[str, ...] = METADATA_FIELDS
) -> dict[str, dict[str, Any]]:
    """Compute a per-field ``{name: {"old": ..., "new": ...}}`` map.

    Walks ``fields`` and includes only entries that actually differ
    between ``old`` and ``new``. The ``version`` field is intentionally
    *not* in the default field list — its change is implicit in the
    bump itself; rendering "version: 0.1.0 → 0.1.1" alongside the
    headline summary that already says the same is noise.
    """
    out: dict[str, dict[str, Any]] = {}
    for field in fields:
        old_value = old.get(field)
        new_value = new.get(field)
        if old_value == new_value:
            continue
        out[field] = {"old": _truncate(old_value), "new": _truncate(new_value)}
    return out


def headline_for_metadata_edit(
    old_version: str, new_version: str, changes: dict[str, dict[str, Any]]
) -> str:
    """Build the change-log row's one-line headline for a metadata edit.

    Lists which fields were touched so an operator can grok the whole
    snapshot from the headline alone if they don't expand to see
    old/new bullets. Empty changes (no-op edits) get a generic line
    — caller should normally skip the snapshot entirely in that case.
    """
    if not changes:
        return f"Metadata edit — patch bump {old_version} → {new_version}"
    fields = ", ".join(sorted(changes))
    return f"Metadata edit — patch bump {old_version} → {new_version} (changed: {fields})"


def headline_for_bundle_replace(old_version: str, new_version: str) -> str:
    """One-line headline for a replace-bundle snapshot."""
    return f"Bundle replaced — major version bump {old_version} → {new_version}"


def headline_for_first_release_snap(old_version: str) -> str:
    """One-line headline for the 0.x.y → 1.0.0 promotion snap."""
    return f"Promoted to Released — version snapped from {old_version} to {FIRST_RELEASE_VERSION}"


def headline_for_initial_install() -> str:
    """One-line headline for the first ``tool_version`` row of a new tool."""
    return f"Initial install — version {INITIAL_VERSION}"


def headline_for_archive(version: str, reason: str | None = None) -> str:
    """One-line headline for an archive snapshot (no version change)."""
    base = f"Archived — version held at {version}"
    if reason and reason.strip():
        return f"{base} (reason: {reason.strip()})"
    return base


def headline_for_unarchive(version: str) -> str:
    """One-line headline for a re-activate snapshot (no version change)."""
    return f"Re-activated — version held at {version}"


def headline_for_file_edit(old_version: str, new_version: str, files: tuple[str, ...]) -> str:
    """One-line headline for an in-UI file edit (Session C).

    Wired here in Session B as a stable surface; the Session-C editor
    will call into the service method that builds this summary.
    """
    if not files:
        return f"File edit — minor bump {old_version} → {new_version}"
    return f"File edit — minor bump {old_version} → {new_version} (modified: {', '.join(files)})"


# ---- Bundle fingerprinting (V&V cohort key, ADR-0011) ----------------

_HASH_CHUNK_BYTES: Final[int] = 65_536
"""Per-file read chunk for :func:`compute_bundle_hash`. 64 KiB is a
sensible sweet spot — small enough to keep peak memory bounded on a
multi-GB bundle, large enough to amortise the per-syscall cost."""

EMPTY_BUNDLE_HASH: Final[str] = hashlib.sha256(b"").hexdigest()
"""The hash an empty bundle directory produces — the SHA-256 of zero
bytes. A fixed sentinel value (``"e3b0c4..."``); deliberately distinct
from the empty *string* (``""``) stored on a ``tool_version`` row that
hasn't had its hash computed yet. ``""`` means "unknown"; this constant
means "known to be empty"."""


def compute_bundle_hash(bundle_dir: Path) -> str:
    r"""SHA-256 hex of the bundle directory contents — deterministic across OS.

    The V&V cohort key per ADR-0011 / ``designs/vv-tracking.md`` §6.5:
    two ``tool_version`` rows sharing the same ``(alias, bundle_hash)``
    are byte-equivalent installations and a V&V record on either applies
    to both.

    Walks ``bundle_dir`` (without following symlinks to directories),
    sorts leaf entries lexicographically by POSIX-style relative path,
    and feeds ``f"{rel_path}\\0{kind}{payload}\\n"`` for each leaf into
    a single SHA-256 stream:

    - **File** (``kind='F'``): payload is the file's bytes.
    - **Symlink** (``kind='L'``): payload is the symlink target string —
      the target itself is *not* followed. This is cheap insurance against
      an out-of-bundle symlink silently smuggling content into the hash.

    Invariants:

    - Deterministic across OS: paths normalised to forward slashes;
      entry order is lexicographic on the normalised relative path.
    - Metadata-invariant: ``mtime`` / ``atime`` / file permissions do
      not contribute.
    - Empty-directory invariant: an empty bundle returns
      :data:`EMPTY_BUNDLE_HASH`.

    Raises :class:`FileNotFoundError` if ``bundle_dir`` does not exist
    and :class:`NotADirectoryError` if it exists but isn't a directory.
    Callers in the snapshot-recording path (``ToolService._record_snapshot``)
    catch these and substitute the empty-string singleton sentinel so a
    botched bundle path never blocks a version-snapshot write.
    """
    bundle_dir = Path(bundle_dir).resolve()
    if not bundle_dir.exists():
        raise FileNotFoundError(f"bundle directory does not exist: {bundle_dir}")
    if not bundle_dir.is_dir():
        raise NotADirectoryError(f"bundle path is not a directory: {bundle_dir}")

    leaves: list[tuple[str, str, Path]] = []
    for root, dirs, files in os.walk(bundle_dir, followlinks=False):
        root_path = Path(root)
        # Symlinks-to-dirs land in `dirs`. Because we passed
        # followlinks=False, os.walk doesn't descend into them — but it
        # also doesn't surface them as leaf entries we'd hash. Capture
        # them as symlink leaves, then remove from `dirs` so the walk
        # skips them entirely (otherwise os.walk would yield (root,
        # [], []) for the symlinked dir's name and we'd double-process).
        symlink_dirs = [d for d in dirs if (root_path / d).is_symlink()]
        for name in symlink_dirs:
            dirs.remove(name)
            abs_path = root_path / name
            rel = abs_path.relative_to(bundle_dir).as_posix()
            leaves.append((rel, "L", abs_path))
        for name in files:
            abs_path = root_path / name
            rel = abs_path.relative_to(bundle_dir).as_posix()
            kind = "L" if abs_path.is_symlink() else "F"
            leaves.append((rel, kind, abs_path))

    leaves.sort(key=lambda entry: entry[0])

    sha = hashlib.sha256()
    for rel, kind, abs_path in leaves:
        sha.update(rel.encode("utf-8"))
        sha.update(b"\x00")
        sha.update(kind.encode("ascii"))
        if kind == "L":
            target = str(abs_path.readlink())
            sha.update(target.encode("utf-8"))
        else:
            with abs_path.open("rb") as fh:
                while True:
                    chunk = fh.read(_HASH_CHUNK_BYTES)
                    if not chunk:
                        break
                    sha.update(chunk)
        sha.update(b"\n")
    return sha.hexdigest()
