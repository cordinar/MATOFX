"""Coverage stored on the model version (ADR-0020 §9, §12, slice 5).

The verdict is computed **at fit** and travels with the version, so the model
record, the per-job report and the V&V trail all read the same evidence rather
than each recomputing it. Two properties matter as much as the storage:

- an absent report renders **"not assessed"**, never "good" — silence about
  coverage and a clean bill of health are opposite claims, and every version
  fitted before this feature existed has no report;
- a **refit mints a new version**, so its report is computed fresh rather than
  inherited. §9 assumed that without confirming it; it holds
  (``refit`` calls ``_add_version(ordinal=latest.ordinal + 1)``).

Seeding through the ORM (``feedback_test_seeding_via_orm``).
"""

from __future__ import annotations

import itertools

import pytest

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.orm import Job
from matofx.models.orm import SurrogateModelVersion
from matofx.studies.coverage import CoverageReport
from matofx.studies.orm import ParametricStudy, StudyResult
from tests.studies.conftest import Basics

_SEQ = itertools.count(1)


def _audit() -> AuditWriter:
    return AuditWriter(signing_key=None, key_id=None)


def _service(tmp_path):
    from matofx.models import make_model_service  # noqa: PLC0415

    return make_model_service(_audit(), models_root=tmp_path / "store", environment="test")


async def _seed_source(session, basics: Basics, rows: list[dict]) -> int:
    """A parametric study whose children carry ``rows`` as their inputs."""
    study = ParametricStudy(
        project_id=basics.project_id,
        name=f"cov-{next(_SEQ)}",
        tool_alias=basics.tool_alias,
        tool_version_id=basics.tool_version_id,
        mode="grid",
        factor_spec={},
        sampling_params={},
        run_cap=500,
        created_by_user_id=basics.user_id,
    )
    session.add(study)
    await session.flush()
    for i, row in enumerate(rows):
        job = Job(
            parametric_study_id=study.id,
            scenario_id=None,
            name=f"run-{i}",
            tool_alias=basics.tool_alias,
            tool_version_id=basics.tool_version_id,
            state="complete",
            inputs=row,
            created_by_user_id=basics.user_id,
        )
        session.add(job)
        await session.flush()
        session.add(
            StudyResult(
                source_kind="parametric",
                source_id=study.id,
                job_id=job.id,
                scalars={"y": float(row.get("thickness", 1.0)) * 2.0},
                retained_files=[],
                validity="valid",
                validity_reasons=[],
            )
        )
    await session.flush()
    return int(study.id)


def _spread() -> list[dict]:
    return [
        {"thickness": float(a), "heat_load": float(b)}
        for a in (1.0, 3.0, 5.0, 7.0, 9.0)
        for b in (1.0, 3.0, 5.0, 7.0, 9.0)
    ]


def _clustered() -> list[dict]:
    rows = [
        {"thickness": 1.0 + i * 0.05, "heat_load": 1.0 + j * 0.05}
        for i in range(5)
        for j in range(5)
    ]
    rows.append({"thickness": 9.0, "heat_load": 9.0})
    return rows


async def _create(svc, session, basics, rows):
    source_id = await _seed_source(session, basics, rows)
    return await svc.create_model(
        session,
        actor_user_id=basics.user_id,
        name=f"m-{next(_SEQ)}",
        tool_alias=basics.tool_alias,
        scope="personal",
        source_kind="parametric",
        source_id=source_id,
        response_names=["y"],
    )


# --- written at fit ------------------------------------------------------


async def test_a_new_version_carries_its_coverage_verdict(
    studies_engine,
    basics: Basics,
    tmp_path,
) -> None:
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        _model, version, _ = await _create(svc, session, basics, _spread())

    assert version.coverage_verdict in ("good", "thin", "poor", "insufficient")
    assert version.coverage_report_json is not None


async def test_the_stored_report_parses_back_into_a_report(
    studies_engine,
    basics: Basics,
    tmp_path,
) -> None:
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        _model, version, _ = await _create(svc, session, basics, _spread())
        report = CoverageReport.from_json_obj(version.coverage_report_json)

    assert report.verdict == version.coverage_verdict
    assert {f.name for f in report.envelope.factors} >= {"thickness", "heat_load"}


async def test_a_well_spread_source_stores_a_good_verdict(
    studies_engine,
    basics: Basics,
    tmp_path,
) -> None:
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        _model, version, _ = await _create(svc, session, basics, _spread())
    assert version.coverage_verdict == "good"


async def test_a_clustered_source_stores_a_poor_verdict_without_blocking_the_fit(
    studies_engine,
    basics: Basics,
    tmp_path,
) -> None:
    # The verdict never blocks a fit — it is recorded and travels with the
    # model. A user may knowingly build on thin data; they may not do so
    # unknowingly.
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        _model, version, _ = await _create(svc, session, basics, _clustered())

    assert version.coverage_verdict == "poor"
    assert version.bundle_sha256  # the fit completed regardless


async def test_the_categorical_columns_reach_the_stored_report(
    studies_engine,
    basics: Basics,
    tmp_path,
) -> None:
    """The failure the whole feature exists to catch must survive the fit path.

    ``_fit_responses`` keeps only the fitted surrogates, and a ``Surrogate``'s
    ``training_inputs`` is numeric-only — so a report rebuilt from the fit
    alone would be blind to a 40-CFRP / 2-GFRP split, exactly as the cohort
    path was before finding 21.
    """
    rows = [
        {"thickness": 1.0 + i * 0.3, "heat_load": 1.0 + (i % 7), "material": "cfrp"}
        for i in range(24)
    ]
    rows += [{"thickness": 4.0, "heat_load": 3.0, "material": "gfrp"}]
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        _model, version, _ = await _create(svc, session, basics, rows)
        report = CoverageReport.from_json_obj(version.coverage_report_json)

    assert "material" in {f.name for f in report.envelope.factors}
    assert any(w.code == "categorical_imbalance" for w in report.warnings + report.design.warnings)


# --- an absent report is not a clean bill of health ----------------------


async def test_a_version_with_no_report_reads_not_assessed(
    studies_engine,
    basics: Basics,
    tmp_path,
) -> None:
    # Every version fitted before this feature existed has NULL here.
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        _model, version, _ = await _create(svc, session, basics, _spread())
        version.coverage_report_json = None
        version.coverage_verdict = None
        await session.flush()

        stored = await svc.coverage_report_for_version(
            session, actor_user_id=basics.user_id, version_id=version.id
        )

    assert stored is None  # the caller renders "not assessed", never "good"


async def test_the_columns_are_nullable_so_existing_rows_are_untouched(
    studies_engine,
    basics: Basics,
    tmp_path,
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        svc = _service(tmp_path)
        _model, version, _ = await _create(svc, session, basics, _spread())
        row = await session.get(SurrogateModelVersion, version.id)
        assert row is not None
        row.coverage_report_json = None
        row.coverage_verdict = None
        await session.flush()  # no NOT NULL violation


async def test_reading_a_stored_report_is_access_checked(
    studies_engine,
    basics: Basics,
    tmp_path,
) -> None:
    from matofx.hierarchy.errors import NotFoundError  # noqa: PLC0415

    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        with pytest.raises(NotFoundError):
            await svc.coverage_report_for_version(
                session, actor_user_id=basics.user_id, version_id=999_999
            )


# --- refit ---------------------------------------------------------------


async def test_a_refit_computes_its_own_verdict_rather_than_inheriting_one(
    studies_engine,
    basics: Basics,
    tmp_path,
) -> None:
    """§9 assumed a refit mints a new version. It does — so nothing goes stale.

    The stale-report risk would be real if a refit mutated the row in place;
    it does not, and this test is what keeps that true.
    """
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        model, first, _ = await _create(svc, session, basics, _spread())
        second = await svc.refit(session, actor_user_id=basics.user_id, model_id=model.id)

    assert second.id != first.id
    assert second.ordinal == first.ordinal + 1
    assert second.coverage_report_json is not None
    assert second.coverage_verdict == first.coverage_verdict  # same source, same answer


async def test_the_existing_training_range_gauge_is_untouched(
    studies_engine,
    basics: Basics,
    tmp_path,
) -> None:
    # ADR-0008 §7.3 already has a `coverage_for_version` meaning something
    # different — the in-form training-range gauge. The two must not collide.
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        _model, version, _ = await _create(svc, session, basics, _spread())
        gauge = await svc.coverage_for_version(
            session, actor_user_id=basics.user_id, version_id=version.id
        )

    assert isinstance(gauge, list)
    assert {f["name"] for f in gauge} >= {"thickness", "heat_load"}


# --- the envelope must carry descriptor labels, not internal names --------


async def test_stored_envelope_carries_labels_not_internal_names(
    studies_engine,
    basics: Basics,
    tmp_path,
) -> None:
    """Every coverage surface shows labels ([[feedback_use_labels_not_names]]).

    The fit path resolved no descriptor, so ``_label_for`` returned ``""`` for
    every axis and ``display_name`` fell back to the internal input name — in the
    model record, the V&V trail, and the Training data coverage panel on the
    exported tool. The cohort preview path always passed a descriptor; this one
    did not, and a blank label is indistinguishable from "no label declared".
    """
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        _model, version, _ = await _create(svc, session, basics, _spread())
        report = CoverageReport.from_json_obj(version.coverage_report_json)

    by_name = {f.name: f for f in report.envelope.factors}
    assert {"thickness", "heat_load"} <= set(by_name)
    for name in ("thickness", "heat_load"):
        factor = by_name[name]
        assert factor.label, f"{name} has no label - the descriptor was not resolved"
        assert factor.display_name == factor.label
        assert factor.display_name != name
