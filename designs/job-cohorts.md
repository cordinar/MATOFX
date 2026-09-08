# Design — Job Cohorts (rework of "Run Cohorts")

Status: **Design drafted 2026-06-11 — pending build.** Supersedes the cohort portions of `designs/surrogate-family.md` §3 (the project-homed, query-only "run cohort"). Companion to ADR-0008; governance follows `decisions/0005-multi-role-capabilities.md` and the Materials/Library precedents (`decisions/0006`, `decisions/0007`).

This records *what to build*. The surrogate engine that consumes cohorts is unchanged — only how a cohort is named, scoped, governed, and assembled changes.

---

## 1. Why

The shipped feature (table `run_cohort`, migration 0059) has two limitations the maintainer hit in use:

1. **Single project.** The create form (`ui/pages/cohorts.py` `_render_create_form`) asks for one project + one tool. You cannot gather runs of the same tool from across projects, even though the *data model already supports it* (`project_ids` JSON column, unused by the UI). The single-**tool** constraint is correct and stays; the single-**project** constraint is wrong.
2. **No real assembly UX.** Membership is a saved query, but the only exposed filters are project + tool. The richer filters that exist in the schema (`input_ranges`, `cross_field_predicate`, time window, material/library versions) are never surfaced, and there is no way to browse the matching runs and pick.

A third, latent bug blocks the headline use case regardless of UI: workflow-step jobs and optimisation-evaluation jobs are **silently excluded** from membership (see §4).

The critical key is the **tool version**, not the project. The rework makes the tool (+ tolerated versions) the anchor and searches the whole workspace for matching runs.

---

## 2. Decisions (locked 2026-06-11)

| # | Decision | Choice |
|---|---|---|
| D1 | Rename | `Run Cohorts` → `Job Cohorts` **everywhere**, including the DB table (`run_cohort` → `job_cohort`) via Alembic. |
| D2 | Anchor | Single **tool** (unchanged) + one or more **tolerated tool versions** (default = latest at create; divergence-warned). |
| D3 | Search scope | **Workspace-wide**, across **all four job sources**: standalone scenario jobs, parametric-study children, workflow-step jobs, optimisation evaluations. |
| D4 | Membership model | Filter-driven candidate search → live **preview** → per-run curate (tick/untick) → **auto-grow switch**: OFF freezes the chosen job IDs on save (static set); ON re-resolves the filters live so new matches join (feeds active learning). |
| D5 | Object scope | **Workspace-level** object (no mandatory home project), modelled on Materials/Library. Owned by `created_by_user_id`. |
| D6 | Curation rights | New capability permission **`COHORTS_CURATE`**, granted via a dedicated **`cohort_curator`** capability role, gates create/edit/archive; reads open to any authenticated user (same as materials). |
| D7 | Result ACL | Member runs from projects the viewer cannot access are **hidden and counted** (existing `acl_hidden` behaviour). A cohort never launders run data out of a restricted project. |

Feature flag unchanged: the whole surface stays behind `features.surrogate_family`.

---

## 3. Assembly flow (the new builder)

1. **Tool** (required) — picked workspace-wide, not project-scoped.
2. **Tolerated version(s)** — default to the tool's latest version; curator can add more, with the existing descriptor-divergence warnings (`service.tolerated_version_warnings`). Equivalence is warned, never inferred.
3. **Filters** (all optional, narrow the candidate pool) — exposed in the UI for the first time:
   - `input_ranges` — per numeric input min/max
   - `completed_after` / `completed_before` — time window
   - `owner_user_ids`, `scenario_ids`
   - `material_version_ids`, `library_file_version_ids`
   - `cross_field_predicate` — advanced `safe_expr` DSL
   - `project_ids` — optional **positive** filter ("restrict to these projects"); empty = whole workspace.
4. **Preview** — a live table of matching completed runs. Columns: project · source (standalone / parametric / workflow / optimisation) · tool version · completed · key inputs. Labels not internal names ([[feedback_use_labels_not_names]]). Result ACL applies (D7) — hidden runs surface only as a count.
5. **Curate** — per-row tick/untick.
6. **Auto-grow toggle** (D4) — OFF: freeze ticked job IDs on save. ON: store the filters; re-resolve live each read; ticked-off rows persist as an exclusion list.
7. **Save** → cohort detail page.

---

## 4. The cross-source fix (mandatory)

A `Job` has exactly one **home**, enforced by the `ck_job_exactly_one_home` **1-of-3** CHECK (migration 0066, `hierarchy/orm.py`): `scenario_id` / `parametric_study_id` / `optimisation_study_id`. `workflow_run_id` is a *separate tag*, not a home — **workflow-step children are homed under `scenario_id`** (with `workflow_run_id` set as well). The canonical resolver is `hierarchy/service._job_project_id`.

Today `service._select_candidates` `outerjoin`s `Scenario` and `ParametricStudy` and resolves project via `COALESCE(sc.project_id, ps.project_id)`. Consequences:
- Standalone scenario jobs ✅ (scenario join)
- **Workflow-step jobs ✅ already** — they carry `scenario_id`, so the scenario join picks them up. (The first audit wrongly claimed these were dropped.)
- Parametric-study children ✅ (parametric-study join)
- **Optimisation-study jobs ❌ dropped** — `scenario_id` and `parametric_study_id` are both NULL, so `pid` is NULL and the `pid.in_(...)` clause filters them out.

Fix is therefore narrow: add an `OptimisationStudy` outer-join and extend the COALESCE to `COALESCE(sc.project_id, ps.project_id, os.project_id)`. Mirrors `_job_project_id` exactly. This is the only source-completeness gap; the headline "tools in workflows, anywhere" need is otherwise blocked solely by the single-project UI limit (later slices).

---

## 5. Schema changes

Migration (next free revision) on table renamed `run_cohort` → `job_cohort`:

- **Rename** table + indexes (`ix_run_cohort_*` → `ix_job_cohort_*`).
- **Drop** the mandatory home `project_id` (and its FK/index). `project_ids` is retained as the optional positive project filter (§3.3).
- **Add** `auto_grow` BOOLEAN NOT NULL DEFAULT false.
- **Add** `member_job_ids` JSON NULL — frozen membership snapshot when `auto_grow` is false.
- **Add** `excluded_job_ids` JSON NULL — manual exclusions when `auto_grow` is true.
- Keep `tool_alias`, `tolerated_tool_version_ids`, all filter columns, `state`, `last_resolved_at`, `created_by_user_id`, optimistic-lock `version`.

**Backfill existing rows** (all are query-only, project-homed today): set `auto_grow = true`, `member_job_ids = NULL`; if `project_ids` is NULL, set `project_ids = [old project_id]` to preserve current scope; otherwise leave as-is. No cohort loses members.

`study_result.source_kind = 'cohort'` value is unchanged.

---

## 6. Governance

- Add `Permission.COHORTS_CURATE` to `users/permissions.py` (a `.curate` capability, so `is_capability_permission` treats it as additive). Grant it via a **dedicated `cohort_curator` capability role** (locked 2026-06-11), alongside the existing curator roles; no built-in grant beyond super_user, matching materials/models.
- Service/router writes switch from `_require_project_access(...)` to `user_has_permission(session, actor_user_id, Permission.COHORTS_CURATE)`.
- Reads open to any authenticated user.
- **Per-project result ACL stays.** `resolve_members` already intersects with `ProjectService.list_for_user` and counts `acl_hidden`; this is retained verbatim (D7). The cohort object being workspace-visible does not widen visibility of the underlying runs.

General Settings: `COHORTS_CURATE` needs a grantable surface in the roles/capabilities admin (same place materials/library curators are assigned).

---

## 7. Layer-by-layer touch list

- **ORM** `studies/cohort/orm.py` — `RunCohort` → `JobCohort`; new columns; drop home `project_id`.
- **Service** `studies/cohort/service.py` — `CohortService` write-auth → `COHORTS_CURATE`; `_select_candidates` four-home project resolution; `create`/`update` snapshot-vs-live per `auto_grow`; exclusion handling.
- **API** `api/routers/cohorts.py` + `api/schemas.py` — list/create move from `/api/projects/{id}/cohorts` to workspace-level `/api/cohorts`; request schema drops required `project_id`, adds `auto_grow` + curation; preview endpoint for the builder.
- **UI** `ui/pages/cohorts.py` + `ui/_features.py` — new tool-first builder with filters + preview + curate + toggle; workspace-level list; detail page; rename strings.
- **Permissions** `users/permissions.py`, `users/roles.py` — add `COHORTS_CURATE` + grant path.
- **Migration** — rename + columns + backfill (§5).
- **Docs (ship-with-change):** `/help` user chapter (rename + new builder), `ARCHITECTURE.md` §16, `ROADMAP.md`, and this design doc supersedes `surrogate-family.md` §3 (cross-link).

---

## 8. Suggested build order

1. ✅ **Rename only** (mechanical, isolated, tests green): table + identifiers + strings + help text. **Shipped 2026-06-11** — `RunCohort`→`JobCohort`, table `run_cohort`→`job_cohort` (migration 0073, indexes renamed, constraint names preserved per the 0058 precedent), lineage `NodeKind.RUN_COHORT`→`JOB_COHORT` (reserved/unused), nav + page + help-doc strings. Route `/cohorts` unchanged (the noun "cohort" is unchanged; the workspace-level route move is step 6). Verified: alembic up/down roundtrip, cohort-service + lineage-resolver suites, ruff, mypy.
2. ✅ **Cross-source fix** (§4) — **Shipped 2026-06-11** alongside step 1. Added the `OptimisationStudy` outer-join to `_select_candidates` so optimisation-study jobs resolve their project (the only source actually dropped; workflow jobs already resolved via their scenario home).
3. ✅ **Schema** (§5) — **Shipped 2026-06-11** (migration 0074). Added `auto_grow` (NOT NULL, backfilled `true` for existing rows), `member_job_ids`, `excluded_job_ids` to `job_cohort`. **Sequencing note:** the home `project_id` is **not** dropped here — its consumers (the project-scoped service list, the `/api/projects/{id}/cohorts` routes, the UI project picker) still exist, so it stays as a now-non-ACL *origin* field and is dropped in the API/UI slice that removes those consumers. No `project_ids` backfill needed yet for the same reason (resolve still defaults to the origin project).
4. ✅ **Governance** (§6) — **Shipped 2026-06-11** (migration 0074). Added `Permission.COHORTS_CURATE` (`cohorts.curate`) + description, seeded the built-in **`cohort_curator`** capability role (granted to administrator; super_user via sentinel). Service writes (create/edit/archive) now gate on `cohorts.curate` (raise `AccessDeniedError`); reads (get/list/resolve) are open to any authenticated caller; the per-project result-ACL intersect is unchanged (D7). UI gates the create form + archive button on the capability. `auto_grow` surfaced on `CohortResponse`; create forces it `True` (live) until the membership slice. Verified: alembic roundtrip, cohort-service suite (incl. new curate-gating + open-read tests), permission-catalog completeness, ruff, mypy.
5. ✅ **Membership model** (D4) — **Shipped 2026-06-11** (slice 3). `resolve_members` branches on `auto_grow`: live cohorts run the workspace-wide §3.1 query and drop `excluded_job_ids`; fixed cohorts return the frozen `member_job_ids`. Member picks validated against the live candidate universe (completed · tool · tolerated version · visible). The candidate query was parameterised (`_CandidateFilter`) and now returns source + version + completion metadata.
6. ✅ **API** (slice 3) — **Shipped 2026-06-11**. Migration **0075** drops the home `project_id` (backfilling `project_ids=[project_id]` first to preserve scope) → cohorts are workspace-level. Routes moved from `/api/projects/{id}/cohorts` to `/api/cohorts` (list/create); added `POST /api/cohorts/preview`. Schemas gained `auto_grow`/`member_job_ids`/`excluded_job_ids` and the preview shapes; `project_id` dropped from the response.
7. ✅ **UI builder** (slice 3) — **Shipped 2026-06-11**. New flow: pick tool → **Preview matching runs** (workspace-wide table: run · project · source · completed, ACL-hidden count) → tick a fixed set or leave **auto-grow** on → save. Detail page shows live-vs-fixed + workspace/restricted scope. Curate-gated.
8. **Docs** — help updated (slices 1–3); ARCHITECTURE/ROADMAP refresh still due.

### Follow-ups
Built in a slice-3 addendum (**2026-06-11**):
- ✅ **Version picker (major.minor bands)** — new curate-gated `GET /api/cohorts/tools/{alias}/build-meta` returns `version_bands` (versions grouped by `major.minor`, patches folded in, newest first) + `numeric_inputs`. The builder offers the bands as a multi-select. **Default (2026-06-30): all installed bands** — a new cohort tolerates every version line so it gathers all of the tool's completed runs out of the box. (The original "default latest" silently excluded runs made on earlier versions, which read as the cohort "not picking up all the jobs"; curators can still narrow the multiselect to a single line.)
- ✅ **Future-patch tolerance** (migration **0076**) — a cohort stores its chosen `major.minor` bands in `tolerated_version_bands`; live membership **re-expands** them to the current matching `tool_version.id`s on every read (`_effective_version_ids` → `_expand_bands`), so a patch installed *after* the cohort was built auto-joins. `tolerated_tool_version_ids` is kept as a last-known snapshot (display + fixed-cohort member validation + the band-less legacy path). No backfill — existing cohorts stay on the id path until next edited through the builder. Verified by `test_band_cohort_tolerates_future_patch` (a `1.0.5` installed after creation joins; a `2.0.0` does not).
- ✅ **Advanced filters in the builder UI** — version bands + per-numeric-input min/max **input ranges** + a **completed-on** date window, in an "Advanced filters" panel. (Owner / scenario / material / library / `cross_field_predicate` filters remain API-only.)
- ✅ **Edit existing cohort** — `PATCH /api/cohorts/{id}` wired in the client; the detail page has an **Edit** button (curate) that reopens the builder in edit mode (tool fixed, fields pre-filled), incl. flipping auto-grow ↔ fixed and re-picking runs.

Still open (not blocking):
- **"Key inputs" column in the preview** — deferred to honour [[feedback_use_labels_not_names]] (the builder now *has* the numeric-input labels via build-meta, so a labelled inputs column is now cheap to add).
- **ARCHITECTURE.md / ROADMAP.md** refresh for the workspace-level model.
