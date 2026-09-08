# Studies Engine — shared foundation for ADR-0008 + ADR-0009

Status: Locked 2026-05-26.
Companions: `decisions/0001-parametric-studies.md` (the existing parametric-study feature this generalises), `decisions/0008-surrogate-family.md`, `decisions/0009-optimisation-framework.md`.

This document describes the **shared substrate** that the surrogate family (ADR-0008) and the optimisation framework (ADR-0009) both build on. It exists so the two ADRs reference one consistent design rather than diverging on data shape, ACL, or runtime mechanics. The build-level specs for each ADR (`designs/surrogate-family.md`, `designs/optimisation-framework.md`) land alongside the code; this doc is the *architectural skeleton* both build on.

---

## 1. The three siblings under one roof

The v2 hierarchy gains a unified "dataset / experiment" branch:

```
Project ─┬─ Scenario ─── Job / WorkflowRun
         └─ Study ─── Job (study children)
                 │
                 ├─ ParametricStudy   (designed, one-shot batch — existing, ADR-0001)
                 ├─ OptimisationStudy (adaptive, sequential — new, ADR-0009)
                 └─ RunCohort         (opportunistic, gathered post-hoc — new, ADR-0008)
```

All three are **siblings of `Scenario`** under `Project`, all three carry the same hierarchy semantics (project ACL inheritance, no-delete archive, audit), and all three feed the same downstream surfaces (Results explorer, Surrogate engine, Compare).

The existing `parametric_study` table stays as-is. The two new tables — `optimisation_study` and `run_cohort` — adopt the same column conventions:

| Common columns | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `project_id` | FK → `project.id` | ACL via ADR-0002 |
| `tool_alias` | TEXT | The single tool the study/cohort is keyed on. Cross-tool cohorts are v1.x |
| `tool_version_id` | FK → `tool_version.id` | Pinned at creation; mid-study version drift is rejected |
| `name` | TEXT NOT NULL | Human label |
| `description` | TEXT | Optional |
| `state` | TEXT (CHECK) | See state-machine table below |
| `state_changed_at` | TIMESTAMP | |
| `state_changed_by` | FK → `user.id` | |
| `created_at`, `created_by_user_id` | | Standard audit columns |
| `archived_at`, `archived_by_user_id` | nullable | No-delete pattern (D20 + ADR-0002) |

**Why a unified branch and not three independent ones?** Because Compare, the Results explorer, the surrogate engine, lineage (ADR-0010), and KPI extraction (ADR-0012) all care about "an ordered set of completed jobs of one tool". The three sibling sources differ only in *how they came to be that set*. One downstream consumer, three upstream producers.

The `Study` parent is not a single table — there is **no `study` row** that all three FK into. Each sibling owns its row; the union is logical (resolved by an enum/discriminator at query time). This avoids the failure mode of "phantom study rows for cohort entries" and the join cost on every dataset query.

---

## 2. The state machine (per sibling)

Parametric studies already use `created → running → complete | partial | failed | cancelled` (ADR-0001). The two new siblings adopt **compatible** state vocabularies with one addition each:

**`optimisation_study.state`**

```
draft ─configure─▶ [pending_start_approval] ─approved─▶ running ─┬─ converged ──▶ complete
        (gate skipped unless a target        ╰─rejected─▶ cancelled │ ├─ budget_exhausted ──▶ complete
         tool requires start-approval)                              │ ├─ target_met ──▶ complete
                                                                    │ ├─ user_halted ──▶ cancelled
                                                                    └─┴─ failed (every child fails) ──▶ failed
```

`draft` exists because configuring an optimisation study is multi-step (target → factors → responses → objectives → constraints → stopping criteria → strategy) and the user may want to save partial progress (per [[feedback_save_db_only]] — DB-only, no disk staging until run). A per-tool **start-approval** gate (ADR-0009 / `designs/optimisation-framework.md` §14.1) inserts an optional `pending_start_approval` state between `draft` and `running` when the target tool — or any tool in a workflow target — requires it. Optimisation studies have a richer state because they accumulate state during the run (best-so-far, convergence trace, GP/sampler state).

**`run_cohort.state`**

```
active ──archive──▶ archived
```

A cohort is just a saved query — the rows it selects are computed live each time it is read (the query: `Job` where `tool_version_id` in the pinned versions AND `state='complete'` AND additional filters). There is no "running" state because a cohort doesn't run anything; it organises completed jobs. **However**, a cohort may have an *attached surrogate* (`Surrogate` row referencing it), and that surrogate has its own fit lifecycle (see ADR-0008 §3).

---

## 3. Membership: how jobs attach to each sibling

The existing `Job.parametric_study_id` FK established the pattern. Two new FKs join it:

| Column | Nullable | Semantics |
|---|---|---|
| `Job.scenario_id` | yes | The ad-hoc / workflow-child home. Existing |
| `Job.parametric_study_id` | yes | The parametric-study home. Existing |
| `Job.optimisation_study_id` | yes (new, ADR-0009) | The optimisation-study home (tool target) |
| `WorkflowRun.optimisation_study_id` | yes (new, ADR-0009) | The optimisation-study home for a **workflow-target** study (`designs/optimisation-framework.md` §3.4) — one `WorkflowRun` per candidate |

Run cohorts **do not** add a `Job` FK. A cohort is a *set query*, not a parent relationship. Cohort membership is computed each time the cohort is read — this means a cohort grows automatically as new matching jobs land, which is exactly what the user wants from "build a surrogate from past runs that keeps refreshing as more arrive".

The existing portable CHECK on `Job` (one of `scenario_id` / `parametric_study_id` must be non-null) widens to **one of three** non-null (also including `optimisation_study_id`). Same migration mechanics as the file-library / materials approval-target widenings — bumping the CHECK from 1-of-N to 1-of-(N+1).

A **workflow-target** optimisation (ADR-0009 / `designs/optimisation-framework.md` §3.4) carries its membership on `WorkflowRun.optimisation_study_id` instead; its child `Job` rows keep their normal scenario/run home, so they add no pressure to this Job CHECK.

The `UNIQUE(scenario_id, tool_alias)` constraint (migration 0017) keeps treating NULL `scenario_id` as distinct (per ADR-0001's existing analysis), so optimisation-study children — which set `scenario_id = NULL` — never collide with each other or with parametric-study children.

---

## 4. Snapshotting: what gets pinned, when

Every study sibling pins the **tool version** at creation, exactly as parametric studies already do. The descriptor snapshot is frozen onto the row at creation; child jobs inherit it; a mid-study tool update can't split results across versions.

New pinned references for v1.0:

| Pin | Set when | Purpose |
|---|---|---|
| `tool_version_id` | Study creation | (existing) Ensures all children speak the same descriptor |
| `library_file_version_ids[]` | First child queue | Captures the library files used (frozen in `inputs.json` per ADR-0006) |
| `material_version_ids[]` | First child queue | Captures the materials used (frozen in `inputs.json` per ADR-0007) |
| `surrogate_model_version_id` | First child queue, if any child uses a surrogate-tool | Pins which surrogate version the study uses (ADR-0008) |

These pins are written to a study-level `pinned_refs_json` column for v1.0 (compact, queryable via JSON1 in SQLite and `jsonb_path_query` in Postgres). If a future need emerges to FK each explicitly, a migration can split them out without a data shape change.

---

## 5. Pickup, fairness, and lanes

The runner already supports a **lower-priority study lane** (ADR-0001) so a large study can't starve interactive jobs. Optimisation studies use the same lane.

One subtlety for optimisation: the lane needs **batch coordination** — an optimisation step submits K candidates and must wait for K to complete before deciding the next batch. The lane scheduler is unchanged; the *study runtime* (a new `matofx.studies.optimisation.runtime` module per ADR-0009) holds the wait-for-batch logic outside the runner. It polls the database for child completion, decides next batch via the optimiser's `ask()`, queues the next children, repeats until a stop criterion fires.

**Crash recovery:** the optimisation-study runtime must be resumable. Its in-memory state (search history, GP, sampler) is serialised to `optimisation_study.optimiser_state_json` (or to disk under `<mount>/studies/<env>/opt_<id>/state.pkl` for heavier states) after every batch. On runner restart, the runtime rehydrates and continues from the last completed batch. Failure mode `optimiser_state_corrupt` raises a clear ops error rather than continuing with bad state.

---

## 6. Data flow into the Results explorer

The existing `parametric_study_result` table is **renamed `study_result`** and gains a discriminator:

| Column | Notes |
|---|---|
| `id` | PK |
| `source_kind` | TEXT CHECK in `('parametric', 'optimisation', 'cohort')` — new |
| `source_id` | INTEGER — the FK target depends on `source_kind` (logical FK, not a hard one — same pattern as approval targets) |
| `job_id` | FK → `Job.id` |
| `inputs_json` | The frozen inputs (post library/material resolution) |
| `outputs_json` | Pulled from the canonical `outputs.json` |
| `csv_plot_extract_json` | Per-output CSV summaries (the existing post-pre-cleanup write path, ADR-0003) |
| `written_at` | Pre-cleanup (ADR-0003) |

**Migration discipline.** The rename is one Alembic migration with an UPDATE backfilling `source_kind='parametric'` and `source_id = parametric_study_id`. The old column drops in the next migration after consumers are switched. Standard two-step pattern. The Results explorer (existing surface) reads from the unified `study_result` table via a `(source_kind, source_id)` filter — no API shape change for existing parametric users.

Cohorts populate `study_result` **lazily** the first time a cohort is read, then incrementally as the cohort's underlying query yields new jobs. The write triggers from the cohort read path (cohort service), not from job completion — because a job doesn't know which cohorts it belongs to.

---

## 7. ACL: who can see / mutate which

All three siblings inherit project ACL (ADR-0002):

- **View** — Viewer+ on the project sees the study/cohort and its child jobs (with the usual job ACL on the children — already enforced).
- **Edit** (configure, change strategy, change stopping criteria for an optimisation in progress) — Member+ on the project.
- **Halt / cancel** — Member+ on the project; admin override via `live_jobs.cancel` (ADR `16.18`).
- **Archive** — Creator or Owner on the project; never a hard delete.

Cohorts have one wrinkle: a cohort can reference jobs from **multiple projects** (a user with access to two related projects can build a cohort spanning both). The cohort's *home project* is the one it was created in; its member-resolution at read time intersects with the caller's per-project ACL. A user with access to project A but not B sees only A's jobs in a cross-project cohort — not an error, just a filtered view, with a "(N jobs hidden by access controls)" footnote.

**The home-project ACL governs edit/delete; the per-job ACL governs visibility of the contents.** This mirrors how Compare already handles cross-project scenarios.

---

## 8. Audit

Every state transition, configuration edit, child enqueue, cohort membership refresh, and surrogate fit/refit/publish is audited via the existing hash-chained writer (`matofx.audit`). Event names follow the prefix-by-namespace house style:

| Namespace | Examples |
|---|---|
| `study.parametric.*` | (existing) `study.parametric.created`, `study.parametric.child_queued`, … |
| `study.optimisation.*` | `study.optimisation.created`, `study.optimisation.start_approval_requested`, `study.optimisation.start_approved`, `study.optimisation.start_rejected`, `study.optimisation.batch_started`, `study.optimisation.batch_completed`, `study.optimisation.converged`, `study.optimisation.halted` |
| `study.cohort.*` | `study.cohort.created`, `study.cohort.query_edited`, `study.cohort.refit_triggered`, `study.cohort.archived` |
| `surrogate.*` | `surrogate.fit_started`, `surrogate.fit_completed`, `surrogate.published_as_tool`, `surrogate.refit`, `surrogate.use` |

No raw simulation data goes into audit payloads — sensitivity coefficients and best-found summaries are fine, but actual `outputs.json` blobs are referenced by job ID only.

---

## 9. UI surfaces — one explorer, three entry points

`/studies` becomes the unified landing surface with three tabs: **Parametric**, **Optimisation**, **Cohorts**. Each tab lists the user's accessible items, filterable by project / state / tool / created-after.

Drilling into any item lands on the existing Results explorer (`matofx.ui.pages.studies`), parameterised by `(source_kind, source_id)`. The explorer's tabs (matrix, summary, plot, sensitivity, ANOVA, RSM, surrogate, Pareto) all already operate on `study_result` rows — no per-source-kind branching needed except where it adds value (e.g. optimisation studies get an extra "Convergence" tab showing best-so-far vs iteration; cohorts get an extra "Coverage" tab showing input-space density).

The unified surface keeps the home Quick Access cards focused — instead of three separate launch points there's one "Studies" entry.

---

## 10. The surrogate engine — single source

The existing `matofx.studies.surrogate` module is the canonical fit/predict surface. ADR-0008 extends it (not duplicates it) with:

- **Field-output support** — POD/PCA modal decomposition before fitting per-mode scalar surrogates over the modal coefficients. Same `fit(X, y_modes)` / `predict(X)` shape; the modal vector is hidden inside the model object.
- **Source-agnostic ingestion** — a fit takes a `(source_kind, source_id)` and pulls the relevant `study_result` rows itself. Today's API signature `surrogate.fit(study_results: list[StudyResult], ...)` is unchanged — the dispatch layer reads from `study_result` regardless of who wrote the rows.
- **Persistence** — fitted models serialise to a versioned, governed `surrogate_model_version` row + an on-disk blob (per ADR-0008 §3). Exporting one as a tool is then a `surrogate_model_version_id → ToolVersion` rendering step.

The optimiser inside `studies.surrogate` (the existing differential-evolution-on-the-GP "set-to-optimum" feature) is extended in ADR-0009 into a fuller optimisation strategy registry — but the registered strategies all consume the same surrogate handle. One engine, two ADRs build on it.

---

## 11. What's deliberately not in this shared foundation

These belong in the per-ADR design docs, not here:

- The surrogate-as-tool **descriptor synthesis** (ADR-0008 §4): how the exported tool's `descriptor.toml` is generated from the fitted model.
- The **Model registry** governance (ADR-0008 §5): scope tiers, capability, approver gate.
- The optimisation-study **strategy registry** (ADR-0009 §3): BO / NSGA-II / Hyperband / active-learning entries.
- The optimisation-study **constraint syntax** (ADR-0009 §5): reuse of `cross_field_constraints` and `safe_expr`.
- The optimisation-study **stopping-criterion DSL** (ADR-0009 §6).

Cross-references between ADRs are expected to flow through this document where they touch shared substrate, and through `designs/parametric-studies.md` where they touch the existing parametric-study mechanics.

---

## Revision history

- **2026-05-26** — initial draft locked alongside ADRs 0008 + 0009 (architect ruling).
- **2026-06-01** — amended for the ADR-0009 scope expansion: `optimisation_study.state` gains the optional `pending_start_approval` gate (§2); a new `WorkflowRun.optimisation_study_id` FK carries workflow-target membership (§3); `study.optimisation.*` audit namespace gains the start-approval events (§8). Detail in `designs/optimisation-framework.md` §3.4 / §14.1.
