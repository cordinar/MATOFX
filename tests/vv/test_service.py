"""End-to-end tests for :class:`VVService` (slice 4).

Covers the lifecycle (author → submit → decide → retire), the
state-machine guards, the bundle-hash inheritance query (the ADR-0011
Option C invariant), the sole-curator guard, the envelope soft-warn,
audit events, and approval-request creation.

The bundle-hash inheritance tests are the most important — they
exercise the actual ADR-0011 cohort-key rule that everything else hangs
on. Two helpers (``_grant_vv_curator`` + ``_seed_extra_tool_version``)
let the tests build the right shapes without having to repeat the
multi-table setup in every case.
"""

from __future__ import annotations

import datetime as _dt

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter
from matofx.db import make_session_factory, session_scope
from matofx.users.errors import PermissionDeniedError
from matofx.vv import (
    VVApproverRequiredError,
    VVDecisionReasonRequiredError,
    VVMigrationError,
    VVSoleCuratorError,
    VVStateTransitionError,
    make_vv_service,
)

from .conftest import Basics


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


def _make_service() -> object:
    """Build a VVService wired to a no-signing-key audit writer."""
    return make_vv_service(AuditWriter(signing_key=None, key_id=None))


async def _grant_vv_curator(session: sa.ext.asyncio.AsyncSession, user_id: int) -> None:
    """Attach the seeded ``vv_curator`` capability role to ``user_id``."""
    role_id = (
        await session.execute(sa.text("SELECT id FROM role WHERE name = 'vv_curator'"))
    ).scalar_one()
    await session.execute(
        sa.text(
            "INSERT INTO user_role (user_id, role_id, granted_at) VALUES (:uid, :rid, :now)"
        ).bindparams(uid=user_id, rid=role_id, now=_utcnow())
    )


async def _seed_extra_tool_version(
    session: sa.ext.asyncio.AsyncSession,
    *,
    alias: str,
    version: str,
    bundle_hash: str,
    created_by_user_id: int,
) -> int:
    """Insert an extra ``tool_version`` row (for inheritance-cohort tests)."""
    from matofx.tools.orm import ToolVersion  # noqa: PLC0415

    tv = ToolVersion(
        alias=alias,
        version=version,
        descriptor_snapshot={"alias": alias},
        bundle_path=f"bundle/{alias}/{version}",
        bundle_hash=bundle_hash,
        created_at=_utcnow(),
        created_by_user_id=created_by_user_id,
        change_summary="seeded by test",
        changes={},
    )
    session.add(tv)
    await session.flush()
    return tv.id


# ---- Authoring + draft edit -----------------------------------------


@pytest.mark.asyncio
async def test_record_tool_vv_creates_draft(vv_engine: AsyncEngine, basics: Basics) -> None:
    """``record_tool_vv`` lands in ``state='draft'`` with the supplied envelope."""
    svc = _make_service()
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await _grant_vv_curator(session, basics.user_id)
        row = await svc.record_tool_vv(
            session,
            actor_user_id=basics.user_id,
            tool_version_id=basics.tool_version_id_a,
            kind="validation",
            applicable_range={"temperature": {"min": 300.0, "max": 600.0}},
            confidence_band=0.02,
            standards_cited=["ASME V&V 10"],
            notes="rig validation",
        )
    assert row.state == "draft"
    assert row.kind == "validation"
    assert row.applicable_range_json == {"temperature": {"min": 300.0, "max": 600.0}}
    assert row.confidence_band == 0.02
    assert row.standards_cited_json == ["ASME V&V 10"]
    assert row.recorded_by_user_id == basics.user_id


@pytest.mark.asyncio
async def test_record_tool_vv_rejects_non_author(vv_engine: AsyncEngine, basics: Basics) -> None:
    """A user with neither ``vv.create`` nor ``vv.curate`` cannot author a draft.

    Authoring was relaxed to base-tier ``vv.create`` (migration 0083, which
    seeds it onto ``regular_user`` by default), so demoting to
    ``regular_user`` alone is no longer enough to be a non-author — the user
    keeps ``vv.create``. We also revoke that grant so the user genuinely has
    no authoring right, then assert the gate rejects them.
    """
    svc = _make_service()
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        # basics.user_id is super_user (sentinel grant), so use the other
        # user. Demote to regular_user *and* strip the default vv.create so
        # they hold neither authoring permission.
        await session.execute(
            sa.text(
                'UPDATE "user" SET role_id = '
                "(SELECT id FROM role WHERE name = 'regular_user') "
                "WHERE id = :uid"
            ).bindparams(uid=basics.other_user_id)
        )
        await session.execute(
            sa.text(
                "DELETE FROM role_permission WHERE permission = 'vv.create' "
                "AND role_id = (SELECT id FROM role WHERE name = 'regular_user')"
            )
        )
        with pytest.raises(PermissionDeniedError):
            await svc.record_tool_vv(
                session,
                actor_user_id=basics.other_user_id,
                tool_version_id=basics.tool_version_id_a,
                kind="verification",
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_record_tool_vv_rejects_unknown_kind(vv_engine: AsyncEngine, basics: Basics) -> None:
    """A kind outside the closed vocabulary raises ``ValueError``."""
    svc = _make_service()
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await _grant_vv_curator(session, basics.user_id)
        with pytest.raises(ValueError, match="not a valid tool V&V kind"):
            await svc.record_tool_vv(
                session,
                actor_user_id=basics.user_id,
                tool_version_id=basics.tool_version_id_a,
                kind="invented",
            )


@pytest.mark.asyncio
async def test_update_tool_record_blocked_in_non_draft(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """``update_tool_record`` refuses to edit a non-draft record."""
    svc = _make_service()
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await _grant_vv_curator(session, basics.user_id)
        # Insert a row already in pending state via raw SQL.
        await session.execute(
            sa.text(
                "INSERT INTO tool_vv_record "
                "(tool_version_id, kind, state, recorded_by_user_id, recorded_at, version) "
                "VALUES (:tv, 'verification', 'pending', :uid, :now, 1)"
            ).bindparams(tv=basics.tool_version_id_a, uid=basics.user_id, now=_utcnow())
        )
        record_id = (
            await session.execute(sa.text("SELECT MAX(id) FROM tool_vv_record"))
        ).scalar_one()
        with pytest.raises(VVStateTransitionError):
            await svc.update_tool_record(
                session,
                actor_user_id=basics.user_id,
                record_id=record_id,
                notes="trying to edit a pending record",
            )
        await session.rollback()


# ---- The badge + inheritance (the ADR-0011 Option C invariant) -----


@pytest.mark.asyncio
async def test_badge_none_when_no_records(vv_engine: AsyncEngine, basics: Basics) -> None:
    """``summarise_tool_status`` returns ``kind='none'`` for a tool with no records."""
    svc = _make_service()
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        badge = await svc.summarise_tool_status(session, tool_version_id=basics.tool_version_id_a)
    assert badge.kind == "none"
    assert badge.has_approved_verification is False
    assert badge.has_approved_validation is False
    assert badge.contributing_version_ids == ()
    assert badge.inherited is False


@pytest.mark.asyncio
async def test_badge_verified_validated_with_both_approvals(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """``kind='verified_validated'`` requires approved records of BOTH kinds."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        for vv_kind in ("verification", "validation"):
            await session.execute(
                sa.text(
                    "INSERT INTO tool_vv_record "
                    "(tool_version_id, kind, state, "
                    " decided_by_user_id, decided_at, "
                    " recorded_by_user_id, recorded_at, version) "
                    "VALUES (:tv, :k, 'approved', :did, :now, :uid, :now, 1)"
                ).bindparams(
                    tv=basics.tool_version_id_a,
                    k=vv_kind,
                    did=basics.other_user_id,
                    uid=basics.user_id,
                    now=_utcnow(),
                )
            )

    svc = _make_service()
    async with session_scope(factory) as session:
        badge = await svc.summarise_tool_status(session, tool_version_id=basics.tool_version_id_a)
    assert badge.kind == "verified_validated"
    assert badge.has_approved_verification is True
    assert badge.has_approved_validation is True
    assert badge.contributing_version_ids == (basics.tool_version_id_a,)
    assert badge.inherited is False


@pytest.mark.asyncio
async def test_badge_inherits_across_matching_bundle_hash(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """A V&V record on cohort-mate (same alias + bundle_hash) inherits.

    The core Option C invariant: V&V on version X applies to every
    version Y where ``(Y.alias, Y.bundle_hash) == (X.alias, X.bundle_hash)``.
    Setup: insert a second ``tool_version`` with the same alias and the
    same bundle hash as ``tool_version_id_a``, record V&V on the FIRST,
    then assert the SECOND's badge reflects it with ``inherited=True``.
    """
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        # Cohort-mate of tv_a (same alias + same bundle_hash).
        cohort_mate_id = await _seed_extra_tool_version(
            session,
            alias="demo",
            version="1.0.1",
            bundle_hash="aaaa" * 16,  # SAME as tv_a's hash from conftest
            created_by_user_id=basics.user_id,
        )
        # Approved validation on tv_a.
        await session.execute(
            sa.text(
                "INSERT INTO tool_vv_record "
                "(tool_version_id, kind, state, "
                " applicable_range_json, "
                " decided_by_user_id, decided_at, "
                " recorded_by_user_id, recorded_at, version) "
                "VALUES (:tv, 'validation', 'approved', '{}', "
                "        :did, :now, :uid, :now, 1)"
            ).bindparams(
                tv=basics.tool_version_id_a,
                did=basics.other_user_id,
                uid=basics.user_id,
                now=_utcnow(),
            )
        )

    svc = _make_service()
    async with session_scope(factory) as session:
        badge = await svc.summarise_tool_status(session, tool_version_id=cohort_mate_id)
    # Inherited from tv_a, NOT recorded against cohort_mate_id itself.
    assert badge.has_approved_validation is True
    assert badge.inherited is True
    assert badge.contributing_version_ids == (basics.tool_version_id_a,)
    assert badge.cohort_bundle_hash == "aaaa" * 16


@pytest.mark.asyncio
async def test_badge_does_not_inherit_across_differing_bundle_hash(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """A V&V record on a DIFFERENT-hash version does NOT contribute to the badge.

    Setup: tv_b has a different bundle hash from tv_a (conftest seeds
    "aaaa…" vs "bbbb…"). Record an approval on tv_a; tv_b's badge must
    NOT pick it up — they're in different cohorts.
    """
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await session.execute(
            sa.text(
                "INSERT INTO tool_vv_record "
                "(tool_version_id, kind, state, "
                " decided_by_user_id, decided_at, "
                " recorded_by_user_id, recorded_at, version) "
                "VALUES (:tv, 'verification', 'approved', "
                "        :did, :now, :uid, :now, 1)"
            ).bindparams(
                tv=basics.tool_version_id_a,
                did=basics.other_user_id,
                uid=basics.user_id,
                now=_utcnow(),
            )
        )

    svc = _make_service()
    async with session_scope(factory) as session:
        badge_b = await svc.summarise_tool_status(session, tool_version_id=basics.tool_version_id_b)
    # tv_b has a different bundle hash; the approval on tv_a is invisible.
    assert badge_b.kind == "none"
    assert badge_b.has_approved_verification is False
    assert badge_b.contributing_version_ids == ()
    assert badge_b.inherited is False
    assert badge_b.cohort_bundle_hash == "bbbb" * 16


@pytest.mark.asyncio
async def test_badge_excludes_retired_records(vv_engine: AsyncEngine, basics: Basics) -> None:
    """A retired record stops contributing to the badge (cohort-wide effect)."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await session.execute(
            sa.text(
                "INSERT INTO tool_vv_record "
                "(tool_version_id, kind, state, "
                " recorded_by_user_id, recorded_at, version) "
                "VALUES (:tv, 'verification', 'retired', :uid, :now, 1)"
            ).bindparams(tv=basics.tool_version_id_a, uid=basics.user_id, now=_utcnow())
        )

    svc = _make_service()
    async with session_scope(factory) as session:
        badge = await svc.summarise_tool_status(session, tool_version_id=basics.tool_version_id_a)
    assert badge.kind == "none"  # no approved + no pending; retired is excluded


@pytest.mark.asyncio
async def test_badge_empty_bundle_hash_is_singleton_cohort(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """``bundle_hash=''`` (the "unknown" sentinel) does NOT inherit to/from.

    Insert a tool_version with empty-string hash. Even if another version
    shares the same alias + has approved records, the unknown-hash row
    sees only its own records.
    """
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        unknown_hash_id = await _seed_extra_tool_version(
            session,
            alias="demo",
            version="1.0.2",
            bundle_hash="",  # explicitly the "unknown" sentinel
            created_by_user_id=basics.user_id,
        )
        # Approved validation on tv_a (different hash from unknown).
        await session.execute(
            sa.text(
                "INSERT INTO tool_vv_record "
                "(tool_version_id, kind, state, "
                " decided_by_user_id, decided_at, "
                " recorded_by_user_id, recorded_at, version) "
                "VALUES (:tv, 'validation', 'approved', :did, :now, :uid, :now, 1)"
            ).bindparams(
                tv=basics.tool_version_id_a,
                did=basics.other_user_id,
                uid=basics.user_id,
                now=_utcnow(),
            )
        )

    svc = _make_service()
    async with session_scope(factory) as session:
        badge = await svc.summarise_tool_status(session, tool_version_id=unknown_hash_id)
    assert badge.kind == "none"
    assert badge.cohort_bundle_hash == ""
    assert badge.contributing_version_ids == ()


@pytest.mark.asyncio
async def test_badge_pending_with_no_approved(vv_engine: AsyncEngine, basics: Basics) -> None:
    """A cohort with only pending records (no approved) gets ``kind='pending'``."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await session.execute(
            sa.text(
                "INSERT INTO tool_vv_record "
                "(tool_version_id, kind, state, "
                " recorded_by_user_id, recorded_at, version) "
                "VALUES (:tv, 'verification', 'pending', :uid, :now, 1)"
            ).bindparams(tv=basics.tool_version_id_a, uid=basics.user_id, now=_utcnow())
        )

    svc = _make_service()
    async with session_scope(factory) as session:
        badge = await svc.summarise_tool_status(session, tool_version_id=basics.tool_version_id_a)
    assert badge.kind == "pending"
    assert badge.has_pending is True


# ---- Envelope soft-warn ---------------------------------------------


@pytest.mark.asyncio
async def test_envelope_warning_fires_outside_range(vv_engine: AsyncEngine, basics: Basics) -> None:
    """Submitting a value outside the approved envelope returns a warning."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await session.execute(
            sa.text(
                "INSERT INTO tool_vv_record "
                "(tool_version_id, kind, state, applicable_range_json, "
                " decided_by_user_id, decided_at, "
                " recorded_by_user_id, recorded_at, version) "
                "VALUES (:tv, 'validation', 'approved', "
                '        \'{"temperature": {"min": 300, "max": 600}}\', '
                "        :did, :now, :uid, :now, 1)"
            ).bindparams(
                tv=basics.tool_version_id_a,
                did=basics.other_user_id,
                uid=basics.user_id,
                now=_utcnow(),
            )
        )

    svc = _make_service()
    async with session_scope(factory) as session:
        warning = await svc.resolve_envelope_warning(
            session,
            tool_version_id=basics.tool_version_id_a,
            form_values={"temperature": 800.0},
        )
    assert warning is not None
    assert len(warning.out_of_range) == 1
    factor, value, _lo, hi = warning.out_of_range[0]
    assert factor == "temperature"
    assert value == 800.0
    assert hi == 600.0


@pytest.mark.asyncio
async def test_envelope_warning_silent_inside_range(vv_engine: AsyncEngine, basics: Basics) -> None:
    """A value inside the approved envelope returns ``None`` (no warn)."""
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await session.execute(
            sa.text(
                "INSERT INTO tool_vv_record "
                "(tool_version_id, kind, state, applicable_range_json, "
                " decided_by_user_id, decided_at, "
                " recorded_by_user_id, recorded_at, version) "
                "VALUES (:tv, 'validation', 'approved', "
                '        \'{"temperature": {"min": 300, "max": 600}}\', '
                "        :did, :now, :uid, :now, 1)"
            ).bindparams(
                tv=basics.tool_version_id_a,
                did=basics.other_user_id,
                uid=basics.user_id,
                now=_utcnow(),
            )
        )

    svc = _make_service()
    async with session_scope(factory) as session:
        warning = await svc.resolve_envelope_warning(
            session,
            tool_version_id=basics.tool_version_id_a,
            form_values={"temperature": 450.0},
        )
    assert warning is None


@pytest.mark.asyncio
async def test_envelope_warning_silent_when_no_validation(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """No approved validation → no envelope → no warning, regardless of value."""
    svc = _make_service()
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        warning = await svc.resolve_envelope_warning(
            session,
            tool_version_id=basics.tool_version_id_a,
            form_values={"temperature": 99999.0},
        )
    assert warning is None


# ---- Submit → approve / reject lifecycle ----------------------------


@pytest.mark.asyncio
async def test_submit_creates_approval_request(vv_engine: AsyncEngine, basics: Basics) -> None:
    """``submit_tool_record`` flips draft → pending + opens an approval_request."""
    svc = _make_service()
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await _grant_vv_curator(session, basics.user_id)
        await _grant_vv_curator(session, basics.other_user_id)
        row = await svc.record_tool_vv(
            session,
            actor_user_id=basics.user_id,
            tool_version_id=basics.tool_version_id_a,
            kind="verification",
        )
        await svc.submit_tool_record(
            session,
            actor_user_id=basics.user_id,
            record_id=row.id,
            designated_approver_user_id=basics.other_user_id,
            designated_approver_username="brendan",
        )

    async with session_scope(factory) as session:
        ar_count = (
            await session.execute(
                sa.text(
                    "SELECT COUNT(*) FROM approval_request "
                    "WHERE tool_vv_record_id = :rid AND status = 'pending'"
                ).bindparams(rid=row.id)
            )
        ).scalar_one()
        record_state = (
            await session.execute(
                sa.text("SELECT state FROM tool_vv_record WHERE id = :rid").bindparams(rid=row.id)
            )
        ).scalar_one()
    assert ar_count == 1
    assert record_state == "pending"


@pytest.mark.asyncio
async def test_submit_blocked_by_sole_curator_guard(vv_engine: AsyncEngine, basics: Basics) -> None:
    """If only one ``vv_curator`` exists, submit raises ``VVSoleCuratorError``."""
    svc = _make_service()
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        # Only the author has VV_CURATE — make the second user a non-curator
        # AND drop the second user out of super_user (which implicitly holds
        # everything via the catalog sentinel) so the count of curators
        # excluding the author is genuinely zero.
        await session.execute(
            sa.text(
                'UPDATE "user" SET role_id = '
                "(SELECT id FROM role WHERE name = 'regular_user') "
                "WHERE id = :uid"
            ).bindparams(uid=basics.other_user_id)
        )
        await _grant_vv_curator(session, basics.user_id)
        row = await svc.record_tool_vv(
            session,
            actor_user_id=basics.user_id,
            tool_version_id=basics.tool_version_id_a,
            kind="verification",
        )
        with pytest.raises(VVSoleCuratorError):
            await svc.submit_tool_record(
                session,
                actor_user_id=basics.user_id,
                record_id=row.id,
                designated_approver_user_id=basics.other_user_id,  # not a curator
                designated_approver_username="brendan",
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_submit_without_approver_raises(vv_engine: AsyncEngine, basics: Basics) -> None:
    """Submit with ``approval_required=True`` and no approver picked raises."""
    svc = _make_service()
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await _grant_vv_curator(session, basics.user_id)
        await _grant_vv_curator(session, basics.other_user_id)
        row = await svc.record_tool_vv(
            session,
            actor_user_id=basics.user_id,
            tool_version_id=basics.tool_version_id_a,
            kind="verification",
        )
        with pytest.raises(VVApproverRequiredError):
            await svc.submit_tool_record(
                session,
                actor_user_id=basics.user_id,
                record_id=row.id,
                designated_approver_user_id=None,
                designated_approver_username=None,
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_approve_flips_state_and_stamps_decided(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """``approve_tool_record`` flips pending → approved with decided_*."""
    svc = _make_service()
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await _grant_vv_curator(session, basics.user_id)
        await _grant_vv_curator(session, basics.other_user_id)
        row = await svc.record_tool_vv(
            session,
            actor_user_id=basics.user_id,
            tool_version_id=basics.tool_version_id_a,
            kind="verification",
        )
        await svc.submit_tool_record(
            session,
            actor_user_id=basics.user_id,
            record_id=row.id,
            designated_approver_user_id=basics.other_user_id,
            designated_approver_username="brendan",
        )
        await svc.approve_tool_record(
            session,
            decider_user_id=basics.other_user_id,
            decider_username="brendan",
            record_id=row.id,
        )

    async with session_scope(factory) as session:
        loaded = (
            await session.execute(
                sa.text(
                    "SELECT state, decided_by_user_id, decided_at "
                    "FROM tool_vv_record WHERE id = :rid"
                ).bindparams(rid=row.id)
            )
        ).first()
    assert loaded is not None
    state, decided_by, decided_at = loaded
    assert state == "approved"
    assert decided_by == basics.other_user_id
    assert decided_at is not None


@pytest.mark.asyncio
async def test_reject_requires_reason(vv_engine: AsyncEngine, basics: Basics) -> None:
    """``reject_tool_record`` without a reason raises ``VVDecisionReasonRequiredError``."""
    svc = _make_service()
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await _grant_vv_curator(session, basics.user_id)
        with pytest.raises(VVDecisionReasonRequiredError):
            await svc.reject_tool_record(
                session,
                decider_user_id=basics.user_id,
                decider_username="anika",
                record_id=999,  # doesn't matter — the reason check fires first
                reason="",
            )


@pytest.mark.asyncio
async def test_reject_with_reason_lands_rejected_state(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """``reject_tool_record`` with a reason flips to ``rejected``."""
    svc = _make_service()
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await _grant_vv_curator(session, basics.user_id)
        await _grant_vv_curator(session, basics.other_user_id)
        row = await svc.record_tool_vv(
            session,
            actor_user_id=basics.user_id,
            tool_version_id=basics.tool_version_id_a,
            kind="verification",
        )
        await svc.submit_tool_record(
            session,
            actor_user_id=basics.user_id,
            record_id=row.id,
            designated_approver_user_id=basics.other_user_id,
            designated_approver_username="brendan",
        )
        await svc.reject_tool_record(
            session,
            decider_user_id=basics.other_user_id,
            decider_username="brendan",
            record_id=row.id,
            reason="evidence dataset is too narrow",
        )

    async with session_scope(factory) as session:
        loaded = (
            await session.execute(
                sa.text(
                    "SELECT state, decision_reason FROM tool_vv_record WHERE id = :rid"
                ).bindparams(rid=row.id)
            )
        ).first()
    assert loaded is not None
    state, reason = loaded
    assert state == "rejected"
    assert reason == "evidence dataset is too narrow"


# ---- Retire ----------------------------------------------------------


@pytest.mark.asyncio
async def test_retire_tool_record_from_approved(vv_engine: AsyncEngine, basics: Basics) -> None:
    """An approved record can transition to retired (curate-gated)."""
    svc = _make_service()
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await _grant_vv_curator(session, basics.user_id)
        await session.execute(
            sa.text(
                "INSERT INTO tool_vv_record "
                "(tool_version_id, kind, state, "
                " decided_by_user_id, decided_at, "
                " recorded_by_user_id, recorded_at, version) "
                "VALUES (:tv, 'verification', 'approved', :did, :now, :uid, :now, 1)"
            ).bindparams(
                tv=basics.tool_version_id_a,
                did=basics.other_user_id,
                uid=basics.user_id,
                now=_utcnow(),
            )
        )
        record_id = (
            await session.execute(sa.text("SELECT MAX(id) FROM tool_vv_record"))
        ).scalar_one()
        await svc.retire_tool_record(session, actor_user_id=basics.user_id, record_id=record_id)

    async with session_scope(factory) as session:
        state = (
            await session.execute(
                sa.text("SELECT state FROM tool_vv_record WHERE id = :rid").bindparams(
                    rid=record_id
                )
            )
        ).scalar_one()
    assert state == "retired"


@pytest.mark.asyncio
async def test_retire_pending_tool_record_withdraws_gate(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """Retiring a *pending* tool record lands retired and closes the open gate."""
    svc = _make_service()
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await _grant_vv_curator(session, basics.user_id)
        await _grant_vv_curator(session, basics.other_user_id)
        row = await svc.record_tool_vv(
            session,
            actor_user_id=basics.user_id,
            tool_version_id=basics.tool_version_id_a,
            kind="verification",
        )
        await svc.submit_tool_record(
            session,
            actor_user_id=basics.user_id,
            record_id=row.id,
            designated_approver_user_id=basics.other_user_id,
            designated_approver_username="brendan",
        )
        # The gate exists and is pending before we retire.
        gate_before = (
            await session.execute(
                sa.text(
                    "SELECT status FROM approval_request WHERE tool_vv_record_id = :rid"
                ).bindparams(rid=row.id)
            )
        ).scalar_one()
        assert gate_before == "pending"
        await svc.retire_tool_record(session, actor_user_id=basics.user_id, record_id=row.id)

    async with session_scope(factory) as session:
        rec_state = (
            await session.execute(
                sa.text("SELECT state FROM tool_vv_record WHERE id = :rid").bindparams(rid=row.id)
            )
        ).scalar_one()
        gate = (
            await session.execute(
                sa.text(
                    "SELECT status, decided_via, decision_reason "
                    "FROM approval_request WHERE tool_vv_record_id = :rid"
                ).bindparams(rid=row.id)
            )
        ).first()
    assert rec_state == "retired"
    assert gate is not None
    status, decided_via, reason = gate
    assert status == "denied"  # closed terminally, reusing the denied status
    assert decided_via == "self"  # the submitter withdrew it
    assert "retired" in (reason or "")


@pytest.mark.asyncio
async def test_retire_pending_job_record(vv_engine: AsyncEngine, basics: Basics) -> None:
    """A pending job record can also be retired (gate withdrawn)."""
    svc = _make_service()
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await _grant_vv_curator(session, basics.user_id)
        await _grant_vv_curator(session, basics.other_user_id)
        row = await svc.record_job_vv(
            session,
            actor_user_id=basics.user_id,
            job_id=basics.job_id,
            kind="validated_against",
        )
        await svc.submit_job_record(
            session,
            actor_user_id=basics.user_id,
            record_id=row.id,
            designated_approver_user_id=basics.other_user_id,
            designated_approver_username="brendan",
        )
        await svc.retire_job_record(session, actor_user_id=basics.user_id, record_id=row.id)

    async with session_scope(factory) as session:
        rec_state = (
            await session.execute(
                sa.text("SELECT state FROM job_vv_record WHERE id = :rid").bindparams(rid=row.id)
            )
        ).scalar_one()
        gate_status = (
            await session.execute(
                sa.text(
                    "SELECT status FROM approval_request WHERE job_vv_record_id = :rid"
                ).bindparams(rid=row.id)
            )
        ).scalar_one()
    assert rec_state == "retired"
    assert gate_status == "denied"


# ---- Migrate to a new version ---------------------------------------


async def _make_approved_tool_record(
    svc: object,
    session: sa.ext.asyncio.AsyncSession,
    basics: Basics,
    *,
    kind: str = "validation",
    applicable_range: dict[str, dict[str, float]] | None = None,
) -> int:
    """Author → submit → approve a tool record; return its id."""
    row = await svc.record_tool_vv(  # type: ignore[attr-defined]
        session,
        actor_user_id=basics.user_id,
        tool_version_id=basics.tool_version_id_a,
        kind=kind,
        applicable_range=applicable_range or {"temperature": {"min": 300.0, "max": 600.0}},
        confidence_band=0.02,
        standards_cited=["ASME V&V 10"],
        notes="rig validation",
    )
    await svc.submit_tool_record(  # type: ignore[attr-defined]
        session,
        actor_user_id=basics.user_id,
        record_id=row.id,
        designated_approver_user_id=basics.other_user_id,
        designated_approver_username="brendan",
    )
    await svc.approve_tool_record(  # type: ignore[attr-defined]
        session,
        decider_user_id=basics.other_user_id,
        decider_username="brendan",
        record_id=row.id,
    )
    return int(row.id)


@pytest.mark.asyncio
async def test_migrate_copies_approved_as_draft(vv_engine: AsyncEngine, basics: Basics) -> None:
    """Migration copies an approved record to the target as a fresh draft."""
    svc = _make_service()
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await _grant_vv_curator(session, basics.user_id)
        await _grant_vv_curator(session, basics.other_user_id)
        source_id = await _make_approved_tool_record(svc, session, basics)
        migrated, skipped = await svc.migrate_tool_records(
            session,
            actor_user_id=basics.user_id,
            source_tool_version_id=basics.tool_version_id_a,
            target_tool_version_id=basics.tool_version_id_b,
            record_ids=[source_id],
        )
    assert skipped == []
    assert len(migrated) == 1
    new = migrated[0]
    assert new.tool_version_id == basics.tool_version_id_b
    assert new.state == "draft"
    assert new.kind == "validation"
    assert new.applicable_range_json == {"temperature": {"min": 300.0, "max": 600.0}}
    assert new.standards_cited_json == ["ASME V&V 10"]
    assert new.recorded_by_user_id == basics.user_id


@pytest.mark.asyncio
async def test_migrate_skips_non_approved_and_wrong_source(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """A draft (not approved) and a record on the wrong source are skipped."""
    svc = _make_service()
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await _grant_vv_curator(session, basics.user_id)
        await _grant_vv_curator(session, basics.other_user_id)
        draft = await svc.record_tool_vv(
            session,
            actor_user_id=basics.user_id,
            tool_version_id=basics.tool_version_id_a,
            kind="verification",
        )
        # A record that lives on the target, not the source.
        wrong = await svc.record_tool_vv(
            session,
            actor_user_id=basics.user_id,
            tool_version_id=basics.tool_version_id_b,
            kind="verification",
        )
        migrated, skipped = await svc.migrate_tool_records(
            session,
            actor_user_id=basics.user_id,
            source_tool_version_id=basics.tool_version_id_a,
            target_tool_version_id=basics.tool_version_id_b,
            record_ids=[draft.id, wrong.id],
        )
    assert migrated == []
    skipped_ids = {rid for rid, _reason in skipped}
    assert skipped_ids == {draft.id, wrong.id}


@pytest.mark.asyncio
async def test_migrate_same_version_and_cross_tool_raise(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """Whole-request invalids (same version / different tool) raise VVMigrationError."""
    svc = _make_service()
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await _grant_vv_curator(session, basics.user_id)
        with pytest.raises(VVMigrationError):
            await svc.migrate_tool_records(
                session,
                actor_user_id=basics.user_id,
                source_tool_version_id=basics.tool_version_id_a,
                target_tool_version_id=basics.tool_version_id_a,
                record_ids=[1],
            )
        # A different tool needs its own `tool` row (tool_version.alias FKs it).
        from matofx.tools.orm import Tool  # noqa: PLC0415

        session.add(
            Tool(
                alias="other_tool",
                name="Other",
                version="1.0",
                state="released",
                descriptor={"alias": "other_tool"},
                bundle_path="bundle/other_tool",
                installed_by_user_id=basics.user_id,
            )
        )
        await session.flush()
        other_alias_id = await _seed_extra_tool_version(
            session,
            alias="other_tool",
            version="1.0.0",
            bundle_hash="dddd" * 16,
            created_by_user_id=basics.user_id,
        )
        with pytest.raises(VVMigrationError):
            await svc.migrate_tool_records(
                session,
                actor_user_id=basics.user_id,
                source_tool_version_id=basics.tool_version_id_a,
                target_tool_version_id=other_alias_id,
                record_ids=[1],
            )


@pytest.mark.asyncio
async def test_migrate_is_idempotent(vv_engine: AsyncEngine, basics: Basics) -> None:
    """Re-running the same migration skips the identical copy already on target."""
    svc = _make_service()
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await _grant_vv_curator(session, basics.user_id)
        await _grant_vv_curator(session, basics.other_user_id)
        source_id = await _make_approved_tool_record(svc, session, basics)
        first, _ = await svc.migrate_tool_records(
            session,
            actor_user_id=basics.user_id,
            source_tool_version_id=basics.tool_version_id_a,
            target_tool_version_id=basics.tool_version_id_b,
            record_ids=[source_id],
        )
        assert len(first) == 1
        second, skipped = await svc.migrate_tool_records(
            session,
            actor_user_id=basics.user_id,
            source_tool_version_id=basics.tool_version_id_a,
            target_tool_version_id=basics.tool_version_id_b,
            record_ids=[source_id],
        )
    assert second == []
    assert len(skipped) == 1
    assert skipped[0][0] == source_id


# ---- Curator inbox --------------------------------------------------


@pytest.mark.asyncio
async def test_list_pending_for_curator_unified_inbox(
    vv_engine: AsyncEngine, basics: Basics
) -> None:
    """Pending tool + job records appear in one inbox, oldest-first."""
    svc = _make_service()
    factory = make_session_factory(vv_engine)
    async with session_scope(factory) as session:
        await _grant_vv_curator(session, basics.user_id)
        # Two pending records — one tool, one job.
        await session.execute(
            sa.text(
                "INSERT INTO tool_vv_record "
                "(tool_version_id, kind, state, "
                " recorded_by_user_id, recorded_at, version) "
                "VALUES (:tv, 'verification', 'pending', :uid, :now, 1)"
            ).bindparams(tv=basics.tool_version_id_a, uid=basics.user_id, now=_utcnow())
        )
        await session.execute(
            sa.text(
                "INSERT INTO job_vv_record "
                "(job_id, kind, state, "
                " recorded_by_user_id, recorded_at, version) "
                "VALUES (:j, 'validated_against', 'pending', :uid, :now, 1)"
            ).bindparams(j=basics.job_id, uid=basics.user_id, now=_utcnow())
        )
        inbox = await svc.list_pending_for_curator(session, actor_user_id=basics.user_id)
    kinds = {p.record_kind for p in inbox}
    assert kinds == {"tool", "job"}
    assert len(inbox) == 2
