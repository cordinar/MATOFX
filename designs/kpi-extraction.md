# KPI extraction — per-tool results warehouse — build spec

Implements ADR-0012 (`decisions/0012-kpi-extraction.md`).

The decision doc is the architectural ruling; this file is the build-level spec.
Read the ADR first. The slice plan in §11 is what gets ticked in `ROADMAP.md`
as work lands.

---

## Implementation checkpoint (placeholder)

_To be filled as slices land. Mirrors §11's slice list. Each row records the
landing date + the PR / commit summary + the surface impact._

| Slice | Landed | Notes |
|---|---|---|
| 0 — feature flag + bootstrap | | |
| 1 — descriptor extension + validation | | |
| 2 — migration 0057 + ORM | | |
| 3 — `KpiExtractor` + runner hook | | |
| 4 — re-extraction background job | | |
| 5 — API surface | | |
| 6 — `/results/{tool_alias}` table | | |
| 7 — Plotly region + CSV/PDF export | | |
| 8 — ADR-0004 report headline block | | |
| 9 — ADR-0011 V&V status filter | | |
| 10 — "View in Compare" handoff | | |
| 11 — Descriptor-wizard KPI editor | | |
| 12 — Docs + ROADMAP tick | | |

---

## 1. Scope at a glance

| Capability | v1 | Deferred |
|---|---|---|
| `job_kpi_value` table — one row per (job, kpi) pair | ✅ | |
| Descriptor `kpi = true` on scalar-numeric outputs (+ optional `kpi_label` / `kpi_unit` / `kpi_category`) | ✅ | |
| Install-time validation: only `type == "number"` outputs can carry `kpi = true` | ✅ | |
| Extraction hook inside `runner.supervisor._post_complete` (before cleanup) | ✅ | |
| Sentinel `value_numeric = NULL` for missing / non-numeric / NaN | ✅ | |
| Background re-extraction on descriptor change (low priority, audited) | ✅ | |
| `/results/{tool_alias}` page — AG-Grid + filter row + Plotly card + CSV / PDF export | ✅ | |
| Time-series trend chart driven by `extracted_at` | ✅ | |
| V&V status filter (pulls from ADR-0011) | ✅ | |
| "View in Compare" handoff (≤ 6 rows → `/compare`) | ✅ | |
| ADR-0004 report headline KPI cards | ✅ | |
| `Feature.KPI_EXTRACTION` gate (404-when-off; nav + surfaces hidden) | ✅ | |
| Audit: `kpi.extracted` / `kpi.reextracted` / `kpi.viewed` (surface discriminator) | ✅ | |
| **Cross-tool / global KPI dashboard** — one page rolling up KPIs across many tools | | ✅ v1.x |
| **Project-rollup scorecards** — per-project top-N KPIs at a glance | | ✅ v1.x |
| **Saved warehouse queries** — share / reapply filter sets | | ✅ v1.x |
| **Scheduled exports** — "email me this CSV every Monday" | | ✅ v1.x |
| **Custom / derived KPI formulae** — `kpi_x = a / b` declared outside the descriptor | | ✅ v1.x |
| **Group-by aggregation** — pivot on project metadata / requestor team | | ✅ v1.x |
| **Time-series alerting** — "warn me when KPI X exceeds Y" | | Out (different feature) |
| **Non-scalar / vector KPIs** (e.g. peak from a curve) | | Out (compute in tool, expose scalar) |

Governance scope is **descriptor-bundled, no new curator role**. KPI declarations
ride along the existing tool-descriptor approval gate — flagging an output as a
KPI is a descriptor edit, which goes through the existing tool-promotion review.
No new permission. No new capability.

---

## 2. Data model + migration

New package `matofx/kpi/`. One migration chains off head `0056`:

| Rev | Purpose |
|---|---|
| `0057_job_kpi_values.py` | Create `job_kpi_value` table + composite UNIQUE + the two indexes. Idempotent; one-way-lossy downgrade `DROP TABLE`. |

Migration follows house style: `_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")`; CHECK bodies as frozen literals (never imported from the ORM); `batch_alter_table(recreate="always")` for any SQLite CHECK change; idempotent seeds.

### 2.1 `job_kpi_value` — extracted KPI values

| col | type | notes |
|---|---|---|
| `id` | PK (`_BIGINT`) | |
| `job_id` | FK → `job.id` ON DELETE CASCADE, NOT NULL | One row per (job, kpi) pair. CASCADE because a deleted job leaves no semantic claim |
| `tool_alias` | str(64) NOT NULL | Denormalised from `job.tool_alias` for filter-path speed (avoids a join on the warehouse query) |
| `tool_version_id` | FK → `tool_version.id` RESTRICT, NOT NULL | Denormalised from `job.tool_version_id`. RESTRICT because retiring a tool_version still owning KPI rows should fail loudly (orphan rows are not the right shape) |
| `kpi_name` | str(64) NOT NULL | Mirrors `OutputField.name`. Validated against the live descriptor at write time |
| `value_numeric` | Numeric(precision=38, scale=12) nullable | `NULL` is the sentinel for "extraction failed" (missing key, not numeric, NaN, ±Inf). Wide precision so heat-transfer K and parts-per-million both fit without quantisation loss |
| `unit` | str(32) nullable | Captured from the descriptor (`kpi_unit` if set, else `unit`). Stored per-row so a later unit-override in a new descriptor version doesn't retroactively mislabel old data |
| `extracted_at` | DateTime(tz) NOT NULL | When the post-completion hook wrote this row. Drives the time-series chart |

Indexes:

- `uq_job_kpi_value_job_kpi` — composite UNIQUE on `(job_id, kpi_name)`. Re-extraction updates the existing row; never duplicates.
- `ix_job_kpi_value_alias_kpi` — composite `(tool_alias, kpi_name)`. The warehouse's "all values of KPI X for tool Y" query hits this directly.
- `ix_job_kpi_value_extracted_at` — single-column. Drives the time-series chart's range scan.

Sizing note: a million-job install with five KPIs per tool and four tools = 20 million rows. At ~100 bytes per row (rough order of magnitude) that's ~2 GB — comfortably within SQLite + Postgres single-table reach. Partition-by-tool-alias is a v1.x problem if any customer crosses 100 million rows.

### 2.2 `OutputField` descriptor extension

Four new optional fields on `OutputField` (`matofx/tools/descriptor.py`):

| field | type | default | notes |
|---|---|---|---|
| `kpi` | `bool` | `False` | Marks this output as a headline KPI. Constraint: only `type == "number"` outputs can set `kpi = true`. Enforced by the `_kpi_consistency` validator (§3.2) at descriptor-load time |
| `kpi_label` | `str \| None` | `None` | Optional short label for warehouse columns. Falls back to `OutputField.label` if absent |
| `kpi_unit` | `str \| None` | `None` | Optional unit override for warehouse display. Falls back to `OutputField.unit` if absent |
| `kpi_category` | `str \| None` | `None` | Optional grouping label (`"thermal"`, `"structural"`, `"manufacturing"`). Drives the future cross-tool dashboard (v1.x). v1.0 uses it only for the descriptor-wizard grouping affordance |

Frozen / Pydantic-`v2`-strict; defaults preserve back-compat for all existing descriptors (zero change required to ship the migration). A descriptor that has no `kpi = true` output is functionally invisible to the KPI layer — no rows ever extracted, the `/results/{tool_alias}` page short-circuits to a "no KPIs declared" placeholder.

---

## 3. Descriptor extension — validation + bootstrap

### 3.1 Wizard surface

Descriptor-wizard (`matofx/ui/pages/_descriptor_wizard.py`) gains a **KPI** affordance on the *Outputs* tab. For each numeric output the editor shows:

- A "Mark as KPI" checkbox (binds `kpi`).
- When ticked: a popover with `kpi_label`, `kpi_unit`, `kpi_category` inputs. Live-validated; `kpi_category` is a free-text combo-box pre-populated with categories already used in the install (`SELECT DISTINCT kpi_category FROM job_kpi_value`).
- The checkbox is **disabled with tooltip** for non-numeric outputs ("KPIs must be scalar-numeric"). The tooltip wording mirrors the validator error message in §3.2.

The wizard surfaces a small **"KPIs declared"** badge in the Outputs tab header showing the running count (`3 KPIs declared`). Helps an author spot accidental no-KPI descriptors.

### 3.2 Install-time validation

`OutputField._kpi_consistency` (Pydantic `model_validator(mode="after")`):

```python
if self.kpi and self.type != "number":
    raise ValueError(
        f"OutputField {self.name!r}: kpi=true requires type='number', "
        f"got type={self.type!r}"
    )
```

`ToolDescriptor.model_validate` already calls per-field validators; this rides for free. A descriptor TOML with `kpi = true` on a `type = "string"` output fails at descriptor load, surfaced via the existing tool-install error UI.

A second, tool-level validator forbids two different KPIs sharing the same `kpi_label` within one descriptor (warehouse columns must be unambiguous) — same pattern as the existing duplicate-name check on `OutputField.name`.

### 3.3 Feature flag

`matofx/features.py` gains `Feature.KPI_EXTRACTION = "kpi_extraction"`. The flag gates:

- API router (`api/routers/kpi.py`) — 404-when-off via `Depends(_require_kpi_feature)`, mirroring the V&V pattern.
- UI nav entry — "Results warehouse" link hidden when off.
- ADR-0004 report block — report builder skips the KPI cards section when off.
- Descriptor wizard — KPI checkbox / popover hidden when off (descriptors that already carry `kpi = true` still validate; the wizard just won't surface the editor).

`kpi_extraction_enabled(settings)` helper in `matofx/ui/_features.py` for the UI surfaces. Default for v1.0 demo installs: ON.

---

## 4. ORM — `matofx/kpi/orm.py`

```python
class JobKpiValue(Base):
    __tablename__ = "job_kpi_value"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("job.id", ondelete="CASCADE"), nullable=False
    )
    tool_alias: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_version_id: Mapped[int] = mapped_column(
        ForeignKey("tool_version.id"), nullable=False
    )
    kpi_name: Mapped[str] = mapped_column(String(64), nullable=False)
    value_numeric: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    unit: Mapped[str | None] = mapped_column(String(32))
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("job_id", "kpi_name", name="uq_job_kpi_value_job_kpi"),
        Index("ix_job_kpi_value_alias_kpi", "tool_alias", "kpi_name"),
        Index("ix_job_kpi_value_extracted_at", "extracted_at"),
    )
```

No relationships back to `Job` or `ToolVersion` — the table is queried directly and only the FK constraints are needed; SQLAlchemy `relationship` adds N+1 traps the warehouse path doesn't need.

`Decimal` mapping: the runner's `KpiExtractor` casts the JSON float to `Decimal(str(float_value))` to avoid binary-rounding artefacts in the table. The warehouse query path returns `float` for plot / CSV consumers; the `Decimal` storage is the durable truth.

---

## 5. Service — `matofx/kpi/service.py`

`KpiService` is a thin coordinator. The three public surfaces:

### 5.1 `KpiService.extract_for_job`

```python
async def extract_for_job(
    self,
    *,
    session: AsyncSession,
    job: Job,
    descriptor: ToolDescriptor,
    outputs_doc: OutputsDocument,
) -> KpiExtractResult: ...
```

- Filters `descriptor.outputs` to `[f for f in outputs if f.kpi]`.
- For each KPI field, reads `outputs_doc.values.get(field.name)`:
  - Missing key → `value_numeric = NULL`, log `kpi.extract.missing` at WARNING.
  - Not numeric → `NULL`, log `kpi.extract.non_numeric`.
  - `math.isnan(v)` or `math.isinf(v)` → `NULL`, log `kpi.extract.nan_or_inf`.
  - Else → `Decimal(str(v))`.
- UPSERTs each row keyed on `(job_id, kpi_name)` via the existing dialect-aware upsert helper (already in `matofx/db/upsert.py` per the V&V slice 4 pattern).
- Returns `KpiExtractResult(extracted=N, missing=M, total=K)` for the audit event.
- **Never raises** — extraction failures degrade gracefully to sentinels. The runner's job-completion path must never fail because of a KPI extraction problem.

### 5.2 `KpiService.list_warehouse_rows`

```python
async def list_warehouse_rows(
    self,
    *,
    session: AsyncSession,
    requester: User,
    tool_alias: str,
    filters: WarehouseFilters,
    page: PageSpec,
) -> WarehousePage: ...
```

- Composes the JobService ACL filter (`hierarchy.service._resolve_access(..., requester.id) >= ProjectAccessLevel.VIEW`).
- Joins `job` ⋈ `job_kpi_value` for the requested tool + version filter.
- Pivots the KPI rows into per-job dicts (`{kpi_name: value_numeric}`) — driven in SQL via `FILTER (WHERE kpi_name = ...) AS ...` on Postgres and a per-KPI subquery on SQLite. Both kept under one query round-trip.
- Applies `WarehouseFilters` (project / scenario / requestor / completed-at window / per-KPI value ranges / V&V status).
- Returns `WarehousePage(rows=[WarehouseRow], page=..., hidden_count=N)`. `hidden_count` is the "12 of 47 hidden by access controls" figure (computed via a second COUNT query restricted only by the non-ACL filters minus the ACL filter — same pattern as Job Search).

### 5.3 `KpiService.timeseries`

```python
async def timeseries(
    self,
    *,
    session: AsyncSession,
    requester: User,
    tool_alias: str,
    kpi_name: str,
    filters: WarehouseFilters,
) -> list[TimeseriesPoint]: ...
```

One indexed range scan on `(tool_alias, kpi_name)` + ACL filter. Returns `[(extracted_at, value_numeric, job_id)]` ordered by `extracted_at`. Plot consumers can hover a point and click through to the job page.

### 5.4 `KpiService.reextract_tool_version`

```python
async def reextract_tool_version(
    self,
    *,
    session: AsyncSession,
    tool_version_id: int,
    triggered_by_user_id: int | None,
) -> KpiReextractResult: ...
```

- Lists all `job` rows with `tool_version_id == X` and `state == "complete"`.
- For each: loads `outputs.json` from durable storage (ADR-0003 retention floor), re-runs `extract_for_job`, accumulates counts.
- Rate-limited: yields control every 50 jobs so the daemon doesn't monopolise the event loop.
- Audits `kpi.reextracted` with `(tool_version_id, job_count, extracted, missing)` and the trigger user (or `None` for descriptor-change-triggered runs).

**Trigger.** Called from `ToolService.promote_version` (and equivalents that activate a new descriptor) when the new descriptor's KPI set differs from the previous version's. The differ is `set((f.name, f.kpi_label, f.kpi_unit) for f in outputs if f.kpi)` — any change re-extracts.

**Operator visibility.** A "warehouse refreshing" banner on the tool's `/results/{tool_alias}` page reads from a transient `tool_reextract_state` runtime key (in-memory in the runner daemon, surfaced via the API). No new table — the state is ephemeral.

---

## 6. Write path — runner hook

`runner/supervisor.py::Supervisor._post_complete` (existing method, ~line 763) gains one call, slotted **immediately after `record_study_result`** and **before** `_apply_cleanup_for_tool`:

```python
async def _post_complete(self, staged, tool, *, session, job):
    outputs_doc = await self._read_outputs_document(staged)
    if outputs_doc is not None:
        await record_study_result(session=session, job=job, outputs=outputs_doc)
        await self._kpi_service.extract_for_job(  # NEW
            session=session, job=job,
            descriptor=tool.descriptor_snapshot,
            outputs_doc=outputs_doc,
        )
    await self._apply_cleanup_for_tool(staged, tool, job=job)
```

The hook re-uses the already-parsed `OutputsDocument` (no extra read). Cleanup runs *after* KPI write so the retention sweep can drop `outputs.json` without losing the headline scalars.

**Failure modes.** `extract_for_job` never raises. If it did (DB error, etc.), the job-completion transaction would roll back, leaving the job in a stuck state — exactly the failure mode V&V's policy avoided. KPI's hook is best-effort by contract.

**Surrogate-tool jobs.** ADR-0008 surrogate-tool jobs follow the same path: their outputs document carries scalar predictions (`<response>`) and uncertainty bands (`<response>_std`). Descriptor authors mark predictions as KPIs; bands default to non-KPI but can be promoted. No special-casing needed in the KPI service.

**Optimisation study children.** ADR-0009 children are regular jobs from the warehouse's perspective. The Optimisation Study detail page (ADR-0009) gets a "View in results warehouse" link that deep-links into `/results/{tool_alias}` pre-filtered to the study's children.

---

## 7. API surface

New router `matofx/api/routers/kpi.py` mounted under `/api/tools/{alias}`. Four endpoints, all behind `Depends(_require_kpi_feature)`:

| Method | Path | Body / Query | Returns |
|---|---|---|---|
| `GET` | `/api/tools/{alias}/kpis` | `?version_id=...` (optional; default = the version in force + names that still have captured values — see §7.1) | `ToolKpiCatalogResponse` — `[{kpi_name, kpi_label, kpi_unit, kpi_category}]` |
| `GET` | `/api/tools/{alias}/results` | `?version_id=...&project_id=...&scenario_id=...&requestor_user_id=...&completed_from=...&completed_to=...&vv_status=...&kpi_range[name]=min,max&page=1&page_size=50` | `WarehousePageResponse` — `{rows: [{job_id, project_id, scenario_id, requestor, completed_at, vv_status, kpi_values: {name: value}}], page, page_size, total, hidden_by_acl}` |
| `GET` | `/api/tools/{alias}/results/timeseries` | `?kpi_name=...` + same filters as `/results` | `TimeseriesResponse` — `[{extracted_at, value_numeric, job_id}]` |
| `GET` | `/api/tools/{alias}/results/export.csv` | same filters as `/results`; no pagination; **optional `?job_ids=...`** comma-separated allow-list (overrides other filters when present) | `text/csv` streaming response — ACL applied, max 100k rows (hard cap; UI surfaces "result set truncated" toast). With `job_ids` present: the set is the **intersection** of (ticked IDs) ∩ (ACL-visible IDs); silently drops IDs the requester can't see (no information leak) |

`page_size` capped at 200 to match Job Search. `total` is the unfiltered-by-pagination count; `hidden_by_acl` is the ACL-excluded count from §5.2.

ACL is applied **inside the service** (§5.2), not at the router. Routers stay thin.

**Why no POST.** v1.0 has no surface to create / edit / delete KPI rows directly — the descriptor is the only authoring path. A future "user-defined derived KPI" feature would land as a separate router; orthogonal to this surface.

### 7.1 Catalog semantics (corrected 2026-09-21)

The catalog was first built as a **union over every `ToolVersion`**, deduped on `(kpi_name, kpi_label, kpi_unit)`. Both halves were wrong in ways that reached the UI:

- The union contradicted §109 of this design ("a descriptor that has no `kpi = true` output is functionally invisible to the KPI layer"). A KPI un-ticked long ago kept its column forever, even when *no job had ever run* a version that declared it — `bracket_stress` advertised "2 KPI(s)" and drew two all-`—` columns over 650 rows.
- The `(name, label, unit)` dedup key doubled columns on un-tick → re-tick. Un-ticking sweeps `kpi_label` (`clear_kpi_overrides`), so the re-tick's effective label falls back to the output's own `label` and the key diverges. Because every downstream surface keys on the *name* (`field: kpi__{kpi_name}`, the row pivot, the CSV header, the range filter), two entries sharing a name can only render as duplicates of one column — never as two distinct ones. The comment claiming divergent labels "surface as separate entries so the warehouse can't accidentally collide them" described an outcome the rendering layer cannot produce.

**The rule now:**

- `?version_id=` set → exactly that version's declared KPIs. This is the per-run answer: the job page, the auto-summaries collector and the ADR-0004 report all pin it, so a run is judged against the descriptor it actually ran.
- No `version_id` → the **newest version's** declared KPIs, plus any `kpi_name` that still has rows in `job_kpi_value`, with each recovered name's label/unit taken from the newest version that declared it. Dedup is on `kpi_name` alone, newest-version-wins on the label.

That keeps un-ticking non-destructive (captured values keep their column for as long as the jobs exist) while letting a KPI that was never captured disappear cleanly.

---

## 8. UI surfaces

Six surfaces touched. All `kpi_extraction_enabled()`-gated.

### 8.1 `/results/{tool_alias}` — the warehouse page

New page `matofx/ui/pages/results.py`. Layout top-to-bottom:

1. **Header bar.** Tool name + version dropdown (`All released versions` default, plus per-version entries from `GET /api/tools/{alias}/kpis`). V&V status filter chip (`Any` / `Verified` / `Verified + Validated`) — driven by ADR-0011's `JobVVRecord` join.

2. **Filter card.** Per UI Conventions (filter row above the table). Inputs:
   - Project (multi-select; ACL-filtered list).
   - Scenario (multi-select; cascades from project picks).
   - Requestor (multi-select; alpha-sorted).
   - Completed window (date-range picker; `last 7 / 30 / 90 days / custom`).
   - Per-KPI value ranges — one row per declared KPI, with `min` and `max` numeric inputs and the KPI's unit suffix.
   - "Reset filters" link.

3. **The table** — AG-Grid (`ui.aggrid` + the existing themed config). Columns:
   - Sticky identity columns: Job ID (link), Project, Scenario, Requestor, Completed-at, V&V badge, Lineage icon (link to `/lineage/job/{id}`).
   - KPI columns: one per declared KPI of the selected version(s). Header shows `kpi_label` + `(kpi_unit)` + a 3-dot menu for show/hide. Numeric formatting from the descriptor's `precision`.
   - Per-column sort + filter; per-column show/hide; column reordering persists per-user via `user_ui_state` (the existing key/value store the dashboard already uses).
   - Pagination: 50 rows/page; "showing 1–50 of 412" footer; "12 hidden by access controls" annotation (from `hidden_by_acl`).
   - **Selection: unbounded.** Tick boxes on every row, plus AG-Grid's standard select-all-on-page / select-all-filtered toggles. The selection is the working set the export and Compare actions operate on. A persistent "N rows selected" pill sits above the table with a "Clear selection" link.
   - **"View in Compare"** is enabled when 2–6 rows are ticked. Past 6 it is **disabled with a tooltip** — `"Compare is limited to 6 rows — narrow your selection"`. Selection survives pagination + filter changes (rows that leave the filter stay ticked but are visually hidden; the pill still counts them).

4. **Plot region.** Inline Plotly card under the table, collapsible.
   - Axis pickers: X = any KPI column; Y = any KPI column; *Series* = optional grouping column (Project, V&V status, Tool version); *Marker size* = optional third KPI.
   - **Time-series toggle.** Flips X to `extracted_at`; Y stays a KPI picker; Series stays as configured. Drives `/results/timeseries`.
   - Defaults: first two KPIs as X/Y, coloured by project. If only one KPI exists, defaults to time-series.
   - Themed via `themed_plotly` per UI Conventions.

5. **Export bar.** Three buttons:
   - **"Download CSV (filtered)"** → `GET /api/tools/{alias}/results/export.csv` with current filters; full filtered set. Always enabled.
   - **"Download selected as CSV (N)"** → same endpoint with an extra `?job_ids=...` query param carrying the ticked rows. Button text shows the live N; disabled when N == 0. Respects the same 100k hard cap as the filtered export.
   - **"Download PDF"** → server-rendered PDF of table snapshot + plot snapshot, self-contained per ADR-0004 pattern (re-uses `matofx/reports/builder.py`'s rendering infra; new template `results.html.j2`). PDF honours selection identically to CSV: ticked rows when N > 0, else the filtered set.

6. **Empty / no-KPI states.**
   - `descriptor declares no KPIs` → placeholder card linking to "Edit descriptor → Outputs → Mark as KPI" for admins; plain text for non-admins.
   - `KPIs declared but no completed jobs yet` → empty-state card with submit-job CTA.

### 8.2 Nav entry — Dashboard chrome

`matofx/ui/_chrome.py`: new "Results warehouse" entry under the Dashboard nav (above "V&V records"). Per-user landing-dashboard config doesn't change.

### 8.3 Per-job card — "Headline KPIs"

`matofx/ui/pages/job.py::_render_complete` adds a card *after* the V&V card and *before* the Outputs panel:

- Reads `GET /api/jobs/{id}/kpis` (or — if cheaper — embeds the KPI values in the existing `JobResponse`; choice driven by the slice 6 perf measurement).
- Shows one card per declared KPI: large numeric, unit, sparkline of the last 30 days of the same KPI on the same tool (clickable → time-series chart on `/results/{tool_alias}`).
- Skipped on inflight / failed jobs (no KPI data yet / extraction skipped).

### 8.4 Per-job HTML report — ADR-0004 integration

`ReportContext` gains `kpi_values: list[ReportKpiCard]` (frozen Pydantic). `report.html.j2` gains an executive-summary block that renders the cards inline (no external assets — same bounded-size invariant as the rest of the report). Block omitted when `kpi_values == []` or `Feature.KPI_EXTRACTION` is off.

### 8.5 Compare column-header badge

`matofx/ui/pages/compare/render.py` adds a small "KPI" pill to each tool-column header listing the KPI count (`5 KPIs`). Clicking the pill opens the warehouse for that tool with the ticked Compare jobs pre-filtered.

### 8.6 Descriptor-wizard outputs editor

Per §3.1 — the "Mark as KPI" checkbox + popover.

---

## 9. Audit

Three new audit event kinds, all hash-chained per `matofx.audit`:

| Event | Surface / when | Payload |
|---|---|---|
| `kpi.extracted` | `_post_complete` runs the extractor | `{job_id, tool_alias, tool_version_id, extracted: N, missing: M, total: K}` |
| `kpi.reextracted` | `KpiService.reextract_tool_version` completes | `{tool_version_id, job_count, extracted, missing, triggered_by_user_id, trigger: "descriptor_promote" \| "admin_manual"}` |
| `kpi.viewed` | API hits / report renders | `{tool_alias, surface: "table" \| "timeseries" \| "csv" \| "report", filter_summary}` |

`filter_summary` is a short string ("`project=2,scenario=5,kpi_range[max_temperature]=300..400`") — enough for audit replay without bloating the chain.

The per-job extraction event is the noisiest: one row per completed job. Acceptable — V&V's `vv.policy_evaluated` is the same density and ships fine.

---

## 10. Cross-ADR integration touchpoints

| ADR | Touchpoint |
|---|---|
| **ADR-0002** (project ACL) | Warehouse query composes `_resolve_access(..., requester.id) >= VIEW`. `hidden_by_acl` count surfaces the existence-hidden rows |
| **ADR-0003** (job-I/O retention) | KPI extraction runs *before* the cleanup sweep, so `outputs.json` ages out per policy but `job_kpi_value` rows persist forever. KPI table is the permanent headline index; outputs.json is the bulk archive |
| **ADR-0004** (per-job HTML report) | `ReportContext.kpi_values` + new template block (§8.4). Report builder reads from `job_kpi_value` not the outputs doc — keeps the report renderable even after retention has dropped the bulk file |
| **ADR-0006** (file library) | No interaction |
| **ADR-0007** (materials) | No interaction. A future "material property as KPI proxy" feature would not change this layer |
| **ADR-0008** (surrogate family) | Surrogate-tool jobs feed the warehouse identically. `<response>_std` uncertainty bands are not KPIs by default; operators promote if useful. **The "surrogate-as-tool export" path needs to carry the parent's KPI flags forward** — its `tools.versioning` hand-off must copy descriptor `kpi*` fields onto the synthetic surrogate tool's descriptor. Cross-checked here, owned by ADR-0008 |
| **ADR-0009** (optimisation framework) | Optimisation Study detail page gets a "View in results warehouse" deep-link. The study's children populate the warehouse like any other jobs; no special pathway |
| **ADR-0010** (data lineage) | Every warehouse row carries a lineage-icon link to `/lineage/job/{id}`. No new node kind, no new edge — the existing graph already covers the job side |
| **ADR-0011** (V&V tracking) | Warehouse filter chip pulls `JobVVRecord.state` per the existing `vv.summarise_for_jobs` batched fetch. **V&V's slice 13 marker resolves here** — the KPI page is V&V's integration point. The V&V status badge in the table identity columns is the same component as Compare's column-header badge |
| **Compare** (§16.3) | "View in Compare" handoff: warehouse builds the same base64-encoded selection state Compare's deep-link uses, redirects to `/compare?selection=...`. No new state contract; reuse `compare.selection.encode` |
| **Job Search** (§16.5) | KPI is *cross-job KPI discovery*; Job Search is *input-value discovery*. They are complementary. No shared code in v1.0; the Job Search "Refine by KPI" panel is a v1.x follow-on |
| **General Settings** | One new key: `kpi_table_default_page_size` (default 50, range 25..200). No new section — lands under the existing *Branding & UX* section. Per `feedback_general_settings_two_places`: catalog + SECTIONS + test count all updated |
| **Auto-generated results summaries** (Phase 2 expansion 2026-05-26) | The templated narrative reads `job_kpi_value` to populate its "headline numbers" sentence. **Contract:** narrative gets `dict[kpi_name, (value, unit, kpi_label)]` from the same view ADR-0004's `ReportKpiCard` uses. KPI surface owns the view; auto-summary owns the prose |

---

## 11. Phased build sequence

Slice plan for `ROADMAP.md`. Each slice is shippable on its own; later slices add surfaces without breaking earlier ones.

| Slice | Scope | Migration | Tests | Exit criterion |
|---|---|---|---|---|
| **0** | `Feature.KPI_EXTRACTION` flag + `kpi_extraction_enabled` helper + `matofx/kpi/__init__.py` stub | — | flag-on / flag-off helpers (8) | Toggling the flag flips the (empty) feature without import errors |
| **1** | Descriptor extension (`kpi`, `kpi_label`, `kpi_unit`, `kpi_category`) + `_kpi_consistency` validator + duplicate-`kpi_label` check | — | OutputField validation (12); ToolDescriptor round-trip (4) | A descriptor with `kpi = true` on a numeric output loads; same flag on a string output fails with a clear message |
| **2** | Migration `0057_job_kpi_values.py` + `JobKpiValue` ORM + `matofx/kpi/orm.py` + `matofx/kpi/errors.py` | `0057` | ORM CRUD (6); migration up/down (2 — sqlite + postgres) | Migration applies on a fresh + populated DB; ORM row CRUD round-trips |
| **3** | `KpiService.extract_for_job` + `_post_complete` hook wiring + `Decimal` casting + sentinel handling | — | extractor unit tests across {missing, non-numeric, NaN, ±Inf, normal} (10); upsert idempotence (3); never-raise contract (4) | A `local`-backend job with a KPI descriptor completes → `job_kpi_value` row landed; cleanup runs *after* extraction |
| **4** | `KpiService.reextract_tool_version` + `ToolService.promote_version` trigger + runtime "warehouse refreshing" state + `kpi.reextracted` audit | — | re-extraction integration (6); descriptor-diff trigger (4); rate-limit yield (2) | Promoting a descriptor that adds a new KPI back-fills the table for all completed jobs of affected versions |
| **5** | API: `GET /kpis`, `GET /results`, `GET /results/timeseries`, `GET /results/export.csv` + ACL composition + pagination + `_require_kpi_feature` 404 gate | — | router happy path (12); ACL filtering (8); pagination edges (4); 404-when-off (4) | Curl against any endpoint with the flag off → 404; with the flag on + a populated install → paginated rows |
| **6** | `/results/{tool_alias}` page — header, filter card, AG-Grid table, pagination, **unbounded selection + "N selected" pill + selection-survives-filter behaviour**, "View in Compare" handoff (auto-disabled past 6 with tooltip), empty / no-KPI states | — | page-render snapshot (6); filter wiring (8); selection state encode (3); **selection-cap tooltip behaviour (3); selection survives pagination + filter (4)** | Operator can open the warehouse, filter, tick any number of rows, see the count pill, jump to Compare when 2–6 are ticked, see the tooltip past 6 |
| **7** | Plotly card (X/Y/series/marker-size pickers + time-series toggle) + **"Download CSV (filtered)" + "Download selected as CSV (N)" + "Download PDF"** via `results.html.j2`; `job_ids` filter on the export endpoint | — | plot axis-picker wiring (5); filtered-CSV stream (3); **selected-CSV stream + `job_ids` intersection with ACL (4)**; PDF render — both modes (3) | Operator can plot any KPI vs any KPI, switch to time-series, export either the full filtered set or just the ticked rows as CSV + PDF |
| **8** | ADR-0004 report integration — `ReportContext.kpi_values` + `report.html.j2` headline block + `kpi.viewed surface=report` audit | — | report-block render (4); KPI-empty fallback (2); flag-off skip (2) | Per-job HTML report shows headline KPI cards when present |
| **9** | ADR-0011 V&V filter wiring — warehouse `vv_status` filter + per-row V&V badge component re-use | — | V&V join (4); filter combinatorics (4) | Setting V&V filter to `Verified + Validated` only returns jobs whose tool_version has both V&V record kinds approved |
| **10** | Per-job page "Headline KPIs" card (`_render_complete`) + sparkline component | — | per-job card render (4); sparkline data path (3) | Job page shows large-numeric KPI cards with 30-day sparkline; click-through opens warehouse pre-filtered |
| **11** | Descriptor-wizard KPI editor — checkbox + popover + `kpi_category` autocomplete + "KPIs declared" badge | — | wizard render (4); checkbox disabled-state (2); category autocomplete (3) | Tool author can mark / unmark a numeric output as KPI; non-numeric outputs show disabled checkbox + tooltip |
| **12** | Docs — `ARCHITECTURE.md §16.25` (full KPI chapter); `ROADMAP.md` row → Landed; operator note on the re-extraction background job | — | none (docs) | `ROADMAP.md` shows KPI in Landed; `ARCHITECTURE.md` §16.25 documents the surface; **V&V's "slice 13 deferred" marker is removed from `ROADMAP.md` and `project_vv_build_checkpoint`** |

Estimated total: ~130 tests (mirroring the V&V build's 104 + the lineage build's ~60 — KPI is between the two in size).

---

## 12. Test plan

### 12.1 Unit

- `kpi/service.py` — `extract_for_job` exhaustively across the {missing, non-numeric, NaN, +Inf, -Inf, very-small, very-large, integer, float, Decimal-string} value space.
- `kpi/orm.py` — UNIQUE constraint violation behaviour (upsert path); CASCADE on job delete; RESTRICT on tool_version delete.
- `OutputField` — `_kpi_consistency` validator across {`kpi=True,type=number` ✓}, {`kpi=True,type∈{string,file,table}` ✗}, {`kpi=False,type=*` ✓}.
- `ToolDescriptor` — duplicate-`kpi_label` rejection; round-trip of all four new fields through TOML.

### 12.2 Integration — runner hook

- Submit a `local`-backend job with a KPI descriptor → completion writes the row → cleanup happens after.
- Submit a job with a missing KPI output key → sentinel NULL row + WARNING log.
- Submit a job whose extraction would raise (mock raise inside `extract_for_job`) → job still completes (never-raise contract); audit logs the failure but doesn't roll the transaction back.

### 12.3 Integration — re-extraction

- Promote a descriptor adding a new KPI → background job extracts for all completed jobs on the affected versions.
- Promote a descriptor that does *not* change the KPI set → no re-extraction triggered.
- Concurrent re-extraction during a normal job's completion → both write idempotently; UNIQUE constraint holds.

### 12.4 API

- 404-when-off for every endpoint with `Feature.KPI_EXTRACTION` off.
- ACL filtering — a non-Owner user querying the warehouse sees only their project's jobs; `hidden_by_acl` carries the count.
- Pagination edges — page 1, last page, beyond last page (empty rows + `total` honest).
- CSV streaming — 100k-row cap; truncation flag; chunked transfer; `job_ids` allow-list intersected with ACL silently drops invisible IDs.
- Filter combinatorics — every filter alone and the full filter set; verifies no SQL-injection vector in the per-KPI range syntax.

### 12.5 UI

- Page snapshot in {empty / no-KPI / populated / V&V-filtered} states.
- AG-Grid column-state persistence (re-render preserves user reorder + show/hide).
- Plot axis-picker wiring across {scatter, time-series, series-grouped}.
- "View in Compare" handoff round-trip — encode → redirect → Compare reads selection.
- PDF export — content matches the table + plot DOM state at the time of click.

### 12.6 Report

- `report.html.j2` headline block: renders KPI cards with values; renders empty when no KPI rows; renders with the existing V&V card stacked above.

Postgres-marked tests (per V&V slice 12 precedent): the JSON / `Decimal` precision behaviour and any `FILTER (WHERE ...)` query forms run under both engines; the sqlite path uses the per-KPI subquery fallback.

---

## 13. Open / deferred (recorded, not silent)

- **Cross-tool / global dashboard.** Data shape supports it (`kpi_category` exists). UI is a v1.x exercise.
- **Project-rollup scorecards.** "Top N KPIs per project" page. Different layout. v1.x.
- **Saved warehouse queries.** Share / re-apply filter sets across users. v1.x.
- **Scheduled exports.** "Email me CSV every Monday." Notifications layer is single-event in v1.0. v1.x.
- **Custom / derived KPI formulae.** `kpi_x = output_a / output_b` declared outside the descriptor. v1.x; the descriptor is the only authoring path in v1.0.
- **Group-by aggregation.** Filter-yes, group-by-no. v1.x.
- **Time-series alerting.** "Warn me when KPI X exceeds Y." Separate feature; defer.
- **Vector / curve KPIs** (peak from a curve, area-under-curve). Out of scope — compute the scalar in the tool, expose as a `type = number` output.
- **Cross-install KPI export/import** (via the Migrate bundle). KPI rows are install-local by design; carrying them across installs conflates audit lineage. v1.x or never.
- **Hard-delete a KPI value** (e.g. wrong unit captured on a since-fixed descriptor). v1.0 fix is descriptor-promote → re-extract. A "force re-extract" admin button is a small v1.x add.

---

## 14. Risk + mitigation

- **Risk:** A descriptor with a sentinel-heavy KPI ("most jobs miss this output") swamps the warehouse with NULL rows; users mistake the layer for broken.  
  **Mitigation:** Descriptor-wizard "KPIs declared" badge encourages explicit thought; the warehouse page surfaces a "this KPI is null for 87% of jobs — is the output name correct?" hint inline when a column is >50% NULL.

- **Risk:** Re-extraction on a million-job install pegs the runner daemon for minutes.  
  **Mitigation:** Rate-limited (yield every 50 jobs); banner surfaces progress; audit logs the start/end so an operator can correlate runner load.

- **Risk:** Numeric precision drift — float → Decimal → CSV could lose decimals at the last leg.  
  **Mitigation:** CSV export emits `Decimal.__str__` directly (no `float()` round-trip). UI display formats from the descriptor's `precision`; the audit-truth value never leaves the table.

- **Risk:** The `(tool_alias, kpi_name)` index degrades on a million-job install with one dominant tool.  
  **Mitigation:** Acceptable for v1.0; partitioning by `tool_alias` is a v1.x follow-on. Add a runbook note for operators ("if a single tool exceeds 5M completed jobs, consider PARTITION BY tool_alias").

- **Risk:** V&V filter join cost on a high-row-count table.  
  **Mitigation:** Re-uses the V&V batched-fetch pattern (one fetch per distinct tool_version_id) — same shape as Compare's column-header badge per `feedback_test_suite_speeds`.

---

## 15. Out-of-band notes

- **Why `Decimal(38, 12)` not `Float`.** Auditable engineering KPIs must compare bit-equal across re-extractions. Float→Decimal→Float through Pydantic round-trips introduces binary-rounding artefacts that break "did this value actually change?" diffs in the audit chain.
- **Why no new permission.** The descriptor is the source of truth for what is a KPI; the descriptor itself is governed by the existing tool-promotion approval flow. Adding `kpi.view` would duplicate `jobs.view`'s ACL ladder without value.
- **Why no curator role.** KPIs are descriptor-bundled metadata; they ship with the tool. The materials / library curator pattern exists because *data* needs governance independent of *tool*. KPI is a property of the tool's outputs, not separate data.
- **Why per-row `unit` storage.** A descriptor edit that changes `kpi_unit` (or drops the override and changes `unit`) must not retroactively re-label historic values. The captured unit is part of the audit record.
- **The "5 KPIs" warehouse-UI degrade-past-12 note from the ADR** — the AG-Grid column manager handles this transparently; no special wide-table UI work. Authors who declare more than 12 KPIs get a wizard hint suggesting `kpi_category` for column grouping (a v1.x feature; in v1.0 the hint is informational only).
