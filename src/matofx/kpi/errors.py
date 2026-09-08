"""User-facing errors for the KPI service (ADR-0012).

Plain English; safe to render directly to the end user. Shape parallels
:mod:`matofx.vv.errors` / :mod:`matofx.materials.errors` so the API
exception handler can register one umbrella handler (:class:`KpiError`)
and treat the domain uniformly.

Slice 0 pre-declares the slice 3 + slice 4 error symbols so the import
surface doesn't grow between slices (matches the V&V pattern of
declaring pending-slice errors up-front). They are only raised once
the corresponding slice's logic lands; until then they are inert
constructors useful for type-narrowing in router stubs.

The post-completion extraction hook itself never raises — extraction
failure degrades to a sentinel ``NULL`` row + a WARNING log
(:class:`KpiExtractionFailedError` is the *diagnostic* shape, not a
control-flow exception). See ``designs/kpi-extraction.md`` §5.1 for
the never-raise contract.
"""

from __future__ import annotations


class KpiError(Exception):
    """Base class for KPI domain errors."""


# ---- Slice 3 (extraction diagnostics) -------------------------------


class KpiExtractionFailedError(KpiError):
    """Diagnostic shape for an extraction failure on one (job, kpi) pair.

    Constructed by :class:`KpiService.extract_for_job` when a declared
    KPI can't be extracted from a job's ``outputs.json`` — used to
    populate the WARNING log line and the audit event detail, *not*
    raised to the caller. The post-completion hook must never fail
    because of a KPI extraction problem (design §6 never-raise
    contract).
    """

    def __init__(self, job_id: int, kpi_name: str, reason: str) -> None:
        """Construct with the (job, kpi) pair and the failure reason."""
        super().__init__(
            f"Couldn't extract KPI {kpi_name!r} for job {job_id}: {reason}."
        )
        self.job_id = job_id
        self.kpi_name = kpi_name
        self.reason = reason


# ---- Slice 5 (warehouse query) --------------------------------------


class KpiNotDeclaredError(KpiError):
    """A warehouse query named a KPI that isn't declared by any released version.

    Raised by :class:`KpiService.list_warehouse_rows` (and
    :meth:`KpiService.timeseries`) when the request filters on a
    ``kpi_name`` that no released ``tool_version`` of the named tool
    actually declares. Distinct from "the KPI exists but every row is
    NULL" — that is a valid empty result.
    """

    def __init__(self, tool_alias: str, kpi_name: str) -> None:
        """Construct with the tool and the unknown KPI name."""
        super().__init__(
            f"No released version of {tool_alias!r} declares a KPI called "
            f"{kpi_name!r}."
        )
        self.tool_alias = tool_alias
        self.kpi_name = kpi_name


# ---- Slice 4 (re-extraction) ----------------------------------------


class KpiReextractInProgressError(KpiError):
    """An admin-triggered re-extraction was requested while one is already running.

    Re-extractions are serialised per ``tool_version_id`` — only one
    background sweep runs for a given version at a time so the rate
    limit + audit narrative stay coherent. The fix is to wait for the
    in-flight sweep to complete (the "warehouse refreshing" banner on
    the tool's ``/results`` page shows progress).
    """

    def __init__(self, tool_version_id: int) -> None:
        """Construct with the tool_version_id whose sweep is already running."""
        super().__init__(
            "A KPI re-extraction is already running for this tool version. "
            "Wait for it to finish before starting another."
        )
        self.tool_version_id = tool_version_id
