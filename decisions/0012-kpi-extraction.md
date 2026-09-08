# 0012. KPI extraction — per-tool results warehouse

Date: 2026-05-26
Status: Accepted (architect ruling 2026-05-26 — full v1.0 scope per "Phase 2 expansion" conversation; pilot date re-baselined accordingly)

## Context

MATOFX has rich per-job result rendering (Outputs panel, downloads, the
ADR-0004 per-job HTML report) and rich within-a-set result analysis
(Compare across 2–6 picked jobs; Parametric Studies' AG-Grid matrix across
a study's children). What it does **not** have is the **cross-job
discovery layer**: "for every job ever run on this tool, give me the value
of these three outputs in a sortable / filterable / plottable / exportable
table".

This is the **results-warehouse** capability of every commercial SPDM
product, and the typical customer ask:

- A quality manager wants the max-stress KPI across the last 12 months of
  `stress_analysis` runs in a single table — filterable by project,
  scenario, V&V status, requestor — and a trend chart over time.
- An R&D lead wants to spot anomalies — "show me every `heat_transfer`
  run where the steady-state temperature exceeded 800 K".
- An executive wants a one-page dashboard showing tool-usage trends and
  the headline KPI averages per project.

Today the answer is **Compare with at most 6 jobs**, which is a totally
different need. Compare is *focused side-by-side analysis*; results-
warehouse is *aggregate cross-job discovery*. They share zero useful UI.

The data lives in `Job.outputs` (the JSON outputs document, retained per
ADR-0003) but isn't indexable or queryable in any useful way — pulling a
single output value across 10 000 jobs means downloading all 10 000
outputs documents.

The customer is the sole maintainer ([[user_role]]) and has ruled this ADR
into v1.0 at full scope as part of the 2026-05-26 Phase 2 expansion.

## Decision

Build a **KPI extraction layer**: tool descriptors declare which outputs
are KPIs; a write-path captures KPI values into a dedicated `job_kpi_value`
table at job completion; a new `/results/{tool_alias}` page presents the
cross-job table with filtering, plotting, CSV export, and lineage
click-through. Module `matofx/kpi/`. The build-level spec lands as
`designs/kpi-extraction.md`; this ADR records the decisions and rationale.

**1. KPI declaration in the descriptor.** Tool authors mark which outputs
are KPIs via a new descriptor field on `OutputField`:

```toml
[[outputs]]
name = "max_temperature"
label = "Max temperature"
unit = "K"
file_type = "scalar"
kpi = true                  # NEW — declares this output as a KPI
kpi_label = "Peak temp"     # NEW — optional short label for warehouse columns
kpi_unit = "K"              # NEW — optional unit override for warehouse display
kpi_category = "thermal"    # NEW — optional grouping label
```

Only **scalar-numeric outputs** can be flagged `kpi = true` in v1.0;
the cross-job table needs uniform numerics for sort + filter + plot.
String / file / table / image / animation / video outputs are explicitly
not KPIs. Tool authors declare which numeric outputs are KPIs via the
existing descriptor wizard.

A tool may have 0..N KPIs (no upper bound, but the warehouse UI degrades
past ~12 columns — the operator can hide columns via the existing AG-Grid
column manager).

**2. The write path — `job_kpi_value` table.** New table:

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `job_id` | FK → `Job.id` ON DELETE CASCADE | One row per (job, kpi) pair |
| `tool_alias` | TEXT | Denormalised for filter speed |
| `tool_version_id` | FK | Denormalised |
| `kpi_name` | TEXT | The descriptor's `OutputField.name` |
| `value_numeric` | NUMERIC | The captured scalar value |
| `unit` | TEXT | From descriptor (`kpi_unit` or `unit`) |
| `extracted_at` | TIMESTAMP | When the post-completion hook wrote this row |

Composite UNIQUE on `(job_id, kpi_name)` — a re-extraction (e.g. after a
descriptor change) updates the existing row, never duplicates. Indexed on
`(tool_alias, kpi_name)` for the warehouse query path; indexed on
`extracted_at` for time-series plots.

**Write timing.** The KPI extractor runs **inside `runner.supervisor`'s
job-completion finalisation**, after the existing `outputs.json` validation
and before the cleanup pass (ADR-0003 retention). Reading the value happens
once, from the already-loaded outputs document — no extra I/O per KPI.
Failure to extract a declared KPI (output missing, not numeric, NaN) logs a
warning + writes `value_numeric = NULL` so the row exists but the value is
sentinel. The table answers "did this job complete with KPI X populated?"
without a join back to `outputs.json`.

**Re-extraction.** A descriptor update that changes the KPI set triggers a
**background re-extraction** for all completed jobs of the affected tool
versions (`kpi.reextract_tool_version(tool_version_id)` — runs in the
runner daemon, low priority). This keeps the warehouse honest when an
author flags a new output as a KPI retrospectively. Re-extraction is
audited; users see a "warehouse refreshing" banner on the tool's results
page while it runs.

**3. The warehouse page — `/results/{tool_alias}`.** A new top-level UI
page beside `/job-search`:

- **Header** — tool name + version dropdown (defaults to "All versions") +
  V&V status filter (`Verified+Validated` / `Verified only` / any —
  pulls from ADR-0011).
- **Filter row** (above the table, per UI Conventions) — project /
  scenario / requestor / completion-date window / KPI value ranges (per
  KPI column, contextual to the column's unit).
- **The table** — AG Grid (already in the v2 stack via Parametric Studies'
  results matrix). One row per job; columns are job-identity columns
  (Job ID, scenario, requestor, completed-at, V&V status, lineage link)
  followed by one numeric column per declared KPI of the selected
  version(s). Per-column sort + filter; per-column show / hide; column
  reordering persists per-user.
- **Plot region** — below the table, an inline Plotly card with axis-pickers
  (any KPI column + any KPI column for X/Y, optionally a third for marker
  size or colour) and a third axis chooser for *time* (the
  `extracted_at`-driven trend chart). Defaults: "first two KPIs as X/Y,
  coloured by project". Routes through `themed_plotly` (per UI Conventions).
- **Export** — "Download as CSV" (full filtered set); "Download as PDF"
  (the table screenshot + the plot, self-contained per ADR-0004 pattern).
- **Click-through** — every job-row in the table is a hyperlink to the
  Job page; the lineage column is a hyperlink to ADR-0010's Provenance
  page.

The page is permission-gated by the existing `jobs.view` permission
(same ladder as Compare): a user sees only the jobs their per-project ACL
allows. Aggregate counts ("12 of 47 hidden by access controls") surface
under the table when filtered.

**4. API surface.**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/tools/{alias}/kpis` | List the KPIs declared by the tool's released version(s) |
| GET | `/api/tools/{alias}/results` | The warehouse query — paginated; same filters as the UI; per-row job-identity + KPI values |
| GET | `/api/tools/{alias}/results/timeseries?kpi={name}` | Time-series for one KPI (used by the trend chart) |
| GET | `/api/tools/{alias}/results/export.csv` | Full filtered set as CSV download |

ACL is applied in the query (the warehouse service composes
`JobService.list_visible_to_user`'s ACL filter).

**5. Cross-tool aggregation — v1.0 minimum, expand v1.x.** This ADR's
v1.0 deliverable is **per-tool** (`/results/{tool_alias}`) — one tool's
KPIs across all jobs. **Cross-tool aggregation** (one dashboard widget
that rolls up KPIs across many tools — "all thermal tools' max-temperature
KPIs over the last quarter") is conceptually simple given the table
exists, but the UI design is heavier. Defer to v1.x.

The data shape supports this future widget: `job_kpi_value` is queryable
by `(kpi_name, kpi_category)` across tools; a future "global dashboard"
just reads it with different aggregation.

**6. Project-rollup scorecards — v1.x.** "Show me each project's
top-N KPIs at a glance" is a different page. The data is right there;
the design is a separate exercise. Defer.

**7. Cross-cutting impacts.**

- **ADR-0003 (job-I/O retention)** — KPI extraction happens *before* the
  retention sweep, so even after `outputs.json` ages out, the KPI row
  persists. KPIs are essentially **a permanent index of headline values**;
  the full outputs document is the **archive**. This is exactly what
  customers want — the headline is queryable forever; the bulk is
  retained according to the configurable retention policy.
- **ADR-0004 (per-job HTML report)** — the report's executive-summary
  block reads from `job_kpi_value` so the headline-numbers section is
  populated automatically (the templated auto-summary feature pulls KPIs
  for its narrative).
- **ADR-0006 (file library)** — unaffected.
- **ADR-0007 (materials)** — unaffected.
- **ADR-0008 (surrogate family)** — surrogate-tool outputs respect the
  same `kpi = true` flag; a surrogate-tool job feeds the warehouse like
  any other tool's. The "uncertainty band" outputs the surrogate produces
  (`<response>_std`) are not flagged KPIs by default — operators can
  promote them if useful.
- **ADR-0009 (optimisation framework)** — optimisation studies' children
  populate the warehouse identically; the warehouse becomes one of the
  primary surfaces for inspecting an optimisation run's outcomes
  alongside the Convergence tab.
- **ADR-0010 (data lineage)** — every warehouse row links to its lineage
  page.
- **ADR-0011 (V&V tracking)** — the warehouse filters by V&V status; a
  V&V-required project sees only V&V'd-tool runs by default.
- **Compare (§16.3)** — Compare and the warehouse are complementary, not
  competing. Compare is for ≤6-job focused review; the warehouse is for
  cross-job discovery and dashboarding. The warehouse's "View in Compare"
  action lets a user pick rows in the table and switch to Compare with
  those rows pre-selected.

**8. Out-of-scope for v1.0** (no-add-back lock):

- **Cross-tool / global KPI dashboard.** The data shape supports it; the
  UI is a v1.x exercise.
- **Saved warehouse queries.** Users can re-apply filters; saving a
  filter set + sharing it lands v1.x.
- **Scheduled exports.** "Email me this CSV every Monday." Out — the
  notifications layer is single-event for v1.0.
- **Custom KPI formulae.** "KPI = output_a / output_b." The descriptor's
  `kpi = true` is the only authoring path in v1.0; user-defined derived
  KPIs are a v1.x feature.
- **Aggregation by descriptor metadata** (project metadata, requestor
  team). Filter-yes, aggregate-by-grouping-no.
- **Time-series alerting** ("warn me when KPI X exceeds Y"). Different
  feature; defer.

## Consequences

- **One new table** + a new descriptor field + a new completion hook in
  the runner supervisor + a new top-level page + four API endpoints.
  Self-contained; no governance new beyond the existing tool-descriptor
  approval gate (descriptors are reviewed when tools are promoted; KPI
  declarations ride along).
- **Background re-extraction job** — adds work to the runner daemon, but
  cheap (one outputs-document read + N inserts/updates per job; rate-
  limited).
- **Warehouse query performance** — indexed on
  `(tool_alias, kpi_name)`, so the typical "all KPI X for tool Y" query
  is one indexed range scan. Million-job installs may need a partitioned
  view; not v1.0's problem.
- **No new permissions** — uses the existing `jobs.view` ACL.

## Build phases

Per house style:

- **Phase 1** — Descriptor extension (`kpi`, `kpi_label`, `kpi_unit`,
  `kpi_category`) + descriptor-wizard editor surface; ToolService
  install-time validation that only scalar-numeric outputs carry
  `kpi = true`.
- **Phase 2** — `matofx/kpi/` package with `KpiExtractor` (reads
  `outputs.json`, writes `job_kpi_value`); migration adding the table +
  indexes; hook wired into `runner.supervisor`'s post-job finalisation.
- **Phase 3** — Background re-extraction job triggered on descriptor
  update; audit events.
- **Phase 4** — API: `/api/tools/{alias}/kpis`, `/results`,
  `/results/timeseries`, `/results/export.csv`; pagination + ACL.
- **Phase 5** — UI: `/results/{tool_alias}` page; AG-Grid table; filter
  row; Plotly card; CSV/PDF download; lineage click-through.
- **Phase 6** — Integration: ADR-0004 summary block pulls KPIs; ADR-0011
  V&V filter; "View in Compare" handoff; Optimisation Study detail
  page links to its tool's results.
- **Phase 7** — Docs + ROADMAP tick + the operator note on the
  re-extraction background job.

## Revision history

- **2026-05-26** — initial ruling at full v1.0 scope. Decision driven by
  the observation that the SPDM-defining "results discovery" capability
  is materially missing in v1.0 unless this layer ships; Compare alone is
  not it. The data write path is small; the page is moderate; the
  combined cost is justified by the customer signal (every regulated
  customer expects a "show me KPI X across all runs" interface).
