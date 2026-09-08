"""The builder's payload must fit the endpoint it is sent to (2026-09-02).

``_gather_filters`` feeds **three** endpoints — preview, create and update —
and all three bodies forbid unknown fields. So a key that exists on only one of
those schemas cannot live in the shared dict.

It happened twice. First when the source filters (``source_kinds`` and friends)
were added to ``CohortCreateRequest`` instead of ``CohortUpdateRequest`` and
every preview 422'd; then again when ``auto_include_study_ids`` — an
update-only field, because a cohort cannot commission a study before it exists
— was put into ``_gather_filters`` and every preview of an edited cohort failed
with ``extra_forbidden``.

Both were reported from the UI rather than caught here, because nothing tested
the payload against the schemas that receive it. This does.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from matofx.api.schemas import (
    CohortCreateRequest,
    CohortPreviewRequest,
    CohortUpdateRequest,
)
from matofx.ui.pages.cohorts import _CohortBuilder


def _bare_form() -> Any:
    """A stand-in carrying only the widgets ``_gather_filters`` reads.

    Deliberately not a real builder: the point is the shape of the payload,
    which must not depend on a browser, a client or a rendered page.
    """
    return SimpleNamespace(
        range_inputs={},
        after_in=None,
        before_in=None,
        owner_sel=None,
        source_sel=None,
        parametric_sel=None,
        optimisation_sel=None,
    )


def _shared_keys() -> set[str]:
    return set(_CohortBuilder._gather_filters(_bare_form()))


def test_the_shared_payload_is_not_empty() -> None:
    """Guards the test itself: a helper that silently returned ``{}`` would
    make every assertion below vacuous."""
    assert _shared_keys()


@pytest.mark.parametrize(
    "schema",
    [
        pytest.param(CohortPreviewRequest, id="preview"),
        pytest.param(CohortCreateRequest, id="create"),
        pytest.param(CohortUpdateRequest, id="update"),
    ],
)
def test_every_shared_key_is_accepted_by_every_endpoint(schema: Any) -> None:
    """The bug in one assertion. All three bodies forbid extras, so a key in
    the shared dict that one of them lacks is a 422 the moment a curator
    touches that path."""
    missing = _shared_keys() - set(schema.model_fields)
    assert not missing, f"{schema.__name__} would reject {sorted(missing)}"


def test_the_update_only_field_stays_out_of_the_shared_payload() -> None:
    """``auto_include_study_ids`` rides on the update call alone."""
    assert "auto_include_study_ids" not in _shared_keys()


def test_the_commissioned_payload_is_empty_when_the_control_never_rendered() -> None:
    """A form that did not show the field must not send it — otherwise an
    unrelated save (a rename) detaches every commissioned study."""
    assert _CohortBuilder._commissioned_payload(SimpleNamespace()) == {}


def test_the_commissioned_payload_carries_the_selection_when_it_did() -> None:
    form = SimpleNamespace(commissioned_sel=SimpleNamespace(value=[27, 31]))
    assert _CohortBuilder._commissioned_payload(form) == {"auto_include_study_ids": [27, 31]}


def test_an_emptied_selection_still_travels() -> None:
    """Unticking the last study is exactly how you detach, so ``[]`` has to be
    sent rather than omitted — the service turns it into NULL."""
    form = SimpleNamespace(commissioned_sel=SimpleNamespace(value=[]))
    assert _CohortBuilder._commissioned_payload(form) == {"auto_include_study_ids": []}


def test_a_none_selection_reads_as_empty_not_a_crash() -> None:
    """NiceGUI hands back ``None`` for a multiselect nobody has touched."""
    form = SimpleNamespace(commissioned_sel=SimpleNamespace(value=None))
    assert _CohortBuilder._commissioned_payload(form) == {"auto_include_study_ids": []}
