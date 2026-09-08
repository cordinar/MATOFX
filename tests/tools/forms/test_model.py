"""Tests for ``matofx.tools.forms.model``.

Covers the must-be-exhaustively-tested surface from §H40:

- Per descriptor type: model construction + happy-path + boundary failures.
- Cache key behaviour (per ``(alias, version)``) per §F30 Q5.
- Plain-English error messages (§F30 clarifier 3) for every Pydantic
  error code we map.
- Sum-check post-validation (positive, negative, dedupe, tolerance,
  missing participant).
- Unknown / extra fields rejected (``extra="forbid"``).

Property-based coverage with Hypothesis is deferred until the dev
dep lands (§H40 mentions Hypothesis but it isn't currently in the
project's lock file). The unit cases below give us the same
edge-case shape; if a regression slips through we can promote the
relevant case to property-based later.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from matofx.tools.descriptor import InputField, SumCheck, ToolDescriptor
from matofx.tools.forms import (
    FieldError,
    FormValidationError,
    build_input_model,
    clear_cache,
    validate_inputs,
)


@pytest.fixture(autouse=True)
def _isolate_model_cache() -> Iterator[None]:
    """Each test starts with an empty model cache so collisions are impossible."""
    clear_cache()
    yield
    clear_cache()


# --- helpers --------------------------------------------------------------


def _descriptor(*, alias: str = "demo", version: str = "1.0.0", **fields: Any) -> ToolDescriptor:
    """Build a minimal ``ToolDescriptor`` with sensible defaults."""
    base: dict[str, Any] = {
        "alias": alias,
        "name": "Demo",
        "version": version,
        "description": "Demo.",
        "main_script": "main.py",
    }
    base.update(fields)
    return ToolDescriptor(**base)


def _first_error(payload: dict[str, Any], descriptor: ToolDescriptor) -> FieldError:
    """Run validation expecting it to fail and return the first FieldError."""
    with pytest.raises(FormValidationError) as exc_info:
        validate_inputs(descriptor, payload)
    assert exc_info.value.field_errors
    return exc_info.value.field_errors[0]


# --- build_input_model ----------------------------------------------------


def test_model_name_is_descriptor_keyed_and_stable() -> None:
    d = _descriptor(
        alias="heat_transfer",
        version="1.0.0",
        inputs=[InputField(name="x", label="X", type="float")],
    )
    model = build_input_model(d)
    assert model.__name__ == "JobInputs_heat_transfer_1_0_0"
    assert sorted(model.model_fields.keys()) == ["x"]


def test_cache_returns_same_class_for_same_alias_version() -> None:
    d = _descriptor(inputs=[InputField(name="x", label="X", type="float")])
    assert build_input_model(d) is build_input_model(d)


def test_cache_differentiates_between_versions() -> None:
    d1 = _descriptor(version="1.0.0", inputs=[InputField(name="x", label="X", type="float")])
    d2 = _descriptor(version="2.0.0", inputs=[InputField(name="x", label="X", type="float")])
    assert build_input_model(d1) is not build_input_model(d2)


def test_clear_cache_forces_rebuild() -> None:
    d = _descriptor(inputs=[InputField(name="x", label="X", type="float")])
    first = build_input_model(d)
    clear_cache()
    assert build_input_model(d) is not first


# --- happy-path validation per descriptor type ---------------------------


def test_string_input_passes_through() -> None:
    d = _descriptor(inputs=[InputField(name="comment", label="Comment", type="string")])
    assert validate_inputs(d, {"comment": "hello"}) == {"comment": "hello"}


def test_integer_input_coerces_and_round_trips() -> None:
    d = _descriptor(
        inputs=[
            InputField(name="n", label="N", type="integer", minimum=0, maximum=10, default=5),
        ]
    )
    assert validate_inputs(d, {"n": 7}) == {"n": 7}


def test_float_input_accepts_int_and_float_literals() -> None:
    d = _descriptor(
        inputs=[
            InputField(
                name="t", label="T", type="float", minimum=0, maximum=300, default=25, unit="degC"
            )
        ]
    )
    assert validate_inputs(d, {"t": 100})["t"] == 100.0
    assert validate_inputs(d, {"t": 100.5})["t"] == 100.5


def test_boolean_input_round_trips() -> None:
    d = _descriptor(inputs=[InputField(name="flag", label="Flag", type="boolean", default=True)])
    assert validate_inputs(d, {"flag": False}) == {"flag": False}


def test_select_input_accepts_listed_option_only() -> None:
    d = _descriptor(
        inputs=[
            InputField(
                name="material",
                label="Material",
                type="select",
                options=["steel", "aluminium"],
                default="steel",
            )
        ]
    )
    assert validate_inputs(d, {"material": "aluminium"}) == {"material": "aluminium"}


def test_multiselect_input_accepts_subset_of_options() -> None:
    d = _descriptor(
        inputs=[
            InputField(
                name="tags",
                label="Tags",
                type="multiselect",
                options=["hot", "cold", "wet", "dry"],
                default=[],
                required=False,
            )
        ]
    )
    assert validate_inputs(d, {"tags": ["hot", "dry"]}) == {"tags": ["hot", "dry"]}


def test_file_input_accepts_path_string_when_no_extension_constraint() -> None:
    d = _descriptor(
        inputs=[InputField(name="model", label="Model", type="file", required=False)],
    )
    assert validate_inputs(d, {"model": "/jobs/1/inputs/anything.bin"}) == {
        "model": "/jobs/1/inputs/anything.bin"
    }


def test_file_input_with_extension_allow_list_accepts_case_insensitively() -> None:
    d = _descriptor(
        inputs=[
            InputField(
                name="cad",
                label="CAD",
                type="file",
                accepted_file_types=["step", "stl"],
                required=True,
            )
        ]
    )
    assert validate_inputs(d, {"cad": "/jobs/1/inputs/part.STEP"}) == {
        "cad": "/jobs/1/inputs/part.STEP"
    }


def test_file_input_accepts_library_sentinel_despite_extension_allow_list() -> None:
    # A "Pick from library" selection (ADR-0006 §6) carries the version
    # sentinel, not a path — the extension validator must let it through
    # (its type was vetted at pick time; bytes resolved at submit).
    from matofx.library.refs import build_library_ref  # noqa: PLC0415

    d = _descriptor(
        inputs=[
            InputField(
                name="cad",
                label="CAD",
                type="file",
                accepted_file_types=["step", "stl"],
                required=True,
            )
        ]
    )
    ref = build_library_ref(42)
    assert validate_inputs(d, {"cad": ref}) == {"cad": ref}


def test_optional_field_without_value_resolves_to_none() -> None:
    d = _descriptor(inputs=[InputField(name="n", label="N", type="float", required=False)])
    assert validate_inputs(d, {}) == {"n": None}


def test_required_field_with_default_uses_default_when_omitted() -> None:
    # The legacy app's "required + default" semantics: the field always
    # has *some* value at submit time, with the default pre-populated.
    d = _descriptor(
        inputs=[
            InputField(
                name="t", label="T", type="float", default=25, minimum=0, maximum=300, required=True
            )
        ]
    )
    assert validate_inputs(d, {})["t"] == 25.0


# --- per-field plain-English error messages ------------------------------


def test_missing_required_field_says_required() -> None:
    d = _descriptor(inputs=[InputField(name="t", label="Temperature", type="float", required=True)])
    err = _first_error({}, d)
    assert err.name == "t"
    assert err.message == "Temperature is required."


def test_integer_failure_says_whole_number() -> None:
    d = _descriptor(inputs=[InputField(name="n", label="N", type="integer")])
    err = _first_error({"n": 7.5}, d)
    assert err.message == "N must be a whole number."


def test_float_parse_failure_says_must_be_number() -> None:
    d = _descriptor(inputs=[InputField(name="t", label="Temperature", type="float")])
    err = _first_error({"t": "not-a-number"}, d)
    assert err.message == "Temperature must be a number."


def test_boolean_parse_failure_says_yes_or_no() -> None:
    d = _descriptor(inputs=[InputField(name="flag", label="Flag", type="boolean")])
    err = _first_error({"flag": "maybe"}, d)
    assert err.message == "Flag must be Yes or No."


def test_string_type_failure_says_must_be_text() -> None:
    d = _descriptor(inputs=[InputField(name="comment", label="Comment", type="string")])
    err = _first_error({"comment": ["a", "list"]}, d)
    assert err.message == "Comment must be text."


def test_below_minimum_says_cant_be_less_than_with_unit() -> None:
    d = _descriptor(
        inputs=[InputField(name="t", label="Temperature", type="float", unit="degC", minimum=0)]
    )
    err = _first_error({"t": -1.0}, d)
    assert err.message == "Temperature can't be less than 0.0 degC."


def test_above_maximum_says_cant_be_more_than_with_unit() -> None:
    d = _descriptor(
        inputs=[InputField(name="t", label="Temperature", type="float", unit="degC", maximum=300)]
    )
    err = _first_error({"t": 999.0}, d)
    assert err.message == "Temperature can't be more than 300.0 degC."


def test_below_minimum_without_unit_omits_trailing_unit() -> None:
    d = _descriptor(inputs=[InputField(name="n", label="Count", type="integer", minimum=1)])
    err = _first_error({"n": 0}, d)
    assert err.message == "Count can't be less than 1."


def test_select_wrong_option_lists_choices() -> None:
    d = _descriptor(
        inputs=[
            InputField(
                name="material", label="Material", type="select", options=["steel", "aluminium"]
            )
        ]
    )
    err = _first_error({"material": "wood"}, d)
    assert "Material must be one of" in err.message
    assert "'steel'" in err.message
    assert "'aluminium'" in err.message


def test_multiselect_wrong_option_lists_offending_and_choices() -> None:
    d = _descriptor(
        inputs=[
            InputField(
                name="tags",
                label="Tags",
                type="multiselect",
                options=["hot", "cold"],
                required=False,
                default=[],
            )
        ]
    )
    err = _first_error({"tags": ["hot", "spicy"]}, d)
    assert "Tags contains values not in the allowed list" in err.message
    assert "'spicy'" in err.message
    assert "'hot'" in err.message


def test_file_extension_failure_lists_allowed_extensions() -> None:
    d = _descriptor(
        inputs=[
            InputField(
                name="cad",
                label="CAD",
                type="file",
                accepted_file_types=["step", "stl"],
            )
        ]
    )
    err = _first_error({"cad": "/jobs/1/x.txt"}, d)
    assert "CAD must be a file with one of these extensions" in err.message
    assert "step" in err.message
    assert "stl" in err.message


def test_extra_field_rejected_by_name() -> None:
    d = _descriptor(inputs=[InputField(name="t", label="T", type="float", default=0)])
    err = _first_error({"t": 1, "uninvited": "x"}, d)
    assert err.name == "uninvited"
    assert "isn't a recognised input" in err.message


# --- sum-check post-validation -------------------------------------------


def _percentages_descriptor() -> ToolDescriptor:
    """Three percentages that must total 100 — the §F30 cross-field example."""
    return _descriptor(
        inputs=[
            InputField(
                name="a",
                label="A",
                type="float",
                unit="%",
                minimum=0,
                maximum=100,
                default=33,
                sum_check=SumCheck(value=100, sum_parameters=["a", "b", "c"]),
            ),
            InputField(
                name="b", label="B", type="float", unit="%", minimum=0, maximum=100, default=33
            ),
            InputField(
                name="c", label="C", type="float", unit="%", minimum=0, maximum=100, default=34
            ),
        ]
    )


def test_sum_check_passes_when_total_matches() -> None:
    d = _percentages_descriptor()
    assert validate_inputs(d, {"a": 30, "b": 30, "c": 40})["a"] == 30.0


def test_sum_check_passes_within_tolerance() -> None:
    # Floating-point noise from summing 33.33 + 33.33 + 33.34 produces
    # tiny discrepancies that should NOT trip the constraint.
    d = _percentages_descriptor()
    assert validate_inputs(d, {"a": 33.33, "b": 33.33, "c": 33.34})["a"] == 33.33


def test_sum_check_fails_with_clear_message_anchored_to_first_param() -> None:
    d = _percentages_descriptor()
    err = _first_error({"a": 30, "b": 30, "c": 50}, d)
    assert err.name == "a"  # first listed parameter is the anchor
    assert err.message == "A, B, C must add up to 100."
    assert err.sum_parameters == ("a", "b", "c")


def test_sum_check_deduplicated_when_declared_on_multiple_fields() -> None:
    same_constraint = SumCheck(value=100, sum_parameters=["a", "b"])
    d = _descriptor(
        inputs=[
            InputField(
                name="a",
                label="A",
                type="float",
                unit="%",
                minimum=0,
                maximum=100,
                default=50,
                sum_check=same_constraint,
            ),
            InputField(
                name="b",
                label="B",
                type="float",
                unit="%",
                minimum=0,
                maximum=100,
                default=50,
                sum_check=same_constraint,
            ),
        ]
    )
    with pytest.raises(FormValidationError) as exc_info:
        validate_inputs(d, {"a": 40, "b": 50})
    # Despite the constraint being declared on both inputs we only
    # surface one error — duplicates would noise up the form UX.
    assert len(exc_info.value.field_errors) == 1


def test_sum_check_skipped_when_a_required_participant_is_missing() -> None:
    # When a required participant is missing the user already sees the
    # "X is required." error from per-field validation; we should not
    # double up with a sum-check failure for the same submission.
    # Note: this descriptor explicitly omits defaults on the participants
    # so Pydantic raises "missing" rather than auto-filling them.
    no_default_constraint = SumCheck(value=100, sum_parameters=["a", "b", "c"])
    d = _descriptor(
        inputs=[
            InputField(
                name="a",
                label="A",
                type="float",
                unit="%",
                minimum=0,
                maximum=100,
                required=True,
                sum_check=no_default_constraint,
            ),
            InputField(
                name="b",
                label="B",
                type="float",
                unit="%",
                minimum=0,
                maximum=100,
                required=True,
            ),
            InputField(
                name="c",
                label="C",
                type="float",
                unit="%",
                minimum=0,
                maximum=100,
                required=True,
            ),
        ]
    )
    with pytest.raises(FormValidationError) as exc_info:
        validate_inputs(d, {"a": 30, "b": 30})
    # Pydantic flagged C as missing; the sum-check stayed silent
    # because it can't evaluate without C.
    assert any(fe.message == "C is required." for fe in exc_info.value.field_errors)
    assert all(fe.sum_parameters == () for fe in exc_info.value.field_errors)


def test_sum_check_target_renders_integer_targets_without_decimals() -> None:
    # The target ``100`` should appear in the message as "100" (not
    # "100.0") — the message ends with ".", which is sentence
    # punctuation, not a decimal separator.
    d = _percentages_descriptor()
    err = _first_error({"a": 30, "b": 30, "c": 50}, d)
    assert "100.0" not in err.message
    assert err.message.endswith("up to 100.")


# --- model_dump round-trip ------------------------------------------------


def test_validate_inputs_returns_plain_python_dict_not_pydantic_model() -> None:
    # The submit endpoint writes this to ``inputs.json`` — has to be a
    # plain dict (json-serialisable) of values.
    d = _descriptor(
        inputs=[
            InputField(name="t", label="T", type="float", default=25),
            InputField(name="flag", label="Flag", type="boolean", default=False),
        ]
    )
    result = validate_inputs(d, {"t": 100, "flag": True})
    assert isinstance(result, dict)
    assert result == {"t": 100.0, "flag": True}
