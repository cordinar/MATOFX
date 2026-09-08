"""Pure-function tests for ``compare.diff.compute_differing_keys``.

The diff helper has no NiceGUI / DB dependencies, so the whole surface
is testable as a plain function. These tests pin down the contract the
Compare page relies on: identical bundles produce no diff, missing
keys count as different, and dict / list ordering doesn't matter.
"""

from __future__ import annotations

from matofx.ui.pages.compare.diff import JobBundle, compute_differing_keys


def test_zero_or_one_bundle_has_no_diff() -> None:
    """With nothing to compare against, the diff is always empty."""
    assert compute_differing_keys([]) == set()
    assert (
        compute_differing_keys([JobBundle(job_id=1, inputs={"a": 1}, outputs={"b": 2})])
        == set()
    )


def test_identical_bundles_have_no_diff() -> None:
    a = JobBundle(job_id=1, inputs={"x": 1, "y": "z"}, outputs={"out": 42})
    b = JobBundle(job_id=2, inputs={"x": 1, "y": "z"}, outputs={"out": 42})
    assert compute_differing_keys([a, b]) == set()


def test_differing_input_value_is_flagged() -> None:
    a = JobBundle(job_id=1, inputs={"temperature": 100}, outputs={})
    b = JobBundle(job_id=2, inputs={"temperature": 200}, outputs={})
    assert compute_differing_keys([a, b]) == {"temperature"}


def test_differing_output_value_is_flagged() -> None:
    a = JobBundle(job_id=1, inputs={}, outputs={"yield": 0.85})
    b = JobBundle(job_id=2, inputs={}, outputs={"yield": 0.91})
    assert compute_differing_keys([a, b]) == {"yield"}


def test_missing_key_in_one_bundle_is_flagged() -> None:
    """Present-only-in-one is a difference, even if the value is None."""
    a = JobBundle(job_id=1, inputs={"shared": 1, "only_in_a": None}, outputs={})
    b = JobBundle(job_id=2, inputs={"shared": 1}, outputs={})
    assert compute_differing_keys([a, b]) == {"only_in_a"}


def test_dict_ordering_does_not_create_false_positives() -> None:
    """Inputs whose nested dicts have different key order should compare equal."""
    a = JobBundle(
        job_id=1,
        inputs={"composition": {"Fe": 0.7, "C": 0.3}},
        outputs={},
    )
    b = JobBundle(
        job_id=2,
        inputs={"composition": {"C": 0.3, "Fe": 0.7}},
        outputs={},
    )
    assert compute_differing_keys([a, b]) == set()


def test_list_element_difference_is_flagged() -> None:
    a = JobBundle(job_id=1, inputs={"steps": [1, 2, 3]}, outputs={})
    b = JobBundle(job_id=2, inputs={"steps": [1, 2, 4]}, outputs={})
    assert compute_differing_keys([a, b]) == {"steps"}


def test_three_bundles_with_one_outlier() -> None:
    """A key only differs when at least one bundle disagrees with the others."""
    a = JobBundle(job_id=1, inputs={"x": 1, "y": 9}, outputs={})
    b = JobBundle(job_id=2, inputs={"x": 1, "y": 9}, outputs={})
    c = JobBundle(job_id=3, inputs={"x": 1, "y": 7}, outputs={})
    assert compute_differing_keys([a, b, c]) == {"y"}


def test_inputs_and_outputs_are_unioned() -> None:
    """A name that's an input in one bundle and an output in another still diffs."""
    a = JobBundle(job_id=1, inputs={"temperature": 100}, outputs={})
    b = JobBundle(job_id=2, inputs={}, outputs={"temperature": 100})
    # Both bundles report the same value for ``temperature`` (just on
    # different sides of the dict) — diff should be empty.
    assert compute_differing_keys([a, b]) == set()


def test_value_none_and_missing_distinguished() -> None:
    """An explicit None is not the same as a missing key."""
    a = JobBundle(job_id=1, inputs={"k": None}, outputs={})
    b = JobBundle(job_id=2, inputs={}, outputs={})
    assert compute_differing_keys([a, b]) == {"k"}
