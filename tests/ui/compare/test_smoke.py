"""End-to-end smoke test for the Compare diff against a real descriptor.

Loads the ``heat_transfer`` descriptor, fakes three job submissions
that overlap on most inputs but disagree on a couple, and asserts the
diff helper highlights exactly those keys. Catches regressions where
the data-flattening helpers in :mod:`compare.page` lose a key on the
way from output document → ``JobBundle`` — the unit tests in
``test_diff.py`` cover the diff logic itself, but they don't exercise
the realistic output-document shape produced by the runner.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from matofx.ui.pages.compare.diff import JobBundle, compute_differing_keys
from matofx.ui.pages.compare.page import (
    _flatten_output_filenames,
    _flatten_output_values,
)

_DESCRIPTOR_PATH = (
    Path(__file__).resolve().parents[3] / "example_tools" / "heat_transfer" / "descriptor.toml"
)


def _load_descriptor() -> dict:
    with _DESCRIPTOR_PATH.open("rb") as fh:
        return tomllib.load(fh)


def test_smoke_diff_against_heat_transfer_descriptor() -> None:
    """Three jobs that share most inputs but vary one — diff highlights it."""
    descriptor = _load_descriptor()
    # Sanity check: the descriptor we're driving against is the one
    # whose group structure the renderer assumes.
    assert descriptor["alias"] == "heat_transfer"
    assert descriptor.get("inputs"), "descriptor must declare inputs"

    base_inputs = {
        "object_temperature": 25.0,
        "width": 0.2,
        "height": 0.2,
    }
    docs = [
        {
            "schema_version": 1,
            "tool_alias": "heat_transfer",
            "outputs": [
                {
                    "name": "core_temperature",
                    "label": "Core temperature",
                    "type": "number",
                    "unit": "degC",
                    "value": 80.0,
                },
                {
                    "name": "report",
                    "label": "Report",
                    "type": "file",
                    "filename": "report.html",
                },
            ],
        },
        {
            "schema_version": 1,
            "tool_alias": "heat_transfer",
            "outputs": [
                {
                    "name": "core_temperature",
                    "label": "Core temperature",
                    "type": "number",
                    "unit": "degC",
                    "value": 95.0,
                },
                {
                    "name": "report",
                    "label": "Report",
                    "type": "file",
                    "filename": "report.html",
                },
            ],
        },
        {
            "schema_version": 1,
            "tool_alias": "heat_transfer",
            "outputs": [
                {
                    "name": "core_temperature",
                    "label": "Core temperature",
                    "type": "number",
                    "unit": "degC",
                    "value": 80.0,
                },
                {
                    "name": "report",
                    "label": "Report",
                    "type": "file",
                    "filename": "report.html",
                },
            ],
        },
    ]
    # Job 2 ran with the same inputs except a different starting temperature.
    inputs_per_job = [
        dict(base_inputs),
        dict(base_inputs, object_temperature=40.0),
        dict(base_inputs),
    ]

    bundles = [
        JobBundle(
            job_id=1000 + i,
            inputs=inputs_per_job[i],
            outputs=_flatten_output_values(docs[i]),
        )
        for i in range(3)
    ]
    differing = compute_differing_keys(bundles)
    # The varying input + the varying output should both be flagged.
    assert "object_temperature" in differing
    assert "core_temperature" in differing
    # Identical fields don't show up.
    assert "width" not in differing
    assert "height" not in differing
    # File outputs whose filenames match across jobs aren't flagged.
    assert "report" not in differing


def test_flatten_helpers_separate_values_from_filenames() -> None:
    """Sanity check: filenames live in the filenames map, not the values map."""
    doc = {
        "schema_version": 1,
        "tool_alias": "heat_transfer",
        "outputs": [
            {
                "name": "core_temperature",
                "label": "Core temperature",
                "type": "number",
                "value": 80.0,
            },
            {
                "name": "report",
                "label": "Report",
                "type": "file",
                "filename": "report.html",
            },
        ],
    }
    values = _flatten_output_values(doc)
    filenames = _flatten_output_filenames(doc)
    # Scalar value flows through to the values map.
    assert values["core_temperature"] == 80.0
    # File output: the filename ends up in *both* maps — diffing the
    # filename is meaningful (renamed report = different output) but
    # the renderer also needs the filename for the "open" link.
    assert values["report"] == "report.html"
    assert filenames["report"] == "report.html"
