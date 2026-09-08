"""Execution backend for the ``matofx.tools`` runner.

Three orthogonal concerns live here:

- :mod:`matofx.tools.exec.contracts` — the ``inputs.json`` and
  ``outputs.json`` schemas that every tool's ``<tool_alias>.py``
  consumes / produces. Same on every execution target.
- :mod:`matofx.tools.exec.staging` — creates the per-job working
  directory under the chosen ``StorageLocation`` mount, copies the
  tool's bundle into it, writes ``inputs/inputs.json`` with the
  user's form values.
- :mod:`matofx.tools.exec.launcher` — the small Python script
  MATOFX ships into every working directory. It writes the
  heartbeat-state files (``running.json`` / ``complete.json`` /
  ``failed.json``) and spawns the tool's ``<tool_alias>.py`` under
  the configured Python interpreter. Same launcher used for local /
  AOS / HPC; the differences are *how* it gets started, not what it
  does.
- :mod:`matofx.tools.exec.cleanup` — applies ``retain_files`` and
  ``delete_io_files`` from the descriptor after the run finishes.

Per-target backends (``LocalTarget``, ``SshAosTarget``,
``WinRmAosTarget``, ``SlurmHpcTarget``, ``MsHpcTarget``) land as
follow-up sub-tasks; they all build on this foundation.

Public surface:

- :class:`InputsDocument`, :class:`OutputsDocument`,
  :class:`InputValue`, :class:`OutputValue` — the JSON contracts.
- :class:`HeartbeatState` and :class:`HeartbeatRecord` — the
  closed vocabulary the launcher writes into ``heartbeat/*.json``.
- :func:`stage_working_dir` — given a job + tool + storage
  location + form values, lay down the working dir.
- :func:`render_launcher_script` — return the launcher script as a
  string, with the agreed grace-period env-var protocol.
- :func:`apply_cleanup` — honour ``retain_files`` /
  ``delete_io_files`` after a successful run.
"""

from __future__ import annotations

from matofx.tools.exec.aos_gpu import AosGpuNotImplementedError, AosGpuScheduler
from matofx.tools.exec.aos_ssh import (
    AosTargetError,
    SshAosTarget,
    SshConnectOptions,
)
from matofx.tools.exec.aos_winrm import (
    WinRmAosTarget,
    WinRmConnectOptions,
)
from matofx.tools.exec.base import (
    ExecutionTarget,
    ExecutionTargetKind,
    JobObservation,
    LogsRef,
    ResourceRequest,
    SubmitResult,
    TargetContext,
)
from matofx.tools.exec.cleanup import apply_cleanup
from matofx.tools.exec.contracts import (
    INPUTS_DOCUMENT_SCHEMA_VERSION,
    OUTPUTS_DOCUMENT_SCHEMA_VERSION,
    HeartbeatRecord,
    HeartbeatState,
    InputsDocument,
    InputValue,
    OutputsDocument,
    OutputValue,
)
from matofx.tools.exec.hpc_mshpc import MsHpcHpcTarget
from matofx.tools.exec.hpc_slurm import HpcTargetError, SlurmHpcTarget
from matofx.tools.exec.launcher import (
    LAUNCHER_FILENAME,
    LauncherInputs,
    render_launcher_script,
)
from matofx.tools.exec.local import LocalTarget
from matofx.tools.exec.staging import (
    HEARTBEAT_DIRNAME,
    INPUTS_DIRNAME,
    LOGS_DIRNAME,
    OUTPUTS_DIRNAME,
    StagedWorkingDir,
    stage_working_dir,
)
from matofx.tools.exec.uploads import (
    DRAFTS_DIRNAME,
    StoredUpload,
    UploadError,
    list_draft_uploads,
    promote_draft_uploads_into_working_dir,
    remove_draft_upload,
    stage_draft_upload,
    stage_wired_file_inputs,
)

__all__ = [
    "DRAFTS_DIRNAME",
    "HEARTBEAT_DIRNAME",
    "INPUTS_DIRNAME",
    "INPUTS_DOCUMENT_SCHEMA_VERSION",
    "LAUNCHER_FILENAME",
    "LOGS_DIRNAME",
    "OUTPUTS_DIRNAME",
    "OUTPUTS_DOCUMENT_SCHEMA_VERSION",
    "AosGpuNotImplementedError",
    "AosGpuScheduler",
    "AosTargetError",
    "ExecutionTarget",
    "ExecutionTargetKind",
    "HeartbeatRecord",
    "HeartbeatState",
    "HpcTargetError",
    "InputValue",
    "InputsDocument",
    "JobObservation",
    "LauncherInputs",
    "LocalTarget",
    "LogsRef",
    "MsHpcHpcTarget",
    "OutputValue",
    "OutputsDocument",
    "ResourceRequest",
    "SlurmHpcTarget",
    "SshAosTarget",
    "SshConnectOptions",
    "StagedWorkingDir",
    "StoredUpload",
    "SubmitResult",
    "TargetContext",
    "UploadError",
    "WinRmAosTarget",
    "WinRmConnectOptions",
    "apply_cleanup",
    "list_draft_uploads",
    "promote_draft_uploads_into_working_dir",
    "remove_draft_upload",
    "render_launcher_script",
    "stage_draft_upload",
    "stage_wired_file_inputs",
    "stage_working_dir",
]
