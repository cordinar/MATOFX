"""``check_cross_field`` must give the exact cross-field verdict
``validate_inputs`` gives — same SumCheck + §F30 evaluator, same
plain-English messages, same don't-nag tolerance. The parametric-
studies generator depends on this equivalence (design §4 step 3).
"""

from __future__ import annotations

import pytest

from matofx.tools.descriptor import (
    CrossFieldConstraint,
    InputField,
    SumCheck,
    ToolDescriptor,
)
from matofx.tools.forms import FormValidationError, check_cross_field, validate_inputs


def _descriptor() -> ToolDescriptor:
    return ToolDescriptor(
        alias="demo",
        name="Demo",
        version="1.0.0",
        description="cross-field test",
        main_script="static/main.py",
        inputs=[
            InputField(
                name="a",
                label="A pct",
                type="float",
                minimum=0,
                maximum=100,
                sum_check=SumCheck(value=100.0, sum_parameters=["a", "b"]),
            ),
            InputField(name="b", label="B pct", type="float", minimum=0, maximum=100),
            InputField(name="lo", label="Low", type="float"),
            InputField(name="hi", label="High", type="float", required=False),
        ],
        cross_field_constraints=[
            CrossFieldConstraint(expression="lo <= hi", error_message="Low must be <= High."),
        ],
    )


def test_valid_point_yields_no_errors() -> None:
    errs = check_cross_field(_descriptor(), {"a": 40.0, "b": 60.0, "lo": 1.0, "hi": 2.0})
    assert errs == []


def test_sum_check_violation_reported() -> None:
    errs = check_cross_field(_descriptor(), {"a": 40.0, "b": 50.0, "lo": 1.0, "hi": 2.0})
    assert len(errs) == 1
    assert "add up to 100" in errs[0].message


def test_cross_field_constraint_violation_reported() -> None:
    errs = check_cross_field(_descriptor(), {"a": 50.0, "b": 50.0, "lo": 9.0, "hi": 2.0})
    assert [e.message for e in errs] == ["Low must be <= High."]


def test_missing_optional_participant_is_skipped_not_failed() -> None:
    # 'hi' is optional + absent: the constraint is skipped, not failed
    # (same tolerance validate_inputs uses).
    errs = check_cross_field(_descriptor(), {"a": 50.0, "b": 50.0, "lo": 9.0})
    assert errs == []


def test_parity_with_validate_inputs() -> None:
    desc = _descriptor()
    bad = {"a": 40.0, "b": 50.0, "lo": 1.0, "hi": 2.0}
    helper_msgs = sorted(e.message for e in check_cross_field(desc, bad))
    with pytest.raises(FormValidationError) as exc:
        validate_inputs(desc, bad)
    submit_msgs = sorted(e.message for e in exc.value.field_errors)
    assert helper_msgs == submit_msgs
