# 0009. Optimisation framework — adaptive sequential studies + active learning

Date: 2026-05-26
Status: Accepted (architect ruling 2026-05-26 — full v1.0 scope per "Phase 2 expansion" conversation; companion to ADR-0008; pilot date re-baselined accordingly)

## Context

Parametric studies (ADR-0001) and the surrogate family (ADR-0008) together
deliver **designed** and **post-hoc** data analysis: pick a DOE, run it, fit a
surrogate, explore. What they don't deliver is **goal-seeking** — the
canonical engineering ask "given these factors and these constraints, find
the configuration that minimises mass / maximises stiffness / hits target X
in the fewest runs". Commercial PIDO tools (IOSO, modeFRONTIER, Cameo,
SmartUQ, ANSYS optiSLang, Altair HyperStudy) are built on this loop;
MATOFX's current Compare → Sensitivity → "set-to-optimum on the surrogate"
path is a *post-hoc* approximation and does not orchestrate the loop on the
user's behalf.

Concretely, the missing piece is an **adaptive sequential experiment**:

1. Pick K candidate points based on what's been learned so far.
2. Submit them as real jobs (or evaluate them on the surrogate).
3. Wait for completion (real jobs are expensive — 4–48h per CFD run).
4. Refit / update the strategy's internal model.
5. Decide the next batch — or stop, if a stopping criterion fires.
6. Repeat.

The same shape covers four distinct user objectives:

- **Single-objective optimisation** — minimise or maximise one response.
- **Multi-objective optimisation** — find the Pareto front when two or three
  responses matter.
- **Constrained optimisation** — respect equality / inequality constraints
  (mass ≤ budget; stress ≤ allowable).
- **Active learning** — minimise surrogate uncertainty (build a *good*
  surrogate cheaply rather than find an optimum).

All four are the **same outer loop** with a different inner acquisition /
selection function. Building one engine that covers all four is the design
target; building four separate features misses the leverage.

The customer is the sole maintainer ([[user_role]]) and has ruled this ADR
into v1.0 at full scope alongside ADR-0008, accepting the pilot-date slip
this implies.

## Decision

Introduce a first-class **Optimisation Study** — sibling of `parametric_study`
and `run_cohort` (ADR-0008) under `Project`. Build the engine in
`matofx/studies/optimisation/`; the build-level spec lands as
`designs/optimisation-framework.md`; shared substrate is
`designs/studies-engine.md`. This ADR records the decisions and rationale.

**1. Optimisation studies are siblings, not modes of parametric studies.**
A new `optimisation_study` table sits beside `parametric_study`. The two
tables differ enough in their state machine and supporting data
(`optimiser_state_json`, `convergence_trace`, `current_best_so_far`) that
overloading parametric_study with optional columns would obscure both. Both
write to the same `study_result` table (per studies-engine §6) so the Results
explorer treats them identically downstream.

**2. The outer loop.** The optimisation runtime is a long-lived Python
coroutine that:

- Reads the study row + the optimiser state.
- Asks the active strategy's `ask(n=batch_size)` for the next K candidate
  points.
- For each candidate: validate against descriptor `cross_field_constraints`
  + the study's added constraints (§5); if invalid, log + drop + continue.
- Queue the valid candidates as `Job` rows under the study (`Job.optimisation_study_id`).
- Wait — async — for batch completion (`asyncio.wait_for` on a future
  tied to each child's terminal-state transition; honours per-study
  concurrency cap).
- Pull results for completed children (`study_result` rows already written
  via the post-job hook).
- Update the strategy via `tell(X, y)`.
- Persist `optimiser_state_json` and the convergence trace.
- Check stopping criteria (§6); if any fire, transition to `complete` /
  `cancelled` and exit. Else loop.

The runtime is hosted in the existing runner daemon (the same long-lived
process that already runs cohort refresh, the GC sweeper, the user-disable
sweeper). A bounded pool of optimisation-study tasks runs concurrently;
backpressure via the per-study concurrency cap and the study lane (ADR-0001).

**3. Pluggable strategy registry.** Strategies register against a closed
catalogue in `matofx.studies.optimisation.strategies`:

| Strategy | Library | Use |
|---|---|---|
| `bo_ei` | Optuna (BSD-3) | Single-objective Bayesian, Expected Improvement acquisition. Sample-efficient — the default for expensive jobs |
| `bo_ucb` | Optuna | Single-objective Bayesian, Upper-Confidence-Bound acquisition. More exploration than EI |
| `nsga2` | pymoo (Apache-2) | Multi-objective evolutionary. Pareto-front discovery |
| `moea_d` | pymoo | Multi-objective decomposition. Pareto for >3 objectives |
| `hyperband` | Optuna | Async budget-aware. Mixes shallow + deep evaluations |
| `random` | Built-in over `numpy.random` | Baseline; useful for verifying instrumentation |
| `grid` | Built-in | Exhaustive on a coarse grid; useful for smoke-test optimisation runs |
| `cmaes` | Optuna `CmaEsSampler` / pymoo | Robust global; the workhorse when BO stops scaling (>~20 factors) |
| `de` | pymoo / `scipy.optimize` | Differential evolution; robust on multimodal / noisy |
| `pso` | pymoo | Particle swarm; familiar, global |
| `ga` | pymoo | Single-objective genetic algorithm |
| `nsga3` | pymoo | Many-objective (>3); reference-direction NSGA-III |
| `dual_annealing` | `scipy.optimize` | Simulated-annealing baseline |
| `nelder_mead` | `scipy.optimize` | Derivative-free local simplex; surrogate-mode / cheap-tool polish |
| `slsqp` | `scipy.optimize` | Gradient (finite-diff) constrained local; surrogate-mode refinement |
| `active_max_variance` | The surrogate engine (ADR-0008) | **Active learning** — picks the candidate where the surrogate's predicted variance is highest. Builds a *good* surrogate, not an optimum |
| `active_max_uncertainty_reduction` | The surrogate engine | Active learning — picks the candidate that maximises expected reduction in posterior variance integrated over the design space (sharper than `max_variance`, slower) |

Adding a strategy means a new module + a registration call. The submit
form's strategy dropdown reads from this registry. Strategies declare which
objective shapes they support (`single`, `multi`, `info_gain`) — the UI
filters incompatible combinations.

The expanded registry (the `cmaes` … `slsqp` rows, added 2026-06-01) covers
the full cost spectrum — cheap/smooth (gradient-local), medium (CMA-ES / DE /
PSO / GA), expensive (BO / surrogate-assisted) — so the offer is no longer
BO-only. Two best-in-class additions ride alongside it: an
**algorithm-recommendation wizard** (the optiSLang "Navigator" analogue —
pre-selects a strategy from problem characteristics, fully overridable) and
**MOP/CoP** (metamodel of optimal prognosis: auto-select the best surrogate by
cross-validated Q², attribute a per-factor Coefficient of Prognosis from the
metamodel's Sobol indices, and prune low-CoP factors before optimising on the
reduced space). MOP/CoP is a mode flag over a surrogate run mode, built on the
ADR-0008 surrogate engine — no new dependency. Detail in
`designs/optimisation-framework.md` §5.5–§5.7.

**4. Run modes (orthogonal axis).** A study has a `run_mode`:

- `real` — every candidate submits a real tool job. The bread-and-butter
  for expensive CFD/FEA.
- `surrogate` — every candidate is evaluated against an attached
  surrogate (ADR-0008). Cheap, instant. Good for "what does the surrogate
  think is optimal?" sanity checks; the result is only as accurate as the
  surrogate.
- `surrogate_assisted_real` — **the powerful one**. Each batch: the
  strategy proposes K candidates *informed by the surrogate's predictions
  and uncertainties*; the K candidates run as real jobs; on completion, the
  surrogate refits with the new points + the strategy uses the refit
  surrogate for the next batch. State-of-the-art for sample-efficient
  optimisation under high evaluation cost.

`surrogate` and `surrogate_assisted_real` require a referenced
`surrogate_model_version_id` (the study pins it); the surrogate's training
data must come from the same tool the study optimises (validated at
configure time).

**Evaluation target — a second orthogonal axis (added 2026-06-01).** A study
optimises over either one **tool** (each candidate is a tool job, as above) or
one **workflow** (each candidate is a full `WorkflowRun` — process-chain
optimisation, the optiSLang / modeFRONTIER pattern). For a workflow target the
factors are the chain's *open* inputs and responses may come from any tool in
the chain; the outer loop is target-agnostic (only "submit a candidate" and
"collect results" differ). Membership rides a new
`WorkflowRun.optimisation_study_id` FK (studies-engine §3). Designed and
implemented for v1.0; build-level detail in
`designs/optimisation-framework.md` §3.4.

**5. Constraints.** Three constraint kinds, all expressible through the
descriptor's existing `cross_field_constraints` syntax + safe_expr DSL
(§F30 / §16.16):

- **Box constraints** — factor min/max. Already in the descriptor; the
  optimiser reads them directly.
- **Linear / non-linear constraints over inputs** — e.g. `a + b <= 100`.
  Written as a `cross_field_constraint` with an `error_message`. The
  optimiser's strategy honours these *during selection* (pymoo natively;
  Optuna via `study.add_user_attr('constraint', …)` + a constraint-aware
  sampler). Candidates that violate at selection time are not enqueued.
- **Black-box (output) constraints** — `output_x <= some_value`. Declared
  per-response in the study config as `{response, op, value}`. The
  optimiser treats violators as either a penalty (BO modes) or an
  infeasible classification (NSGA-II's constraint domination). Status
  `feasible` / `infeasible` written to each `study_result` row.

No new DSL; the existing `safe_expr` evaluator is enough. The
study-configuration UI provides a constraint-entry editor wrapping the same
DSL the descriptor wizard already uses.

**6. Stopping criteria — composable, evaluated in order.** A study's
`stopping_criteria` is an ordered JSON list:

```jsonc
[
  { "kind": "max_runs",      "value": 200 },
  { "kind": "target",        "response": "mass", "op": "<=", "value": 5.0 },
  { "kind": "convergence",   "metric": "best_so_far", "window": 15, "epsilon": 1e-3 },
  { "kind": "wall_clock",    "hours": 168 }
]
```

Any kind firing stops the study. `max_runs` is mandatory (budget — no
unbounded studies). `target` and `convergence` are optional and stack.
`wall_clock` is a safety net for runaway studies. **Manual halt** is always
available (the user clicks Stop — in-flight children complete; no new
batches queue; status → `cancelled`).

`convergence` for multi-objective optimisation uses the hypervolume-indicator
delta over the window (rather than best-so-far).

**7. Batch / async coordination.** Two modes:

- **Synchronous batch** — submit K, wait for all K to terminate, refit,
  repeat. Simpler; fine for studies where children take similar wall-clock
  times. The default.
- **Asynchronous batch** — start the next candidate as soon as a slot
  opens (per-study concurrency cap). Better wall-clock for studies with
  variable child runtimes. Used by Hyperband natively; available for
  BO modes when the strategy supports asynchronous `tell`.

Both honour the study lane (low-priority queue, ADR-0001) so an
optimisation can't starve interactive users.

**8. Durable resumable state.** The optimiser's in-memory state (search
history, GP, sampler / acquisition cache, hypervolume reference point,
convergence-window buffer) serialises to `optimisation_study.optimiser_state_json`
after every batch — or to a disk blob under
`<mount>/studies/<env>/opt_<id>/state.pkl` when JSON would exceed 1 MB
(typical for GP samplers with >100 observations). Runner restart rehydrates
and continues from the last completed batch. **Crash mid-batch:** in-flight
children either complete on restart (the runner already handles this) or
are re-queued; either way, when all K terminate the strategy gets the
results and proceeds — no double-counting because `tell` is idempotent on
identical `(X, y)` pairs.

**9. Job pinning + provenance.** Every optimisation-study child carries:

- `Job.optimisation_study_id` (new FK, ADR-0009).
- The same tool-version / library-version / material-version / surrogate-version
  pinning the existing study children carry, plus the **batch number** and
  **selection rationale** the optimiser produced (e.g. `acquisition_value: 0.42`,
  `proposed_by: bo_ei_batch_7`). These get persisted as a per-child
  `optimisation_metadata_json` for the convergence-trace plot and the
  lineage graph (ADR-0010).

**10. UI surfaces.**

- `/studies` "Optimisation" tab — sibling of Parametric and Cohorts.
- **Create flow** — multi-step (factors with bounds → responses + which
  to min/max/track → constraints → strategy + run mode → batch size +
  stopping criteria). State saved as `draft` after every step (per
  [[feedback_save_db_only]] — DB only, not disk).
- **Study detail** — extends the Results explorer with two new tabs:
  - **Convergence** — best-so-far vs iteration (single-obj); hypervolume vs
    iteration (multi-obj); each strategy's relevant metric.
  - **Acquisition map** — 2D contour of the strategy's acquisition function
    over a chosen pair of factors (BO modes only); reveals where the
    optimiser thinks the next batch should go. Computed off the live model
    via `run.io_bound`.
- **Run-time controls** — Pause / Resume / Halt / Extend (add N to budget) /
  Change-strategy-mid-run (audited, warns about implications), all
  permission-gated.
- **Surrogate handoff** — at any time during a `surrogate_assisted_real`
  study, the user can "Publish current surrogate" — which forks the
  in-progress fitted model into the Model registry as a new
  `surrogate_model_version` (ADR-0008).

**11. Permissions & ACL.** Same as parametric studies — Member+ on the
project to configure and submit; Viewer+ to view; Owner / Creator + the
admin override (`live_jobs.cancel`) to bulk-halt. No new top-level
permission for running studies; the existing `studies.run` + project ACL
govern.

**11a. Start-approval gate (added 2026-06-01).** Because an optimisation can
queue hundreds of expensive children, a **per-tool toggleable** gate
(`tool.optimisation_start_approval_required`) requires a directed approver to
sign off *before the first batch queues* — separate from, and additional to, a
tool's own per-job approval policy (§16.15). The approver pool is a new
`optimisation_approver` capability role (ADR-0005 pattern); the request reuses
`matofx.approvals` with a new `optimisation_study_start` target kind and
carries the campaign cost estimate, and inserts a `pending_start_approval`
state between `draft` and `running`. Granting it **subsumes** the tool's
per-child *pre*-approval (no per-candidate approval storm) and **waives**
per-child *post*-approval in favour of one gate on the study's final
best / Pareto result. Build-level detail in
`designs/optimisation-framework.md` §14.1.

**12. Library picks — air-gap bundled, license-clean.**

| Library | License | Adds (approx) | Role |
|---|---|---|---|
| Optuna | BSD-3 | 5 MB | BO + Hyperband + TPE; trial database; visualisation |
| pymoo | Apache-2 | 3 MB | Multi-objective (NSGA-II, MOEA/D); constraints |
| `scipy.optimize` | BSD-3 | (already in stack) | Gradient-based + scalar bridge |
| `scipy.stats.qmc` | BSD-3 | (already in stack) | QMC sampling for initial seed |
| `scikit-learn` | BSD-3 | (already in stack) | GP backend (`GaussianProcessRegressor`) |

Total bundle bump ~12 MB. SBOM regen + lockfile pickup is a maintainer
step before the first release that includes this ADR's code (same
discipline ADR-0001 used). All four libraries are mypy-permissive (Optuna
has stubs; pymoo doesn't — `Any` via the per-module override pattern
already used for statsmodels).

The 2026-06-01 additions (the expanded strategy set, the recommendation
wizard, MOP/CoP, and workflow-target optimisation) add **no new dependency** —
all come from Optuna's `CmaEsSampler`, pymoo's algorithm suite,
`scipy.optimize`, the ADR-0008 surrogate engine, and the existing workflow
engine. Net new deps remain Optuna + pymoo.

**13. Initial-seed strategy.** Pure BO with no initial data is unstable
(GP fits with <5 points are wildly uncertain). Every optimisation study
**must** start with a seed batch:

- For `real` / `surrogate_assisted_real` runs: a Latin Hypercube seed of
  `max(10, 3 × dim)` points, then BO/NSGA-II takes over.
- For `surrogate` runs: no seed (the surrogate is the truth — BO operates
  on its predictions directly).
- For `active_*` runs: same LHS seed; the active strategy needs *some* data
  to compute variance over.

Seed size is operator-tunable per study; the UI default fires plain-English
guidance ("This will run ~25 jobs before the optimiser starts learning").

**14. Out of scope for v1.0** (no-add-back lock):

- **Topology / shape optimisation** — gradient-based or evolutionary
  morphing of CAD/mesh. Specialised per CAD kernel; v1.x.
- **Robust / reliability-based optimisation** — optimises considering input
  uncertainty (chance constraints, mean-variance trade-off). Couples with
  uncertainty propagation; revisit when that lands.
- **Mixed-variable optimisation** beyond what Optuna covers natively
  (continuous + integer + categorical — those work; trees / graphs / sets
  don't). Promote when first customer drives it.
- **Black-box constraint learning** — fitting a surrogate of the constraint
  function itself. Not in the v1.0 strategy registry.
- **Auto-publish closed-loop** — when a `surrogate_assisted_real` converges,
  automatically promoting the final surrogate to a published global tool.
  Flagged 2026-05-22; deferred to v1.x — the manual "Publish current
  surrogate" affordance covers v1.0.
- **Online learning inside the optimiser** — each `tell` always triggers a
  full refit of the surrogate; incremental Bayesian updates are a v1.x
  surrogate-engine concern.

**15. Cross-cutting impacts.**

- **ADR-0008** — `surrogate_assisted_real` mode hard-depends on the Model
  registry. The Model registry must exist before ADR-0009's
  `surrogate_assisted_real` ships; the order in `designs/studies-engine.md`
  §11 reflects this. Active-learning strategies are a wrapper around the
  surrogate engine.
- **ADR-0010 (data lineage)** treats `optimisation_study` as another root
  node in the provenance graph; reuses the same UI shape as parametric
  studies.
- **ADR-0011 (V&V tracking)** records V&V status on a surrogate version
  produced by an optimisation study no differently than one produced by a
  parametric study.
- **ADR-0012 (KPI extraction)** sees optimisation-study children as
  ordinary completed jobs of one tool; per-tool KPIs apply.
- **§16.18 (Live Jobs)** sees the convergence-trace as the natural progress
  indicator for an optimisation study row; the cancel-bulk path already
  works because the `bypass_access` extension shipped in that section.

## Consequences

- **Phase 2 scope expands materially.** Comparable in build size to the
  file library or materials database; pilot date re-baselines per ROADMAP
  "Pilot date re-baselines 2026-05-26."
- **Two new dependencies** (Optuna, pymoo); air-gap bundle grows ~12 MB.
  SBOM regen + lockfile bump.
- **One new table** (`optimisation_study`), one new `Job` FK
  (`Job.optimisation_study_id`), one CHECK widening (1-of-2 → 1-of-3 home
  constraint on `Job`). No data migration cost.
- **`study_result` rename** is a shared cost with ADR-0008; both ADRs land
  the migration together to avoid double-rewriting consumers.
- **New nightly-sweeper concern in the runner daemon** — optimisation
  studies running for weeks need their state persisted reliably. Existing
  audit + the new disk-blob path cover this; no new daemon needed.

## Build phases

Per house style (pause for review after the design doc + after each phase):

- **Phase 1** — `matofx/studies/optimisation/` package skeleton; `optimisation_study`
  table + `Job.optimisation_study_id` FK + CHECK widening; orm + service +
  errors + crud (configure / list / archive / get-state) without a runtime
  loop yet.
- **Phase 2** — Strategy registry + the three foundational strategies
  (`random`, `grid`, `bo_ei`); seed-batch logic; sync-batch outer loop;
  constraint evaluation against `cross_field_constraints`; `study_result`
  write path (shared with ADR-0008 phase 3).
- **Phase 3** — Stopping criteria evaluation (`max_runs`, `target`,
  `wall_clock`, `convergence` single-obj); durable state persistence
  (JSON + disk-blob); crash-recovery rehydrate-on-restart.
- **Phase 4** — Multi-objective strategies (`nsga2`, `moea_d`); hypervolume
  convergence; per-response constraint kind.
- **Phase 5** — `surrogate` + `surrogate_assisted_real` run modes (after
  ADR-0008 phase 5 lands); attached-surrogate refit on every `tell`.
- **Phase 6** — Active-learning strategies (`active_max_variance`,
  `active_max_uncertainty_reduction`) and the in-form coverage gauge
  integration (ADR-0008 §4 borrows this).
- **Phase 7** — Async batch coordination (Hyperband + the optional
  async-BO `tell`); per-study concurrency cap + study-lane integration.
- **Phase 8** — UI: `/studies` Optimisation tab + multi-step create flow +
  Convergence / Acquisition tabs + Pause/Resume/Halt/Extend controls.
- **Phase 9** — Strategy spectrum: `cmaes` / `de` / `pso` / `ga` / `nsga3` /
  `dual_annealing` / `nelder_mead` / `slsqp` (zero new deps); per-strategy
  batch defaults; the gradient/local polish path.
- **Phase 10** — Algorithm-recommendation wizard (the "Navigator").
- **Phase 11** — Workflow-target optimisation: `WorkflowRun.optimisation_study_id`;
  the `target` axis + validation; target-agnostic submit/collect; open-input
  factor detection; per-`{member, output}` response spec.
- **Phase 12** — MOP/CoP: auto-best-metamodel by Q²; per-factor CoP from
  Sobol × Q²; CoP-cutoff variable reduction + override UI.
- **Phase 13** — Start-approval gate: per-tool flag + `optimisation_approver`
  capability + `optimisation_study_start` approval target +
  `pending_start_approval` state + cost estimate; pre-subsume / post-waive.
- **Phase 14** — Cross-ADR consistency (lineage / V&V / KPI / live-jobs
  touchpoints); docs; ROADMAP tick.

## Revision history

- **2026-06-15 — amendment: integer factors must vary over integers — and over an integral range. (Phases A–D all shipped same day.)**
  Build deviations from the plan: **(B)** the dedupe runs the unique remainder of
  a batch rather than re-asking to backfill (re-asking would desync pymoo's
  generational ask/tell); a fully-discrete space that's exhausted, or a strategy
  that stalls re-proposing seen points, stops with a new `space_exhausted` reason.
  **(C)** integer-awareness in the pymoo EAs is a lattice **Repair** operator, not
  native `Integer`/`Choice` mixed variables — those don't fit the continuous
  algorithms (CMA-ES / DE / PSO); CMA-ES rejects `repair` outright so it stays
  relaxation + `snap_int`; the `bo` candidate pool was already drawn on the integer
  lattice so it only gained top-*distinct* selection. Files: new
  `studies/_intsnap.py`; `studies/optimisation/runtime.py` (dedupe loop +
  `_discrete_capacity`); strategies `multi.py` (`_make_lattice_repair`), `soo.py`,
  `surrogate_bo.py`, `grid.py`; `config.py` (integral-bounds validation); UI
  `ui/pages/optimisation.py` (integer-stepped min/max, both builders).
  Audit (`designs/integer-parameter-handling.md`) confirmed every candidate
  source already coerces integer factors to `int` before a job is created, so no
  float reaches an integer input's `inputs.json` from a study. Three defects
  remain and are ruled on: **(D1)** an integer factor's `min`/`max` are *not*
  constrained to whole numbers (`_validate_numeric_factor` is shared with float
  factors) — they will be **rejected** at config time (not snapped), since a
  fractional integer bound is an authoring slip and snapping shifts the user's
  range; **(D2)** the snap rule differs per path (`round` half-to-even vs `floor`
  vs `int()`-truncation) — unified onto a single **round-half-away-from-zero**
  helper `snap_int(value, lo, hi)` (`2.5→3`, `-2.5→-3`), the only place an integer
  factor value is produced; **(D3)** optimisers relax to continuous and round on
  decode, which wastes budget on duplicate integer points and searches a false
  landscape — fixed in two steps: decode-time **dedupe** with bounded backfill +
  batch-shrink (reusing the parametric "distinct < requested" warning and the §10
  durable state), then **native integer optimisation** via pymoo `Integer`/`Choice`
  mixed-variable types and an integer-lattice `bo` candidate pool (CMA-ES stays
  relax+snap by construction). **Integer step is always 1** (a stride is modelled
  as a `select` factor) — consistent with the descriptor rule that integer
  `precision == 1.0`. Build order **A** (snap helper + bounds validation, no
  algorithm change) → **B** (dedupe) → **C** (native integer search, behind the
  optimisation feature gate) → **D** (integer-only min/max UI + level warning at
  parity with parametric, + docs). No new dependency (pymoo already vendored). The
  §3 strategy notes, §5 decode contract, and §13 seed/dedupe are amended by this
  note once C ships.
- **2026-06-08 — build note (Phase 14, cross-ADR integration + docs): ADR-0009 complete (all 14 phases shipped).**
  Lineage (ADR-0010) resolves `optimisation_study` as a provenance root (project /
  tool-version / creator + child `SEEDED_BY_STUDY` edges) and the Job resolver
  draws the optimisation home edge; the lineage centre-picker lists optimisation
  studies. Live Jobs groups an optimisation campaign's in-flight children as one
  item (a new `optimisation_study` live-kind) instead of flooding the standalone
  list. KPI extraction needs no change (children are ordinary Jobs); V&V is
  unaffected (per-tool/job, no optimisation surface). ARCHITECTURE.md §16.27
  records the as-built; ROADMAP marks ADR-0009 shipped. **v1.x carry-overs:**
  scipy local/annealing strategies (surrogate-mode polish), workflow-target
  run-level `study_result` (results-explorer browse + workflow surrogate publish),
  `surrogate_assisted_real` (subsumed by real-mode `bo`), live-jobs bulk-halt for
  optimisation, and KPI-warehouse optimisation grouping.
- **2026-06-08 — build note (Phase 13, start-approval gate): shipped on the existing approvals machinery.**
  The §14.1 compute-cost start gate shipped: a per-tool
  ``tool.optimisation_start_approval_required`` flag (mutable, like
  ``run_cap``) routes a study — at ``configure`` — into
  ``pending_start_approval`` with a directed approval request to a
  nominated ``optimisation_approver``, instead of launching. Approve →
  ``running``; reject → ``cancelled``. Reused the ``matofx.approvals``
  directed-approver machinery wholesale: ``approval_request`` gained an
  eighth target column (``optimisation_study_id``, migration 0070) rather
  than a separate ``target_kind`` field (the schema discriminates by which
  target-id column is set + ``kind='pre'``), the exactly-one-of CHECK
  widened 7→8, and a new ``optimisation_approver`` capability role +
  ``optimisation.approve`` permission were seeded (ADR-0005 pattern,
  granted to administrator). The campaign cost estimate (``max_runs`` +
  seed) rides in the request justification and a dedicated ``cost-estimate``
  endpoint. Per-child pre/post approval subsumption (§14.1) is unchanged —
  this is the campaign-level gate; the per-child interaction is enforced by
  the existing per-tool policy + the future final-result release gate.
- **2026-06-08 — build note (Phase 12, MOP/CoP): shipped; gated on the `bo` strategy (not "surrogate run mode").**
  The §5.7 MOP/CoP capability shipped on the surrogate family (no new
  dependency): `studies/optimisation/mop.py` fits all four metamodel kinds
  and keeps the best cross-validated Q² (auto-best-metamodel / "use best
  model"), attributes **CoP = Sobol-total-effect · max(0, Q²)** per factor,
  and lists low-CoP factors for variable reduction. Wired through config
  (`mop{enabled, cop_cutoff}`), persistence (`OptimisationStudy.mop_state_json`,
  migration 0069), a `compute_mop` service + API, runtime variable reduction
  (frozen factors dropped from the search space and held at their nominal;
  `bo` uses the best kind), and a UI CoP bar chart + per-factor freeze.
  **Gating decision (confirmed with the maintainer):** §5.7's "MOP runs in a
  *surrogate run mode*" predates the 2026-06-08 amendment; the amendment
  re-oriented single-objective search onto the `bo` strategy (the only one
  that *builds* a metamodel, and the owner of the reserved "use best model"
  hook). So **`mop.enabled` is gated on `strategy == "bo"`**, not on the
  `surrogate` run mode — random/grid/evolutionary strategies build no
  metamodel for MOP to use. **Tool-target only** for now (the workflow
  `study_result` store is deferred, so there's no data source there yet).
- **2026-06-08 — build note (Phase 11, workflow-target optimisation): headless workflow orchestration built as a prerequisite; dedicated-scenario model; results-explorer/UI deferred.**
  Discovery found the workflow engine was **purely UI-driven** (no auto-cascade;
  upstream→downstream value transfer happened only in the browser form;
  `start_run` took no input overrides), so a headless optimiser could not run a
  workflow candidate. **The maintainer chose to build the headless orchestration
  prerequisite first.** It shipped as: `workflows/wiring.py` (open-vs-wired input
  rules + the `ready_member_inputs` planner), `WorkflowRun.auto_advance` +
  `open_inputs_json` (migration 0067), `WorkflowRunService.start_headless_run` /
  `advance`, and a runner post-completion hook that self-cascades auto-advance
  runs. Manual UI runs are unchanged. **Scenario-home decision:** §3.4 says a
  workflow candidate's child jobs "keep their scenario home", but an
  `OptimisationStudy` is a *project* child with no scenario. The maintainer chose
  **auto-create one dedicated scenario per study** (at `configure`); migration
  0068 adds `OptimisationStudy.scenario_id` and **relaxes `workflow_run`
  uniqueness to a partial index** (`WHERE optimisation_study_id IS NULL`) so the
  study runs the same workflow many times in that scenario. Factors/responses are
  **member-qualified** (`{member, input}` / `{spec:{member, name}}`);
  `validate_workflow_config` enforces rule 9; `WorkflowRunRunner` distributes each
  candidate to the chain's open inputs and reads `{member, output}` responses.
  **Close-out:** the create wizard gained a Tool/Workflow **target picker** + a
  workflow config wizard (member-qualified open-input factors + member-output
  responses, via `GET /api/workflows` + per-member `get_tool`); a real-data
  `configure(workflow)` integration test covers the descriptor-load → validate →
  dedicated-scenario path. **Justified-deferred (not gaps):** run-level
  `study_result` for workflow targets has **no current consumer** —
  `publish_current_surrogate` already rejects workflow targets and the strategy's
  history is durable via the optimiser-state envelope, so the rows would be
  write-only; they land if/when a workflow results-explorer or workflow surrogate
  is built. The `WorkflowRunRunner` end-to-end-through-the-daemon test is likewise
  deferred (its collect path is simple; the cascade it relies on is tested).
- **2026-06-08 — build note (Phase 10, Navigator): `recommend_strategy` shipped; §5.6 heuristics re-tuned to the implemented strategy set.**
  The recommendation wizard (§5.6) landed as a pure `recommend_strategy(config)`
  → `StrategyRecommendation` (strategy + run_mode + batch_size + acquisition +
  one-line rationale), exposed read-only at `GET
  /api/optimisation-studies/{id}/recommendation` (Viewer+) and surfaced as a
  "Recommended" badge that pre-selects the strategy in the create wizard's
  strategy step. The §5.6 heuristic table referenced now-removed strategies, so
  it was adapted to what's implemented: the `hyperband` (variable-runtime) and
  `active_*` ("improve the model") branches fold into **`bo`** (with the
  `explore` acquisition mode covering active learning), and because the scipy
  local/annealing polish is deferred (Phase 9 note), the cheap/`surrogate`-mode
  branch recommends **`de`** alone. Net branches: ≥2 objectives → `nsga2`/`nsga3`;
  surrogate mode → `de`; high-dim or discrete-dominated → `cmaes`; else → `bo`.
  Always a suggestion, never a lock (the full dropdown stays).
- **2026-06-08 — build note (Phase 9, strategy spectrum): pymoo classical spectrum shipped; scipy local/annealing strategies carved into a follow-up.**
  Phase 9 registered the single-objective pymoo strategies **`cmaes` / `de` /
  `pso` / `ga`** (driven through the same external generational ask/tell
  wrapper as the multi-objective EAs — only the `Algorithm` object differs,
  §5.2) plus the many-objective **`nsga3`**. Zero new dependency (pymoo +
  the `cma` package it vendors). The seed rule was corrected: the pymoo EAs
  bootstrap their own first generation, so only **`bo`** consumes an external
  LHS seed (`config._SELF_SEEDING_STRATEGIES`, was `_SPACE_FILLING_STRATEGIES`).
  The runtime loop now **caps each generation to the remaining budget** — CMA-ES
  sizes its own (large) population and ignores the requested `k`, so `max_runs`
  is enforced as a hard ceiling for all population strategies (the pymoo wrapper
  already penalises un-told candidates, so a truncated generation stays whole).
  **Deferred:** the scipy local/annealing strategies (`nelder_mead` / `slsqp` /
  `dual_annealing`, §5.5). They **own their own evaluation loop**
  (`scipy.optimize.minimize` / `dual_annealing` call the objective internally),
  which doesn't fit the ask-K/tell-K `Strategy` Protocol and can't be serialised
  for the durable-state/resume contract (§10) when driving real jobs. Per §5.5
  they are intended for **`surrogate` run mode** (instant in-process evaluations,
  whole optimisation completes in one runtime pass) — so they land as a focused
  slice tied to the surrogate-mode single-pass execution path, not the batched
  job loop. Catalogue + registry stay the single source of truth, so the absent
  three simply aren't registered yet.
- **2026-06-08 — amendment: surrogate-driven optimisation re-oriented onto MATOFX's own surrogate engine; Optuna dropped (build-phase review with the maintainer).**
  The locked design used **Optuna** for the single-objective Bayesian
  strategies (`bo_ei` / `bo_ucb` / `hyperband`). In build, the maintainer's
  intent clarified that the surrogate must be a **first-class MATOFX
  artifact** — the same object that (a) is fit on the runs, (b) chooses the
  next run(s) via an acquisition function, (c) can have its kind/kernel
  chosen or be auto-selected ("use best model"), and (d) is inspectable and
  publishable. Optuna's default sampler (TPE) isn't a queryable predictive
  surrogate at all, and even its GP sampler is library-internal — a poor
  fit. **Ruling:**
  1. **The single-objective surrogate-driven optimiser is built on MATOFX's
     own surrogate engine** (`matofx.studies.surrogate.fit_surrogate`,
     GP/uncertainty + the existing on-surrogate optimiser) plus an
     **acquisition function**, registered as the **`bo`** strategy. It
     replaces the Optuna `bo_ei` / `bo_ucb` / `hyperband` strategies.
  2. **Three user-selectable acquisition modes** drive next-run selection
     from the surrogate's predicted mean μ + uncertainty σ: **`optimise`**
     (exploit — go to the predicted optimum, κ≈0), **`balanced`** (μ ∓ κσ,
     κ≈2), **`explore`** (improve the model — maximise σ). One knob (κ),
     three buttons. This folds the design's separate active-learning
     strategies (§3 `active_*`) into the `explore` mode for the practical
     with-objective case.
  3. **Cohort-seeding.** A study's initial sample may come from a chosen
     **cohort of prior runs** (imported as initial told points, no real
     jobs, no budget spend) *or* from a Latin-Hypercube sample (real seed
     jobs). §13's seed extends accordingly.
  4. **The optimiser's surrogate *is* the working/publishable model**, kept
     **latest-only** (refit each batch, overwritten — not one registry
     version per batch, which would explode on a long study). Publishing it
     to the Model registry is an explicit, on-demand action. This **subsumes
     the separate `surrogate_assisted_real` run mode** — a `real`-mode study
     with the `bo` strategy *is* surrogate-assisted-real by construction. The
     run modes reduce to **`real`** (a surrogate or evolutionary strategy
     proposes the next real runs) and **`surrogate`** (pure in-process
     prediction, zero real runs — unchanged).
  5. **Optuna is dropped** as a dependency. **pymoo is retained** for the
     evolutionary multi-objective strategies (`nsga2` / `moea_d`);
     surrogate-driven *multi*-objective (expected-hypervolume-improvement)
     stays a later topic. The §3 cost-spectrum strategies (`cmaes` / `de` /
     `pso` / …) and **MOP/CoP** (kind-choice / auto-best-metamodel) remain
     **committed for later phases** — MOP/CoP is the natural home of the
     "use best model" option, and the `bo` strategy's `surrogate_kind`
     field reserves the kernel-choice hook.
  Net dependency change: **−Optuna**. The §3 strategy table, §5
  run-mode list, §6 dependency ledger, and §13 seed are amended by this
  note; the surrogate-engine substrate is ADR-0008's, already shipped.

- **2026-06-01 — scope expanded (architect review of `designs/optimisation-framework.md`).**
  Four rulings: (1) the strategy registry expands to the full cost spectrum
  (`cmaes` / `de` / `pso` / `ga` / `nsga3` / `dual_annealing` / `nelder_mead` /
  `slsqp`) plus an algorithm-recommendation wizard and **MOP/CoP** — all
  zero-new-dependency (§3); (2) a second orthogonal **evaluation-target** axis
  — `tool` or `workflow` (process-chain optimisation) — designed + implemented
  for v1.0 (§4); (3) a **per-tool toggleable start-approval gate** with its own
  `optimisation_approver` pool and `optimisation_study_start` approval target,
  subsuming per-child pre-approval and waiving per-child post-approval (§11a);
  (4) build phases extended to **14**. Decision driven by a best-in-class
  benchmark against optiSLang. No new dependency beyond Optuna + pymoo.
- **2026-05-26** — initial ruling at full v1.0 scope; companion to
  ADR-0008; shares `designs/studies-engine.md`. Decision driven by the
  realisation that the four user-facing capabilities (BO, multi-obj,
  active learning, surrogate-assisted real-tool optimisation) all share
  one outer loop and one engine — building them separately would be
  triple the work for the same end-state. The v1.x list shed: closed-loop
  optimisation; active learning (now a strategy here, not a separate
  feature).
