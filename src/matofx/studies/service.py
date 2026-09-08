"""StudyService — the parametric-study lifecycle (designs §5/§6/§8).

A study is a project child, parallel to a scenario, that pins one tool
``(alias, version)`` at creation and fans a generated input matrix out
as child :class:`~matofx.hierarchy.orm.Job` rows.

Slice boundary (2-PS-a): this service owns the **lifecycle** —
``create`` (with tool-version pinning + descriptor freeze, §5/§13.1),
project-ACL-gated ``get`` / ``list_for_project``, ``submit`` (run the
pure generator, fan the surviving points into child ``Job`` rows in
``created`` state, move the study ``draft -> running``), the stored
state machine + :func:`compute_state` rollup, and a study-level
``cancel`` that stamps the cancel flags and cascades to children
(mirroring :class:`~matofx.workflows.run_service.WorkflowRunService`).

Actually enqueueing the children to the runner — which requires
generalising ``JobService``'s scenario→project resolution to also
resolve via ``parametric_study_id`` — plus the per-study concurrency
cap, the lower-priority study lane, the runner-side cancel pickup and
per-child retry are **slice 4 (execution wiring)**. Until then a
submitted study's children sit in ``created`` and the study sits at
``running``.

ACL is inherited wholesale from the parent project, resolved through
the exact same ``_require_project_access`` path a scenario uses
(post-ADR-0002) — there is no per-study ACL.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from matofx.approvals import ApprovalRequest, Decision, make_approval_service
from matofx.audit.schema import AuditActorType, RecordInput
from matofx.hierarchy.errors import NotFoundError
from matofx.hierarchy.orm import Job, ProjectAccessLevel
from matofx.hierarchy.service import _require_project_access, make_job_service
from matofx.studies.errors import NoValidPointsError, StudyStateError
from matofx.studies.factors import FactorSpec
from matofx.studies.generate import SamplingParams, generate
from matofx.studies.orm import STUDY_MODES, ParametricStudy, StudyState

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter

DEFAULT_RUN_CAP = 200
"""Install-default hard ceiling on generated points (design §16 — to be
confirmed against the first pilot's hardware before locking). The
caller resolves the effective value from the install setting and
passes it to :meth:`StudyService.create`, which freezes it on the row."""

# Child-job states that mean "still in flight" for the rollup.
_INFLIGHT: frozenset[str] = frozenset(
    {"created", "queued", "running", "pending_approval", "pending_results"}
)


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


def compute_state(study: ParametricStudy, children: list[Job]) -> StudyState:
    """Roll the child jobs' states up into the study-level state (§6/§8).

    Rules, first match wins:

    1. ``rejected`` is terminal+sticky (a pre-approval rejection, §7).
    2. ``cancel_requested_at`` set -> ``cancelled`` (sticky once asked).
    3. ``draft`` / ``pending_approval`` -> unchanged (no children to
       roll up yet).
    4. No children -> the stored state (defensive).
    5. Every child ``complete`` -> ``complete``.
    6. Any child still in flight -> ``running``.
    7. All children terminal, at least one ``complete`` -> ``partial``.
    8. All children terminal, none ``complete`` -> ``failed``.
    """
    if study.state == "rejected":
        return "rejected"
    if study.cancel_requested_at is not None:
        return "cancelled"
    if study.state in ("draft", "pending_approval") or not children:
        # Not submitted yet (or defensively empty) — keep the stored state.
        return study.state  # type: ignore[return-value]
    states = {c.state for c in children}
    if states <= {"complete"}:
        return "complete"
    if states & _INFLIGHT:
        return "running"
    return "partial" if "complete" in states else "failed"


def _terminal_from_children(children: list[Job]) -> StudyState | None:
    """The rolled-up terminal state from children, ignoring study.state.

    ``None`` when the children aren't all terminal yet (so the rollup
    shouldn't conclude). Used by the post-gate path, which must know
    "what the study *would* settle at" while the study is being held
    at ``pending_approval``.
    """
    if not children:
        return None
    states = {c.state for c in children}
    if states & _INFLIGHT:
        return None
    if states <= {"complete"}:
        return "complete"
    return "partial" if "complete" in states else "failed"


def _plan_hash(study: ParametricStudy, result: Any) -> str:
    """Canonical SHA-256 of the sampling plan (design §7 ``data_hash``).

    Bound into the *signed* audit payload of the approval events — the
    audit chain is hash-chained + Ed25519-signed, so this is the
    "signed data_hash" §7 asks for, consistent with how job-approval
    integrity works (no per-row hash column the job path lacks).
    """
    canonical = json.dumps(
        {
            "study_id": study.id,
            "tool": f"{study.tool_alias}:{study.tool_version_id}",
            "mode": study.mode,
            "factor_spec": study.factor_spec,
            "sampling_params": study.sampling_params,
            "seed": study.seed,
            "generated_count": result.generated_count,
            "skipped_invalid_count": result.skipped_invalid_count,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _results_hash(study: ParametricStudy, children: list[Job]) -> str:
    """Canonical SHA-256 of the results matrix (post-gate ``data_hash``)."""
    canonical = json.dumps(
        {
            "study_id": study.id,
            "children": sorted(
                ({"id": c.id, "state": c.state, "exit_code": c.exit_code} for c in children),
                key=lambda c: c["id"],
            ),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class StudyService:
    """Lifecycle for one parametric study (single-tool, single-shot)."""

    audit: AuditWriter
    environment: str | None = None
    """Threaded into the :class:`JobService` used to enqueue children
    (it derives the working-dir path segment from the environment),
    mirroring how the API builds ``JobService``."""

    # --- mutations -------------------------------------------------------

    async def create(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        project_id: int,
        name: str,
        tool_alias: str,
        mode: str,
        factor_spec: dict[str, Any] | None = None,
        sampling_params: dict[str, Any] | None = None,
        description: str = "",
        seed: int | None = None,
        run_cap: int = DEFAULT_RUN_CAP,
    ) -> ParametricStudy:
        """Create a ``draft`` study in ``project_id``.

        EDIT on the project is required (same gate as creating a
        scenario). The tool's currently-deployed ``(alias, version)``
        is captured now and frozen on the row — every child the study
        later fans out is pinned to this exact ``tool_version`` so a
        mid-study tool update can't split the response surface (§5 /
        §13.1). The descriptor freeze is implicit: ``tool_version``
        snapshots are append-only.

        ``factor_spec`` / ``sampling_params`` may be partial (or empty)
        — a draft is edited incrementally; full validation happens at
        :meth:`submit`.
        """
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.EDIT,
        )

        clean_name = name.strip()
        if not clean_name:
            raise StudyStateError("A study needs a name.")
        if mode not in STUDY_MODES:
            raise StudyStateError(
                f"{mode!r} isn't a sampling mode MATOFX knows. Choose one "
                f"of: {', '.join(STUDY_MODES)}."
            )

        cleaned_alias = tool_alias.strip()
        version_stmt = (
            select(ToolVersion.id, ToolVersion.descriptor_snapshot)
            .where(ToolVersion.alias == cleaned_alias)
            .order_by(ToolVersion.created_at.desc())
            .limit(1)
        )
        version_row = (await session.execute(version_stmt)).first()
        if version_row is None:
            raise NotFoundError("tool", tool_alias)
        tool_version_id, descriptor_snapshot = version_row
        # A surrogate-of-a-surrogate is meaningless — a surrogate tool is a
        # proxy for an existing solver, not a point to sample against
        # (ADR-0008 §7.5). Reject it here with a plain-English message.
        if isinstance(descriptor_snapshot, dict) and descriptor_snapshot.get(
            "surrogate_model_version_id"
        ):
            raise StudyStateError(
                f"{cleaned_alias!r} is a surrogate tool, which can't be a study's "
                "fitting tool — fit a surrogate from a study of the real tool instead."
            )

        study = ParametricStudy(
            project_id=project_id,
            name=clean_name,
            description=description,
            tool_alias=cleaned_alias,
            tool_version_id=int(tool_version_id),
            mode=mode,
            factor_spec=factor_spec or {},
            sampling_params=sampling_params or {},
            run_cap=run_cap,
            state="draft",
            generated_count=0,
            skipped_invalid_count=0,
            seed=seed,
            created_by_user_id=actor_user_id,
        )
        session.add(study)
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="studies.study.create",
                resource_type="parametric_study",
                resource_id=str(study.id),
                outcome="success",
                payload={
                    "project_id": project_id,
                    "name": study.name,
                    "tool": f"{study.tool_alias}:{study.tool_version_id}",
                    "mode": mode,
                    "run_cap": run_cap,
                },
            ),
        )
        return study

    async def submit(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        study_id: int,
    ) -> ParametricStudy:
        """Generate the matrix and either gate the study or fan it out.

        EDIT on the project required; the study must be a ``draft``.
        Runs the pure generator against the **pinned** descriptor
        (``RunCapExceededError`` / ``FactorSpecError`` /
        ``ModeNotApplicableError`` / ``NoValidPointsError`` propagate
        unchanged — all plain-English) and persists the generated /
        skipped counts. A seed is pinned now so the matrix is
        reproducible (design §4) — the approve path re-runs the
        generator and must get the identical plan.

        Then, per the pinned tool's ``approvals_policy`` (design §7):

        - ``pre`` / ``both`` → the **single** study-level pre-gate:
          state ``pending_approval``, one ``ApprovalRequest`` for the
          *study* (never per child); children are generated + queued
          only on approval.
        - ``none`` / ``post`` → fan every surviving point into a child
          ``Job`` and queue it now; the study moves to ``running`` (a
          ``post`` gate, if any, opens at terminal rollup).
        """
        study = await self._get_with_access(
            session, study_id, actor_user_id, required=ProjectAccessLevel.EDIT
        )
        if study.state != "draft":
            raise StudyStateError(
                f"This study is {study.state!r}; only a draft study can be "
                f"submitted. Clone it to run a fresh sweep."
            )

        self._ensure_seed(study)
        descriptor = await self._descriptor(session, study)
        result = self._run_generate(study, descriptor)
        study.generated_count = result.generated_count
        study.skipped_invalid_count = result.skipped_invalid_count

        if descriptor.approvals_policy in ("pre", "both"):
            self._set_state(study, "pending_approval", actor_user_id)
            await session.flush()
            await make_approval_service(self.audit).create_request(
                session,
                parametric_study_id=study.id,
                kind="pre",
                requested_by_user_id=actor_user_id,
                eligible_approvers=list(descriptor.approver_usernames),
                tool_alias=study.tool_alias,
                tool_version_id=study.tool_version_id,
            )
            await self._audit_study(
                session,
                study,
                "studies.study.request_approval",
                actor_user_id,
                payload={
                    "kind": "pre",
                    "data_hash": _plan_hash(study, result),
                    "generated_count": result.generated_count,
                    "skipped_invalid_count": result.skipped_invalid_count,
                },
            )
            return study

        n = await self._enqueue_children(session, study, result, actor_user_id)
        self._set_state(study, "running", actor_user_id)
        await session.flush()
        await self._audit_study(
            session,
            study,
            "studies.study.submit",
            actor_user_id,
            payload={
                "tool": f"{study.tool_alias}:{study.tool_version_id}",
                "mode": study.mode,
                "child_jobs": n,
                "generated_count": result.generated_count,
                "skipped_invalid_count": result.skipped_invalid_count,
                "raw_count": result.raw_count,
                "data_hash": _plan_hash(study, result),
            },
        )
        return study

    # --- approval gate (design §7) ---------------------------------------

    async def decide_approval(
        self,
        session: AsyncSession,
        *,
        request_id: int,
        decider_user_id: int,
        decider_username: str,
        decision: Decision,
        reason: str | None = None,
        decider_is_approver: bool,
        decider_is_admin: bool,
    ) -> tuple[ApprovalRequest, ParametricStudy]:
        """Decide a study's single approval gate (mirrors JobService).

        Delegates the decision + the three-layer self-approval defence
        to :class:`ApprovalService`, then applies the study-level
        side-effects:

        - **pre / approved** → re-run the generator (deterministic —
          the seed is pinned) and fan + queue the children; study →
          ``running``.
        - **pre / denied** → study → ``rejected`` (frozen, immutable
          per D20; the user clones a new study to retry).
        - **post / approved** → the held study settles at its real
          rolled-up terminal (``complete`` / ``partial`` / ``failed``).
        - **post / denied** → study → ``rejected`` (results withheld;
          ``STUDY_STATES`` has no separate withheld terminal — the
          audit reason records that the runs succeeded but release was
          refused).
        """
        request = await make_approval_service(self.audit).decide(
            session,
            request_id=request_id,
            decider_user_id=decider_user_id,
            decider_username=decider_username,
            decision=decision,
            reason=reason,
            decider_is_approver=decider_is_approver,
            decider_is_admin=decider_is_admin,
        )
        if request.parametric_study_id is None:
            raise StudyStateError("That approval request is for a job, not a parametric study.")
        study = await session.get(ParametricStudy, request.parametric_study_id)
        if study is None:
            raise NotFoundError("parametric study", request.parametric_study_id)

        if request.kind == "pre":
            if request.status == "approved":
                descriptor = await self._descriptor(session, study)
                result = self._run_generate(study, descriptor)
                n = await self._enqueue_children(session, study, result, study.created_by_user_id)
                self._set_state(study, "running", decider_user_id)
                await session.flush()
                await self._audit_study(
                    session,
                    study,
                    "studies.study.approve",
                    decider_user_id,
                    payload={
                        "kind": "pre",
                        "child_jobs": n,
                        "data_hash": _plan_hash(study, result),
                        "decided_via": request.decided_via,
                    },
                )
            else:
                self._set_state(study, "rejected", decider_user_id)
                await session.flush()
                await self._audit_study(
                    session,
                    study,
                    "studies.study.reject",
                    decider_user_id,
                    payload={"kind": "pre", "decided_via": request.decided_via},
                    reason=request.decision_reason,
                )
            return request, study

        # kind == "post": the runs already happened (study held at
        # pending_approval by the rollup). Approve → settle at the real
        # terminal; deny → rejected (results withheld).
        children = await self._list_children(session, study.id)
        if request.status == "approved":
            terminal = _terminal_from_children(children) or "complete"
            self._set_state(study, terminal, decider_user_id)
            await session.flush()
            await self._audit_study(
                session,
                study,
                "studies.study.approve",
                decider_user_id,
                payload={
                    "kind": "post",
                    "state": terminal,
                    "data_hash": _results_hash(study, children),
                    "decided_via": request.decided_via,
                },
            )
        else:
            self._set_state(study, "rejected", decider_user_id)
            await session.flush()
            await self._audit_study(
                session,
                study,
                "studies.study.reject",
                decider_user_id,
                payload={
                    "kind": "post",
                    "results_withheld": True,
                    "decided_via": request.decided_via,
                },
                reason=request.decision_reason,
            )
        return request, study

    # --- internals: generation + enqueue ---------------------------------

    def _ensure_seed(self, study: ParametricStudy) -> None:
        """Pin a seed so the matrix is reproducible (design §4).

        Stochastic modes with no seed would otherwise generate a
        *different* matrix on the approve-path re-run than the one the
        approver's ``data_hash`` covered. Pinning once at submit makes
        ``generate`` deterministic for the study's life.
        """
        if study.seed is None:
            study.seed = secrets.randbelow(2**31 - 1)

    async def _descriptor(self, session: AsyncSession, study: ParametricStudy) -> Any:
        from matofx.tools.descriptor import ToolDescriptor  # noqa: PLC0415
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        tool_version = await session.get(ToolVersion, study.tool_version_id)
        if tool_version is None:
            raise NotFoundError("tool", study.tool_alias)
        return ToolDescriptor.model_validate(tool_version.descriptor_snapshot)

    def _run_generate(self, study: ParametricStudy, descriptor: Any) -> Any:
        spec = FactorSpec.from_obj(study.factor_spec)
        sampling = SamplingParams.model_validate(study.sampling_params or {})
        result = generate(
            descriptor,
            spec,
            study.mode,  # type: ignore[arg-type]
            sampling,
            run_cap=study.run_cap,
            seed=study.seed,
        )
        if result.generated_count == 0:
            raise NoValidPointsError(result.raw_count, result.skip_reasons)
        return result

    async def _enqueue_children(
        self,
        session: AsyncSession,
        study: ParametricStudy,
        result: Any,
        owner_user_id: int,
    ) -> int:
        """Create + queue one child ``Job`` per surviving point.

        ``owner_user_id`` owns the child rows (the submitter, or the
        original requester on the approve path). The study already
        passed the project EDIT gate so the per-child gate is skipped.
        """
        children: list[Job] = []
        for i, point in enumerate(result.points, start=1):
            child = Job(
                scenario_id=None,
                parametric_study_id=study.id,
                name=f"{study.name} #{i:05d}",
                tool_alias=study.tool_alias,
                tool_version_id=study.tool_version_id,
                state="created",
                inputs=point,
                created_by_user_id=owner_user_id,
            )
            session.add(child)
            children.append(child)
        await session.flush()
        job_service = make_job_service(self.audit, environment=self.environment)
        for child in children:
            await job_service.queue_for_runner(
                session,
                job_id=child.id,
                actor_user_id=owner_user_id,
                inputs=child.inputs,
                access_check=False,
                audit_via="study",
            )
        return len(children)

    async def _audit_study(
        self,
        session: AsyncSession,
        study: ParametricStudy,
        action: str,
        actor_user_id: int | None,
        *,
        payload: dict[str, Any],
        reason: str | None = None,
    ) -> None:
        actor = f"user:{actor_user_id}" if actor_user_id is not None else "system:runner"
        actor_type: AuditActorType = "user" if actor_user_id is not None else "system"
        await self.audit.record(
            session,
            RecordInput(
                actor=actor,
                actor_type=actor_type,
                action=action,
                resource_type="parametric_study",
                resource_id=str(study.id),
                outcome="success",
                payload=payload,
                reason=reason,
            ),
        )

    async def cancel(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        study_id: int,
        bypass_access: bool = False,
    ) -> ParametricStudy:
        """Cancel the study and signal still-pending children.

        EDIT on the project required. Idempotent (a cancel on an
        already-cancelled study is a no-op). Mirrors
        :meth:`WorkflowRunService.cancel_run`: ``created`` children are
        left alone (the runner never picked them up; slice 4's pickup
        gate skips a cancelled study); ``queued`` / ``running``
        children get ``cancel_requested_at`` stamped for the runner's
        cancel loop; terminal children are untouched.

        ``bypass_access=True`` skips the EDIT gate — used by the Live
        Jobs bulk-cancel surface, which gates on
        :attr:`Permission.LIVE_JOBS_CANCEL` before calling, and whose
        operator may hold no ACL on the study's project.
        """
        if bypass_access:
            study = await session.get(ParametricStudy, study_id)
            if study is None:
                raise NotFoundError("parametric study", study_id)
        else:
            study = await self._get_with_access(
                session, study_id, actor_user_id, required=ProjectAccessLevel.EDIT
            )
        if study.cancel_requested_at is not None:
            return study

        now = _utcnow()
        study.cancel_requested_at = now
        study.cancel_requested_by_user_id = actor_user_id
        children = await self._list_children(session, study.id)
        signalled = 0
        for child in children:
            if child.state in ("queued", "running") and child.cancel_requested_at is None:
                child.cancel_requested_at = now
                child.cancel_requested_by_user_id = actor_user_id
                signalled += 1
        self._set_state(study, "cancelled", actor_user_id)
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="studies.study.cancel",
                resource_type="parametric_study",
                resource_id=str(study.id),
                outcome="success",
                payload={"signalled_children": signalled},
            ),
        )
        return study

    async def retry_child(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        study_id: int,
        job_id: int,
    ) -> Job:
        """Re-run one failed study child (design §6).

        EDIT on the project required. The job must belong to this
        study and be ``failed``. Resets the row to a fresh ``created``
        job (clearing the previous run's working dir / cancel / timing
        fields), re-queues it on the runner, and rolls the study back
        to ``running``.
        """
        study = await self._get_with_access(
            session, study_id, actor_user_id, required=ProjectAccessLevel.EDIT
        )
        child = await session.get(Job, job_id)
        if child is None or child.parametric_study_id != study_id:
            raise NotFoundError("job", job_id)
        if child.state != "failed":
            raise StudyStateError(f"Only a failed run can be retried; this one is {child.state!r}.")

        child.state = "created"
        child.cancel_requested_at = None
        child.cancel_requested_by_user_id = None
        child.started_at = None
        child.completed_at = None
        child.exit_code = None
        child.working_dir_relative = None
        child.storage_location_id = None
        child.scheduler_job_id = None
        child.heartbeat_state = None
        child.heartbeat_last_seen_at = None
        await session.flush()

        job_service = make_job_service(self.audit, environment=self.environment)
        await job_service.queue_for_runner(
            session,
            job_id=child.id,
            actor_user_id=actor_user_id,
            inputs=child.inputs,
            access_check=False,
            audit_via="study_retry",
        )
        if study.state != "running":
            self._set_state(study, "running", actor_user_id)
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="studies.study.child_retry",
                resource_type="parametric_study",
                resource_id=str(study.id),
                outcome="success",
                payload={"job_id": job_id},
            ),
        )
        return child

    async def refresh_state(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int | None,
        study_id: int,
    ) -> ParametricStudy:
        """Recompute the stored state from the children and persist it.

        Two callers:

        - a **user** read path (``actor_user_id`` is an int): VIEW on
          the project is required (an observation, not a mutation the
          user drives);
        - the **runner** rolling a study up as a child finalises
          (``actor_user_id is None``): system-attributed, no ACL gate
          — the runner isn't a user and the study id comes straight
          off the child row.

        Audits ``studies.study.complete`` the first time the study
        reaches a terminal state. Idempotent: a no-op when the
        recomputed state already matches.
        """
        actor: str
        actor_type: AuditActorType
        if actor_user_id is None:
            study = await session.get(ParametricStudy, study_id)
            if study is None:  # child orphaned from its study — nothing to do
                raise NotFoundError("parametric study", study_id)
            actor, actor_type = "system:runner", "system"
        else:
            study = await self._get_with_access(
                session, study_id, actor_user_id, required=ProjectAccessLevel.VIEW
            )
            actor, actor_type = f"user:{actor_user_id}", "user"

        children = await self._list_children(session, study.id)

        # §7 single POST gate: when a running study's children have all
        # settled and the pinned tool is post/both, hold the *whole*
        # study at pending_approval behind one post ApprovalRequest
        # (never per child) instead of concluding. Opened once; the
        # decide path then settles the real terminal.
        if study.state == "running" and study.cancel_requested_at is None:
            terminal = _terminal_from_children(children)
            if terminal is not None:
                descriptor = await self._descriptor(session, study)
                if descriptor.approvals_policy in ("post", "both"):
                    existing = (
                        await session.execute(
                            select(ApprovalRequest.id).where(
                                ApprovalRequest.parametric_study_id == study.id,
                                ApprovalRequest.kind == "post",
                            )
                        )
                    ).first()
                    if existing is None:
                        self._set_state(study, "pending_approval", actor_user_id)
                        await session.flush()
                        await make_approval_service(self.audit).create_request(
                            session,
                            parametric_study_id=study.id,
                            kind="post",
                            requested_by_user_id=study.created_by_user_id,
                            eligible_approvers=list(descriptor.approver_usernames),
                            tool_alias=study.tool_alias,
                            tool_version_id=study.tool_version_id,
                        )
                        await self._audit_study(
                            session,
                            study,
                            "studies.study.request_approval",
                            actor_user_id,
                            payload={
                                "kind": "post",
                                "data_hash": _results_hash(study, children),
                            },
                        )
                        return study
                    # A post request already exists: pending → stay held
                    # (compute_state passes ``pending_approval`` through);
                    # decided → decide_approval already set the terminal.

        new_state = compute_state(study, children)
        if new_state == study.state:
            return study
        was_terminal = study.state in ("complete", "partial", "failed", "cancelled", "rejected")
        self._set_state(study, new_state, actor_user_id)
        await session.flush()
        if not was_terminal and new_state in ("complete", "partial", "failed"):
            await self.audit.record(
                session,
                RecordInput(
                    actor=actor,
                    actor_type=actor_type,
                    action="studies.study.complete",
                    resource_type="parametric_study",
                    resource_id=str(study.id),
                    outcome="success",
                    payload={"state": new_state},
                ),
            )
        return study

    # --- reads -----------------------------------------------------------

    async def get(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        study_id: int,
    ) -> ParametricStudy:
        """Fetch one study; VIEW on the parent project required."""
        return await self._get_with_access(
            session, study_id, actor_user_id, required=ProjectAccessLevel.VIEW
        )

    async def list_for_project(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        project_id: int,
    ) -> list[ParametricStudy]:
        """Every study in a project, newest first; VIEW required."""
        await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.VIEW,
        )
        stmt = (
            select(ParametricStudy)
            .where(ParametricStudy.project_id == project_id)
            .order_by(ParametricStudy.created_at.desc())
        )
        return list((await session.execute(stmt)).scalars())

    async def list_children(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        study_id: int,
    ) -> list[Job]:
        """The study's child jobs (oldest first); VIEW required."""
        await self._get_with_access(
            session, study_id, actor_user_id, required=ProjectAccessLevel.VIEW
        )
        return await self._list_children(session, study_id)

    # --- internals -------------------------------------------------------

    async def _get_with_access(
        self,
        session: AsyncSession,
        study_id: int,
        actor_user_id: int,
        *,
        required: ProjectAccessLevel,
    ) -> ParametricStudy:
        study = await session.get(ParametricStudy, study_id)
        if study is None:
            raise NotFoundError("parametric study", study_id)
        await _require_project_access(
            session,
            project_id=study.project_id,
            actor_user_id=actor_user_id,
            required=required,
            resource_type="parametric study",
            resource_id=study_id,
        )
        return study

    async def _list_children(self, session: AsyncSession, study_id: int) -> list[Job]:
        stmt = (
            select(Job).where(Job.parametric_study_id == study_id).order_by(Job.created_at, Job.id)
        )
        return list((await session.execute(stmt)).scalars())

    def _set_state(
        self, study: ParametricStudy, new_state: StudyState, actor_user_id: int | None
    ) -> None:
        study.state = new_state
        study.state_changed_at = _utcnow()
        # ``None`` = the runner (system) rolled the state up — no user
        # to attribute; the column is nullable.
        study.state_changed_by_user_id = actor_user_id


def make_study_service(audit: AuditWriter, *, environment: str | None = None) -> StudyService:
    """Wire up a :class:`StudyService` from the ambient audit writer.

    ``environment`` is threaded into the child-job ``JobService`` used
    by :meth:`StudyService.submit` / :meth:`retry_child` (API callers
    pass ``settings.environment``; the runner's rollup path doesn't
    enqueue and leaves it unset).
    """
    return StudyService(audit=audit, environment=environment)
