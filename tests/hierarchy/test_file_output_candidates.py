"""Unit tests for the cross-tool file-output picker helpers.

The :meth:`JobService.list_file_output_candidates` method (powering the
file-input widget's "use a file from another job" mode) is assembled from
four pure module helpers. These exercise the filtering, stat, and row-shape
logic without a DB/session — the service method just glues them to
``list_visible_to_user`` + batch lookups.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from matofx.hierarchy.service import (
    _assemble_output_candidate_rows,
    _collect_output_resolution_work,
    _job_file_output_specs,
    _resolve_existing_output_files,
)


def _descriptor(outputs: list[dict[str, Any]], *, name: str = "Thermal Soak") -> dict[str, Any]:
    return {"name": name, "outputs": outputs}


def test_file_output_specs_keeps_only_file_outputs_with_filenames() -> None:
    descriptor = _descriptor(
        [
            {"type": "number", "name": "t_max"},  # scalar — skipped
            {"type": "file", "name": "no_file"},  # no filename — skipped
            {
                "type": "file",
                "name": "temp",
                "label": "Temperature field",
                "filename": "temperature_final.vtu",
                "file_type": "field",
            },
        ]
    )
    specs = _job_file_output_specs(descriptor, accepted_exts=set())
    assert len(specs) == 1
    assert specs[0] == {
        "name": "temp",
        "label": "Temperature field",
        "filename": "temperature_final.vtu",
        "file_type": "field",
        "field_array": "",
    }


def test_file_output_specs_captures_field_array_name() -> None:
    descriptor = _descriptor(
        [
            {
                "type": "file",
                "name": "temp",
                "filename": "t.vtu",
                "file_type": "field",
                "field_node_array_name": "temperature",
            },
        ]
    )
    specs = _job_file_output_specs(descriptor, accepted_exts=set())
    assert specs[0]["field_array"] == "temperature"


def test_file_output_specs_filters_by_expected_field_array() -> None:
    """The expected-field filter keeps only field outputs carrying that exact
    nodal array — a temperature input never lists a failure-index field."""
    descriptor = _descriptor(
        [
            {
                "type": "file",
                "name": "temp",
                "filename": "t.vtu",
                "file_type": "field",
                "field_node_array_name": "temperature",
            },
            {
                "type": "file",
                "name": "fail",
                "filename": "f.vtu",
                "file_type": "field",
                "field_node_array_name": "failure_index",
            },
            {"type": "file", "name": "mesh", "filename": "m.vtu"},  # no array → excluded
        ]
    )
    specs = _job_file_output_specs(
        descriptor, accepted_exts=set(), expected_field_array="temperature"
    )
    assert [s["name"] for s in specs] == ["temp"]
    # Case-insensitive match.
    specs_ci = _job_file_output_specs(
        descriptor, accepted_exts=set(), expected_field_array="TEMPERATURE"
    )
    assert [s["name"] for s in specs_ci] == ["temp"]


def test_file_output_specs_filters_by_accepted_extension() -> None:
    descriptor = _descriptor(
        [
            {"type": "file", "name": "mesh", "filename": "out.vtu", "file_type": "field"},
            {"type": "file", "name": "report", "filename": "summary.csv", "file_type": "data"},
        ]
    )
    only_vtu = _job_file_output_specs(descriptor, accepted_exts={"vtu"})
    assert [s["name"] for s in only_vtu] == ["mesh"]
    # Empty accepted set accepts everything.
    both = _job_file_output_specs(descriptor, accepted_exts=set())
    assert {s["name"] for s in both} == {"mesh", "report"}


def test_file_output_specs_label_falls_back_to_name() -> None:
    descriptor = _descriptor([{"type": "file", "name": "temp", "filename": "t.vtu"}])
    specs = _job_file_output_specs(descriptor, accepted_exts=set())
    assert specs[0]["label"] == "temp"


def test_resolve_existing_output_files_keeps_only_present(tmp_path: Any) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "present.vtu").write_text("x")
    specs = [
        {"name": "here", "filename": "present.vtu"},
        {"name": "gone", "filename": "absent.vtu"},
    ]
    resolved = _resolve_existing_output_files([(7, str(tmp_path), specs)])
    assert set(resolved[7]) == {"here"}
    assert resolved[7]["here"].endswith("present.vtu")


def test_resolve_existing_output_files_strips_outputs_prefix(tmp_path: Any) -> None:
    """A descriptor filename declared *with* the ``outputs/`` prefix (e.g.
    Thermal Soak's "outputs/temperature_final.vtu") must resolve under
    ``<wd>/outputs/`` and not double the prefix — the bug behind the empty
    cross-tool picker."""
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    (outputs / "temperature_final.vtu").write_text("x")
    specs = [{"name": "temp", "filename": "outputs/temperature_final.vtu"}]
    resolved = _resolve_existing_output_files([(9, str(tmp_path), specs)])
    assert "temp" in resolved.get(9, {})
    # Resolved under <wd>/outputs/ exactly once (no doubled segment).
    assert resolved[9]["temp"].replace("\\", "/").endswith("outputs/temperature_final.vtu")
    assert "outputs/outputs" not in resolved[9]["temp"].replace("\\", "/")


def test_collect_work_skips_jobs_missing_pins() -> None:
    descriptors = {1: _descriptor([{"type": "file", "name": "t", "filename": "t.vtu"}])}
    mounts = {10: "/mnt"}
    jobs = [
        SimpleNamespace(
            id=1, tool_version_id=1, storage_location_id=10, working_dir_relative="job_0000001"
        ),  # good
        SimpleNamespace(
            id=2, tool_version_id=None, storage_location_id=10, working_dir_relative="job_0000002"
        ),  # no version
        SimpleNamespace(
            id=3, tool_version_id=1, storage_location_id=None, working_dir_relative="job_0000003"
        ),  # no location
        SimpleNamespace(
            id=4, tool_version_id=1, storage_location_id=10, working_dir_relative=""
        ),  # no working dir
    ]
    work, specs_by_job = _collect_output_resolution_work(
        jobs,
        descriptors,
        mounts,
        accepted_exts=set(),  # type: ignore[arg-type]
    )
    assert [w[0] for w in work] == [1]
    assert set(specs_by_job) == {1}


def test_collect_work_drops_jobs_without_matching_outputs() -> None:
    descriptors = {1: _descriptor([{"type": "file", "name": "t", "filename": "t.csv"}])}
    mounts = {10: "/mnt"}
    jobs = [
        SimpleNamespace(
            id=1, tool_version_id=1, storage_location_id=10, working_dir_relative="job_0000001"
        ),
    ]
    work, specs_by_job = _collect_output_resolution_work(
        jobs,
        descriptors,
        mounts,
        accepted_exts={"vtu"},  # type: ignore[arg-type]
    )
    assert work == []
    assert specs_by_job == {}


def test_assemble_rows_shapes_tool_job_output() -> None:
    descriptors = {
        1: _descriptor(
            [
                {
                    "type": "file",
                    "name": "temp",
                    "label": "Temperature field",
                    "filename": "temperature_final.vtu",
                    "file_type": "field",
                }
            ],
            name="Thermal Soak",
        )
    }
    specs_by_job = {
        5: [
            {
                "name": "temp",
                "label": "Temperature field",
                "filename": "temperature_final.vtu",
                "file_type": "field",
            }
        ],
    }
    resolved = {5: {"temp": "/mnt/job_0000005/outputs/temperature_final.vtu"}}
    scenarios = {3: SimpleNamespace(id=3, project_id=2, name="Soak case")}
    projects = {2: SimpleNamespace(id=2, name="Heat sink")}
    jobs = [
        SimpleNamespace(id=5, tool_version_id=1, tool_alias="thermal_soak", scenario_id=3),
        # job 6 has no resolved file → dropped from the rows
        SimpleNamespace(id=6, tool_version_id=1, tool_alias="thermal_soak", scenario_id=3),
    ]
    rows = _assemble_output_candidate_rows(
        jobs,  # type: ignore[arg-type]
        resolved=resolved,
        descriptors_by_version=descriptors,
        specs_by_job=specs_by_job,
        scenarios_by_id=scenarios,
        projects_by_id=projects,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["value"] == "/mnt/job_0000005/outputs/temperature_final.vtu"
    assert row["filename"] == "temperature_final.vtu"
    assert row["file_type"] == "field"
    assert row["output_label"] == "Temperature field"
    assert row["tool_alias"] == "thermal_soak"
    assert row["tool_name"] == "Thermal Soak"
    assert row["job_id"] == 5
    assert row["project_name"] == "Heat sink"
    assert row["scenario_name"] == "Soak case"
