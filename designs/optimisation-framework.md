# Design — Optimisation framework (adaptive sequential studies · strategy registry · surrogate-assisted real · active learning)

Status: **Design locked 2026-06-01** (drafted + scope-expanded + locked the same day after architect review; companion to ADR-0009; written alongside ADR-0008 / `designs/surrogate-family.md`). Implementation not started. The 2026-06-01 expansion added the full strategy spectrum + recommendation wizard + MOP/CoP (§5), workflow-target optimisation (§3.4), and the per-tool start-approval gate (§14.1) — see §25.
Decision record: `decisions/0009-optimisation-framework.md`. Shared substrate: `designs/studies-engine.md`. Foundation: `decisions/0001-parametric-studies.md` (the existing parametric-study sibling), `decisions/0008-surrogate-family.md` (the Model registry this hard-depends on for `surrogate_assisted_real`).
Existing engine this generalises: `designs/parametric-studies.md` §10 (the differential-evolution "set-to-optimum on the GP" optimiser, shipped 2026-05-22 as `matofx/studies/surrogate.py:optimize_surrogate`).
Brief sections: `PROJECT_BRIEF.md` §G39a (design-space exploration). Delivery tracking: `ROADMAP.md` Phase 2.

This is the build-level spec. The ADR records *why*; this records *what to build*. Everything here is inside ADR-0009 + ADR-0001. Shared mechanics (the three siblings under one roof, the unified `study_result` table, ACL inheritance, the study lane, audit prefixes) live in `designs/studies-engine.md` and are referenced by section here rather than restated.

---

## 1. Scope at a glance

| Capability | v1 | v1.x |
|---|---|---|
| Optimisation study — first-class sibling of parametric study + cohort | ✅ | |
| `draft` multi-step configure (DB-only save, per [[feedback_save_db_only]]) | ✅ | |
| Outer loop hosted in the existing runner daemon | ✅ | |
| Strategy registry — closed, pluggable catalogue | ✅ | |
| Single-objective Bayesian (`bo_ei`, `bo_ucb` — Optuna) | ✅ | |
| Multi-objective evolutionary (`nsga2`, `moea_d` — pymoo) | ✅ | |
| Async budget-aware (`hyperband` — Optuna) | ✅ | |
| Baselines (`random`, `grid` — built-in) | ✅ | |
| Active learning (`active_max_variance`, `active_max_uncertainty_reduction`) | ✅ | |
| Classical-global strategies (`cmaes`, `de`, `pso`, `nsga3`, `dual_annealing`) | ✅ | |
| Local / gradient strategies (`nelder_mead`, `slsqp`) — cheap + surrogate-mode polish | ✅ | |
| Algorithm-recommendation wizard ("Navigator") | ✅ | |
| MOP/CoP — auto-best-metamodel + Coefficient-of-Prognosis variable reduction | ✅ | |
| Run mode `real` — every candidate is a real tool job | ✅ | |
| Run mode `surrogate` — every candidate evaluated on an attached surrogate | ✅ | |
| Run mode `surrogate_assisted_real` (refit surrogate each batch) | ✅ | |
| Evaluation target `tool` — each candidate is one tool job | ✅ | |
| Evaluation target `workflow` — each candidate is a full WorkflowRun (process-chain optimisation) | ✅ | |
| Box + linear/non-linear (`cross_field_constraints`) + black-box (output) constraints | ✅ | |
| Stopping criteria — `max_runs` (mandatory) + `target` + `convergence` + `wall_clock` | ✅ | |
| Manual halt (in-flight children complete; no new batches) | ✅ | |
| Per-tool toggleable start-approval gate (directed `optimisation_approver`) | ✅ | |
| Synchronous batch coordination | ✅ | |
| Asynchronous batch coordination (Hyperband + async-`tell` BO) | ✅ | |
| Durable resumable state (JSON ≤1 MB, else disk blob); crash rehydrate | ✅ | |
| LHS initial seed (`max(10, 3 × dim)`, operator-tunable) | ✅ | |
| Convergence tab (best-so-far / hypervolume vs iteration) | ✅ | |
| Acquisition-map tab (2D acquisition contour, BO modes) | ✅ | |
| Pause / Resume / Halt / Extend-budget / Change-strategy-mid-run | ✅ | |
| "Publish current surrogate" handoff (forks in-progress model into the registry) | ✅ | |
| Topology / shape optimisation (CAD/mesh morphing) | | ✅ |
| Robust / reliability-based optimisation (input uncertainty) | | ✅ |
| Mixed-variable beyond Optuna-native (trees / graphs / sets) | | ✅ |
| Black-box constraint learning (surrogate of the constraint function) | | ✅ |
| Auto-publish closed-loop on convergence | | ✅ (the manual handoff covers v1.0) |
| Online learning inside the optimiser (incremental `tell` without full refit) | | ✅ |

An optimisation study is **single-tool** (one `tool_alias`, one pinned `tool_version_id`) exactly like its siblings — the strategy operates over that tool's descriptor factor space.

---

## 2. The three siblings — what this doc owns

Per `designs/studies-engine.md` §1, three siblings sit under `Project` next to `Scenario`:

```
Project ─┬─ Scenario ─── Job / WorkflowRun
         └─ Study branch
                 ├─ ParametricStudy   (existing — ADR-0001 / designs/parametric-studies.md)
                 ├─ OptimisationStudy (this doc §3–§11)
                 └─ RunCohort         (ADR-0008 — designs/surrogate-family.md §3)
```

This doc owns:

- **OptimisationStudy** — the second sibling, its table, state machine, and config (§3).
- The **outer-loop runtime** hosted in the runner daemon (§4).
- The **strategy registry** and the nine v1.0 strategies (§5).
- The three **run modes** (`real` / `surrogate` / `surrogate_assisted_real`) (§6).
- The **constraint** evaluation reusing `cross_field_constraints` + `safe_expr` (§7).
- The **stopping-criteria** evaluator (§8).
- **Batch / async coordination** (§9) and **durable resumable state** (§10).
- The **initial-seed** strategy (§11).
- The Job FK `Job.optimisation_study_id`, batch number, and selection rationale (§12.4, §13).

It does **not** own (cross-references only):

- The `study_result` rename / discriminator backfill — `designs/studies-engine.md` §6 (landed first by whichever of ADR-0008/0009 ships first; see §12.6).
- The unified surrogate engine, the Model registry, and `surrogate_model_version` — `designs/surrogate-family.md` §4–§8. `surrogate` and `surrogate_assisted_real` **consume** that registry; they do not define it.
- The active-learning *acquisition primitives* over the surrogate (predicted variance, expected variance reduction) — these live in `matofx.studies.surrogate` per `designs/surrogate-family.md` §4; this doc wraps them as strategies (§5.4).
- The lineage / V&V / KPI view of an optimisation-study job — `designs/data-lineage.md`, `designs/vv-tracking.md`, `designs/kpi-extraction.md` (§20).

---

## 3. The optimisation study

### 3.1 State machine

Per `designs/studies-engine.md` §2, extended with a `pending_start_approval` gate (§14.1):

```
draft ──configure──▶ pending_start_approval ──approved──▶ running ─┬─ converged ──────▶ complete
       (skipped when no                      ╰─rejected──▶ cancelled │ ├─ budget_exhausted ▶ complete
        target tool requires it)                                     │ ├─ target_met ──────▶ complete
                                                                     │ ├─ user_halted ─────▶ cancelled
                                                                     └─┴─ failed (every child fails) ▶ failed
```

`draft` exists because configuring a study is multi-step and the user may save partial progress (DB-only, no disk staging until the first batch queues, per [[feedback_save_db_only]]). `pending_start_approval` is entered at `configure()` **only when the target tool — or any tool in a workflow target (§3.4) — carries the per-tool `optimisation_start_approval_required` flag** (§14.1); otherwise `configure` goes straight to `running`. The terminal-reason values (`converged` / `budget_exhausted` / `target_met` / `user_halted`) are recorded in `stop_reason` for the convergence-trace header and the audit payload; `state` collapses to `complete` / `cancelled` / `failed`.

A study only reaches `running` once configuration validates (§3.3) **and** any required start-approval is granted — at which point the seed batch (§11) queues and the runtime takes over.

### 3.4 Evaluation target — tool vs workflow

A study optimises over one **evaluation target** (`config.target.kind`):

- **`tool`** (`{tool_alias, tool_version_id}`) — each candidate is one **tool `Job`** under the study (`Job.optimisation_study_id`, §12.2). Factors are the tool descriptor's inputs.
- **`workflow`** (`{workflow_alias, workflow_version_id}`) — each candidate is one full **`WorkflowRun`** (process-chain optimisation, à la optiSLang / modeFRONTIER). The run executes the chain's tools in dependency order; the candidate is scored once the whole run terminates.

For a workflow target:

- **Factors = the workflow's *open* inputs** — inputs not fed by an upstream tool's auto-discovered output→input wiring (§16.4 workflow graph). Inputs that *are* wired from upstream outputs are not free parameters and are excluded from the factor picker.
- **Responses = any output (KPI) of any tool in the chain**, named `{tool_alias, output}` within the workflow (the response spec §4.2 gains a `member` discriminator for the workflow case).
- **Membership** is `WorkflowRun.optimisation_study_id` (new FK, §12.2) rather than `Job.optimisation_study_id`. The run's child jobs keep their normal scenario/run home; only the run carries the study FK.
- **`study_result` rows** are written per terminated `WorkflowRun`: `inputs_json` = the open-input vector, `outputs_json` = the union of the named responses pulled from the run's child-job outputs.

The outer loop (§4) is **target-agnostic** — only the "submit a candidate" and "collect results" primitives differ (submit a `Job` vs a `WorkflowRun`; read one job's outputs vs the run's child outputs). Strategies, constraints, stopping, durable state, and the convergence/acquisition surfaces are identical. Surrogate run modes (§6) for a workflow target fit the surrogate over the open-input → response mapping exactly as for a tool target.

### 3.2 Configuration shape

A study row carries a single frozen `config_json` capturing the full multi-step form. Shape:

```jsonc
{
  "target": { "kind": "tool", "tool_alias": "heat_sink", "tool_version_id": 42 },
  "factors": [
    { "name": "fin_height",  "type": "float", "unit": "mm", "min": 5.0, "max": 40.0, "step": null },
    { "name": "fin_count",   "type": "int",   "unit": "",   "min": 4,   "max": 60,   "step": 1 },
    { "name": "material",    "type": "categorical", "choices": ["al", "cu", "steel"] }
  ],
  "responses": [
    { "name": "mass",            "spec": { "kind": "scalar" },                          "goal": "minimise" },
    { "name": "max_temperature", "spec": { "kind": "scalar" },                          "goal": "minimise" },
    { "name": "pressure_drop",   "spec": { "kind": "csv_column", "file": "dp.csv",
                                            "column": 1, "aggregate": "last" },          "goal": "track" }
  ],
  "constraints": [
    { "kind": "cross_field", "expr": "fin_height * fin_count <= 1200",
      "error_message": "Total fin length exceeds the manufacturable envelope." },
    { "kind": "output",      "response": "max_temperature", "op": "<=", "value": 85.0 }
  ],
  "strategy": "bo_ei",
  "run_mode": "real",
  "surrogate_model_version_id": null,
  "mop": { "enabled": false, "cop_cutoff": 0.01 },
  "batch_size": 4,
  "batch_mode": "synchronous",
  "concurrency_cap": 4,
  "seed": { "kind": "lhs", "n": 25 },
  "stopping_criteria": [
    { "kind": "max_runs",    "value": 200 },
    { "kind": "target",      "response": "mass", "op": "<=", "value": 5.0 },
    { "kind": "convergence", "metric": "best_so_far", "window": 15, "epsilon": 1e-3 },
    { "kind": "wall_clock",  "hours": 168 }
  ],
  "random_seed": 12345
}
```

- **`target`** — `tool` or `workflow` (§3.4); for `workflow`, `factors` are the chain's open inputs and each response names `{member, output}`.
- **`factors`** — drawn from the pinned tool descriptor's inputs (for a workflow target, the chain's open inputs); bounds default to the descriptor box constraints and may be tightened (never widened past the descriptor). Continuous (`float`), integer (`int`), and categorical inputs are supported (Optuna-native); other descriptor input kinds are excluded from the factor picker with a plain-English note.
- **`responses[].goal ∈ {minimise, maximise, track}`** — `track` responses are recorded and plotted but not optimised (useful for monitoring a secondary metric). At least one `minimise`/`maximise` goal is required to leave `draft`.
- **`random_seed`** — captured so a study is reproducible; every strategy seeds its RNG from it. Surfaced read-only on the detail page.

### 3.3 Configure-time validation

Leaving `draft` runs a single `validate_config(config, descriptor)` gate that raises a structured `OptimisationConfigError` (→ plain-English HTTP per the house pattern) on the first failure:

1. ≥1 factor; every factor exists in the pinned descriptor; bounds within descriptor box constraints; `int`/`categorical` shapes match the descriptor type.
2. ≥1 response with goal `minimise`/`maximise`; every response resolvable (scalar name exists, or `csv_column` file/column declared, etc.).
3. Chosen `strategy` exists in the registry **and** declares support for the objective shape implied by the responses (`single` if exactly one optimised response, `multi` if ≥2, `info_gain` for the `active_*` strategies — those require no optimised response). The UI filters incompatible combinations up front; this is the server-side backstop.
4. Every `cross_field` constraint compiles under `safe_expr` and references only declared factors (reuses the descriptor wizard's existing compile/cycle check, §16.16).
5. Every `output` constraint names a declared response.
6. `max_runs` present (mandatory budget). `convergence.metric ∈ {best_so_far, hypervolume}`; `hypervolume` only valid when ≥2 optimised responses.
7. If `run_mode ∈ {surrogate, surrogate_assisted_real}`: `surrogate_model_version_id` set, the referenced version is visible to the caller (§14), its `tool_alias` matches the study's tool, and its factor set is a superset of the study factors. A surrogate whose source tool is *itself* a synthesised surrogate-tool is rejected (surrogate-of-a-surrogate, mirroring `designs/surrogate-family.md` §7.5).
8. `batch_size ≥ 1`; `concurrency_cap ≥ 1`; `seed.n ≥ 1` for seeded modes (§11).
9. **Workflow target (§3.4):** the workflow + version exist and are visible; every factor is an *open* input of the chain (not upstream-wired); every response names a real `{member, output}` in the chain.
10. **Strategy/run-mode sanity:** gradient/local strategies (`nelder_mead`, `slsqp`) warn when paired with `run_mode='real'` on an expensive tool (finite-difference gradients burn evaluations — they are intended for `surrogate` mode, cheap tools, or as a polish stage); `mop.enabled` requires a surrogate run mode (§5.7).

---

## 4. The outer-loop runtime

A new package `matofx/studies/optimisation/` holds the engine. The runtime (`runtime.py`) is a long-lived async coroutine **hosted in the existing runner daemon** — the same process that already runs cohort refresh, the GC sweeper, and the user-disable sweeper (no new daemon, per ADR-0009 §2 / studies-engine §5). A bounded pool of optimisation-study tasks runs concurrently; backpressure is the per-study `concurrency_cap` and the shared study lane.

### 4.1 One batch tick

```
loop(study):
    state  ← rehydrate(study)                  # §10 — GP / sampler / history / convergence buffer
    strat  ← registry.get(study.strategy).bind(study, state)

    while True:
        # 1. ASK — strategy proposes the next K candidates
        cands ← strat.ask(n=study.batch_size)          # list[dict[factor → value]]

        # 2. FILTER — selection-time constraint check (§7)
        valid ← [c for c in cands if constraints_ok(c)] # box + cross_field; black-box deferred to tell
        log + audit dropped candidates; if all dropped, ask again (bounded retries) or fail-stop

        # 3. SUBMIT — queue valid candidates as study children
        children ← [queue_child(study, c, batch=n, rationale=strat.rationale(c)) for c in valid]

        # 4. WAIT — async until this batch terminates (honours concurrency_cap + batch_mode §9)
        await batch_complete(children)

        # 5. PULL — completed children already wrote study_result via the post-job hook
        X, y, feasible ← collect_results(children)      # from study_result rows

        # 6. TELL — update the strategy's internal model (idempotent on identical (X,y))
        strat.tell(X, y, feasible)
        if run_mode == surrogate_assisted_real: refit_attached_surrogate(study, X, y)  # §6.3

        # 7. PERSIST — serialise optimiser state + append convergence trace
        persist_state(study, strat.dump())              # §10
        append_convergence_point(study, best_so_far_or_hypervolume(strat))

        # 8. STOP? — evaluate stopping criteria in order (§8)
        reason ← evaluate_stopping(study, strat)
        if reason: finish(study, reason); return        # → complete / cancelled
        if user_requested_halt(study): finish(study, "user_halted"); return
```

`batch_complete` awaits a future tied to each child's terminal-state transition (the runner already emits these). It does **not** poll tightly — it subscribes to the runner's existing completion signal and falls back to a coarse DB poll as a safety net (mirrors the parametric-study batch wait).

The tick is **target-agnostic** (§3.4): for a `tool` target `queue_child` queues a `Job` and `collect_results` reads that job's outputs; for a `workflow` target it queues a `WorkflowRun` and reads the named `{member, output}` responses from the run's child outputs, with the future resolving on the run's terminal state. Everything between is identical.

### 4.2 Strategy interface (the `ask`/`tell` contract)

Every strategy implements one Protocol so the loop is strategy-agnostic:

```python
class Strategy(Protocol):
    objective_shapes: ClassVar[frozenset[Literal["single", "multi", "info_gain"]]]
    supports_async_tell: ClassVar[bool]          # gates §9 async batch
    needs_seed: ClassVar[bool]                   # gates §11 seed batch

    def bind(self, study: OptimisationStudy, state: StrategyState) -> "Strategy": ...
    def ask(self, n: int) -> list[Candidate]: ...
    def tell(self, X: list[Candidate], y: list[ResponseVector], feasible: list[bool]) -> None: ...
    def rationale(self, c: Candidate) -> dict: ...   # e.g. {"acquisition_value": 0.42, "proposed_by": "bo_ei_batch_7"}
    def dump(self) -> StrategyState: ...              # §10 — JSON-serialisable or blob-backed
    def best_so_far(self) -> ResponseVector | None: ...
    def pareto_front(self) -> list[ResponseVector]: ...   # multi-obj; [] for single
```

`tell` is **idempotent on identical `(X, y)`** — re-feeding a point that crash-recovery already recorded is a no-op (ADR-0009 §8). This is what makes mid-batch crash recovery safe.

---

## 5. Strategy registry

Strategies register against a closed catalogue in `matofx.studies.optimisation.strategies` (ADR-0009 §3). Adding a strategy = a new module + a `register()` call; the submit form's strategy dropdown reads the registry and filters by `objective_shapes`.

| Strategy | Library | Shapes | `supports_async_tell` | Notes |
|---|---|---|---|---|
| `bo_ei` | Optuna (BSD-3) | `single` | ✅ | TPE/GP + Expected Improvement; the default for expensive jobs |
| `bo_ucb` | Optuna | `single` | ✅ | Upper-Confidence-Bound; more exploration than EI |
| `nsga2` | pymoo (Apache-2) | `multi` | ❌ | Generational; Pareto-front discovery (2–3 objectives) |
| `moea_d` | pymoo | `multi` | ❌ | Decomposition; Pareto for >3 objectives |
| `hyperband` | Optuna | `single` | ✅ (native) | Budget-aware; mixes shallow + deep evaluations |
| `random` | `numpy.random` | `single`, `multi` | ✅ | Baseline; verifies instrumentation |
| `grid` | built-in | `single`, `multi` | ✅ | Exhaustive coarse grid; smoke-test runs |
| `cmaes` | Optuna `CmaEsSampler` / pymoo | `single` | ✅ | Robust global; the workhorse when BO stops scaling (>~20 factors) |
| `de` | pymoo / `scipy.optimize` | `single` | ✅ | Differential evolution; robust on multimodal / noisy |
| `pso` | pymoo | `single` | ❌ | Particle swarm; familiar, global |
| `ga` | pymoo | `single` | ❌ | Single-objective genetic algorithm |
| `nsga3` | pymoo | `multi` | ❌ | Many-objective (>3); reference-direction NSGA-III |
| `dual_annealing` | `scipy.optimize` | `single` | ✅ | Simulated-annealing baseline |
| `nelder_mead` | `scipy.optimize` | `single` | ✅ | Derivative-free local simplex; **surrogate-mode / cheap-tool polish** |
| `slsqp` | `scipy.optimize` | `single` | ✅ | Gradient (finite-diff) constrained local; surrogate-mode refinement |
| `active_max_variance` | surrogate engine (ADR-0008) | `info_gain` | ✅ | Active learning — picks max predicted-variance point |
| `active_max_uncertainty_reduction` | surrogate engine | `info_gain` | ✅ | Active learning — max integrated posterior-variance reduction (sharper, slower) |

### 5.1 Optuna-backed strategies (`bo_ei`, `bo_ucb`, `hyperband`)

Wrap an `optuna.Study` whose search space is built from `config.factors` (`suggest_float` / `suggest_int` / `suggest_categorical`, with `step` honoured). `ask` calls `study.ask()` K times → K `optuna.Trial`s; `rationale` reads the sampler's acquisition value where exposed. `tell` calls `study.tell(trial, values)` and marks infeasible trials with `optuna.TrialState.PRUNED` + a `constraint` user-attr (constraint-aware sampler per ADR-0009 §5). The acquisition map (§16) reads the live sampler.

### 5.2 pymoo-backed strategies (`nsga2`, `moea_d`)

pymoo's `ask`/`tell` `Problem` is driven externally: the `Strategy` wrapper holds the `Algorithm` object, calls `algorithm.ask()` for a population (sliced to `batch_size` when the population exceeds it across batches), and `algorithm.tell(F=objectives, G=constraint_violations)` on completion. Black-box (output) constraints feed pymoo's native constraint domination via `G`. Generational by nature → `supports_async_tell=False` (a generation must complete before the next).

The single-objective pymoo algorithms (`de`, `pso`, `ga`, `cmaes`) and the many-objective `nsga3` reuse this **same wrapper** — only the `Algorithm` object differs. `cmaes` is also available through Optuna's `CmaEsSampler` (which supports async `tell`); the recommendation wizard (§5.6) picks the backend, defaulting to the Optuna path when async batching is wanted.

### 5.3 Built-in baselines (`random`, `grid`)

`random` draws uniform-in-box (respecting `int`/`categorical`) and rejects cross-field violators before yielding. `grid` enumerates a coarse Cartesian grid (per-factor level count from `config`, default 5 for floats) and yields the next K unused cells; stops naturally when the grid is exhausted (a `grid_exhausted` stop reason if it precedes `max_runs`). Both are pure-`numpy`, no external dep, and exist to validate instrumentation and provide a control.

### 5.4 Active-learning strategies (`active_*`)

These do **not** optimise a response — they minimise surrogate uncertainty (ADR-0009 §3). They wrap the surrogate engine's acquisition primitives (`designs/surrogate-family.md` §4): `active_max_variance` picks the candidate maximising predicted variance; `active_max_uncertainty_reduction` maximises expected integrated posterior-variance reduction. `objective_shapes={info_gain}` — the configure form hides response-goal pickers and instead asks "which surrogate are you improving?" (the attached `surrogate_model_version_id`, required). Each `tell` refits the surrogate on the accumulated points (full retrain — no online learning, per §6.3 + ADR-0009 §14). The seed batch (§11) still applies — the strategy needs *some* data to compute variance over.

### 5.5 scipy-backed local & annealing strategies

`nelder_mead` and `slsqp` wrap `scipy.optimize.minimize`; `dual_annealing` wraps `scipy.optimize.dual_annealing`; `de` is also exposed via `scipy.optimize.differential_evolution` (all already in stack — `de` already backs the surrogate "set-to-optimum" optimiser, surrogate §10). All single-objective. Gradient/local methods are **finite-difference** under a black-box tool, so each gradient costs `dim+1` evaluations — wasteful against an expensive `real` tool. They are therefore intended for **`surrogate` run mode** (instant evaluations), **cheap tools**, and as a **polish stage** refining a global optimum found by BO / CMA-ES / EA (the wizard, §5.6, proposes them as refinement, not a from-scratch strategy on expensive tools).

### 5.6 Algorithm-recommendation wizard ("Navigator")

The single biggest usability lever (the optiSLang Navigator analogue). `recommend_strategy(config) -> StrategyRecommendation` proposes a strategy + run mode + batch size from problem characteristics; the create wizard (§16) shows a **"Recommended"** badge on the suggested option with a one-line rationale. Heuristics (deterministic, documented, fully overridable):

| Signal | Steer |
|---|---|
| 1 objective, ≤~20 continuous factors, expensive `real` tool | `bo_ei` (sample-efficient) |
| 1 objective, >~20 factors, or integer/mixed | `cmaes` (scales past BO) |
| ≥2 objectives | `nsga2` (2–3) / `nsga3` or `moea_d` (>3) |
| cheap tool or `surrogate` mode | `de` global, then `nelder_mead` / `slsqp` polish |
| "build a good surrogate, not an optimum" | `active_*` (info_gain) |
| variable child runtimes | `hyperband` (async budget-aware) |
| user wants a control / baseline | `random` / `grid` |

The wizard never *forces* a choice — it pre-selects and explains; the full dropdown stays available behind the `objective_shapes` filter (§3.3).

### 5.7 MOP/CoP — metamodel of optimal prognosis

optiSLang's signature capability, built on the surrogate family (ADR-0008) rather than a new engine. A MOP study runs in `surrogate` or `surrogate_assisted_real` mode (`config.mop.enabled=true`) with three additions:

1. **Auto-best-metamodel.** Instead of the user picking GP vs polynomial, the engine fits each available surrogate kind on the source data and selects the one with the best cross-validated **Q²** (the existing `SurrogateQuality.q2`, `matofx/studies/surrogate.py`). This is the "optimal prognosis" choice.
2. **Coefficient of Prognosis (CoP) per factor.** CoP is the share of response variance the metamodel explains, attributed per input — computed from the fitted metamodel's **Sobol total-effect indices** (the existing `sobol_indices`, surrogate §10) scaled by the model's overall Q². Surfaced as a CoP bar chart (the "what actually drives this response" view).
3. **Sensitivity-driven variable reduction.** Factors whose CoP is below `mop.cop_cutoff` (default 0.01, operator-tunable) are **frozen at their nominal value** and dropped from the active search space; the optimiser works the reduced space (fewer dimensions → faster convergence — the core MOP/CoP benefit). Pruned factors + the cutoff are recorded on the study and shown in the UI ("3 of 11 factors pruned at CoP < 0.01"); the user can override the auto-prune per factor.

MOP/CoP is a **mode flag** layered on a surrogate run mode, not a separate strategy — any single-objective strategy can run inside it. It reuses the surrogate engine's fit / predict / Sobol primitives entirely; **no new dependency**.

---

## 6. Run modes

`config.run_mode` is orthogonal to the strategy (ADR-0009 §4):

### 6.1 `real`

Every candidate submits a real tool job under the study (`Job.optimisation_study_id`). The bread-and-butter for expensive CFD/FEA. `collect_results` reads each child's `study_result` row written by the existing post-job hook.

### 6.2 `surrogate`

Every candidate is evaluated against the pinned `surrogate_model_version_id` **in-process** — no child job, no queue. The loop calls `load_surrogate_model_version(...).predict([candidate])` (the same runtime entry the synthesised orchestrator uses, `designs/surrogate-family.md` §7.2) and writes a `study_result` row with `source_kind='optimisation'` directly, marked `evaluated_on_surrogate=true`. Cheap, instant — for "what does the surrogate think is optimal?" sanity checks. No seed batch (the surrogate is the truth; §11). Results carry a banner that accuracy is bounded by the surrogate's `quality`.

### 6.3 `surrogate_assisted_real` (the powerful one)

Each batch: the strategy proposes K candidates *informed by the surrogate's predictions + uncertainties*; the K run as **real** jobs; on completion the surrogate **refits** with the new points and the strategy uses the refit surrogate for the next batch (ADR-0009 §4). State-of-the-art for sample-efficient optimisation under high evaluation cost.

- Requires `surrogate_model_version_id` (validated §3.3); hard-depends on the Model registry (`designs/surrogate-family.md` §5–§6).
- The refit is a **full retrain** via `ModelService.refit` against the study's accumulating points (no online learning — ADR-0009 §14 / surrogate §8.4). Each refit mints a new `surrogate_model_version` in `publish_state='draft'` owned by the study (not auto-published).
- The pinned `surrogate_model_version_id` advances to each new draft within the study's working set; the **"Publish current surrogate"** handoff (§16.4) forks the current draft into the registry as a real candidate version via the normal publish gate.
- Refit cost is bounded by the same `surrogate_field_fit_memory_gb` budget (surrogate §4.4); a refit that would exceed the per-batch wall-clock budget logs a diagnostic and the batch proceeds on the prior surrogate (strategy still gets the real `tell`).

---

## 7. Constraints

Three kinds, all expressible through the descriptor's existing `cross_field_constraints` + `safe_expr` DSL (ADR-0009 §5 / §16.16) — **no new DSL**.

| Kind | Where evaluated | Effect |
|---|---|---|
| **Box** (`factor min/max`) | In the search-space construction | The strategy never proposes out-of-box; read directly from `config.factors` |
| **Cross-field** (`a + b <= 100`) | **At selection time** (`ask` → filter, §4.1 step 2) | Violators are dropped before enqueue (never run); pymoo gets them as `G` so its sampler avoids the region; Optuna gets a constraint-aware sampler + pruned trial |
| **Black-box / output** (`output_x <= value`) | **At `tell` time** (after the job runs — the output isn't known until then) | Violators recorded with `feasible=false` on the `study_result` row; BO modes apply a penalty; NSGA-II uses constraint domination |

`constraints_ok(candidate)` (the §4.1 filter) evaluates box + cross-field only; output constraints can't be known pre-run. The study-configuration UI provides a constraint-entry editor wrapping the same `safe_expr` compile + cycle check the descriptor wizard already uses. Feasibility status (`feasible` / `infeasible`) is written to every `study_result` row and surfaced in the matrix + convergence views (infeasible points greyed; best-so-far ignores infeasible points).

---

## 8. Stopping criteria

`config.stopping_criteria` is an ordered list; **any** kind firing stops the study; evaluated after every batch in declared order (ADR-0009 §6). `evaluate_stopping(study, strat)` returns the first matching reason or `None`.

| Kind | Fields | Fires when |
|---|---|---|
| `max_runs` | `value` | Completed children (real) or evaluations (surrogate) ≥ `value`. **Mandatory** — no unbounded studies |
| `target` | `response`, `op`, `value` | A feasible best-so-far satisfies `best <op> value` for the named response |
| `convergence` | `metric ∈ {best_so_far, hypervolume}`, `window`, `epsilon` | The metric's improvement over the last `window` batches is `< epsilon` |
| `wall_clock` | `hours` | Wall-clock since first batch ≥ `hours`. Runaway safety net |

- Single-objective `convergence` tracks `best_so_far`; multi-objective uses the **hypervolume-indicator delta** over the window (ADR-0009 §6) against a reference point fixed at the seed batch (the per-response worst seen in the seed, nudged outward 10%).
- `max_runs` counts *terminal* children — a failed child counts toward the budget (so a tool that always fails can't loop forever); the all-children-failed path transitions to `failed` (studies-engine §2).
- **Manual halt** is always available regardless of criteria: the user clicks Stop → no new batches queue, in-flight children complete, `state → cancelled`, `stop_reason='user_halted'` (ADR-0009 §6).

---

## 9. Batch / async coordination

Two modes (ADR-0009 §7), both honouring the study lane (low-priority queue, ADR-0001 / studies-engine §5) so an optimisation can't starve interactive users.

- **`synchronous`** (default) — submit K, wait for all K to terminate, `tell`, repeat. Simplest; fine when children take similar wall-clock. `batch_complete` awaits all K.
- **`asynchronous`** — start the next candidate as soon as a slot opens under `concurrency_cap`; `tell` incrementally as each completes. Better wall-clock for variable child runtimes. Only offered when `strategy.supports_async_tell` (Hyperband natively; BO via async `tell`; the baselines). pymoo strategies force `synchronous` (generational). The configure form disables `asynchronous` for incompatible strategies with a tooltip.

`concurrency_cap` bounds in-flight children per study; the runner's existing lane scheduler enforces the global ceiling. The runtime never holds more than `concurrency_cap` un-`tell`'d children.

---

## 10. Durable resumable state

The optimiser's in-memory state (search history, GP / sampler, acquisition cache, hypervolume reference point, convergence-window buffer) serialises after **every** batch (ADR-0009 §8 / studies-engine §5):

- **JSON** into `optimisation_study.optimiser_state_json` when the serialised state ≤ **1 MB** (typical for the baselines, small BO runs, pymoo populations).
- **Disk blob** under `<mount>/studies/<env>/opt_<id>/state.pkl` when it would exceed 1 MB (GP samplers with >100 observations). The row stores `optimiser_state_blob_path` + a sha256; load verifies it (mirrors the surrogate bundle integrity check, surrogate §16.2). `joblib` only — never raw `pickle.load` in the web process; the runner-hosted runtime owns deserialisation.

**Runner restart** rehydrates from the last completed batch and continues. **Crash mid-batch:** in-flight children either complete on restart (the runner already recovers in-flight jobs) or are re-queued; when all K terminate, `tell` runs — and because `tell` is idempotent on identical `(X, y)`, no double-counting (§4.2). **Corrupt state** raises `OptimiserStateCorruptError` (a clear ops error) rather than continuing on bad state (studies-engine §5); the study parks in `running` with a surfaced error banner and an admin "reset to last good batch" affordance (the convergence trace + `study_result` rows are the durable source of truth — a study can always be reconstructed from completed children).

The convergence trace itself lives in `optimisation_study.convergence_trace` as an append-only JSON list of `{batch, best_so_far | hypervolume, n_feasible, n_total, wall_clock_s}` — cheap, always JSON (never blob), and the source for the Convergence tab even when the heavy optimiser state is blob-backed.

---

## 11. Initial seed

Pure BO with no initial data is unstable (a GP fit on <5 points is wildly uncertain), so every seeded study **must** start with a seed batch (ADR-0009 §13):

| Run mode / strategy | Seed |
|---|---|
| `real` / `surrogate_assisted_real` | LHS of `max(10, 3 × dim)` points (operator-tunable via `config.seed.n`), then the strategy takes over |
| `active_*` | Same LHS seed (the active strategy needs data to compute variance over) |
| `surrogate` run mode | **No seed** (the surrogate is the truth; BO operates on its predictions from point one) |
| `grid` / `random` | No separate seed (they *are* space-filling) |

The seed uses `scipy.stats.qmc.LatinHypercube` (already in stack), scaled into each factor's box and snapped for `int` / `categorical` factors. Seed children carry `rationale={"proposed_by": "lhs_seed"}` and `batch=0`. The configure form fires plain-English guidance from `seed.n` ("This will run ~25 jobs before the optimiser starts learning").

---

## 12. Data model + migration

New package `matofx/studies/optimisation/`. Migrations chain off the studies-engine `study_result` rename (§12.6); cumulative + idempotent; PK pattern `sa.BigInteger().with_variant(sa.Integer(), "sqlite")`; optimistic lock via `version`.

### 12.1 `optimisation_study`

| col | type | notes |
|---|---|---|
| `id` | PK | |
| `project_id` | FK → `project.id` RESTRICT | home project; ACL source (studies-engine §7) |
| `name` | str(128) | display label; not unique |
| `description` | text | |
| `target_kind` | str(16) | CHECK `IN ('tool','workflow')` (§3.4) |
| `tool_alias` | FK → `tool.alias` RESTRICT, nullable | set iff `target_kind='tool'` |
| `tool_version_id` | FK → `tool_version.id` RESTRICT, nullable | pinned at configure; set iff `target_kind='tool'`; mid-study drift rejected |
| `workflow_alias` | FK → `workflow.alias` RESTRICT, nullable | set iff `target_kind='workflow'` |
| `workflow_version_id` | FK → `workflow_version.id` RESTRICT, nullable | pinned at configure; set iff `target_kind='workflow'` |
| `config_json` | JSON | the frozen §3.2 config (target / factors / responses / constraints / strategy / run_mode / mop / stopping) |
| `pinned_refs_json` | JSON / null | library/material/surrogate version pins, written at first child queue (studies-engine §4) |
| `state` | str(16) | CHECK `IN ('draft','pending_start_approval','running','complete','cancelled','failed')` |
| `start_approver_user_id` | FK → `user.id`, nullable | nominated approver when the start gate fires (§14.1) |
| `stop_reason` | str(24) / null | `converged` / `budget_exhausted` / `target_met` / `user_halted` / `grid_exhausted` / `all_children_failed` |
| `optimiser_state_json` | JSON / null | §10 — inline state ≤1 MB |
| `optimiser_state_blob_path` | str / null | §10 — disk-blob path when state >1 MB |
| `optimiser_state_sha256` | str(64) / null | integrity check for the blob |
| `convergence_trace` | JSON | append-only `[{batch, metric, n_feasible, n_total, wall_clock_s}]` |
| `current_best_json` | JSON / null | best-so-far (single) or Pareto-front summary (multi) for the list view |
| `last_batch_number` | int | monotonic; 0 = seed |
| `halt_requested` | bool | set by the Halt control; read at the top of the loop |
| `state_changed_at` / `state_changed_by` | | studies-engine common columns |
| `created_at` / `created_by_user_id` | | |
| `archived_at` / `archived_by_user_id` | nullable | no-delete (D20 + ADR-0002) |
| `version` | int | optimistic lock |

Indexes: `(project_id, state)`, `(tool_alias)`, `(state)` for the runtime's "find runnable studies" scan.

### 12.2 `Job.optimisation_study_id` (new nullable FK)

```python
op.add_column("job", sa.Column(
    "optimisation_study_id",
    sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
    sa.ForeignKey("optimisation_study.id", ondelete="RESTRICT"),
    nullable=True,
))
op.create_index("ix_job_optimisation_study_id", "job", ["optimisation_study_id"])
```

The **same FK + index** is added to `workflow_run` for the workflow target (§3.4): `WorkflowRun.optimisation_study_id` carries the run-level membership. A candidate is either a `Job` (tool target) or a `WorkflowRun` (workflow target), never both.

### 12.3 `Job` home CHECK widening (1-of-2 → 1-of-3)

The portable CHECK requiring one of `scenario_id` / `parametric_study_id` non-null widens to one of `scenario_id` / `parametric_study_id` / `optimisation_study_id` (studies-engine §3). Same mechanics as the file-library / materials approval-target widenings — drop + recreate the named CHECK on both engines; no data migration (existing rows already satisfy the looser constraint). The `UNIQUE(scenario_id, tool_alias)` (migration 0017) keeps treating NULL `scenario_id` as distinct, so optimisation children (`scenario_id=NULL`) never collide (studies-engine §3).

**Workflow-target children are unaffected by this CHECK.** Their child `Job` rows keep their normal run/scenario home (§3.4) — the study FK lives on the `WorkflowRun`, not the child `Job` — so a workflow-target candidate adds no Job-home-CHECK pressure.

### 12.4 Per-child optimisation metadata

The selection rationale + batch number live on the child `Job` in a nullable `optimisation_metadata_json` column (`{batch, proposed_by, acquisition_value?, feasible?}`, ADR-0009 §9) — added in the same migration. This drives the convergence-trace point colouring and the lineage edge label; it is **not** on the hot `Job` path (nullable, read only by the study surfaces).

### 12.5 `study_result` reuse

Optimisation children write `study_result` rows with `source_kind='optimisation'`, `source_id=optimisation_study.id` via the **existing** post-job hook (studies-engine §6) — no new write path. `surrogate`-mode evaluations write the same row shape directly from the runtime with `evaluated_on_surrogate=true` (a new nullable boolean on `study_result`, defaulted false; the only `study_result` column this doc adds, and it is shared-harmless for the other source kinds). Feasibility (`feasible`) likewise lands on the `study_result` row (§7).

### 12.6 Shared `study_result` rename (cross-ADR)

The `parametric_study_result → study_result` rename + `source_kind`/`source_id` discriminator is a **shared cost with ADR-0008** (studies-engine §6). Whichever of ADR-0008 / ADR-0009 ships first owns that migration; the other consumes it. Per `designs/surrogate-family.md` §5.6 + §17 the surrogate build assumes it ships first (its `surrogate_assisted_real` dependency forces the Model registry earlier). **This doc therefore treats the rename as a prerequisite** and does not re-own it; if scheduling flips, ADR-0009 Phase 1 absorbs the rename instead (the migration body is identical). The `evaluated_on_surrogate` + `feasible` columns on `study_result` are added by **this** ADR's first migration regardless of ordering.

### 12.7 Start-approval: per-tool flag, capability, approval target

The compute-cost start gate (§14.1) adds, in this ADR's migration:

- **`tool.optimisation_start_approval_required`** — a new boolean column on the tool table (default `false`), set per tool by an admin (mirrors the existing per-tool approval-policy + email columns, §16.15). A study whose target tool — or, for a workflow target, *any* tool in the chain — has this flag set routes through the start gate.
- **`optimisation_approver`** capability role + an `OPTIMISATION_APPROVE` permission (ADR-0005 pattern; seeded by the migration, no assignment by default). The eligible-approver pool for the start gate; the submitter nominates one (≠ self).
- **`approval_request.target_kind='optimisation_study_start'`** — one new literal + the exactly-one-of CHECK bump (same one-line mechanics as the surrogate-family / V&V target widenings). The directed-approver machinery in `matofx.approvals` is reused wholesale.

Running and configuring studies still need no new top-level verb — `studies.run` + project ACL govern those (ADR-0009 §11). The "Publish current surrogate" handoff (§16.4) still routes through the **surrogate family's** model-publish gate (`surrogate_model_version` target, `designs/surrogate-family.md` §6) — unchanged.

---

## 13. Job pinning + provenance

Every optimisation-study child carries (ADR-0009 §9):

- `Job.optimisation_study_id` (§12.2).
- The same tool-version / library-version / material-version / surrogate-version pinning the existing study children carry (studies-engine §4 — written to the study's `pinned_refs_json` at first child queue).
- `Job.optimisation_metadata_json` — batch number + selection rationale (§12.4).
- For `surrogate_assisted_real`, `Job.surrogate_model_version_id` (the surrogate-family lineage tag, surrogate §5.4 / §9.1) records *which* surrogate version informed the proposal — so lineage can show the assist edge.

ADR-0010 treats `optimisation_study` as another root node in the provenance graph (§20.1), reusing the parametric-study lineage shape.

---

## 14. Service surface — `matofx.studies.optimisation.OptimisationService` (async, audited)

Configuration / lifecycle (the runtime in §4 is a separate runner-hosted module, not a request-path service):

- `create_draft(project_id, name, tool_alias, tool_version_id)` — mint a `draft`; audit `study.optimisation.created`.
- `update_draft(study_id, config_patch)` — merge a step's config into `config_json`; DB-only (no disk), optimistic-locked; per [[feedback_save_db_only]].
- `configure(study_id, user)` — run `validate_config` (§3.3); on pass, freeze config; if the start gate applies (§14.1) transition `draft → pending_start_approval` and raise a directed approval request, else `draft → running`, write `pinned_refs` lazily on first queue, hand the study to the runtime; audit `study.optimisation.configured`.
- `approve_start(study_id, approver_user)` / `reject_start(study_id, approver_user)` — the nominated `optimisation_approver` resolves the start gate; approve → `running` (seed batch queues); reject → `cancelled`. Audit `study.optimisation.start_approved` / `.start_rejected`.
- `estimate_cost(study_id)` — campaign footprint (`max_runs` children + seed; per-child resource profile → est. core-/GPU-hours) shown in the approval request and the create wizard (§14.1).
- `request_halt(study_id, user)` — set `halt_requested=True`; the loop honours it at the next top-of-batch; audit `study.optimisation.halt_requested`.
- `pause(study_id)` / `resume(study_id)` — the runtime checks a `paused` flag and stops queuing new batches without ending the study (distinct from halt — resumable); audited.
- `extend_budget(study_id, additional_runs)` — bump the `max_runs` value in `config_json.stopping_criteria`; only while `running`; audit `study.optimisation.budget_extended` (ADR-0009 §10).
- `change_strategy(study_id, new_strategy)` — mid-run strategy swap; validates objective-shape compatibility; warns about implications (the prior strategy's internal model is discarded — the new strategy re-`tell`s from the durable `study_result` history); audit `study.optimisation.strategy_changed`.
- `publish_current_surrogate(study_id, user, **publish_opts)` — `surrogate_assisted_real` only; forks the study's current surrogate draft into the registry via `ModelService.publish_version` (§16.4); audit `study.optimisation.surrogate_published`.
- `archive(study_id)` — `state` keeps its terminal value; sets `archived_at`; never hard-deletes.
- `list_for_project(project_id, user)` / `get(study_id, user)` — read side, ACL-filtered (studies-engine §7).

Runtime-internal (in `matofx.studies.optimisation.runtime`, not exposed via API): `run_batch`, `collect_results`, `evaluate_stopping`, `persist_state`, `rehydrate`, `recover_in_flight`.

Permission gates: read = Viewer+ on the project; configure / edit / halt / pause / extend / change-strategy = Member+ on the project; `approve_start` / `reject_start` = the nominated `optimisation_approver` (§14.1); bulk-halt via admin `live_jobs.cancel` (ADR-0009 §11 / studies-engine §7). Every method audits `study.optimisation.<verb>` per the studies-engine §8 namespace.

### 14.1 Start-approval gate (compute-cost guard)

Optimisations can queue hundreds of expensive children, so a **per-tool toggleable** gate lets operators require sign-off *before the campaign starts*. It is **separate from and additional to** a tool's own per-job approval policy (§16.15).

- **Trigger — per tool.** An admin sets `tool.optimisation_start_approval_required` on a high-cost tool (CFD/FEA). At `configure()`, if the target tool — or *any* tool in a workflow target (§3.4) — has the flag, the study enters `pending_start_approval`; otherwise it runs immediately.
- **Directed approver.** The submitter nominates one approver from the `optimisation_approver` capability pool, **minus self** (no self-approval). The request is a `matofx.approvals` directed request with `target_kind='optimisation_study_start'` carrying the **cost estimate** (`estimate_cost`, §14) so the approver decides on the real footprint ("up to 200 children + ~25 seed; est. *N* core-hours").
- **Outcome.** Approve → `running`, the seed batch queues (audit `study.optimisation.start_approved`). Reject → `cancelled`, no children ever queue (audit `study.optimisation.start_rejected`).

**Interaction with the tool's own approval policy (architect ruling 2026-06-01):**

- **Per-child *pre*-approval is subsumed.** Granting the start gate authorises the whole campaign, so the study's children do **not** each re-trigger the tool's `pre` gate — otherwise a 200-run optimisation would demand 200 approvals. (A study whose tool is `approvals='pre'` but whose per-tool start flag is *off* still gates per child — the flag is the operator's lever to convert that into one campaign-level approval.)
- **Per-child *post*-approval is waived within the study; only the final result is gated.** For a tool with `post` / `both` policy, individual children's outputs are not held — they feed the optimiser internally. Instead the study's **final best (single-obj) / Pareto set (multi-obj)** enters the post-approval / output-lockdown flow once, before those results are released or exported. This keeps the loop unblocked while preserving a release checkpoint on what the user actually consumes.

---

## 15. API surface

All audited, all `*Error` → plain-English HTTP, all behind a feature-gated router absent when `Feature.OPTIMISATION_FRAMEWORK` is off (404-when-off, the per-ADR pattern — file-library / materials / VV / KPI / surrogate-family each use their own flag).

```
GET    /api/projects/{pid}/optimisation-studies        list (ACL-filtered)
POST   /api/projects/{pid}/optimisation-studies        create draft
GET    /api/optimisation-studies/{sid}                 detail (config + state + best-so-far)
PATCH  /api/optimisation-studies/{sid}                 update draft config (optimistic lock)
POST   /api/optimisation-studies/{sid}/configure       validate + launch (draft → running | pending_start_approval)
GET    /api/optimisation-studies/{sid}/cost-estimate   campaign footprint for the start gate + wizard (§14.1)
POST   /api/optimisation-studies/{sid}/start-approve    nominated approver grants the start gate (→ running)
POST   /api/optimisation-studies/{sid}/start-reject     nominated approver declines (→ cancelled)
POST   /api/optimisation-studies/{sid}/halt            request halt (graceful)
POST   /api/optimisation-studies/{sid}/pause           pause new batches
POST   /api/optimisation-studies/{sid}/resume          resume
POST   /api/optimisation-studies/{sid}/extend          extend max_runs budget
POST   /api/optimisation-studies/{sid}/change-strategy  mid-run strategy swap
POST   /api/optimisation-studies/{sid}/publish-surrogate  fork current surrogate to registry (sa_real only)
POST   /api/optimisation-studies/{sid}/archive         archive (no delete)
GET    /api/optimisation-studies/{sid}/convergence     convergence_trace points (Convergence tab)
GET    /api/optimisation-studies/{sid}/acquisition?fx=&fy=  acquisition contour over a factor pair (BO only)
GET    /api/optimisation-studies/{sid}/results         study_result rows (Results explorer mount)
GET    /api/optimisation-strategies                    registry catalogue (name + objective_shapes + async support)
GET    /api/optimisation-strategies/recommend          wizard recommendation for a draft config (§5.6)
```

The `study_result`-backed `…/results` endpoint reuses the existing Results-explorer read path parameterised by `(source_kind='optimisation', source_id=sid)` (studies-engine §9). The acquisition contour is computed off the live optimiser state via `run.io_bound` (ADR-0009 §10) and is BO-modes-only (404/empty for non-BO strategies).

---

## 16. UI surfaces

| Surface | File | Change |
|---|---|---|
| Studies workspace | `ui/pages/studies.py` | New **Optimisation** tab beside Parametric and Cohorts (studies-engine §9) |
| Create flow | `ui/pages/optimisation_create.py` (new) | Multi-step wizard: **target (tool / workflow, §3.4)** → factors+bounds → responses+goals → constraints → **strategy+run-mode with the "Recommended" badge (§5.6)** → **batch size (prominent) / seed / stopping** → **MOP/CoP toggle (§5.7)** → (when required) **start-approval nomination + cost-estimate panel (§14.1)**. `draft` saved after every step (DB-only) |
| Start-approval | create flow + existing approvals inbox | Approver-nomination step + cost-estimate panel when the target tool requires it (§14.1); the request lands in the nominated `optimisation_approver`'s existing approvals inbox |
| MOP/CoP panel | within study detail | CoP bar chart + pruned-factor list + per-factor auto-prune override (§5.7) |
| Study detail | `ui/pages/optimisation.py` (new) | Mounts the existing Results explorer via `(source_kind='optimisation', source_id)` + two extra tabs (§16.1, §16.2) + run-time controls (§16.3) |
| Convergence tab | within study detail | best-so-far vs iteration (single) / hypervolume vs iteration (multi); feasible/infeasible point colouring; auto-poll every 3 s until terminal (per [[feedback_studies_results_tab_no_teardown]] — refresh hint, no panel teardown while the user reads) |
| Acquisition map | within study detail | 2D acquisition-function contour over a chosen factor pair (BO modes); reveals where the next batch will go |
| Run-time controls | within study detail | Pause / Resume / Halt / Extend-budget / Change-strategy; permission-gated (hidden, not just rejected) |
| Surrogate handoff | within study detail | "Publish current surrogate" (sa_real only) → routes to the surrogate-family publish dialog |
| Nav | `ui/_chrome.py` | Feature-gated "Studies" entry already exists; no new nav row (the Optimisation tab lives inside it) |
| Lineage view | `ui/_cytoscape.py` | `OptimisationStudy` reuses the parametric-study node shape; no new node kind needed beyond the study root (§20.1) |
| Live Jobs | `ui/pages/admin/usage.py` Live Jobs tab | Optimisation-study children appear with the convergence trace as the natural progress indicator (ADR-0009 §15 / §16.18); bulk-cancel already works via `bypass_access` |

### 16.1 Convergence tab

Reads `convergence_trace` (§10). Single-objective: a best-so-far step plot with the `target` line overlaid when a `target` criterion is set. Multi-objective: hypervolume-vs-batch line + a live Pareto-front scatter (2-D / 3-D toggle) pulled from `strat.pareto_front()` via the detail endpoint. Infeasible points are greyed in the scatter. The tab polls every 3 s while `running` and surfaces fresh data as a **Refresh** hint rather than tearing down the panel the user is reading ([[feedback_studies_results_tab_no_teardown]]).

### 16.2 Acquisition-map tab

BO modes only. The user picks a factor pair (other factors held at the current best); the endpoint evaluates the live sampler's acquisition function on a grid and returns a Plotly contour. This is the "where does the optimiser want to look next" diagnostic (ADR-0009 §10) — empty with a plain-English note for non-BO strategies.

### 16.3 Run-time controls

Pause / Resume / Halt / Extend / Change-strategy (§14). **Change-strategy** warns ("Switching strategy discards the current sampler; the new strategy re-learns from the {N} completed runs — no data is lost, but in-flight exploration state is reset") and is audited. **Extend** prompts for the additional run count and confirms the new budget. All gated on Member+ (hidden for Viewers).

### 16.4 "Publish current surrogate" handoff

For a `surrogate_assisted_real` study, at any time the user can fork the in-progress fitted surrogate into the Model registry as a new `surrogate_model_version` (ADR-0009 §10). This calls `OptimisationService.publish_current_surrogate`, which delegates to the surrogate family's `ModelService.publish_version` — entering that ADR's directed-approver gate for a global publish (surrogate §6.3). This doc owns the affordance; the surrogate family owns the gate.

---

## 17. Audit

Per the `designs/studies-engine.md` §8 namespace (`study.optimisation.*`):

| Event | When | Payload (notable) |
|---|---|---|
| `study.optimisation.created` | `create_draft` | study id, tool, project |
| `study.optimisation.configured` | `configure` | study id, target (tool/workflow), strategy, run_mode, factor/response/constraint counts, seed n |
| `study.optimisation.start_approval_requested` | `configure` raises the start gate | study id, flagged tool(s), nominated approver, cost estimate |
| `study.optimisation.start_approved` / `.start_rejected` | approver resolves the start gate | study id, approver |
| `study.optimisation.batch_started` | top of a batch | study id, batch number, K, candidate rationales |
| `study.optimisation.candidate_dropped` | a constraint-violating candidate filtered pre-enqueue | study id, batch, which constraint |
| `study.optimisation.batch_completed` | after `tell` + persist | study id, batch, n_feasible/n_total, best-so-far or hypervolume |
| `study.optimisation.converged` / `.budget_exhausted` / `.target_met` | a stop criterion fires | study id, reason, final best |
| `study.optimisation.halt_requested` / `.halted` | Halt control → graceful stop | study id, requester |
| `study.optimisation.paused` / `.resumed` | Pause/Resume | study id |
| `study.optimisation.budget_extended` | `extend_budget` | study id, old/new max_runs |
| `study.optimisation.strategy_changed` | `change_strategy` | study id, old/new strategy |
| `study.optimisation.surrogate_published` | `publish_current_surrogate` | study id, model version id (delegates to surrogate audit too) |
| `study.optimisation.state_corrupt` | rehydrate sha/parse failure | study id, blob path |

No raw simulation data in audit payloads — best-found summaries and acquisition values are fine; actual `outputs.json` blobs are referenced by job id only (studies-engine §8).

---

## 18. Permissions, licence, feature flag

- **Permission**: no new top-level verb for running studies. Configure / submit / halt / extend / change-strategy = Member+ on the project; view = Viewer+; bulk-halt = admin `live_jobs.cancel` (ADR-0009 §11). The existing `studies.run` + project ACL govern (studies-engine §7). **One new capability** — `optimisation_approver` / `OPTIMISATION_APPROVE` (ADR-0005 pattern) — gates *who may approve* a start request (§14.1); it grants neither running nor configuring studies.
- **Surrogate handoff**: publishing a surrogate inherits the surrogate family's `MODELS_CURATE` gate (surrogate §14) — a Member who lacks `MODELS_CURATE` can run a `surrogate_assisted_real` study but the "Publish current surrogate" control routes them through the directed-approver request rather than direct publish.
- **Licence (§C16)**: no new optimisation studies may be configured or extended in licence read-only mode; viewing existing studies + their results is allowed. In-flight studies at the moment the licence lapses pause gracefully (no new batches) rather than hard-failing — the durable state (§10) means they resume when the licence is restored.
- **Feature flag**: `features.optimisation_framework` (404-when-off). Off ⇒ the Optimisation studies tab, all routes, the runtime's study scan, and the strategy registry are absent. The surrogate-assisted run modes additionally require `features.surrogate_family` on (the registry dependency) — configure rejects `surrogate`/`surrogate_assisted_real` with a plain-English message when the surrogate flag is off.

---

## 19. Dependency & licence ledger

Project-wide rule: no paid/restrictive licences; air-gap-bundled; no Node. Per ADR-0009 §12.

| Dep | Use | Licence | Status |
|---|---|---|---|
| Optuna | BO (`bo_ei`/`bo_ucb`) + Hyperband + TPE; trial store | BSD-3 | **new — vendor + SBOM** (~5 MB) |
| pymoo | Multi-objective (`nsga2`/`moea_d`); constraint domination | Apache-2 | **new — vendor + SBOM** (~3 MB) |
| `scipy.optimize` | gradient bridge / scalar inner solves | BSD-3 | already in stack |
| `scipy.stats.qmc` | LHS initial seed (§11) | BSD-3 | already in stack |
| `scikit-learn` | GP backend for the active-learning acquisition primitives | BSD-3 | already in stack |
| `numpy` | candidate arithmetic, baselines | BSD-3 | already in stack |

Total bundle bump ~12 MB (ADR-0009 §12). Optuna ships type stubs; **pymoo does not** — `Any` via the per-module mypy override already used for statsmodels (ADR-0009 §12). SBOM regen + hashed-lockfile bump is a maintainer step before the first release that includes this ADR's code (same discipline ADRs 0001 / 0006 / 0007 / 0008 used).

**The 2026-06-01 expansion adds no new dependency.** The extra strategies (`cmaes` / `de` / `pso` / `ga` / `nsga3` / `dual_annealing` / `nelder_mead` / `slsqp`) come from Optuna's `CmaEsSampler`, pymoo's algorithm suite, and `scipy.optimize` — all already pulled. MOP/CoP reuses the surrogate family's scikit-learn engine (fit / predict / Sobol). Workflow-target optimisation reuses the existing workflow engine. Net new deps remain **Optuna + pymoo only**.

### 19.1 Alternatives considered

| Candidate | Role it would fill | Why not |
|---|---|---|
| **scikit-optimize (`skopt`)** | single-objective BO (would replace Optuna's `bo_ei`/`bo_ucb`) | **Single-objective only** — no native Pareto front, so it cannot replace pymoo's `nsga2`/`moea_d`; we'd still need a second multi-objective library. And it is **effectively dormant** (long maintenance gaps; known friction on modern numpy/scikit-learn — this stack is on **numpy 2.4**), which is a procurement/SBOM liability for air-gapped defence/aerospace/pharma customers (ROADMAP security-hardening section). Its `ask`/`tell` is thinner than Optuna's, it has no Hyperband/pruning, no constraint-aware sampling (§7), and no type stubs. BSD-3 (licence is a wash). Net: covers one quadrant with a dormant dep. |
| **BoTorch / Ax** | single- + multi-objective BO | State-of-the-art, actively maintained — but **hard-depends on PyTorch** (~hundreds of MB + CUDA/CPU build matrix), which dwarfs the ~12 MB Optuna+pymoo bundle and complicates the air-gap/lockfile story. Overkill for parameter optimisation over expensive black-box jobs; revisit only if a customer needs differentiable/GP-gradient acquisition that Optuna+pymoo can't supply. |
| **`scipy.optimize` alone** | gradient / evolutionary single-objective | Already in stack and used as the inner scalar bridge — but no sample-efficient BO, no managed multi-objective, no `ask`/`tell` loop fit for the §4 batch coordination. Kept as a helper, not the engine. |

Optuna (single-objective + Hyperband + active-learning host) + pymoo (multi-objective) covers all four ADR-0009 objective shapes with two maintained, modern-stack-clean, permissive-licence libraries at a modest bundle cost — the reason both were chosen over the alternatives above.

---

## 20. Lineage / V&V / KPI / Live-Jobs cross-cuts

ADR-0009 §15 lists the cross-ADR impacts. The build-level handles (most are no-ops because optimisation children are ordinary completed jobs of one tool):

### 20.1 Data lineage (ADR-0010)

- `optimisation_study` becomes a lineage root node, reusing the **parametric-study node/resolver shape** — `LineageService` gains an `OptimisationStudy` case in the existing study resolver rather than a brand-new `NodeKind` mechanism (the study root + its child-job edges follow the parametric pattern exactly).
- For `surrogate_assisted_real` children, the `Job.surrogate_model_version_id` tag draws the assist edge to the `SurrogateModel` node (surrogate §9.1) — no new edge kind beyond surrogate-family's `PREDICTED_BY`.
- The `/admin/lineage` picker gains "Optimisation study" alongside "Parametric study".

### 20.2 V&V (ADR-0011)

A surrogate version produced *by* an optimisation study (via the §16.4 handoff) records V&V status no differently from one produced by a parametric study (ADR-0009 §15) — the `vv_surrogate` target lands with the surrogate-family V&V follow-up (surrogate §9.2), not here.

### 20.3 KPI extraction (ADR-0012)

Optimisation-study children flow through the existing `runner.supervisor._post_complete` extraction hook with **no optimisation-specific code path** — they're ordinary completed jobs of one tool; per-tool KPIs apply (ADR-0009 §15). `surrogate`-mode evaluations (which write `study_result` without a real child job) do **not** produce KPI rows — there's no job to extract from, and the prediction isn't a measured result. The results explorer is the surface for surrogate-mode output.

### 20.4 Live Jobs (§16.18)

The convergence trace is the natural progress indicator for an optimisation-study row in the install-wide Live Jobs monitor; the bulk-cancel path already works because the `bypass_access` extension shipped in §16.18 (ADR-0009 §15). No new code beyond surfacing the trace summary.

---

## 21. Non-negotiables (build these in from the start, not retrofitted)

1. **`max_runs` is mandatory.** No study leaves `draft` without a budget; the runtime never loops unbounded (§8 / §3.3).
2. **`tell` is idempotent on identical `(X, y)`.** This is what makes mid-batch crash recovery safe (§4.2 / §10) — re-feeding a recovered point is a no-op.
3. **The durable `study_result` history is the source of truth, not the optimiser blob.** A study can always be rebuilt from its completed children; corrupt optimiser state parks the study with an ops error, never silently continues (§10).
4. **Optimiser state is `joblib`-serialised in the runner context, never `pickle.load`'d in the web process.** Same discipline as the surrogate bundle (surrogate §16.9).
5. **Constraints reuse `safe_expr` — no new DSL.** Cross-field at selection time, output constraints at `tell` time; feasibility recorded on every `study_result` row (§7).
6. **The study lane keeps optimisation from starving interactive users.** Every child queues low-priority; `concurrency_cap` bounds in-flight children (§9 / studies-engine §5).
7. **Strategy compatibility is validated server-side, not just filtered in the UI.** `objective_shapes` mismatch is rejected at `configure` (§3.3 / §5).
8. **`surrogate_assisted_real` hard-depends on the Model registry.** Configure rejects it when `features.surrogate_family` is off (§18); the build ordering puts surrogate Phase 5 before this ADR's surrogate-mode phase (§22 / studies-engine §11).
9. **Manual halt always wins.** No criterion configuration can prevent a user from stopping a study gracefully (§8); in-flight children complete, no new batches queue.
10. **Existing parametric-study users see no change.** The `study_result` rename is transparent to them (studies-engine §6); the optimisation sibling adds tables and routes, never alters the parametric path.
11. **Start-approval subsumes per-child pre-approval — never per-child approval storms.** A campaign approved once authorises its runs; per-child *post* gates are waived in favour of one gate on the final result (§14.1).
12. **Workflow factors are the chain's open inputs only.** Upstream-wired inputs are never offered as factors (§3.4); the loop stays target-agnostic (§4.1).
13. **MOP/CoP and the new strategies add no second engine and no new dependency.** Auto-best-metamodel + CoP + variable reduction compose the existing surrogate fit / Sobol primitives; the extra strategies come from Optuna / pymoo / scipy already in stack (§5, §19).

---

## 22. Phased build sequence (within ROADMAP Phase 2)

Per house style — **pause for review after this design doc, and after each phase**. Mirrors ADR-0009's nine build phases; interleaves with the surrogate-family build at this doc's surrogate-mode phase (§22 Phase 5 gates on surrogate Phase 5).

- **Phase 0 (prereq) — `study_result` rename.** Owned by the surrogate-family build (surrogate §17 Phase 1) under the assumed ship order; this build consumes it. If the order flips, this phase absorbs the identical rename migration. The `evaluated_on_surrogate` + `feasible` columns on `study_result` land here regardless (§12.5/§12.6).
- **Phase 1 — package skeleton + data model.** `matofx/studies/optimisation/` skeleton; `optimisation_study` table + `Job.optimisation_study_id` FK + `Job` home CHECK widening (1-of-2 → 1-of-3) + `Job.optimisation_metadata_json`; ORM + `OptimisationService` config/lifecycle CRUD (`create_draft` / `update_draft` / `configure` validation / `archive` / `list` / `get`) **without a runtime loop yet**; errors module. Both DB engines. Tests cover `validate_config` exhaustively + the CHECK widening on both engines.
- **Phase 2 — strategy registry + foundational strategies + sync outer loop.** Registry + the three foundational strategies (`random`, `grid`, `bo_ei`); the `Strategy` Protocol; seed-batch logic (§11); the synchronous outer loop (§4) hosted in the runner daemon; selection-time constraint filter (§7 box + cross-field); the `study_result` write path for real children (shared with surrogate Phase 1's hook). Tests: a fast fake-tool end-to-end on `random`/`grid`/`bo_ei` reaching `max_runs`.
- **Phase 3 — stopping criteria + durable state + crash recovery.** `evaluate_stopping` for `max_runs` / `target` / `wall_clock` / single-objective `convergence`; durable state persistence (JSON ≤1 MB + disk-blob path + sha256); rehydrate-on-restart; `recover_in_flight`; idempotent `tell`. Tests: kill-and-resume mid-batch reaches the same terminal state; corrupt-state parks with the ops error.
- **Phase 4 — multi-objective.** `nsga2` + `moea_d` (pymoo); hypervolume convergence + reference point; black-box (output) constraint kind via constraint domination; Pareto-front summary in `current_best_json`. Tests: a 2-objective fake tool produces a non-trivial Pareto front; output-constraint infeasibility recorded.
- **Phase 5 — surrogate run modes** *(gates on surrogate-family Phase 5 — Model registry).* `surrogate` (in-process predict, no child jobs) + `surrogate_assisted_real` (refit each batch via `ModelService.refit`); the §3.3 surrogate validation; the "Publish current surrogate" handoff routing to the surrogate publish gate. Tests: a `surrogate` study runs with zero real jobs; a `surrogate_assisted_real` study refits and advances its working surrogate each batch.
- **Phase 6 — active learning.** `active_max_variance` + `active_max_uncertainty_reduction` wrapping the surrogate acquisition primitives (surrogate §4); `info_gain` configure path (no response goals; pick the surrogate to improve); the in-form coverage gauge integration borrowed by surrogate §4. Tests: an active study drives down held-out reconstruction error vs a random baseline.
- **Phase 7 — async batch coordination.** Hyperband (`supports_async_tell` native) + the optional async-`tell` path for BO baselines; `concurrency_cap` + study-lane integration under async; backpressure tests. Tests: variable-runtime children complete faster wall-clock under `asynchronous` than `synchronous` for the same budget.
- **Phase 8 — UI.** Optimisation studies tab; the multi-step create wizard (DB-only draft save); the study detail page mounting the Results explorer + Convergence + Acquisition tabs; Pause/Resume/Halt/Extend/Change-strategy controls; the surrogate-handoff dialog; auto-poll-without-teardown on the live tabs.
- **Phase 9 — strategy spectrum.** Register `cmaes` / `de` / `pso` / `ga` / `nsga3` / `dual_annealing` / `nelder_mead` / `slsqp` (zero new deps); per-strategy batch defaults; the gradient/local "polish" path for surrogate mode. Tests: each strategy reaches budget on a fake tool; local strategies warn on expensive `real`.
- **Phase 10 — recommendation wizard.** `recommend_strategy` + the create-wizard "Recommended" badge (§5.6).
- **Phase 11 — workflow-target optimisation.** `WorkflowRun.optimisation_study_id`; the `target` axis in config + validation; target-agnostic `queue_child` / `collect_results`; open-input factor detection from the workflow graph; per-`{member, output}` response spec. Tests: a 2-tool chain optimises an open input against a downstream KPI.
- **Phase 12 — MOP/CoP.** Auto-best-metamodel selection by Q²; CoP per factor from Sobol × Q²; `mop.cop_cutoff` variable reduction + per-factor override UI (§5.7).
- **Phase 13 — start-approval gate.** Per-tool `optimisation_start_approval_required` flag + `optimisation_approver` capability + `optimisation_study_start` approval target + `pending_start_approval` state + cost estimate; the pre-subsume / post-waive interaction (§14.1).
- **Phase 14 — cross-ADR integration + docs.** Lineage `OptimisationStudy` resolver case + admin-picker entry + the `surrogate_assisted_real` assist edge; V&V `vv_surrogate` no-op confirmation; KPI read-path validation (no code change expected); Live-Jobs trace summary; `ARCHITECTURE.md` §16.27 as-built note; `ROADMAP.md` tick; package READMEs; SBOM regen + lockfile bump.

Gates between phases: `ruff format --check . && ruff check . && mypy && pytest`, both DB engines, plus a feature-flag-on/off smoke test for the router + runtime scan.

---

## 23. Explicitly out of scope for v1.0

Per ADR-0009 §14; all recorded in ROADMAP v1.x:

- **Topology / shape optimisation** — gradient-based or evolutionary morphing of CAD/mesh; specialised per CAD kernel.
- **Robust / reliability-based optimisation** — chance constraints, mean-variance trade-off; couples with uncertainty propagation.
- **Mixed-variable beyond Optuna-native** — continuous + integer + categorical work; trees / graphs / sets don't.
- **Black-box constraint learning** — fitting a surrogate of the constraint function itself; not in the v1.0 strategy registry.
- **Auto-publish closed-loop** — automatically promoting the final surrogate to a published global tool on convergence; the manual "Publish current surrogate" affordance (§16.4) covers v1.0.
- **Online learning inside the optimiser** — every `tell` triggers a full surrogate refit in `surrogate_assisted_real`; incremental Bayesian updates are a v1.x surrogate-engine concern (surrogate §8.4).

---

## 24. Open questions / revisit triggers

- **Default `batch_size` (4) and `concurrency_cap` (4)** — confirm against pilot HPC throughput. A scheduler that admits 16 concurrent CFD jobs wants a larger batch; a single-GPU box wants 1.
- **Default seed size `max(10, 3 × dim)`** — confirm with the first pilot's dimensionality. High-dimensional studies (>10 factors) may want a smaller multiplier to avoid burning the budget on the seed.
- **`convergence.epsilon` defaults** — best-so-far epsilon is response-scale-dependent; the UI should suggest a value from the seed-batch response spread rather than a fixed default. Decide the heuristic with pilot data.
- **Multi-objective hypervolume reference point** — fixing it at the seed-batch worst +10% is a heuristic; a study that improves past the reference distorts the metric. Revisit whether a dynamic (nadir-tracking) reference is worth the complexity.
- **`change_strategy` re-learn cost** — re-`tell`ing a new strategy from the full `study_result` history is fine for hundreds of points; for thousands it may stall a batch. Cap or background the re-learn if pilot studies get large.
- **Whether `surrogate_assisted_real` should auto-pause when the refit `quality` regresses** (the surrogate getting *worse* as points accumulate signals a modelling problem) — a guardrail worth considering; currently the study proceeds and surfaces the quality trend on the detail page.
- **Async-BO `tell` correctness under pymoo** — pymoo is generational by design; if a customer needs async multi-objective, that's a strategy-level research item, not a config flag. Confirmed out for v1.0; revisit on signal.
- **Whether the acquisition map should support >2 factors** (slice / projection UX) — 2-D contour covers the common case; N-D exploration is a v1.x UX iteration.
- **CoP definition (§5.7)** — using Sobol-total × Q² as the per-factor CoP is a pragmatic analogue of optiSLang's CoP; confirm against pilot data whether a metamodel-intrinsic CoP (per-variable predictive R²) is worth the extra fit.
- **Workflow open-input detection (§3.4)** — relies on the workflow graph's auto-discovered wiring (§16.4); a *partially*-wired input (fed for some members, free for others) needs a clear rule — propose "free unless fully wired", confirm with a real chain.
- **Per-tool start-approval default (§14.1)** is off, set manually per tool — confirm operators are comfortable flagging high-cost tools by hand vs a cost-threshold auto-trigger (a possible v1.x convenience).

---

## 25. Revision history

- **2026-06-01 — drafted, pending architect review.** Initial draft, the last design-only artifact gating the pilot re-baseline (ROADMAP "Pilot date re-baselines once the surrogate / optimisation design docs land"). Companion to ADR-0009 (ruled 2026-05-26 at full v1.0 scope). Shared substrate `designs/studies-engine.md` locked 2026-05-26; companion `designs/surrogate-family.md` locked 2026-05-29. The `study_result` rename is shared with ADR-0008 and consumed (not re-owned) here per the assumed surrogate-first ship order; the `surrogate_assisted_real` run mode hard-depends on surrogate-family Phase 5 (Model registry).
- **2026-06-01 — scope expanded (architect review).** Added the **full strategy spectrum** (`cmaes` / `de` / `pso` / `ga` / `nsga3` / `dual_annealing` / `nelder_mead` / `slsqp`), the **algorithm-recommendation wizard**, and full **MOP/CoP** (auto-best-metamodel + Coefficient-of-Prognosis variable reduction) — all **zero-new-dependency** (§5). Added **workflow-target optimisation** (`target ∈ {tool, workflow}`, process-chain à la optiSLang) designed + implemented for v1.0 (§3.4, §12.2). Added the **per-tool toggleable start-approval gate** — `optimisation_approver` capability, `optimisation_study_start` approval target, `pending_start_approval` state — which subsumes per-child *pre*-approval and waives per-child *post*-approval (final result gated once) (§14.1). Touched §1, §3.1, §3.3–§3.4, §4.1, §5, §12, §14–§19, §21, §22, §24. Build phases now run to **14**. New v1.0 deps unchanged (Optuna + pymoo).
- **2026-06-01 — design locked.** Architect review complete; this doc and `designs/surrogate-family.md` are both locked. Companion ADR-0009, shared substrate `designs/studies-engine.md`, and the `ROADMAP.md` ADR-0009 row are aligned. The last design-only artifact gating the pilot re-baseline has landed — implementation can begin per the §22 phase plan (no code yet).
- **2026-06-08 — amendment: MATOFX-surrogate optimiser; Optuna dropped (see `decisions/0009-optimisation-framework.md` revision history for the full ruling).** Build-phase change driven by the maintainer's intent that the surrogate be a first-class, inspectable, publishable MATOFX artifact. Supersedes the Optuna-based parts of §5/§6/§19/§11: (1) single-objective Bayesian optimisation is the **`bo`** strategy on `matofx.studies.surrogate` + an acquisition function (replaces `bo_ei`/`bo_ucb`/`hyperband`); (2) **three acquisition modes** `optimise`/`balanced`/`explore` (one κ knob) select next runs from the surrogate's μ ± σ — `explore` folds in the §5.4 `active_*` case; (3) **cohort-seeding** added beside the LHS seed (§11) — prior runs imported as initial points, no jobs/budget; (4) the optimiser's surrogate **is** the working/publishable model, **latest-only**, published on demand — this **subsumes the `surrogate_assisted_real` run mode** (a `real`-mode `bo` study is surrogate-assisted by construction), leaving run modes `real` + `surrogate`; (5) **Optuna dropped**, **pymoo retained** (`nsga2`/`moea_d`); the §5 cost-spectrum strategies + **MOP/CoP** (kind-choice / use-best) stay committed for later phases (MOP/CoP owns "use best model"; the `bo` strategy's `surrogate_kind` reserves the hook). Build status at amendment: Phases 1-5 shipped; this re-points the surrogate strategy onto MATOFX's engine.
