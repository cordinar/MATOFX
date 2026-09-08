"""JSON contracts for the execution backend.

Three documents are written to / read from the per-job working
directory; these are the closed schemas all targets honour:

- :class:`InputsDocument` — written by MATOFX into
  ``<wd>/inputs/inputs.json`` at submit time. Parsed by the tool's
  ``<tool_alias>.py`` script.
- :class:`OutputsDocument` — written by ``<tool_alias>.py`` into
  ``<wd>/outputs/outputs.json`` at end of run. Parsed by MATOFX
  on terminal state to populate the UI's results panel.
- :class:`HeartbeatRecord` — written by the launcher into
  ``<wd>/heartbeat/{running,complete,failed}.json``. Read by the
  runner daemon on its poll loop.

All three are :class:`pydantic.BaseModel` subclasses so a malformed
file produced by a buggy tool surfaces as a clean
:class:`pydantic.ValidationError` rather than a silent KeyError or
type confusion downstream.

Per the per-customer feature-toggle and intuitive-UI rules, the
field labels and units carried in the documents are exactly the
descriptor labels and units — the renderer doesn't need to look
back at the descriptor to format the result.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from matofx.tools.descriptor import InputFieldType, OutputFieldType

INPUTS_DOCUMENT_SCHEMA_VERSION: int = 1
"""Bumped only on incompatible field-set changes; tools record the version they target."""

OUTPUTS_DOCUMENT_SCHEMA_VERSION: int = 1
"""Bumped only on incompatible field-set changes."""


class _Frozen(BaseModel):
    """Frozen base — extra fields rejected so typos surface immediately."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


# --- Inputs document -----------------------------------------------------


class InputValue(_Frozen):
    """One input value, ready to be consumed by the tool's main script.

    Carries label and unit alongside the raw ``value`` so the tool
    can echo them in plots / reports without re-parsing the
    descriptor — convenient for tools that publish results back into
    a visual summary.
    """

    name: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    type: InputFieldType
    unit: str | None = None
    value: Any = None
    """Whatever the user entered. ``None`` when the field is optional and unset."""


class InputsDocument(_Frozen):
    """Top-level shape of ``<wd>/inputs/inputs.json``."""

    schema_version: int = Field(default=INPUTS_DOCUMENT_SCHEMA_VERSION)
    job_id: int = Field(ge=1)
    tool_alias: str = Field(min_length=1, max_length=64)
    tool_version: str = Field(min_length=1, max_length=32)
    inputs: list[InputValue] = Field(default_factory=list)


# --- Outputs document ----------------------------------------------------


class OutputValue(_Frozen):
    """One output value the tool produced.

    ``value`` carries the scalar result for ``number`` / ``string``
    outputs. For ``file`` outputs the value is conventionally
    ``None`` and ``filename`` points to the artefact under the
    working directory (e.g. ``"outputs/report.html"``).
    """

    name: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    type: OutputFieldType
    unit: str | None = None
    value: Any = None
    filename: str | None = None
    """Set for ``file`` outputs; otherwise ``None``."""


class OutputsDocument(_Frozen):
    """Top-level shape of ``<wd>/outputs/outputs.json``."""

    schema_version: int = Field(default=OUTPUTS_DOCUMENT_SCHEMA_VERSION)
    tool_alias: str = Field(min_length=1, max_length=64)
    outputs: list[OutputValue] = Field(default_factory=list)


# --- Heartbeat protocol --------------------------------------------------


class HeartbeatState(StrEnum):
    """Closed vocabulary for heartbeat-file basenames.

    Matches the ``job.heartbeat_state`` CHECK constraint on the
    database row, so the value can round-trip without translation.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


HeartbeatStateLiteral = Literal["pending", "running", "complete", "failed"]


class HeartbeatRecord(_Frozen):
    """The contents of any of the heartbeat files.

    Always sized to fit comfortably in a single page write so reads
    on the polling side never see a torn document — the launcher
    writes to ``<file>.tmp`` then renames over the target path, but
    even an unprotected reader would see either the old contents or
    the new contents, never a half-flushed mix.
    """

    state: HeartbeatStateLiteral
    started_at: str
    """ISO-8601 UTC timestamp the launcher recorded at start."""

    ended_at: str | None = None
    """ISO-8601 UTC timestamp recorded when the tool exited.
    ``None`` for the ``running`` heartbeat; populated for
    ``complete`` and ``failed``."""

    pid: int | None = None
    """OS process id the launcher started; surfaced so a
    cancellation can target the exact process tree."""

    host: str | None = None
    """Host the tool ran on. Populated for AOS / HPC so operators
    can correlate with the cluster's own logs."""

    exit_code: int | None = None
    """Process exit code; ``0`` on ``complete``, non-zero on
    ``failed``. ``None`` for ``running``."""

    signal: str | None = None
    """Signal name (e.g. ``"SIGTERM"``, ``"SIGKILL"``) when the
    process was killed; ``None`` when it exited normally."""

    failure_reason: str | None = None
    """Plain-English explanation surfaced in the UI when
    ``state == "failed"``. Per the SPDM-end-user feedback rule:
    keep it jargon-free."""
