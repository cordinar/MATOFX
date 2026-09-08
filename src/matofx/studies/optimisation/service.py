"""OptimisationService — study config + lifecycle (ADR-0009 §14).

This service owns the **request-path** lifecycle: minting a ``draft``,
merging multi-step config edits (DB-only, no disk staging until the
first batch queues — [[feedback_save_db_only]]), the ``configure`` gate
(``validate_config`` → freeze → launch), the per-tool **start-approval
gate** (§14.1) and its approve/reject decisions, ``archive`` (no-delete,
D20), and the ACL-filtered read side. ACL is inherited from the study's
home project (studies-engine §7): read = Viewer+, mutate = Member+.

``configure`` validates + freezes the config, then either parks the
study in ``pending_start_approval`` (raising a directed ``pre``
:class:`ApprovalRequest` when ``_start_gate_applies``) or moves it
straight to ``running``. The **outer-loop runtime** (the propose → run →
learn batch loop, and the campaign-level results-release gate) is a
separate runner-hosted module; this service never touches disk or the
job queue itself — it only decides the request-path state and hands the
approve/deny verdicts back through :meth:`decide_approval`.
"""

from __future__ import annotations

import datetime as _dt
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from matofx.audit.schema import RecordInput
from matofx.hierarchy.errors import NotFoundError
from matofx.hierarchy.orm import ProjectAccessLevel, Scenario
from matofx.hierarchy.service import _require_project_access
from matofx.studies.optimisation.config import (
    factor_precisions,
    validate_config,
    validate_workflow_config,
)
from matofx.studies.optimisation.errors import OptimisationConfigError, OptimisationStateError
from matofx.studies.optimisation.navigator import StrategyRecommendation, recommend_strategy
from matofx.studies.optimisation.orm import OptimisationStudy

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.approvals.orm import ApprovalRequest
    from matofx.audit import AuditWriter
    from matofx.models.service import ModelService
    from matofx.tools.descriptor import ToolDescriptor


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


def _estimate_cost(config: dict[str, Any]) -> dict[str, int]:
    """Campaign footprint for the start gate + wizard (§14.1).

    ``max_runs`` (the budget) + the LHS seed batch (a cohort seed imports
    prior runs, so it costs no new children). The ceiling on children the
    campaign can queue.
    """
    max_runs = 0
    for criterion in config.get("stopping_criteria") or []:
        if isinstance(criterion, dict) and criterion.get("kind") == "max_runs":
            max_runs = int(criterion.get("value") or 0)
            break
    seed = config.get("seed") or {}
    seed_runs = int(seed.get("n") or 0) if seed.get("kind") != "cohort" else 0
    return {
        "max_runs": max_runs,
        "seed_runs": seed_runs,
        "max_total_children": max_runs + seed_runs,
        "batch_size": int(config.get("batch_size", 1) or 1),
    }


class OptimisationService:
    """Config + lifecycle service for optimisation studies.

    Composed over the shared hash-chained audit writer; every mutating
    method records a ``study.optimisation.<verb>`` event (studies-engine
    §8 namespace).
    """

    def __init__(self, audit: AuditWriter) -> None:
        """Compose over the shared audit writer."""
        self.audit = audit

    # --- lifecycle -------------------------------------------------------

    async def create_draft(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        project_id: int,
        name: str,
        tool_alias: str,
        tool_version_id: int,
        description: str = "",
    ) -> OptimisationStudy:
        """Mint a ``draft`` study over one pinned tool version (Member+).

        Validates project access, the name, and that ``tool_version_id``
        belongs to ``tool_alias``. Seeds ``config_json`` with the
        ``tool`` target; factors / responses / strategy / stopping are
        filled in by later :meth:`update_draft` steps before
        :meth:`configure`.
        """
        await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.EDIT,
        )
        cleaned_name = name.strip()
        if not cleaned_name:
            raise OptimisationConfigError("An optimisation study needs a name.")
        await self._require_tool_version(session, tool_alias, tool_version_id)

        study = OptimisationStudy(
            project_id=project_id,
            name=cleaned_name,
            description=description.strip(),
            target_kind="tool",
            tool_alias=tool_alias.strip(),
            tool_version_id=tool_version_id,
            config_json={
                "target": {
                    "kind": "tool",
                    "tool_alias": tool_alias.strip(),
                    "tool_version_id": tool_version_id,
                }
            },
            state="draft",
            created_by_user_id=actor_user_id,
        )
        session.add(study)
        await session.flush()
        await self._audit(
            session,
            study,
            "study.optimisation.created",
            actor_user_id,
            payload={
                "project_id": project_id,
                "name": study.name,
                "tool_alias": study.tool_alias,
                "tool_version_id": tool_version_id,
            },
        )
        return study

    async def create_workflow_draft(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        project_id: int,
        name: str,
        workflow_alias: str,
        workflow_version_id: int | None = None,
        description: str = "",
    ) -> OptimisationStudy:
        """Mint a ``draft`` study over one workflow chain (Member+; §3.4).

        The ``workflow`` sibling of :meth:`create_draft`: each candidate
        will be a full :class:`WorkflowRun`. Factors / responses /
        strategy / stopping are filled in by later :meth:`update_draft`
        steps; the dedicated scenario that hosts the runs is created at
        :meth:`configure` once the config validates.
        """
        from matofx.workflows.orm import Workflow  # noqa: PLC0415

        await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.EDIT,
        )
        cleaned_name = name.strip()
        if not cleaned_name:
            raise OptimisationConfigError("An optimisation study needs a name.")
        workflow = (
            await session.execute(select(Workflow).where(Workflow.alias == workflow_alias))
        ).scalar_one_or_none()
        if workflow is None:
            raise OptimisationConfigError(f"No workflow called {workflow_alias!r}.")

        study = OptimisationStudy(
            project_id=project_id,
            name=cleaned_name,
            description=description.strip(),
            target_kind="workflow",
            workflow_alias=workflow_alias.strip(),
            workflow_version_id=workflow_version_id,
            config_json={
                "target": {
                    "kind": "workflow",
                    "workflow_alias": workflow_alias.strip(),
                    "workflow_version_id": workflow_version_id,
                }
            },
            state="draft",
            created_by_user_id=actor_user_id,
        )
        session.add(study)
        await session.flush()
        await self._audit(
            session,
            study,
            "study.optimisation.created",
            actor_user_id,
            payload={
                "project_id": project_id,
                "name": study.name,
                "workflow_alias": study.workflow_alias,
                "workflow_version_id": workflow_version_id,
            },
        )
        return study

    async def update_draft(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        study_id: int,
        config_patch: dict[str, Any],
    ) -> OptimisationStudy:
        """Merge one config step into ``config_json`` (Member+, draft-only).

        DB-only (no disk staging — [[feedback_save_db_only]]),
        optimistic-locked via the ORM ``version`` column. The patch is a
        shallow top-level merge (each step owns a top-level config key:
        ``factors`` / ``fixed_inputs`` / ``responses`` / ``constraints`` /
        ``strategy`` / ``run_mode`` / ``stopping_criteria`` / …). The
        ``target`` key is frozen at creation and may not be patched.
        """
        study = await self._get_with_access(
            session, study_id, actor_user_id, required=ProjectAccessLevel.EDIT
        )
        if study.state != "draft":
            raise OptimisationStateError(
                f"This study is {study.state!r}; only a draft study can be edited."
            )
        if "target" in config_patch:
            raise OptimisationConfigError(
                "The evaluation target is fixed when the study is created and can't be changed."
            )
        # Reassign (not in-place mutate) so SQLAlchemy flags the JSON
        # column dirty — same discipline the other JSON-column writers use.
        study.config_json = {**study.config_json, **config_patch}
        await session.flush()
        await self._audit(
            session,
            study,
            "study.optimisation.draft_updated",
            actor_user_id,
            payload={"keys": sorted(config_patch)},
        )
        return study

    async def configure(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        study_id: int,
        start_approver_user_id: int | None = None,
    ) -> OptimisationStudy:
        """Validate + freeze the config and launch the study (Member+).

        Runs :func:`validate_config` against the pinned tool descriptor
        (§3.3). On success the study either parks in
        ``pending_start_approval`` — when the per-tool start-approval gate
        applies (:meth:`_start_gate_applies`), recording
        ``start_approver_user_id`` and raising a directed ``pre``
        approval request — or moves ``draft → running`` when no gate
        applies. The seed-batch queue (§11) is owned by the runtime, not
        here.
        """
        study = await self._get_with_access(
            session, study_id, actor_user_id, required=ProjectAccessLevel.EDIT
        )
        if study.state != "draft":
            raise OptimisationStateError(
                f"This study is {study.state!r}; only a draft study can be configured."
            )
        # Initialised before the branch: only the tool path assigns
        # ``precisions``, and the freeze below reads it either way — a
        # workflow-target study raised UnboundLocalError here.
        precisions: dict[str, float] = {}
        if study.target_kind == "workflow":
            members = await self._load_member_descriptors(session, study)
            sum_groups = validate_workflow_config(study.config_json, members)
            # A workflow candidate is a WorkflowRun, which (with its child
            # jobs' 1-of-3 home CHECK) needs a scenario — so the study gets
            # one dedicated scenario in its project to host every run (§3.4).
            await self._ensure_dedicated_scenario(session, study, actor_user_id)
        else:
            descriptor = await self._load_descriptor(session, study)
            sum_groups = validate_config(study.config_json, descriptor)
            precisions = factor_precisions(study.config_json, descriptor)
            if study.config_json.get("run_mode") == "surrogate":
                await self._validate_surrogate_attachment(session, study)
        # Freeze the derived sum-check groups (§3.6) into the config so the
        # runtime holds each group's total exact without reloading the
        # descriptor. Reassign (not mutate) so SQLAlchemy flags the JSON dirty.
        if sum_groups:
            study.config_json = {**study.config_json, "sum_groups": sum_groups}
        # Freeze each float factor's declared step alongside them, for the same
        # reason: the runtime snaps candidates onto it without reloading the
        # descriptor. Without this the optimiser proposes designs at a resolution
        # the tool does not have, and the run record quotes digits it never ran.
        if precisions:
            factors = [
                ({**f, "precision": precisions[f["name"]]} if f.get("name") in precisions else f)
                for f in study.config_json.get("factors") or []
            ]
            study.config_json = {**study.config_json, "factors": factors}

        # Compute-cost start gate (§14.1): if the target tool — or any tool
        # in a workflow chain — requires sign-off, park the study in
        # ``pending_start_approval`` and raise a directed approval request
        # to the nominated optimisation_approver; otherwise launch.
        config = study.config_json
        gated = await self._start_gate_applies(session, study)
        terminal_state = "running"
        if gated:
            approver = await self._validate_start_approver(session, study, start_approver_user_id)
            study.state = "pending_start_approval"
            study.start_approver_user_id = approver.id
            study.state_changed_at = _utcnow()
            study.state_changed_by_user_id = actor_user_id
            await session.flush()
            estimate = _estimate_cost(config)
            from matofx.approvals import make_approval_service  # noqa: PLC0415

            await make_approval_service(self.audit).create_request(
                session,
                optimisation_study_id=study.id,
                kind="pre",
                requested_by_user_id=actor_user_id,
                eligible_approvers=[(approver.username or "").strip().lower()],
                designated_approver_user_id=approver.id,
                justification=(
                    f"Optimisation start gate: up to {estimate['max_total_children']} runs "
                    f"(budget {estimate['max_runs']} + seed {estimate['seed_runs']})."
                ),
            )
            terminal_state = "pending_start_approval"
        else:
            study.state = "running"
            study.state_changed_at = _utcnow()
            study.state_changed_by_user_id = actor_user_id
            await session.flush()

        await self._audit(
            session,
            study,
            "study.optimisation.configured",
            actor_user_id,
            payload={
                "target": study.target_kind,
                "state": terminal_state,
                "strategy": config.get("strategy"),
                "run_mode": config.get("run_mode"),
                "factor_count": len(config.get("factors") or []),
                "response_count": len(config.get("responses") or []),
                "constraint_count": len(config.get("constraints") or []),
            },
        )
        return study

    async def estimate_cost(
        self, session: AsyncSession, *, actor_user_id: int, study_id: int
    ) -> dict[str, int]:
        """Campaign footprint for the start gate + create wizard (Viewer+; §14.1)."""
        study = await self._get_with_access(
            session, study_id, actor_user_id, required=ProjectAccessLevel.VIEW
        )
        return _estimate_cost(dict(study.config_json or {}))

    async def list_eligible_approvers(
        self, session: AsyncSession, *, actor_user_id: int
    ) -> list[tuple[int, str]]:
        """``(user_id, username)`` of every other optimisation_approver (§14.1).

        Drives the create-wizard's start-approver picker. Excludes the
        actor (no self-approval); empty when no one else holds the
        capability.
        """
        from matofx.users.orm import User  # noqa: PLC0415
        from matofx.users.permissions import Permission, user_has_permission  # noqa: PLC0415

        out: list[tuple[int, str]] = []
        for uid, uname in (await session.execute(select(User.id, User.username))).all():
            if uid == actor_user_id:
                continue
            if await user_has_permission(session, uid, Permission.OPTIMISATION_APPROVE):
                out.append((int(uid), str(uname)))
        return out

    async def approve_start(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        actor_username: str,
        study_id: int,
        decider_is_admin: bool = False,
        reason: str | None = None,
    ) -> OptimisationStudy:
        """The nominated approver grants the start gate → ``running`` (§14.1)."""
        return await self._decide_start(
            session,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            study_id=study_id,
            decision="approve",
            decider_is_admin=decider_is_admin,
            reason=reason,
        )

    async def reject_start(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        actor_username: str,
        study_id: int,
        decider_is_admin: bool = False,
        reason: str | None = None,
    ) -> OptimisationStudy:
        """The nominated approver declines the start gate → ``cancelled`` (§14.1)."""
        return await self._decide_start(
            session,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            study_id=study_id,
            decision="deny",
            decider_is_admin=decider_is_admin,
            reason=reason,
        )

    async def _decide_start(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        actor_username: str,
        study_id: int,
        decision: str,
        decider_is_admin: bool,
        reason: str | None,
    ) -> OptimisationStudy:
        """Resolve the pending start gate, then transition the study (§14.1).

        Access is governed by the approval itself (the decider must be the
        nominated ``optimisation_approver`` or an admin — enforced by
        :meth:`ApprovalService.decide`), not project ACL: the directed
        approver needn't be a project member.
        """
        from matofx.approvals import make_approval_service  # noqa: PLC0415
        from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415
        from matofx.users.permissions import Permission, user_has_permission  # noqa: PLC0415

        study = await session.get(OptimisationStudy, study_id)
        if study is None:
            raise NotFoundError("optimisation study", study_id)
        if study.state != "pending_start_approval":
            raise OptimisationStateError(
                f"This study is {study.state!r}; only a study awaiting start approval "
                "can be approved or rejected."
            )
        request = (
            await session.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.optimisation_study_id == study_id,
                    ApprovalRequest.kind == "pre",
                    ApprovalRequest.status == "pending",
                )
            )
        ).scalar_one_or_none()
        if request is None:
            raise OptimisationStateError("No pending start-approval request for this study.")

        is_approver = await user_has_permission(
            session, actor_user_id, Permission.OPTIMISATION_APPROVE
        )
        await make_approval_service(self.audit).decide(
            session,
            request_id=request.id,
            decider_user_id=actor_user_id,
            decider_username=actor_username,
            decision=decision,  # type: ignore[arg-type]
            reason=reason,
            decider_is_approver=is_approver,
            decider_is_admin=decider_is_admin,
        )
        if decision == "approve":
            study.state = "running"
        else:
            study.state = "cancelled"
            study.stop_reason = "start_rejected"
        study.state_changed_at = _utcnow()
        study.state_changed_by_user_id = actor_user_id
        await session.flush()
        await self._audit(
            session,
            study,
            "study.optimisation.start_approved"
            if decision == "approve"
            else "study.optimisation.start_rejected",
            actor_user_id,
            payload={"request_id": request.id, "reason": reason},
        )
        return study

    async def decide_approval(
        self,
        session: AsyncSession,
        *,
        request_id: int,
        decision: str,
        reason: str | None,
        decider_user_id: int,
        decider_username: str,
        decider_is_approver: bool,
        decider_is_admin: bool,
    ) -> tuple[ApprovalRequest, OptimisationStudy]:
        """Decide an optimisation start (``pre``) or results-release (``post``) gate.

        Routed here from the approvals inbox (``submit_decisions``) so an
        optimisation gate is decided exactly like any other. The ``pre``
        gate's authority is the directed ``OPTIMISATION_APPROVE``
        capability (resolved inside :meth:`_decide_start`); the ``post``
        gate uses the tool-approver flag (``decider_is_approver``), the
        same authority the other results-release gates use.
        """
        from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415

        request = await session.get(ApprovalRequest, request_id)
        if request is None or request.optimisation_study_id is None:
            raise OptimisationStateError("No such optimisation approval request.")
        study = await session.get(OptimisationStudy, request.optimisation_study_id)
        if study is None:
            raise NotFoundError("optimisation study", request.optimisation_study_id)
        if request.kind == "pre":
            study = await self._decide_start(
                session,
                actor_user_id=decider_user_id,
                actor_username=decider_username,
                study_id=study.id,
                decision=decision,
                decider_is_admin=decider_is_admin,
                reason=reason,
            )
            return request, study
        return await self._decide_results(
            session,
            request=request,
            study=study,
            decision=decision,
            reason=reason,
            decider_user_id=decider_user_id,
            decider_username=decider_username,
            decider_is_approver=decider_is_approver,
            decider_is_admin=decider_is_admin,
        )

    async def _decide_results(
        self,
        session: AsyncSession,
        *,
        request: ApprovalRequest,
        study: OptimisationStudy,
        decision: str,
        reason: str | None,
        decider_user_id: int,
        decider_username: str,
        decider_is_approver: bool,
        decider_is_admin: bool,
    ) -> tuple[ApprovalRequest, OptimisationStudy]:
        """Release (``approve`` → ``complete``) or withhold (``deny`` → ``cancelled``).

        The results stay withheld for the whole non-terminal window; an
        approval flips the study to ``complete`` (results become visible),
        a denial to ``cancelled`` with ``stop_reason='results_rejected'``.
        """
        from matofx.approvals import make_approval_service  # noqa: PLC0415

        if study.state != "pending_results_approval":
            raise OptimisationStateError(
                f"This study is {study.state!r}; only a study awaiting results "
                "release can be approved or rejected."
            )
        await make_approval_service(self.audit).decide(
            session,
            request_id=request.id,
            decider_user_id=decider_user_id,
            decider_username=decider_username,
            decision=decision,  # type: ignore[arg-type]
            reason=reason,
            decider_is_approver=decider_is_approver,
            decider_is_admin=decider_is_admin,
        )
        if decision == "approve":
            study.state = "complete"
        else:
            study.state = "cancelled"
            study.stop_reason = "results_rejected"
        study.state_changed_at = _utcnow()
        study.state_changed_by_user_id = decider_user_id
        await session.flush()
        await self._audit(
            session,
            study,
            "study.optimisation.results_released"
            if decision == "approve"
            else "study.optimisation.results_rejected",
            decider_user_id,
            payload={"request_id": request.id, "reason": reason},
        )
        return request, study

    async def _start_gate_applies(self, session: AsyncSession, study: OptimisationStudy) -> bool:
        """Whether the campaign needs start approval — any target tool flagged (§14.1)."""
        from matofx.tools.orm import Tool  # noqa: PLC0415

        aliases: list[str] = []
        if study.target_kind == "workflow":
            from matofx.workflows.orm import Workflow, WorkflowMember  # noqa: PLC0415

            workflow = (
                await session.execute(
                    select(Workflow).where(Workflow.alias == study.workflow_alias)
                )
            ).scalar_one_or_none()
            if workflow is None:
                return False
            aliases = list(
                (
                    await session.execute(
                        select(WorkflowMember.tool_alias).where(
                            WorkflowMember.workflow_id == workflow.id
                        )
                    )
                )
                .scalars()
                .all()
            )
        elif study.tool_alias:
            aliases = [study.tool_alias]
        if not aliases:
            return False
        flagged = (
            await session.execute(
                select(Tool.alias).where(
                    Tool.alias.in_(aliases),
                    Tool.optimisation_start_approval_required.is_(True),
                )
            )
        ).first()
        return flagged is not None

    async def _validate_start_approver(
        self,
        session: AsyncSession,
        study: OptimisationStudy,
        approver_user_id: int | None,
    ) -> Any:
        """The nominated approver exists, holds the capability, and isn't the submitter."""
        from matofx.users.orm import User  # noqa: PLC0415
        from matofx.users.permissions import Permission, user_has_permission  # noqa: PLC0415

        if approver_user_id is None:
            raise OptimisationConfigError(
                "This study's tool requires a nominated approver to start the campaign."
            )
        if approver_user_id == study.created_by_user_id:
            raise OptimisationConfigError("You can't nominate yourself as the start-gate approver.")
        approver = await session.get(User, approver_user_id)
        if approver is None:
            raise OptimisationConfigError("The nominated approver no longer exists.")
        if not await user_has_permission(
            session, approver_user_id, Permission.OPTIMISATION_APPROVE
        ):
            raise OptimisationConfigError(
                "The nominated approver isn't in the optimisation-approver pool."
            )
        return approver

    async def archive(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        study_id: int,
    ) -> OptimisationStudy:
        """Archive a study — hide from default lists, never delete (D20).

        The ``state`` keeps its terminal value; ``archived_at`` /
        ``archived_by_user_id`` record the action.
        """
        study = await self._get_with_access(
            session, study_id, actor_user_id, required=ProjectAccessLevel.EDIT
        )
        if study.archived_at is not None:
            return study
        study.archived_at = _utcnow()
        study.archived_by_user_id = actor_user_id
        await session.flush()
        await self._audit(session, study, "study.optimisation.archived", actor_user_id, payload={})
        return study

    async def request_halt(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        study_id: int,
    ) -> OptimisationStudy:
        """Request a graceful halt (Member+); the loop stops at the next batch (§14).

        Sets ``halt_requested`` — the runtime reads it at the top of the
        outer loop, lets in-flight children finish, and stops without
        queuing more batches. A no-op on a study that isn't ``running``.
        """
        study = await self._get_with_access(
            session, study_id, actor_user_id, required=ProjectAccessLevel.EDIT
        )
        if study.state == "running" and not study.halt_requested:
            study.halt_requested = True
            await session.flush()
            await self._audit(
                session, study, "study.optimisation.halt_requested", actor_user_id, payload={}
            )
        return study

    async def extend_budget(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        study_id: int,
        additional_runs: int,
    ) -> OptimisationStudy:
        """Continue a completed study by raising its run budget (Member+; §14).

        Only a (non-archived) ``complete`` study can continue: it resumes from
        its persisted optimiser state — the strategy rebuilds its archive /
        refits its surrogate from the told history and starts fresh batches —
        runs until the new ``max_runs``, then stops again. The seed isn't
        re-run (the study already has data). The scan loop picks the study up
        once it's back in ``running``. Raises on a non-positive bump or a study
        that isn't a plain completed study.
        """
        if additional_runs < 1:
            raise OptimisationConfigError("Add at least one run to continue the study.")
        study = await self._get_with_access(
            session, study_id, actor_user_id, required=ProjectAccessLevel.EDIT
        )
        if study.archived_at is not None:
            raise OptimisationStateError("This study is archived; un-archive it before continuing.")
        if study.state != "complete":
            raise OptimisationStateError(
                f"This study is {study.state!r}; only a completed study can be continued."
            )
        config = dict(study.config_json or {})
        criteria = [dict(c) for c in (config.get("stopping_criteria") or [])]
        bumped = False
        for criterion in criteria:
            if criterion.get("kind") == "max_runs":
                criterion["value"] = int(criterion.get("value") or 0) + additional_runs
                bumped = True
        if not bumped:
            criteria.append({"kind": "max_runs", "value": additional_runs})
        config["stopping_criteria"] = criteria
        study.config_json = config
        study.state = "running"
        study.stop_reason = None
        study.halt_requested = False
        study.state_changed_at = _utcnow()
        study.state_changed_by_user_id = actor_user_id
        await session.flush()
        await self._audit(
            session,
            study,
            "study.optimisation.continued",
            actor_user_id,
            payload={"additional_runs": additional_runs},
        )
        return study

    async def get(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        study_id: int,
    ) -> OptimisationStudy:
        """Fetch a study (Viewer+ on its home project)."""
        return await self._get_with_access(
            session, study_id, actor_user_id, required=ProjectAccessLevel.VIEW
        )

    async def run_results(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        study_id: int,
    ) -> list[dict[str, Any]]:
        """Per-run results for the study (Viewer+) — one row per child job.

        Each row carries the run's batch number, state, varied inputs, and
        the scalar outputs recorded into ``study_result`` (``source_kind=
        'optimisation'``). Failed / still-running children appear with an
        empty ``scalars`` map so the explorer never silently drops a run.
        Objective / constraint / feasibility / Pareto derivation is left to
        the caller (the UI has the study config to hand). Mirrors the
        parametric ``ResultsService.get_results_for_study`` shape.
        """
        from matofx.hierarchy.orm import Job  # noqa: PLC0415
        from matofx.studies.orm import StudyResult  # noqa: PLC0415

        study = await self._get_with_access(
            session, study_id, actor_user_id, required=ProjectAccessLevel.VIEW
        )
        if study.target_kind == "workflow":
            # A workflow candidate is a run, not a job: its results row (written
            # by ``WorkflowRunRunner._record_run_result``) is keyed on the
            # chain's final member job, which carries the candidate + batch on
            # its optimisation metadata. Find the rows via the study, not via
            # ``Job.optimisation_study_id`` (member jobs don't carry it).
            wf_stmt = (
                select(Job, StudyResult)
                .join(StudyResult, StudyResult.job_id == Job.id)
                .where(
                    StudyResult.source_kind == "optimisation",
                    StudyResult.source_id == study.id,
                )
                .order_by(StudyResult.id.asc())
            )
            rows = (await session.execute(wf_stmt)).all()
            results: list[dict[str, Any]] = []
            for job, result in rows:
                meta = (
                    job.optimisation_metadata_json
                    if isinstance(job.optimisation_metadata_json, dict)
                    else {}
                )
                batch = meta.get("batch")
                candidate = meta.get("candidate")
                results.append(
                    {
                        "job_id": int(job.id),
                        "batch": int(batch) if isinstance(batch, int) else None,
                        "state": str(job.state),
                        "inputs": dict(candidate) if isinstance(candidate, dict) else {},
                        "scalars": dict(result.scalars or {}),
                    }
                )
            return results
        stmt = (
            select(Job, StudyResult)
            .outerjoin(
                StudyResult,
                (StudyResult.job_id == Job.id) & (StudyResult.source_kind == "optimisation"),
            )
            .where(Job.optimisation_study_id == study.id)
            .order_by(Job.id.asc())
        )
        rows = (await session.execute(stmt)).all()
        results = []
        for job, result in rows:
            meta = (
                job.optimisation_metadata_json
                if isinstance(job.optimisation_metadata_json, dict)
                else {}
            )
            batch = meta.get("batch")
            results.append(
                {
                    "job_id": int(job.id),
                    "batch": int(batch) if isinstance(batch, int) else None,
                    "state": str(job.state),
                    "inputs": dict(job.inputs or {}),
                    "scalars": dict(result.scalars or {}) if result is not None else {},
                }
            )
        return results

    async def recommend(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        study_id: int,
    ) -> StrategyRecommendation:
        """Suggest a strategy for the draft's current config (Viewer+; §5.6).

        The Navigator reads the study's saved config (factors / responses
        / run-mode chosen so far) and proposes a strategy + acquisition +
        batch size with a one-line rationale. Pure heuristic — no DB write,
        no validation; the wizard pre-selects it and the user can override.
        """
        study = await self._get_with_access(
            session, study_id, actor_user_id, required=ProjectAccessLevel.VIEW
        )
        return recommend_strategy(study.config_json or {})

    async def list_for_project(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        project_id: int,
        include_archived: bool = False,
    ) -> list[OptimisationStudy]:
        """List a project's studies (Viewer+); archived hidden by default."""
        await _require_project_access(
            session,
            project_id=project_id,
            actor_user_id=actor_user_id,
            required=ProjectAccessLevel.VIEW,
        )
        stmt = select(OptimisationStudy).where(OptimisationStudy.project_id == project_id)
        if not include_archived:
            stmt = stmt.where(OptimisationStudy.archived_at.is_(None))
        stmt = stmt.order_by(OptimisationStudy.created_at.desc(), OptimisationStudy.id.desc())
        return list((await session.execute(stmt)).scalars().all())

    async def publish_current_surrogate(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        study_id: int,
        model_service: ModelService,
        name: str | None = None,
        scope: str = "personal",
        surrogate_kind: str | None = None,
    ) -> Any:
        """Save the study's working surrogate into the Model registry (amendment).

        A ``real``-mode optimisation study is surrogate-assisted by
        construction (the ``bo`` strategy fits MATOFX's own surrogate on the
        runs). This publishes a **governed, versioned copy** — fit from the
        study's own completed runs (``source_kind='optimisation'``) — into
        the registry on demand. Member+ on the study's project; the
        ``ModelService`` is passed in so this service stays free of the
        bundle-storage plumbing. Returns the new ``SurrogateModelVersion``.
        """
        study = await self._get_with_access(
            session, study_id, actor_user_id, required=ProjectAccessLevel.EDIT
        )
        if study.tool_alias is None:
            raise OptimisationConfigError("Only a tool-target study can publish a surrogate.")
        config = study.config_json
        response_names = [
            r["name"]
            for r in (config.get("responses") or [])
            if isinstance(r, dict) and r.get("name")
        ]
        if not response_names:
            raise OptimisationConfigError("The study has no responses to build a surrogate from.")
        model_name = (name or f"{study.name} surrogate").strip()
        kind = surrogate_kind or config.get("surrogate_kind") or "gp"
        project_id = study.project_id if scope == "project" else None
        # `create_model` returns (model, version, excluded). The third
        # value arrived with ADR-0017's per-output validity filtering and
        # this caller was never updated, so publishing a surrogate from
        # an optimisation study raised `ValueError: too many values to
        # unpack` — the feature was simply broken.
        model, version, excluded = await model_service.create_model(
            session,
            actor_user_id=actor_user_id,
            name=model_name,
            tool_alias=study.tool_alias,
            scope=scope,
            source_kind="optimisation",
            source_id=study.id,
            response_names=response_names,
            project_id=project_id,
            surrogate_kind=kind,
            description=f"Published from optimisation study {study.id}.",
        )
        await self._audit(
            session,
            study,
            "study.optimisation.surrogate_published",
            actor_user_id,
            payload={
                "model_id": model.id,
                "version_id": version.id,
                "scope": scope,
                # Outputs dropped from training because too few of their
                # values were valid (ADR-0017). Recorded rather than
                # discarded: a surrogate published having silently lost a
                # response is exactly the thing somebody needs to be able
                # to find out about afterwards.
                "excluded": excluded or {},
            },
        )
        return version

    async def compute_mop(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        study_id: int,
        cutoff: float | None = None,
    ) -> dict[str, Any]:
        """Run a MOP/CoP analysis over the study's runs + persist it (Member+; §5.7).

        Fits the best-Q² metamodel over the study's completed candidates,
        attributes a Coefficient of Prognosis to each factor, and records
        the result + the suggested low-CoP prune set in ``mop_state_json``.
        A previously-set ``frozen_factors`` user override is preserved.
        Tool-target only (the workflow-target results store is a later
        slice). Available on demand regardless of the ``mop.enabled`` auto-
        freeze flag; raises only if there's nothing to analyse.
        """
        from matofx.hierarchy.orm import Job  # noqa: PLC0415
        from matofx.studies.optimisation.mop import (  # noqa: PLC0415
            DEFAULT_COP_CUTOFF,
            fit_best_metamodel,
        )
        from matofx.studies.orm import StudyResult  # noqa: PLC0415

        study = await self._get_with_access(
            session, study_id, actor_user_id, required=ProjectAccessLevel.EDIT
        )
        if study.tool_alias is None:
            raise OptimisationConfigError("MOP/CoP analysis is available for tool-target studies.")
        config = study.config_json
        # Sensitivity/CoP analysis is available on demand for any surrogate
        # study; the ``mop.enabled`` flag only governs auto-freezing low-CoP
        # factors *during* the search, not whether the analysis can be viewed.
        objective = next(
            (
                r["name"]
                for r in (config.get("responses") or [])
                if isinstance(r, dict) and r.get("goal") in {"minimise", "maximise"}
            ),
            None,
        )
        if objective is None:
            raise OptimisationConfigError("The study has no optimised response to analyse.")
        factor_names = [
            f["name"] for f in (config.get("factors") or []) if isinstance(f, dict) and "name" in f
        ]

        rows = (
            await session.execute(
                select(StudyResult.scalars, Job.inputs)
                .join(Job, StudyResult.job_id == Job.id)
                .where(
                    StudyResult.source_kind == "optimisation",
                    StudyResult.source_id == study_id,
                )
            )
        ).all()
        factor_columns: dict[str, list[float | None]] = {name: [] for name in factor_names}
        response: list[float | None] = []
        for scalars, inputs in rows:
            value = (scalars or {}).get(objective)
            if not isinstance(value, int | float) or isinstance(value, bool):
                continue
            for name in factor_names:
                factor_columns[name].append((inputs or {}).get(name))
            response.append(float(value))

        result = fit_best_metamodel(factor_columns, response)
        applied_cutoff = (
            float(cutoff)
            if cutoff is not None
            else float((config.get("mop") or {}).get("cop_cutoff", DEFAULT_COP_CUTOFF))
        )
        suggested = result.prunable(applied_cutoff)
        # Preserve a prior user override of the frozen set; otherwise adopt
        # the freshly-suggested prune (§5.7 — the user can override later).
        existing = (study.mop_state_json or {}).get("frozen_factors")
        frozen = existing if existing is not None else suggested
        study.mop_state_json = {
            "best_kind": result.best_kind,
            "q2": result.q2,
            "cop": [
                {"factor": c.factor, "cop": c.cop, "total_effect": c.total_effect}
                for c in result.cop
            ],
            "kind_q2": result.kind_q2,
            "cutoff": applied_cutoff,
            "suggested_prune": suggested,
            "frozen_factors": frozen,
            "message": result.message,
            "n_runs": len(response),
            "objective": objective,
            "updated_at": _utcnow().isoformat(),
        }
        await session.flush()
        await self._audit(
            session,
            study,
            "study.optimisation.mop_computed",
            actor_user_id,
            payload={
                "best_kind": result.best_kind,
                "n_runs": len(response),
                "suggested_prune": suggested,
            },
        )
        return study.mop_state_json

    async def set_mop_frozen_factors(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        study_id: int,
        frozen_factors: list[str],
    ) -> dict[str, Any]:
        """Override which low-CoP factors are frozen out of the search (Member+; §5.7)."""
        study = await self._get_with_access(
            session, study_id, actor_user_id, required=ProjectAccessLevel.EDIT
        )
        if study.mop_state_json is None:
            raise OptimisationConfigError("Run a MOP analysis before choosing frozen factors.")
        factor_names = {
            f["name"] for f in (study.config_json.get("factors") or []) if isinstance(f, dict)
        }
        unknown = sorted(set(frozen_factors) - factor_names)
        if unknown:
            raise OptimisationConfigError(
                "These aren't factors of the study: " + ", ".join(unknown) + "."
            )
        study.mop_state_json = {
            **study.mop_state_json,
            "frozen_factors": sorted(set(frozen_factors)),
        }
        await session.flush()
        await self._audit(
            session,
            study,
            "study.optimisation.mop_frozen_set",
            actor_user_id,
            payload={"frozen_factors": sorted(set(frozen_factors))},
        )
        return study.mop_state_json

    # --- internals -------------------------------------------------------

    async def _require_tool_version(
        self, session: AsyncSession, tool_alias: str, tool_version_id: int
    ) -> None:
        """The version exists and belongs to ``tool_alias`` (mirrors cohort §3.2)."""
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        alias = (
            await session.execute(
                select(ToolVersion.alias).where(ToolVersion.id == tool_version_id)
            )
        ).scalar_one_or_none()
        if alias is None:
            raise OptimisationConfigError(f"Tool version {tool_version_id} doesn't exist.")
        if alias != tool_alias.strip():
            raise OptimisationConfigError(
                f"Tool version {tool_version_id} belongs to '{alias}', not '{tool_alias}'. "
                "An optimisation study is keyed on a single tool."
            )

    async def _load_descriptor(
        self, session: AsyncSession, study: OptimisationStudy
    ) -> ToolDescriptor:
        """Parse the study's pinned tool-version descriptor snapshot."""
        from matofx.tools.descriptor import ToolDescriptor  # noqa: PLC0415
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        if study.tool_version_id is None:
            raise OptimisationConfigError("The study has no pinned tool version.")
        tool_version = await session.get(ToolVersion, study.tool_version_id)
        if tool_version is None:
            raise NotFoundError("tool version", study.tool_version_id)
        try:
            return ToolDescriptor.model_validate(tool_version.descriptor_snapshot)
        except ValueError as exc:
            raise OptimisationConfigError(
                "The tool's descriptor couldn't be read for validation."
            ) from exc

    async def _load_member_descriptors(
        self, session: AsyncSession, study: OptimisationStudy
    ) -> dict[str, ToolDescriptor]:
        """Parse every member tool's current descriptor for a workflow target (§3.4)."""
        from matofx.tools.descriptor import ToolDescriptor  # noqa: PLC0415
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415
        from matofx.workflows.orm import Workflow, WorkflowMember  # noqa: PLC0415

        if study.workflow_alias is None:
            raise OptimisationConfigError("The study has no workflow target configured.")
        workflow = (
            await session.execute(select(Workflow).where(Workflow.alias == study.workflow_alias))
        ).scalar_one_or_none()
        if workflow is None:
            raise OptimisationConfigError(
                f"Workflow {study.workflow_alias!r} no longer exists; it can't be optimised."
            )
        members = (
            (
                await session.execute(
                    select(WorkflowMember)
                    .where(WorkflowMember.workflow_id == workflow.id)
                    .order_by(WorkflowMember.position)
                )
            )
            .scalars()
            .all()
        )
        descriptors: dict[str, ToolDescriptor] = {}
        for member in members:
            version = (
                await session.execute(
                    select(ToolVersion)
                    .where(ToolVersion.alias == member.tool_alias)
                    .order_by(ToolVersion.created_at.desc(), ToolVersion.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if version is None:
                raise OptimisationConfigError(
                    f"Member tool {member.tool_alias!r} has no installed version."
                )
            try:
                descriptors[member.tool_alias] = ToolDescriptor.model_validate(
                    version.descriptor_snapshot
                )
            except ValueError as exc:
                raise OptimisationConfigError(
                    f"The descriptor for member {member.tool_alias!r} couldn't be read."
                ) from exc
        return descriptors

    async def _ensure_dedicated_scenario(
        self, session: AsyncSession, study: OptimisationStudy, actor_user_id: int
    ) -> None:
        """Create the study's dedicated scenario (idempotent) for workflow runs (§3.4)."""
        if study.scenario_id is not None:
            return
        scenario = Scenario(
            project_id=study.project_id,
            name=f"{study.name} — optimisation runs",
            state="active",
        )
        session.add(scenario)
        await session.flush()
        study.scenario_id = scenario.id
        await self._audit(
            session,
            study,
            "study.optimisation.scenario_created",
            actor_user_id,
            payload={"scenario_id": scenario.id},
        )

    async def _validate_surrogate_attachment(
        self, session: AsyncSession, study: OptimisationStudy
    ) -> None:
        """Rule 7 (§3.3) — the attached surrogate version fits the study.

        Checks the version exists, its model is for the study's tool, and
        its factor + response sets cover the study's factors + optimised
        responses. Caller-visibility of the model is enforced by the
        surrogate picker (the create wizard, build Phase 8); the structural
        compatibility checks here are the server-side backstop.
        """
        from matofx.models.orm import SurrogateModel, SurrogateModelVersion  # noqa: PLC0415

        config = study.config_json
        version_id = config.get("surrogate_model_version_id")
        version = await session.get(SurrogateModelVersion, version_id)
        if version is None:
            raise OptimisationConfigError(
                f"The attached surrogate model version {version_id} doesn't exist."
            )
        model = await session.get(SurrogateModel, version.surrogate_model_id)
        if model is None or model.tool_alias != study.tool_alias:
            raise OptimisationConfigError(
                "The attached surrogate is for a different tool than the study."
            )
        version_factors = {f.get("name") for f in (version.factors or [])}
        study_factors = {f.get("name") for f in (config.get("factors") or [])}
        missing_factors = sorted(str(n) for n in study_factors - version_factors)
        if missing_factors:
            raise OptimisationConfigError(
                "The attached surrogate doesn't cover factor(s): "
                + ", ".join(missing_factors)
                + "."
            )
        version_responses = {r.get("name") for r in (version.responses or [])}
        optimised = {
            r.get("name")
            for r in (config.get("responses") or [])
            if r.get("goal") in {"minimise", "maximise"}
        }
        missing_responses = sorted(str(n) for n in optimised - version_responses)
        if missing_responses:
            raise OptimisationConfigError(
                "The attached surrogate doesn't predict response(s): "
                + ", ".join(missing_responses)
                + "."
            )

    async def _get_with_access(
        self,
        session: AsyncSession,
        study_id: int,
        actor_user_id: int,
        *,
        required: ProjectAccessLevel,
    ) -> OptimisationStudy:
        study = await session.get(OptimisationStudy, study_id)
        if study is None:
            raise NotFoundError("optimisation study", study_id)
        await _require_project_access(
            session,
            project_id=study.project_id,
            actor_user_id=actor_user_id,
            required=required,
            resource_type="optimisation_study",
            resource_id=study_id,
        )
        return study

    async def _audit(
        self,
        session: AsyncSession,
        study: OptimisationStudy,
        action: str,
        actor_user_id: int,
        *,
        payload: dict[str, Any],
    ) -> None:
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action=action,
                resource_type="optimisation_study",
                resource_id=str(study.id),
                outcome="success",
                payload=payload,
            ),
        )


def make_optimisation_service(audit: AuditWriter) -> OptimisationService:
    """Wire up an :class:`OptimisationService` over the shared audit writer."""
    return OptimisationService(audit)
