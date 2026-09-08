"""Per-draft file-upload staging on the shared mount.

Tool input forms support three file-source modes:

1. **Upload a file** (drag/drop into the field or browse via File Explorer).
2. **Pick a file from a previous job** (re-use without re-uploading).
3. **Type a path** (legacy / power-user fallback).

This module owns mode 1's on-disk layout. Uploads for an unsubmitted
draft job land under the shared storage location's mount at::

    <mount>/drafts/<env>/job_<id>/<field_name>/<filename>

- ``drafts/`` namespace keeps unsubmitted uploads visually separate
  from the ``jobs/<env>/job_<id>/`` working directory layout
  :mod:`matofx.tools.exec.staging` writes to at queue time.
- ``<env>`` (``Settings.environment``) follows the same segmentation
  applied to staged working dirs — a single mount serving dev /
  staging / prod doesn't have uploads from three deployments
  interleaved at the top level.
- ``<field_name>`` subdir avoids collisions when two file inputs on
  the same tool accept files of the same name (one ``inputs.csv`` for
  ``materials_csv``, another for ``temperatures_csv``).

When the draft is queued, :meth:`matofx.hierarchy.service.JobService.queue_for_runner`
calls :func:`promote_draft_uploads_into_working_dir` to copy every
staged upload into the freshly-staged working directory under
``inputs/uploaded/<field_name>/<filename>``, then rewrite the
matching ``inputs[<field_name>]`` values to point at the new
location so the tool's main script can read from ``inputs.json`` and
find the file. The draft staging directory is removed once
everything is promoted so the next draft for the same job id starts
clean.

Filenames are sanitised (POSIX-safe; no path segments; no NUL) and
the resolved target path is checked to stay below the draft root —
defensive against a forged client trying to escape the namespace
via ``..`` segments or symlink-like trickery.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DRAFTS_DIRNAME: str = "drafts"
"""Top-level segment under the storage mount for unsubmitted uploads."""


class UploadError(RuntimeError):
    """Raised when an upload cannot be persisted to the draft area.

    Message is plain-English and surfaced verbatim to the user (the
    API layer converts it to a friendly error body).
    """


@dataclass(frozen=True)
class StoredUpload:
    """One file's place on disk after :func:`stage_draft_upload`."""

    field_name: str
    filename: str
    absolute_path: Path
    size_bytes: int


def _safe_filename_segment(value: str) -> str:
    """Sanitise a filename to a single POSIX-safe segment.

    Strips any path components (``./``, ``../``, drive letters,
    backslashes), rejects NUL bytes, and keeps only the leaf. An
    empty result raises :class:`UploadError` so the caller surfaces
    a friendly "give me a real filename" message rather than
    silently writing to an unnamed file.
    """
    if not value or "\x00" in value:
        raise UploadError("That filename can't be empty or contain NUL bytes.")
    # ``Path(..).name`` gives the leaf for both POSIX and Windows-style
    # separators when the parser is run on the same OS as the caller.
    # We additionally split on the other separator manually so a
    # Windows path uploaded on a POSIX server still loses its prefix.
    leaf = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not leaf or leaf in (".", ".."):
        raise UploadError("That filename is not valid.")
    # Reject filenames that consist of nothing but the kind of
    # characters a file system rejects (control chars, etc.). We keep
    # spaces, dots, dashes, underscores, parentheses and unicode word
    # characters; everything else is replaced with ``_`` so the
    # operator never gets a 500 from a stray colon in a Windows-source
    # filename.
    cleaned = []
    for ch in leaf:
        if ch in {"\t", "\r", "\n"}:
            cleaned.append("_")
        elif ord(ch) < 32:
            cleaned.append("_")
        elif ch in {"<", ">", ":", '"', "|", "?", "*"}:
            cleaned.append("_")
        else:
            cleaned.append(ch)
    return "".join(cleaned).strip()


def _safe_field_segment(value: str) -> str:
    """Sanitise a descriptor input field name to a path segment.

    Field names already follow the ``[a-z][a-z0-9_]*`` convention (see
    :func:`matofx.tools.descriptor.InputField._name_is_safe_identifier`),
    but we re-validate here so the API endpoint can't be tricked into
    writing under ``../`` by a caller that forged the URL parameter.
    """
    if not value:
        raise UploadError("Missing input field name.")
    cleaned = "".join(c for c in value if c.isalnum() or c == "_")
    if not cleaned or not cleaned[0].isalpha():
        raise UploadError(f"Invalid input field name {value!r}.")
    return cleaned


def _safe_env_segment(environment: str | None) -> str:
    """Mirror of :func:`matofx.hierarchy.service._safe_env_segment`.

    Duplicated here rather than imported so this module stays cheap
    and free of the hierarchy → tools back-import that other staging
    code carefully avoids.
    """
    if not environment:
        return ""
    return "".join(c for c in environment.lower() if c.isalnum() or c in "-_")


def draft_root_for(*, mount_path: str, environment: str | None, job_id: int) -> Path:
    """Return the absolute draft directory for ``job_id``.

    The caller doesn't need to know whether the directory exists yet;
    :func:`stage_draft_upload` creates it on first write.
    """
    env_segment = _safe_env_segment(environment)
    if env_segment:
        return Path(mount_path) / DRAFTS_DIRNAME / env_segment / f"job_{job_id:07d}"
    return Path(mount_path) / DRAFTS_DIRNAME / f"job_{job_id:07d}"


def stage_draft_upload(
    *,
    mount_path: str,
    environment: str | None,
    job_id: int,
    field_name: str,
    filename: str,
    content: bytes,
    max_bytes: int | None = None,
) -> StoredUpload:
    """Write ``content`` to the draft staging area; return the location.

    A second upload to the same ``(job_id, field_name)`` pair is
    treated as a replacement — the previous file (and any siblings in
    the same field's subdir, in case the operator uploaded a
    differently-named file the first time) is removed before the new
    bytes land. This keeps the form's mutual-exclusion contract
    enforced on disk: one file per file-input field at a time.

    Args:
        mount_path: Root of the active :class:`StorageLocation`.
        environment: ``Settings.environment`` so the draft path slots
            under the right env segment (``drafts/staging/...``).
            ``None`` falls back to ``drafts/job_<id>/`` (tests).
        job_id: The draft job's primary-key id.
        field_name: Descriptor input ``name`` (e.g. ``materials_csv``).
        filename: Client-supplied filename (browser-sourced).
        content: Raw bytes to write.
        max_bytes: Optional size cap. When set and exceeded, raises
            :class:`UploadError` with a plain-English message so the
            API can surface the descriptor's ``max_file_size_mb``
            limit to the user. ``None`` means "no cap here" (the API
            layer may still enforce one).

    Returns:
        :class:`StoredUpload` carrying the absolute path the file
        landed at; the caller persists this path as the field's
        ``Job.inputs[field_name]`` value.

    Raises:
        UploadError: when sanitisation fails, the mount is unreachable,
            the size cap is exceeded, or any underlying ``OSError``
            surfaces. Always plain-English, ready to surface to the
            user.
    """
    if max_bytes is not None and len(content) > max_bytes:
        raise UploadError(
            f"That file is too large ({len(content):,} bytes). "
            f"This input accepts files up to {max_bytes:,} bytes — "
            "ask an administrator to raise the cap if you need more."
        )

    field_segment = _safe_field_segment(field_name)
    safe_filename = _safe_filename_segment(filename)
    root = draft_root_for(mount_path=mount_path, environment=environment, job_id=job_id)
    field_dir = root / field_segment
    target = field_dir / safe_filename

    # Resolve target back against the draft root — a forged filename
    # that smuggled traversal segments past sanitisation would land
    # outside ``field_dir``; refuse before writing.
    try:
        resolved_field = field_dir.resolve(strict=False)
        resolved_target = target.resolve(strict=False)
        resolved_target.relative_to(resolved_field)
    except (OSError, ValueError) as exc:
        raise UploadError(
            "Couldn't determine a safe location for that upload — try a simpler filename."
        ) from exc

    try:
        # Replacement semantics: drop everything currently in the
        # field's directory before writing the new bytes so the
        # draft state matches the form's single-file UI.
        if field_dir.exists():
            for existing in field_dir.iterdir():
                if existing.is_file():
                    try:
                        existing.unlink()
                    except OSError:
                        # Best-effort — if a stale file can't be
                        # cleaned up we still write the new one; the
                        # form state already overwrote the input value.
                        continue
        field_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    except OSError as exc:
        raise UploadError(
            f"Couldn't save the upload to disk: {exc}. "
            "Check that the storage mount is reachable and writable."
        ) from exc

    return StoredUpload(
        field_name=field_segment,
        filename=safe_filename,
        absolute_path=target,
        size_bytes=len(content),
    )


def list_draft_uploads(
    *,
    mount_path: str,
    environment: str | None,
    job_id: int,
) -> dict[str, StoredUpload]:
    """Return ``{field_name: StoredUpload}`` for every draft upload.

    Missing draft directory returns an empty dict. Used by the form
    widget on workspace re-open: the UI walks the descriptor's file
    inputs and seeds each widget's "selected file" indicator from this
    map so a user who closed the tab mid-upload doesn't lose state.
    """
    root = draft_root_for(mount_path=mount_path, environment=environment, job_id=job_id)
    out: dict[str, StoredUpload] = {}
    if not root.is_dir():
        return out
    for field_dir in root.iterdir():
        if not field_dir.is_dir():
            continue
        # Each field dir holds at most one file (replacement semantics
        # in stage_draft_upload). If multiple exist (e.g. someone
        # dropped a file in by hand), surface the lexicographically
        # first one — deterministic and matches what a re-render would
        # do after the next upload's replacement sweep.
        files = sorted(p for p in field_dir.iterdir() if p.is_file())
        if not files:
            continue
        target = files[0]
        try:
            size = target.stat().st_size
        except OSError:
            size = 0
        out[field_dir.name] = StoredUpload(
            field_name=field_dir.name,
            filename=target.name,
            absolute_path=target,
            size_bytes=size,
        )
    return out


def remove_draft_upload(
    *,
    mount_path: str,
    environment: str | None,
    job_id: int,
    field_name: str,
) -> bool:
    """Remove the draft upload for one field; return ``True`` when something was deleted.

    Best-effort; a stuck file is logged at the call site, not raised,
    so the user's "Clear" click never wedges the UI. Re-uploading
    after a failed clear still works (``stage_draft_upload`` sweeps
    the field dir before writing).
    """
    field_segment = _safe_field_segment(field_name)
    root = draft_root_for(mount_path=mount_path, environment=environment, job_id=job_id)
    field_dir = root / field_segment
    if not field_dir.is_dir():
        return False
    removed = False
    for existing in field_dir.iterdir():
        if existing.is_file():
            try:
                existing.unlink()
                removed = True
            except OSError:
                continue
    # Tidy the empty field dir so a subsequent ``list_draft_uploads``
    # doesn't return a stale entry pointing at no file.
    try:
        field_dir.rmdir()
    except OSError:
        pass
    return removed


def promote_draft_uploads_into_working_dir(
    *,
    mount_path: str,
    environment: str | None,
    job_id: int,
    working_dir_absolute: Path,
    inputs: dict[str, object],
) -> dict[str, object]:
    """Copy draft uploads into the working dir and rewrite the input map.

    Called from :meth:`JobService.queue_for_runner` after
    :func:`matofx.tools.exec.staging.stage_working_dir` has built the
    working directory. For each input field whose value is a path
    inside the draft area, the file is copied into the working dir at
    ``inputs/uploaded/<field_name>/<filename>`` and the corresponding
    ``inputs[field_name]`` value is rewritten to the new path. Other
    inputs (typed paths, previous-job picks, non-file inputs) pass
    through unchanged.

    The draft staging directory is removed once every file has been
    promoted so the on-disk state matches "this job is no longer a
    draft".

    Args:
        mount_path: Active storage location's mount.
        environment: ``Settings.environment``.
        job_id: The job being queued.
        working_dir_absolute: The absolute path returned by
            :func:`stage_working_dir`.
        inputs: The form values about to be persisted onto
            ``Job.inputs``. Returned with file-input values rewritten.

    Returns:
        A new dict with any draft-pointing file paths replaced by
        their working-dir-resolved equivalents. The original dict is
        not mutated.
    """
    import shutil  # noqa: PLC0415 — keeps the IO at the seam

    draft_root = draft_root_for(mount_path=mount_path, environment=environment, job_id=job_id)
    if not draft_root.is_dir():
        return dict(inputs)

    uploaded_dir = working_dir_absolute / "inputs" / "uploaded"
    uploaded_dir.mkdir(parents=True, exist_ok=True)

    new_inputs: dict[str, object] = dict(inputs)
    draft_root_str = str(draft_root.resolve())
    for field_name, value in list(inputs.items()):
        if not isinstance(value, str) or not value:
            continue
        try:
            value_path = Path(value).resolve(strict=False)
        except OSError:
            continue
        try:
            value_path.relative_to(draft_root_str)
        except ValueError:
            # Not a draft-area path — leave the value alone.
            continue
        if not value_path.is_file():
            # The DB pointed at a draft path that no longer exists;
            # leave the value as-is so the runner surfaces a friendly
            # "input file not found" error rather than crashing the
            # queue step.
            continue
        field_segment = _safe_field_segment(field_name)
        target_dir = uploaded_dir / field_segment
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / value_path.name
        try:
            shutil.copy2(value_path, target)
        except OSError:
            # Defensive — propagate to the operator via the runner's
            # job-failure path rather than blowing up the queue
            # endpoint. Leaves the value pointing at the draft path
            # so the operator sees the missing-file error in context.
            continue
        new_inputs[field_name] = str(target)

    # Best-effort cleanup of the draft tree.
    try:
        shutil.rmtree(draft_root)
    except OSError:
        pass
    return new_inputs


def stage_wired_file_inputs(
    *,
    working_dir_absolute: Path,
    inputs: dict[str, object],
    wired_file_sources: Mapping[str, list[str]],
) -> dict[str, object]:
    """Copy workflow-wired upstream output files into the working dir (ADR-0015).

    The headless workflow orchestrator (:meth:`WorkflowRunService.advance`)
    resolves each file-wired input to the **absolute path** of the upstream
    member's retained output (its ``outputs/`` file) — the same shape a
    *previous-job pick* produces. This staging pass turns that reference into
    a copy: for every field in ``wired_file_sources`` the listed source
    files are copied into ``inputs/uploaded/<field_name>/<filename>`` and the
    field's input value (the upstream's *primary* output path) is rewritten
    to its staged location.

    We copy rather than reference (mirroring how uploads / library refs are
    staged) so the handoff survives the upstream job's cleanup and lands on
    the shared mount inside the downstream working dir, where every execution
    target (subprocess / SSH / WinRM / HPC) reads it uniformly.

    ``wired_file_sources`` is ``{field_name: [absolute source paths]}`` — one
    path for a single-file output, the whole series for a time-resolved
    ``field_timesteps`` output (the primary ``filename`` is identified among
    them by basename and becomes the rewritten value; siblings land in the
    same directory for a tool that wants to glob the series).

    A source that no longer exists on disk is skipped; a field with no
    surviving source is left pointing at its original value so the downstream
    job's own input validation surfaces a plain "required input missing"
    (identical to the scalar-absent path). The original ``inputs`` dict is
    not mutated.
    """
    import shutil  # noqa: PLC0415 — keeps the IO at the seam

    new_inputs: dict[str, object] = dict(inputs)
    if not wired_file_sources:
        return new_inputs

    uploaded_dir = working_dir_absolute / "inputs" / "uploaded"
    for field_name, sources in wired_file_sources.items():
        if not sources:
            continue
        primary = new_inputs.get(field_name)
        primary_name = Path(primary).name if isinstance(primary, str) and primary else None
        field_segment = _safe_field_segment(field_name)
        target_dir = uploaded_dir / field_segment
        staged_by_name: dict[str, str] = {}
        for src in sources:
            if not isinstance(src, str) or not src:
                continue
            try:
                src_path = Path(src)
            except (TypeError, ValueError):
                continue
            if not src_path.is_file():
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / src_path.name
            try:
                shutil.copy2(src_path, target)
            except OSError:
                # Defensive — leave the field pointing at the upstream path
                # so the runner surfaces the missing file in context rather
                # than blowing up the queue endpoint.
                continue
            staged_by_name[src_path.name] = str(target)
        if not staged_by_name:
            continue
        # Rewrite the field value to the staged *primary* (the representative
        # filename). Fall back to the first staged file when the primary
        # wasn't among the copied sources — the tool still gets a real path.
        if primary_name is not None and primary_name in staged_by_name:
            new_inputs[field_name] = staged_by_name[primary_name]
        else:
            new_inputs[field_name] = sorted(staged_by_name.values())[0]
    return new_inputs
