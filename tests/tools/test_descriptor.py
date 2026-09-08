"""Tests for the ToolDescriptor schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from matofx.tools import InputField, ToolDescriptor
from matofx.tools.descriptor import SHORT_DESCRIPTION_MAX, tool_descriptions


def _basic(**overrides: object) -> ToolDescriptor:
    base: dict[str, object] = {
        "alias": "demo",
        "name": "Demo",
        "version": "1.0.0",
        "short_description": "Demo tool.",
        "main_script": "static/main.py",
    }
    base.update(overrides)
    return ToolDescriptor(**base)  # type: ignore[arg-type]


def test_minimal_descriptor_round_trips() -> None:
    d = _basic()
    re = ToolDescriptor.model_validate(d.model_dump())
    assert re == d


def test_short_and_long_description_round_trip() -> None:
    d = _basic(short_description="One-liner.", long_description="The fuller story.")
    assert d.short_description == "One-liner."
    assert d.long_description == "The fuller story."
    re = ToolDescriptor.model_validate(d.model_dump())
    assert re == d


def test_long_description_optional() -> None:
    d = _basic(short_description="Just the short one.")
    assert d.long_description is None


def test_short_description_required() -> None:
    base = {
        "alias": "demo",
        "name": "Demo",
        "version": "1.0.0",
        "main_script": "static/main.py",
    }
    with pytest.raises(ValidationError):
        ToolDescriptor.model_validate(base)


def test_short_description_capped() -> None:
    with pytest.raises(ValidationError):
        _basic(short_description="x" * (SHORT_DESCRIPTION_MAX + 1))


def test_legacy_description_maps_to_short() -> None:
    """A pre-split descriptor carrying only ``description`` still loads."""
    d = ToolDescriptor.model_validate(
        {
            "alias": "demo",
            "name": "Demo",
            "version": "1.0.0",
            "main_script": "static/main.py",
            "description": "Legacy single blurb.",
        }
    )
    assert d.short_description == "Legacy single blurb."
    assert d.long_description is None


def test_legacy_long_description_spills_into_long() -> None:
    legacy = "y" * 250
    d = ToolDescriptor.model_validate(
        {
            "alias": "demo",
            "name": "Demo",
            "version": "1.0.0",
            "main_script": "static/main.py",
            "description": legacy,
        }
    )
    assert len(d.short_description) == SHORT_DESCRIPTION_MAX
    assert d.short_description.endswith("…")
    assert d.long_description == legacy


# --- Bundle-relative filename traversal guards ----------------------------


def test_subdir_bundle_paths_are_allowed() -> None:
    """A legitimate relative sub-path (no traversal) stays valid."""
    d = _basic(
        main_script="src/run.py",
        icon_filename="assets/icon.png",
        presets_filename="config/presets.toml",
        input_guides=[{"filename": "guides/step1.png", "caption": "First"}],
    )
    assert d.main_script == "src/run.py"
    assert d.icon_filename == "assets/icon.png"


@pytest.mark.parametrize(
    "bad",
    [
        "../escape.py",
        "sub/../../escape.py",
        "/etc/passwd",
        "C:/Windows/system32/x.py",
        "sub\\win.py",
        "..\\escape.py",
    ],
)
def test_main_script_rejects_traversal(bad: str) -> None:
    with pytest.raises(ValidationError):
        _basic(main_script=bad)


@pytest.mark.parametrize("bad", ["../../secret.png", "/root/.ssh/id_rsa", "d:/x.png"])
def test_icon_filename_rejects_traversal(bad: str) -> None:
    with pytest.raises(ValidationError):
        _basic(icon_filename=bad)


@pytest.mark.parametrize("bad", ["../../secret.png", "../../../etc/passwd"])
def test_guide_image_filename_rejects_traversal(bad: str) -> None:
    with pytest.raises(ValidationError):
        _basic(input_guides=[{"filename": bad, "caption": "x"}])


def test_presets_filename_rejects_traversal() -> None:
    with pytest.raises(ValidationError):
        _basic(presets_filename="../../other/presets.toml")


def test_tool_descriptions_helper() -> None:
    assert tool_descriptions({"short_description": "s", "long_description": "l"}) == ("s", "l")
    assert tool_descriptions({"short_description": "s"}) == ("s", None)
    assert tool_descriptions({"description": "legacy"}) == ("legacy", None)
    assert tool_descriptions({}) == ("", None)


def test_alias_lowercased_and_validated() -> None:
    d = _basic(alias="MyTool_2")
    assert d.alias == "mytool_2"
    with pytest.raises(ValidationError):
        _basic(alias="bad alias!")


def test_input_field_name_must_be_identifier() -> None:
    with pytest.raises(ValidationError):
        InputField(name="1bad", label="X", type="string")
    with pytest.raises(ValidationError):
        InputField(name="bad-name", label="X", type="string")
    InputField(name="good_name1", label="X", type="string")  # passes


def test_unique_input_names_enforced() -> None:
    with pytest.raises(ValidationError):
        _basic(
            inputs=[
                InputField(name="x", label="X", type="string"),
                InputField(name="x", label="Y", type="integer"),
            ]
        )


def test_select_field_requires_options() -> None:
    with pytest.raises(ValidationError):
        _basic(inputs=[InputField(name="mode", label="Mode", type="select")])
    _basic(
        inputs=[
            InputField(
                name="mode",
                label="Mode",
                type="select",
                options=["fast", "slow"],
            )
        ]
    )


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        _basic(rubbish="no")


def test_default_duration_class_is_medium() -> None:
    assert _basic().duration_class == "medium"


def test_default_email_notifications_is_false() -> None:
    """Job-state emails are opt-in: a tool sends none until asked to."""
    assert _basic().email_notifications is False


def test_default_input_groups_start_collapsed_is_true() -> None:
    assert _basic().input_groups_start_collapsed is True


def test_default_require_all_groups_opened_is_true() -> None:
    assert _basic().require_all_groups_opened is True


def test_group_expansion_flags_are_overridable() -> None:
    d = _basic(input_groups_start_collapsed=False, require_all_groups_opened=False)
    assert d.input_groups_start_collapsed is False
    assert d.require_all_groups_opened is False


def test_table_input_requires_at_least_one_column() -> None:
    """A ``type: table`` input with no columns must fail validation."""
    with pytest.raises(ValidationError, match="table but has no columns"):
        InputField(name="grid", label="Grid", type="table")


def test_table_input_with_columns_validates() -> None:
    """A table input with at least one TableColumn passes."""
    field = InputField(
        name="components",
        label="Components",
        type="table",
        columns=[
            {"name": "material", "label": "Material", "type": "string"},  # type: ignore[list-item]
            {"name": "fraction", "label": "Fraction", "type": "float"},  # type: ignore[list-item]
        ],
        default_rows=3,
    )
    assert field.type == "table"
    assert field.columns is not None
    assert len(field.columns) == 2
    assert field.columns[0].name == "material"
    assert field.default_rows == 3


def test_table_input_rejects_duplicate_column_names() -> None:
    """Duplicate column names within a table input fail validation."""
    with pytest.raises(ValidationError, match="unique names"):
        InputField(
            name="dup",
            label="Dup",
            type="table",
            columns=[
                {"name": "x", "label": "X", "type": "string"},  # type: ignore[list-item]
                {"name": "x", "label": "X again", "type": "float"},  # type: ignore[list-item]
            ],
        )


def test_table_output_requires_at_least_one_column() -> None:
    """``type: table`` outputs must declare at least one column."""
    from matofx.tools import OutputField  # noqa: PLC0415

    with pytest.raises(ValidationError, match="table but has no columns"):
        OutputField(name="grid", label="Grid", type="table")


# --- Widget / file-input / table-column-extras new-schema tests ---------


def test_integer_precision_must_be_one() -> None:
    """An integer input with a non-1 precision is refused."""
    with pytest.raises(ValidationError, match="precision must be 1"):
        InputField(name="n", label="N", type="integer", precision=0.5)
    InputField(name="n", label="N", type="integer", precision=1.0)  # passes


def test_integer_min_max_must_be_whole() -> None:
    """Integer fields refuse fractional minimum / maximum bounds."""
    with pytest.raises(ValidationError, match="minimum must be a whole number"):
        InputField(name="n", label="N", type="integer", minimum=1.5)
    with pytest.raises(ValidationError, match="maximum must be a whole number"):
        InputField(name="n", label="N", type="integer", maximum=10.25)
    # Floats with no fractional part are accepted (Pydantic gives us
    # whatever the JSON serialiser passed; 0.0 is a valid integer bound).
    InputField(name="n", label="N", type="integer", minimum=0, maximum=10)
    InputField(name="n", label="N", type="integer", minimum=0.0, maximum=10.0)


def test_table_column_integer_min_max_must_be_whole() -> None:
    """Integer table columns also reject fractional bounds."""
    from matofx.tools import TableColumn  # noqa: PLC0415

    with pytest.raises(ValidationError, match="minimum must be a whole number"):
        TableColumn(name="x", label="X", type="integer", minimum=0.5)
    TableColumn(name="x", label="X", type="integer", minimum=0, maximum=10)


def test_widget_slider_only_for_numeric() -> None:
    """``widget=slider`` on a string field is refused."""
    with pytest.raises(ValidationError, match="slider widget but is not numeric"):
        InputField(name="s", label="S", type="string", widget="slider")
    InputField(name="n", label="N", type="float", widget="slider")  # passes


def test_choice_widget_radio_only_for_choice() -> None:
    """``choice_widget=radio`` on a numeric field is refused."""
    with pytest.raises(ValidationError, match="radio widget but is not select"):
        InputField(name="n", label="N", type="float", choice_widget="radio")
    InputField(
        name="m", label="M", type="select", options=["a", "b"], choice_widget="radio"
    )  # passes


def test_file_extras_only_on_file_inputs() -> None:
    """``accepted_file_types`` / ``max_file_size_mb`` outside of file inputs are refused."""
    with pytest.raises(ValidationError, match="accepted_file_types but is not a file"):
        InputField(name="x", label="X", type="string", accepted_file_types=["csv"])
    with pytest.raises(ValidationError, match="max_file_size_mb but is not a file"):
        InputField(name="x", label="X", type="string", max_file_size_mb=10.0)


def test_file_extras_validate_and_clean() -> None:
    """File inputs accept and normalise their extras."""
    field = InputField(
        name="upload",
        label="Upload",
        type="file",
        accepted_file_types=[".CSV", "Xlsx"],
        max_file_size_mb=25.5,
    )
    assert field.accepted_file_types == ["csv", "xlsx"]
    assert field.max_file_size_mb == 25.5


def test_max_file_size_must_be_positive() -> None:
    """Zero or negative max file size is rejected (use None for unlimited)."""
    with pytest.raises(ValidationError, match="max_file_size_mb must be positive"):
        InputField(name="upload", label="Upload", type="file", max_file_size_mb=0.0)


def test_accepted_file_types_reject_duplicates() -> None:
    """Duplicate accepted-file-type entries are rejected."""
    with pytest.raises(ValidationError, match="unique"):
        InputField(
            name="upload",
            label="Upload",
            type="file",
            accepted_file_types=["csv", "csv"],
        )


def test_table_column_default_validates_against_type() -> None:
    """A table-column default with a mismatched Python type is refused."""
    from matofx.tools import TableColumn  # noqa: PLC0415

    with pytest.raises(ValidationError, match="default must be numeric"):
        TableColumn(name="x", label="X", type="float", default="not-a-number")
    TableColumn(name="x", label="X", type="float", default=0.5)  # passes


def test_table_column_integer_precision_must_be_one() -> None:
    """Integer table columns also lock precision to 1."""
    from matofx.tools import TableColumn  # noqa: PLC0415

    with pytest.raises(ValidationError, match="precision must be 1"):
        TableColumn(name="x", label="X", type="integer", precision=0.1)
    TableColumn(name="x", label="X", type="integer", precision=1.0)


def test_table_column_carries_unit_min_max() -> None:
    """Per-column unit / min / max / precision / default round-trip in JSON."""
    from matofx.tools import TableColumn  # noqa: PLC0415

    col = TableColumn(
        name="frac",
        label="Frac",
        type="float",
        unit="%",
        minimum=0.0,
        maximum=100.0,
        precision=0.01,
        default=50.0,
    )
    payload = col.model_dump(mode="json")
    again = TableColumn.model_validate(payload)
    assert again == col


# --- material inputs (ADR-0007 §3) ---------------------------------------


def test_material_field_requires_material_class() -> None:
    # a valid material field constructs
    f = InputField(
        name="fluid",
        label="Working fluid",
        type="material",
        material_class="Fluid",
        required_properties=["thermal_conductivity"],
    )
    assert f.material_class == "Fluid"
    assert f.required_properties == ["thermal_conductivity"]
    # ...but a material field with no class is rejected
    with pytest.raises(ValidationError):
        InputField(name="fluid", label="Working fluid", type="material")


def test_material_field_rejects_options_and_default() -> None:
    with pytest.raises(ValidationError):  # frozen-options rule: no enumerated catalog
        InputField(
            name="fluid",
            label="Fluid",
            type="material",
            material_class="Fluid",
            options=["Water", "Air"],
        )
    with pytest.raises(ValidationError):  # install-specific id can't be a descriptor default
        InputField(
            name="fluid",
            label="Fluid",
            type="material",
            material_class="Fluid",
            default=7,
        )


def test_material_constraints_only_on_material_fields() -> None:
    with pytest.raises(ValidationError):
        InputField(name="x", label="X", type="string", material_class="Fluid")
    with pytest.raises(ValidationError):
        InputField(name="x", label="X", type="string", required_properties=["thermal_conductivity"])


def test_required_properties_cleaned_unique() -> None:
    f = InputField(
        name="fluid",
        label="Fluid",
        type="material",
        material_class="Fluid",
        required_properties=[" thermal_conductivity ", "density"],
    )
    assert f.required_properties == ["thermal_conductivity", "density"]
    with pytest.raises(ValidationError):  # duplicates rejected
        InputField(
            name="fluid",
            label="Fluid",
            type="material",
            material_class="Fluid",
            required_properties=["density", "density"],
        )


def test_material_field_round_trips() -> None:
    f = InputField(
        name="fluid",
        label="Fluid",
        type="material",
        material_class="Fluid",
        required_properties=["thermal_conductivity"],
        help_text="Pick the coolant",
        required=True,
    )
    again = InputField.model_validate(f.model_dump())
    assert again == f


# --- Field outputs (ADR-0008 §4.4 — POD field surrogates) -----------------


def test_field_output_round_trips() -> None:
    """A well-formed field output validates and round-trips through JSON."""
    from matofx.tools import OutputField  # noqa: PLC0415

    out = OutputField(
        name="temperature_field",
        label="Temperature field",
        type="file",
        file_type="field",
        filename="outputs/temperature.vtu",
        field_format="vtu",
        field_node_array_name="Temperature",
    )
    again = OutputField.model_validate(out.model_dump())
    assert again == out


def test_field_output_npy_round_trips() -> None:
    """A bare ``.npy`` field needs no node-array name."""
    from matofx.tools import OutputField  # noqa: PLC0415

    out = OutputField(
        name="t_field",
        label="T",
        type="file",
        file_type="field",
        filename="outputs/t.npy",
        field_format="npy",
    )
    assert out.field_node_array_name is None


def test_field_output_requires_field_format() -> None:
    """A field output with no ``field_format`` is refused."""
    from matofx.tools import OutputField  # noqa: PLC0415

    with pytest.raises(ValidationError, match="must declare field_format"):
        OutputField(
            name="t_field",
            label="T",
            type="file",
            file_type="field",
            filename="outputs/t.npy",
        )


def test_field_output_rejects_unknown_format() -> None:
    """An unsupported ``field_format`` is refused with the allowed list."""
    from matofx.tools import OutputField  # noqa: PLC0415

    with pytest.raises(ValidationError, match="unsupported field_format"):
        OutputField(
            name="t_field",
            label="T",
            type="file",
            file_type="field",
            filename="outputs/t.odb",
            field_format="odb",
        )


def test_field_attrs_only_on_field_outputs() -> None:
    """Field-only attributes on a non-field output are refused."""
    from matofx.tools import OutputField  # noqa: PLC0415

    with pytest.raises(ValidationError, match="may only be set on a field output"):
        OutputField(
            name="chart",
            label="Chart",
            type="file",
            file_type="csv_plot",
            filename="outputs/data.csv",
            field_format="npy",
        )


def test_time_resolved_field_output_round_trips() -> None:
    """A transient field declares its timestep glob and round-trips."""
    from matofx.tools import OutputField  # noqa: PLC0415

    out = OutputField(
        name="t_field",
        label="T",
        type="file",
        file_type="field",
        filename="outputs/t_0000.npy",
        field_format="npy",
        field_timesteps="outputs/t_*.npy",
    )
    again = OutputField.model_validate(out.model_dump())
    assert again == out
    assert again.field_timesteps == "outputs/t_*.npy"


# --- Output validity bounds (ADR-0017) --------------------------------------


def test_scalar_output_min_max_round_trips() -> None:
    """A numeric output may declare min/max bounds and round-trips."""
    from matofx.tools import OutputField  # noqa: PLC0415

    out = OutputField(
        name="failure_index",
        label="Peak failure index",
        type="number",
        min=0.0,
        max=10.0,
    )
    again = OutputField.model_validate(out.model_dump())
    assert again == out
    assert again.min == 0.0
    assert again.max == 10.0


def test_field_output_value_bounds_round_trip() -> None:
    """A field output may declare per-node value_min/value_max and round-trips."""
    from matofx.tools import OutputField  # noqa: PLC0415

    out = OutputField(
        name="failure_field",
        label="Failure-index field",
        type="file",
        file_type="field",
        filename="outputs/failure_index.vtu",
        field_format="vtu",
        field_node_array_name="failure_index",
        value_min=0.0,
        value_max=10.0,
    )
    again = OutputField.model_validate(out.model_dump())
    assert again == out
    assert again.value_min == 0.0
    assert again.value_max == 10.0


def test_min_max_require_number() -> None:
    """min/max on a non-numeric output are refused."""
    from matofx.tools import OutputField  # noqa: PLC0415

    with pytest.raises(ValidationError, match="min / max require type='number'"):
        OutputField(
            name="chart",
            label="Chart",
            type="file",
            file_type="csv_plot",
            filename="outputs/data.csv",
            min=0.0,
        )


def test_value_bounds_require_field() -> None:
    """value_min/value_max on a non-field output are refused."""
    from matofx.tools import OutputField  # noqa: PLC0415

    with pytest.raises(ValidationError, match="value_min / value_max may only be set"):
        OutputField(name="x", label="X", type="number", value_min=0.0)


def test_scalar_min_must_not_exceed_max() -> None:
    """A min above max is refused."""
    from matofx.tools import OutputField  # noqa: PLC0415

    with pytest.raises(ValidationError, match=r"min .* must be <= max"):
        OutputField(name="x", label="X", type="number", min=5.0, max=1.0)


def test_field_value_min_must_not_exceed_value_max() -> None:
    """A value_min above value_max is refused."""
    from matofx.tools import OutputField  # noqa: PLC0415

    with pytest.raises(ValidationError, match=r"value_min .* must be <= "):
        OutputField(
            name="f",
            label="F",
            type="file",
            file_type="field",
            filename="outputs/f.npy",
            field_format="npy",
            value_min=5.0,
            value_max=1.0,
        )


def test_allow_non_finite_only_on_numeric_or_field() -> None:
    """allow_non_finite is meaningless on a string/table output and refused there."""
    from matofx.tools import OutputField  # noqa: PLC0415

    with pytest.raises(ValidationError, match="allow_non_finite only applies"):
        OutputField(name="note", label="Note", type="string", allow_non_finite=True)

    # Valid on a numeric output.
    ok = OutputField(name="x", label="X", type="number", allow_non_finite=True)
    assert ok.allow_non_finite is True
