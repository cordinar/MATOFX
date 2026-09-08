"""Bundle staging — write uploaded tool artefacts into ``bundles_dir/<alias>/``.

The browser-driven New Tool / Replace Bundle flows arrive at the API
as a multipart upload (form metadata + a static-files ZIP + optional
icon + optional presets file). The endpoint assembles a complete
:class:`ToolDescriptor` from those inputs and calls
:func:`stage_uploaded_bundle` to land everything on disk; the caller
then registers the bundle with :class:`matofx.tools.service.ToolService`.

Staging is the *only* place that touches the filesystem on behalf of an
uploaded tool. Centralising it means:

- ZIP-slip protection lives in one place.
- Per-alias overwrite semantics are consistent (refuse on create,
  swap on replace-bundle).
- Error rollback is uniform: if any step fails, the partial directory
  is removed before the exception propagates so a half-written bundle
  never sticks around.
"""

from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from matofx.tools.descriptor import ToolDescriptor


class BundleStagingError(Exception):
    """An uploaded bundle couldn't be staged on disk.

    Carries a plain-English ``detail`` so the API's error mapping can
    surface it directly to the operator.
    """

    def __init__(self, detail: str) -> None:
        """Construct with a user-facing reason."""
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class StagedBundle:
    """The artefacts produced by a successful staging.

    Caller passes ``bundle_path`` to :meth:`ToolService.install` /
    ``update_bundle``; the staged ``descriptor`` is the one written
    into ``bundle_path/descriptor.json`` and used as the source of
    truth for any descriptor-comparison work upstream.
    """

    bundle_path: Path
    descriptor: ToolDescriptor


_DESCRIPTOR_FILENAME = "descriptor.json"

# POSIX file-type bit masks (st_mode high bits). ZIP entries encode
# Unix permissions in the upper half of ``external_attr``; we use these
# to recognise + reject symlink entries during extraction.
_S_IFMT = 0o170000  # File-type bit mask.
_S_IFLNK = 0o120000  # Symbolic link.


def stage_uploaded_bundle(
    *,
    bundles_dir: Path,
    descriptor: ToolDescriptor,
    static_zip_bytes: bytes | None = None,
    icon_bytes: bytes | None = None,
    presets_bytes: bytes | None = None,
    overwrite: bool = False,
) -> StagedBundle:
    """Write ``<bundles_dir>/<alias>/<version>/`` from upload bytes.

    Per-version subdirectories — introduced in Session B alongside the
    versioning backbone — mean every bundle snapshot has its own
    immutable on-disk path. A replace-bundle (major bump) writes a
    fresh ``v2.0.0/`` next to the existing ``v1.0.0/``, so old jobs
    that captured the previous ``tool_version_id`` keep resolving
    against the bundle they were submitted with.

    Steps, in order:

    1. Resolve ``target = bundles_dir/alias/version``. Refuse if it
       exists and ``overwrite`` is ``False`` — a fresh version
       directory should never collide.
    2. ``mkdir`` the target (parents created on demand). When
       ``overwrite`` is ``True`` the directory is wiped first so old
       static files don't bleed into the new bundle.
    3. Extract ``static_zip_bytes`` into the target. Each entry path
       is resolved against the target and rejected if it escapes
       (zip-slip).
    4. Write ``<alias>.png`` from ``icon_bytes`` and
       ``<alias>_presets.json`` from ``presets_bytes`` if supplied.
    5. Serialise the supplied :class:`ToolDescriptor` to
       ``descriptor.json`` at the bundle root. Any descriptor file
       that came in via the ZIP is *overwritten* — the form-assembled
       descriptor is authoritative.

    On any failure the target directory is removed before the
    exception is re-raised, so callers never see a half-staged bundle.

    Args:
        bundles_dir: Root directory the API stages bundles under.
        descriptor: The fully-assembled descriptor to write. The
            descriptor's ``alias`` and ``version`` together drive the
            target directory.
        static_zip_bytes: Optional ZIP archive of the bundle's static
            files (scripts, data, etc.). Skipped on ``None``.
        icon_bytes: Optional PNG. Saved as ``<alias>.png``.
        presets_bytes: Optional JSON. Saved as ``<alias>_presets.json``.
        overwrite: ``True`` to wipe an existing per-version directory
            before recreating (rarely needed since every snapshot has
            a fresh version); ``False`` to refuse on collision.

    Returns:
        :class:`StagedBundle` — absolute bundle path + the descriptor.

    Raises:
        BundleStagingError: bundles_dir is unwritable, the per-version
            directory already exists (when ``overwrite=False``), the
            ZIP is malformed, an entry escapes the target, or any disk
            write fails.
    """
    target = (bundles_dir / descriptor.alias / descriptor.version).resolve()
    bundles_dir_resolved = bundles_dir.resolve()
    # Defence-in-depth: descriptor.alias + .version are already
    # validated upstream, but staging double-checks the resolved path
    # is still under bundles_dir before any mkdir.
    try:
        target.relative_to(bundles_dir_resolved)
    except ValueError as exc:  # pragma: no cover — descriptor validators catch first
        raise BundleStagingError(
            f"Refusing to stage outside the configured bundles directory: {target}"
        ) from exc

    if target.exists() and not overwrite:
        raise BundleStagingError(
            f"A bundle for {descriptor.alias!r} version {descriptor.version!r} "
            f"already exists at {target}. This shouldn't normally happen — "
            "the version is supposed to be unique per snapshot. Investigate "
            "before retrying."
        )

    if target.exists() and overwrite:
        shutil.rmtree(target)

    try:
        target.mkdir(parents=True, exist_ok=False)
        if static_zip_bytes is not None:
            _extract_zip_safely(static_zip_bytes, target)
        if icon_bytes is not None:
            (target / f"{descriptor.alias}.png").write_bytes(icon_bytes)
        if presets_bytes is not None:
            (target / f"{descriptor.alias}_presets.json").write_bytes(presets_bytes)
        # Always overwrite descriptor.json — the form-assembled
        # descriptor wins over any descriptor that came in through
        # the ZIP. Pretty-printed for human inspection / diffs.
        (target / _DESCRIPTOR_FILENAME).write_text(
            descriptor.model_dump_json(indent=2),
            encoding="utf-8",
        )
    except BundleStagingError:
        shutil.rmtree(target, ignore_errors=True)
        raise
    except (OSError, ValueError) as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise BundleStagingError(f"Couldn't stage the bundle on disk: {exc}") from exc

    return StagedBundle(bundle_path=target, descriptor=descriptor)


def copy_bundle_for_minor_bump(
    *,
    bundles_dir: Path,
    alias: str,
    source_bundle: Path,
    new_version: str,
) -> Path:
    """Copy ``source_bundle`` to ``<bundles_dir>/<alias>/<new_version>/``.

    The Session-C in-browser editor calls this when an operator saves a
    text-file edit. The minor bump owns its own per-version directory
    so previously-submitted jobs (whose ``tool_version_id`` still points
    at the source bundle's snapshot) keep resolving against their
    original files on disk.

    Refuses if the target already exists — every minor bump should mint
    a fresh version directory. Cleans up on copy failure so a partial
    target never sticks around.

    Args:
        bundles_dir: Root directory the API stages bundles under.
        alias: Tool alias (drives the ``alias`` segment of the target).
        source_bundle: Existing bundle directory to clone.
        new_version: The bumped version string (drives the ``version``
            segment of the target).

    Returns:
        Absolute :class:`Path` of the newly-created target directory.

    Raises:
        BundleStagingError: ``source_bundle`` doesn't exist, the target
            already exists, or any disk operation fails.
    """
    source = source_bundle.resolve()
    if not source.is_dir():
        raise BundleStagingError(
            f"The current bundle directory {source} doesn't exist on disk. "
            "The tool's recorded bundle_path may be stale — investigate "
            "before retrying."
        )
    target = (bundles_dir / alias / new_version).resolve()
    bundles_dir_resolved = bundles_dir.resolve()
    try:
        target.relative_to(bundles_dir_resolved)
    except ValueError as exc:  # pragma: no cover — alias / version validators catch first
        raise BundleStagingError(
            f"Refusing to stage outside the configured bundles directory: {target}"
        ) from exc
    if target.exists():
        raise BundleStagingError(
            f"A bundle for {alias!r} version {new_version!r} already exists at "
            f"{target}. This shouldn't normally happen — every minor bump "
            "should land on a fresh version directory. Investigate before retrying."
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, symlinks=False)
    except (OSError, shutil.Error) as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise BundleStagingError(
            f"Couldn't clone the bundle for {alias!r} {new_version!r}: {exc}"
        ) from exc
    return target


# Upper bound on the *uncompressed* total we will write to disk when staging
# a tool's static-files ZIP. The upload cap bounds only the compressed bytes,
# so a well-formed ZIP can expand orders of magnitude larger (a "decompression
# bomb") and fill the API host's disk. 1 GiB is far above any real static
# bundle yet caps the blast radius; the ZIP central directory declares each
# entry's uncompressed size, so we can refuse before extracting.
_MAX_UNCOMPRESSED_STATIC_BYTES = 1024 * 1024 * 1024


def _extract_zip_safely(zip_bytes: bytes, target: Path) -> None:
    """Unzip ``zip_bytes`` into ``target`` with zip-slip + zip-bomb protection.

    Each entry's resolved path must stay under ``target`` — entries
    with absolute paths or ``..`` segments that would escape are
    rejected with :class:`BundleStagingError`. Symlinks aren't
    extracted (zipfile doesn't materialise them by default on Python's
    standard implementation, but we surface a clear error if one is
    encountered explicitly). The total uncompressed size is capped so a
    decompression bomb can't fill the disk.
    """
    target_resolved = target.resolve()
    try:
        with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
            # First pass — validate every entry path before writing
            # anything, so a malicious entry late in the archive can't
            # slip through after benign earlier entries have landed.
            total_uncompressed = 0
            for info in zf.infolist():
                # Reject explicit symlink entries.
                if (info.external_attr >> 16) & _S_IFMT == _S_IFLNK:
                    raise BundleStagingError(
                        f"Symlink entries are not allowed in tool bundles ({info.filename!r})."
                    )
                resolved = (target / info.filename).resolve()
                try:
                    resolved.relative_to(target_resolved)
                except ValueError as exc:
                    raise BundleStagingError(
                        f"Bundle entry {info.filename!r} escapes the bundle directory."
                    ) from exc
                total_uncompressed += max(info.file_size, 0)
                if total_uncompressed > _MAX_UNCOMPRESSED_STATIC_BYTES:
                    raise BundleStagingError(
                        "The static-files upload expands to more than "
                        f"{_MAX_UNCOMPRESSED_STATIC_BYTES // (1024 * 1024)} MiB "
                        "uncompressed, which is refused as a decompression-bomb guard."
                    )
            # Second pass — actually extract.
            zf.extractall(target)
    except zipfile.BadZipFile as exc:
        raise BundleStagingError("The static-files upload isn't a valid ZIP archive.") from exc
