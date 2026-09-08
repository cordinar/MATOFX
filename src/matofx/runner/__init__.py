"""The matofx-runner daemon.

A separate long-lived process started alongside ``matofx-api``. The
runner owns three async loops on a single event loop:

1. **Local supervisor** — picks up jobs in DB state ``"queued"`` whose
   tool targets ``"local"``, respects the configured concurrency cap,
   spawns the launcher via :class:`LocalTarget`, and transitions the
   job to ``"running"`` once the PID is recorded.
2. **Heartbeat watcher** — for every job in DB state ``"running"``,
   polls the heartbeat directory at the configured interval and
   transitions the canonical state when the launcher writes a
   terminal heartbeat. On terminal state it parses
   ``outputs/outputs.json`` and applies the descriptor's
   ``retain_files`` / ``delete_io_files`` cleanup.
3. **HPC scheduler poller** — placeholder for sub-task #4. Calls the
   target's ``poll`` to catch scheduler-side state changes (queued
   on the cluster, running on a compute node, etc.).

A fourth task hot-reloads :class:`RuntimeSettings` every 30 s so an
admin's change to ``local_concurrency`` / interval / grace takes
effect without a restart.

Public surface:

- :class:`RuntimeSettings` and :func:`load_runtime_settings` —
  read the admin-tunable knobs from the ``runtime_setting`` table.
- :data:`RUNTIME_SETTINGS_CATALOG`, :class:`SettingSpec` —
  the closed catalog of admin-tunable keys with type / range / label.
- :class:`RuntimeSettingsService`,
  :func:`make_runtime_settings_service` — admin CRUD for the
  ``runtime_setting`` table; permission-gated and audited.
- :class:`RuntimeSettingsError`,
  :class:`RuntimeSettingsAccessDeniedError`,
  :class:`UnknownSettingKeyError`,
  :class:`InvalidSettingValueError` — plain-English errors safe
  for admin-UI rendering.
- :class:`TargetRegistry` — maps a :class:`Tool`'s
  ``execution_target`` value to an :class:`ExecutionTarget`
  instance. Local target is the only built-in for now.
- :class:`RunnerSupervisor` — the long-lived async coordinator.
- :func:`run_daemon` — top-level entry point started by
  ``matofx-runner`` (see ``pyproject.toml`` ``[project.scripts]``).
"""

from __future__ import annotations

from matofx.runner.admin_service import (
    RuntimeSettingsService,
    make_runtime_settings_service,
)
from matofx.runner.daemon import run_daemon
from matofx.runner.errors import (
    InvalidSettingValueError,
    RuntimeSettingsAccessDeniedError,
    RuntimeSettingsError,
    UnknownSettingKeyError,
)
from matofx.runner.registry import TargetRegistry, UnsupportedTargetError
from matofx.runner.settings import (
    DEFAULT_RUNTIME_SETTINGS,
    RUNTIME_SETTINGS_CATALOG,
    RuntimeSettings,
    SettingDefault,
    SettingKind,
    SettingSpec,
    load_runtime_settings,
)
from matofx.runner.supervisor import RunnerSupervisor

__all__ = [
    "DEFAULT_RUNTIME_SETTINGS",
    "RUNTIME_SETTINGS_CATALOG",
    "InvalidSettingValueError",
    "RunnerSupervisor",
    "RuntimeSettings",
    "RuntimeSettingsAccessDeniedError",
    "RuntimeSettingsError",
    "RuntimeSettingsService",
    "SettingDefault",
    "SettingKind",
    "SettingSpec",
    "TargetRegistry",
    "UnknownSettingKeyError",
    "UnsupportedTargetError",
    "load_runtime_settings",
    "make_runtime_settings_service",
    "run_daemon",
]
