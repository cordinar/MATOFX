"""Tests for the execution-backend JSON contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from matofx.tools import (
    INPUTS_DOCUMENT_SCHEMA_VERSION,
    OUTPUTS_DOCUMENT_SCHEMA_VERSION,
    HeartbeatRecord,
    HeartbeatState,
    InputsDocument,
    InputValue,
    OutputsDocument,
    OutputValue,
)


def test_inputs_document_round_trip() -> None:
    doc = InputsDocument(
        job_id=42,
        tool_alias="demo",
        tool_version="1.0.0",
        inputs=[
            InputValue(name="temp", label="Temperature", type="float", unit="K", value=300.0),
            InputValue(name="material", label="Material", type="string", value="steel"),
        ],
    )
    payload = doc.model_dump(mode="json")
    parsed = InputsDocument.model_validate(payload)
    assert parsed == doc
    assert payload["schema_version"] == INPUTS_DOCUMENT_SCHEMA_VERSION


def test_outputs_document_round_trip_with_file() -> None:
    doc = OutputsDocument(
        tool_alias="demo",
        outputs=[
            OutputValue(name="yield", label="Yield Stress", type="number", unit="MPa", value=420),
            OutputValue(
                name="report",
                label="Report",
                type="file",
                filename="outputs/report.html",
            ),
        ],
    )
    payload = doc.model_dump(mode="json")
    parsed = OutputsDocument.model_validate(payload)
    assert parsed == doc
    assert payload["schema_version"] == OUTPUTS_DOCUMENT_SCHEMA_VERSION


def test_inputs_document_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        InputsDocument.model_validate(
            {
                "schema_version": 1,
                "job_id": 1,
                "tool_alias": "demo",
                "tool_version": "1.0",
                "inputs": [],
                "rogue_field": True,
            }
        )


def test_input_value_rejects_blank_name() -> None:
    with pytest.raises(ValidationError):
        InputValue(name="", label="x", type="string")


def test_heartbeat_record_running_minimal() -> None:
    record = HeartbeatRecord(state="running", started_at="2026-05-04T10:00:00+00:00")
    assert record.state == "running"
    assert record.exit_code is None


def test_heartbeat_record_failed_carries_failure_reason() -> None:
    record = HeartbeatRecord(
        state="failed",
        started_at="2026-05-04T10:00:00+00:00",
        ended_at="2026-05-04T10:00:05+00:00",
        exit_code=1,
        failure_reason="Tool exited with non-zero status 1.",
    )
    assert record.failure_reason == "Tool exited with non-zero status 1."


def test_heartbeat_state_enum_matches_record_literal() -> None:
    """Each enum value must be a legal ``state`` literal."""
    for member in HeartbeatState:
        record = HeartbeatRecord(state=member.value, started_at="2026-05-04T10:00:00+00:00")
        assert record.state == member.value
