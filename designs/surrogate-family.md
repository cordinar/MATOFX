# Design — Surrogate family (run cohorts · field surrogates · Model registry · refit lifecycle)

Status: **Design locked 2026-05-29; governance amended + re-locked 2026-06-01** (companion to ADR-0008; written alongside ADR-0009 / `designs/optimisation-framework.md`). Implementation not started.
Decision record: `decisions/0008-surrogate-family.md`. Shared substrate: `designs/studies-engine.md`. Foundation: `decisions/0005-multi-role-capabilities.md`, `decisions/0006-file-library.md`, `decisions/0007-materials-database.md`.
Existing engine this generalises: `designs/parametric-studies.md` §10 (the 2-PS-c scalar surrogate, shipped 2026-05-22 as `matofx/studies/surrogate.py`).
Brief sections: `PROJECT_BRIEF.md` §G39a (design-space exploration). Delivery tracking: `ROADMAP.md` Phase 2.

This is the build-level spec. The ADR records *why*; this records *what to build*. Everything here is inside ADR-0008 + ADR-0005. Shared mechanics (the three siblings under one roof, the unified `study_result` table, ACL inheritance, audit prefixes) live in `designs/studies-engine.md` and are referenced by section here rather than restated.

---

## 1. Scope at a glance

| Capability | v1 | v1.x |
|---|---|---|
| Run cohort — saved query over completed jobs of one tool | ✅ | |
| Cross-project cohort (intersection with caller's per-project ACL) | ✅ | |
| Cohort tool-version tolerance list (curator-curated) | ✅ | |
| Cohort lazy `study_result` population on first read | ✅ | |
| Cohort growth badge ("N new matching runs since last fit") | ✅ | |
| Scalar surrogate (GP + polynomial) — source-agnostic ingestion | ✅ | (generalises today's parametric-only fit) |
| Field surrogate — POD/PCA over snapshot ensemble | ✅ | |
| Field formats: VTK (`.vtk`/`.vtu`/`.vtp`), CGNS (`.cgns`), Ensight Gold (`.case`), `.npy` | ✅ | |
| Predicted-field viewer (coloured mesh via `ui.scene`) | ✅ | |
| Common-mesh assumption + opt-in descriptor `field_remap_reference_mesh` | ✅ | |
| Automatic mesh remap (numerical-quality validated) | | ✅ |
| FEA binary formats (Abaqus `.odb`, ANSYS `.rst`) | | ✅ |
| Model registry — `surrogate_model` + `surrogate_model_version` | ✅ | |
| Tiered scope: personal → project → global | ✅ | |
| `MODELS_CURATE` via `model_curator` capability role (install-global) | ✅ | |
| Per-tool curator assignment (`tool_model_curator` table) | | ✅ |
| Checker gate on global publish — directed nomination, toggleable (default ON) | ✅ | |
| su/admin release to general use — always required, not toggleable | ✅ | |
| Surrogate-as-exportable-tool synthesis | ✅ | |
| Per-response uncertainty outputs (`<response>_std` for GP) | ✅ | |
| In-form training-data coverage gauge + optional hard `block_extrapolation` | ✅ | |
| `Job.surrogate_model_version_id` lineage tag | ✅ | |
| Refit lifecycle (manual / cohort-growth / source-extended) | ✅ | |
| Multi-output co-kriging | | ✅ |
| Online surrogate learning (incremental update without full refit) | | ✅ |
| Surrogate-tool dynamic re-fit mid-workflow-run | | ✅ |
| Tool-author SDK access to the surrogate engine | | ✅ |
| Auto-publish on optimisation convergence | | ✅ (ADR-0009 flag) |

A surrogate model is **single-tool** (one `tool_alias`, one or more tolerated `tool_version_id` pins). Multi-tool surrogates are a v1.x concern; the design space is too divergent to fit one model across solver versions whose descriptors differ.

---

## 2. The three siblings — what this doc owns

Per `designs/studies-engine.md` §1, three siblings sit under `Project` next to `Scenario`:

```
Project ─┬─ Scenario ─── Job / WorkflowRun
         └─ Study branch
                 ├─ ParametricStudy   (existing — ADR-0001)
                 ├─ OptimisationStudy (ADR-0009 — `designs/optimisation-framework.md`)
                 └─ RunCohort         (this doc §3)
```

This doc owns:

- **RunCohort** — the third sibling (§3).
- The unified surrogate engine that any sibling feeds (§4–§5).
- The **Model registry** (`surrogate_model` + `surrogate_model_version`) and its governance (§6).
- The **surrogate-as-tool** synthesis pipeline (§7).
- The **refit lifecycle** (§8).
- The Job lineage tag `Job.surrogate_model_version_id` (§9).

It does **not** own (cross-references only):

- The `study_result` rename / discriminator backfill — `designs/studies-engine.md` §6.
- The `Job.optimisation_study_id` FK or the optimisation study runtime — `designs/optimisation-framework.md`.
- The lineage view of a surrogate-tool job — `designs/data-lineage.md`.
- The V&V record on a surrogate version — `designs/vv-tracking.md` (`vv_surrogate` target kind, follow-up).

---

## 3. Run cohorts

A cohort is a **saved query** over completed jobs of one tool. Membership is computed live; new matching jobs join automatically; archived/retired jobs drop out. The cohort has no `Job` FK back-reference — membership is set-valued and resolved at read time.

### 3.1 What a cohort selects

```
candidates = job
WHERE job.tool_alias = cohort.tool_alias
  AND job.tool_version_id IN cohort.tolerated_tool_version_ids
  AND job.state = 'complete'
  AND (cohort.project_ids IS NULL OR job.project_id IN cohort.project_ids)
  AND (cohort.scenario_ids IS NULL OR job.scenario_id IN cohort.scenario_ids)
  AND (cohort.owner_user_ids IS NULL OR job.created_by_user_id IN cohort.owner_user_ids)
  AND (cohort.completed_after IS NULL OR job.state_changed_at >= cohort.completed_after)
  AND (cohort.completed_before IS NULL OR job.state_changed_at <= cohort.completed_before)
  AND (cohort.library_file_version_ids IS NULL
       OR EXISTS (SELECT 1 FROM job_library_file_use jlu
                  WHERE jlu.job_id = job.id
                    AND jlu.library_file_version_id IN cohort.library_file_version_ids))
  AND (cohort.material_version_ids IS NULL
       OR EXISTS (SELECT 1 FROM job_material_use jmu
                  WHERE jmu.job_id = job.id
                    AND jmu.material_version_id IN cohort.material_version_ids))
  AND (cohort.input_ranges IS NULL
       OR all_input_ranges_satisfied(job.inputs_json, cohort.input_ranges))
  AND (cohort.cross_field_predicate IS NULL
       OR safe_expr_eval(cohort.cross_field_predicate, job.inputs_json))
```

`input_ranges` is `{factor_name: {min, max}}` (numeric inputs only); `cross_field_predicate` is a `safe_expr` string (the same DSL the descriptor uses for `cross_field_constraints` and that the optimisation framework reuses, §F30 / §16.16).

**ACL intersect** happens after this selection: the calling user's per-project ACL filters out jobs they cannot see. Hidden rows surface as a footnote ("N jobs hidden by access controls") — never an error.

### 3.2 Tool-version tolerance

`tolerated_tool_version_ids` is a list. Default at creation: `[tool_version_id_at_creation]` (one entry, the version the cohort was anchored on). The cohort owner / project Member+ can **add** equivalent versions; the system surfaces a plain-English warning when adding a version whose descriptor differs in input set, input type, or unit (because mixing those will silently corrupt the surrogate fit).

No "automatic equivalence" inference for v1. The curator decides; the system warns.

### 3.3 Lazy `study_result` population

The first time a cohort is read with intent to consume the data (the Results explorer opens it, a surrogate fits on it, an export runs against it), the cohort service:

1. Resolves the membership query (§3.1) → list of `job.id`s.
2. For each `job.id` not already in `study_result` with `source_kind='cohort' AND source_id=cohort.id`, reads the canonical `inputs.json` + `outputs.json` from the job's retained-floor (ADR-0003) and writes a `study_result` row.
3. Returns the full set.

Subsequent reads do the cheap diff and write only new rows. A cohort's "freshness" stamp (`last_resolved_at`) appears in the UI so the user knows when membership was last recomputed.

**Why lazy not eager?** A job doesn't know which cohorts it belongs to (cohorts are queries, not memberships) — eager would mean every job-completion event scanning every cohort. Lazy keeps the cohort cost paid only when a user actually looks.

**What if a `job.id` later transitions back out** (e.g. archived)? The next read drops it from the membership set, but the `study_result` row remains (it's a snapshot of *that completion*, not a live mirror). Surrogates fitted on that snapshot stay valid; the next fit will exclude the now-out-of-set row.

### 3.4 State machine

Per `designs/studies-engine.md` §2:

```
active ──archive──▶ archived
```

No `running` — a cohort doesn't run anything. Archive hides from default lists; never deletes (no-delete pattern, D20 + ADR-0002). Attached surrogates carry their own state (§6.2).

---

## 4. Surrogate engine — source-agnostic ingestion

The existing `matofx.studies.surrogate` module (`fit_surrogate(factor_columns, response, kind)`, `Surrogate.predict(points)`, `sobol_indices`, `optimize_surrogate`; shipped 2026-05-22) is the canonical fit/predict surface. ADR-0008 generalises it without breaking the parametric-studies caller.

### 4.1 The ingestion layer

A new `matofx.studies.surrogate.ingest` module sits between the source kind and the existing `fit_surrogate`:

```python
def collect_training_data(
    source_kind: Literal["parametric", "optimisation", "cohort"],
    source_id: int,
    response_spec: ResponseSpec,
    factor_filter: FactorFilter | None = None,
) -> TrainingData:
    """Pull (X, y) from study_result rows for one source.

    Returns aligned columns + response, applies the existing NaN /
    extreme-value masking, and surfaces the same partial-data banner
    the Results explorer uses today.
    """
```

`ResponseSpec` is `{kind: 'scalar', name: str}` or `{kind: 'csv_column', file: str, column: int, aggregate: 'last|max|min|mean|at_x', at_x: float | None}` (the same `csv_plot` column+aggregate machinery the parametric Results explorer already exposes) or `{kind: 'field', field_name: str, format: 'vtk|cgns|ensight|npy', mesh_node_count: int | None}` (new).

`FactorFilter` is the same shape the cohort uses for `input_ranges` + `cross_field_predicate`; a fit can train on a *subset* of a cohort without modifying the cohort.

### 4.2 Existing scalar callers unchanged

`matofx.studies.surrogate.fit_surrogate(factor_columns, response, kind)` keeps its current signature. Parametric Studies' Results explorer continues to pass `(factor_columns, response)` directly. The new ingestion layer is *additive* — a thin layer that builds those same arguments from `(source_kind, source_id, response_spec)`.

The Surrogate class gains two new properties so consumers (surrogate-as-tool synthesis, in-form coverage gauge) can introspect:

- `Surrogate.training_inputs: dict[str, list[float]]` — the values each factor took during fitting (drives the coverage gauge).
- `Surrogate.source_ref: SourceRef | None` — `(source_kind, source_id, n_points, fitted_at)`; `None` for the current-style "fit from raw columns" callers (parametric flow keeps working unchanged).

### 4.3 Field-output support

A new `fit_field_surrogate(field_snapshots, factor_columns, *, mode_energy_cutoff: float = 0.99, max_modes: int | None = None)` lives beside `fit_surrogate` in the same module.

**Pipeline:**

1. **Load** each snapshot from its retained file via `pyvista` (VTK/CGNS/Ensight) or `numpy.load` (`.npy`). Each loaded snapshot is a 1-D float array of the field values at the mesh nodes.
2. **Common-mesh check** — all snapshots must have the same node count; the engine raises `FieldSurrogateError("Mismatched mesh: snapshot 7 has 12345 nodes; expected 10000")` on first mismatch and aborts. The descriptor's opt-in `field_remap_reference_mesh` flag (§4.4) replaces this with a remap step.
3. **Stack** as `M` of shape `(n_snapshots, n_nodes)`; compute and subtract the mean field `mu` (shape `(n_nodes,)`).
4. **SVD** the centred matrix → `U, S, V` (truncated; `scipy.sparse.linalg.svds` for `n_nodes > 50_000`, otherwise `numpy.linalg.svd`). Compute cumulative explained-variance ratio; truncate to the first `k` modes where ratio ≥ `mode_energy_cutoff` (default 0.99) **or** at `max_modes` if set.
5. **Per-mode scalar surrogates** — the `k`-length coefficient vector for each snapshot becomes the `k` response series. Call `fit_surrogate(factor_columns, mode_coeffs, kind='gp')` `k` times.
6. **Return** a `FieldSurrogate` holding `factors`, `mu`, `modal_basis` (the retained `V` rows), `per_mode_surrogates`, `mode_energy_spectrum`, and `quality` (cross-validated reconstruction RMSE on held-out snapshots).

**Predict:**

```python
field_surrogate.predict(point: dict[str, float]) -> FieldPrediction
    # FieldPrediction has:
    #   .field: np.ndarray of shape (n_nodes,) — reconstructed field values
    #   .field_std: np.ndarray | None — per-node 1-sigma propagated from per-mode GPs
    #   .mode_coefficients: list[float]
    #   .mode_coefficient_stds: list[float] | None
```

Per-node uncertainty propagates as `sqrt(sum((basis[i] * mode_std[i]) ** 2))` — assumes mode independence (true for orthogonal POD modes under the GP-noise model used here). Documented as approximate; tighter UQ is a v1.x research item.

### 4.4 The descriptor's `field` output kind

Per ADR-0008 §2 the descriptor's `OutputField` gains a `kind: 'field'` value (alongside the existing `csv_plot`, etc.). A field output declares:

```jsonc
{
  "name": "temperature_field",
  "kind": "field",
  "file_format": "vtu",                  // vtk / vtu / vtp / cgns / case / npy
  "filename": "outputs/temperature.vtu",
  "field_node_array_name": "Temperature",  // for VTK/CGNS — which point-data array to load
  "field_remap_reference_mesh": null     // optional path to a reference .vtu inside `static/`;
                                         // when set, every snapshot is remapped to this mesh
                                         // via pyvista.PolyData.sample before stacking
}
```

The retention floor (ADR-0003) is extended: descriptor-declared `field` outputs join `inputs.json` / `outputs.json` / `csv_plot` files in the unconditionally-retained set. Without this, the file is reaped by `clean_directory` and the surrogate has nothing to train on.

Compute cost note: a 50–500 MB VTU snapshot × 137 jobs is 7–70 GB just for the snapshot matrix during the fit. The fitter loads streamed (one snapshot at a time → centre → accumulate the `M^T M` Gram matrix) when `n_snapshots × n_nodes × 8` would exceed a per-job memory budget (operator setting `surrogate_field_fit_memory_gb`, default 4). Diagnostic log entries cover memory + wall-clock budget.

### 4.5 Predicted-field viewer

A new `FieldSurrogateViewer` NiceGUI component renders a `FieldPrediction` on the source mesh. Built on `ui.scene` (already in use for the CAD geometry viewer per ARCHITECTURE §16.9) + a per-vertex colour mapping (default colormap: `viridis`). Components:

- Mesh load: from the reference VTU/VTP embedded in the model bundle (§5.3).
- Per-vertex colour: linearly mapped from `field.min()` / `field.max()` (with a "fixed range" toggle so frames stay comparable across slider moves).
- Per-vertex uncertainty overlay: optional, renders `field_std` as a second mesh with opacity proportional to `std / mean(std)` (visible noise where the surrogate is unsure).
- Mode-coefficient bar chart + scree (mode-energy spectrum) + reconstruction-error-vs-mode-count diagnostic land alongside as Plotly inline panels.

Full ParaView-style volume rendering / streamlines / animated time-series stays v1.x (recorded in the SPDM-scope-deferral table, ROADMAP).

---

## 5. Data model + migration

New package `matofx/models/` for the registry; new module `matofx/studies/cohort/` for cohorts. Migrations chain off the V&V / KPI head (`0057`); cumulative + idempotent; PK pattern `sa.BigInteger().with_variant(sa.Integer(), "sqlite")`; optimistic lock via `version` where the row is mutable.

### 5.1 `run_cohort` (the saved query)

| col | type | notes |
|---|---|---|
| `id` | PK | |
| `project_id` | FK → `project.id` RESTRICT | home project; ACL source |
| `name` | str(128) | display label; not unique |
| `description` | text | |
| `tool_alias` | FK → `tool.alias` RESTRICT | the single tool |
| `tolerated_tool_version_ids` | JSON list of int | one or more tool_version ids the cohort accepts |
| `project_ids` | JSON list of int / null | NULL = home project only; explicit list = cross-project |
| `scenario_ids` | JSON list of int / null | optional further filter |
| `owner_user_ids` | JSON list of int / null | optional further filter |
| `completed_after` / `completed_before` | timestamp / null | optional window |
| `library_file_version_ids` | JSON list of int / null | optional further filter |
| `material_version_ids` | JSON list of int / null | optional further filter |
| `input_ranges` | JSON `{factor: {min, max}}` / null | numeric-input bounds |
| `cross_field_predicate` | text / null | `safe_expr` DSL |
| `state` | str(16) | `active` / `archived` |
| `last_resolved_at` | timestamp / null | freshness stamp; touched by the lazy populate |
| `created_at` / `created_by_user_id` | | |
| `version` | int | optimistic lock |

CHECK constraints: `state IN ('active','archived')`; `tolerated_tool_version_ids` non-empty (DB-level via `sa.text` for JSON length on Postgres; service-level for SQLite).

### 5.2 `surrogate_model` (logical entry; mutable metadata)

| col | type | notes |
|---|---|---|
| `id` | PK | |
| `name` | str(255) | display title |
| `description` | text | |
| `tags` | JSON list of str | |
| `tool_alias` | FK → `tool.alias` RESTRICT | the single tool this model predicts for |
| `scope` | str(16) | CHECK `IN ('personal','project','global')` |
| `project_id` | FK → `project.id` RESTRICT, nullable | set iff `scope='project'` |
| `owner_user_id` | FK → `user.id` RESTRICT | steward; defaults to creator |
| `state` | str(16) | `active` / `pending` / `retired` (no hard delete) |
| `published_version_id` | FK → `surrogate_model_version.id` SET NULL, nullable | the version pickable by default |
| `created_at` / `created_by_user_id` | | |
| `version` | int | optimistic lock |

Portable CHECK enforces `project_id` non-null **iff** `scope='project'`. `scope` is the *current* scope; transitions are audited state changes, never deletions.

### 5.3 `surrogate_model_version` (immutable)

| col | type | notes |
|---|---|---|
| `id` | PK | |
| `surrogate_model_id` | FK → `surrogate_model.id` CASCADE | |
| `ordinal` | int | 1,2,3… per model; UNIQUE `(surrogate_model_id, ordinal)` |
| `kind` | str(16) | `scalar` / `field` |
| `factors` | JSON list of `{name, type, unit, min, max}` | frozen from descriptor at fit time |
| `responses` | JSON list of `{name, kind, unit}` | scalar names or one field-output spec |
| `source_kind` | str(16) | `parametric` / `optimisation` / `cohort` |
| `source_id` | int | logical FK target depends on `source_kind` |
| `tool_version_ids_at_fit` | JSON list of int | the actual tool versions in the training set |
| `n_training_points` | int | |
| `bundle_path` | str | absolute path under the StorageLocation mount |
| `bundle_sha256` | str(64) | integrity check on load |
| `quality` | JSON | `{q2, rmse_cv, n, kind, modes_retained?, mode_energy?, recon_rmse?}` |
| `keep_training_data` | bool | when true, the bundle includes a copy of the training (X, y); when false, only metadata |
| `publish_state` | str(16) | `draft` / `pending` / `published` / `rejected` |
| `notes` | text | optional fitter / curator notes |
| `fitted_at` | timestamp | |
| `fitted_by_user_id` | FK → `user.id` RESTRICT | |

Versions are append-only; a refit mints a new version. The `bundle_sha256` is verified on every load — corruption raises `ModelBundleCorruptError` rather than silently returning bad predictions.

**Bundle layout on disk:**

```
<mount>/models/<env>/model_<id>/v<ordinal>/
  manifest.json                  # version + factors + responses + checksums per inner file
  scalar/
    surrogate_<response>.joblib  # one per scalar response (for scalar kind)
  field/
    mean_field.npy               # field kind: mean over snapshots
    modal_basis.npy              # field kind: retained modal rows
    reference_mesh.vtu           # field kind: source mesh for the viewer
    mode_<i>_surrogate.joblib    # field kind: per-mode scalar surrogate
  training/
    inputs.parquet               # if keep_training_data
    response.parquet             # if keep_training_data
```

Sanitised filenames, traversal-checked, never `pickle.load` outside a `joblib` call. Allowed serialisation formats are `joblib` (sklearn pipelines only) + the custom JSON+numpy bundle for the manifest and modal artefacts. Raw `pickle` is rejected at load time.

### 5.4 `Job.surrogate_model_version_id` (new nullable FK)

Added by this migration:

```python
op.add_column("job", sa.Column(
    "surrogate_model_version_id",
    sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
    sa.ForeignKey("surrogate_model_version.id", ondelete="RESTRICT"),
    nullable=True,
))
op.create_index("ix_job_surrogate_model_version_id", "job", ["surrogate_model_version_id"])
```

Non-null only on jobs whose tool is a surrogate-as-tool synthesised tool (§7). The synthesised orchestrator writes the value via the existing `JobService` submit path. ADR-0010 consumes this to draw the surrogate-tool's provenance edge.

### 5.5 `approval_request` target widening (4 → 5)

The approval target CHECK (file_library / materials / vv_tool / vv_job) gains `surrogate_model_version_id`. One-line constraint bump + a new `target_kind` literal added to `matofx.approvals.orm`. Identical mechanics to the V&V widening (migration `0056`).

### 5.6 `study_result` rename + discriminator

Per `designs/studies-engine.md` §6 the existing `parametric_study_result` becomes `study_result` with `source_kind` + `source_id`. The rename is one Alembic migration with an UPDATE backfilling `source_kind='parametric'` and `source_id = parametric_study_id`. The old column drops in the next migration after consumers are switched.

**This is a shared cost with ADR-0009.** Both ADRs land this migration in their *first* phase; whichever ships first owns the migration; the other consumes it. The phased build sequence below assumes ADR-0008 ships first (per the studies-engine §11 ordering — `surrogate_assisted_real` hard-depends on the Model registry).

### 5.7 Permissions seed (`MODELS_CURATE` + `model_curator`)

Following the ADR-0005 pattern (the capability-role mechanism), the migration adds:

- A new permission row `MODELS_CURATE` with description "Act as technical checker for global surrogate-model publishes."
- A new capability role `model_curator` mapped to `MODELS_CURATE`.
- No assignment — operators grant `model_curator` to specific users via the existing admin user-edit picker.

**The capability is install-global, not per-tool** (architect ruling 2026-06-01). A `model_curator` is *eligible* to be nominated as the technical checker for any tool's surrogate; domain targeting happens by the builder **nominating** an appropriate checker (§6.3), not by assigning curators to specific tools. Per-tool curator assignment — a `tool_model_curator(tool_alias, user_id)` join narrowing the eligible-checker pool per tool — is **deferred to v1.x**: cheap to add later without a data reshape, so reserve the concept now (mirrors the ROADMAP "reserve schema space" guidance) but don't build the table for v1.0.

**Release authority is separate from `MODELS_CURATE`.** The mandatory final release to general use (§6.3 gate 2) is a **super-user / admin** action, not a `MODELS_CURATE` grant — `model_curator` covers the technical *check*; the su/admin role covers the formal *release*.

---

## 6. Model registry — governance

Mirrors the file library (ADR-0006) and materials database (ADR-0007) layer-for-layer. Same scope tiers, same capability mechanism, same directed-approver gate. A reviewer audit could "find" three identical governance layers across files / materials / models — exactly the design intent.

### 6.1 Scope resolution

`scope_visible(model, U)`:

- `personal` → `model.owner_user_id == U`
- `project` → U has ADR-0002 Viewer+ on `model.project_id`
- `global` → visible to all authenticated users

The picker (and the `/models` catalogue) shows the latest `published` version by default with a "choose version" affordance. A pending (un-approved) global model version is **not** visible in pickers — it surfaces only in the curator's approval inbox.

### 6.2 Version state machine

Personal and project scope publish directly (no gate — §6.3). **Global / general use** runs a two-gate flow: a technical **checker** (toggleable) then a mandatory **su/admin release**.

```
draft ──submit (nominate checker)──▶ in_review ──checker approves──▶ checked ──su/admin releases──▶ published
                                              ╰─checker rejects──▶ rejected
checked ──su/admin declines──▶ rejected
```

A draft is the immediate output of `fit` before any gate. Drafts are visible only on the model's own detail page (to the steward), never in pickers. `in_review` holds a nominated checker; when the checker gate is toggled off the version skips straight to `checked` (release-pending). `checked` means technically signed off but **not yet generally available** — only a su/admin release flips it to `published`, which is what the world sees in pickers. Rejected versions stay on the version list (auditable history); the model's `published_version_id` rolls back to the previous published version if one exists, or `NULL` if not.

### 6.3 The two-gate release (global / general use)

Personal and project scope have **no gate** — a fitted model is immediately usable by its steward (personal) or the project team (project). Only landing in **global / general use** runs the release flow. "Landing in global" = a new model created at global scope, a new version of an already-global model, or a `set_scope(… 'global')` transition. The surrogate-as-tool export (§7) is the same boundary and reuses the gate via the tool lifecycle (§7.6).

**Gate 1 — checker (toggleable).** Controlled by `models_global_publish_approval` (General Settings, default ON, `requires_role='super_user'`, "Data governance" section):

- **ON:** the action sets `surrogate_model_version.publish_state='in_review'` (and `surrogate_model.state='pending'` for a brand-new global model). The builder nominates **one checker** — drawn from `MODELS_CURATE` holders ∪ su/admins, **minus self** — and a request is created via `matofx.approvals` with `designated_approver_user_id` set. Submit is blocked until required fields are valid **and** a checker is chosen. Checker approve → `publish_state='checked'`; audited `models.model.check_approve`. Checker reject → `publish_state='rejected'`, the prior published version (if any) remains canonical; audited `models.model.check_reject`.
- **OFF:** the version moves straight to `checked` (release-pending) with no checker request.

**Gate 2 — su/admin release (always required, not toggleable).** A super-user / admin performs the formal release: `checked → published`, `surrogate_model.published_version_id = this.id`, the model becomes pickable; audited `models.model.release`. A su/admin may instead decline at this step (`checked → rejected`; audited `models.model.release_decline`) — e.g. on a policy concern the technical check didn't cover.

**Who can be whom.** The hard invariant is **the builder is never the checker or the releaser** (no self-anything). The checker *may* be a su/admin — so a two-person flow (builder + one su/admin who both checks and releases) is valid; the two transitions are still audited separately. The releaser is always a su/admin. Per-tool domain targeting is achieved by *nominating* a domain-appropriate checker, not by per-tool capability assignment (deferred — §5.7).

Same "no self-approval" property as ADR-0006 / ADR-0007, strengthened with the mandatory release authority. Same edge: if the chosen checker is unavailable, an admin can reassign; finer rules deferred to v1.x.

### 6.4 Scope transitions

- `personal → project` — Member+ on the destination project. **No gate** (the project's curators see it; if they don't want it, they retire it).
- `project → global` — runs the two-gate release of §6.3 (checker if the toggle is ON, then the mandatory su/admin release).
- `global → project` / `global → personal` — admin override only (audited `models.model.scope.demote`). Existing surrogate-tool jobs continue to function (the FK-protected version isn't deleted); new picker queries no longer surface it.

---

## 7. Surrogate-as-exportable-tool

Publishing a `surrogate_model_version` to global with the **"Export as tool"** toggle on generates a `ToolVersion` whose descriptor + orchestrator are fully derived from the model. The exported tool is a normal `Tool` row going through the existing install / promote / archive lifecycle (so it inherits ACL, the job-submission UI, workflow inclusion, audit).

### 7.1 Generated descriptor

```toml
[tool]
alias = "surrogate_<source_tool_alias>_<surrogate_model_id>"
display_name = "<surrogate_model.name>"
description = "Surrogate of <source tool> (v<ordinal>). <surrogate_model.description>"

[exec]
backend = "local"                # surrogates always run in-process; no AOS / HPC
queue = "cpu-fast"
resource_profile = { cpu = 1, memory_mb = 2048 }

[inputs]
# One entry per factor in surrogate_model_version.factors:
#   - type / unit / min / max inherited from the source tool's descriptor (frozen at fit time)
#   - cross_field_constraints from the source descriptor are copied across
#   - bounds tightened to the training range when block_extrapolation = true (§7.3)

[outputs]
# One entry per response in surrogate_model_version.responses:
#   - scalar responses: kind = "scalar"
#   - field responses: kind = "field" + the original file_format
#   - per-response std outputs added as siblings: "<response>_std" (GP-backed only)

[surrogate]
surrogate_model_version_id = <id>
block_extrapolation = false      # per §7.3
```

The descriptor is generated **once** at publish time and frozen onto the `ToolVersion` — the same way every other tool version is frozen at install. Subsequent refits of the underlying model don't mutate this descriptor; they mint a new `ToolVersion` (§8).

### 7.2 Generated orchestrator

```python
# surrogate_orchestrator.py — auto-generated, do not edit
"""
Surrogate tool orchestrator.
Loads model bundle <id>/v<ordinal>, predicts at the inputs, writes outputs.json.
"""
import json
from pathlib import Path

from matofx.models.runtime import load_surrogate_model_version
from matofx.tools.ddp import PythonWrapper

with PythonWrapper(__file__) as ddp:
    inputs = ddp.inputs
    model = load_surrogate_model_version(
        version_id=<id>,                       # baked at synthesis
        expected_sha256="<bundle_sha256>",     # baked at synthesis
    )
    prediction = model.predict([inputs])
    ddp.outputs.update(prediction.to_outputs_dict())
```

The runner contract is unchanged — `inputs.json` arrives in the working dir, `outputs.json` lands beside it, retention applies. For field responses the orchestrator additionally writes the predicted field to the declared filename (e.g. `outputs/temperature.vtu`) so the existing field viewer / KPI extraction / lineage all work without surrogate-specific code paths.

`load_surrogate_model_version` reads the bundle from the canonical model storage path (`<mount>/models/<env>/...`) — there is no per-tool copy of the bundle bytes. Promotion / scope demotion of the underlying model doesn't bake into the tool; the tool always points at one immutable version id.

### 7.3 In-form training-data coverage gauge

The form for a surrogate-tool ships a **coverage gauge** built from `surrogate_model_version.factors` (which carries each factor's training-set min/max/mean). For each numeric factor, the gauge classifies the current input value:

| Class | Condition | UI |
|---|---|---|
| `in_range` | `value ∈ [training_min, training_max]` | green tick |
| `near_edge` | within 10% of either edge of the training range | yellow info |
| `extrapolation` | `value` outside the training range but within ±50% of the training span | orange warning |
| `far_extrapolation` | `value` more than ±50% of training span outside the range | red warning |

The gauge is *advisory* — submission isn't blocked. The optional per-tool descriptor setting `block_extrapolation` (off by default) hard-rejects out-of-bounds submissions with a plain-English message. When `block_extrapolation=true` the tool's input bounds are **tightened** to the training range at descriptor synthesis time (§7.1), so the existing form-validation layer enforces it without a special path.

A summary indicator on the submit button shows the worst class across all factors: green / yellow / orange / red. Hover surfaces "3 factors extrapolating, 1 near edge".

### 7.4 Lineage tag (`Job.surrogate_model_version_id`)

The synthesised orchestrator (via the existing `JobService.queue_for_runner`) writes the model version id onto the child `Job` row. ADR-0010's `LineageService` reads this column to draw the surrogate-tool edge in the provenance graph; the per-prediction lineage composes back to the cohort / study that fitted the surrogate (which composes back to the contributing source jobs).

### 7.5 What surrogate-tool jobs can and can't do

- **Can:** appear in workflows alongside real solvers; produce KPIs (ADR-0012 extracts headline outputs identically); be compared with real-tool jobs (ADR-0004 / Compare); enter approval flows (the synthesised descriptor inherits `approvals='none'` by default — operator can override per tool).
- **Cannot:** be cancelled mid-prediction (surrogates run in milliseconds — the cancel path is unused); take a long-running queue (always `cpu-fast`); be picked as a study's *fitting* tool (a surrogate-of-a-surrogate is rejected at study creation with a plain-English message).

### 7.6 Tool creation, editing, and release

Exporting is an **explicit, opt-in action** — not every global model becomes a runnable tool (many exist only for the registry or to back `surrogate_assisted_real` studies, `designs/optimisation-framework.md` §6.3). The "Export as tool" affordance is available once the model version is `checked` (§6.2).

**MATOFX auto-generates both artefacts** — the descriptor (§7.1) and the orchestrator (§7.2); the operator never hand-writes TOML or Python. `register_as_tool` (§10.3) calls the existing `ToolService.install`, and the synthesised tool lands as a `ToolVersion` in state **`test`** — installed but **not** generally available.

**The su/admin then edits the safe surface and promotes `test → released`** — and that promotion *is* the §6.3 gate-2 release for the exported-tool path (the existing admin-gated tool promotion already supplies the release authority; no parallel gate is invented).

**Editable vs frozen.** Some descriptor fields are cosmetic; others are load-bearing — change them and the tool either misrepresents the model or breaks the bundle-integrity contract. The editor renders the frozen fields read-only with a one-line explanation ("derived from surrogate model v\<n\> — refit and re-export to change").

| Field | Editable before release? | Rationale |
|---|---|---|
| Display name, description, tags | ✅ | Cosmetic |
| `[exec]` queue / resource profile | ✅ | Tunable; **`backend` stays `local`** (surrogates run in-process) |
| Approvals policy | ✅ | Synthesised default `approvals='none'` (§7.5); operator overrides per tool |
| Email notifications, tutorials, presets | ✅ | Normal tool metadata |
| `block_extrapolation` toggle | ✅ | Advisory-vs-hard-block is a genuine operator choice (§7.3) |
| Input labels / help text / unit strings | ✅ | Presentation only |
| Input identities / types / bounds | 🔒 | Derived from the model's training factors; widening invites silent extrapolation, renaming breaks the predict mapping |
| Output names / structure | 🔒 | The orchestrator maps predictions onto these names |
| `surrogate_model_version_id` + `bundle_sha256` | 🔒 | This is the model↔tool pin; changing it = a different model |
| The orchestrator `.py` | 🔒 | The correctness + integrity contract ("auto-generated, do not edit") |

**Carry-forward on re-export.** A refit re-export mints a **new `ToolVersion`** of the same `Tool` (§10.3). The su/admin's prior **editable-field** edits (queue, approvals policy, display text, tutorials, `block_extrapolation`) **carry forward** onto the freshly-synthesised version so the operator doesn't re-tune them every refit; the frozen fields are re-derived from the new model version. The carried-forward version still lands in `test` and still requires a `test → released` promotion (architect ruling 2026-06-01).

---

## 8. Refit / update lifecycle

A `surrogate_model` accumulates immutable `surrogate_model_version` rows over time. Each new fit produces a new version (the ordinal-versioning pattern). Refits never mutate the published version; they mint candidates that go through the publish gate.

### 8.1 Refit triggers

| Trigger | Who | UI surface |
|---|---|---|
| **Manual** | Curator (or steward, for personal) | "Refit on current cohort" / "Refit with these N additional points" button on `/models/{id}` |
| **Cohort growth** | System badge | "Refit available — N new matching runs since last fit" on `/models/{id}` and on the model row in `/models` |
| **Source extended** | System badge | "Source study extended — N new points" when the source is a parametric or optimisation study that gained children |

For all three the refit produces a new `surrogate_model_version` in `publish_state='pending'` (or `'published'` directly if the install toggle is off and the model is global, or if the model is personal/project where the gate doesn't apply).

### 8.2 What the refit captures

The refit re-runs the ingestion + fit with the same `kind` (scalar or field), the same factor set, and the source's *current* membership. The new version captures:

- `n_training_points` at this fit
- `tool_version_ids_at_fit` (may differ from prior version if the cohort's tolerated list changed)
- `quality` (fresh CV metrics — comparable to prior version side-by-side on the model detail page)
- `bundle_sha256` (fresh)

If the source has no new points since the last fit, the refit short-circuits with a plain-English message ("No new data since version 7 fitted on 2026-05-12; refit would produce an identical model.").

### 8.3 The "Refit available" badge

Cheap to compute: `current_cohort_member_count - last_version.n_training_points`. Surfaced on:

- The `/models` catalogue (small numeric badge on the row).
- The `/models/{id}` header.
- The exported tool's submit page (admin / curator view only — a discoverability nudge for the maintainer, not the end user).

Only computed for cohort-backed and study-backed sources whose underlying set can grow. Static cohorts (those filtered to a closed window) never show the badge.

### 8.4 No online learning

Every refit is a full retrain. Online learning (incremental update without full refit) limits the surrogate families that work and is a v1.x research item. The "Refit on current cohort" button always re-runs the full pipeline.

---

## 9. Lineage / V&V / KPI cross-cuts

ADR-0008 §10 lists the cross-ADR impacts. The build-level handles:

### 9.1 Data lineage (ADR-0010)

- `EdgeKind.PRODUCED_BY` already exists; a job that ran a surrogate tool gets an additional `EdgeKind.PREDICTED_BY` edge pointing at the `surrogate_model_version` node.
- `NodeKind.SURROGATE_MODEL` is added (16th kind) with the lineage view's existing resolver pattern (`LineageService._resolve_surrogate_model`).
- The surrogate's own provenance — back to source jobs via the cohort / study — composes via the existing `_list_study_results` handler; no new resolver beyond the SurrogateModel node itself.
- The standalone `/admin/lineage` picker gains "Surrogate model" as a third picker kind (alongside the existing 14 host kinds).

### 9.2 V&V tracking (ADR-0011)

Reserved by ADR-0008 §10; implementation deferred to a V&V follow-up. The `vv_surrogate` target kind would mirror `vv_tool` / `vv_job`: a curator records "this surrogate was validated against held-out test data X by Y on Z" — surfaced on the model detail page and in the surrogate-tool's submit-form badge. Not in this doc's build phases.

### 9.3 KPI extraction (ADR-0012)

Surrogate-tool outputs flow through the existing extraction hook in `runner.supervisor._post_complete` with no surrogate-specific code path. The synthesised descriptor's KPI declaration (`kpi: true` on selected outputs, copied from the source tool's KPI declaration at fit time) is what the hook reads.

---

## 10. Service surfaces

### 10.1 `matofx.studies.cohort.CohortService` (async, audited)

- `create_cohort(project_id, name, tool_alias, tolerated_tool_version_ids, **filters)` — write a new `run_cohort`; audit `study.cohort.created`.
- `update_cohort(cohort_id, **changes)` — edit filters / metadata; optimistic-locked; audit `study.cohort.query_edited`.
- `resolve_members(cohort_id, user)` — §3.1 query intersected with the user's ACL; returns `(job_ids, acl_hidden_count)`.
- `populate_study_results(cohort_id, user)` — §3.3 lazy populate; updates `last_resolved_at`.
- `archive_cohort(cohort_id)` — `state='archived'`; never hard-deletes; audit `study.cohort.archived`.
- `list_for_project(project_id, user)` / `get(cohort_id, user)` — read side.
- `attach_surrogate(cohort_id, model_id)` — record the cohort → model linkage for refit-available badging (a join table `run_cohort_surrogate(cohort_id, model_id)` keeps the relationship; CASCADE on both).

### 10.2 `matofx.models.ModelService` (async, audited)

- `create_model(name, tool_alias, scope, project_id, source_kind, source_id, kind, factors, responses, ...)` — fit + write new `surrogate_model` + first `surrogate_model_version`; audit `models.model.create` + `surrogate.fit_started` / `surrogate.fit_completed`.
- `refit(model_id, additional_filter=None)` — §8 refit; mints new version; audit `surrogate.refit`.
- `update_metadata(model_id, **changes)` — entry-level fields; optimistic-locked.
- `set_scope(model_id, new_scope, **kwargs)` — §6.4 transitions; routes through approvals if applicable.
- `submit_for_review(version_id, checker_user_id)` — global only: `draft → in_review` with a nominated checker (≠ builder); routes through `matofx.approvals`. When the checker toggle is off, moves straight to `checked`.
- `release_version(version_id, releaser_user_id)` — su/admin only: `checked → published` (the mandatory gate-2 release, §6.3). The releaser may be the same su/admin who checked, never the builder.
- `retire(model_id)` — `state='retired'`; no hard delete.
- `list_for_picker(user, tool_alias)` — picker query against §6.1.
- `open_bundle(version_id, user)` — access-checked path → `(path, sha256)`; verifies sha256 on read.
- `load_for_runtime(version_id, expected_sha256)` — used by the synthesised orchestrator (§7.2); raises `ModelBundleCorruptError` on mismatch.

Permission gates: read = scope-resolved (§6.1); personal create = authenticated; project create = ADR-0002 Member+ on the project; global publish = `MODELS_CURATE`. Every method audits `models.model.<verb>` or `surrogate.<verb>` per the namespace in `designs/studies-engine.md` §8.

### 10.3 Tool synthesis (`matofx.models.tool_synthesis`)

- `synthesise_descriptor(model_version) -> ToolDescriptor` — §7.1.
- `synthesise_orchestrator(model_version) -> str` — §7.2 (returns Python source).
- `register_as_tool(model_version, **install_options) -> ToolVersion` — calls the existing `ToolService.install` with the synthesised descriptor + orchestrator; the new `ToolVersion` lands in **`test`** (not released) for su/admin edit + promotion (§7.6); audit `models.exported_as_tool`.

Re-registering an already-exported model with a newer version mints a new `ToolVersion` of the same `Tool` (the tool alias is stable — `surrogate_<source_tool>_<model_id>`); the existing tool-version state machine (`new`/`test`/`released`/`archived`) governs visibility, the new version lands in `test` and requires a `test → released` promotion to reach general use, and the su/admin's prior editable-field edits carry forward onto it (§7.6).

---

## 11. API surface

All audited, all `*Error` → plain-English HTTP, all under a feature-gated router (absent when `Feature.SURROGATE_FAMILY` off — note: file-library / materials / VV / KPI all use separate feature flags; surrogate family follows the same per-ADR gating pattern).

### 11.1 Cohorts

```
GET    /api/projects/{pid}/cohorts                list cohorts in project (ACL-filtered)
POST   /api/projects/{pid}/cohorts                create cohort
GET    /api/cohorts/{cid}                         detail + resolved member count
PATCH  /api/cohorts/{cid}                         edit (optimistic lock)
POST   /api/cohorts/{cid}/resolve                 force re-resolve membership now (debug / refresh)
POST   /api/cohorts/{cid}/archive                 archive (no delete)
GET    /api/cohorts/{cid}/members                 paginated members list (intersected with caller ACL)
GET    /api/cohorts/{cid}/results                 study_result rows for the cohort (lazy populate)
```

### 11.2 Models

```
GET    /api/models                                catalogue (scope+tool filters; ACL-filtered)
POST   /api/models                                create model (fit + first version)
GET    /api/models/{mid}                          detail + versions
PATCH  /api/models/{mid}                          edit metadata (optimistic lock)
POST   /api/models/{mid}/refit                    refit (mints new version)
POST   /api/models/{mid}/versions/{vid}/submit-review  nominate checker; draft → in_review (global)
POST   /api/models/{mid}/versions/{vid}/release        su/admin release; checked → published (general)
POST   /api/models/{mid}/scope                    change scope (may enter approval)
POST   /api/models/{mid}/retire                   retire (no delete)
POST   /api/models/{mid}/export-as-tool           §7 synthesis + register (new ToolVersion lands in `test`)
GET    /api/models/{mid}/versions/{vid}/diagnostics  quality + scree + recon-error
GET    /api/models/{mid}/versions/{vid}/download   curator-only; bundle archive
GET    /api/models/picker?tool=                    picker query (§6.1)
GET    /api/models/approvers?scope=global          eligible approvers for the picker
POST   /api/models/{mid}/versions/{vid}/predict   one-off predict (debug; not a hot path — surrogate tools handle production)
```

The checker's approve / reject reuse the existing approvals endpoints with the directed-approver extension (`in_review → checked | rejected`); the su/admin **release** (`checked → published`) is the separate, always-required gate-2 step above.

### 11.3 Surrogate fitting from a study (existing parametric flow)

The existing parametric-studies surrogate endpoint (`POST /api/studies/{sid}/surrogate`) stays — it fits an *unpersisted* in-session surrogate for the Results explorer's Prediction Profiler, exactly as today. The new "Save to Model registry" affordance on that surface calls `POST /api/models` under the hood, passing the in-session surrogate's parameters; the registry's `create_model` re-fits to ensure determinism (bundle bytes match the persisted parameters).

---

## 12. UI surfaces

| Surface | File | Change |
|---|---|---|
| Studies workspace | `ui/pages/studies.py` | New **Cohorts** tab beside Parametric and Optimisation (per `designs/studies-engine.md` §9) |
| Cohort detail | `ui/pages/cohorts.py` (new) | Members list, filter editor, fit-a-surrogate affordance, archive |
| Model catalogue | `ui/pages/models.py` (new) | Browse by scope / tool / tag; create from cohort/study; refit-available badges |
| Model detail | `ui/pages/models.py` `/models/{id}` | Version history with side-by-side quality metrics; per-version diagnostics; scope-change dialog; export-as-tool; retire; refit |
| Field viewer | `ui/_field_surrogate_viewer.py` (new) | Coloured-mesh `ui.scene` + scree + reconstruction-error diagnostic |
| Submit form | `ui/pages/job.py` form widget | Training-data coverage gauge for surrogate-tools; "Find / Refit surrogate" affordance (admin/curator only) on any tool's form |
| Approvals inbox | existing approvals UI | Pending global model versions appear for the designated approver |
| Lineage view | `ui/_cytoscape.py` | New `SurrogateModel` node kind + per-version selector inside the model node |
| Admin lineage | `ui/pages/admin/lineage.py` | "Surrogate model" added to the picker kinds |
| Nav | `ui/_chrome.py` | Feature-gated "Models" entry |

UI gating uses the resolved permission so curate / publish / refit controls are hidden, not just server-rejected.

---

## 13. Audit

Per the `designs/studies-engine.md` §8 namespace:

| Event | When | Payload (notable) |
|---|---|---|
| `study.cohort.created` | `CohortService.create_cohort` | cohort id, tool, tolerated versions, filter summary |
| `study.cohort.query_edited` | `update_cohort` | cohort id, diff summary |
| `study.cohort.archived` | `archive_cohort` | cohort id |
| `study.cohort.surrogate_attached` | `attach_surrogate` | cohort id, model id |
| `surrogate.fit_started` | enter `create_model` / `refit` | source kind/id, kind, factor count, n points |
| `surrogate.fit_completed` | leave the same path | model id, version id, quality summary, wall-clock |
| `surrogate.fit_failed` | exception during fit | exception class, summary |
| `models.model.create` / `update` / `version.add` / `scope.change` / `retire` / `check_approve` / `check_reject` / `release` / `release_decline` | `ModelService` mutations | actor, model id, version id, diff |
| `models.exported_as_tool` | `register_as_tool` | model id, version id, new tool alias + version id |
| `models.bundle.corrupt` | `load_for_runtime` sha mismatch | bundle path, expected vs actual sha |
| `job.surrogate.use` | `JobService.queue_for_runner` for a surrogate-tool job | job id, model version id |

No raw simulation data goes into audit payloads — quality coefficients and best-found summaries are fine, but actual snapshot blobs are referenced by job id only.

---

## 14. Permissions, licence, feature flag

- **Permission**: new `MODELS_CURATE` verb in the feature matrix; granted via the new `model_curator` capability role (ADR-0005 pattern). Defaults to **off** for `Regular user`; `Admin` / `Super-user` may grant; `Auditor` never. The verb gates: global publish, scope demote/promote across the global boundary, refit of a global model, bundle download.
- **Cohort creation**: project-ACL-gated only (Member+ on the cohort's home project) — no new top-level permission. Cross-project cohorts require Viewer+ on every referenced project (validated at create + at every member resolution).
- **Surrogate-tool execution**: the synthesised tool inherits the standard `tool.run` permission ladder. The end user does **not** need `MODELS_CURATE` to *run* a surrogate-tool — only to *publish* one.
- **Licence (§C16)**: no new models may be created, refitted, or published in licence read-only mode; viewing existing models and running surrogate-tool predictions is allowed (a fitted surrogate is consumable just like any other tool).
- **Feature flag**: `features.surrogate_family` (404-when-off pattern). Off ⇒ nav entries, all routes, the Cohorts studies tab, the field-output `OutputField.kind='field'` validator, and the synthesis pipeline all absent. The lineage SurrogateModel node kind is gated by the same flag (`LineageService._resolve_surrogate_model` returns "not found" when off).

---

## 15. Dependency & licence ledger

Project-wide rule: no paid/restrictive licences; air-gap-bundled; no Node.

| Dep | Use | Licence | Status |
|---|---|---|---|
| `pyvista` | VTK/CGNS/Ensight reading + `ui.scene` mesh marshal | MIT | **already in stack** (per ARCHITECTURE §16.9 the CAD viewer uses it) |
| `meshio` | extra format support beside `pyvista` | MIT | **new — vendor + SBOM** |
| `numpy` / `scipy` | SVD, GP backend, modal arithmetic | BSD-3 | already in stack |
| `scikit-learn` | scalar GP + polynomial surrogates (existing) | BSD-3 | already in stack |
| `joblib` | model serialisation | BSD-3 | already in stack (sklearn pulls it) |
| `pyarrow` | training-data parquet (when `keep_training_data=true`) | Apache-2 | **new — vendor + SBOM** |

`pyvista` is already a dependency for the CAD geometry viewer; if it isn't pulled into the runtime by default it gets promoted from "viewer-conditional" to "core" by this ADR. `meshio` and `pyarrow` are the only net-new deps; both are permissive and already-known-clean by the SBOM allow-list lint (H41). Total bundle bump ~25 MB (`meshio` ~5 MB, `pyarrow` ~20 MB).

SBOM regen + hashed-lockfile bump is a maintainer step before the first release that includes this ADR's code (same discipline ADRs 0001 / 0006 / 0007 used).

`scipy.sparse.linalg.svds` is used for the truncated SVD path on large meshes (>50_000 nodes) — already in scipy, no extra dep.

---

## 16. Non-negotiables (build these in from the start, not retrofitted)

1. **Cohort membership is computed live, never materialised.** A job-completion event never writes to a cohort. The lazy `study_result` populate (§3.3) is the only write triggered by cohort use.
2. **Bundle integrity is verified on every load.** `bundle_sha256` mismatch raises `ModelBundleCorruptError`; the runner never returns predictions from a corrupt bundle.
3. **`pickle.load` is never called outside a `joblib` call site.** Bundle layout (§5.3) limits the deserialisation surface; raw pickle in the manifest fails the load.
4. **Surrogate-tool jobs carry `Job.surrogate_model_version_id`.** Without this column populated, lineage (ADR-0010) cannot draw the surrogate edge.
5. **Field outputs are unconditionally retained.** The retention floor (ADR-0003) extends to descriptor-declared `field` outputs; the surrogate fitter never depends on `retain_files`.
6. **Tool-version tolerance is curator-explicit, never inferred.** Adding a tolerated version to a cohort surfaces a plain-English warning when the descriptors diverge.
7. **Global release is two-gate and segregated.** A directed (one nominated) technical checker — toggleable — then a *mandatory* su/admin release; the builder is never the checker or the releaser (no self-anything). Personal and project scope have no gate. Per-tool curation is deferred (§5.7); domain targeting is by checker nomination.
8. **Coverage gauge is advisory by default; `block_extrapolation` is opt-in.** A surrogate tells the user where it's confident; it doesn't refuse extrapolation unless the model author opted in (and even then, only by tightening the form's bounds, not by adding a new rejection path).
9. **No `pickle.load` of the model bundle in any web-process path.** Bundle reads happen in the runner-subprocess context (the synthesised orchestrator runs there), never in the FastAPI process directly.
10. **Existing scalar surrogate users see no behavioural change.** The parametric Results explorer's Prediction Profiler, set-to-optimum optimiser, contour profiler, and Sobol sensitivity remain identical from the user's perspective — the engine generalises beneath them.

---

## 17. Phased build sequence (within ROADMAP Phase 2)

Per house style (pause for review after the design doc + after each phase). Phases 1 and 2 land the `study_result` rename + the cohort sibling — the shared foundation ADR-0009 also consumes. Phases 3–8 build the surrogate family proper. ADR-0009's phases interleave: this doc's phase 5 (Model registry) is the gate for ADR-0009's `surrogate_assisted_real` run mode.

- **Phase 1 — `study_result` rename + `source_kind` discriminator.** Single Alembic migration: rename `parametric_study_result` → `study_result`, add `source_kind` + `source_id`, backfill `('parametric', parametric_study_id)`. Update consumers (Results explorer, surrogate ingestion, KPI extraction's read path, lineage `_list_study_results`). Drop the old `parametric_study_id` column in the next migration **after** all consumers ship. Both DB engines.
- **Phase 2 — `run_cohort` table + `CohortService` + cohorts tab.** Migration; ORM; service (create / update / resolve / populate / archive / list / get); cross-project ACL intersect; lazy `study_result` populate; `/studies` Cohorts tab; cohort detail page with the existing Results explorer mounted via `(source_kind='cohort', source_id=cohort.id)`. Tests cover ACL-hide, lazy populate, tolerated-version-list warnings.
- **Phase 3 — Surrogate engine refactor (source-agnostic ingestion).** New `matofx.studies.surrogate.ingest` module; new `collect_training_data(source_kind, source_id, response_spec, factor_filter)` returning the same shape `fit_surrogate` expects today. Parametric Studies flow unchanged. Tests confirm parametric-only callers behave identically; cohort callers fit successfully.
- **Phase 4 — Field-surrogate engine.** `OutputField.kind='field'` descriptor extension; retention-floor extension; `fit_field_surrogate` + `FieldSurrogate.predict` + per-mode scalar surrogates; ingestion for VTK / CGNS / Ensight / `.npy`; common-mesh check; opt-in `field_remap_reference_mesh` via the descriptor; streaming-SVD path for large meshes. Predicted-field viewer NiceGUI component + scree + reconstruction-error diagnostic. Tests cover one VTK + one CGNS + one Ensight + one `.npy` ensemble end-to-end.
- **Phase 5 — Model registry (data model + governance).** Migration: `surrogate_model` + `surrogate_model_version` + `Job.surrogate_model_version_id` + `approval_request` target widening + `MODELS_CURATE` + `model_curator` seed. `ModelService` (create / refit / update / scope / publish / retire / picker / open_bundle / load_for_runtime). General Setting `models_global_publish_approval`. Bundle layout + sha256 verification. Tests across personal / project / global + approval-gate paths. **Gate for ADR-0009's `surrogate_assisted_real` run mode.**
- **Phase 6 — Surrogate-as-tool synthesis.** `matofx.models.tool_synthesis`: descriptor synthesis, orchestrator synthesis, `register_as_tool` integration with the existing `ToolService.install`. In-form coverage gauge; `block_extrapolation` mode. Per-response `<response>_std` outputs for GP-backed models. `Job.surrogate_model_version_id` write at submit. Tests cover scalar + field tools, both with and without `block_extrapolation`.
- **Phase 7 — Refit lifecycle + UI.** §8 triggers; refit-available badging; `/models` catalogue + `/models/{id}` detail + scope-change dialog + retire dialog + export-as-tool dialog. Diagnostics page (per-version side-by-side quality). Find/refit affordance on the per-tool submit page (admin/curator only).
- **Phase 8 — Cross-ADR integration + docs.** Lineage `SurrogateModel` node kind + `PREDICTED_BY` edge + admin-picker entry; V&V `vv_surrogate` placeholder hook (reserved by ADR-0008 §10; full implementation deferred); KPI extraction read-path validation (no code change expected — surrogate-tool outputs flow through identically). `ARCHITECTURE.md` §16.26 as-built note; `ROADMAP.md` tick; package READMEs. SBOM regen + lockfile bump.

Gates between phases: `ruff format --check . && ruff check . && mypy && pytest`, both DB engines, plus a feature-flag-on/off smoke test for the synthesis pipeline.

---

## 18. Explicitly out of scope for v1.0

- **Online surrogate learning** — every refit is a full retrain.
- **Multi-output co-kriging** — per-response surrogates only.
- **Surrogate-tool dynamic re-fit mid-workflow-run** — the orchestrator loads a fixed version.
- **Tool-author SDK access to the surrogate engine** — a hand-written tool cannot programmatically fit a surrogate from inside its orchestrator. (Promoting this needs an SDK ADR.)
- **Field surrogates for Abaqus `.odb` / ANSYS `.rst`** — convert externally for v1.0.
- **Automatic mesh remap** — common-mesh assumption holds; opt-in descriptor flag covers the manual case.
- **Auto-publish of an updated surrogate when an optimisation converges** — ADR-0009 flag, not a feature this ADR ships.
- **ParaView-style scientific visualisation** of the predicted field — coloured-mesh viewer only. Volume rendering / streamlines / animated time-series stay v1.x (ROADMAP).
- **Surrogate-of-a-surrogate** — rejected at study creation with a plain-English message.

All recorded in ROADMAP v1.x.

---

## 19. Open questions / revisit triggers

- **Default `mode_energy_cutoff` (0.99)** for field surrogates — confirm with the first pilot's CFD/FEA data. Some flow problems need 0.999+; some structural problems are happy at 0.95.
- **Default `surrogate_field_fit_memory_gb` (4)** — confirm with pilot hardware. The streaming SVD path is the safety valve; tightening the default forces it on more often.
- **`keep_training_data` default** (currently `false` to keep bundles small) — if customers consistently want the (X, y) embedded for retrospective re-fits in external tools, flip the default.
- **Whether scope demotion from `global` should require approval** (currently admin-override-only with audit) — if pilot governance reviews flag this as too loose, add a `models_global_demote_approval` toggle.
- **Whether the synthesised tool's `cpu-fast` queue is universally correct** — milliseconds for scalar; field reconstruction over a 100k-node mesh can be 100s of milliseconds. Probably fine for `cpu-fast`; revisit if surrogate-tools start saturating it.
- **Per-version side-by-side diagnostics layout** — two columns vs N columns vs a "compare two" picker. Decide with first-pilot UX.
- **Whether `LibraryService.list_consumers` / `MaterialsService.list_consumers` extensions** (currently v1.x carry-overs per ADR-0010) should land in this ADR to enable "models that depend on this library file / material version" reverse lookups in the registry UI. Cheap to add later; deferred.

---

## 20. Revision history

- **2026-05-29 — design locked.** Initial draft alongside `designs/optimisation-framework.md`. Companion to ADR-0008 (ruled 2026-05-26 at full v1.0 scope). Shared substrate `designs/studies-engine.md` locked 2026-05-26. The `study_result` rename + cohort table + Model registry land in this doc's first five phases; ADR-0009's `surrogate_assisted_real` mode hard-depends on Phase 5 of this build.
- **2026-06-01 — governance amended.** Architect ruling on the Model-registry release flow: per-tool curation deferred to v1.x (install-global `model_curator` + checker nomination cover v1.0, §5.7); global / general use is a two-gate flow — toggleable technical **checker** then a **mandatory su/admin release** — with the builder excluded from both (§6.2–§6.4); personal and project scope are ungated. Surrogate-as-tool export is explicit/opt-in, auto-synthesised into a `test` `ToolVersion` whose safe surface the su/admin edits before promoting `test → released` (which is the release gate); editable-field edits carry forward on refit re-export (§7.6). Touched: §1 scope table, §5.7, §6.2–§6.4, §7.6 (new), §10.2–§10.3, §11.2, §13, §16 (#7).
- **2026-06-08 — field path shipped (Phase 4), incl. time-resolved.** The §4 field-surrogate path landed end-to-end: deps vendored (`pyvista`/`meshio`/`pyarrow`); the descriptor `field` output kind + retention floor (§4.4); `collect_field_training_data` ingestion with the **time-resolved `(run, timestep)` extension** (the 2026-06-07 requirement — `time` is a reserved factor; `predict({…, 'time': t})` reconstructs any step); the governed `kind='field'` POD bundle + `ModelService` create/refit/`field_predict` + the `field-predict` API; and the predicted-field viewer (`ui/_field_surrogate_viewer.py`) with a **time slider + play/pause**, value-vs-node trace + 1-sigma band, fixed-range toggle, and scree / reconstruction-RMSE / mode-coefficient panels. **No migration** (the `kind` CHECK already permitted `field`; field metadata rides in `responses` JSON + the bundle manifest). **Implementation deltas vs this design:** (a) the field output is `type='file'` + `file_type='field'` (matching the as-built descriptor) rather than a new `OutputField.kind`; (b) the viewer ships the §4.5 scree / reconstruction / uncertainty panels, the value-vs-node field trace, **and the 3-D coloured-mesh surface** — implemented as a Plotly `Mesh3d` (viridis, per-node intensity) rather than `ui.scene`, fed by a `field-geometry` endpoint that extracts the reference mesh's surface triangulation server-side (`extract_surface` → `vtkOriginalPointIds` aligned to the full node set). `.npy` fields (no geometry) fall back to the value-vs-node view, the universal path. The dep-resolution for `pyvista`/`meshio`/`pyarrow` was validated locally; the canonical hashed `requirements.lock` remains the maintainer's Linux `workflow_dispatch` (`lockfile.yml`).
