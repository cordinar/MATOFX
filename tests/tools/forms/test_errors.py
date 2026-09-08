"""Tests for ``matofx.tools.forms.errors``."""

from __future__ import annotations

import pytest

from matofx.tools.forms.errors import FieldError, FormValidationError


def test_field_error_is_frozen_and_carries_optional_sum_parameters() -> None:
    fe = FieldError(name="x", label="X", message="Bad")
    assert fe.name == "x"
    assert fe.label == "X"
    assert fe.message == "Bad"
    assert fe.sum_parameters == ()
    with pytest.raises(AttributeError):
        fe.name = "y"  # type: ignore[misc]


def test_form_validation_error_single_summary_uses_first_message() -> None:
    exc = FormValidationError([FieldError(name="x", label="X", message="X is required.")])
    assert str(exc) == "X is required."


def test_form_validation_error_multiple_summary_appends_count() -> None:
    exc = FormValidationError(
        [
            FieldError(name="a", label="A", message="A is required."),
            FieldError(name="b", label="B", message="B is required."),
            FieldError(name="c", label="C", message="C must be a number."),
        ]
    )
    # Summary leads with the first error, then mentions how many more there are
    # so a log line is informative without leaking values from later fields.
    assert "A is required." in str(exc)
    assert "2 more" in str(exc)


def test_form_validation_error_refuses_empty_list() -> None:
    with pytest.raises(ValueError, match="at least one"):
        FormValidationError([])


def test_by_name_uses_first_when_duplicates_exist() -> None:
    first = FieldError(name="a", label="A", message="first")
    second = FieldError(name="a", label="A", message="second")
    exc = FormValidationError([first, second])
    assert exc.by_name()["a"] is first
