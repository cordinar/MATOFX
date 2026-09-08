"""Extended tests covering the legacy-parity additions to ToolDescriptor."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from matofx.tools import (
    ExecutionBackend,
    InputField,
    InputGroup,
    LicenceRequirement,
    OutputField,
    OutputGroup,
    ResourceRequirements,
    SumCheck,
    TableColumn,
    ToolDescriptor,
)


def _basic(**overrides: object) -> ToolDescriptor:
    base: dict[str, object] = {
        "alias": "demo",
        "name": "Demo",
        "version": "1.0.0",
        "description": "Demo tool.",
        "main_script": "static/main.py",
    }
    base.update(overrides)
    return ToolDescriptor(**base)  # type: ignore[arg-type]


# --- Licences -------------------------------------------------------------


def test_licences_default_is_empty_list() -> None:
    d = _basic()
    assert d.licences_required == []


def test_licences_with_counts() -> None:
    d = _basic(
        licences_required=[
            LicenceRequirement(name="ANSYS_MECHANICAL", count=2),
            LicenceRequirement(name="ANSYS_FLUENT", count=1),
        ]
    )
    assert len(d.licences_required) == 2
    assert d.licences_required[0].count == 2


def test_licence_count_must_be_at_least_one() -> None:
    with pytest.raises(ValidationError):
        LicenceRequirement(name="X", count=0)


# --- Resources ------------------------------------------------------------


def test_resource_defaults() -> None:
    d = _basic()
    assert d.resources.cpu_cores == 1
    assert d.resources.ram_mb == 512
    assert d.resources.gpu_memory_mb == 0
    assert d.resources.requires_gpu is False


def test_requires_gpu_derived_from_memory() -> None:
    res = ResourceRequirements(cpu_cores=4, ram_mb=8192, gpu_memory_mb=4096)
    assert res.requires_gpu is True


def test_cpu_cores_bounds() -> None:
    with pytest.raises(ValidationError):
        ResourceRequirements(cpu_cores=0)
    with pytest.raises(ValidationError):
        ResourceRequirements(cpu_cores=999)


# --- Teams ----------------------------------------------------------------


def test_teams_default_to_empty() -> None:
    d = _basic()
    assert d.teams == []


def test_teams_custom() -> None:
    d = _basic(teams=["thermal", "mechanical"])
    assert d.teams == ["thermal", "mechanical"]


def test_teams_reject_duplicates() -> None:
    with pytest.raises(ValidationError):
        _basic(teams=["thermal", "thermal"])


def test_teams_reject_empty_strings() -> None:
    with pytest.raises(ValidationError):
        _basic(teams=["thermal", "  "])


# --- Execution backend ----------------------------------------------------


def test_execution_backend_default_is_subprocess() -> None:
    d = _basic()
    assert d.execution_backend.kind == "subprocess"


def test_hpc_batch_queue_is_optional() -> None:
    # A cluster with no partitions has no queue name to give (P-02);
    # requiring one forced the author to invent a partition that
    # doesn't exist on the target cluster.
    eb = ExecutionBackend(kind="hpc_batch")
    assert eb.queue is None
    assert ExecutionBackend(kind="hpc_batch", queue="default").queue == "default"


def test_aos_requires_server() -> None:
    with pytest.raises(ValidationError):
        ExecutionBackend(kind="aos_calculator")
    ExecutionBackend(kind="aos_calculator", server="aos-1")


def test_web_app_requires_url_and_port() -> None:
    with pytest.raises(ValidationError):
        ExecutionBackend(kind="web_app", url="http://x")
    ExecutionBackend(kind="web_app", url="http://x", port="8080")


# --- Approvals ------------------------------------------------------------


def test_approvals_default_none_no_approvers() -> None:
    d = _basic()
    assert d.approvals_policy == "none"
    assert d.approver_usernames == []


def test_approvers_required_when_policy_set() -> None:
    with pytest.raises(ValidationError):
        _basic(approvals_policy="pre")


def test_approvers_rejected_when_policy_none() -> None:
    with pytest.raises(ValidationError):
        _basic(approver_usernames=["alice"])


def test_approvers_with_each_policy() -> None:
    for policy in ("pre", "post", "both"):
        d = _basic(approvals_policy=policy, approver_usernames=["alice", "bob"])
        assert d.approvals_policy == policy
        assert d.approver_usernames == ["alice", "bob"]


# --- Inputs: numeric ranges + groups + sum_check -------------------------


def test_input_min_max_precision() -> None:
    field = InputField(
        name="temperature",
        label="Temp",
        type="float",
        unit="degC",
        minimum=0,
        maximum=300,
        precision=0.1,
    )
    assert field.minimum == 0
    assert field.maximum == 300
    assert field.precision == 0.1


def test_input_unit_renamed_from_legacy_units() -> None:
    """Legacy descriptor JSON uses ``unit`` (singular). Confirmed."""
    field = InputField(name="x", label="X", type="float", unit="mm")
    assert field.unit == "mm"


def test_multiselect_input() -> None:
    field = InputField(
        name="modes",
        label="Modes",
        type="multiselect",
        options=["a", "b", "c"],
    )
    assert field.type == "multiselect"


def test_multiselect_requires_options() -> None:
    with pytest.raises(ValidationError):
        InputField(name="modes", label="Modes", type="multiselect")


def test_downstream_input_requires_upstream_tool() -> None:
    with pytest.raises(ValidationError):
        InputField(
            name="conductivity",
            label="Conductivity",
            type="float",
            downstream_input=True,
        )
    field = InputField(
        name="conductivity",
        label="Conductivity",
        type="float",
        downstream_input=True,
        upstream_tool="material_properties",
    )
    assert field.upstream_tool == "material_properties"


def test_file_input_can_be_wired() -> None:
    """A ``file`` input may carry the wiring flags (ADR-0015 file hand-off).

    The schema treats the wiring flags type-agnostically — a downstream
    member can consume an upstream member's file output (a mesh, a field
    ``.vtp``), not just a scalar.
    """
    field = InputField(
        name="thermal_field",
        label="Thermal field",
        type="file",
        accepted_file_types=["vtp"],
        downstream_input=True,
        upstream_tool="thermal_soak",
        upstream_output="temperature_field",
    )
    assert field.type == "file"
    assert field.upstream_tool == "thermal_soak"
    assert field.upstream_output == "temperature_field"


def test_input_groups_with_sub_columns() -> None:
    d = _basic(
        input_groups=[
            InputGroup(name="g1", label="Group 1", column=1, row=1, sub_columns=2),
        ],
        inputs=[
            InputField(name="a", label="A", type="float", group="g1", sub_column=1),
            InputField(name="b", label="B", type="float", group="g1", sub_column=2),
        ],
    )
    assert d.input_groups[0].sub_columns == 2


def test_input_references_unknown_group_rejected() -> None:
    with pytest.raises(ValidationError):
        _basic(
            input_groups=[InputGroup(name="g1", label="G", column=1, row=1)],
            inputs=[
                InputField(name="a", label="A", type="float", group="ghost"),
            ],
        )


def test_sum_check_validates_against_siblings() -> None:
    d = _basic(
        inputs=[
            InputField(
                name="p1",
                label="P1",
                type="float",
                sum_check=SumCheck(value=100, sum_parameters=["p1", "p2"]),
            ),
            InputField(
                name="p2",
                label="P2",
                type="float",
                sum_check=SumCheck(value=100, sum_parameters=["p1", "p2"]),
            ),
        ]
    )
    assert d.inputs[0].sum_check is not None


def test_sum_check_unknown_target_rejected() -> None:
    with pytest.raises(ValidationError):
        _basic(
            inputs=[
                InputField(
                    name="p1",
                    label="P1",
                    type="float",
                    sum_check=SumCheck(value=100, sum_parameters=["p1", "ghost"]),
                ),
            ]
        )


# --- Outputs --------------------------------------------------------------


def test_output_number() -> None:
    d = _basic(
        output_groups=[OutputGroup(name="vars", label="Vars", column=1, row=1)],
        outputs=[
            OutputField(
                name="t_core",
                label="Core T",
                type="number",
                unit="degC",
                precision=0.01,
                group="vars",
            )
        ],
    )
    assert d.outputs[0].type == "number"


def test_output_file_requires_filename() -> None:
    with pytest.raises(ValidationError):
        OutputField(name="plot", label="Plot", type="file")
    OutputField(name="plot", label="Plot", type="file", filename="x.html")


def test_output_csv_plot_with_axis_config() -> None:
    field = OutputField(
        name="profile",
        label="Profile",
        type="file",
        file_type="csv_plot",
        filename="profile.csv",
        xaxis_column=0,
        yaxis_column=1,
        plot_title="P",
        xaxis_title="X",
        yaxis_title="Y",
    )
    assert field.file_type == "csv_plot"


def test_output_references_unknown_group_rejected() -> None:
    with pytest.raises(ValidationError):
        _basic(
            outputs=[OutputField(name="x", label="X", type="number", group="ghost")],
        )


# --- KPI extraction (ADR-0012, slice 1) -----------------------------------


def test_output_kpi_defaults_false() -> None:
    """Existing descriptors that don't mention KPI fields keep the old shape."""
    field = OutputField(name="t_core", label="Core T", type="number")
    assert field.kpi is False
    assert field.kpi_label is None
    assert field.kpi_unit is None
    assert field.kpi_category is None
    assert field.uncertainty_for is None


def test_output_uncertainty_for_on_number_accepted() -> None:
    """A number output may name the value it's the 1-sigma uncertainty of (ADR-0008)."""
    field = OutputField(
        name="mass_std", label="Mass (1-sigma)", type="number", uncertainty_for="mass"
    )
    assert field.uncertainty_for == "mass"


def test_output_uncertainty_for_on_non_number_rejected() -> None:
    """uncertainty_for only makes sense on a scalar numeric output."""
    with pytest.raises(ValidationError, match="uncertainty_for requires type='number'"):
        OutputField(
            name="report", label="Report", type="file", filename="r.pdf", uncertainty_for="mass"
        )


def _tool_with_outputs(outputs: list[dict]) -> dict:
    """A minimal descriptor payload carrying just the outputs under test."""
    return {
        "schema_version": 1,
        "alias": "t",
        "name": "T",
        "version": "1.0.0",
        "short_description": "x",
        "main_script": "m.py",
        "execution_backend": {"kind": "subprocess"},
        "inputs": [],
        "outputs": outputs,
    }


def test_an_uncertainty_pair_validates() -> None:
    d = ToolDescriptor.model_validate(
        _tool_with_outputs(
            [
                {"name": "mass", "label": "Mass", "type": "number"},
                {
                    "name": "mass_std",
                    "label": "Mass (1-sigma)",
                    "type": "number",
                    "uncertainty_for": "mass",
                },
            ]
        )
    )
    assert d.outputs[1].uncertainty_for == "mass"


def test_a_dangling_uncertainty_reference_is_rejected() -> None:
    """A dangling link does not degrade — it **hides a result**.

    The job page folds an uncertainty into its parent and suppresses its own
    row, so pointing at an output that was renamed or deleted removes the
    number from the results page entirely, with nothing on screen to say so.
    """
    with pytest.raises(ValidationError, match="declares no output called 'mass'"):
        ToolDescriptor.model_validate(
            _tool_with_outputs(
                [
                    {
                        "name": "mass_std",
                        "label": "s",
                        "type": "number",
                        "uncertainty_for": "mass",
                    }
                ]
            )
        )


def test_an_uncertainty_of_a_non_numeric_output_is_rejected() -> None:
    with pytest.raises(ValidationError, match="is a 'file' output"):
        ToolDescriptor.model_validate(
            _tool_with_outputs(
                [
                    {"name": "r", "label": "R", "type": "file", "filename": "r.pdf"},
                    {"name": "r_std", "label": "s", "type": "number", "uncertainty_for": "r"},
                ]
            )
        )


def test_an_uncertainty_of_an_uncertainty_is_rejected() -> None:
    """No chains: ± has one level, and the renderer folds exactly once."""
    with pytest.raises(ValidationError, match="which is itself an uncertainty"):
        ToolDescriptor.model_validate(
            _tool_with_outputs(
                [
                    {"name": "a", "label": "A", "type": "number"},
                    {"name": "b", "label": "B", "type": "number", "uncertainty_for": "a"},
                    {"name": "c", "label": "C", "type": "number", "uncertainty_for": "b"},
                ]
            )
        )


def test_output_kpi_true_on_number_accepted() -> None:
    """`kpi = true` on a numeric output is valid."""
    field = OutputField(
        name="t_max",
        label="Max temperature",
        type="number",
        unit="K",
        kpi=True,
        kpi_label="Peak temp",
        kpi_unit="K",
        kpi_category="thermal",
    )
    assert field.kpi is True
    assert field.kpi_label == "Peak temp"
    assert field.kpi_unit == "K"
    assert field.kpi_category == "thermal"


def test_output_kpi_true_minimal_optional_fields_omitted() -> None:
    """`kpi = true` is valid without any of the optional kpi_* override fields."""
    field = OutputField(name="t_max", label="Max T", type="number", kpi=True)
    assert field.kpi is True
    assert field.kpi_label is None
    assert field.kpi_unit is None
    assert field.kpi_category is None


def test_output_kpi_true_on_string_rejected() -> None:
    """`kpi = true` on a string output fails validation with a clear message."""
    with pytest.raises(ValidationError, match="kpi=true requires type='number'"):
        OutputField(name="status", label="Status", type="string", kpi=True)


def test_output_kpi_true_on_file_rejected() -> None:
    """`kpi = true` on a file output fails validation."""
    with pytest.raises(ValidationError, match="kpi=true requires type='number'"):
        OutputField(
            name="plot",
            label="Plot",
            type="file",
            filename="p.html",
            kpi=True,
        )


def test_output_kpi_true_on_table_rejected() -> None:
    """`kpi = true` on a table output fails validation."""
    with pytest.raises(ValidationError, match="kpi=true requires type='number'"):
        OutputField(
            name="grid",
            label="Grid",
            type="table",
            columns=[TableColumn(name="x", label="X")],
            kpi=True,
        )


def test_output_kpi_label_without_flag_rejected() -> None:
    """`kpi_label` set on a non-KPI output fails — keeps the descriptor honest."""
    with pytest.raises(ValidationError, match="may only be set when kpi=true"):
        OutputField(
            name="t_max",
            label="Max T",
            type="number",
            kpi=False,
            kpi_label="Peak temp",
        )


def test_output_kpi_unit_without_flag_rejected() -> None:
    """`kpi_unit` set on a non-KPI output fails."""
    with pytest.raises(ValidationError, match="may only be set when kpi=true"):
        OutputField(
            name="t_max",
            label="Max T",
            type="number",
            kpi=False,
            kpi_unit="K",
        )


def test_output_kpi_category_without_flag_rejected() -> None:
    """`kpi_category` set on a non-KPI output fails."""
    with pytest.raises(ValidationError, match="may only be set when kpi=true"):
        OutputField(
            name="t_max",
            label="Max T",
            type="number",
            kpi=False,
            kpi_category="thermal",
        )


def test_descriptor_round_trip_with_kpi_fields() -> None:
    """A descriptor with KPI outputs survives `_basic` assembly."""
    d = _basic(
        output_groups=[OutputGroup(name="vars", label="Vars", column=1, row=1)],
        outputs=[
            OutputField(
                name="t_max",
                label="Max T",
                type="number",
                unit="K",
                kpi=True,
                kpi_label="Peak temp",
                kpi_unit="K",
                kpi_category="thermal",
                group="vars",
            ),
            OutputField(
                name="p_max",
                label="Max P",
                type="number",
                unit="Pa",
                kpi=True,
                kpi_label="Peak pressure",
                kpi_category="structural",
                group="vars",
            ),
            OutputField(
                name="notes",
                label="Notes",
                type="string",
                group="vars",
            ),
        ],
    )
    kpi_outputs = [o for o in d.outputs if o.kpi]
    assert len(kpi_outputs) == 2
    assert {o.kpi_label for o in kpi_outputs} == {"Peak temp", "Peak pressure"}


def test_descriptor_duplicate_kpi_label_rejected() -> None:
    """Two KPI outputs in one descriptor may not share a `kpi_label`."""
    with pytest.raises(ValidationError, match=r"KPI labels.*must be unique"):
        _basic(
            outputs=[
                OutputField(
                    name="t_max",
                    label="Max T",
                    type="number",
                    kpi=True,
                    kpi_label="Peak",
                ),
                OutputField(
                    name="p_max",
                    label="Max P",
                    type="number",
                    kpi=True,
                    kpi_label="Peak",
                ),
            ],
        )


def test_descriptor_duplicate_kpi_label_via_fallback_to_label_rejected() -> None:
    """When `kpi_label` is unset, `label` is the effective KPI column header.

    Two KPI outputs that both fall back to the same `label` are rejected
    on the same grounds as explicit `kpi_label` duplicates.
    """
    with pytest.raises(ValidationError, match=r"KPI labels.*must be unique"):
        _basic(
            outputs=[
                OutputField(name="t1", label="Headline", type="number", kpi=True),
                OutputField(name="t2", label="Headline", type="number", kpi=True),
            ],
        )


def test_descriptor_non_kpi_duplicate_label_allowed() -> None:
    """Non-KPI outputs may share a label — the warehouse never surfaces them.

    Only the KPI subset must have unique effective labels; the warehouse
    column-uniqueness invariant doesn't apply to outputs the warehouse
    never renders.
    """
    d = _basic(
        outputs=[
            OutputField(name="a", label="Shared", type="string"),
            OutputField(name="b", label="Shared", type="string"),
        ],
    )
    assert len(d.outputs) == 2


def test_descriptor_mixed_kpi_and_non_kpi_label_clash_allowed() -> None:
    """A non-KPI output may share its `label` with a KPI's `kpi_label`.

    The duplicate check is intra-KPI only: a non-KPI output's label never
    appears in the warehouse, so the column-uniqueness invariant holds
    even when its string collides with a KPI label.
    """
    d = _basic(
        outputs=[
            OutputField(
                name="t_max",
                label="Max T",
                type="number",
                kpi=True,
                kpi_label="Headline",
            ),
            OutputField(name="other", label="Headline", type="string"),
        ],
    )
    assert d.outputs[0].kpi is True
    assert d.outputs[1].kpi is False


# --- Workflow / bundle additions -----------------------------------------


def test_downstream_tools_default_empty() -> None:
    d = _basic()
    assert d.downstream_tools == []


def test_downstream_tools_listed() -> None:
    d = _basic(downstream_tools=["heat_transfer", "stress"])
    assert "heat_transfer" in d.downstream_tools


def test_icon_and_presets_filenames_optional() -> None:
    d = _basic()
    assert d.icon_filename is None
    assert d.presets_filename is None
    d2 = _basic(icon_filename="demo.png", presets_filename="demo_presets.json")
    assert d2.icon_filename == "demo.png"


# --- Round-trip persistence ----------------------------------------------


def test_full_descriptor_round_trips_through_json() -> None:
    """A richly populated descriptor survives ``model_dump`` → ``model_validate``."""
    d = _basic(
        teams=["thermal"],
        resources=ResourceRequirements(cpu_cores=4, ram_mb=8192, gpu_memory_mb=2048),
        licences_required=[LicenceRequirement(name="ANSYS", count=2)],
        execution_backend=ExecutionBackend(kind="hpc_batch", queue="gpu_q"),
        input_groups=[InputGroup(name="g1", label="G1", column=1, row=1)],
        inputs=[
            InputField(
                name="t",
                label="T",
                type="float",
                unit="degC",
                minimum=0,
                maximum=100,
                precision=0.1,
                group="g1",
            )
        ],
        output_groups=[OutputGroup(name="o", label="O", column=1, row=1)],
        outputs=[OutputField(name="t_out", label="T Out", type="number", group="o")],
        approvals_policy="pre",
        approver_usernames=["root"],
        downstream_tools=["next-tool"],
    )
    payload = d.model_dump(mode="json")
    d2 = ToolDescriptor.model_validate(payload)
    assert d2 == d
