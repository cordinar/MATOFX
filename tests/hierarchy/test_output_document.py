"""Tests for the ``_read_output_document`` helper in ``hierarchy.service``.

The helper backs :meth:`JobService.get_output_document`. The malformed-
file and missing-file branches are exercised here directly because
they don't need a full job lifecycle — just a temp directory and a
file. The happy path is covered by the lifecycle test in
``tests/api/test_jobs.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from matofx.hierarchy.service import _read_output_document


@pytest.mark.asyncio
async def test_missing_file_returns_empty_document(tmp_path: Path) -> None:
    """No outputs/outputs.json on disk → empty doc, no exception.

    Compare's grid relies on this behaviour to render rows for jobs
    whose runner never wrote the document; raising would 500 the whole
    page over a single bad job.
    """
    doc = await _read_output_document(
        mount_path=str(tmp_path),
        working_dir_relative="job_0000001",
        tool_alias="demo_tool",
    )
    assert doc.tool_alias == "demo_tool"
    assert doc.outputs == []


@pytest.mark.asyncio
async def test_malformed_json_returns_empty_document(tmp_path: Path) -> None:
    """A truncated / non-JSON outputs.json resolves to an empty doc."""
    wd = tmp_path / "job_0000002" / "outputs"
    wd.mkdir(parents=True)
    (wd / "outputs.json").write_text("{ this isn't valid json", encoding="utf-8")

    doc = await _read_output_document(
        mount_path=str(tmp_path),
        working_dir_relative="job_0000002",
        tool_alias="demo_tool",
    )
    assert doc.tool_alias == "demo_tool"
    assert doc.outputs == []


@pytest.mark.asyncio
async def test_schema_mismatch_returns_empty_document(tmp_path: Path) -> None:
    """JSON that parses but doesn't fit OutputsDocument falls back gracefully.

    For example an array at the root, or an object missing required
    fields. The helper logs and returns the empty doc rather than
    propagating Pydantic's ValidationError up to a 500.
    """
    wd = tmp_path / "job_0000003" / "outputs"
    wd.mkdir(parents=True)
    (wd / "outputs.json").write_text('["not", "an", "outputs", "doc"]', encoding="utf-8")

    doc = await _read_output_document(
        mount_path=str(tmp_path),
        working_dir_relative="job_0000003",
        tool_alias="demo_tool",
    )
    assert doc.outputs == []


@pytest.mark.asyncio
async def test_well_formed_document_round_trips(tmp_path: Path) -> None:
    """A valid outputs.json round-trips through the helper unchanged."""
    import json  # noqa: PLC0415

    payload = {
        "schema_version": 1,
        "tool_alias": "demo_tool",
        "outputs": [
            {
                "name": "thermal_conductivity",
                "label": "Thermal conductivity",
                "type": "number",
                "unit": "W/mK",
                "value": 0.675,
            },
            {
                "name": "report",
                "label": "Tool report",
                "type": "file",
                "filename": "report.html",
            },
        ],
    }
    wd = tmp_path / "job_0000004" / "outputs"
    wd.mkdir(parents=True)
    (wd / "outputs.json").write_text(json.dumps(payload), encoding="utf-8")

    doc = await _read_output_document(
        mount_path=str(tmp_path),
        working_dir_relative="job_0000004",
        tool_alias="demo_tool",
    )
    assert doc.tool_alias == "demo_tool"
    by_name = {o.name: o for o in doc.outputs}
    assert by_name["thermal_conductivity"].value == 0.675
    assert by_name["report"].filename == "report.html"
