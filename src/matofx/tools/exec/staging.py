"""Working-directory staging.

Every job — local, AOS, or HPC — runs out of a per-job directory
on the shared ``StorageLocation`` mount. This module owns the
layout of that directory and the act of populating it.

Layout::

    <storage_location.mount_path>/
        <working_dir_relative>/                 # e.g. jobs/job_0000123
            <tool bundle copied verbatim>
            inputs/
                inputs.json                     # InputsDocument
            outputs/                            # tool writes outputs.json here
            logs/                               # launcher captures stdout / stderr
            heartbeat/                          # launcher writes state files
            launcher.py                         # rendered from launcher.render_launcher_script

The directory is laid out by :func:`stage_working_dir` at submit
time and never re-staged: a job's working dir is immutable from
MATOFX's side once the launcher fires.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from matofx.tools.exec.contracts import InputsDocument
from matofx.tools.exec.launcher import LAUNCHER_FILENAME, render_launcher_script

_log = logging.getLogger(__name__)

INPUTS_DIRNAME: str = "inputs"
OUTPUTS_DIRNAME: str = "outputs"
LOGS_DIRNAME: str = "logs"
HEARTBEAT_DIRNAME: str = "heartbeat"
INPUTS_JSON_FILENAME: str = "inputs.json"
ORPHAN_DIRNAME: str = "_orphaned"


def _quarantine_orphaned_dir(*, mount: Path, working_dir: Path, working_dir_relative: str) -> Path:
    r"""Move a stale pre-existing working directory aside into ``<mount>/_orphaned/``.

    A job's working directory is keyed by its (deterministic) job id, so an
    on-disk folder at that path when the job has *never* staged can only be a
    leftover from a reused job id or a database rollback that put the id
    counter behind the filesystem — never a live job's directory. Rather than
    hard-fail the submit, the orphan is *moved*, never deleted, so an operator
    can still recover a stale run's retained outputs.

    Returns the quarantine path the orphan was moved to. A numeric suffix is
    appended if a previous orphan already occupies the natural name, so
    repeated collisions never clobber each other.
    """
    orphan_root = mount / ORPHAN_DIRNAME
    orphan_root.mkdir(parents=True, exist_ok=True)
    flat = "_".join(Path(working_dir_relative).parts)
    dest = orphan_root / flat
    n = 1
    while dest.exists():
        dest = orphan_root / f"{flat}__{n}"
        n += 1
    try:
        shutil.move(str(working_dir), str(dest))
    except OSError as e:
        raise StagingError(
            f"A stale working directory {working_dir!s} is blocking this job and could "
            f"not be moved aside to {dest!s}: {e}. Check that the storage mount is "
            "writable, then re-submit."
        ) from e
    return dest


class StagingError(RuntimeError):
    """Raised when the working directory cannot be created or populated.

    The caller surfaces the message to the user, so it must be
    plain-English and actionable — never a raw OSError.
    """


@dataclass(frozen=True)
class StagedWorkingDir:
    """The artefacts :func:`stage_working_dir` produces.

    Carries absolute paths derived from the storage location's
    mount path so the caller (the runner) can pass them into a
    subprocess / SSH session without re-resolving anything.
    """

    working_dir: Path
    inputs_dir: Path
    outputs_dir: Path
    logs_dir: Path
    heartbeat_dir: Path
    inputs_json_path: Path
    launcher_path: Path


def stage_working_dir(
    *,
    mount_path: str,
    working_dir_relative: str,
    bundle_path: str,
    inputs_document: InputsDocument,
    interpreter_path: str,
    main_script_relative: str,
    cancel_grace_seconds: int,
    reclaim_orphaned: bool = False,
) -> StagedWorkingDir:
    r"""Create the working directory and fill in every artefact the tool needs.

    Args:
        mount_path: Root of the chosen storage location (e.g.
            ``r"\\fsxdrv1\matofx"``).
        working_dir_relative: Path under ``mount_path`` where the
            job lives (e.g. ``"jobs/job_0000123"``). Validated to
            stay below ``mount_path`` — a relative path with
            ``..`` segments raises :class:`StagingError` rather
            than escaping the mount.
        bundle_path: Source directory (the tool's installed
            bundle) whose contents are copied into the working
            directory verbatim.
        inputs_document: The validated :class:`InputsDocument`
            written to ``inputs/inputs.json``.
        interpreter_path: Absolute path to the Python interpreter
            the launcher will invoke (resolved by the caller from
            the chosen :class:`PythonEnv`).
        main_script_relative: Path to the tool's main script
            (``<tool_alias>.py``), relative to the bundle root —
            also relative to the working directory after staging.
        cancel_grace_seconds: How long the launcher waits between
            SIGTERM and SIGKILL when cancellation is requested.
        reclaim_orphaned: When ``True`` and the target working
            directory already exists, move the pre-existing folder
            aside into ``<mount_path>/_orphaned/`` and continue,
            instead of raising. The caller passes this only when the
            job has never successfully staged (its
            ``working_dir_relative`` is still ``NULL``), which makes
            any folder at the deterministic job path a leftover from a
            reused job id or a database rollback — never a live job's
            directory. Defaults to ``False`` (strict refuse-to-overwrite).

    Returns:
        A :class:`StagedWorkingDir` with absolute paths to every
        artefact.

    Raises:
        StagingError: when the working directory cannot be
            created (already exists, mount unreachable, parent
            missing) or when the bundle copy fails.
    """
    if ".." in Path(working_dir_relative).parts:
        raise StagingError("Working directory path may not contain parent-segment references.")
    if Path(working_dir_relative).is_absolute():
        raise StagingError("Working directory path must be relative to the storage mount.")

    mount = Path(mount_path)
    working_dir = mount / working_dir_relative

    if working_dir.exists():
        if not reclaim_orphaned:
            raise StagingError(
                f"Working directory {working_dir!s} already exists; refusing to overwrite."
            )
        quarantined = _quarantine_orphaned_dir(
            mount=mount,
            working_dir=working_dir,
            working_dir_relative=working_dir_relative,
        )
        _log.warning(
            "Reclaimed orphaned working directory %s (moved to %s). This job has "
            "never staged, so the pre-existing folder is a leftover from a reused "
            "job id or a database rollback; the stale copy has been preserved, not "
            "deleted.",
            working_dir,
            quarantined,
        )

    bundle = Path(bundle_path)
    if not bundle.exists():
        raise StagingError(
            f"Tool bundle path {bundle!s} does not exist. "
            "Re-install the tool, or have an administrator check the bundle location."
        )
    if not bundle.is_dir():
        raise StagingError(
            f"Tool bundle path {bundle!s} is not a directory. Tool bundles must be on-disk folders."
        )

    try:
        shutil.copytree(bundle, working_dir)
    except OSError as e:
        raise StagingError(
            f"Failed to stage tool bundle into {working_dir!s}: {e}. "
            "Check that the storage mount is reachable and writable."
        ) from e

    inputs_dir = working_dir / INPUTS_DIRNAME
    outputs_dir = working_dir / OUTPUTS_DIRNAME
    logs_dir = working_dir / LOGS_DIRNAME
    heartbeat_dir = working_dir / HEARTBEAT_DIRNAME
    for d in (inputs_dir, outputs_dir, logs_dir, heartbeat_dir):
        d.mkdir(parents=True, exist_ok=True)

    inputs_json_path = inputs_dir / INPUTS_JSON_FILENAME
    inputs_json_path.write_text(
        json.dumps(inputs_document.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )

    launcher_path = working_dir / LAUNCHER_FILENAME
    launcher_path.write_text(
        render_launcher_script(
            interpreter_path=interpreter_path,
            main_script_relative=main_script_relative,
            cancel_grace_seconds=cancel_grace_seconds,
        ),
        encoding="utf-8",
    )

    return StagedWorkingDir(
        working_dir=working_dir,
        inputs_dir=inputs_dir,
        outputs_dir=outputs_dir,
        logs_dir=logs_dir,
        heartbeat_dir=heartbeat_dir,
        inputs_json_path=inputs_json_path,
        launcher_path=launcher_path,
    )
