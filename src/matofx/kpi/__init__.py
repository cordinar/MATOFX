"""KPI extraction + per-tool results warehouse (ADR-0012).

A descriptor-declared, write-once index of headline numeric results
across every completed job — the **cross-job discovery layer** every
commercial SPDM ships and the typical regulated-industry customer
expects. Decision doc: ``decisions/0012-kpi-extraction.md``; build
spec: ``designs/kpi-extraction.md``.

Tool authors mark scalar-numeric outputs as ``kpi = true`` in the
descriptor. At job completion, the runner's post-completion hook reads
the captured value from the already-loaded ``outputs.json`` and writes
it to :class:`JobKpiValue` — one row per ``(job, kpi)`` pair. A new
``/results/{tool_alias}`` page surfaces the cross-job table with
filtering, plotting, CSV/PDF export, and lineage click-through.

The bulk ``outputs.json`` ages out per ADR-0003 retention; KPI rows
persist forever. The table is, by design, the **permanent headline
index** — the bulk document is the archive.

Built in slices (design ``designs/kpi-extraction.md`` §11):

- **Slice 0 (this module set):** the feature flag
  (:class:`matofx.features.Feature.KPI_EXTRACTION`), the
  :func:`matofx.ui._features.kpi_extraction_enabled` UI helper, and
  the empty package skeleton with the error base + pre-declared
  symbols. No DB changes, no API surface, no UI surface; toggling the
  flag is functionally a no-op except that downstream slices'
  gate-points now exist as importable callables.
- **Slice 1:** ``OutputField.kpi`` + ``kpi_label`` + ``kpi_unit`` +
  ``kpi_category`` descriptor fields + the ``_kpi_consistency``
  validator (only ``type == "number"`` outputs may carry
  ``kpi = true``) + duplicate-``kpi_label`` rejection.
- **Slice 2:** migration ``0057_job_kpi_values`` + the
  :class:`JobKpiValue` ORM + the composite UNIQUE + the two indexes.
- **Slice 3:** :class:`KpiService.extract_for_job` + the
  ``runner.supervisor._post_complete`` hook + ``Decimal`` casting +
  sentinel ``NULL`` handling for missing / non-numeric / NaN / ±Inf.
- **Slice 4:** :class:`KpiService.reextract_tool_version` + the
  ``ToolService.promote_version`` trigger on KPI-set delta + the
  in-memory "warehouse refreshing" state + ``kpi.reextracted`` audit.
- **Slice 5:** API — ``GET /api/tools/{alias}/kpis``, ``/results``,
  ``/results/timeseries``, ``/results/export.csv`` (+ optional
  ``job_ids`` allow-list for the selected-rows export).
- **Slice 6:** ``/results/{tool_alias}`` page — header, filter card,
  AG-Grid table, unbounded selection + "N selected" pill, "View in
  Compare" handoff (auto-disabled past 6 with tooltip).
- **Slice 7:** Plotly card (X/Y/series/marker-size + time-series
  toggle) + "Download CSV (filtered)" + "Download selected as
  CSV (N)" + "Download PDF" via ``results.html.j2``.
- **Slice 8:** ADR-0004 report integration — ``ReportContext.kpi_values``
  + headline-KPI block in ``report.html.j2``.
- **Slice 9:** ADR-0011 V&V status filter wiring on the warehouse page.
- **Slice 10:** per-job page "Headline KPIs" card + sparkline.
- **Slice 11:** descriptor-wizard KPI editor + ``kpi_category``
  autocomplete + "KPIs declared" badge.
- **Slice 12:** docs — ``ARCHITECTURE.md §16.25`` + ``ROADMAP.md`` tick
  + remove the V&V slice 13 deferral marker.

The feature flag follows the project-wide toggle rule (see
:class:`matofx.features.Feature.VV_TRACKING` for the precedent): the
data path stays present always, the surfaces hide when off. Toggling
off ⇒ no rows written by the post-completion hook; turning back on
starts populating ``job_kpi_value`` from new completions only —
back-fill of historical jobs is the slice 4 re-extraction action.
"""

from __future__ import annotations

from matofx.kpi._reextract_state import is_reextract_in_progress
from matofx.kpi.errors import (
    KpiError,
    KpiExtractionFailedError,
    KpiNotDeclaredError,
    KpiReextractInProgressError,
)
from matofx.kpi.orm import JobKpiValue
from matofx.kpi.service import (
    KpiExtractResult,
    KpiReextractResult,
    TimeseriesPoint,
    ToolKpiCatalogEntry,
    WarehouseFilters,
    WarehousePage,
    WarehouseRow,
    compute_kpi_signature,
    extract_kpis_for_job,
    iter_warehouse_csv,
    list_kpis_for_tool,
    list_warehouse_rows,
    reextract_tool_version,
    should_reextract_after_descriptor_change,
    timeseries_for_kpi,
)

__all__ = [
    "JobKpiValue",
    "KpiError",
    "KpiExtractResult",
    "KpiExtractionFailedError",
    "KpiNotDeclaredError",
    "KpiReextractInProgressError",
    "KpiReextractResult",
    "TimeseriesPoint",
    "ToolKpiCatalogEntry",
    "WarehouseFilters",
    "WarehousePage",
    "WarehouseRow",
    "compute_kpi_signature",
    "extract_kpis_for_job",
    "is_reextract_in_progress",
    "iter_warehouse_csv",
    "list_kpis_for_tool",
    "list_warehouse_rows",
    "reextract_tool_version",
    "should_reextract_after_descriptor_change",
    "timeseries_for_kpi",
]
