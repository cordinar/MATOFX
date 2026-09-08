"""The optimisation outer-loop runtime (ADR-0009 §4).

A long-lived async coroutine, one per ``running`` study, hosted in the
runner daemon. Each batch tick (§4.1):

1. **ask** the strategy for the next K candidates (or run the LHS seed
   batch first, §11);
2. **filter** out selection-time constraint violators (§7);
3. **submit** the survivors as study children and **wait** for the batch
   to terminate;
4. **collect** their responses, **tell** the strategy, and append a
   convergence-trace point;
5. **stop** when the ``max_runs`` budget is spent, the grid is exhausted,
   or a halt is requested.

The submit/wait/collect step is the only target-specific seam, behind the
:class:`CandidateRunner` Protocol — :class:`ToolJobRunner` queues real
``Job`` children and reads their results, while tests inject a fast
in-memory fake. **Phase 2 scope:** the strategy's state lives in memory
for the duration of the run and a ``max_runs`` budget terminates it; the
full :func:`evaluate_stopping` criteria set, durable rehydrate-on-restart,
and crash recovery are build Phase 3. The convergence trace, best-so-far,
batch number, and terminal state *are* persisted each batch.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import select

from matofx.db import session_scope
from matofx.studies._intsnap import snap_int
from matofx.studies.optimisation.constraints import constraints_ok, output_feasible
from matofx.studies.optimisation.errors import OptimisationConfigError, OptimiserStateCorruptError
from matofx.studies.optimisation.orm import OptimisationStudy
from matofx.studies.optimisation.seed import default_seed_size, lhs_seed
from matofx.studies.optimisation.state import load_optimiser_state, persist_optimiser_state
from matofx.studies.optimisation.stopping import evaluate_stopping
from matofx.studies.optimisation.strategies import (
    Candidate,
    Objective,
    ResponseVector,
    StrategyContext,
    build_context,
)
from matofx.studies.optimisation.strategies import registry as strategy_registry
from matofx.studies.optimisation.sumcheck import apply_sum_derivation

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter
    from matofx.db.session import SessionFactory
    from matofx.studies.optimisation.strategies.base import Strategy

_DEFAULT_POLL_INTERVAL_S = 2.0
_DEFAULT_BATCH_TIMEOUT_S = 3600.0
_MAX_CONSECUTIVE_EMPTY_BATCHES = 5
"""After this many batches that propose nothing runnable, fail-stop
(every proposal dropped by constraints / the strategy exhausted)."""

_TERMINAL_JOB_STATES: frozenset[str] = frozenset({"complete", "failed", "cancelled"})

# Workflow-run-level states (``compute_state``) at which a headless run can
# make no further progress on its own — used by the workflow-target runner so
# a failed member (downstream left ``created``) doesn't block the batch.
_TERMINAL_RUN_STATES: frozenset[str] = frozenset({"complete", "failed", "cancelled"})


def _split_member_inputs(fixed_inputs: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``{"member.input": value}`` → ``{member: {input: value}}`` (§3.5).

    The workflow target keys held-constant inputs by ``member.input`` (the
    factor convention); ``start_headless_run`` wants them grouped per member.
    Keys without a member prefix are skipped.
    """
    out: dict[str, dict[str, Any]] = {}
    for key, value in fixed_inputs.items():
        member, _, input_name = str(key).partition(".")
        if member and input_name:
            out.setdefault(member, {})[input_name] = value
    return out


def _apply_sum_reduction(config: Mapping[str, Any]) -> dict[str, Any]:
    """Drop each §3.6 sum-group's *derived* member from the search-space factors.

    The derived member isn't varied — it's worked out from the others
    (`apply_sum_derivation`) — so removing it from the factors the strategy
    context is built from means the optimiser searches only the group's true
    degrees of freedom (no wasted exploration of the redundant direction).
    Mirrors the MOP-freeze reduction in `_apply_mop_reduction`.
    """
    sum_groups = config.get("sum_groups") or []
    derived = {g["balance"] for g in sum_groups if isinstance(g, Mapping) and g.get("balance")}
    if not derived:
        return dict(config)
    reduced = dict(config)
    reduced["factors"] = [f for f in (config.get("factors") or []) if f.get("name") not in derived]
    return reduced


def _sum_member_keys(config: Mapping[str, Any]) -> set[str]:
    """Every §3.6 sum-group member's factor key (so they're never MOP-frozen)."""
    keys: set[str] = set()
    for group in config.get("sum_groups") or []:
        if not isinstance(group, Mapping):
            continue
        for member in group.get("members") or []:
            key = member.get("factor")
            if isinstance(key, str):
                keys.add(key)
    return keys


def _candidate_open_inputs(
    candidate: Mapping[str, Any],
    fixed_by_member: Mapping[str, Mapping[str, Any]],
    factor_map: Mapping[str, tuple[str, str]],
) -> dict[str, dict[str, Any]]:
    """One candidate's open inputs: held-constant values + the varied factors.

    Factors win over a held value for the same ``member.input`` (they should
    never overlap, but the search is authoritative for anything it varies).
    """
    open_inputs: dict[str, dict[str, Any]] = {
        member: dict(values) for member, values in fixed_by_member.items()
    }
    for fname, (member, input_name) in factor_map.items():
        if fname in candidate:
            open_inputs.setdefault(member, {})[input_name] = candidate[fname]
    return open_inputs


_MULTI_OBJECTIVE = 2
"""Optimised-objective count at/above which a study uses the multi-objective
metric (hypervolume + Pareto front) rather than single best-so-far."""


@dataclass(frozen=True, slots=True)
class StudyRef:
    """The immutable study facts a :class:`CandidateRunner` needs.

    Passed instead of the ORM object so the runner can open its own
    sessions without carrying a detached instance across them.
    """

    study_id: int
    tool_alias: str | None
    tool_version_id: int | None
    created_by_user_id: int
    response_names: tuple[str, ...]
    target_kind: str = "tool"
    workflow_alias: str | None = None
    scenario_id: int | None = None
    """Set for a ``workflow`` target (§3.4): the chain to run and the
    dedicated scenario hosting its candidate runs. ``tool_alias`` /
    ``tool_version_id`` are ``None`` then; for a ``tool`` target the
    reverse holds."""
    fixed_inputs: Mapping[str, Any] = field(default_factory=dict)
    """Held-constant values for required inputs the search isn't varying
    (§3.5). A tool keys them by input name; a workflow by ``member.input``.
    Merged under each candidate at job-build time — materials carry their
    ``material_version_id`` string and resolve at queue time like a normal
    submission. The search never sees them (they aren't factors)."""


@dataclass(frozen=True, slots=True)
class Evaluation:
    """One candidate's outcome after it ran."""

    candidate: Candidate
    responses: ResponseVector
    feasible: bool
    succeeded: bool


class CandidateRunner(Protocol):
    """Submit a batch of candidates and return their evaluations (§4.1 steps 3-5)."""

    async def run_batch(
        self,
        *,
        ref: StudyRef,
        candidates: Sequence[Candidate],
        batch: int,
        rationales: Sequence[dict[str, Any]],
    ) -> list[Evaluation]:
        """Run the candidates as study children and collect their responses."""
        ...


class OptimisationRuntime:
    """Drives ``running`` optimisation studies to completion (ADR-0009 §4)."""

    def __init__(
        self,
        factory: SessionFactory,
        audit: AuditWriter,
        *,
        runner: CandidateRunner,
        environment: str | None = None,
    ) -> None:
        """Compose over the session factory, audit writer, and candidate runner.

        ``environment`` is the staging environment the workflow-target
        runner needs (it queues child jobs via ``queue_for_runner``); the
        tool-target ``runner`` carries its own.
        """
        self._factory = factory
        self.audit = audit
        self._runner = runner
        self._environment = environment
        self._active: set[int] = set()
        self._parked: set[int] = set()

    async def scan(self) -> int:
        """Spawn a loop task for each ``running`` study not already driven.

        Returns the number of studies newly picked up. Called periodically
        by the runner daemon (the ``_sleepable`` pattern); the per-study
        coroutine runs to a terminal state then drops out of the active
        set.
        """
        async with session_scope(self._factory) as session:
            rows = (
                (
                    await session.execute(
                        select(OptimisationStudy.id).where(
                            OptimisationStudy.state == "running",
                            OptimisationStudy.archived_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
        spawned = 0
        for study_id in rows:
            if study_id in self._active or study_id in self._parked:
                continue
            self._active.add(study_id)
            task = asyncio.create_task(
                self._run_tracked(study_id), name=f"optimisation.study.{study_id}"
            )
            task.add_done_callback(partial(self._on_study_done, study_id))
            spawned += 1
        return spawned

    def _on_study_done(self, study_id: int, _task: object) -> None:
        """Drop a finished study from the active set (done-callback)."""
        self._active.discard(study_id)

    async def _run_tracked(self, study_id: int) -> None:
        try:
            await self.run_study(study_id)
        except OptimiserStateCorruptError as exc:
            await self._park_corrupt(study_id, exc)
        finally:
            self._active.discard(study_id)

    async def _park_corrupt(self, study_id: int, exc: OptimiserStateCorruptError) -> None:
        """Park a study whose persisted state is corrupt (§10, non-negotiable #3).

        The study stays ``running`` (an admin reset is the recovery path),
        but is added to the parked set so the scan stops re-picking it,
        and the failure is audited.
        """
        self._parked.add(study_id)
        async with session_scope(self._factory) as session:
            study = await session.get(OptimisationStudy, study_id)
            blob = study.optimiser_state_blob_path if study is not None else None
            await self.audit.record(
                session,
                _system_record(
                    action="study.optimisation.state_corrupt",
                    study_id=study_id,
                    payload={"blob_path": blob, "error": str(exc)},
                ),
            )

    async def run_study(self, study_id: int) -> None:  # noqa: PLR0912, PLR0915 — one sequential outer-loop flow
        """Run one study's outer loop to a terminal state (§4.1).

        Resumes transparently (§10): a study with persisted optimiser
        state is rehydrated and continued rather than restarted, and any
        children that ran but weren't folded into that state (a crash
        mid-batch) are recovered idempotently. A corrupt persisted state
        raises :class:`OptimiserStateCorruptError` — the caller parks the
        study rather than continuing on bad state (non-negotiable #3).
        """
        loaded = await self._load_runnable(study_id)
        if loaded is None:
            return
        config, context, ref, frozen_values = loaded
        objective_names = _objective_names(config)
        objectives = context.objectives
        # §3.6 sum-checks: each group's *derived* member was dropped from the
        # search space (`_apply_sum_reduction`); the strategy varies the others
        # and ``apply_sum_derivation`` fills the derived one in (dropping a
        # candidate whose residual can't fit the derived member's box).
        sum_groups = list(config.get("sum_groups") or [])

        max_runs = _max_runs(config)
        batch_size = max(1, int(config.get("batch_size", 1)))
        runner = self._select_runner(config)
        on_surrogate = config.get("run_mode") == "surrogate"
        started = time.monotonic()

        envelope, batch_no = await self._load_resume(study_id)
        strategy = strategy_registry.get(config["strategy"]).bind(
            context, envelope.get("strategy") if envelope else None
        )
        total = int(envelope.get("total", 0)) if envelope else 0
        any_success = bool(envelope.get("any_success", False)) if envelope else False
        trace = await self._load_trace(study_id)
        # Restore the hypervolume reference point (multi-objective) so a resumed
        # study — e.g. a continued one — keeps measuring against the *same*
        # reference. Otherwise it would re-anchor to the (now much better) front
        # and the metric would jump down at the resume boundary even though the
        # Pareto front only improved (§10/§16.1).
        _saved_ref = envelope.get("ref_point") if envelope else None
        ref_point: list[float] | None = (
            [float(v) for v in _saved_ref] if isinstance(_saved_ref, list) and _saved_ref else None
        )

        # Recover children that ran but weren't in the persisted state
        # (crash mid-batch); idempotent re-tell, never double-counts (#2).
        total, any_success, seen_keys = await self._recover(
            ref, strategy, objective_names, config, total, any_success
        )
        # Distinct-design tracking for the dedupe (§4.1, ADR-0009 amendment
        # 2026-06-15): a fully-discrete space has a finite point count, so once
        # every point is evaluated the study stops rather than re-running.
        discrete_capacity = _discrete_capacity(context.factors)

        # Seed batch (§11). Surrogate mode skips it (the surrogate is the
        # truth). A cohort seed imports prior runs (no jobs, no budget); an
        # LHS seed runs real jobs that count toward the budget.
        fresh = batch_no == 0 and total == 0
        seed_cfg = config.get("seed") or {}
        # Candidates dropped by the selection-time constraint filter (i.e. they
        # broke a rule) in batches that don't otherwise persist a trace point —
        # carried forward and folded into the next persisted batch's count so
        # the trace sum reflects every rule-driven drop.
        pending_dropped = 0
        if fresh and strategy.needs_seed and not on_surrogate:
            if seed_cfg.get("kind") == "cohort":
                cohort_id = seed_cfg.get("cohort_id")
                evals = (
                    await self._cohort_seed(ref, int(cohort_id), objective_names, strategy)
                    if cohort_id is not None
                    else []
                )
                if evals:
                    any_success = any_success or any(e.succeeded for e in evals)
                    seen_keys.update(_candidate_key(e.candidate) for e in evals)
                    batch_no += 1
                    # Prior runs don't consume the new-run budget — total unchanged.
                    ref_point = await self._persist_batch(
                        study_id,
                        strategy,
                        batch_no,
                        evals,
                        started,
                        trace,
                        total,
                        any_success,
                        objectives,
                        ref_point,
                    )
            else:
                # ``_seed_candidates`` samples the *free* members (the derived
                # ones were dropped from ``context.factors``) and fills the
                # derived members in before its constraint filter.
                seed_free, seed_dropped = self._seed_candidates(config, context, max_runs)
                seed_cands = [{**frozen_values, **c} for c in seed_free]
                if seed_cands:
                    evals = await self._evaluate(
                        runner, ref, strategy, seed_cands, batch_no, objective_names, config
                    )
                    total += len(evals)
                    any_success = any_success or any(e.succeeded for e in evals)
                    seen_keys.update(_candidate_key(e.candidate) for e in evals)
                    batch_no += 1
                    ref_point = await self._persist_batch(
                        study_id,
                        strategy,
                        batch_no,
                        evals,
                        started,
                        trace,
                        total,
                        any_success,
                        objectives,
                        ref_point,
                        n_dropped=seed_dropped,
                    )
                else:
                    # Whole seed was infeasible — carry its drops to the first
                    # persisted ask/tell batch rather than losing the count.
                    pending_dropped += seed_dropped

        empty_streak = 0
        dup_streak = 0
        stop_reason: str | None = None
        while True:
            if await self._halt_requested(study_id):
                stop_reason = "user_halted"
                break
            reason = evaluate_stopping(
                config,
                total=total,
                best=strategy.best_so_far(),
                trace=trace,
                elapsed_s=time.monotonic() - started,
            )
            if reason:
                stop_reason = reason
                break
            k = min(batch_size, max(1, max_runs - total))
            raw = strategy.ask(k)
            if not raw:
                # The strategy genuinely has nothing left (grid exhausted).
                stop_reason = "grid_exhausted"
                break
            # Hold any MOP-frozen factors at their nominal value. Re-read the
            # frozen set each batch so a freeze / unfreeze chosen from the
            # Sensitivity card takes effect on the next batch; the frozen value
            # wins over the strategy's proposal (§5.7).
            frozen_values = await self._frozen_values_now(study_id)
            raw = [{**c, **frozen_values} for c in raw]
            if sum_groups:
                # Fill in each group's derived member before the candidate is
                # run or told back (told X == run X — the strategy already did
                # this in decode, so this is idempotent except where a frozen
                # value shifts the residual). Infeasible candidates are dropped,
                # feeding the empty-streak guard below like a constraint miss.
                raw = apply_sum_derivation(raw, sum_groups, frozen_values)
            # Merge the held-constant inputs in for the constraint check only
            # (not into the proposal) so a descriptor validity rule may
            # reference a fixed input as well as a varied factor; the queued
            # candidate stays the strategy's proposal.
            valid = [c for c in raw if constraints_ok({**ref.fixed_inputs, **c}, config)]
            dropped = len(raw) - len(valid)
            if not valid:
                # Every proposal was constraint-infeasible — retry a
                # bounded number of times before fail-stopping (§4.1).
                pending_dropped += dropped
                empty_streak += 1
                if empty_streak >= _MAX_CONSECUTIVE_EMPTY_BATCHES:
                    stop_reason = "all_children_failed"
                    break
                continue
            empty_streak = 0
            # Drop proposals whose design repeats one already evaluated this
            # study — integer/categorical rounding can map distinct proposals
            # onto the same point, and re-running an identical design only burns
            # budget (ADR-0009 amendment 2026-06-15). We run the unique
            # remainder rather than re-asking, which would desync the pymoo
            # generational ask/tell contract; the strategy proposes afresh next
            # batch (the wrapper penalises the un-told duplicates).
            unique = _dedupe_unseen(valid, seen_keys)
            if not unique:
                # This batch had rule-driven drops but persists no trace point
                # (every survivor was a duplicate) — carry the drop count.
                pending_dropped += dropped
                dup_streak += 1
                exhausted = discrete_capacity is not None and len(seen_keys) >= discrete_capacity
                if exhausted or dup_streak >= _MAX_CONSECUTIVE_EMPTY_BATCHES:
                    stop_reason = "space_exhausted"
                    break
                continue
            dup_streak = 0
            # Population strategies (pymoo EAs, e.g. CMA-ES) size their own
            # generation and ignore the requested ``k``, so a generation can
            # exceed the remaining budget. Cap the submitted candidates to it
            # — ``max_runs`` is a hard ceiling (§6); the pymoo wrapper marks
            # the un-told candidates with a penalty so its generation stays
            # whole. The ask/tell strategies already honour ``k``, so this is
            # a no-op for them.
            valid = unique[: max(1, max_runs - total)]
            seen_keys.update(_candidate_key(c) for c in valid)
            evals = await self._evaluate(
                runner, ref, strategy, valid, batch_no, objective_names, config
            )
            total += len(evals)
            any_success = any_success or any(e.succeeded for e in evals)
            batch_no += 1
            ref_point = await self._persist_batch(
                study_id,
                strategy,
                batch_no,
                evals,
                started,
                trace,
                total,
                any_success,
                objectives,
                ref_point,
                n_dropped=dropped + pending_dropped,
            )
            pending_dropped = 0

        if stop_reason is None:
            stop_reason = "budget_exhausted"
        # Ran children but none succeeded → failed. A study that halted or
        # exhausted its grid before running anything keeps that reason.
        if total > 0 and not any_success:
            stop_reason = "all_children_failed"
        await self._finish(study_id, stop_reason)

    # --- batch helpers ---------------------------------------------------

    def _seed_candidates(
        self, config: Mapping[str, Any], context: StrategyContext, max_runs: int
    ) -> tuple[list[Candidate], int]:
        """The LHS seed points that pass the constraint filter + the drop count.

        The second element is how many sampled seed points were rejected
        for breaking a rule, so the caller can record it in the trace.
        """
        seed_cfg = config.get("seed") or {}
        n = int(seed_cfg.get("n") or default_seed_size(len(context.factors)))
        n = min(n, max_runs)
        points: list[Candidate] = lhs_seed(context.factors, n, random_seed=context.random_seed)
        # §3.6: the LHS samples only the free members; fill in each group's
        # derived member (dropping infeasible points) *before* the constraint
        # filter, so a cross-field constraint can reference the derived value.
        sum_groups = config.get("sum_groups") or []
        if sum_groups:
            points = apply_sum_derivation(points, sum_groups, {})
        # Held-constant inputs merged in for the check only (see run_study) so
        # descriptor validity rules referencing a fixed input resolve.
        fixed_inputs = config.get("fixed_inputs") or {}
        kept = [c for c in points if constraints_ok({**fixed_inputs, **c}, config)]
        return kept, len(points) - len(kept)

    def _select_runner(self, config: Mapping[str, Any]) -> CandidateRunner:
        """Pick the candidate runner for a study's run mode (§6).

        ``surrogate`` evaluates in-process against the attached model
        version (no child jobs); ``real`` uses the injected job runner.
        """
        if config.get("run_mode") == "surrogate":
            return SurrogateRunner(self._factory, int(config["surrogate_model_version_id"]))
        if (config.get("target") or {}).get("kind") == "workflow":
            return WorkflowRunRunner(self._factory, self.audit, environment=self._environment)
        return self._runner

    async def _evaluate(
        self,
        runner: CandidateRunner,
        ref: StudyRef,
        strategy: Strategy,
        candidates: Sequence[Candidate],
        batch: int,
        objective_names: frozenset[str],
        config: Mapping[str, Any],
    ) -> list[Evaluation]:
        rationales = [strategy.rationale(c) for c in candidates]
        evals = await runner.run_batch(
            ref=ref, candidates=candidates, batch=batch, rationales=rationales
        )
        evals = [_with_output_feasibility(e, config) for e in evals]
        self._tell(strategy, evals, objective_names)
        return evals

    def _tell(
        self,
        strategy: Strategy,
        evals: Sequence[Evaluation],
        objective_names: frozenset[str],
    ) -> None:
        """Tell the strategy only points carrying every optimised response.

        A failed child has empty responses — it consumes budget but has no
        usable objective, so it is not fed to the optimiser (which would
        otherwise break best-so-far).
        """
        usable = [e for e in evals if objective_names <= e.responses.keys()]
        if usable:
            strategy.tell(
                [e.candidate for e in usable],
                [e.responses for e in usable],
                [e.feasible for e in usable],
            )

    # --- recovery + resume ----------------------------------------------

    async def _recover(
        self,
        ref: StudyRef,
        strategy: Strategy,
        objective_names: frozenset[str],
        config: Mapping[str, Any],
        total: int,
        any_success: bool,
    ) -> tuple[int, bool, set[str]]:
        """Fold in any terminal children not yet in the persisted state (§10).

        Awaits children still in flight from a crashed batch, then re-tells
        every terminal child from its ``study_result`` row — idempotent, so
        points already in the rehydrated state are skipped. For a study run
        purely through an in-memory runner (no DB children) this is a
        no-op and the persisted envelope's counters stand. The recovered
        designs' keys seed the dedupe set so a resumed study doesn't re-run a
        point it already evaluated before the restart (§4.1).
        """
        await self._await_children_terminal(ref.study_id)
        children = [
            _with_output_feasibility(e, config)
            for e in await self._collect_source_evals("optimisation", ref.study_id, ref)
        ]
        keys = {_candidate_key(c.candidate) for c in children}
        if not children:
            return total, any_success, keys
        self._tell(strategy, children, objective_names)
        return max(total, len(children)), any_success or any(c.succeeded for c in children), keys

    async def _cohort_seed(
        self, ref: StudyRef, cohort_id: int, objective_names: frozenset[str], strategy: Strategy
    ) -> list[Evaluation]:
        """Import a cohort's prior runs as the strategy's initial sample (amendment §13).

        Prior runs inform the surrogate from point one but run **no jobs**
        and consume **no budget** — they are read from the cohort's
        ``study_result`` rows and told to the strategy. Returns the
        evaluations told (empty if the cohort has no materialised rows).
        """
        evals = await self._collect_source_evals("cohort", cohort_id, ref)
        if evals:
            self._tell(strategy, evals, objective_names)
        return evals

    async def _await_children_terminal(self, study_id: int) -> None:
        from matofx.hierarchy.orm import Job  # noqa: PLC0415

        deadline = time.monotonic() + _DEFAULT_BATCH_TIMEOUT_S
        while time.monotonic() < deadline:
            async with session_scope(self._factory) as session:
                states = (
                    (
                        await session.execute(
                            select(Job.state).where(Job.optimisation_study_id == study_id)
                        )
                    )
                    .scalars()
                    .all()
                )
            if all(s in _TERMINAL_JOB_STATES for s in states):
                return
            await asyncio.sleep(_DEFAULT_POLL_INTERVAL_S)

    async def _collect_source_evals(
        self, source_kind: str, source_id: int, ref: StudyRef
    ) -> list[Evaluation]:
        """Build evaluations from a ``study_result`` source (optimisation/cohort)."""
        from matofx.hierarchy.orm import Job  # noqa: PLC0415
        from matofx.studies.orm import StudyResult  # noqa: PLC0415

        async with session_scope(self._factory) as session:
            rows = (
                await session.execute(
                    select(StudyResult.scalars, StudyResult.feasible, Job.inputs, Job.state)
                    .join(Job, StudyResult.job_id == Job.id)
                    .where(
                        StudyResult.source_kind == source_kind,
                        StudyResult.source_id == source_id,
                    )
                )
            ).all()
        out: list[Evaluation] = []
        for scalars, feasible, inputs, state in rows:
            scalar_map = dict(scalars or {})
            responses = {
                name: float(scalar_map[name])
                for name in ref.response_names
                if isinstance(scalar_map.get(name), int | float)
            }
            out.append(
                Evaluation(
                    candidate=dict(inputs or {}),
                    responses=responses,
                    feasible=bool(feasible) if feasible is not None else True,
                    succeeded=state == "complete",
                )
            )
        return out

    async def _load_resume(self, study_id: int) -> tuple[dict[str, Any] | None, int]:
        """Load the persisted state envelope + last batch number (§10).

        Raises :class:`OptimiserStateCorruptError` (via
        :func:`load_optimiser_state`) when a disk blob fails its integrity
        check — the caller parks the study.
        """
        async with session_scope(self._factory) as session:
            study = await session.get(OptimisationStudy, study_id)
            if study is None:
                return None, 0
            envelope = load_optimiser_state(study)
            return envelope, int(study.last_batch_number or 0)

    async def _load_trace(self, study_id: int) -> list[dict[str, Any]]:
        async with session_scope(self._factory) as session:
            study = await session.get(OptimisationStudy, study_id)
            return list(study.convergence_trace or []) if study is not None else []

    async def _blob_root(self) -> Any:
        """Where to spill a >1 MB optimiser state (§10), or ``None`` for inline.

        Phase 3 keeps oversize state inline (the 1 MB rule is a perf
        guideline, not correctness); wiring the spill directory from the
        active storage mount is a later-phase follow-up. The blob path is
        exercised directly in the state round-trip tests.
        """
        return None

    # --- persistence -----------------------------------------------------

    async def _persist_batch(
        self,
        study_id: int,
        strategy: Strategy,
        batch_no: int,
        evals: Sequence[Evaluation],
        started: float,
        trace: list[dict[str, Any]],
        total: int,
        any_success: bool,
        objectives: Sequence[Objective],
        ref_point: list[float] | None,
        n_dropped: int = 0,
    ) -> list[float] | None:
        """Persist the batch's trace point, best/Pareto, and optimiser state.

        ``n_dropped`` is how many proposals this batch (plus any preceding
        all-infeasible batches whose drops were carried forward) were
        rejected by the selection-time constraint filter — i.e. they broke
        a rule. It rides in the trace point so the UI can sum it without a
        schema change.

        Returns the (possibly newly-fixed) hypervolume reference point so
        the caller keeps it stable across batches (§10).
        """
        metric, current_best, ref_point = self._compute_metrics(strategy, objectives, ref_point)
        blob_root = await self._blob_root()
        async with session_scope(self._factory) as session:
            study = await session.get(OptimisationStudy, study_id)
            if study is None:
                return ref_point
            trace.append(
                {
                    "batch": batch_no,
                    "metric": metric,
                    "n_feasible": sum(1 for e in evals if e.feasible),
                    "n_total": len(evals),
                    "n_dropped": n_dropped,
                    "wall_clock_s": round(time.monotonic() - started, 3),
                }
            )
            study.convergence_trace = list(trace)
            study.last_batch_number = batch_no
            study.current_best_json = current_best
            persist_optimiser_state(
                study,
                {
                    "strategy": strategy.dump(),
                    "total": total,
                    "any_success": any_success,
                    # Fixed once (first feasible batch); persisted so a resumed
                    # study keeps the same hypervolume reference (see run_study).
                    "ref_point": ref_point,
                },
                blob_root=blob_root,
            )
            await session.flush()
        return ref_point

    def _compute_metrics(
        self,
        strategy: Strategy,
        objectives: Sequence[Objective],
        ref_point: list[float] | None,
    ) -> tuple[float | None, dict[str, Any] | None, list[float] | None]:
        """The trace metric + ``current_best`` summary for a batch (§16.1).

        Single-objective: the best-so-far value + vector. Multi-objective:
        the hypervolume (against a reference point fixed on the first
        feasible batch) + the Pareto-front summary.
        """
        if len(objectives) >= _MULTI_OBJECTIVE:
            from matofx.studies.optimisation.strategies.multi import (  # noqa: PLC0415
                hypervolume,
                reference_point,
            )

            pareto = strategy.pareto_front()
            if ref_point is None and pareto:
                ref_point = reference_point(pareto, objectives)
            metric = hypervolume(pareto, objectives, ref_point) if (ref_point and pareto) else None
            return metric, {"pareto_front": pareto}, ref_point
        best = strategy.best_so_far()
        metric = float(best[objectives[0].name]) if (best and objectives) else None
        return metric, (dict(best) if best is not None else None), ref_point

    async def _post_gate_applies(
        self, session: AsyncSession, study: OptimisationStudy
    ) -> tuple[bool, list[str]]:
        """Does the target tool's ``post`` / ``both`` policy gate results release?

        Reads the pinned tool-version descriptor snapshot (never the live
        tool row). Returns ``(applies, approver_usernames)``. A workflow
        target isn't reachable yet (refused at configure), so it never
        gates here.
        """
        if study.target_kind != "tool" or study.tool_version_id is None:
            return False, []
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        tool_version = await session.get(ToolVersion, study.tool_version_id)
        snapshot = (tool_version.descriptor_snapshot or {}) if tool_version is not None else {}
        if snapshot.get("approvals_policy", "none") not in ("post", "both"):
            return False, []
        approvers = [
            str(u) for u in (snapshot.get("approver_usernames") or []) if isinstance(u, str)
        ]
        return True, approvers

    async def _descriptor_cross_field(
        self, session: AsyncSession, study: OptimisationStudy
    ) -> list[dict[str, Any]]:
        """The target tool's input relationships as ``cross_field`` constraints.

        Reads the pinned tool-version descriptor snapshot (never the live
        tool row), so a study keeps obeying the rules it was launched
        against, and compiles every input relationship the descriptor
        declares — §F30 cross-field constraints, §16.16 ``require``
        validity rules, and formula-driven ``set_min_max`` bounds (e.g.
        "B's minimum is A") — into ``{"kind": "cross_field", "expr": ...}``
        entries the existing :func:`constraints_ok` selection filter
        understands. A candidate that violates any is dropped before it's
        queued. Workflow targets carry no single descriptor here, so they
        contribute none; an unreadable snapshot degrades to no extra
        constraints rather than failing the run.
        """
        if study.target_kind != "tool" or study.tool_version_id is None:
            return []
        from matofx.tools import forms  # noqa: PLC0415
        from matofx.tools.descriptor import ToolDescriptor  # noqa: PLC0415
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        tool_version = await session.get(ToolVersion, study.tool_version_id)
        snapshot = (tool_version.descriptor_snapshot or {}) if tool_version is not None else {}
        if not snapshot:
            return []
        try:
            descriptor = ToolDescriptor.model_validate(snapshot)
        except Exception:
            # Fail-safe: a minimal/legacy snapshot that won't re-validate
            # contributes no extra constraints rather than parking the run.
            return []
        return [
            {"kind": "cross_field", "expr": expr}
            for expr, _msg in forms.selection_constraint_exprs(descriptor)
        ]

    async def _finish(self, study_id: int, stop_reason: str) -> None:
        terminal_state = {
            "all_children_failed": "failed",
            "user_halted": "cancelled",
        }.get(stop_reason, "complete")
        async with session_scope(self._factory) as session:
            study = await session.get(OptimisationStudy, study_id)
            if study is None or study.state != "running":
                return
            # A clean completion of a post/both-policy tool withholds the
            # campaign's results behind a results-release gate instead of
            # completing (designs/approval-detail-completeness.md §10). A
            # failed/halted run releases nothing, so it isn't gated.
            if terminal_state == "complete":
                gated, approvers = await self._post_gate_applies(session, study)
                if gated:
                    study.state = "pending_results_approval"
                    study.stop_reason = stop_reason
                    study.state_changed_at = _utcnow()
                    await session.flush()
                    from matofx.approvals import make_approval_service  # noqa: PLC0415

                    await make_approval_service(self.audit).create_request(
                        session,
                        optimisation_study_id=study.id,
                        kind="post",
                        requested_by_user_id=study.created_by_user_id,
                        eligible_approvers=approvers,
                    )
                    await self.audit.record(
                        session,
                        _system_record(
                            action="study.optimisation.results_pending_approval",
                            study_id=study_id,
                            payload={"stop_reason": stop_reason, "approvers": approvers},
                        ),
                    )
                    return
            study.state = terminal_state
            study.stop_reason = stop_reason
            study.state_changed_at = _utcnow()
            await session.flush()
            await self.audit.record(
                session,
                _system_record(
                    action="study.optimisation.batch_completed"
                    if terminal_state == "complete"
                    else "study.optimisation.halted",
                    study_id=study_id,
                    payload={
                        "stop_reason": stop_reason,
                        "state": terminal_state,
                        "last_batch": study.last_batch_number,
                    },
                ),
            )

    # --- loading ---------------------------------------------------------

    async def _load_runnable(
        self, study_id: int
    ) -> tuple[dict[str, Any], StrategyContext, StudyRef, dict[str, Any]] | None:
        async with session_scope(self._factory) as session:
            study = await session.get(OptimisationStudy, study_id)
            if study is None or study.state != "running" or study.archived_at is not None:
                return None
            config = dict(study.config_json)
            response_names = tuple(
                r["name"] for r in (config.get("responses") or []) if "name" in r
            )
            fixed_inputs = dict(config.get("fixed_inputs") or {})
            # Fold the *tool's own* §F30 validity rules into the selection
            # filter so the optimiser obeys them when proposing candidates
            # (a violating point is dropped before it's queued, exactly as
            # the parametric-studies generator drops one). These ride
            # alongside any study-level constraints the wizard added; the
            # candidate is evaluated with ``fixed_inputs`` merged in at the
            # call site (see ``constraints_ok`` use in ``run_study``), so a
            # rule may reference a held-constant input as well as a factor.
            config["constraints"] = list(config.get("constraints") or []) + (
                await self._descriptor_cross_field(session, study)
            )
            if study.target_kind == "workflow":
                # The chain + its dedicated scenario must be pinned (set at
                # configure); without them the run can't be hosted (§3.4).
                if study.workflow_alias is None or study.scenario_id is None:
                    return None
                ref = StudyRef(
                    study_id=study.id,
                    tool_alias=None,
                    tool_version_id=None,
                    created_by_user_id=study.created_by_user_id,
                    response_names=response_names,
                    target_kind="workflow",
                    workflow_alias=study.workflow_alias,
                    scenario_id=study.scenario_id,
                    fixed_inputs=fixed_inputs,
                )
            else:
                if study.tool_alias is None or study.tool_version_id is None:
                    return None
                ref = StudyRef(
                    study_id=study.id,
                    tool_alias=study.tool_alias,
                    tool_version_id=study.tool_version_id,
                    created_by_user_id=study.created_by_user_id,
                    response_names=response_names,
                    fixed_inputs=fixed_inputs,
                )
            # MOP/CoP variable reduction (§5.7): the strategy searches the
            # reduced space (frozen low-CoP factors dropped) and, when MOP
            # picked a best metamodel, the bo surrogate uses that kind. The
            # frozen factors are re-attached at their nominal value before
            # each candidate is evaluated (see run_study).
            effective_config, frozen_values = _apply_mop_reduction(config, study.mop_state_json)
            # §3.6: also drop each sum-check group's *derived* member from the
            # search space — it isn't varied, it's worked out from the others,
            # so the optimiser doesn't waste effort on the redundant direction.
            effective_config = _apply_sum_reduction(effective_config)
            context = build_context(effective_config)
        return config, context, ref, frozen_values

    async def _frozen_values_now(self, study_id: int) -> dict[str, Any]:
        """Current MOP-frozen factors → nominal values, re-read fresh each batch.

        Lets a freeze / unfreeze chosen from the Sensitivity card take effect on
        the next batch without restarting the study. Empty when nothing is
        frozen (so the optimiser varies every factor again).
        """
        async with session_scope(self._factory) as session:
            study = await session.get(OptimisationStudy, study_id)
            if study is None:
                return {}
            config = study.config_json or {}
            mop_state = study.mop_state_json
        frozen = {str(n) for n in ((mop_state or {}).get("frozen_factors") or [])}
        # §3.6 members are never frozen (see _apply_mop_reduction).
        frozen -= _sum_member_keys(config)
        if not frozen:
            return {}
        return {
            f["name"]: _nominal_value(f)
            for f in (config.get("factors") or [])
            if isinstance(f, Mapping) and f.get("name") in frozen
        }

    async def _halt_requested(self, study_id: int) -> bool:
        async with session_scope(self._factory) as session:
            return bool(
                await session.scalar(
                    select(OptimisationStudy.halt_requested).where(OptimisationStudy.id == study_id)
                )
            )


class ToolJobRunner:
    """Production :class:`CandidateRunner`: queues real ``Job`` children (§6.1).

    Each candidate becomes a ``Job`` homed under the optimisation study
    (``scenario_id=None``), queued via the same ``queue_for_runner`` path
    parametric children use. The runner waits for the batch to terminate
    (the supervisor's pickup loop runs the jobs concurrently), then reads
    each child's ``study_result`` row — written by the runner's
    post-completion hook — to build the evaluations.
    """

    def __init__(
        self,
        factory: SessionFactory,
        audit: AuditWriter,
        *,
        environment: str | None = None,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
        batch_timeout_s: float = _DEFAULT_BATCH_TIMEOUT_S,
    ) -> None:
        """Compose over the session factory + audit; pin the staging environment."""
        self._factory = factory
        self.audit = audit
        self.environment = environment
        self.poll_interval_s = poll_interval_s
        self.batch_timeout_s = batch_timeout_s

    async def run_batch(
        self,
        *,
        ref: StudyRef,
        candidates: Sequence[Candidate],
        batch: int,
        rationales: Sequence[dict[str, Any]],
    ) -> list[Evaluation]:
        """Queue the candidates, wait for terminal, and collect responses."""
        from matofx.hierarchy.orm import Job  # noqa: PLC0415
        from matofx.hierarchy.service import make_job_service  # noqa: PLC0415

        queued: list[tuple[int, Candidate]] = []
        async with session_scope(self._factory) as session:
            job_service = make_job_service(self.audit, environment=self.environment)
            new_children: list[tuple[Job, Candidate]] = []
            for i, (cand, rationale) in enumerate(zip(candidates, rationales, strict=True), 1):
                # Held-constant inputs first, then the candidate's varied
                # factors on top (§3.5). A material's version_id string rides
                # along and is frozen by ``queue_for_runner`` below.
                child = Job(
                    scenario_id=None,
                    optimisation_study_id=ref.study_id,
                    optimisation_metadata_json={"batch": batch, **rationale},
                    name=f"opt {ref.study_id} b{batch:04d}#{i:04d}",
                    tool_alias=ref.tool_alias,
                    tool_version_id=ref.tool_version_id,
                    state="created",
                    inputs={**ref.fixed_inputs, **cand},
                    created_by_user_id=ref.created_by_user_id,
                )
                session.add(child)
                new_children.append((child, cand))
            await session.flush()
            for child, cand in new_children:
                await job_service.queue_for_runner(
                    session,
                    job_id=child.id,
                    actor_user_id=ref.created_by_user_id,
                    inputs=child.inputs,
                    access_check=False,
                    audit_via="study",
                )
                queued.append((child.id, cand))

        await self._await_terminal([jid for jid, _ in queued])
        return await self._collect(ref, queued)

    async def _await_terminal(self, job_ids: Sequence[int]) -> None:
        from matofx.hierarchy.orm import Job  # noqa: PLC0415

        deadline = time.monotonic() + self.batch_timeout_s
        while time.monotonic() < deadline:
            async with session_scope(self._factory) as session:
                states = (
                    (await session.execute(select(Job.state).where(Job.id.in_(list(job_ids)))))
                    .scalars()
                    .all()
                )
            if all(s in _TERMINAL_JOB_STATES for s in states):
                return
            await asyncio.sleep(self.poll_interval_s)

    async def _collect(
        self, ref: StudyRef, queued: Sequence[tuple[int, Candidate]]
    ) -> list[Evaluation]:
        from matofx.hierarchy.orm import Job  # noqa: PLC0415
        from matofx.studies.orm import StudyResult  # noqa: PLC0415

        evaluations: list[Evaluation] = []
        async with session_scope(self._factory) as session:
            for job_id, cand in queued:
                job_state = await session.scalar(select(Job.state).where(Job.id == job_id))
                row = (
                    await session.execute(
                        select(StudyResult).where(
                            StudyResult.source_kind == "optimisation",
                            StudyResult.source_id == ref.study_id,
                            StudyResult.job_id == job_id,
                        )
                    )
                ).scalar_one_or_none()
                scalars = dict(row.scalars) if row is not None else {}
                responses = {
                    name: float(scalars[name])
                    for name in ref.response_names
                    if isinstance(scalars.get(name), int | float)
                }
                # Selection-time constraints already passed → feasible, unless
                # the run's outputs were flagged invalid (ADR-0017: non-finite /
                # out-of-bounds), in which case the objective can't be trusted —
                # treat the candidate as infeasible so the optimiser discards it.
                feasible = True
                if row is not None and str(row.validity or "valid") != "valid":
                    feasible = False
                if row is not None:
                    row.feasible = feasible
                evaluations.append(
                    Evaluation(
                        candidate=dict(cand),
                        responses=responses,
                        feasible=feasible,
                        succeeded=job_state == "complete",
                    )
                )
            await session.flush()
        return evaluations


class WorkflowRunRunner:
    """:class:`CandidateRunner` for a ``workflow`` target (ADR-0009 §3.4).

    Each candidate becomes one headless :class:`WorkflowRun` in the
    study's dedicated scenario: the factor vector is distributed to the
    chain's open inputs (``{member: {input: value}}``), the run
    self-cascades to completion (the runner daemon's
    :meth:`WorkflowRunService.advance` hook drives it), and the responses
    are read from the named ``{member, output}`` child-job outputs once
    the whole run terminates. Mirrors :class:`ToolJobRunner`'s
    queue→await→collect shape, one rung up (a run, not a single job).

    Writes one ``study_result`` row per run (``_record_run_result``), keyed on
    the chain's final member job since a workflow candidate has no single job —
    so the Results explorer + per-run summary see workflow runs the same way
    they see tool-target children.
    """

    def __init__(
        self,
        factory: SessionFactory,
        audit: AuditWriter,
        *,
        environment: str | None = None,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
        batch_timeout_s: float = _DEFAULT_BATCH_TIMEOUT_S,
    ) -> None:
        """Compose over the session factory + audit; pin the staging environment."""
        self._factory = factory
        self.audit = audit
        self.environment = environment
        self.poll_interval_s = poll_interval_s
        self.batch_timeout_s = batch_timeout_s

    async def run_batch(
        self,
        *,
        ref: StudyRef,
        candidates: Sequence[Candidate],
        batch: int,
        rationales: Sequence[dict[str, Any]],  # noqa: ARG002 — runs carry no per-job rationale yet
    ) -> list[Evaluation]:
        """Start one headless run per candidate, await all, collect responses."""
        from matofx.workflows.run_service import make_workflow_run_service  # noqa: PLC0415

        async with session_scope(self._factory) as session:
            study = await session.get(OptimisationStudy, ref.study_id)
            config = dict(study.config_json) if study is not None else {}
        factor_map = {
            f["name"]: (f["member"], f["input"])
            for f in (config.get("factors") or [])
            if isinstance(f, Mapping) and "name" in f and "member" in f and "input" in f
        }
        response_specs = [
            (r["name"], r["spec"]["member"], r["spec"].get("name", r["name"]))
            for r in (config.get("responses") or [])
            if isinstance(r, Mapping)
            and isinstance(r.get("spec"), Mapping)
            and "member" in r["spec"]
        ]

        # Held-constant member inputs (§3.5), keyed ``member.input`` → seed
        # every candidate's open inputs with them; the factors override any
        # overlap. Materials carry their version_id string and freeze when
        # ``advance`` queues the member.
        fixed_by_member = _split_member_inputs(ref.fixed_inputs)

        wf_service = make_workflow_run_service(self.audit, environment=self.environment)
        started: list[tuple[int, Candidate]] = []
        async with session_scope(self._factory) as session:
            for i, cand in enumerate(candidates, 1):
                open_inputs = _candidate_open_inputs(cand, fixed_by_member, factor_map)
                run = await wf_service.start_headless_run(
                    session,
                    actor_user_id=ref.created_by_user_id,
                    scenario_id=int(ref.scenario_id) if ref.scenario_id is not None else 0,
                    workflow_alias=str(ref.workflow_alias),
                    open_inputs=open_inputs,
                    optimisation_study_id=ref.study_id,
                    name=f"opt {ref.study_id} b{batch:04d}#{i:04d}",
                )
                started.append((run.id, cand))

        await self._await_runs_terminal([rid for rid, _ in started])
        return await self._collect(started, response_specs, batch=batch, study_id=ref.study_id)

    async def _await_runs_terminal(self, run_ids: Sequence[int]) -> None:
        # Wait on the *run-level* state, not every child job being terminal.
        # A failed upstream member leaves its downstream stuck in ``created``
        # (``advance`` only promotes once upstreams are *complete*), so an
        # all-jobs-terminal check would never fire and the batch would block
        # until ``batch_timeout_s``. ``compute_state`` makes a run with any
        # failed child terminal ("failed"), matching how the workflow itself
        # rolls the run up.
        from matofx.hierarchy.orm import Job  # noqa: PLC0415
        from matofx.workflows.orm import WorkflowRun  # noqa: PLC0415
        from matofx.workflows.run_service import compute_state  # noqa: PLC0415

        ids = list(run_ids)
        deadline = time.monotonic() + self.batch_timeout_s
        while time.monotonic() < deadline:
            async with session_scope(self._factory) as session:
                runs = (
                    (await session.execute(select(WorkflowRun).where(WorkflowRun.id.in_(ids))))
                    .scalars()
                    .all()
                )
                jobs = (
                    (await session.execute(select(Job).where(Job.workflow_run_id.in_(ids))))
                    .scalars()
                    .all()
                )
            jobs_by_run: dict[int, list[Job]] = {}
            for j in jobs:
                if j.workflow_run_id is not None:
                    jobs_by_run.setdefault(int(j.workflow_run_id), []).append(j)
            runs_by_id = {r.id: r for r in runs}
            if all(
                rid in runs_by_id
                and compute_state(runs_by_id[rid], jobs_by_run.get(rid, [])) in _TERMINAL_RUN_STATES
                for rid in ids
            ):
                return
            await asyncio.sleep(self.poll_interval_s)

    async def _collect(
        self,
        started: Sequence[tuple[int, Candidate]],
        response_specs: Sequence[tuple[str, str, str]],
        *,
        batch: int,
        study_id: int,
    ) -> list[Evaluation]:
        from matofx.hierarchy.errors import NotFoundError  # noqa: PLC0415
        from matofx.hierarchy.orm import Job  # noqa: PLC0415
        from matofx.hierarchy.service import make_job_service  # noqa: PLC0415

        evaluations: list[Evaluation] = []
        async with session_scope(self._factory) as session:
            job_service = make_job_service(self.audit, environment=self.environment)
            for run_id, cand in started:
                children = list(
                    (await session.execute(select(Job).where(Job.workflow_run_id == run_id)))
                    .scalars()
                    .all()
                )
                succeeded = bool(children) and all(c.state == "complete" for c in children)
                outputs_by_member: dict[str, dict[str, Any]] = {}
                for child in children:
                    if child.state != "complete":
                        continue
                    try:
                        doc = await job_service.get_output_document(session, job_id=child.id)
                    except NotFoundError:
                        continue
                    outputs_by_member[child.tool_alias] = {
                        e.name: e.value for e in doc.outputs if isinstance(e.name, str)
                    }
                responses: ResponseVector = {}
                for response_name, member, output in response_specs:
                    value = (outputs_by_member.get(member) or {}).get(output)
                    if isinstance(value, int | float):
                        responses[response_name] = float(value)
                # Per-run results row (§3.4) — workflow members carry no
                # ``optimisation_study_id`` and the supervisor never writes one,
                # so the runner does it here: keyed on the chain's final member
                # job, scalars are *every* member output (member-qualified) so
                # the explorer can plot/inspect any of them, and the candidate +
                # batch ride on that job's optimisation metadata for the reader.
                await self._record_run_result(
                    session,
                    study_id=study_id,
                    batch=batch,
                    children=children,
                    candidate=cand,
                    outputs_by_member=outputs_by_member,
                )
                evaluations.append(
                    Evaluation(
                        candidate=dict(cand),
                        responses=responses,
                        feasible=True,
                        succeeded=succeeded,
                    )
                )
        return evaluations

    async def _record_run_result(
        self,
        session: Any,
        *,
        study_id: int,
        batch: int,
        children: list[Any],
        candidate: Candidate,
        outputs_by_member: Mapping[str, Mapping[str, Any]],
    ) -> None:
        """Write one ``study_result`` row for a workflow run (idempotent)."""
        from matofx.studies.orm import StudyResult  # noqa: PLC0415

        if not children:
            return
        # The chain's final member (highest position) keys the row + carries the
        # candidate/batch for the reader.
        anchor = max(children, key=lambda c: (c.workflow_position or 0, c.id))
        scalars = {
            f"{member}.{name}": value
            for member, outs in outputs_by_member.items()
            for name, value in outs.items()
        }
        meta = dict(anchor.optimisation_metadata_json or {})
        meta.update({"batch": batch, "candidate": dict(candidate)})
        anchor.optimisation_metadata_json = meta
        existing = await session.scalar(
            select(StudyResult.id).where(
                StudyResult.source_kind == "optimisation",
                StudyResult.source_id == study_id,
                StudyResult.job_id == anchor.id,
            )
        )
        if existing is None:
            session.add(
                StudyResult(
                    source_kind="optimisation",
                    source_id=study_id,
                    job_id=anchor.id,
                    scalars=scalars,
                )
            )
        await session.flush()


class SurrogateRunner:
    """In-process :class:`CandidateRunner` for ``surrogate`` mode (§6.2).

    Loads the attached model version's bundle once (sha256-verified) and
    predicts each candidate's responses directly — no child jobs, no
    queue. Nothing is written to ``study_result`` (there is no job to key
    the row on); the strategy, the convergence trace, and the persisted
    optimiser envelope carry the run, so resume works the same way the
    in-memory path does.
    """

    def __init__(self, factory: SessionFactory, version_id: int) -> None:
        """Compose over the session factory + the attached version id."""
        self._factory = factory
        self._version_id = version_id
        self._model_runtime: Any = None

    async def run_batch(
        self,
        *,
        ref: StudyRef,  # noqa: ARG002 — Protocol arg
        candidates: Sequence[Candidate],
        batch: int,  # noqa: ARG002 — Protocol arg
        rationales: Sequence[dict[str, Any]],  # noqa: ARG002 — Protocol arg
    ) -> list[Evaluation]:
        """Predict every candidate against the attached surrogate (instant)."""
        model_runtime = await self._load()
        return [
            Evaluation(
                candidate=dict(c),
                responses={k: float(v) for k, v in model_runtime.predict(c).items()},
                feasible=True,
                succeeded=True,
            )
            for c in candidates
        ]

    async def _load(self) -> Any:
        if self._model_runtime is None:
            from matofx.models.orm import SurrogateModelVersion  # noqa: PLC0415
            from matofx.models.runtime import load_surrogate_model_version  # noqa: PLC0415

            async with session_scope(self._factory) as session:
                version = await session.get(SurrogateModelVersion, self._version_id)
                if version is None:
                    raise OptimisationConfigError(
                        f"The attached surrogate version {self._version_id} no longer exists."
                    )
                bundle_path, sha = version.bundle_path, version.bundle_sha256
            self._model_runtime = load_surrogate_model_version(
                bundle_path=bundle_path, expected_sha256=sha, version_id=self._version_id
            )
        return self._model_runtime


# --- module helpers ---------------------------------------------------------


def _key_value(v: Any) -> Any:
    """Normalise a factor value for keying — integral floats collapse to int."""
    if isinstance(v, bool):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _candidate_key(cand: Mapping[str, Any]) -> str:
    """A stable identity for a design point (factor values only, §4.1 dedupe).

    Integral floats collapse to ``int`` so a value produced as ``3`` and one
    round-tripped from storage as ``3.0`` compare equal — without that an
    integer-rounding collision could slip past the dedupe.
    """
    return json.dumps({k: _key_value(cand[k]) for k in sorted(cand)}, sort_keys=True, default=str)


def _dedupe_unseen(candidates: Sequence[Candidate], seen_keys: set[str]) -> list[Candidate]:
    """Candidates whose design is neither already evaluated nor repeated in-batch."""
    out: list[Candidate] = []
    batch_keys: set[str] = set()
    for c in candidates:
        key = _candidate_key(c)
        if key in seen_keys or key in batch_keys:
            continue
        batch_keys.add(key)
        out.append(c)
    return out


def _discrete_capacity(factors: Sequence[Any]) -> int | None:
    """Distinct points in a fully-discrete space, or ``None`` if any is continuous.

    ``None`` (a float factor present) means the space has no finite point count,
    so the dedupe never declares it exhausted. A frozen factor still counts its
    full range here — an over-count that is safe (it only delays the exhausted
    verdict, deferring to the empty-streak guard).
    """
    capacity = 1
    for f in factors:
        if f.kind == "int":
            capacity *= int(f.high) - int(f.low) + 1
        elif f.kind == "categorical":
            capacity *= max(len(f.choices), 1)
        else:
            return None
    return capacity


def _nominal_value(factor: Mapping[str, Any]) -> Any:
    """A frozen factor's held value — box midpoint (numeric) or first choice (categorical)."""
    if factor.get("type") == "categorical":
        choices = factor.get("choices") or []
        return choices[0] if choices else None
    lo, hi = factor.get("min"), factor.get("max")
    if isinstance(lo, int | float) and isinstance(hi, int | float):
        mid = (float(lo) + float(hi)) / 2.0
        return snap_int(mid, lo, hi) if factor.get("type") == "int" else mid
    return lo


def _apply_mop_reduction(
    config: Mapping[str, Any], mop_state: Mapping[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply MOP/CoP variable reduction to the strategy's search space (§5.7).

    Returns ``(config_for_context, frozen_values)``. When the study has frozen
    factors (chosen from the Sensitivity card — independent of the ``mop.enabled``
    auto-freeze flag), those factors are dropped from the config the strategy
    context is built from (so the strategy stops varying them) and returned as
    ``{factor: nominal_value}`` to re-attach at evaluation time. When MOP picked
    a best metamodel, its kind is set as ``surrogate_kind`` so the ``bo``
    strategy fits that kind. A no-op when there's no MOP state at all.
    """
    if not mop_state:
        return dict(config), {}
    factors = config.get("factors") or []
    frozen = {str(n) for n in (mop_state.get("frozen_factors") or [])}
    # Never MOP-freeze a sum-check member (§3.6): a frozen member breaks the
    # in-strategy derivation (decode has no frozen value to use), and the group
    # already removes its own redundant degree of freedom via the derived
    # member, so freezing one buys nothing.
    frozen -= _sum_member_keys(config)
    effective = dict(config)
    if frozen:
        effective["factors"] = [f for f in factors if f.get("name") not in frozen]
    best_kind = mop_state.get("best_kind")
    if best_kind:
        effective["surrogate_kind"] = best_kind
    frozen_values = {
        f["name"]: _nominal_value(f)
        for f in factors
        if isinstance(f, Mapping) and f.get("name") in frozen
    }
    return effective, frozen_values


def _max_runs(config: Mapping[str, Any]) -> int:
    """The mandatory ``max_runs`` budget (validated present at configure)."""
    for criterion in config.get("stopping_criteria") or []:
        if isinstance(criterion, Mapping) and criterion.get("kind") == "max_runs":
            return int(criterion["value"])
    raise ValueError("Study config has no max_runs budget (should be validated).")


def _objective_names(config: Mapping[str, Any]) -> frozenset[str]:
    """The names the strategy is told. Weighted-sum studies use one synthetic key."""
    from matofx.studies.optimisation.config import (  # noqa: PLC0415
        WEIGHTED_SUM_KEY,
        is_weighted_sum,
    )

    if is_weighted_sum(config):
        return frozenset({WEIGHTED_SUM_KEY})
    return frozenset(
        r["name"]
        for r in (config.get("responses") or [])
        if r.get("goal") in {"minimise", "maximise"} and "name" in r
    )


def _with_output_feasibility(evaluation: Evaluation, config: Mapping[str, Any]) -> Evaluation:
    """Re-stamp feasibility from the output constraints; add the weighted-sum scalar.

    Selection-time constraints already passed, so feasibility is the runner's
    flag AND every ``output`` constraint holding on the measured responses (the
    tell-time half of §7). For a weighted-sum study the run's responses are also
    scalarised into the synthetic objective the single-objective strategy
    optimises.
    """
    from matofx.studies.optimisation.config import (  # noqa: PLC0415
        WEIGHTED_SUM_KEY,
        is_weighted_sum,
        weighted_sum_value,
    )

    result = evaluation
    if result.feasible and not output_feasible(result.responses, config):
        result = replace(result, feasible=False)
    if is_weighted_sum(config):
        scalar = weighted_sum_value(result.responses, config)
        if scalar is not None:
            result = replace(result, responses={**result.responses, WEIGHTED_SUM_KEY: scalar})
    return result


def _utcnow() -> Any:
    import datetime as _dt  # noqa: PLC0415

    return _dt.datetime.now(tz=_dt.UTC)


def _system_record(*, action: str, study_id: int, payload: dict[str, Any]) -> Any:
    from matofx.audit.schema import RecordInput  # noqa: PLC0415

    return RecordInput(
        actor="system:runner",
        actor_type="system",
        action=action,
        resource_type="optimisation_study",
        resource_id=str(study_id),
        outcome="success",
        payload=payload,
    )
