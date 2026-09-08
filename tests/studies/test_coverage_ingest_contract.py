"""The ingest contract design-space coverage depends on (ADR-0020 §1.1, §6).

Coverage consumes the :class:`TrainingData` ``ingest.collect_training_data``
produces — never raw cohort membership. Two things the coverage engine needs
are not on that dataclass today, and both are load-bearing:

1. **``categorical_columns``.** ``collect_training_data`` keeps *numeric*
   inputs only, so a varied ``select`` / ``bool`` / ``file`` input never
   reaches the training data at all. Categorical imbalance is the silent
   failure of ADR-0020's three diseases (40 CFRP / 2 GFRP looks perfect on
   every continuous axis), so it cannot be measured from a numeric-only view.

2. **``n_excluded_filtered``.** ``FactorFilter`` drops are ``continue``d
   silently, so "62 of 100" cannot be explained to the user — §6's
   ``CoverageDiagnostics.n_excluded_filtered`` has no source.

Both additions are purely additive to a dataclass with defaults, so
``fit_surrogate`` and every existing caller are untouched. Seeding goes
through the ORM (``feedback_test_seeding_via_orm``).
"""

from __future__ import annotations

from matofx.audit import ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy.orm import Job
from matofx.studies import FactorFilter, ResponseSpec, collect_training_data
from matofx.studies.orm import ParametricStudy, StudyResult
from tests.studies.conftest import Basics


def _study(basics: Basics) -> ParametricStudy:
    return ParametricStudy(
        project_id=basics.project_id,
        name="coverage-ingest",
        tool_alias=basics.tool_alias,
        tool_version_id=basics.tool_version_id,
        mode="grid",
        factor_spec={},
        sampling_params={},
        run_cap=100,
        created_by_user_id=basics.user_id,
    )


async def _seed(
    session,
    basics: Basics,
    study_id: int,
    *,
    inputs: dict[str, object],
    y: float,
    validity: str = "valid",
) -> None:
    job = Job(
        parametric_study_id=study_id,
        scenario_id=None,
        name="run",
        tool_alias=basics.tool_alias,
        tool_version_id=basics.tool_version_id,
        state="complete",
        inputs=inputs,
        created_by_user_id=basics.user_id,
    )
    session.add(job)
    await session.flush()
    session.add(
        StudyResult(
            source_kind="parametric",
            source_id=study_id,
            job_id=job.id,
            scalars={"y": y},
            retained_files=[],
            validity=validity,
            validity_reasons=[],
        )
    )
    await session.flush()


async def test_a_varied_categorical_input_reaches_the_training_data(
    studies_engine,
    basics: Basics,
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _study(basics)
        session.add(study)
        await session.flush()
        for i, material in enumerate(("cfrp", "cfrp", "gfrp", "cfrp", "cfrp", "cfrp")):
            await _seed(
                session,
                basics,
                study.id,
                inputs={"thickness": float(i + 1), "material": material},
                y=float(i),
            )

        data = await collect_training_data(
            session,
            source_kind="parametric",
            source_id=study.id,
            response_spec=ResponseSpec(kind="scalar", name="y"),
        )

    # Numeric inputs keep their existing home, untouched.
    assert data.factor_columns["thickness"] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    # The varied categorical is carried alongside, row-aligned — without it
    # the 5/1 imbalance is invisible to coverage.
    assert data.categorical_columns["material"] == [
        "cfrp",
        "cfrp",
        "gfrp",
        "cfrp",
        "cfrp",
        "cfrp",
    ]


async def test_a_constant_categorical_input_stays_a_constant_not_a_column(
    studies_engine,
    basics: Basics,
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _study(basics)
        session.add(study)
        await session.flush()
        for i in range(5):
            await _seed(
                session,
                basics,
                study.id,
                inputs={"thickness": float(i + 1), "mesh_density": "fine"},
                y=float(i),
            )

        data = await collect_training_data(
            session,
            source_kind="parametric",
            source_id=study.id,
            response_spec=ResponseSpec(kind="scalar", name="y"),
        )

    assert data.input_constants["mesh_density"] == "fine"
    assert "mesh_density" not in data.categorical_columns


async def test_factor_filter_drops_are_counted_not_swallowed(
    studies_engine,
    basics: Basics,
) -> None:
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _study(basics)
        session.add(study)
        await session.flush()
        for i in range(10):
            await _seed(session, basics, study.id, inputs={"thickness": float(i + 1)}, y=float(i))

        data = await collect_training_data(
            session,
            source_kind="parametric",
            source_id=study.id,
            response_spec=ResponseSpec(kind="scalar", name="y"),
            factor_filter=FactorFilter(input_ranges={"thickness": {"min": 4.0}}),
        )

    assert data.n_rows == 7  # thickness 4..10
    assert data.n_excluded_filtered == 3  # 1, 2, 3 — explains "7 of 10"


async def test_validity_and_filter_exclusions_are_counted_separately(
    studies_engine,
    basics: Basics,
) -> None:
    # A user reading "62 of 100" needs to know which gate took which runs:
    # an invalid result is a solver problem, a filtered one is their own
    # on-the-fly exclusion.
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        study = _study(basics)
        session.add(study)
        await session.flush()
        for i in range(10):
            await _seed(
                session,
                basics,
                study.id,
                inputs={"thickness": float(i + 1)},
                y=float(i),
                validity="invalid" if i >= 8 else "valid",
            )

        data = await collect_training_data(
            session,
            source_kind="parametric",
            source_id=study.id,
            response_spec=ResponseSpec(kind="scalar", name="y"),
            factor_filter=FactorFilter(input_ranges={"thickness": {"min": 4.0}}),
        )

    assert data.n_excluded_filtered == 3
    assert data.n_excluded_invalid == 2
    assert data.n_rows == 5
