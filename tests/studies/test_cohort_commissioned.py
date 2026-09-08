"""A commissioned study can be detached again (2026-09-02 regression).

**The defect.** ``auto_include_study_ids`` — the studies a cohort commissioned
to fill its own coverage gaps — was declared on ``CohortUpdateRequest`` and
missing from **both** ``_FILTER_FIELDS`` tuples, the router's and the
service's. The API accepted the field and dropped it on the floor, so nothing,
through any surface, could ever detach a study once commissioned.

Reported from the UI: a fixed cohort narrowed to one 20-run study still
resolved to 60 runs, because a 40-run commissioned study came back every time.
The detail page read *"20 run(s) picked, plus completed runs of 1 commissioned
study(ies)"* while the coverage panel counted 60, with nothing on screen to
reconcile them — and since all 60 runs shared one material, that axis was
recorded ``pinned`` and disappeared from the pairwise plots as well.

These are the pure-logic halves: the two lists that drifted, and the validator.
The database round-trips live in ``test_cohort_service.py`` beside the existing
commissioned-runs tests, which already have the fixtures for it.
"""

from __future__ import annotations

from typing import Any

import pytest

from matofx.api.routers.cohorts import _FILTER_FIELDS as ROUTER_FILTER_FIELDS
from matofx.api.schemas import CohortCreateRequest, CohortUpdateRequest
from matofx.studies.cohort.service import (
    _FILTER_FIELDS,
    CohortConfigError,
    _validate_study_ids,
)

# --- the two lists that drifted -------------------------------------------


def test_the_router_and_the_service_agree_on_the_filter_set() -> None:
    """The defect in one line: two hand-maintained copies of one list.

    ``auto_include_study_ids`` was on the request schema and in neither tuple,
    so it was accepted and discarded at both layers at once.
    """
    assert set(ROUTER_FILTER_FIELDS) == set(_FILTER_FIELDS)


def test_every_settable_field_on_the_update_schema_is_actually_applied() -> None:
    """The general form of the bug: a field the API accepts and nobody stores.

    Anything on ``CohortUpdateRequest`` that ``update_cohort`` does not handle
    by name has to be in ``_FILTER_FIELDS``, or it is silently ignored exactly
    as this one was. This catches the *next* field added to the schema and
    forgotten in the tuples, which is how this one happened.
    """
    handled_by_name = {
        "name",
        "description",
        "tolerated_tool_version_ids",
        "tolerated_version_bands",
        "auto_grow",
        "member_job_ids",
        "excluded_job_ids",
    }
    unhandled = set(CohortUpdateRequest.model_fields) - handled_by_name - set(_FILTER_FIELDS)
    assert not unhandled, f"accepted by the API and never stored: {sorted(unhandled)}"


def test_the_commissioned_field_is_in_the_filter_set() -> None:
    assert "auto_include_study_ids" in _FILTER_FIELDS


# --- validation ------------------------------------------------------------


def test_nothing_supplied_is_not_an_error() -> None:
    """A caller that never mentions the field must not be forced to."""
    _validate_study_ids(None, "auto_include_study_ids")


def test_an_empty_list_is_accepted_because_that_is_how_you_detach() -> None:
    """Rejecting ``[]`` would make "remove the last one" impossible, which is
    the whole point of the fix."""
    _validate_study_ids([], "auto_include_study_ids")


def test_real_ids_pass() -> None:
    _validate_study_ids([1, 7, 900], "auto_include_study_ids")


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("3", id="a-bare-string"),
        pytest.param({"id": 3}, id="a-mapping"),
        pytest.param(3, id="a-bare-int-not-a-list"),
    ],
)
def test_a_non_list_is_refused(bad: Any) -> None:
    with pytest.raises(CohortConfigError, match="list of study ids"):
        _validate_study_ids(bad, "auto_include_study_ids")


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param([0], id="zero"),
        pytest.param([-1], id="negative"),
        pytest.param(["4"], id="a-string-id"),
        pytest.param([None], id="a-null"),
        pytest.param([1, 2, "x"], id="one-bad-among-good"),
        pytest.param([True], id="a-bool-which-is-an-int-in-python"),
    ],
)
def test_junk_ids_are_refused(bad: Any) -> None:
    with pytest.raises(CohortConfigError, match="positive study ids"):
        _validate_study_ids(bad, "auto_include_study_ids")


def test_the_message_names_the_field_it_is_complaining_about() -> None:
    """Two fields share this validator; a message saying only "study ids"
    leaves the curator guessing which control is wrong."""
    with pytest.raises(CohortConfigError, match="auto_include_study_ids"):
        _validate_study_ids(["x"], "auto_include_study_ids")


def test_a_filter_the_create_body_lacks_does_not_break_creation() -> None:
    """``_FILTER_FIELDS`` is shared by create, preview and update, but not
    every filter exists on every body: a cohort cannot commission a study
    before it exists, so ``auto_include_study_ids`` is update-only.

    Adding it to the shared tuple broke *creation* with an ``AttributeError``
    until the create and preview paths learned to skip what the body does not
    carry. This pins which fields are update-only so the next one is a
    decision rather than a 500.
    """
    update_only = set(_FILTER_FIELDS) - set(CohortCreateRequest.model_fields)
    assert update_only == {"auto_include_study_ids"}


def test_the_create_body_has_no_filter_the_service_would_reject() -> None:
    """The other direction: a filter the create form offers and the service
    does not know is a silent drop at creation time."""
    creatable = set(CohortCreateRequest.model_fields) & set(CohortUpdateRequest.model_fields)
    handled = {
        "name",
        "description",
        "tool_alias",
        "tolerated_tool_version_ids",
        "tolerated_version_bands",
        "auto_grow",
        "member_job_ids",
        "excluded_job_ids",
    }
    assert not (creatable - handled - set(_FILTER_FIELDS))
