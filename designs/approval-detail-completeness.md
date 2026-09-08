# Design — Approval-detail completeness

Status: **Design drafted 2026-06-15 — pending Robert's review, then build.** No new ADR — this fills gaps in already-decided gates (ADR-0006 library, 0007 materials, 0008 surrogate, 0009 optimisation, 0011 V&V). Governance, eligibility, and the decide endpoint are unchanged; only *what the approver sees while deciding* changes.

This records *what to build*. The guiding rule: when anything is awaiting approval, the review panel must be a **snapshot-faithful, fully-resolved, self-contained** picture — enough to decide without leaving the page, always in human terms (descriptor **labels + units**, never internal names/keys/aliases — [[feedback_use_labels_not_names]]).

Three workstreams (amended 2026-06-15 after review):
- **A. Detail completeness** (§3–§5) — wire the unwired gates, structured review blocks, footprint.
- **B. Inbox bulk actions** (§8) — "Select all" + bulk approve/deny over the existing batch endpoint.
- **C. Condensed multi-run review + optimisation post gate** (§9–§10) — one gate already covers a 50-run study; make that one gate *show* all runs, and add the missing optimisation results-release gate.

---

## 1. Why

The approver inbox (`ui/pages/approvals.py`) and its detail endpoint (`api/routers/approvals.py:get_approval_detail`) were built gate-by-gate, and two gates were never wired in while a third under-renders. Verified against source 2026-06-15:

| Gate | Inbox + detail wired? | Labels + units? | Plan shown? |
|---|---|---|---|
| **Job** (pre/post) | ✅ | ✅ from descriptor **snapshot** (`_build_review`) | inputs grouped; outputs on post |
| **Parametric study** | ✅ | ⚠️ **factors by raw name, no unit, no range/levels** (`_study_approval_detail:561`) | mode, cap, counts only |
| **Library / Material / V&V** | ✅ | n/a (tool-less) | target name + metadata |
| **Optimisation start-gate** | ❌ **unwired** | ❌ | ❌ |
| **Surrogate-model publish** | ❌ **unwired** | ❌ | ❌ |

Concretely:

- The optimisation start-gate **creates a real** `ApprovalRequest(optimisation_study_id=…)` (`studies/optimisation/service.py:310`), but the router has **no branch**: in `_enrich` it falls into the job `else` and renders as **"Job None"**; opening detail hits `get_approval_detail:472` (`job_id is None`) and **404s**. Surrogate-model gates (`surrogate_model_version_id`) have the identical hole.
- An optimisation start-gate authorises potentially *hundreds* of tool runs. The approver currently sees **nothing** — not the variables being swept, their ranges, the objectives, the constraints, the strategy, the run budget, or the compute blast-radius they are signing off on.
- Parametric-study factors are listed by internal key with no unit and no range/level, violating [[feedback_use_labels_not_names]].

The good news: the **job** path is already correct — it resolves labels + units from `request.tool_version_id`'s `descriptor_snapshot` (captured at request time, not the live descriptor). Every other gate should meet that bar.

---

## 2. Decisions (locked 2026-06-15)

| # | Decision | Choice |
|---|---|---|
| D1 | Scope | Raise the **whole approval surface** to one bar: wire optimisation + surrogate gates AND upgrade parametric-study factors. |
| D2 | Payload shape | **Structured typed sections** (not stringified group/row rows) — faithful range/sense/op columns the UI renders properly. |
| D3 | Label source | Resolve every label/unit from the **request's tool-version descriptor snapshot** via `descriptor_maps()` (`summaries/collectors/_descriptor.py`) — input maps for variables, output maps for objectives/constraints. Same helper the optimisation *results* summary already uses. |
| D4 | Footprint | Show a **best-effort compute-footprint estimate** for optimisation start-gates (est. # runs, core-hours, licences/eval). Marked `approximate` and annotated when the target is a workflow or no walltime estimate exists — never present a guess as exact. |
| D5 | Surrogate review | **Full review block**: model kind, responses predicted, training samples + source, validity domain (input ranges), validation metrics (R²/RMSE per response). |
| D6 | Migration | **Workstreams A + B: none** (read paths over existing columns; bulk is UI-only). **Workstream C: one migration** — the optimisation post gate needs a new `OptimisationStudy.state` value, and `state` is CHECK-constrained (`ck_optimisation_study_state`, `optimisation/orm.py:279`). The approval side needs nothing new (the `optimisation_study_id` column, the `kind` discriminator, and `uq_approval_request_pending_optimisation_kind` already exist). |
| D7 | Authority | Unchanged. Detail stays existence-hidden (404) from non-approvers via the same `_can_act_on` gate (`get_approval_detail:456`); the new branches add no new read authority. |
| D8 | Select all | Inbox gains select-all + **Approve selected / Deny selected** that set the existing per-row decisions, then the existing **Submit decisions** applies them. No new endpoint — `POST /approvals/decisions` already does per-item batch (`approvals.py:984`). Selection respects the active filter; bulk approve goes behind a confirm step. |
| D9 | Condensed review | A multi-run gate already collapses to **one** pre + one post gate (`uq_approval_request_pending_study_kind`; children bypass via `studies/service.py:553`). The post-gate panel shows a **runs matrix + drill-in**: one row per run (varied inputs + key outputs, labels+units, failures/outliers flagged) + per-output aggregates; one "Release results" decision covers all. |
| D10 | Optimisation post gate | **Honour the tool's `post`/`both` policy** in optimisation (today it is silently dropped). Add a campaign-level results-release gate keyed on `optimisation_study_id`, `kind="post"`, with the same condensed evaluations matrix. Closes a real governance gap. |

---

## 3. Structured payload (schema)

New typed blocks in `api/schemas.py`. One shared `ApprovalVariable` is reused by optimisation design variables, the surrogate validity domain, and parametric-study factors.

```python
class ApprovalVariable(_Frozen):        # design var / study factor / domain bound
    name: str
    label: str
    unit: str | None = None
    role: Literal["vary", "constant"] = "vary"
    var_type: Literal["continuous", "integer", "categorical"] | None = None
    lower: float | None = None          # vary numeric → range lower
    upper: float | None = None          # vary numeric → range upper
    levels: list[Any] = []              # categorical → level set
    value: Any = None                   # constant → fixed value

class ApprovalObjective(_Frozen):
    name: str; label: str; unit: str | None = None
    sense: Literal["minimise", "maximise"]

class ApprovalConstraint(_Frozen):      # → "Pressure drop ≤ 0.5 bar"
    label: str; unit: str | None = None
    kind: Literal["output", "input", "linear"]
    op: str                              # "<=", ">=", "=="
    value: float
    expression: str | None = None        # plain-language fallback (esp. linear)

class ApprovalStrategy(_Frozen):
    algorithm: str                       # human label, "Bayesian optimisation" not "bo"
    acquisition: str | None = None
    batch_size: int | None = None
    variable_reduction: str | None = None   # MOP/CoP note

class ApprovalStopping(_Frozen):
    criteria: list[str] = []             # ["Max 100 evaluations", "Max 2 h walltime"]
    seeding: str | None = None           # "10 LHS seed points" / "Seed from previous runs"

class ApprovalFootprint(_Frozen):
    est_runs_min: int | None = None
    est_runs_max: int | None = None
    cpu_cores_per_run: int | None = None
    ram_mb_per_run: int | None = None
    gpu_memory_mb_per_run: int | None = None
    est_core_hours: float | None = None
    licences: list[ApprovalLicence] = []
    approximate: bool = False
    note: str | None = None              # why approximate (workflow target / no walltime)

class ApprovalOptimisationReview(_Frozen):
    variables: list[ApprovalVariable] = []
    objectives: list[ApprovalObjective] = []
    constraints: list[ApprovalConstraint] = []
    strategy: ApprovalStrategy | None = None
    stopping: ApprovalStopping | None = None
    footprint: ApprovalFootprint | None = None

class ApprovalSurrogateMetric(_Frozen):
    response_label: str; unit: str | None = None
    metric: str                          # "R²", "RMSE"
    value: float

class ApprovalSurrogateReview(_Frozen):
    model_kind: str                      # human label, e.g. "Gaussian process"
    predicts: list[str] = []             # response labels
    training_samples: int | None = None
    training_source: str | None = None   # source study / cohort name
    domain: list[ApprovalVariable] = []  # input ranges of validity
    metrics: list[ApprovalSurrogateMetric] = []
```

`ApprovalDetail` (and `ApprovalListItem` for `target_kind`) gain:

```python
target_kind: Literal[..., "optimisation", "surrogate_model"]   # add to both literals
optimisation: ApprovalOptimisationReview | None = None
surrogate: ApprovalSurrogateReview | None = None
factors: list[ApprovalVariable] = []        # parametric-study factors, now resolved
```

The existing `input_groups` / `result_groups` stay for the job path; the typed blocks are additive.

---

## 4. Data sources (all snapshot-faithful)

- **Optimisation** — `OptimisationStudy.config_json`: `variables` (role/lower/upper/levels), `responses` (→ objectives, `goal`→sense), `constraints` (kind/op/value), `strategy` (algorithm/acquisition/batch_size + MOP/CoP), `stopping_criteria`, `seed`, `batch_size`. Labels/units via `descriptor_maps(request.tool_version_id)` — **input** maps for variables, **output** maps for objectives/constraints. Algorithm code → human label via a small registry-backed lookup (reuse the strategy registry's display names).
- **Surrogate** — the gated model version + its validation record (R²/RMSE per response, training sample count, source study/cohort, input domain bounds). Labels via the model's tool-version maps.
- **Study factors** — `ParametricStudy.factor_spec`, resolved through the same `descriptor_maps` as optimisation so vary-factors carry range/levels and constants carry value + unit.

---

## 5. Footprint estimate (D4)

Best-effort, computed in the detail handler:

- `est_runs`: `seed_n + max_runs`. When stopping is convergence-only (no `max_runs`), report a range — `min = seed_n`, `max = run cap` — rather than a point.
- `cpu/ram/gpu_per_run`: from the descriptor-snapshot resource block (the same block the job footprint reads).
- `est_core_hours ≈ est_runs × cpu_cores_per_run × walltime_h`, where `walltime_h` comes from the tool's walltime cap/estimate if present.
- `licences`: the tool's per-run licence draw × concurrency (batch_size), labelled.
- Set `approximate=True` + `note` when: the target is a **workflow** (multiple tools per eval — sum across steps, flag as estimate), or **no walltime** estimate exists (omit core-hours, keep run count). Never show core-hours as exact without a walltime basis.

This is the headline addition for a *start* gate: the approver sees the blast-radius, not just a name.

---

## 6. Build slices

**Workstream A — detail completeness:**
1. **Schema** — typed blocks above; add `"optimisation"` / `"surrogate_model"` to both `target_kind` literals.
2. **Inbox** — `_enrich` branches for `optimisation_study_id` + `surrogate_model_version_id` (fetch `OptimisationStudy` / surrogate version for name + project). Kills "Job None" + makes the rows appear.
3. **Optimisation detail** — `_optimisation_approval_detail`: build the review block + footprint.
4. **Surrogate detail** — `_surrogate_approval_detail`: full review block (D5).
5. **Study factors** — upgrade `_study_approval_detail` factor rows to resolved `ApprovalVariable`s (labels/units/ranges/levels).
6. **UI** — structured renderers in `ui/pages/approvals.py` for the new blocks (variables table with range/levels columns; objectives with sense; constraints as plain-language lines; strategy + stopping + footprint cards; surrogate metrics table). Header copy switches per `target_kind`.

**Workstream B — bulk inbox actions (§8):**
7. **Select all + bulk decide** — checkbox column + select-all (filter-aware); "Approve selected" / "Deny selected" (shared reason) set per-row decisions; confirm step on bulk approve. No backend change.

**Workstream C — condensed review + optimisation post gate (§9–§10):**
8. **Migration** — add `pending_results_approval` to `OPTIMISATION_STUDY_STATES` + the CHECK (batch op for SQLite).
9. **Optimisation post gate** — completion hook in the outer loop (`runtime.py` terminal-state setter, ~`:634`): if policy ∈ `post`/`both`, route terminal `complete` → `pending_results_approval` and create the post gate; withhold campaign results until release; release flips to `complete`.
10. **Condensed runs matrix** — shared `_run_matrix(...)` builder; wire into the parametric-study **and** optimisation post-gate detail; UI matrix + per-run drill-in.

**Cross-cutting:**
11. **Tests + docs** — router/UI tests per gate + bulk + post-gate flows; update the **admin** approvals help chapter (`docs/admin/*.md`, per CLAUDE.md) and ARCHITECTURE §16.

Slices 1–2 are the urgent correctness fix (the 404 / "Job None" bug); 7 is low-risk and high-value; 8–10 are the heaviest (state machine + withholding).

---

## 7. Out of scope

- Changing who may approve, eligibility snapshotting, or the decide-endpoint mechanics.
- The full optimisation **results explorer** ([[project_optimisation_results_explorer_gaps]]) — the post gate adds a "pending release" banner there but not the rich explorer.
- Per-child approval gates of any kind — the one-gate-per-study/campaign model (D9) is deliberate and stays.

---

## 8. Inbox bulk actions ("Select all")

The inbox (`ui/pages/approvals.py`) is **already a batch surface**: per-row Approve/Deny + one "Submit decisions" that calls `POST /approvals/decisions` (per-item savepoints, `approvals.py:984`). The only gap is setting many rows at once.

- **Checkbox column** + a **select-all** control in the header; both honour the active search/kind filter (only visible rows; [[feedback_unbounded_selection_capped_actions]]).
- **"Approve selected"** sets `decision="approve"` on the ticked rows; **"Deny selected"** prompts once for a **shared reason** (mandatory for deny) applied to each, still editable per row before submit.
- Bulk approve is irreversible-ish and may skip opening each panel, so it goes behind a **confirm dialog** ("Approve N simulations without opening each?"). Deny's required-reason prompt is its own friction, so no extra confirm there.
- No new endpoint, schema, or authority — purely the inbox UI wiring into the existing batch decide.

---

## 9. Condensed multi-run review (the "50 runs in one" ask)

There was never a per-run review to merge: a multi-run study already collapses to **one** pre + **one** post gate (`uq_approval_request_pending_study_kind`; children bypass approval via `studies/service.py:553`, queued directly). What is thin is the **post-gate panel** — today `_study_approval_detail` shows only a state tally ("47 Complete, 3 Failed").

Make that one gate carry a **runs matrix + drill-in** (D9):
- **Matrix** — one row per child/evaluation run: the **varied** inputs + the **key outputs** (objectives/constraints for optimisation), resolved to labels + units; failed and outlier rows flagged. Built by a shared `_run_matrix(...)` so parametric and optimisation post gates render identically.
- **Aggregates** — per-output min / max / mean and a failure count, above the matrix.
- **Drill-in** — click a row to expand that single run's full inputs/outputs (the existing per-job review rendering), without leaving the gate.
- **One decision** — "Release results" approves/denies the whole set; the existing study-level post-gate decision already does this.

Result-ACL still applies: runs from projects the approver cannot see are counted, not shown ([[project_job_cohorts_rework]] D7 precedent).

---

## 10. Optimisation post gate (new — closes a governance gap)

Today optimisation honours a tool's **pre** policy (`optimisation/service.py:310`) but **silently ignores `post`/`both`** — no post gate is created and evaluations release as they finish (`runtime.py:798` queues them straight through). A "both"-policy tool therefore loses its results-release control entirely inside an optimisation. D10 fixes this.

- **State** — add `pending_results_approval` to `OptimisationStudyState` + `OPTIMISATION_STUDY_STATES` + the `ck_optimisation_study_state` CHECK (migration). Flow becomes `… → running → (pending_results_approval) → complete | cancelled | failed`.
- **Hook** — in the outer-loop terminal-state setter (`runtime.py` ~`:634`), when the run would go terminal `complete` and the tool policy ∈ `post`/`both`, instead set `pending_results_approval` and `create_request(optimisation_study_id=…, kind="post", …)`. Idempotent via the existing unique pending index; mirrors parametric `_check_and_rollup_study` (`studies/service.py:762`).
- **Withholding** — until release, the optimisation results surfaces (best design / Pareto / per-eval results) show a **"pending release"** banner rather than the data; release (post-approval) flips state to `complete` and reveals them. Denial path: study → `cancelled` with the reason (matching study post-deny behaviour).
- **Review panel** — the §9 condensed evaluations matrix (run / inputs / objective + constraint values, labels+units) + drill-in.
- **Approver pool** — the post gate reuses the tool's `approver_usernames` (the per-run results approvers), not necessarily the single `optimisation_approver` who holds the start gate; the start gate is about *compute spend*, the post gate about *releasing results*.
