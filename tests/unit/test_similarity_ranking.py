"""Unit tests for ``rank_jobs_by_similarity`` (find-similar core).

The ranker is pure and DB-free — it reads only ``Job.id`` and
``Job.inputs`` — so these tests build transient (unsaved) ORM instances
and assert on ordering, normalisation, gating, weighting, and the
derived closeness score without touching a session.
"""

from __future__ import annotations

from matofx.hierarchy.orm import Job
from matofx.hierarchy.service import rank_jobs_by_similarity


def _job(job_id: int, inputs: dict) -> Job:
    """A transient Job carrying just the fields the ranker reads."""
    return Job(id=job_id, inputs=inputs)


def test_ranks_closest_first_uniform() -> None:
    jobs = [
        _job(1, {"t": 100.0}),
        _job(2, {"t": 50.0}),
        _job(3, {"t": 10.0}),
    ]
    ranked = rank_jobs_by_similarity(
        jobs,
        reference_inputs={"t": 12.0},
        numeric_fields={"t"},
    )
    # Job 3 (t=10) is nearest 12, then 2 (50), then 1 (100).
    assert [r.job.id for r in ranked] == [3, 2, 1]
    assert ranked[0].distance <= ranked[1].distance <= ranked[2].distance


def test_exact_match_is_full_closeness() -> None:
    jobs = [_job(1, {"t": 42.0}), _job(2, {"t": 0.0})]
    ranked = rank_jobs_by_similarity(jobs, reference_inputs={"t": 42.0}, numeric_fields={"t"})
    top = next(r for r in ranked if r.job.id == 1)
    assert top.distance == 0.0
    assert top.closeness_pct == 100.0


def test_min_max_normalisation_balances_scales() -> None:
    # Without normalisation the kelvin axis (huge magnitude) would
    # swamp the millimetre axis; normalised, an equal *fractional* miss
    # on each axis contributes equally.
    jobs = [
        _job(1, {"temp_k": 1000.0, "thk_mm": 1.0}),  # off on temp only
        _job(2, {"temp_k": 0.0, "thk_mm": 2.0}),  # off on thickness only
    ]
    ranked = rank_jobs_by_similarity(
        jobs,
        reference_inputs={"temp_k": 0.0, "thk_mm": 1.0},
        numeric_fields={"temp_k", "thk_mm"},
    )
    by_id = {r.job.id: r for r in ranked}
    # temp range [0,1000], thk range [1,2]: job1 is a full miss on temp,
    # job2 a full miss on thk — equal normalised distance.
    assert by_id[1].distance == by_id[2].distance


def test_categorical_gate_excludes_mismatches() -> None:
    jobs = [
        _job(1, {"mat": "steel", "t": 10.0}),
        _job(2, {"mat": "alu", "t": 10.0}),
    ]
    ranked = rank_jobs_by_similarity(
        jobs,
        reference_inputs={"mat": "steel", "t": 10.0},
        numeric_fields={"t"},
    )
    assert [r.job.id for r in ranked] == [1]


def test_missing_numeric_is_penalised_not_perfect() -> None:
    jobs = [
        _job(1, {"t": 10.0}),  # exact match
        _job(2, {"t": 0.0}),  # present but far (full normalised miss)
        _job(3, {}),  # missing the factor entirely
    ]
    ranked = rank_jobs_by_similarity(
        jobs, reference_inputs={"t": 10.0}, numeric_fields={"t"}, top_n=5
    )
    by_id = {r.job.id: r for r in ranked}
    # The missing-factor job is penalised the maximum normalised
    # distance — never a perfect match, and ranked below the exact hit.
    assert ranked[0].job.id == 1
    assert by_id[3].distance > by_id[1].distance
    assert by_id[3].distance == by_id[2].distance  # missing == worst present


def test_weights_change_ordering_vs_uniform() -> None:
    jobs = [
        _job(1, {"x": 10.0, "y": 0.0}),  # off on y
        _job(2, {"x": 0.0, "y": 10.0}),  # off on x
    ]
    ref = {"x": 10.0, "y": 10.0}
    fields = {"x", "y"}

    uniform = rank_jobs_by_similarity(jobs, reference_inputs=ref, numeric_fields=fields)
    assert uniform[0].distance == uniform[1].distance  # symmetric → tie

    weighted = rank_jobs_by_similarity(
        jobs, reference_inputs=ref, numeric_fields=fields, weights={"x": 2.0, "y": 1.0}
    )
    # x matters twice as much, so the job that is off on x (job 2) is
    # ranked further than the job off on the cheaper y axis (job 1).
    assert weighted[0].job.id == 1
    assert weighted[0].distance < weighted[1].distance


def test_unscored_active_field_falls_back_to_mean_weight() -> None:
    # 'z' varies and is in the reference but isn't in the study weights;
    # it should still count (mean of scored weights), so a job that
    # differs only on z is not treated as a perfect match.
    jobs = [_job(1, {"x": 0.0, "z": 100.0}), _job(2, {"x": 0.0, "z": 0.0})]
    ranked = rank_jobs_by_similarity(
        jobs,
        reference_inputs={"x": 0.0, "z": 0.0},
        numeric_fields={"x", "z"},
        weights={"x": 1.0},  # z unscored
    )
    by_id = {r.job.id: r for r in ranked}
    assert by_id[1].distance > 0.0  # z still contributed
    assert ranked[0].job.id == 2


def test_top_n_caps_results() -> None:
    jobs = [_job(i, {"t": float(i)}) for i in range(1, 11)]
    ranked = rank_jobs_by_similarity(
        jobs, reference_inputs={"t": 1.0}, numeric_fields={"t"}, top_n=3
    )
    assert len(ranked) == 3
    assert ranked[0].job.id == 1  # closest to t=1


def test_exclude_job_id_drops_the_seed() -> None:
    jobs = [_job(1, {"t": 5.0}), _job(2, {"t": 6.0})]
    ranked = rank_jobs_by_similarity(
        jobs,
        reference_inputs={"t": 5.0},
        numeric_fields={"t"},
        exclude_job_id=1,
    )
    assert [r.job.id for r in ranked] == [2]


def test_pure_categorical_match_is_full_closeness() -> None:
    # No numeric axis to discriminate on → every gated row reads as a
    # full (100%) match, ordered newest-id first.
    jobs = [_job(1, {"mat": "steel"}), _job(2, {"mat": "steel"})]
    ranked = rank_jobs_by_similarity(jobs, reference_inputs={"mat": "steel"}, numeric_fields=set())
    assert {r.job.id for r in ranked} == {1, 2}
    assert all(r.closeness_pct == 100.0 and r.distance == 0.0 for r in ranked)
    assert [r.job.id for r in ranked] == [2, 1]  # tie-break newest id first
