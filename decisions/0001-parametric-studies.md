# 0001. Parametric studies (design-space exploration)

Date: 2026-05-16
Status: Accepted (v1 scope ruled by the architect 2026-05-16; design refined the same day through the §G39a planning conversation — see Revision history)

## Context

MATOFX is positioned as an SPDM ("Simulation Process & Data Management")
product. The defining capability of an SPDM — the thing that separates it from
a well-engineered job runner — is **design-space exploration**: sweeping inputs,
running many variants, and rolling the results up for comparison. The
`PROJECT_BRIEF.md` (47 questions, A–I) is exhaustive on the platform,
security, and operations layers but contained **no design-exploration feature**.

Evidence the need is real and was unmet:

- The legacy app already grew an ad-hoc `material_properties_repeat` "repeat"
  tool — a bolt-on that exists precisely because users needed to run the same
  tool over varying inputs and the platform had no first-class way to do it.
- The Compare page already ships a **"sensitivity / parametric" chart** that
  auto-activates "when the compared set sweeps one input" (brief §G36 Q5). That
  chart had **no producer** — a user had to hand-build the swept runs one
  scenario at a time. The visualisation was designed for exactly this feature;
  the feature that feeds it was never specified.
- The `one-tool-per-scenario` invariant (`UNIQUE(scenario_id, tool_alias)`,
  Alembic 0017; architecture §16) means a 50-point sweep by hand = 50 scenarios.
  No serious simulation customer will accept that.

Without this, a knowledgeable evaluator's first reaction is "this isn't an
SPDM, it's a job tracker." It is the single highest-leverage SPDM gap.

## Decision

Introduce a first-class **Parametric Study** that fans a single tool out over a
generated input matrix, runs the variants on the existing queue/runner, and
gives them a dedicated results-exploration surface. Shape chosen to reuse proven
v2 machinery and stay inside every locked brief decision:

**Object model — Parametric Study is a child of Project, parallel to Scenario.**
The hierarchy gains a branch: `Project → { Scenario → Job/Workflow } |
{ ParametricStudy → Job }`. A `parametric_study` row carries `project_id`, the
pinned tool `(alias, version)`, the mode, the factor specification (JSON), the
run cap, state, and the standard audit / ACL / no-delete fields — it inherits
the project ACL exactly as a `Scenario` does. Child jobs reference the study via
a new `Job.parametric_study_id` FK and carry `scenario_id = NULL`. **This
sidesteps the `one-tool-per-scenario` constraint entirely**: `UNIQUE(scenario_id,
tool_alias)` treats NULL `scenario_id` rows as distinct in both SQLite and
Postgres by default, so N same-tool study children never collide and **no
service-layer constraint-scoping is required** — a strict simplification of this
ADR's original plan. A portable CHECK guarantees every job has exactly one home:
a non-null `scenario_id` (ad-hoc or workflow child) **or** a non-null
`parametric_study_id` (study child), never neither. There is **no separate
study-definition/run split** (unlike Workflow / WorkflowRun): a study is a
single-shot instance; "clone-and-refine" creates a *new* study, preserving D20
immutability. Study child jobs are viewed and acted on from the **Parametric
Studies** surface, **never from My Jobs** — a deliberate asymmetry vs workflow
children (a study may have hundreds of children; My Jobs would drown).

**Sampling modes — all in v1.** explicit value **list**; **range / grid**
(per-factor start/stop by step or N points → full-factorial cross-product);
**fractional-factorial** + **Plackett–Burman** screening; space-filling
**Latin-Hypercube / Sobol / random**; response-surface **Central-Composite** +
**Box–Behnken** (RSM coded designs, offered only when every varied factor is
continuous-numeric). A configurable hard total-run cap (default 500; per-tool override via the Edit Tool Metadata tab — see ADR-0001 2026-05-20 revision) plus a
mandatory plain-English pre-submit confirmation ("This study will run 48
simulations. Continue?") per F30. *Fast-follow:* **D-optimal** (model-dependent,
optimiser-driven — a different build from emitting a fixed design matrix);
study-extension ("add N more points" / "clone-and-refine ranges"). *v1.x:*
closed-loop optimisation; surrogate capability beyond the minimal fit below;
variance-based (Sobol) sensitivity (it requires a Saltelli sampling design, so
it is only meaningful for studies sampled that way).

**Execution & fairness.** Children enqueue on the existing Arq queues with each
tool's existing resource/queue profile; the runner is unchanged. Every child is
pinned to the **tool version captured at study creation** (descriptor frozen
with it) so a mid-study tool update can't split results across versions.
Generated points are validated against the descriptor's `cross_field_constraints`
*before* enqueue; invalid points are dropped and the count surfaced ("352 of 400
violate 'components sum to 100%' and were skipped; 48 will run"). A **per-study
concurrency cap** and a **lower-priority study lane** stop a large study
starving interactive users; study children never inherit skip-queue/priority.
Terminal state: `Complete` / `Partial` / `Failed` / `Cancelled`. Cancel
cascades; failed children support retry.

**Results.** A dedicated **Study Results** surface (distinct from ad-hoc
Compare, whose 2–10 cap is unchanged) renders with **Plotly** (already in the v2
stack, MIT, air-gap-safe — no new chart dependency): 2D scatter/line, 3D
scatter/surface (1–2 factors vs one scalar output), and **parallel-coordinates**
(Plotly's native trace — *not* HiPlot, which fights the no-Node / no-CDN /
air-gap rules) with user-chosen input/output axes. Sensitivity, two tiers: v1 =
correlation (Pearson/Spearman) + standardized regression coefficients + tornado
(valid on any sample); variance-based/Sobol = fast-follow, only when
Saltelli-sampled. A **minimal surrogate** ships in v1 (architect ruling, option
ii): scikit-learn (BSD) Gaussian-process / polynomial fit of one scalar output,
cross-validated R², predict-at-a-point, fitted-surface overlay. Explicitly
**excluded from v1 even so**: optimisation-on-surrogate, export-surrogate-as-
runnable-tool, multi-output co-kriging. Results are explorable **progressively
on partial data**, with explicit statistical-safety warnings when the completed
sample is incomplete or failure-biased. CSV/PDF export reuses the Compare report
machinery; results are ACL-filtered.

**Approvals.** If the tool is approval-gated the **study has one approval gate**
over the whole study (pre = approver reviews the plan/matrix; post = reviews
aggregate results) — identical to the single-gate workflow rule (§D21 clarifier
2 / §G34 clarifier 2); never per-child. Reject → study frozen; the user clones a
new study to retry. The signed `data_hash` covers the canonical plan (pre) or
results matrix (post).

**Permissions / licence / flag.** New `parametric_studies.create` verb,
default-on for `Regular user` (as custom workflows), gated on the user already
being authorised to run the underlying tool (anti-escalation, §G35 Q2). All
study create/submit/approve/reject/cancel/retry events audited (§E26). No new
studies in licence read-only mode (§C16). Behind the `features.parametric_studies`
flag per the project-wide per-customer toggle rule. All copy plain-English per
F30.

**Alternatives considered.** (1) Manual N-scenario workaround — rejected: the
defining SPDM gap, unusable at real sweep sizes. (2) Study as a Scenario child
with the `UNIQUE` constraint scoped in `JobService` (this ADR's original plan) —
superseded: NULL-scenario children under a Project-level study is simpler, needs
no constraint code, and matches the product intuition that "a study is not a
scenario." (3) Full DOE + closed-loop optimisation up front — optimisation
deferred to v1.x: it needs an iterative run-evaluate-resample runtime that is a
project in itself; the full sampling/RSM set + minimal surrogate already
delivers the SPDM value.

## Consequences

- **Accepted for v1, 2026-05-16**, with the refined scope above (all sampling /
  RSM modes + minimal surrogate in; D-optimal + study-extension fast-follow;
  closed-loop optimisation + surrogate-beyond-minimal + Sobol-variance
  sensitivity v1.x).
- **`PROJECT_BRIEF.md` updated** (per §I47): §G39a rewritten to this design;
  §I46 must-ship row updated; §I46 deferred list now defers only
  D-optimal / optimisation / surrogate-beyond-minimal / extend-study; feature
  catalog already carries `[features.parametric_studies]`.
- **Comprehensive design captured in `designs/parametric-studies.md`** (data
  model + migration, UX flows, generator, execution, results, dependency/licence
  ledger, phased build sequence). The ADR records the decision; the design doc
  records the build.
- One Alembic migration: `parametric_study` table, `Job.parametric_study_id`
  FK, `Job.scenario_id` made nullable, the home-exactly-one CHECK. No
  constraint-scoping code (NULL-scenario handles it). Cumulative + idempotent
  per C13.
- New runtime dependencies — all permissive-licence-clean, air-gap-bundled, no
  Node: `pyDOE3` (BSD-3, factorial/RSM designs) and `scikit-learn` (BSD-3,
  surrogate). `scipy.stats.qmc` (LHS/Sobol) and `plotly` are already in the
  stack. `SALib` is **not** pulled in for v1 (Sobol-variance sensitivity is
  fast-follow). HiPlot is explicitly rejected.
- `ARCHITECTURE.md` §16 gains the NULL-scenario / `parametric_study_id` note
  **when the feature is built** (that doc tracks as-built state, not design).
- **(2026-05-19 amendment)** Adds Alembic `0039` (`StudyMode += table`) on the
  shipped 2-PS-a head, plus a `parametric_study_result` store migration with
  2-PS-b0; `factor_spec`/`sampling_params` shape additions are JSON-internal.
  Spawns `decisions/0003-job-io-document-retention.md` for the two cross-cutting
  platform changes (the `csv_plot` declared output kind + the I/O-document
  retention floor) — both touch shipped runner cleanup, the tool-descriptor /
  `OutputsDocument` contract, and the H41 descriptor meta-schema lint, so they
  carry their own regression surface across *all* jobs, not just studies.
- **Revisit when:** first pilot customer's sweep sizes / optimisation need is
  known — drives D-optimal, study-extension, and how aggressively closed-loop
  optimisation is roadmapped.

## Revision history

- **2026-05-16 — created**, Status Proposed.
- **2026-05-16 — Accepted** (architect ruling: ship in v1).
- **2026-05-16 — refined the same day** through the §G39a planning
  conversation: study is a Project child with NULL-scenario children
  (constraint-scoping code dropped — strict simplification); all sampling + RSM
  modes in v1 (D-optimal fast-follow); minimal scikit-learn surrogate in v1;
  Plotly parallel-coordinates instead of HiPlot; per-study concurrency lane,
  tool-version pinning and constraint-aware point generation made explicit
  non-negotiables; single-shot study (no definition/run split); comprehensive
  design moved to `designs/parametric-studies.md`.
- **2026-05-19 — amended** (factor-spec & modes tightening + results-store /
  retention), after 2-PS-a shipped. Sampling: added a 10th mode **`table`** (a
  user-authored run matrix — exact named configurations, distinct from the
  cross-product `list`); **per-factor grid `levels`** + **5 spacing methods**
  (`linear`/`log_min`/`log_max`/`edges`/`center`) defined as monotonic warps on
  the *normalised position*, affine-mapped into `[min,max]` — so **the `min>0`
  constraint is dropped entirely** (the warp is the spacing *distribution*, not
  a logarithm of the values; works on any sign of range); the mode selector is
  **pre-filtered by the tool descriptor** (`available_modes`) and re-validated
  at submit; **Preview** renders the full run matrix as a modal table. Results:
  a **durable per-child results store** extracted from `outputs.json` at
  `_post_complete` **before** `clean_directory` (persists all numeric+string
  scalars; failed/cancelled children recorded **null-with-banner**, never
  dropped); the response axis may be a **named numeric scalar OR a column of a
  declared `csv_plot` tabular/CSV output reduced by an aggregate**, chosen at
  view time; plus a **per-child curve-overlay** surface (legacy
  sensitivity-chart / `_repeat` parity). Results delivery is split
  **2-PS-b0** (store + Results table + response-from-column + CSV export +
  drill-down) → **2-PS-b** (Plotly plots + overlay + sensitivity) →
  **2-PS-c** (surrogate). Two **cross-cutting platform changes** are folded in
  and recorded in `decisions/0003-job-io-document-retention.md`: (a) a
  **`csv_plot` declared output-file kind** in the tool-descriptor /
  `OutputsDocument` contract (also touches the H41 descriptor meta-schema
  lint); (b) an **unconditional retention floor** — every job (single,
  workflow child, study child) retains `inputs/inputs.json`,
  `outputs/outputs.json`, and any declared `csv_plot` file, superseding the
  non-`retain_files` default and any descriptor `delete_io_files`. Build is
  slice-by-slice on the shipped core: migration `0039` (`StudyMode += table`),
  then `factors.py` / `generate.py` (+ exhaustive H40 cases) / UI-API / gates;
  the b0 results store ships its own migration with 2-PS-b0. Full build spec in
  `designs/parametric-studies.md` (§17 revision history).
- **2026-05-21 — 2-PS-b delivered** (results explorer): Plotly 2D/3D +
  parallel-coordinates, server-side `csv_plot` curve-overlay + computed
  response-from-column, and v1 sensitivity — Pearson/Spearman correlation +
  **standardized regression coefficients** (tornado) computed in the pure
  `matofx.studies.analysis` module and served by
  `GET /studies/{id}/sensitivity`. **`scikit-learn` (BSD-3) was pulled forward
  from 2-PS-c into 2-PS-b** to back the standardized-regression fit (z-scored
  inputs + response via `LinearRegression`); it is declared in `pyproject.toml`
  core dependencies with the lockfile/CycloneDX-SBOM/air-gap-vendor regeneration
  remaining the same pending maintainer follow-up as `numpy`/`scipy`/`pyDOE3`.
  Only **2-PS-c** (minimal surrogate) remains of the parametric-studies plan.
- **2026-05-21 — 2-PS-b′ + 2-PS-c-stats delivered** (pilot feedback after using
  2-PS-b): **2-PS-b′** reworked the Results area — descriptor labels everywhere
  (never raw names), an AG-Grid full-grid matrix (sort + per-column filter +
  grouped show/hide) whose filters drive the plots and whose row-selection
  highlights runs, a unified plot card (X/Y/Z + colour), and varied-inputs-only
  sensitivity (tornado + SRC table). **2-PS-c-stats** added the engineering
  statistics an engineer expects: per-response summary + distribution;
  significance (correlation p-values, SRC standard-error / p-value / 95%-CI,
  adjusted-R² + RMSE); best/worst + multi-objective **Pareto front**;
  main-effects + interaction plots + **ANOVA % contribution** (designed
  studies); and a quadratic **response surface** with contour + classified
  optimum. The SRC engine moved from scikit-learn to **statsmodels** (BSD-3, new
  core dep, same pending lockfile/SBOM/vendor follow-up); scikit-learn is
  retained for the surrogate. **Variance-based / Sobol global sensitivity
  (item 7) is deferred to ride on the 2-PS-c surrogate.** Full build detail in
  `designs/parametric-studies.md` §14/§17.
- **2026-05-22 — 2-PS-c scalar surrogate core delivered**: `matofx.studies.surrogate`
  fits a scalar surrogate over the varied inputs — **user-selectable Gaussian
  process (default, uncertainty-aware) or degree-2 polynomial** — reported with
  **cross-validated Q²** + predicted-vs-actual (not in-sample R²). The UI is a
  **JMP-style multi-response Prediction Profiler**: per-factor sliders drive every
  response's profile + live predicted value (GP uncertainty band), plus a
  differential-evolution **"set to optimum"** optimiser. True drag-on-the-curve
  isn't deliverable in NiceGUI/Plotly (same limit as parcoords brushing) — sliders
  give the identical cross-linking. `scikit-learn` is now actually consumed (+
  `scipy` for the optimiser).
- **2026-05-22 — 2-PS-c complete**: added the **surrogate contour profiler**
  (two-factor contour with held-factor sliders) and **item 7 — variance-based /
  Sobol global sensitivity** (first-order S1 + total-effect ST, Saltelli sampling
  on the surrogate via `scipy.stats.qmc` — **SALib was not needed**, dependency
  surface unchanged). This completes the entire parametric-studies plan **except
  the field/non-scalar (CFD/FEA) surrogate**, which remains a later phase.
