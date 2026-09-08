"""VVService — the V&V (Verification & Validation) record lifecycle (ADR-0011).

Owns the lifecycle of :class:`ToolVVRecord` and :class:`JobVVRecord`
plus the inheritance-aware badge derivation that consumers read at
display time.

**Authorisation:** reads are open to any authenticated user (V&V records
are global within the install). The author-vs-curator split is now live:
authoring, editing, and submitting a *draft* require base-tier
:attr:`~matofx.users.permissions.Permission.VV_CREATE` (or the curate
capability — curators can always author), via :meth:`_require_author`.
Promoting a draft to ``approved`` / ``rejected`` and retiring a record
require :attr:`~matofx.users.permissions.Permission.VV_CURATE`, via
:meth:`_require_curate`.

**Cohort inheritance (the ADR-0011 Option C invariant).** The badge a
consumer sees for tool version Y is computed over every ``tool_vv_record``
attached to a sibling version sharing ``(alias, bundle_hash)`` — the
cohort key from slice 0. The query is a single statement with a
self-join on :class:`ToolVersion` so the consumer never has to know
about the hash; it just calls :meth:`summarise_tool_status` and gets
the union. Job records do NOT inherit — a job claim is about a specific
run on a specific frozen tool version.

**Toggleable directed-approver gate.** Submitting a draft for review
creates an :class:`ApprovalRequest` via :class:`ApprovalService` — the
caller (API layer) passes the resolved ``approval_required`` boolean
read from the ``vv_record_approval`` General Setting (default ON; the
runtime read of the setting lands in slice 7). With it ON, the author
names one other curator; the gate proceeds through the existing decide
endpoint. With it OFF, a curator approves directly via
:meth:`approve_tool_record` / :meth:`approve_job_record` without
needing the request object first.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from sqlalchemy import select

from matofx.audit.schema import RecordInput
from matofx.users.errors import PermissionDeniedError
from matofx.users.permissions import (
    Permission,
    user_has_any_permission,
    user_has_permission,
)
from matofx.vv.errors import (
    JobVVRecordNotFoundError,
    ToolVVRecordNotFoundError,
    VVApproverRequiredError,
    VVDecisionReasonRequiredError,
    VVSoleCuratorError,
    VVStateTransitionError,
)
from matofx.vv.orm import (
    JOB_VV_KINDS,
    TOOL_VV_KINDS,
    JobVVRecord,
    ToolVVRecord,
    can_transition,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter


# The approval kind used for a V&V record review (a ``pre`` gate — the
# claim is reviewed before it's authoritative; mirrors the library +
# materials patterns).
_PUBLISH_KIND = "pre"


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


def _normalise_evidence(
    items: list[dict[str, Any]] | None,
    *,
    legacy_file_id: int | None,
    legacy_url: str | None,
) -> tuple[list[dict[str, Any]], int | None, str | None]:
    """Clean an evidence list and derive the legacy single-item mirrors.

    ``items`` is the canonical list ``[{"kind","file_version_id","url"}, ...]``
    (kinds ``library`` / ``url``). When it's ``None`` the list is synthesised
    from the legacy single scalars, so callers that still pass one file / one
    URL keep working. Returns ``(cleaned_list, first_library_file_id,
    first_url)`` — the two scalars mirror the first entry of each kind for
    older readers.
    """
    if items is None:
        items = []
        if legacy_file_id is not None:
            items.append({"kind": "library", "file_version_id": int(legacy_file_id), "url": None})
        if legacy_url:
            items.append({"kind": "url", "file_version_id": None, "url": str(legacy_url)})
    cleaned: list[dict[str, Any]] = []
    for it in items:
        if it.get("kind") == "library" and it.get("file_version_id") is not None:
            cleaned.append(
                {"kind": "library", "file_version_id": int(it["file_version_id"]), "url": None}
            )
        elif it.get("kind") == "url" and str(it.get("url") or "").strip():
            cleaned.append({"kind": "url", "file_version_id": None, "url": str(it["url"]).strip()})
    first_file = next((c["file_version_id"] for c in cleaned if c["kind"] == "library"), None)
    first_url = next((c["url"] for c in cleaned if c["kind"] == "url"), None)
    return cleaned, first_file, first_url


def _row_evidence(
    evidence_json: list[dict[str, Any]] | None,
    evidence_file_version_id: int | None,
    evidence_url: str | None,
) -> list[dict[str, Any]]:
    """The evidence list for a row — the stored list, or synthesised from scalars.

    Rows written before migration ``0087`` carry an empty ``evidence_json``
    but may have the legacy scalar columns set; we present those as a
    one-or-two-item list so every consumer sees a uniform shape.
    """
    items = list(evidence_json or [])
    if items:
        return items
    synth: list[dict[str, Any]] = []
    if evidence_file_version_id is not None:
        synth.append(
            {"kind": "library", "file_version_id": int(evidence_file_version_id), "url": None}
        )
    if evidence_url:
        synth.append({"kind": "url", "file_version_id": None, "url": str(evidence_url)})
    return synth


def _apply_evidence_update(
    row: Any,
    *,
    evidence_items: list[dict[str, Any]] | None,
    evidence_file_version_id: int | None,
    evidence_url: str | None,
) -> None:
    """Apply an evidence edit to a draft row, keeping list + scalars in sync.

    ``evidence_items`` (when given) replaces the whole list. Otherwise a
    legacy single ``evidence_*`` field sets that scalar and the list is
    rebuilt to mirror the scalars. Passing neither leaves evidence untouched
    (the ``None = no change`` convention the other update fields follow).
    """
    if evidence_items is not None:
        cleaned, first_file, first_url = _normalise_evidence(
            evidence_items, legacy_file_id=None, legacy_url=None
        )
        row.evidence_json = cleaned
        row.evidence_file_version_id = first_file
        row.evidence_url = first_url
        return
    if evidence_file_version_id is None and evidence_url is None:
        return
    if evidence_file_version_id is not None:
        row.evidence_file_version_id = evidence_file_version_id
    if evidence_url is not None:
        row.evidence_url = evidence_url
    row.evidence_json = _row_evidence(None, row.evidence_file_version_id, row.evidence_url)


VVStatusKind = Literal["verified_validated", "verified_only", "pending", "none", "retired"]
"""Closed vocabulary for :attr:`VVStatusBadge.kind` (design §5.1).

- ``verified_validated`` — the cohort carries at least one approved
  verification AND at least one approved validation.
- ``verified_only`` — verification approved, no validation.
- ``pending`` — at least one record in ``pending`` state with no
  approved records yet of any kind.
- ``none`` — no records of any state for this cohort.
- ``retired`` — records exist for this cohort but have all been retired
  (none approved or pending).
"""


@dataclass(frozen=True, slots=True)
class VVStatusBadge:
    """The display badge for one tool version's V&V state (inheritance-aware).

    Returned by :meth:`VVService.summarise_tool_status`. Every UI surface
    that wants to render a V&V indicator (submit-form header, Compare
    column header, lineage node, KPI filter, per-job report) reads
    through this single shape so the inheritance rule stays in one
    place.

    ``contributing_version_ids`` lists every tool version in the cohort
    that carries at least one approved record; the UI uses this to
    render "Inherited from v1.0.0" provenance tooltips.
    ``cohort_bundle_hash`` is the fingerprint the cohort is keyed on —
    surfaced verbatim so the per-job HTML report and Compare column
    header can show the first eight chars as a debug aid (the empty
    string when the target version's hash is the "unknown" sentinel).
    """

    tool_version_id: int
    kind: VVStatusKind
    has_approved_verification: bool
    has_approved_validation: bool
    has_pending: bool
    latest_envelope: dict[str, dict[str, float]] | None
    cohort_bundle_hash: str
    contributing_version_ids: tuple[int, ...]
    inherited: bool


@dataclass(frozen=True, slots=True)
class EnvelopeWarning:
    """A soft warning that submitted inputs fall outside an approved envelope.

    Returned by :meth:`VVService.resolve_envelope_warning`. The submit
    surface renders this as a banner; the gate never blocks on envelope
    drift (the per-project ``vv_required_for_submit`` policy is the
    blocking mechanism — design §6.4). ``out_of_range`` carries each
    factor that violated its envelope with the supplied value and the
    approved bounds, so the UI can render a precise message.
    """

    tool_version_id: int
    out_of_range: tuple[tuple[str, float, float, float], ...]
    """``(factor_name, submitted_value, envelope_min, envelope_max)`` per violation."""


@dataclass(frozen=True, slots=True)
class PendingVVRecord:
    """One entry in the curator's pending-review inbox.

    Polymorphic over tool / job records — :attr:`kind` discriminates so
    the UI knows which detail page to deep-link to. Returned by
    :meth:`VVService.list_pending_for_curator`.
    """

    record_id: int
    record_kind: Literal["tool", "job"]
    vv_kind: str
    recorded_by_user_id: int
    recorded_at: _dt.datetime
    target_id: int
    """``tool_version_id`` for tool records, ``job_id`` for job records."""


@dataclass(frozen=True, slots=True)
class VVRecordRow:
    """One row of the cross-install V&V register (design §8 — unscoped list).

    Polymorphic over both tables via :attr:`record_kind`. Carries only the
    raw columns the register needs; id → human-label resolution (tool /
    job / user / evidence names) is done by the API layer, which owns the
    joins to :class:`ToolVersion` / :class:`Job` / :class:`User` /
    :class:`LibraryFileVersion` — the V&V package deliberately does not
    import those (avoids the downstream circular-import edge).

    Returned by :meth:`VVService.list_all_records`, the backbone of the
    browsable workspace that replaced the id-lookup form.
    """

    record_id: int
    record_kind: Literal["tool", "job"]
    vv_kind: str
    state: str
    target_id: int
    """``tool_version_id`` for tool records, ``job_id`` for job records."""
    recorded_by_user_id: int
    recorded_at: _dt.datetime
    decided_by_user_id: int | None
    decided_at: _dt.datetime | None
    evidence_file_version_id: int | None
    evidence_url: str | None
    evidence_items: list[dict[str, Any]]
    """Full evidence list ``[{"kind","file_version_id","url"}, ...]`` — the
    API resolves each library reference to a name."""
    standards_cited: list[str]
    """Tool records only; always ``[]`` for job records."""
    notes: str | None


_VVRecordT = TypeVar("_VVRecordT", ToolVVRecord, JobVVRecord)


@dataclass(frozen=True)
class VVService:
    """Lifecycle + audit + badge derivation for V&V records (ADR-0011)."""

    audit: AuditWriter

    # ----- reads -------------------------------------------------------

    async def get_tool_record(self, session: AsyncSession, *, record_id: int) -> ToolVVRecord:
        """Fetch one tool V&V record or raise :class:`ToolVVRecordNotFoundError`."""
        row = await session.get(ToolVVRecord, record_id)
        if row is None:
            raise ToolVVRecordNotFoundError(record_id)
        return row

    async def get_job_record(self, session: AsyncSession, *, record_id: int) -> JobVVRecord:
        """Fetch one job V&V record or raise :class:`JobVVRecordNotFoundError`."""
        row = await session.get(JobVVRecord, record_id)
        if row is None:
            raise JobVVRecordNotFoundError(record_id)
        return row

    async def list_for_tool_version(
        self,
        session: AsyncSession,
        *,
        tool_version_id: int,
        include_retired: bool = False,
    ) -> list[ToolVVRecord]:
        """Every V&V record for one tool version (not inheritance-expanded).

        For the inheritance-expanded read (the badge), use
        :meth:`summarise_tool_status`. This method is the raw per-version
        listing — useful on the per-tool admin tab where the curator
        wants to see exactly which records live on which version.
        """
        stmt = select(ToolVVRecord).where(ToolVVRecord.tool_version_id == tool_version_id)
        if not include_retired:
            stmt = stmt.where(ToolVVRecord.state != "retired")
        stmt = stmt.order_by(ToolVVRecord.kind, ToolVVRecord.state, ToolVVRecord.recorded_at.desc())
        return list((await session.execute(stmt)).scalars())

    async def list_for_job(
        self,
        session: AsyncSession,
        *,
        job_id: int,
        include_retired: bool = False,
    ) -> list[JobVVRecord]:
        """Every V&V record for one job (no inheritance — job claims are per-run)."""
        stmt = select(JobVVRecord).where(JobVVRecord.job_id == job_id)
        if not include_retired:
            stmt = stmt.where(JobVVRecord.state != "retired")
        stmt = stmt.order_by(JobVVRecord.kind, JobVVRecord.state, JobVVRecord.recorded_at.desc())
        return list((await session.execute(stmt)).scalars())

    async def list_all_records(
        self,
        session: AsyncSession,
        *,
        record_kind: Literal["tool", "job", "all"] = "all",
        states: list[str] | None = None,
        kinds: list[str] | None = None,
        author_user_id: int | None = None,
        since: _dt.datetime | None = None,
        until: _dt.datetime | None = None,
        include_retired: bool = False,
    ) -> list[VVRecordRow]:
        """The cross-install V&V register — every record, both tables, filtered.

        Reads are open to any authenticated user (V&V records are global);
        this is the unscoped browse the id-keyed ``list_for_*`` methods
        never offered. Filters are AND-combined and applied at the DB layer
        on columns the V&V tables own (state, kind, author, recorded-at).
        Target-name search and id→label resolution happen in the API layer,
        which owns the cross-package joins.

        ``states`` wins over ``include_retired`` — pass ``states=['retired']``
        to see only retired records; leave ``states`` empty and
        ``include_retired=False`` (the default) to hide them. Sorted newest
        first, ties broken by descending id for a stable page order.
        """
        rows: list[VVRecordRow] = []

        def _filtered(model: Any) -> Any:
            """Apply the shared column filters to a per-table select."""
            stmt = select(model)
            if states:
                stmt = stmt.where(model.state.in_(states))
            elif not include_retired:
                stmt = stmt.where(model.state != "retired")
            if kinds:
                stmt = stmt.where(model.kind.in_(kinds))
            if author_user_id is not None:
                stmt = stmt.where(model.recorded_by_user_id == author_user_id)
            if since is not None:
                stmt = stmt.where(model.recorded_at >= since)
            if until is not None:
                stmt = stmt.where(model.recorded_at <= until)
            return stmt

        if record_kind in ("all", "tool"):
            for r in (await session.execute(_filtered(ToolVVRecord))).scalars():
                rows.append(
                    VVRecordRow(
                        record_id=r.id,
                        record_kind="tool",
                        vv_kind=r.kind,
                        state=r.state,
                        target_id=r.tool_version_id,
                        recorded_by_user_id=r.recorded_by_user_id,
                        recorded_at=r.recorded_at,
                        decided_by_user_id=r.decided_by_user_id,
                        decided_at=r.decided_at,
                        evidence_file_version_id=r.evidence_file_version_id,
                        evidence_url=r.evidence_url,
                        evidence_items=_row_evidence(
                            r.evidence_json, r.evidence_file_version_id, r.evidence_url
                        ),
                        standards_cited=list(r.standards_cited_json or []),
                        notes=r.notes,
                    )
                )

        if record_kind in ("all", "job"):
            for r in (await session.execute(_filtered(JobVVRecord))).scalars():
                rows.append(
                    VVRecordRow(
                        record_id=r.id,
                        record_kind="job",
                        vv_kind=r.kind,
                        state=r.state,
                        target_id=r.job_id,
                        recorded_by_user_id=r.recorded_by_user_id,
                        recorded_at=r.recorded_at,
                        decided_by_user_id=r.decided_by_user_id,
                        decided_at=r.decided_at,
                        evidence_file_version_id=r.evidence_file_version_id,
                        evidence_url=r.evidence_url,
                        evidence_items=_row_evidence(
                            r.evidence_json, r.evidence_file_version_id, r.evidence_url
                        ),
                        standards_cited=[],
                        notes=r.notes,
                    )
                )

        rows.sort(key=lambda x: (x.recorded_at, x.record_id), reverse=True)
        return rows

    # ----- the badge ---------------------------------------------------

    async def summarise_tool_status(
        self, session: AsyncSession, *, tool_version_id: int
    ) -> VVStatusBadge:
        """Inheritance-aware V&V badge for one tool version (design §5.1 / §6.5).

        Reads every record whose ``tool_version_id`` shares
        ``(alias, bundle_hash)`` with the target — the Option C cohort.
        Empty-string ``bundle_hash`` is treated as a singleton (no
        inheritance to or from); on a real install this only happens
        before the bump path has run for the first time. Job V&V records
        are NOT considered here — those are per-run, never inherited.

        Single-statement query against the composite ``(alias,
        bundle_hash)`` index on ``tool_version`` plus the
        ``(tool_version_id, state, kind)`` index on ``tool_vv_record``.
        """
        # Late import to avoid a tools→vv circular import edge.
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415

        target = await session.get(ToolVersion, tool_version_id)
        if target is None:
            return VVStatusBadge(
                tool_version_id=tool_version_id,
                kind="none",
                has_approved_verification=False,
                has_approved_validation=False,
                has_pending=False,
                latest_envelope=None,
                cohort_bundle_hash="",
                contributing_version_ids=(),
                inherited=False,
            )

        # Empty-hash sentinel: singleton cohort. Just look at this row.
        if not target.bundle_hash:
            records = await self.list_for_tool_version(
                session, tool_version_id=tool_version_id, include_retired=False
            )
            cohort_hash = ""
            cohort_ids: list[int] = [tool_version_id]
        else:
            cohort_hash = target.bundle_hash
            # Resolve cohort members (alias + bundle_hash), then load the
            # records they carry. Two statements; the second is the
            # design's "single indexed scan over the composite index".
            cohort_ids = list(
                (
                    await session.execute(
                        select(ToolVersion.id).where(
                            ToolVersion.alias == target.alias,
                            ToolVersion.bundle_hash == target.bundle_hash,
                        )
                    )
                ).scalars()
            )
            records = list(
                (
                    await session.execute(
                        select(ToolVVRecord).where(
                            ToolVVRecord.tool_version_id.in_(cohort_ids),
                            ToolVVRecord.state != "retired",
                        )
                    )
                ).scalars()
            )

        approved = [r for r in records if r.state == "approved"]
        pending = [r for r in records if r.state == "pending"]
        approved_validations = [r for r in approved if r.kind == "validation"]
        approved_verifications = [r for r in approved if r.kind == "verification"]
        any_approved = bool(approved)
        any_pending = bool(pending)
        contributing = tuple(sorted({r.tool_version_id for r in approved}))
        inherited = bool(approved) and tool_version_id not in contributing

        # Badge label per the closed vocabulary in §5.1.
        if approved_verifications and approved_validations:
            kind: VVStatusKind = "verified_validated"
        elif approved_verifications:
            kind = "verified_only"
        elif any_pending and not any_approved:
            kind = "pending"
        elif not records:
            kind = "none"
        else:
            # Records exist but none approved or pending — all retired
            # by definition (we excluded retired in the query, so this
            # branch only fires if there are non-approved, non-pending,
            # non-retired states, which the lifecycle doesn't allow). The
            # branch stays for defensive completeness; ``none`` is the
            # honest answer.
            kind = "none"

        # Union envelope across approved validations.
        envelope = _union_envelopes(approved_validations) if approved_validations else None

        return VVStatusBadge(
            tool_version_id=tool_version_id,
            kind=kind,
            has_approved_verification=bool(approved_verifications),
            has_approved_validation=bool(approved_validations),
            has_pending=any_pending,
            latest_envelope=envelope,
            cohort_bundle_hash=cohort_hash,
            contributing_version_ids=contributing,
            inherited=inherited,
        )

    async def resolve_envelope_warning(
        self,
        session: AsyncSession,
        *,
        tool_version_id: int,
        form_values: dict[str, float],
    ) -> EnvelopeWarning | None:
        """Soft warning when ``form_values`` fall outside an approved envelope.

        Returns ``None`` when no approved validation exists for the
        cohort, no envelope is declared on those validations, or every
        supplied value falls inside the union envelope. Never blocks —
        the per-project ``vv_required_for_submit`` policy (design §6.4)
        is the blocking gate.
        """
        badge = await self.summarise_tool_status(session, tool_version_id=tool_version_id)
        envelope = badge.latest_envelope
        if not envelope:
            return None
        offenders: list[tuple[str, float, float, float]] = []
        for factor, bounds in envelope.items():
            if factor not in form_values:
                continue
            value = form_values[factor]
            lo = bounds.get("min")
            hi = bounds.get("max")
            if lo is not None and value < lo:
                offenders.append((factor, value, lo, hi if hi is not None else float("inf")))
                continue
            if hi is not None and value > hi:
                offenders.append((factor, value, lo if lo is not None else float("-inf"), hi))
        if not offenders:
            return None
        await self._audit(
            session,
            actor_user_id=0,  # warn is a derivation, not a per-user act
            action="vv.envelope.warn",
            resource_type="tool_version",
            resource_id=tool_version_id,
            payload={
                "out_of_range": [
                    {"factor": f, "value": v, "envelope_min": lo, "envelope_max": hi}
                    for f, v, lo, hi in offenders
                ]
            },
        )
        return EnvelopeWarning(tool_version_id=tool_version_id, out_of_range=tuple(offenders))

    async def suggested_applicable_range(
        self, session: AsyncSession, *, tool_version_id: int
    ) -> Any:
        """Coverage's offer for this tool version's applicable range (§10).

        Only a **surrogate** tool has one: its descriptor carries
        ``surrogate_model_version_id``, and that version may carry a stored
        coverage report whose declared envelope is exactly the operating
        window the predict-time guard already enforces. Offering it here turns
        ``applicable_range_json`` from a curator's assertion into the same
        object the platform checks against, and feeds
        :meth:`resolve_envelope_warning` once the record is approved.

        An empty offer for anything else — a hand-written tool, or a surrogate
        fitted before coverage existed. Empty means *nothing to suggest*, not
        *validated everywhere*, which is why the caller pre-fills nothing
        rather than pre-filling ``{}``.
        """
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415
        from matofx.vv.coverage_range import (  # noqa: PLC0415
            SuggestedApplicableRange,
            applicable_range_from_report,
        )

        version = await session.get(ToolVersion, tool_version_id)
        snapshot = (version.descriptor_snapshot or {}) if version is not None else {}
        model_version_id = (
            snapshot.get("surrogate_model_version_id") if isinstance(snapshot, dict) else None
        )
        if not model_version_id:
            return SuggestedApplicableRange()

        from matofx.models.orm import SurrogateModelVersion  # noqa: PLC0415

        model_version = await session.get(SurrogateModelVersion, int(model_version_id))
        if model_version is None:
            return SuggestedApplicableRange()
        return applicable_range_from_report(
            model_version.coverage_report_json, model_version_id=int(model_version_id)
        )

    # ----- authoring (curator-gated; produces draft) -------------------

    async def record_tool_vv(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        tool_version_id: int,
        kind: str,
        applicable_range: dict[str, dict[str, float]] | None = None,
        confidence_band: float | None = None,
        standards_cited: list[str] | None = None,
        evidence_file_version_id: int | None = None,
        evidence_url: str | None = None,
        evidence_items: list[dict[str, Any]] | None = None,
        notes: str | None = None,
    ) -> ToolVVRecord:
        """Author a draft V&V claim about a tool version. Requires authoring rights.

        The record lands in ``state='draft'``; the author edits it freely
        via :meth:`update_tool_record` and submits for review via
        :meth:`submit_tool_record`. ``evidence_items`` is the full mixed
        list; the legacy single ``evidence_*`` params still work and are
        folded into it. The toggleable directed-approver gate is enforced at
        submit time, not here.
        """
        await self._require_author(session, actor_user_id)
        if kind not in TOOL_VV_KINDS:
            raise ValueError(
                f"{kind!r} is not a valid tool V&V kind. Use one of: {', '.join(TOOL_VV_KINDS)}."
            )
        evidence, first_file, first_url = _normalise_evidence(
            evidence_items, legacy_file_id=evidence_file_version_id, legacy_url=evidence_url
        )
        row = ToolVVRecord(
            tool_version_id=tool_version_id,
            kind=kind,
            state="draft",
            evidence_file_version_id=first_file,
            evidence_url=first_url,
            evidence_json=evidence,
            applicable_range_json=dict(applicable_range or {}),
            confidence_band=confidence_band,
            standards_cited_json=list(standards_cited or []),
            notes=notes,
            recorded_by_user_id=actor_user_id,
            recorded_at=_utcnow(),
        )
        session.add(row)
        await session.flush()
        await self._audit(
            session,
            actor_user_id,
            "vv.tool_record.create",
            "tool_vv_record",
            row.id,
            payload={
                "tool_version_id": tool_version_id,
                "kind": kind,
                "evidence_count": len(evidence),
            },
        )
        return row

    async def record_job_vv(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        job_id: int,
        kind: str,
        evidence_file_version_id: int | None = None,
        evidence_url: str | None = None,
        evidence_items: list[dict[str, Any]] | None = None,
        notes: str | None = None,
    ) -> JobVVRecord:
        """Author a draft V&V claim about a specific job run. Requires authoring rights."""
        await self._require_author(session, actor_user_id)
        if kind not in JOB_VV_KINDS:
            raise ValueError(
                f"{kind!r} is not a valid job V&V kind. Use one of: {', '.join(JOB_VV_KINDS)}."
            )
        evidence, first_file, first_url = _normalise_evidence(
            evidence_items, legacy_file_id=evidence_file_version_id, legacy_url=evidence_url
        )
        row = JobVVRecord(
            job_id=job_id,
            kind=kind,
            state="draft",
            evidence_file_version_id=first_file,
            evidence_url=first_url,
            evidence_json=evidence,
            notes=notes,
            recorded_by_user_id=actor_user_id,
            recorded_at=_utcnow(),
        )
        session.add(row)
        await session.flush()
        await self._audit(
            session,
            actor_user_id,
            "vv.job_record.create",
            "job_vv_record",
            row.id,
            payload={"job_id": job_id, "kind": kind, "evidence_count": len(evidence)},
        )
        return row

    # ----- edit draft (author only) ------------------------------------

    async def update_tool_record(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        record_id: int,
        notes: str | None = None,
        applicable_range: dict[str, dict[str, float]] | None = None,
        confidence_band: float | None = None,
        standards_cited: list[str] | None = None,
        evidence_file_version_id: int | None = None,
        evidence_url: str | None = None,
        evidence_items: list[dict[str, Any]] | None = None,
    ) -> ToolVVRecord:
        """Edit a tool V&V record's metadata. Author + curate + draft-only.

        Only fields explicitly supplied are touched (``None`` means
        "don't change" for the optional fields, which matches the
        materials pattern). To clear a field, pass an empty value of
        the right type (e.g. ``notes=""``, ``applicable_range={}``).
        """
        await self._require_author(session, actor_user_id)
        row = await self.get_tool_record(session, record_id=record_id)
        if row.recorded_by_user_id != actor_user_id:
            raise PermissionDeniedError(
                f"Only the V&V record's author (user {row.recorded_by_user_id}) can edit it."
            )
        if row.state != "draft":
            raise VVStateTransitionError(row.state, row.state)  # "no edits in non-draft state"
        if notes is not None:
            row.notes = notes
        if applicable_range is not None:
            row.applicable_range_json = dict(applicable_range)
        if confidence_band is not None:
            row.confidence_band = confidence_band
        if standards_cited is not None:
            row.standards_cited_json = list(standards_cited)
        _apply_evidence_update(
            row,
            evidence_items=evidence_items,
            evidence_file_version_id=evidence_file_version_id,
            evidence_url=evidence_url,
        )
        await session.flush()
        await self._audit(
            session,
            actor_user_id,
            "vv.tool_record.update",
            "tool_vv_record",
            row.id,
            payload={"tool_version_id": row.tool_version_id},
        )
        return row

    async def update_job_record(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        record_id: int,
        notes: str | None = None,
        evidence_file_version_id: int | None = None,
        evidence_url: str | None = None,
        evidence_items: list[dict[str, Any]] | None = None,
    ) -> JobVVRecord:
        """Edit a job V&V record's metadata. Author + draft-only."""
        await self._require_author(session, actor_user_id)
        row = await self.get_job_record(session, record_id=record_id)
        if row.recorded_by_user_id != actor_user_id:
            raise PermissionDeniedError(
                f"Only the V&V record's author (user {row.recorded_by_user_id}) can edit it."
            )
        if row.state != "draft":
            raise VVStateTransitionError(row.state, row.state)
        if notes is not None:
            row.notes = notes
        _apply_evidence_update(
            row,
            evidence_items=evidence_items,
            evidence_file_version_id=evidence_file_version_id,
            evidence_url=evidence_url,
        )
        await session.flush()
        await self._audit(
            session,
            actor_user_id,
            "vv.job_record.update",
            "job_vv_record",
            row.id,
            payload={"job_id": row.job_id},
        )
        return row

    # ----- submit for review (draft → pending) -------------------------

    async def submit_tool_record(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        record_id: int,
        approval_required: bool = True,
        designated_approver_user_id: int | None = None,
        designated_approver_username: str | None = None,
    ) -> ToolVVRecord:
        """Flip a tool V&V record from ``draft`` to ``pending``; create the gate.

        With ``approval_required=True`` (the
        ``vv_record_approval`` General Setting ON path, design §6.1) the
        author MUST name one other ``vv_curator``; the method opens a
        directed :class:`ApprovalRequest` via :class:`ApprovalService`,
        which itself rejects self-approval. The
        :class:`VVSoleCuratorError` fires when the install carries fewer
        than two curators (no eligible "other"; design §6.3). With it
        off, the record skips ``pending`` and lands in ``approved``
        directly — see :meth:`approve_tool_record`'s direct path.
        """
        await self._require_author(session, actor_user_id)
        row = await self.get_tool_record(session, record_id=record_id)
        if row.recorded_by_user_id != actor_user_id:
            raise PermissionDeniedError(
                f"Only the V&V record's author (user {row.recorded_by_user_id}) can submit it."
            )
        if not can_transition(row.state, "pending"):
            raise VVStateTransitionError(row.state, "pending")
        if approval_required:
            await self._open_directed_approval(
                session,
                actor_user_id=actor_user_id,
                tool_vv_record_id=row.id,
                designated_approver_user_id=designated_approver_user_id,
                designated_approver_username=designated_approver_username,
            )
        row.state = "pending"
        await session.flush()
        await self._audit(
            session,
            actor_user_id,
            "vv.tool_record.submit",
            "tool_vv_record",
            row.id,
            payload={
                "tool_version_id": row.tool_version_id,
                "designated_approver_user_id": designated_approver_user_id,
            },
        )
        return row

    async def submit_job_record(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        record_id: int,
        approval_required: bool = True,
        designated_approver_user_id: int | None = None,
        designated_approver_username: str | None = None,
    ) -> JobVVRecord:
        """Flip a job V&V record from ``draft`` to ``pending``; create the gate."""
        await self._require_author(session, actor_user_id)
        row = await self.get_job_record(session, record_id=record_id)
        if row.recorded_by_user_id != actor_user_id:
            raise PermissionDeniedError(
                f"Only the V&V record's author (user {row.recorded_by_user_id}) can submit it."
            )
        if not can_transition(row.state, "pending"):
            raise VVStateTransitionError(row.state, "pending")
        if approval_required:
            await self._open_directed_approval(
                session,
                actor_user_id=actor_user_id,
                job_vv_record_id=row.id,
                designated_approver_user_id=designated_approver_user_id,
                designated_approver_username=designated_approver_username,
            )
        row.state = "pending"
        await session.flush()
        await self._audit(
            session,
            actor_user_id,
            "vv.job_record.submit",
            "job_vv_record",
            row.id,
            payload={
                "job_id": row.job_id,
                "designated_approver_user_id": designated_approver_user_id,
            },
        )
        return row

    # ----- decide (forwards through ApprovalService) -------------------

    async def approve_tool_record(
        self,
        session: AsyncSession,
        *,
        decider_user_id: int,
        decider_username: str,
        record_id: int,
        reason: str | None = None,
        approval_required: bool = True,
        decider_is_admin: bool = False,
    ) -> ToolVVRecord:
        """Approve a pending tool V&V record. Requires curate.

        Two paths depending on ``approval_required``:

        - **True (gated):** find the open ``approval_request`` for this
          record, forward to :meth:`ApprovalService.decide` (which
          rejects self-approval + non-eligible deciders), then flip the
          record's ``state='approved'`` and stamp ``decided_*``.
        - **False (direct):** the curator approves without an open
          gate — the record was created and submitted without ever
          opening an ``approval_request``. State flip + stamp only.
        """
        await self._require_curate(session, decider_user_id)
        row = await self.get_tool_record(session, record_id=record_id)
        if approval_required:
            await self._decide_via_approval_service(
                session,
                tool_vv_record_id=record_id,
                decider_user_id=decider_user_id,
                decider_username=decider_username,
                decision="approve",
                reason=reason,
                decider_is_admin=decider_is_admin,
            )
        return await self._finalise_decision(
            session,
            row=row,
            decider_user_id=decider_user_id,
            decision="approved",
            reason=reason,
            audit_action="vv.tool_record.approve",
            resource_type="tool_vv_record",
            payload={"tool_version_id": row.tool_version_id},
        )

    async def reject_tool_record(
        self,
        session: AsyncSession,
        *,
        decider_user_id: int,
        decider_username: str,
        record_id: int,
        reason: str,
        approval_required: bool = True,
        decider_is_admin: bool = False,
    ) -> ToolVVRecord:
        """Reject a pending tool V&V record. Requires curate + a reason."""
        if not reason or not reason.strip():
            raise VVDecisionReasonRequiredError
        await self._require_curate(session, decider_user_id)
        row = await self.get_tool_record(session, record_id=record_id)
        if approval_required:
            await self._decide_via_approval_service(
                session,
                tool_vv_record_id=record_id,
                decider_user_id=decider_user_id,
                decider_username=decider_username,
                decision="deny",
                reason=reason,
                decider_is_admin=decider_is_admin,
            )
        return await self._finalise_decision(
            session,
            row=row,
            decider_user_id=decider_user_id,
            decision="rejected",
            reason=reason,
            audit_action="vv.tool_record.reject",
            resource_type="tool_vv_record",
            payload={"tool_version_id": row.tool_version_id},
        )

    async def approve_job_record(
        self,
        session: AsyncSession,
        *,
        decider_user_id: int,
        decider_username: str,
        record_id: int,
        reason: str | None = None,
        approval_required: bool = True,
        decider_is_admin: bool = False,
    ) -> JobVVRecord:
        """Approve a pending job V&V record. Requires curate."""
        await self._require_curate(session, decider_user_id)
        row = await self.get_job_record(session, record_id=record_id)
        if approval_required:
            await self._decide_via_approval_service(
                session,
                job_vv_record_id=record_id,
                decider_user_id=decider_user_id,
                decider_username=decider_username,
                decision="approve",
                reason=reason,
                decider_is_admin=decider_is_admin,
            )
        return await self._finalise_decision(
            session,
            row=row,
            decider_user_id=decider_user_id,
            decision="approved",
            reason=reason,
            audit_action="vv.job_record.approve",
            resource_type="job_vv_record",
            payload={"job_id": row.job_id},
        )

    async def reject_job_record(
        self,
        session: AsyncSession,
        *,
        decider_user_id: int,
        decider_username: str,
        record_id: int,
        reason: str,
        approval_required: bool = True,
        decider_is_admin: bool = False,
    ) -> JobVVRecord:
        """Reject a pending job V&V record. Requires curate + a reason."""
        if not reason or not reason.strip():
            raise VVDecisionReasonRequiredError
        await self._require_curate(session, decider_user_id)
        row = await self.get_job_record(session, record_id=record_id)
        if approval_required:
            await self._decide_via_approval_service(
                session,
                job_vv_record_id=record_id,
                decider_user_id=decider_user_id,
                decider_username=decider_username,
                decision="deny",
                reason=reason,
                decider_is_admin=decider_is_admin,
            )
        return await self._finalise_decision(
            session,
            row=row,
            decider_user_id=decider_user_id,
            decision="rejected",
            reason=reason,
            audit_action="vv.job_record.reject",
            resource_type="job_vv_record",
            payload={"job_id": row.job_id},
        )

    # ----- decide via the unified approvals inbox ---------------------
    #
    # These two methods are the entry point the cross-domain approvals
    # router (``/api/approvals/decisions``) calls when a V&V request is
    # decided through the unified curator inbox. They mirror the
    # ``MaterialsService.decide_approval`` shape: take the
    # ``approval_request`` id (not the V&V record id — the router has
    # the request id from the inbox listing), forward the decision to
    # :class:`ApprovalService`, then apply the V&V-side state flip.

    async def decide_tool_approval(
        self,
        session: AsyncSession,
        *,
        request_id: int,
        decider_user_id: int,
        decider_username: str,
        decision: Literal["approve", "deny"],
        reason: str | None = None,
        decider_is_approver: bool,
        decider_is_admin: bool,
    ) -> tuple[Any, ToolVVRecord]:
        """Decide a pending tool-V&V approval gate via the unified inbox.

        Used by the ``/api/approvals/decisions`` cross-domain router.
        Delegates the decision + self-approval / eligibility defence to
        :class:`ApprovalService`, then flips the
        :class:`ToolVVRecord`'s ``state`` to ``approved``/``rejected``
        and stamps ``decided_*`` — the same effect
        :meth:`approve_tool_record` / :meth:`reject_tool_record`
        produce on the V&V router path. Returns the decided
        ``(request, record)`` pair the router uses to populate its
        per-decision response.
        """
        from matofx.approvals import make_approval_service  # noqa: PLC0415
        from matofx.approvals.errors import ApprovalRequestNotFoundError  # noqa: PLC0415
        from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415

        if decision == "deny" and (not reason or not reason.strip()):
            raise VVDecisionReasonRequiredError

        request = await session.get(ApprovalRequest, request_id)
        if request is None or request.tool_vv_record_id is None:
            raise ApprovalRequestNotFoundError(request_id)
        record_id = request.tool_vv_record_id
        row = await self.get_tool_record(session, record_id=record_id)

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

        terminal: Literal["approved", "rejected"] = (
            "approved" if decision == "approve" else "rejected"
        )
        verb = "approve" if terminal == "approved" else "reject"
        finalised = await self._finalise_decision(
            session,
            row=row,
            decider_user_id=decider_user_id,
            decision=terminal,
            reason=reason,
            audit_action=f"vv.tool_record.{verb}",
            resource_type="tool_vv_record",
            payload={"tool_version_id": row.tool_version_id, "via": "approvals_inbox"},
        )
        return request, finalised

    async def decide_job_approval(
        self,
        session: AsyncSession,
        *,
        request_id: int,
        decider_user_id: int,
        decider_username: str,
        decision: Literal["approve", "deny"],
        reason: str | None = None,
        decider_is_approver: bool,
        decider_is_admin: bool,
    ) -> tuple[Any, JobVVRecord]:
        """Decide a pending job-V&V approval gate via the unified inbox.

        Job-record sibling of :meth:`decide_tool_approval`.
        """
        from matofx.approvals import make_approval_service  # noqa: PLC0415
        from matofx.approvals.errors import ApprovalRequestNotFoundError  # noqa: PLC0415
        from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415

        if decision == "deny" and (not reason or not reason.strip()):
            raise VVDecisionReasonRequiredError

        request = await session.get(ApprovalRequest, request_id)
        if request is None or request.job_vv_record_id is None:
            raise ApprovalRequestNotFoundError(request_id)
        record_id = request.job_vv_record_id
        row = await self.get_job_record(session, record_id=record_id)

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

        terminal: Literal["approved", "rejected"] = (
            "approved" if decision == "approve" else "rejected"
        )
        verb = "approve" if terminal == "approved" else "reject"
        finalised = await self._finalise_decision(
            session,
            row=row,
            decider_user_id=decider_user_id,
            decision=terminal,
            reason=reason,
            audit_action=f"vv.job_record.{verb}",
            resource_type="job_vv_record",
            payload={"job_id": row.job_id, "via": "approvals_inbox"},
        )
        return request, finalised

    # ----- retire (soft archive; curate-gated) -------------------------

    async def retire_tool_record(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        record_id: int,
    ) -> ToolVVRecord:
        """Retire a tool V&V record from any live state. Requires curate.

        Retirable from ``draft``, ``pending``, ``approved`` or
        ``rejected`` — a curator can withdraw a record wherever it sits in
        the lifecycle. When the record is still ``pending``, the open
        approval gate is *withdrawn* as part of the retire so it doesn't
        linger in an approver's inbox.

        Once retired the record no longer contributes to any version's
        badge in :meth:`summarise_tool_status`. Mirrors the design's
        symmetric inherit-cohort-wide / retire-cohort-wide semantics
        (per-version retire is v1.x — see design §13).
        """
        await self._require_curate(session, actor_user_id)
        row = await self.get_tool_record(session, record_id=record_id)
        if not can_transition(row.state, "retired"):
            raise VVStateTransitionError(row.state, "retired")
        if row.state == "pending":
            await self._withdraw_open_gate(
                session, actor_user_id=actor_user_id, tool_vv_record_id=row.id
            )
        row.state = "retired"
        await session.flush()
        await self._audit(
            session,
            actor_user_id,
            "vv.tool_record.retire",
            "tool_vv_record",
            row.id,
            payload={"tool_version_id": row.tool_version_id},
        )
        return row

    async def retire_job_record(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        record_id: int,
    ) -> JobVVRecord:
        """Retire a job V&V record from any live state. Requires curate.

        As with :meth:`retire_tool_record`, a ``pending`` record's open
        approval gate is withdrawn as part of the retire.
        """
        await self._require_curate(session, actor_user_id)
        row = await self.get_job_record(session, record_id=record_id)
        if not can_transition(row.state, "retired"):
            raise VVStateTransitionError(row.state, "retired")
        if row.state == "pending":
            await self._withdraw_open_gate(
                session, actor_user_id=actor_user_id, job_vv_record_id=row.id
            )
        row.state = "retired"
        await session.flush()
        await self._audit(
            session,
            actor_user_id,
            "vv.job_record.retire",
            "job_vv_record",
            row.id,
            payload={"job_id": row.job_id},
        )
        return row

    async def _withdraw_open_gate(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        tool_vv_record_id: int | None = None,
        job_vv_record_id: int | None = None,
    ) -> None:
        """Withdraw the open publish gate for a pending V&V record, if any.

        Closes the ``approval_request`` created at submit time so retiring
        a pending record doesn't strand it in an approver's inbox. A
        no-op when no open gate exists (a record submitted while the
        approval toggle was off never opened one).
        """
        from matofx.approvals import make_approval_service  # noqa: PLC0415
        from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415

        target_col = (
            ApprovalRequest.tool_vv_record_id
            if tool_vv_record_id is not None
            else ApprovalRequest.job_vv_record_id
        )
        target_id = tool_vv_record_id if tool_vv_record_id is not None else job_vv_record_id
        request = (
            await session.execute(
                select(ApprovalRequest)
                .where(target_col == target_id)
                .where(ApprovalRequest.kind == _PUBLISH_KIND)
                .where(ApprovalRequest.status == "pending")
            )
        ).scalar_one_or_none()
        if request is None:
            return
        await make_approval_service(self.audit).withdraw(
            session,
            request_id=request.id,
            actor_user_id=actor_user_id,
            actor_is_requestor=request.requested_by_user_id == actor_user_id,
            reason="The V&V record was retired by a curator.",
        )

    # ----- migrate to a new version (curate-gated) ---------------------

    async def migrate_tool_records(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        source_tool_version_id: int,
        target_tool_version_id: int,
        record_ids: list[int],
    ) -> tuple[list[ToolVVRecord], list[tuple[int, str]]]:
        """Carry approved tool V&V records forward to a newer version (design §13).

        Copies each chosen *approved* source record onto
        ``target_tool_version_id`` as a fresh ``draft`` owned by the actor,
        preserving the claim (kind, applicable range, confidence,
        standards, notes) and its evidence references. The copies must be
        re-submitted and re-approved — the tool bundle changed, so the old
        approval doesn't transfer (mirrors the "imports reset to new"
        rule). Requires ``vv.curate``.

        Whole-request problems raise :class:`VVMigrationError` (different
        tools, same version, missing version). Per-record problems (not
        approved, wrong source, an identical draft already on the target)
        are returned as ``(source_record_id, reason)`` *skips* so one bad
        pick doesn't sink the batch. Re-running is safe: an exact-content
        duplicate already present on the target is skipped, not re-copied.

        Returns ``(migrated_rows, skips)``.
        """
        from matofx.tools.orm import ToolVersion  # noqa: PLC0415
        from matofx.vv.errors import VVMigrationError  # noqa: PLC0415

        await self._require_curate(session, actor_user_id)
        if source_tool_version_id == target_tool_version_id:
            raise VVMigrationError("Choose a different target version to migrate to.")
        source = await session.get(ToolVersion, source_tool_version_id)
        target = await session.get(ToolVersion, target_tool_version_id)
        if source is None or target is None:
            raise VVMigrationError("The source or target tool version doesn't exist.")
        if source.alias != target.alias:
            raise VVMigrationError(
                "V&V records can only be migrated between versions of the same tool."
            )

        # Content keys of the target's live records — used to skip an
        # exact-duplicate copy so a repeated migration is idempotent.
        existing_keys = {
            _tool_record_content_key(r)
            for r in (
                await session.execute(
                    select(ToolVVRecord).where(
                        ToolVVRecord.tool_version_id == target_tool_version_id,
                        ToolVVRecord.state.in_(("draft", "pending", "approved")),
                    )
                )
            ).scalars()
        }

        migrated: list[ToolVVRecord] = []
        skipped: list[tuple[int, str]] = []
        for rid in record_ids:
            row = await session.get(ToolVVRecord, rid)
            if row is None:
                skipped.append((rid, "That record no longer exists."))
                continue
            if row.tool_version_id != source_tool_version_id:
                skipped.append((rid, "That record isn't on the source version."))
                continue
            if row.state != "approved":
                skipped.append(
                    (rid, f"Only approved records can be migrated (this one is {row.state}).")
                )
                continue
            key = _tool_record_content_key(row)
            if key in existing_keys:
                skipped.append((rid, "An identical record already exists on the target version."))
                continue
            new_row = ToolVVRecord(
                tool_version_id=target_tool_version_id,
                kind=row.kind,
                state="draft",
                evidence_file_version_id=row.evidence_file_version_id,
                evidence_url=row.evidence_url,
                evidence_json=list(row.evidence_json or []),
                applicable_range_json=dict(row.applicable_range_json or {}),
                confidence_band=row.confidence_band,
                standards_cited_json=list(row.standards_cited_json or []),
                notes=row.notes,
                recorded_by_user_id=actor_user_id,
                recorded_at=_utcnow(),
            )
            session.add(new_row)
            await session.flush()
            existing_keys.add(key)  # guard against dup picks within one call
            await self._audit(
                session,
                actor_user_id,
                "vv.tool_record.migrate",
                "tool_vv_record",
                new_row.id,
                payload={
                    "source_record_id": rid,
                    "source_tool_version_id": source_tool_version_id,
                    "target_tool_version_id": target_tool_version_id,
                    "kind": row.kind,
                },
            )
            migrated.append(new_row)
        return migrated, skipped

    # ----- curator inbox -----------------------------------------------

    async def list_pending_for_curator(
        self, session: AsyncSession, *, actor_user_id: int
    ) -> list[PendingVVRecord]:
        """Every pending V&V record across both tables, oldest first.

        Curator-only (the unified inbox is a privileged view). The shape
        returned is polymorphic over tool / job records via the
        :attr:`PendingVVRecord.record_kind` discriminator. The caller
        decides what to do with it (typically render the unified curator
        inbox alongside library / material / job / study pending rows).
        """
        await self._require_curate(session, actor_user_id)
        # Read both tables in one execution apiece — the union sort
        # happens in Python because the two tables have different shape.
        tool_rows = list(
            (
                await session.execute(
                    select(ToolVVRecord)
                    .where(ToolVVRecord.state == "pending")
                    .order_by(ToolVVRecord.recorded_at)
                )
            ).scalars()
        )
        job_rows = list(
            (
                await session.execute(
                    select(JobVVRecord)
                    .where(JobVVRecord.state == "pending")
                    .order_by(JobVVRecord.recorded_at)
                )
            ).scalars()
        )
        out: list[PendingVVRecord] = []
        for tool_row in tool_rows:
            out.append(
                PendingVVRecord(
                    record_id=tool_row.id,
                    record_kind="tool",
                    vv_kind=tool_row.kind,
                    recorded_by_user_id=tool_row.recorded_by_user_id,
                    recorded_at=tool_row.recorded_at,
                    target_id=tool_row.tool_version_id,
                )
            )
        for job_row in job_rows:
            out.append(
                PendingVVRecord(
                    record_id=job_row.id,
                    record_kind="job",
                    vv_kind=job_row.kind,
                    recorded_by_user_id=job_row.recorded_by_user_id,
                    recorded_at=job_row.recorded_at,
                    target_id=job_row.job_id,
                )
            )
        out.sort(key=lambda p: (p.recorded_at, p.record_id))
        return out

    async def list_eligible_curators(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
    ) -> list[tuple[int, str]]:
        """``(user_id, username)`` pairs of every other curator the author can pick.

        Used by the submit-form approver picker. Excludes the actor
        themselves (self-approval is forbidden). Empty list when the
        install has only one curator — :meth:`submit_tool_record` /
        :meth:`submit_job_record` raise :class:`VVSoleCuratorError` in
        that case.
        """
        # Late import to keep the vv → users edge one-way.
        from matofx.users.orm import User  # noqa: PLC0415

        all_users = list((await session.execute(select(User.id, User.username))).all())
        out: list[tuple[int, str]] = []
        for uid, uname in all_users:
            if uid == actor_user_id:
                continue
            if await user_has_permission(session, uid, Permission.VV_CURATE):
                out.append((uid, uname))
        return out

    # ----- internals ---------------------------------------------------

    async def _require_author(self, session: AsyncSession, user_id: int) -> None:
        """Gate draft authoring/editing/submitting on ``vv.create`` (or curate).

        Authoring a draft, editing it, and submitting it for review are
        base-tier actions: a ``vv.create`` holder may do them, as may a
        ``vv.curate`` curator (who can always author). Approving, promoting,
        and retiring stay curate-only via :meth:`_require_curate`.
        """
        if not await user_has_any_permission(
            session, user_id, Permission.VV_CREATE, Permission.VV_CURATE
        ):
            raise PermissionDeniedError(
                "Authoring V&V records requires V&V authoring rights "
                "(the V&V create permission or the V&V curator capability)."
            )

    async def _require_curate(self, session: AsyncSession, user_id: int) -> None:
        """Raise :class:`PermissionDeniedError` unless the user holds VV_CURATE."""
        if not await user_has_permission(session, user_id, Permission.VV_CURATE):
            raise PermissionDeniedError(
                "Curating V&V records requires the V&V curator capability role (or super-user)."
            )

    async def _open_directed_approval(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        tool_vv_record_id: int | None = None,
        job_vv_record_id: int | None = None,
        designated_approver_user_id: int | None,
        designated_approver_username: str | None,
    ) -> None:
        """Create the ``ApprovalRequest`` row for a V&V submit.

        Caller passes EXACTLY one of ``tool_vv_record_id`` /
        ``job_vv_record_id``. Validates the approver pick (a chosen
        approver MUST be provided AND must be a curator other than the
        actor); enforces the sole-curator guard before creating the
        request. Self-approval is rejected inside
        :class:`ApprovalService.create_request` so we don't pre-check it
        here — keeps the gate canonical.
        """
        from matofx.approvals import make_approval_service  # noqa: PLC0415

        # Sole-curator guard — must have at least one other curator.
        eligible = await self.list_eligible_curators(session, actor_user_id=actor_user_id)
        if not eligible:
            raise VVSoleCuratorError
        if designated_approver_user_id is None or not designated_approver_username:
            raise VVApproverRequiredError

        await make_approval_service(self.audit).create_request(
            session,
            tool_vv_record_id=tool_vv_record_id,
            job_vv_record_id=job_vv_record_id,
            kind=_PUBLISH_KIND,
            requested_by_user_id=actor_user_id,
            eligible_approvers=[designated_approver_username],
            designated_approver_user_id=designated_approver_user_id,
        )

    async def _decide_via_approval_service(
        self,
        session: AsyncSession,
        *,
        tool_vv_record_id: int | None = None,
        job_vv_record_id: int | None = None,
        decider_user_id: int,
        decider_username: str,
        decision: Literal["approve", "deny"],
        reason: str | None,
        decider_is_admin: bool,
    ) -> None:
        """Forward to :class:`ApprovalService.decide` for the V&V record's gate.

        Looks up the open ``approval_request`` for this V&V record + the
        ``pre`` kind, then forwards. ApprovalService handles the
        self-approval + eligibility checks + decide-twice guard.
        """
        from matofx.approvals import make_approval_service  # noqa: PLC0415
        from matofx.approvals.errors import ApprovalRequestNotFoundError  # noqa: PLC0415
        from matofx.approvals.orm import ApprovalRequest  # noqa: PLC0415

        target_col = (
            ApprovalRequest.tool_vv_record_id
            if tool_vv_record_id is not None
            else ApprovalRequest.job_vv_record_id
        )
        target_id = tool_vv_record_id if tool_vv_record_id is not None else job_vv_record_id
        request = (
            await session.execute(
                select(ApprovalRequest)
                .where(target_col == target_id)
                .where(ApprovalRequest.kind == _PUBLISH_KIND)
                .where(ApprovalRequest.status == "pending")
            )
        ).scalar_one_or_none()
        if request is None:
            raise ApprovalRequestNotFoundError(0)

        await make_approval_service(self.audit).decide(
            session,
            request_id=request.id,
            decider_user_id=decider_user_id,
            decider_username=decider_username,
            decision=decision,
            reason=reason,
            decider_is_approver=True,
            decider_is_admin=decider_is_admin,
        )

    async def _finalise_decision(
        self,
        session: AsyncSession,
        *,
        row: _VVRecordT,
        decider_user_id: int,
        decision: Literal["approved", "rejected"],
        reason: str | None,
        audit_action: str,
        resource_type: str,
        payload: dict[str, Any],
    ) -> _VVRecordT:
        """Stamp the V&V record's terminal state + audit the decision.

        Shared by the four decide methods. Generic over the record kind
        so callers' return types stay precise (the same row goes in and
        out). Verifies the transition is allowed (covers e.g. an
        attempted second decision on an already-terminal row).
        """
        if not can_transition(row.state, decision):
            raise VVStateTransitionError(row.state, decision)
        row.state = decision
        row.decided_by_user_id = decider_user_id
        row.decided_at = _utcnow()
        if reason is not None:
            row.decision_reason = reason
        await session.flush()
        await self._audit(
            session,
            decider_user_id,
            audit_action,
            resource_type,
            row.id,
            payload=payload,
            reason=reason,
        )
        return row

    async def _audit(
        self,
        session: AsyncSession,
        actor_user_id: int,
        action: str,
        resource_type: str,
        resource_id: int,
        *,
        payload: dict[str, Any],
        reason: str | None = None,
    ) -> None:
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}" if actor_user_id else "system",
                actor_type="user" if actor_user_id else "system",
                action=action,
                resource_type=resource_type,
                resource_id=str(resource_id),
                outcome="success",
                payload=payload,
                reason=reason,
            ),
        )


def _tool_record_content_key(row: ToolVVRecord) -> str:
    """A deterministic fingerprint of a tool record's *claim* content.

    Covers the fields a migration copies (kind, notes, confidence,
    standards, applicable range, evidence) but not identity/lifecycle
    (id, state, author, timestamps). Used to make :meth:`migrate_tool_records`
    idempotent — an exact-content copy already on the target is skipped.
    """
    import json  # noqa: PLC0415

    payload = {
        "kind": row.kind,
        "notes": (row.notes or "").strip(),
        "confidence_band": row.confidence_band,
        "standards": sorted(row.standards_cited_json or []),
        "applicable_range": row.applicable_range_json or {},
        "evidence": row.evidence_json or [],
    }
    return json.dumps(payload, sort_keys=True, default=str)


def _union_envelopes(
    records: list[ToolVVRecord],
) -> dict[str, dict[str, float]] | None:
    """Union the ``applicable_range_json`` blocks across approved validations.

    Per-factor min = min of all mins; max = max of all maxes. A factor
    missing from one record's envelope is treated as unbounded on that
    side, so the union widens conservatively (the soft-warn only fires
    when *every* envelope agrees a value is out-of-range).
    """
    out: dict[str, dict[str, float]] = {}
    for r in records:
        env = r.applicable_range_json or {}
        for factor, bounds in env.items():
            lo = bounds.get("min")
            hi = bounds.get("max")
            cur = out.setdefault(factor, {})
            if lo is not None:
                cur["min"] = min(lo, cur["min"]) if "min" in cur else lo
            if hi is not None:
                cur["max"] = max(hi, cur["max"]) if "max" in cur else hi
    return out or None


def make_vv_service(audit: AuditWriter) -> VVService:
    """Wire up a :class:`VVService` from the ambient audit writer."""
    return VVService(audit=audit)
