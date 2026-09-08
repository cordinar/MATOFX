# 0008. Surrogate family — run cohorts, field surrogates, surrogate-as-tool, refit

Date: 2026-05-26
Status: Accepted (architect ruling 2026-05-26 — full v1.0 scope per "Phase 2 expansion" conversation; companion to ADR-0009; pilot date re-baselined accordingly)

## Context

`PROJECT_BRIEF.md` §G39a established design-space exploration as a first-class
capability; ADR-0001 ruled parametric studies into v1.0 and the build delivered
sampling, classical RSM designs, the durable per-child results store, the full
Results explorer with engineering statistics, and a **scalar surrogate** (GP /
polynomial) with a JMP-style Prediction Profiler, "set-to-optimum" optimiser,
contour profiler, and Sobol variance-based global sensitivity. That work,
shipped 2026-05-21/22, makes MATOFX competitive against well-engineered job
runners on the design-space-exploration axis.

It does **not** yet make MATOFX competitive against commercial SPDM/PIDO tools
(IOSO, modeFRONTIER, Cameo Suite, ANSYS Minerva, Dassault SIMULIA SLM, Altair
HyperWorks Unlimited) on the three capabilities those tools build their
differentiation on:

1. **Surrogates over historical, non-DOE data.** Engineers accumulate runs over
   months — across projects, scenarios, ad-hoc experiments. Today, fitting a
   surrogate requires a *parametric study* — i.e. a designed DOE batch. There
   is no path to "use the 137 historical heat-transfer runs across my last six
   projects as the training set". Real engineering teams have far more
   historical data than DOE data.

2. **Field surrogates** (CFD / FEA). v1.0's surrogate predicts *scalar*
   responses. Real CFD/FEA outputs are *fields* — spatial temperature / stress
   / velocity fields on a mesh, or time series. The v1.x list flagged "field /
   non-scalar surrogate" but kept it deferred. A modern SPDM ships at least
   POD/PCA-based field surrogates as table stakes.

3. **Surrogate-as-tool export.** A fitted surrogate is currently a window into
   a study's data — closable. Customers want to **export** that surrogate as a
   first-class tool, drop it into workflows alongside real solvers, and
   **refit** it as more runs land. This is the highest-leverage capability for
   making MATOFX feel like a *learning* SPDM rather than a static analysis
   surface; it also resolves the v1.x deferral "export-surrogate-as-runnable-
   tool" and partially addresses the SPDM-scope deferral on "model / dataset
   revisioning" (a surrogate model becomes a versioned, governed, reusable
   asset).

The brief did not anticipate these because the parametric-study feature itself
was not in the original §I46 list — both were added during the build. With the
file library (ADR-0006) and materials database (ADR-0007) now shipping the
governance pattern for *immutable, versioned, curator-stewarded engineering
data*, the cost of building a third sibling — **a governed model registry** —
is dominated by the surrogate-engine work itself, not by re-inventing the
governance layer.

The customer is the sole maintainer ([[user_role]]) and has ruled that all
three capabilities ship as one combined ADR at **full v1.0 scope** (rather
than minimum-viable slices with the rest deferred), accepting the pilot-date
slip this implies.

## Decision

Build a **surrogate family** as a coordinated extension of ADR-0001's existing
surrogate engine, governed by a new **Model registry** that mirrors the file-
library (ADR-0006) and materials-database (ADR-0007) patterns. The build-level
spec lands as `designs/surrogate-family.md`; the shared substrate is
`designs/studies-engine.md`; this ADR records the decisions and rationale.

**1. Run cohorts as a sibling of parametric studies.** A new `run_cohort`
table sits beside `parametric_study` and (per ADR-0009) `optimisation_study`,
all three living under `Project` as siblings of `Scenario`. A cohort is a
**saved query** over completed `Job` rows of one tool (one
`(tool_alias, tool_version_id)`, or — operator-tunable — a *set* of tool
versions tolerated as equivalent). Rows are computed live each read; new
matching jobs join automatically. Members can be further filtered by
project(s), scenario, owner, completion-date window, library-file version,
material version, descriptor input ranges, and arbitrary `cross_field`
predicate (`safe_expr`, sibling reference). Lives in `matofx/studies/cohort/`;
ACL via the home project (ADR-0002); cross-project cohort reads intersect
with the caller's per-project ACL — hidden rows surface as "(N jobs hidden by
access controls)". No `Job` FK back to the cohort: membership is set-valued
and computed, not relational.

**2. Field surrogates via POD/PCA modal decomposition.** The existing
`matofx.studies.surrogate` engine extends to handle field-valued responses.
Per `output_kind = field`:

- **Ingestion** — supported formats for v1.0: VTK (`.vtk` / `.vtu` / `.vtp`),
  CGNS (`.cgns`), Ensight Gold (`.case`), and a plain-NumPy `.npy` field
  payload. (`pyvista` + `meshio` cover all three air-gap-bundled,
  permissive-licensed.) FEA-specific binary (Abaqus `.odb`, ANSYS `.rst`)
  defers to v1.x — convert externally for v1.0.
- **Common-mesh assumption for v1.0.** All snapshots of a field response must
  share a topologically identical mesh; the engine refuses ensembles with
  mismatched node counts. A remap step (snap each snapshot to a reference
  mesh via `pyvista.PolyData.sample`) is offered as a **manual pre-step**
  through the tool descriptor (an opt-in `field_remap_reference_mesh` on the
  output field). Automatic remap deferred to v1.x — the cost is the
  numerical-quality validation, not the code.
- **POD/PCA fit** — the snapshot matrix is mean-centred and SVD'd; the
  user picks the **mode-energy cutoff** (default 99% of variance) or a fixed
  mode count. Each retained mode's coefficient series is then a scalar
  response — fit with the existing GP / polynomial engine. The model object
  holds the modal basis + the per-mode scalar surrogates.
- **Predict** at a new design point: predict each mode's coefficient, then
  reconstruct `field = mean + Σ cᵢ · φᵢ`. Uncertainty bands propagate from
  the per-mode GPs.
- **Field viewer** — a minimal predicted-field viewer renders the
  reconstructed field on the source mesh via `ui.scene` (existing) coloured
  by scalar value. Sufficient for "show me predicted temperature at this
  design point"; full ParaView-style volume rendering / streamlines stays
  v1.x. Streamlit ports the per-mode coefficient plot, the mode-energy
  spectrum (scree plot), and a reconstruction-error-vs-mode-count diagnostic.

**3. Surrogate-model artefact + Model registry.** Fitted surrogates become a
first-class, versioned, governed catalogue mirroring file-library / materials.
Two new tables under `matofx/models/`:

- `surrogate_model` (logical entry — mutable metadata: name, description,
  tags, scope, state, optimistic-lock `version`, current `published_version_id`).
- `surrogate_model_version` (immutable; `ordinal`, `publish_state ∈
  {draft, in_review, checked, published, rejected}`, fit metadata, training-data
  references, on-disk blob path, sha-256 hash, fitted-at, fitted-by,
  source kind + source id, R² / Q² / RMSE diagnostics).

**Governance — identical pattern to file library (ADR-0006) and materials
(ADR-0007):**

- Tiered scope: `personal` → `project` → `global`.
- `MODELS_CURATE` permission (closed catalogue) via a new **install-global**
  `model_curator` capability role (ADR-0005 pattern), seeded by the same
  migration. Personal is owner; project is Member+. **Per-tool** curation
  (a `tool_model_curator` join) is **deferred to v1.x** — domain targeting for
  v1.0 is by *nominating* the right checker, not per-tool capability grants
  (architect ruling 2026-06-01; `designs/surrogate-family.md` §5.7).
- **Global / general use is a two-gate release** (architect ruling
  2026-06-01). **Gate 1** — a *toggleable* technical **checker**
  (`models_global_publish_approval`, default ON) using the directed-approver
  model already in `matofx.approvals` (ADR-0006 §5; ADR-0007 §5); the builder
  nominates one checker from `MODELS_CURATE` holders ∪ su/admins **minus self**.
  **Gate 2** — a *mandatory*, non-toggleable **su/admin release** to general
  use. The builder is never the checker or the releaser; the checker may be a
  su/admin (so a two-person minimum is valid). Personal and project scope are
  **ungated**. This is the one place the Model registry is *stricter* than the
  file-library / materials pattern (`designs/surrogate-family.md` §6).
- `approval_request` widens to a **fifth target kind** (`surrogate_model_version_id`).
  The exactly-one-of CHECK bumps 4→5 as a one-line migration.

**On-disk storage:** model blobs live under `<mount>/models/<env>/model_<id>/v<ordinal>/<filename>`,
sha-256 hashed, sanitised filenames + traversal-checked (mirrors the
file-library storage pattern). Allowed serialisation formats restricted to
`joblib` (sklearn pipelines) and a custom in-house JSON-+-numpy bundle for
GP / polynomial models (no raw `pickle` — too broad a deserialisation
surface). Sandboxing of loading happens inside the containerised tool runner
already in use.

**4. Surrogate-as-exportable-tool — synthesis pipeline.** Publishing a
`surrogate_model_version` to global with the "Export as tool" toggle on
generates a `ToolVersion` whose descriptor + orchestrator are fully derived
from the model. The exported tool is a normal `Tool` row going through the
existing install / promote / archive lifecycle (so it inherits ACL, the
job-submission UI, workflow inclusion, audit). Export is **explicit/opt-in**;
the synthesised `ToolVersion` lands in **`test`** (not generally available);
the su/admin edits the *safe* (non-load-bearing) descriptor surface and
promotes `test → released` — and that promotion **is** the §3 gate-2 release
for the export path, so no parallel gate is invented. Editable-field edits
carry forward on refit re-export (`designs/surrogate-family.md` §7.6). The
synthesis:

- **Descriptor** — inputs = the surrogate's factors, each with the label /
  unit / bounds inherited from the source tool's descriptor (frozen at fit
  time). Outputs = the predicted responses, plus per-response uncertainty
  outputs (`<response>_std` for GP-backed models — first-class, not buried
  in metadata). Field responses get a `field_output` with the predicted-field
  viewer wired by default.
- **Orchestrator** — auto-generated Python script that: loads the model bundle
  from disk (the staged-bytes path the runner already uses for any tool's
  static assets); calls `model.predict(inputs)`; writes `outputs.json` and
  any field artefacts. Identical runner contract to a hand-written tool.
- **In-form coverage preview** — the form for a surrogate-tool ships a
  **training-data coverage gauge** built from the model's `training_inputs`
  metadata: for each numeric factor, the gauge shows where the current input
  sits relative to the training range (in / near-edge / extrapolation /
  far-extrapolation). The gauge is *advisory* — submission isn't blocked,
  but the user sees the warning. Optional per-tool descriptor setting
  `block_extrapolation` (off by default) hard-rejects out-of-bounds
  submissions.
- **Lineage tag** — `Job.surrogate_model_version_id` (nullable; new column)
  records which version of which surrogate produced the prediction. Picked
  up by ADR-0010 (data lineage) for the provenance graph.
- **The exported tool's lineage** — every prediction by a surrogate-tool
  carries provenance back to the contributing source jobs (via the cohort /
  study the surrogate was fitted on). This composes with the lineage ADR
  cleanly.

**5. Refit / update lifecycle.** A `surrogate_model` accumulates immutable
versions; each new fit produces a new `surrogate_model_version` (the existing
ordinal-versioning pattern). Three refit triggers:

- **Manual** — curator clicks "Refit on current cohort" / "Refit with these
  N additional points"; a new version enters the §3 release flow (`in_review`
  if the checker toggle is on, else `checked`; a su/admin release then
  publishes it for general use).
- **Cohort growth** — a cohort-backed surrogate gains a "Refit available — N
  new matching runs since last fit" badge in the catalogue; one-click refit.
- **Source extended** — a parametric or optimisation study extension (the
  ADR-0001 fast-follow / ADR-0009 batch progression) writes new
  `study_result` rows; if a surrogate is attached, same one-click refit.

No **online learning** for v1.0 — every refit is a full retrain. Online
learning limits the surrogate families that work and is deferred to v1.x.

**6. Active learning is a strategy in ADR-0009, not a feature here.** The
"pick the next K points where the surrogate is least confident" loop is an
optimisation strategy with an information-gain acquisition function; same
engine, different objective. ADR-0009 owns the loop, this ADR owns the
surrogate it consumes.

**7. Existing scalar surrogate work absorbs into this family.** The
`matofx.studies.surrogate` module (shipped 2026-05-21/22) is the engine all
three siblings already consume; this ADR generalises it — no rewrite, no
breaking change to the existing Parametric Studies → Surrogate flow. The
JMP-style Prediction Profiler, set-to-optimum optimiser, contour profiler,
and Sobol Saltelli sampler all become reusable across the family.

**8. UI surfaces.**

- `/studies` gains a "Cohorts" tab beside Parametric and Optimisation (per
  the unified studies surface in `designs/studies-engine.md` §9).
- `/models` is the new Model registry workspace (catalogue, search by name /
  scope / tag / source-tool, browse per-model versions, fit diagnostics,
  Publish-as-tool dialog).
- `/models/{id}` is the per-model detail page: metadata edit, version
  history with per-version diagnostics + download (curator-only), add-version
  via refit dialog, governed scope-change dialog, retire.
- The submit form for any tool gains a **Find / Refit surrogate** affordance
  in admin / curator views — the same per-tool surrogate fitting that today
  lives behind Parametric Studies.
- `Admin → Inactive Users` is its own row in this scope expansion (see
  ROADMAP "Auto-disable inactive users") — not relevant here.

**9. Out of scope for v1.0** (re-stated for the no-add-back lock):

- Online surrogate learning (incremental update without full refit).
- Multi-output co-kriging.
- Surrogate-tool dynamic re-fit on the fly during a workflow run.
- Tool-author SDK access to the surrogate engine (a tool can't programmatically
  fit a surrogate from inside its orchestrator).
- Field surrogates for Abaqus `.odb` / ANSYS `.rst` binary formats.
- Auto-publish of an updated surrogate when an optimisation converges (ADR-0009
  flag, not a feature this ADR ships).
- Mesh remap automation; common-mesh assumption holds for v1.0.

**10. Cross-cutting impacts.**

- **ADR-0010 (data lineage)** consumes `Job.surrogate_model_version_id` to
  draw the surrogate-tool's provenance edge.
- **ADR-0011 (V&V tracking)** gains "surrogate model" as a third target
  kind for V&V records (alongside tools and jobs) — a surrogate can be
  validated against held-out test data and the record displayed in the
  registry. This ADR reserves but does not implement that — the V&V ADR
  picks it up.
- **ADR-0012 (KPI extraction)** treats surrogate-tool outputs identically to
  any tool's outputs; the KPI declaration mechanism is in the descriptor,
  which the synthesis pipeline generates.
- **ADR-0006 / ADR-0007** patterns are explicitly reused — same scope tiers,
  same capability-role mechanism, same directed-approver gate (Gate 1). A
  reviewer audit could "find" three near-identical governance layers across
  files / materials / models, which is exactly the design intent. Models add
  one deliberate extra: the **mandatory su/admin release** (Gate 2) on top of
  the shared checker gate (architect ruling 2026-06-01; §3).

## Consequences

- **Phase 2 scope expands materially.** This ADR is comparable in build size
  to the file library or materials database — multi-week. Pilot date
  re-baselines per ROADMAP "Pilot date re-baselines 2026-05-26."
- **Storage grows.** Field surrogate ensembles can run to 50–500 MB per
  snapshot × N runs; raw snapshots are retained during fitting and may be
  retained afterwards depending on the model's `keep_training_data` policy.
  Operator guidance lands in `INSTALL.md` per the existing storage-monitoring
  section.
- **`approval_request` gets its fifth target kind**; the CHECK + the partial
  unique index extend cleanly. No data migration needed beyond the constraint
  bump.
- **One new capability role** (`model_curator`) and one new permission
  (`MODELS_CURATE`) — additive, follows ADR-0005's pattern.
- **No `Job` schema rework**; one new nullable FK (`surrogate_model_version_id`)
  and a new optimisation-study FK (per ADR-0009 / studies-engine.md §3).
- **Existing scalar surrogate behaviour is preserved.** Users of Parametric
  Studies' current Prediction Profiler see no change unless they explicitly
  exercise the new "Publish as tool" / "Save to Model registry" affordances.

## Build phases

Per house style (pause for review after the design doc + after each phase):

- **Phase 1** — `matofx/models/` package (orm + service + storage + errors);
  migration adding `surrogate_model` + `surrogate_model_version` + the
  `approval_request` target widening; `MODELS_CURATE` + `model_curator` seed
  via a new migration following the ADR-0005 pattern.
- **Phase 2** — `run_cohort` table + cohort service (query + saved-query
  reader + study_result lazy populate); cohorts tab on `/studies`; cohort
  detail page with the existing Results explorer.
- **Phase 3** — `study_result` rename + `source_kind` discriminator backfill;
  surrogate engine refactor to source-agnostic ingestion (no behavioural
  change for parametric users).
- **Phase 4** — Field-surrogate ingestion (`pyvista` + `meshio`); POD/PCA fit;
  per-mode scalar surrogate; reconstruct + predicted-field viewer + scree
  plot + reconstruction-error diagnostic. Tests cover one VTK + one CGNS +
  one Ensight + one .npy ensemble.
- **Phase 5** — Surrogate-as-tool descriptor + orchestrator synthesis;
  registration as a `ToolVersion`; submit form's training-data coverage
  gauge; `Job.surrogate_model_version_id`.
- **Phase 6** — Refit lifecycle (manual, cohort-growth, source-extended);
  per-model UI for refit + diagnostics + publish-as-tool; toggleable
  approval-gate wiring.
- **Phase 7** — `/models` catalogue + detail page; admin/curator UX for
  scope change, archive, retire; tests across personal/project/global +
  approval-gate paths.
- **Phase 8** — Docs + cross-ADR consistency review (lineage, V&V, KPI
  extraction touchpoints) + ROADMAP tick.

## Revision history

- **2026-06-01 — governance amended.** Model-registry release flow refined
  during design review: per-tool curation deferred to v1.x (install-global
  `model_curator` + checker nomination cover v1.0); **global / general use is
  a two-gate flow** — toggleable technical checker (Gate 1) then a mandatory
  su/admin release (Gate 2) — with the builder excluded from both; personal +
  project scope ungated. Surrogate-as-tool export is explicit/opt-in,
  synthesised into a `test` `ToolVersion` whose safe surface the su/admin edits
  before promoting `test → released` (the gate-2 release for that path), with
  editable-field edits carrying forward on re-export. Detail in
  `designs/surrogate-family.md` §5.7 / §6.2–§6.4 / §7.6. Touched §3, §4, §5,
  §10 here.
- **2026-05-26** — initial ruling at full v1.0 scope; companion to ADR-0009;
  shares `designs/studies-engine.md`. Decision driven by the discovery that
  customers ask for "build a surrogate from past runs", "predict whole
  fields, not just scalars", and "use surrogates as workflow components" as
  table-stakes for a modern SPDM. The originally-deferred items
  "export-surrogate-as-runnable-tool" and "field/non-scalar surrogate"
  promote from v1.x into this ADR.
