"""Unit tests for the Compare grid's field-animation loader.

:func:`matofx.ui.pages.compare.page._load_field_outputs` is the pure-ish
selector that decides which outputs are time-resolved ``field`` series
(and so need the parsed ``field-frames`` payload rather than a plain
preview blob). A regression here would silently drop the transient
animations back out of the comparison — the original bug.
"""

from __future__ import annotations

import asyncio
from typing import Any

from matofx.ui.pages.compare.page import _load_field_outputs, _LoadedJob


class _FakeClient:
    """Minimal stand-in exposing only ``get_job_field_frames``."""

    def __init__(self, frames_by: dict[tuple[int, str], Any]) -> None:
        self._frames_by = frames_by
        self.calls: list[tuple[int, str]] = []

    async def get_job_field_frames(self, job_id: int, output: str) -> Any:
        self.calls.append((job_id, output))
        result = self._frames_by[(job_id, output)]
        if isinstance(result, Exception):
            raise result
        return result


def _bundle(job_id: int, descriptor: dict[str, Any], doc: dict[str, Any]) -> _LoadedJob:
    return _LoadedJob(
        job={"id": job_id, "name": f"run {job_id}", "tool_alias": "transient_plate"},
        descriptor=descriptor,
        output_document=doc,
    )


_DESCRIPTOR = {
    "alias": "transient_plate",
    "name": "Transient Plate",
    "outputs": [
        {"name": "t_max", "label": "Peak temperature", "type": "number"},
        {
            "name": "temperature_field",
            "label": "Temperature field (transient)",
            "type": "file",
            "file_type": "field",
        },
        {
            "name": "table",
            "label": "A CSV table",
            "type": "file",
            "file_type": "csv_table",
        },
    ],
}


def _doc(job_id: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "tool_alias": "transient_plate",
        "outputs": [
            {"name": "t_max", "type": "number", "value": 100.0 + job_id},
            {
                "name": "temperature_field",
                "type": "file",
                "filename": "outputs/temperature_0000.vtp",
            },
            {"name": "table", "type": "file", "filename": "outputs/table.csv"},
        ],
    }


def test_load_field_outputs_picks_only_field_typed_outputs() -> None:
    """Only the ``file_type='field'`` output is fetched; csv_table is skipped."""
    frames = {"frames": [[1.0, 2.0], [3.0, 4.0]], "has_geometry": False}
    client = _FakeClient(
        {(1, "temperature_field"): dict(frames), (2, "temperature_field"): dict(frames)}
    )
    bundles = [_bundle(1, _DESCRIPTOR, _doc(1)), _bundle(2, _DESCRIPTOR, _doc(2))]

    outputs = asyncio.run(_load_field_outputs(client, bundles))  # type: ignore[arg-type]

    # One field output per job, in job order; the csv_table never fetched.
    assert [o.job_id for o in outputs] == [1, 2]
    assert all(o.field_name == "temperature_field" for o in outputs)
    assert all(o.field_label == "Temperature field (transient)" for o in outputs)
    assert outputs[0].filename == "outputs/temperature_0000.vtp"
    assert client.calls == [(1, "temperature_field"), (2, "temperature_field")]


def test_load_field_outputs_drops_failed_fetch() -> None:
    """A field-frames error for one job is dropped, the rest still load."""
    frames = {"frames": [[1.0]], "has_geometry": False}
    client = _FakeClient(
        {
            (1, "temperature_field"): RuntimeError("boom"),
            (2, "temperature_field"): dict(frames),
        }
    )
    bundles = [_bundle(1, _DESCRIPTOR, _doc(1)), _bundle(2, _DESCRIPTOR, _doc(2))]

    outputs = asyncio.run(_load_field_outputs(client, bundles))  # type: ignore[arg-type]

    assert [o.job_id for o in outputs] == [2]


def test_load_field_outputs_empty_when_no_field_outputs() -> None:
    """A descriptor with no field output makes no field-frames calls."""
    descriptor = {
        "alias": "heat_transfer",
        "outputs": [{"name": "core_temperature", "type": "number"}],
    }
    doc = {
        "schema_version": 1,
        "tool_alias": "heat_transfer",
        "outputs": [{"name": "core_temperature", "type": "number", "value": 80.0}],
    }
    client = _FakeClient({})
    bundles = [_bundle(1, descriptor, doc)]

    outputs = asyncio.run(_load_field_outputs(client, bundles))  # type: ignore[arg-type]

    assert outputs == []
    assert client.calls == []
