# Design — Parametric Studies

Status: **Design locked 2026-05-16; amended 2026-05-19** (factor-spec & modes
tightening + results-store / retention decisions — see §17). 2-PS-a (core) is
delivered & gate-validated; the amendment and 2-PS-b0/b/c are per §14.
Decision record: `decisions/0001-parametric-studies.md` (+ revision history).
Cross-cutting output-contract & retention change: `decisions/0003-job-io-document-retention.md`.
Brief section: `PROJECT_BRIEF.md` §G39a. Delivery tracking: `ROADMAP.md` Phase 2.

This document is the build-level spec. The ADR records *why*; this records *what to build*.
Everything here is inside the locked brief decisions (A–I). Where it adds an
architectural decision, that decision is already folded into ADR-0001 + §G39a.

---

## 1. Scope at a glance

| Capability | v1 | Fast-follow | v1.x |
|---|---|---|---|
| Run table — exact named configurations (user-authored matrix) | ✅ | | |
| Explicit value list / range / grid (full-factorial) | ✅ | | |
| Per-factor grid levels + 5 spacing methods (linear/log/edges/center) | ✅ | | |
| Fractional-factorial, Plackett–Burman | ✅ | | |
| LHS / Sobol / random space-filling | ✅ | | |
| Central-Composite, Box–Behnken (RSM) | ✅ | | |
| D-optimal | | ✅ | |
| Minimal surrogate (fit + R² + predict + surface overlay) | ✅ | | |
| Response from a declared `csv_plot` column (column + aggregate) | ✅ | | |
| Per-child curve-overlay of a `csv_plot` series across the sweep | ✅ | | |
| Sensitivity: correlation + standardized regression + tornado | ✅ | | |
| Variance-based / Sobol sensitivity (needs Saltelli design) | | ✅ | |
| Retry failed children | ✅ | | |
| Extend study ("add N more points") / clone-and-refine | | ✅ | |
| Closed-loop optimisation; surrogate-as-tool; multi-output co-kriging | | | ✅ |

A study is **single-tool** and **single-shot**. Multi-tool exploration is a
workflow concern, not a study; a study is never reused as a template.

---

## 2. Data model + migration

Hierarchy gains a branch:

```
Project ─┬─ Scenario ─── Job | Workflow        (existing)
         └─ ParametricStudy ─── Job             (new; Job.scenario_id = NULL)
```

**`parametric_study`** (new table)

| col | type | notes |
|---|---|---|
| `id` | PK BigInt | |
| `project_id` | FK → `project.id` | ACL inheritance source (same as `scenario`) |
| `name` | str | auto-suggested, user-editable; globally not required unique (project-scoped display) |
| `tool_alias` | str | the single tool |
| `tool_version` | str | **pinned at creation** (see §6) |
| `mode` | enum | `table` `list` `grid` `frac_factorial` `plackett_burman` `lhs` `sobol` `random` `ccd` `box_behnken` |
| `factor_spec` | JSON | per-input role + vary spec, incl. per-factor grid `levels`+`spacing` (see §3) |
| `sampling_params` | JSON | mode-specific (N samples, resolution, α type, seed, the `table` run rows, …) |
| `run_cap` | int | resolved from install setting at creation; frozen on the row |
| `state` | enum | `draft` `pending_approval` `running` `complete` `partial` `failed` `cancelled` `rejected` |
| `generated_count` | int | points the generator produced |
| `skipped_invalid_count` | int | points dropped by cross-field validation (§5) |
| `seed` | int | RNG seed (reproducibility for stochastic modes) |
| audit/ACL/no-delete | | `created_by`, `created_at`, `state_changed_at/by` — mirrors `scenario` |

**`Job`** changes: add `parametric_study_id` FK (nullable); make `scenario_id`
nullable; add CHECK `((scenario_id IS NOT NULL) <> (parametric_study_id IS NOT
NULL))` — exactly one home. Existing `UNIQUE(scenario_id, tool_alias)` is left
**unchanged**: NULL `scenario_id` rows are distinct in SQLite + Postgres by
default, so N same-tool study children never collide. No service-layer scoping.

Migration is a single Alembic revision (cumulative + idempotent per C13):
create `parametric_study`, add `Job.parametric_study_id`, alter
`Job.scenario_id` nullable, add the CHECK. CI runs it on both engines.

> **2026-05-19 amendment migrations** (chain on the shipped 2-PS-a head):
> `0039` widens the `parametric_study.mode` enum/CHECK to add `table`
> (`op.batch_alter_table(recreate="always")`, frozen literals — no app-enum
> import). The new per-child results store (§10) is a further revision
> delivered with **2-PS-b0**. `factor_spec`/`sampling_params` shape additions
> are JSON-internal — no DDL.

ACL: a study's effective ACL = its project's ACL (read path identical to
`scenario`). No per-study ACL override in v1. Child jobs inherit from the study.
No-delete (D20) applies: studies archive, never delete; draft studies are
reaped on the existing `draft_jobs_days` timer along with their child draft
jobs and CAS references.

---

## 3. The factor specification (`factor_spec` JSON)

Derived from the tool's existing descriptor (the `matofx.tools.forms` pipeline
already gives typed inputs with bounds/options). For each tool input:

```jsonc
{
  "thermal_conductivity": { "role": "vary", "type": "numeric",
                            "vary": { "kind": "range", "min": 0.1, "max": 2.5,
                                      "levels": 7, "spacing": "log_min" } },
  "component_a_pct":       { "role": "vary", "type": "numeric",
                            "vary": { "kind": "list", "values": [10, 20, 30] } },
  "mesh_density":          { "role": "vary", "type": "select",
                            "vary": { "kind": "subset", "options": ["coarse","fine"] } },
  "use_radiation":         { "role": "vary", "type": "bool" },
  "geometry_file":         { "role": "vary", "type": "file",
                            "vary": { "kind": "file_list", "cas_hashes": ["…","…"] } },
  "ambient_temp":          { "role": "constant", "value": 20.0 }
}
```

Per-type vary options:

- **numeric** → `range` (min/max, the factor span used by grid/RSM/LHS) or
  `list` (explicit values) or `distribution` (uniform/normal/loguniform — only
  for lhs/sobol/random). A `range` in **`grid`** additionally carries per-factor
  `levels` (int; defaults to the global `points_per_factor`) and `spacing` (see
  below). `levels`/`spacing` are grid-only — every other mode reads only
  `min`/`max` and ignores them.
- **select / enum** → `subset` of the descriptor's options (categorical factor).
- **bool** → implicitly `{false, true}`.
- **file** → `file_list` of CAS hashes (a discrete factor; per §C agreement,
  **only valid in `list`, `grid`/full-factorial, and `table` modes**, never
  continuous sampling or RSM; never a response-plot axis).
- **constant** → single value; every generated point carries it unchanged.

**Grid spacing** — how a numeric `range` is placed into `levels` points. All
five are *monotonic warps `g(t)` of the normalised position* `t ∈ [0,1]`,
affine-mapped into `[min, max]` as `x = min + g(t)·(max−min)`. The log/cosine is
applied to the **position, never to the data values** — so **there is no
`min > 0` (or sign) constraint**; every spacing works on any range, including
ranges that are negative or span zero:

| `spacing` | placement |
|---|---|
| `linear` | evenly spaced (`g(t)=t`) |
| `log_min` | logarithmic — dense near `min` |
| `log_max` | logarithmic — dense near `max` |
| `edges` | concentrated at both ends, sparse in the middle (cosine) |
| `center` | concentrated in the middle, sparse at the ends |

Integer inputs round each placed level and **de-duplicate**, surfacing a
plain-English warning when distinct levels < requested.

**Run table (`table`) factors** — `table` carries no per-factor vary domain.
Every input is Constant (one value, merged into every run) or Vary (a column);
the explicit run rows live in `sampling_params.runs` (varied columns only).
Any factor type is allowed, **including `file`**. N = the number of rows; no
combination is performed.

RSM modes (`ccd`, `box_behnken`) require **every varied factor to be
continuous-numeric** — the UI gates the mode on factor types and explains why
when it's unavailable.

---

## 4. Sampling-plan generation

`matofx.studies.generate(factor_spec, mode, sampling_params, seed) -> list[dict]`
— a pure function, no DB/IO, exhaustively unit-tested (it is on the
must-be-exhaustively-tested list per H40).

| mode | engine | notes |
|---|---|---|
| `table` | passthrough | the user-authored run rows exactly as entered — no combination |
| `list` | itertools | explicit value sets; cross-product if multiple list factors |
| `grid` | itertools | full-factorial; each numeric `range` → its per-factor `levels` placed by `spacing` |
| `frac_factorial`, `plackett_burman` | `pyDOE3` | screening designs (continuous) |
| `lhs`, `sobol`, `random` | `scipy.stats.qmc` | space-filling; categorical handled by binning |
| `ccd`, `box_behnken` | `pyDOE3` | coded design → mapped to real ranges (±α / face) |

Pipeline, in order:

1. Generate the raw matrix in factor space (for `table`, the matrix *is* the
   user-entered `sampling_params.runs` — no generation; steps 3–5 still apply).
2. Map coded RSM points → real input values via each factor's range.
3. **Validate every point against the descriptor's `cross_field_constraints`**
   (the existing `SumCheck`/DSL evaluator from the forms pipeline). Drop invalid
   points; accumulate `skipped_invalid_count`.
4. Enforce the run cap: if surviving points > `run_cap`, refuse with a
   plain-English message ("This study would run 512 simulations; the limit is
   200. Reduce the ranges or ask your administrator to raise the limit.").
5. Pre-submit confirmation always shows: total points, skipped-invalid count
   and which constraint, and the resolved per-child resource profile. The
   explicit **Preview** action renders the full run matrix as a table (§9).

Determinism: stochastic modes take `seed` (persisted on the row) so a study is
reproducible and a future "extend" can continue the sequence.

---

## 5. Tool-version pinning & descriptor freeze

At study creation MATOFX captures the tool's currently-deployed `(alias,
version)` and the resolved descriptor, and pins **every** child to it — even if
an admin updates the tool mid-study (same rule as F29 / G35-Q4 for standalone
jobs and workflow instances). Without this, a mid-study tool change splits the
response surface across versions and silently corrupts the analysis. The pinned
version is shown in the study header and every results export.

---

## 6. Execution, scheduling & fairness

- Children are ordinary `Job` rows with `parametric_study_id` set; the existing
  runner executes them unchanged, using the tool's existing resource/queue
  profile (`cpu-fast` / `cpu-long` / `gpu` / HPC backend as the descriptor
  dictates).
- **Per-study concurrency cap** (install setting, default e.g. 10): at most N
  children of one study in flight at once, so a 200-run study can't seize every
  worker. Enforced by a study-aware pickup constraint in the runner's
  `_pickup_once` (extends the existing priority ordering, no new runner).
- **Lower-priority study lane**: study children sort *after* interactive
  single jobs and workflow children of equal priority; they never inherit
  skip-queue/priority.
- Cancel: study-level cancel cascades SIGTERM to running children and prevents
  pickup of pending ones (mirrors workflow cancel-cascade). Per-child cancel is
  available from the study detail page.
- Retry: failed children are individually retryable from the study page (v1).
  "Add N more points" / "clone-and-refine" are fast-follow (ROADMAP).
- Terminal roll-up: `complete` (all children complete), `partial` (≥1 complete
  and ≥1 failed/cancelled, none outstanding), `failed` (all failed), `cancelled`.

---

## 7. Approvals

If the pinned tool's descriptor has `approvals != "none"`, the **study has a
single gate** (never per-child), exactly mirroring the workflow single-gate rule
(§D21 cl.2 / §G34 cl.2):

- **pre**: study → `pending_approval`; the approver reviews the *plan* (factor
  spec + generated matrix summary). Approve → children queue. Reject → study
  `rejected` (frozen, immutable per D20); user clones a new study to retry.
- **post**: children all run; study holds at a post-gate; approver reviews
  aggregate results before the study is `complete`.
- The signed approval `data_hash` covers the canonical sampling plan (pre) or
  the canonical results matrix (post). Approver pool = the tool's approver list;
  self-approval forbidden (the §D21 three-layer defence applies unchanged).

---

## 8. Lifecycle & states

```
draft ──(submit)──▶ [pending_approval] ──▶ running ──▶ complete | partial | failed
  │                       │                   │
  │                       └─(reject)─▶ rejected (frozen; clone to retry)
  └─(reaped after draft_jobs_days, with child drafts + CAS refs)
                                          (cancel)─▶ cancelled
```

Draft studies behave like draft jobs (D20): reaped on the `draft_jobs_days`
timer; the reaper decrements CAS refs for any uploaded file-factor inputs.
Nothing is ever deleted post-submit; archive (not delete) hides finished
studies from default lists.

---

## 9. UX surfaces

**Parametric Studies page** (new Dashboard nav entry, behind the feature flag):

1. Pick **tool** (only tools the user is authorised to run; pre-release tools
   only for privileged users, same ladder as the home tool browser §16.8).
2. Pick **project** (the study's home; ACL source).
3. Pick **mode**. The selector is **pre-filtered by the chosen tool's
   descriptor** via a pure `available_modes(descriptor)` helper: a mode the
   tool's input *types* cannot support is greyed with a one-line reason
   (screening/RSM need ≥1 continuous-numeric input — Box–Behnken ≥3;
   lhs/sobol/random need ≥1 numeric or categorical; list/grid/table always
   offerable). Each mode shows an in-UI explanatory note (F30). This is a
   *pre-filter only* — `factors.validate_for_mode` still re-validates the
   actual Constant/Vary selection at preview and submit (two layers).
4. **Factor table**: every descriptor input as a row → Constant | Vary, with a
   type-appropriate editor for the vary spec. For a numeric `range` in `grid`,
   the row also exposes **per-factor `levels` + `spacing`**. For `table` mode
   the surface is a **run-table grid editor** (Vary inputs are columns, one row
   per run). For lhs/sobol/random an explicit **"Number of runs"** field plus a
   persisted **seed** (auto-generated, shown, overridable). Live run-count +
   skipped-invalid as the user edits (instant client-side raw product;
   authoritative net via a debounced `POST /api/studies/preview`).
5. **Preview / Confirm**: a plain-English summary ("This study will run 48
   simulations on the *Heat Transfer* tool, pinned to v1.4. 352 of 400 grid
   points were skipped because 'components must sum to 100%'. Continue?"). An
   explicit **Preview** opens a modal with a scrollable table of *every run
   that will execute* — varied inputs as columns, one job per row — headed
   "N will run · M skipped ('constraint')". When the design exceeds `run_cap`
   the modal shows the plain-English refusal + offending count instead of a
   table (no points exist — generation refuses before building them).
6. Submit → all children queue (or → pending approval).

**Homepage card** — a "Parametric Studies" card directly under the Projects
card, ACL-filtered, listing the user's active/recent studies with a
queued/running/complete/failed/skipped progress strip and overall state. Click →
study detail. (No ETA in v1 — tool durations vary too widely; best-effort
"N of M complete".)

**Study detail page** — two tabs:
- **Progress**: live child-state breakdown, per-child table (id, varied values,
  state, duration), per-child cancel/retry, study-level cancel.
- **Results**: §10. Available progressively as children complete.

Study children are **not** shown in My Jobs (deliberate, §G39a). My Jobs stays
the surface for ad-hoc + workflow work; studies are self-contained.

---

## 10. Results explorer

### 10.0 Durable per-child results store (foundational — 2-PS-b0)

Tool outputs are written to `<wd>/outputs/outputs.json` as an
`OutputsDocument` and are **not** persisted to the DB; the runner's
`_post_complete` parses that file and then `clean_directory` deletes
everything outside the descriptor's `retain_files`, and working dirs are
reaped. A results explorer therefore needs a **durable store written at child
completion**:

- When a study child reaches a terminal state the runner — at the existing
  `_post_complete` parse, **before `clean_directory` runs** — persists into a
  new `parametric_study_result` store: **every numeric and string scalar**
  output (keyed by output name), the child's varied input values, and
  references to the descriptor's `retain_files` artefacts. It survives cleanup
  and reaping regardless of `retain_files`.
- **Failed / cancelled** children are recorded with a **null response**
  (visible, not dropped); excluded from fits; counted in the partial-data
  banner.
- This is the **2-PS-b0** deliverable (store + Results table + CSV export +
  per-child drill-down). 2-PS-b/c are pure read-side over this store.

### 10.1 Response sources

The response axis is chosen **at view time** (never re-run to change it).
A response may be:

1. a named **numeric scalar** output (multi-output tools: user picks which); or
2. a **column of a declared `csv_plot` output**, reduced by a chosen
   **aggregate** (last / max / min / mean / value-at-x). String scalars are
   usable as filters/labels but never as a numeric axis.

**`csv_plot` declared output kind.** Tools declare, per output *file*, what it
is — including a new **`csv_plot`** kind (a CSV/table with numeric columns).
This descriptor/`OutputsDocument`-contract declaration is the gate for both
plottability and the retention floor (ADR-0003). Column roles (x / series) are
**chosen at view time**, not hard-declared, so different analyses use different
columns of the same file. (Cross-cutting contract change — ADR-0003 + the
ADR-0001 revision; also touches the H41 descriptor meta-schema lint.)

### 10.2 Visual surfaces

Operates on the response above. Array/non-`csv_plot` file outputs are listed
and downloadable per child but not plottable.

Rendered with **Plotly** (already in the v2 stack via `matofx.ui.theme`; MIT;
air-gap-safe — no new chart dependency, theme-aware via `apply_theme_to_plotly`):

- **2D**: scatter / line — one factor vs the chosen output.
- **3D**: scatter / surface — one or two factors vs the chosen output.
- **Parallel-coordinates**: Plotly's native trace (interactive brushing), user
  chooses which inputs/outputs are axes. *(This is the "HiPlot capability";
  HiPlot itself is rejected — separate front-end bundle, fights no-Node/no-CDN/
  air-gap.)*
- **Curve-overlay (`csv_plot`)**: overlay every child's series from a declared
  `csv_plot` output (user picks x + y columns), coloured / filtered by a varied
  input — the legacy "sensitivity / parametric chart" + `_repeat` parity ADR-0001
  cites. A distinct surface from the input→scalar response plots; reads the
  retained `csv_plot` files on demand (the scalar store does **not** hold
  series). Large families are down-sampled with a visible note. **2-PS-b**.
- **Sensitivity (v1)**: Pearson/Spearman correlation, standardized regression
  coefficients, tornado chart — all valid on an arbitrary completed sample.
  Variance-based/Sobol indices are fast-follow and only offered when the study
  used a Saltelli design (they are undefined otherwise).
- **Minimal surrogate (v1)**: scikit-learn (BSD) Gaussian-process *or*
  polynomial response surface for the chosen scalar output; show
  cross-validated R²; "predict at a point" form; overlay the fitted surface on
  the 3D plot. **Excluded in v1**: optimisation-on-the-surrogate,
  export-surrogate-as-a-runnable-tool, multi-output co-kriging.
- **Partial data**: results are explorable while the study is still running;
  a persistent banner warns when the completed sample is incomplete or
  failure-biased ("18 of 200 runs failed; sensitivity and surrogate fits on
  this sample may be unreliable.").
- **Export**: CSV (full matrix) + PDF (active view) via the existing Compare
  report machinery. ACL-filtered: a viewer only sees children they can access
  (consistent with §G36 Q7).

Ad-hoc Compare's 2–10 cap is untouched; Study Results is a distinct surface
with its own (study-sized) matrix.

**Results delivery split:** **2-PS-b0** = §10.0 store + Results table +
response-from-scalar/`csv_plot`-column + CSV export + per-child drill-down (the
minimum that makes a finished study investigable). **2-PS-b** = Plotly 2D/3D +
parallel-coords + curve-overlay + correlation/regression/tornado sensitivity.
**2-PS-c** = scikit-learn minimal surrogate. b/c are read-side over the b0
store and add no new write path.

---

## 11. Dependency & licence ledger

Project-wide rule: no paid/restrictive licences; air-gap-bundled; no Node.

| Dep | Use | Licence | Status |
|---|---|---|---|
| `plotly` | all results plots | MIT | already in stack |
| `scipy` (`scipy.stats.qmc`) | LHS/Sobol/random | BSD-3 | already in numeric stack |
| `pyDOE3` | factorial / Plackett–Burman / CCD / Box–Behnken | BSD-3 | **new — vendor + SBOM** |
| `scikit-learn` | scalar surrogate — GP + polynomial (2-PS-c) | BSD-3 | **in use (2-PS-c) — vendor + SBOM regen pending** |
| `statsmodels` | OLS inference (p / CI), ANOVA, RSM (2-PS-c-stats) | BSD-3 | **declared in 2-PS-c-stats — vendor + SBOM regen pending** |
| `SALib` | variance-based/Sobol sensitivity | MIT | **not needed** — item-7 Sobol was hand-rolled on `scipy.stats.qmc` (no new dep) |
| HiPlot | — | — | **rejected** (use Plotly parallel-coords) |

Both new deps are added to the lockfile + CycloneDX SBOM + the SBOM-licence
allow-list lint (H41); vendored into the air-gapped artefact. `scikit-learn`
was pulled forward from 2-PS-c into 2-PS-b (it backs the regression
sensitivity); it is declared in `pyproject.toml` core dependencies, but the
hashed-lockfile + SBOM regeneration + air-gap vendoring remains the pending
maintainer follow-up that already covers `numpy`/`scipy`/`pyDOE3`.

---

## 12. Audit, permissions, licence, feature flag

- **Audit (§E26)**: `study.create`, `study.submit`, `study.approve`,
  `study.reject`, `study.cancel`, `study.child.retry`, `study.complete` — each
  with study id, tool `alias:version`, mode, generated/skipped counts. Child
  job events are the existing job-execution audit events.
- **Permission**: new `parametric_studies.create` verb in the feature matrix;
  default-on for `Regular user` (as custom workflows); `Admin`/`Super-user`
  always; `Auditor` never. Creating a study requires the user to already be
  authorised to run the underlying tool (anti-escalation, §G35 Q2,
  re-checked at submit).
- **Licence (§C16)**: no new studies may be created or submitted in licence
  read-only mode; viewing existing study results is allowed.
- **Feature flag**: `features.parametric_studies` (already in the brief's
  feature catalog). Off ⇒ nav entry, homepage card, API routes and cron all
  absent (per the project-wide toggle rule).

---

## 13. Non-negotiables (build these in from the start, not retrofitted)

1. Tool-version pinning + descriptor freeze at creation (§5).
2. Cross-field-constraint-aware point generation with a reported skip count (§4).
3. Single study-level approval gate when the tool is approval-gated (§7).
4. Per-study concurrency cap + lower-priority study lane (§6).
5. Numeric response axes only — a named scalar **or** a declared `csv_plot`
   column+aggregate; RSM modes require continuous factors (§3, §10).
6. Progressive partial-data results with statistical-safety warnings; failed
   children recorded null-with-banner, never silently dropped (§10).
7. Plain-English everywhere (F30); study children never surface in My Jobs.
8. Durable per-child results store, extracted from `outputs.json` **before**
   post-run cleanup; survives `retain_files`/reaping (§10.0, ADR-0003).
9. Platform-wide retention floor: every job retains `inputs/inputs.json`,
   `outputs/outputs.json`, and any descriptor-declared `csv_plot` file —
   superseding the non-`retain_files` default and `delete_io_files` (ADR-0003).
10. Mode selector pre-filtered by the tool descriptor, re-validated at submit
    (§9 step 3).

---

## 14. Phased build sequence (within ROADMAP Phase 2)

- **2-PS-a — core**: ✅ *delivered & gate-validated.* Migration + data model;
  `factor_spec` + generator (all v1 modes) with constraint filtering + cap;
  tool-version pinning; Parametric Studies page (steps 1–6); execution wiring
  (concurrency cap + study lane); states + cancel + retry; homepage card +
  Progress tab; audit + permission + flag. Exhaustive generator + state-machine
  tests (H40).
- **2-PS-a′ — modes & factor-spec tightening (2026-05-19 amendment)**: built
  slice-by-slice on the shipped core — migration `0039` (`StudyMode += table`);
  `factors.py` (`table` mode, per-factor grid `levels`+`spacing`, drop the
  `min>0` constraint, `available_modes()`); `generate.py` (5 spacing warps,
  `table` engine, `SamplingParams.runs`) + exhaustive H40 cases; UI/API
  (per-factor level/spacing editors, run-table editor, N-runs field, in-UI
  mode notes, live count, full run-table Preview modal, descriptor-filtered
  selector); full gates both engines.
- **2-PS-b0 — results store & table**: ✅ *delivered & gate-validated.* §10.0
  `parametric_study_result` store (+ migration `0041`) written pre-cleanup;
  Results tab = run/response matrix table; response-from-scalar/`csv_plot`-column
  picker; CSV export; per-child drill-down. The minimum that makes a finished
  study investigable.
- **2-PS-b — results explorer**: ✅ *delivered & gate-validated (2026-05-21).*
  Plotly 2D/3D + parallel-coordinates (b-3); server-side `csv_plot`
  curve-overlay (b-1/b-4); computed response-from-column (b-2); Pearson/Spearman
  correlation + standardized-regression coefficients + tornado sensitivity via
  scikit-learn on z-scored data (b-5, `studies/analysis.py` +
  `GET /studies/{id}/sensitivity`); progressive partial-data gating + warnings.
  CSV export reuse from b0 (PDF export deferred with the platform report path).
- **2-PS-b′ — Results-area rework**: ✅ *delivered (2026-05-21).* Wider Results
  area; **labels everywhere** (descriptor labels + units in selectors, the
  AG-Grid full-grid matrix, and every plot — never raw names); the matrix is a
  full grid (sort + per-column filter + grouped show/hide popover) whose
  **filters drive the plots** and whose **row selection highlights** runs in the
  scatter / parallel-coordinates; a single unified plot card with co-located X /
  Y / Z (+ colour) pickers (2D/3D scatter, parallel-coords, csv curve-overlay);
  sensitivity restricted to the **varied** inputs (tornado + a varied-inputs ×
  outputs SRC table). The csv-column-aggregate computed-response was dropped per
  the pilot's call.
- **2-PS-c-stats — engineering statistics**: ✅ *delivered (2026-05-21).* Powered
  by **statsmodels** (BSD-3). (1) per-response **summary + distribution**; (2)
  **significance** — Pearson/Spearman p-values, SRC standard-error / p-value /
  95%-CI error bars, adjusted-R² + RMSE; (4) **best/worst run** per objective +
  a multi-objective **Pareto front**; (5) **main-effects + interaction plots +
  ANOVA % contribution** (sequential/Type-I, for designed grid/factorial
  studies); (6) **quadratic response-surface** fit with contour + classified
  optimum (stationary point). Item (7) variance-based / Sobol global sensitivity
  is deferred to ride on the surrogate (see 2-PS-c).
- **2-PS-c — scalar surrogate**: ✅ *core delivered (2026-05-22).*
  `matofx.studies.surrogate` fits a scalar surrogate over the varied inputs —
  **user-selectable Gaussian-process (default, with 1-sigma uncertainty) or
  degree-2 polynomial** — with **cross-validated Q²** + RMSE + predicted-vs-actual
  as the honest trust metric. The UI is a **JMP-style multi-response Prediction
  Profiler**: rows = responses, columns = varied factors, **per-factor sliders**
  drive every profile + the live predicted value (GP uncertainty band shaded),
  with a **"set to optimum"** optimiser (`scipy` differential evolution). Surrogates
  are fitted once per response (off the event loop) and cached in the session;
  slider moves only re-`predict`. Also: a **surrogate contour profiler** (two
  chosen factors, others held on sliders that re-slice the contour live) and
  **item 7 — variance-based / Sobol global sensitivity** (`sobol_indices`:
  Saltelli sampling on the surrogate → first-order S1 + total-effect ST per
  factor, capturing nonlinearity + interactions the linear SRC misses). True
  drag-on-the-curve is not deliverable in NiceGUI/Plotly (same wall as parcoords
  brushing) — sliders give the same cross-linking. *Only the field/non-scalar
  surrogate remains of the whole parametric-studies plan.*
- **Future (post-2-PS-c) — field/non-scalar surrogate**: extend the surrogate to
  **non-scalar outputs** — full CFD / FEA simulation results (spatial fields,
  meshes, time series), not just scalar outputs. Flagged 2026-05-21; sequenced
  after the scalar surrogate.
- Approvals (§7) land with 2-PS-a if the Approvals feature is already built in
  Phase 2, else as a tracked dependency.

---

## 15. Explicitly out of scope for v1

D-optimal designs; closed-loop optimisation; surrogate beyond the minimal fit
(optimisation-on-surrogate, export-as-tool, multi-output co-kriging);
variance-based/Sobol sensitivity; study extension / clone-and-refine;
multi-tool studies; studies under a scenario; study children in My Jobs;
per-study ACL override; ETA estimation. All recorded in ROADMAP v1.x.

---

## 16. Open questions / revisit triggers

- Default `run_cap` (500, raised from 200 on 2026-05-20; per-tool override on
  the Edit Tool Metadata tab) and default per-study concurrency (10) — confirm
  with the first pilot's hardware before locking the install defaults.
- Whether the first pilot needs Sobol-variance sensitivity early enough to pull
  `SALib` + a Saltelli sampling mode forward from fast-follow.
- Polynomial vs Gaussian-process default for the minimal surrogate — decide
  with real tool output data; expose both, default the safer one.
- `csv_plot` column-role defaults — whether to remember the last-used x/series
  columns per tool, or always pick at view time. Decide with pilot tool data.
- Whether the b0 results store should also snapshot a small head of each
  `csv_plot` file (fast overlay preview) or always read the retained file on
  demand. Decide on pilot CSV sizes.

---

## 17. Revision history

- **2026-05-16 — design locked** (planning conversation); ADR-0001.
- **2026-05-19 — amended** (this document's §1–§14 above), recorded in the
  ADR-0001 revision history and `decisions/0003-job-io-document-retention.md`:
  added the `table` mode (user-authored run matrix, distinct from cross-product
  `list`); per-factor grid `levels` + 5 spacing methods as monotonic
  position-warps (**no `min>0` constraint** — the warp is the *distribution*,
  not a log of the values); descriptor-filtered mode selector
  (`available_modes`) + submit-time re-validation; full run-table Preview
  modal. Results: durable per-child store extracted pre-cleanup (all
  numeric+string scalars; failed children null-with-banner); response axis =
  named scalar **or** declared `csv_plot` column+aggregate; per-child
  curve-overlay; delivery split **2-PS-b0 → 2-PS-b → 2-PS-c**. Two cross-cutting
  platform changes folded in (ADR-0003): the `csv_plot` declared output-file
  kind in the descriptor/`OutputsDocument` contract, and an unconditional
  retention floor (`inputs.json` + `outputs.json` + declared `csv_plot` files,
  every job, superseding the non-`retain_files` default and `delete_io_files`).
- **2026-05-21 — 2-PS-b delivered** (results explorer): Plotly 2D/3D +
  parallel-coordinates, server-side `csv_plot` curve-overlay + computed
  response-from-column, and sensitivity (Pearson/Spearman + standardized
  regression coefficients + tornado) in the pure `matofx.studies.analysis`
  module behind `GET /studies/{id}/sensitivity`. `scikit-learn` (BSD-3) was
  pulled forward from 2-PS-c into 2-PS-b as the SRC backend (declared in
  `pyproject.toml`; lockfile/SBOM/vendor regen still the pending maintainer
  follow-up). 2-PS-b0 also confirmed delivered. Only 2-PS-c (surrogate) remains.
- **2026-05-21 — 2-PS-b′ delivered** (Results-area rework, pilot feedback):
  labels everywhere; AG-Grid full-grid matrix (sort + per-column filter +
  grouped show/hide) whose filters drive the plots and whose row-selection
  highlights runs; unified plot card (X/Y/Z + colour); sensitivity restricted to
  varied inputs + a varied-inputs × outputs SRC table. SRC made robust to
  diverged data (overflow-safe standardisation + extreme-value exclusion).
- **2026-05-21 — 2-PS-c-stats delivered** (engineering statistics): per-response
  summary + distribution; significance (Pearson/Spearman p, SRC SE/p/95%-CI,
  adj-R²/RMSE) — the SRC engine moved sklearn→**statsmodels** OLS; best/worst +
  multi-objective Pareto front; main-effects + interaction plots + ANOVA %
  contribution (Type-I, designed studies); quadratic RSM with contour +
  classified optimum. `statsmodels` (BSD-3) added to core deps (lockfile/SBOM/
  vendor regen = the same pending maintainer follow-up). **Variance-based / Sobol
  global sensitivity (item 7) is deferred to ride on the 2-PS-c surrogate**, and
  the field/non-scalar surrogate stays a later phase.
- **2026-05-22 — 2-PS-c scalar surrogate core delivered**: new
  `matofx.studies.surrogate` — Gaussian-process (default, uncertainty-aware) or
  degree-2 polynomial fit over the varied inputs, cross-validated **Q²** +
  predicted-vs-actual, robust to NaN/diverged values; a JMP-style **multi-response
  Prediction Profiler** (slider-driven, GP uncertainty bands, live predicted
  values) and a **differential-evolution optimiser** ("set to optimum"). Fits are
  cached per session; slider moves only re-predict. Uses `scikit-learn` (now
  actually consumed) + `scipy`.
- **2026-05-22 — 2-PS-c completed**: added the **surrogate contour profiler**
  (two-factor contour, held-factor sliders re-slice it live) and **item 7 —
  variance-based / Sobol global sensitivity** (`sobol_indices`: Saltelli sampling
  via `scipy.stats.qmc` on the surrogate → first-order S1 + total-effect ST,
  Jansen estimators, no SALib dependency). This closes the whole
  parametric-studies plan **except the field/non-scalar (CFD/FEA) surrogate**,
  which stays a later phase.
