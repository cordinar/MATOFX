"""Round-trip tests for the wizard's state ↔ descriptor translators.

The wizard's UI is NiceGUI-driven (browser); the underlying logic
that *translates* between a stored ``ToolDescriptor`` JSON and the
internal authoring state is pure Python and easily unit-tested.
These tests pin down the round-trip so the edit-mode wizard
doesn't silently lose unit / sum-check / plot-wiring data.
"""

from __future__ import annotations

import pytest
from nicegui import Client
from nicegui.page import page

from matofx.tools.descriptor import ToolDescriptor
from matofx.ui.pages._descriptor_wizard import (
    _FILE_TYPE_HINTS,
    UpstreamWiringContext,
    _flush_required_properties_csv,
    _group_inputs_for_presets,
    _normalise_type_dependent_state,
    _preset_input_label,
    _preset_input_label_text,
    _preset_input_unit_str,
    build_descriptor_payload,
    empty_output_field,
    empty_preset,
    empty_state,
    render_descriptor_wizard,
    state_from_descriptor,
)
from matofx.ui.units import UNIT_CUSTOM_VALUE, UNIT_NONE_VALUE


def test_file_type_hints_register_pdf_anim3d_video() -> None:
    """New rich-output renderers surface in the wizard's hint dict so
    tool authors can pick them when declaring an output."""
    for key in ("pdf", "animation_3d", "video", "field"):
        assert key in _FILE_TYPE_HINTS, f"missing wizard hint for {key!r}"


def test_field_output_round_trips_through_the_wizard() -> None:
    """A ``file_type='field'`` output keeps its field attributes through the
    editor (ADR-0008 §4.4). Regression: the wizard used to drop ``field_format``
    on save, so re-validating the rebuilt descriptor raised
    "a field output must declare field_format"."""
    descriptor = {
        "alias": "soak",
        "name": "Soak",
        "version": "1.0.0",
        "short_description": "x",
        "main_script": "soak.py",
        "outputs": [
            {
                "name": "temperature_series",
                "label": "Temperature field",
                "type": "file",
                "file_type": "field",
                "filename": "outputs/t_0000.vtu",
                "field_format": "vtu",
                "field_node_array_name": "temperature",
                "field_timesteps": "outputs/t_*.vtu",
            }
        ],
    }
    state = state_from_descriptor(descriptor)
    payload = build_descriptor_payload(state)
    out = next(o for o in payload["outputs"] if o["name"] == "temperature_series")
    assert out["file_type"] == "field"
    assert out["field_format"] == "vtu"
    assert out["field_node_array_name"] == "temperature"
    assert out["field_timesteps"] == "outputs/t_*.vtu"
    # The rebuilt descriptor must re-validate (the Save path does exactly this).
    ToolDescriptor.model_validate({**descriptor, **payload})


def test_non_field_output_does_not_emit_field_keys() -> None:
    """The field-only keys must not leak onto a plain file output (the
    descriptor validator forbids them off a field output)."""
    descriptor = {
        "outputs": [
            {
                "name": "report",
                "label": "Report",
                "type": "file",
                "file_type": "html_plot",
                "filename": "outputs/r.html",
            },
        ],
    }
    payload = build_descriptor_payload(state_from_descriptor(descriptor))
    out = payload["outputs"][0]
    assert "field_format" not in out
    assert "field_timesteps" not in out


def test_kpi_output_round_trips_through_the_wizard() -> None:
    """A numeric output flagged ``kpi=true`` keeps the flag and its
    label/unit/category overrides through the editor (ADR-0012 §3.1).
    Regression: the wizard's output serialiser omitted every ``kpi*`` key,
    so ticking "Mark as KPI" (and any overrides) was silently lost on Save."""
    descriptor = {
        "alias": "soak",
        "name": "Soak",
        "version": "1.0.0",
        "short_description": "x",
        "main_script": "soak.py",
        "outputs": [
            {
                "name": "peak_temperature",
                "label": "Peak temperature",
                "type": "number",
                "unit": "°C",
                "kpi": True,
                "kpi_label": "Peak T",
                "kpi_unit": "K",
                "kpi_category": "thermal",
            }
        ],
    }
    state = state_from_descriptor(descriptor)
    payload = build_descriptor_payload(state)
    out = next(o for o in payload["outputs"] if o["name"] == "peak_temperature")
    assert out["kpi"] is True
    assert out["kpi_label"] == "Peak T"
    assert out["kpi_unit"] == "K"
    assert out["kpi_category"] == "thermal"
    # The rebuilt descriptor must re-validate (the Save path does exactly this).
    ToolDescriptor.model_validate({**descriptor, **payload})


def test_unticked_kpi_output_emits_no_kpi_keys() -> None:
    """A numeric output with ``kpi`` unset must not leak the flag or any
    override — the descriptor validator rejects kpi_* without kpi=true."""
    descriptor = {
        "outputs": [
            {"name": "mass", "label": "Mass", "type": "number", "unit": "kg"},
        ],
    }
    payload = build_descriptor_payload(state_from_descriptor(descriptor))
    out = payload["outputs"][0]
    assert "kpi" not in out
    assert "kpi_label" not in out
    assert "kpi_unit" not in out
    assert "kpi_category" not in out


def test_kpi_flag_dropped_when_output_is_not_numeric() -> None:
    """If an output was flagged as a KPI and later switched to a non-number
    type, Save must not emit ``kpi=true`` (the validator forbids it off a
    numeric output) — the flag is scoped to numeric outputs at serialise time."""
    state = state_from_descriptor({})
    field = empty_output_field()
    field.update(
        {
            "name": "report",
            "label": "Report",
            "type": "file",
            "file_type": "html_plot",
            "filename": "outputs/r.html",
            "kpi": True,
            "kpi_label": "stale",
        }
    )
    state["outputs"] = [field]
    out = build_descriptor_payload(state)["outputs"][0]
    assert "kpi" not in out
    assert "kpi_label" not in out


def test_empty_descriptor_round_trip() -> None:
    """Loading and saving an empty descriptor preserves the empty shape."""
    state = state_from_descriptor({})
    payload = build_descriptor_payload(state)
    assert payload["inputs"] == []
    assert payload["outputs"] == []
    assert payload["input_groups"] == []
    assert payload["output_groups"] == []
    assert payload["retain_files"] == []
    assert payload["delete_io_files"] == []
    assert payload["execution_backend"] == {"kind": "subprocess"}
    assert payload["duration_class"] == "medium"
    # Section-behaviour flags default to the system default (collapsed
    # sections + the review gate) when absent from the source.
    assert payload["input_groups_start_collapsed"] is True
    assert payload["require_all_groups_opened"] is True


def test_section_behaviour_flags_round_trip() -> None:
    """A tool that opts out of both flags round-trips faithfully."""
    descriptor = {
        "input_groups_start_collapsed": False,
        "require_all_groups_opened": False,
    }
    state = state_from_descriptor(descriptor)
    assert state["input_groups_start_collapsed"] is False
    assert state["require_all_groups_opened"] is False
    payload = build_descriptor_payload(state)
    assert payload["input_groups_start_collapsed"] is False
    assert payload["require_all_groups_opened"] is False


def test_known_unit_round_trips_via_dropdown_choice() -> None:
    """``unit: "MPa"`` survives state → payload as the same string."""
    descriptor = {
        "inputs": [
            {"name": "pressure", "label": "Pressure", "type": "float", "unit": "MPa"},
        ],
    }
    state = state_from_descriptor(descriptor)
    assert state["inputs"][0]["unit_choice"] == "MPa"
    assert state["inputs"][0]["unit_custom"] == ""
    payload = build_descriptor_payload(state)
    assert payload["inputs"][0]["unit"] == "MPa"


def test_custom_unit_lands_in_unit_custom_field() -> None:
    """An unknown unit string maps to the Custom mode with the literal preserved."""
    descriptor = {
        "inputs": [
            {"name": "exotic", "label": "Exotic", "type": "string", "unit": "furlongs/fortnight"},
        ],
    }
    state = state_from_descriptor(descriptor)
    assert state["inputs"][0]["unit_choice"] == UNIT_CUSTOM_VALUE
    assert state["inputs"][0]["unit_custom"] == "furlongs/fortnight"
    payload = build_descriptor_payload(state)
    assert payload["inputs"][0]["unit"] == "furlongs/fortnight"


def test_none_unit_maps_to_none_sentinel() -> None:
    """A ``unit: None`` descriptor field becomes the ``No unit`` choice."""
    descriptor = {
        "inputs": [{"name": "flag", "label": "Flag", "type": "boolean", "unit": None}],
    }
    state = state_from_descriptor(descriptor)
    assert state["inputs"][0]["unit_choice"] == UNIT_NONE_VALUE
    payload = build_descriptor_payload(state)
    assert payload["inputs"][0]["unit"] is None


def test_sum_check_round_trips() -> None:
    """The wizard's ``sum_check_value`` + ``sum_check_targets`` pair restores the dict."""
    descriptor = {
        "inputs": [
            {
                "name": "fraction_a",
                "label": "Fraction A",
                "type": "float",
                "sum_check": {"value": 100.0, "sum_parameters": ["fraction_b", "fraction_c"]},
            },
        ],
    }
    state = state_from_descriptor(descriptor)
    assert state["inputs"][0]["sum_check_value"] == 100.0
    assert state["inputs"][0]["sum_check_targets"] == ["fraction_b", "fraction_c"]
    payload = build_descriptor_payload(state)
    assert payload["inputs"][0]["sum_check"] == {
        "value": 100.0,
        "sum_parameters": ["fraction_b", "fraction_c"],
    }


def test_input_group_and_layout_round_trip() -> None:
    """Groups + the weights of columns that still exist survive the round trip."""
    descriptor = {
        "input_groups": [
            {"name": "left", "label": "Left", "column": 1, "row": 1, "sub_columns": 2},
            {"name": "right", "label": "Right", "column": 2, "row": 1, "sub_columns": 1},
        ],
        "input_column_layout": {"column_1": 0.4, "column_2": 0.6},
    }
    state = state_from_descriptor(descriptor)
    payload = build_descriptor_payload(state)
    assert payload["input_groups"] == [
        {"name": "left", "label": "Left", "column": 1, "row": 1, "sub_columns": 2},
        {"name": "right", "label": "Right", "column": 2, "row": 1, "sub_columns": 1},
    ]
    assert payload["input_column_layout"] == {"column_1": 0.4, "column_2": 0.6}


def test_layout_drops_weights_for_columns_that_no_longer_exist() -> None:
    """A weight for a column no group uses is pruned on save.

    The layout editor hides itself once the groups collapse to a single
    column, so a stale ``column_2`` weight used to sit in the descriptor
    forever — and the single remaining column then rendered at 40% of
    the width, because the renderer read its weight as a flex-grow
    factor. The renderers normalise now; this stops the dead keys being
    written in the first place.
    """
    descriptor = {
        "input_groups": [
            {"name": "main", "label": "Main", "column": 1, "row": 1, "sub_columns": 2},
        ],
        "input_column_layout": {"column_1": 0.4, "column_2": 0.6},
        "output_groups": [
            {"name": "results", "label": "Results", "column": 1, "row": 1},
        ],
        "output_column_layout": {"column_1": 0.5, "column_2": 0.5},
    }
    payload = build_descriptor_payload(state_from_descriptor(descriptor))
    assert payload["input_column_layout"] == {"column_1": 0.4}
    assert payload["output_column_layout"] == {"column_1": 0.5}


def test_csv_plot_output_round_trips_with_axes() -> None:
    """File outputs with ``csv_plot`` keep their axis wiring fields."""
    descriptor = {
        "outputs": [
            {
                "name": "trace",
                "label": "Trace",
                "type": "file",
                "filename": "results.csv",
                "file_type": "csv_plot",
                "xaxis_column": 1,
                "yaxis_column": 2,
                "plot_title": "Trace over time",
                "xaxis_title": "t (s)",
                "yaxis_title": "amplitude",
            }
        ],
    }
    state = state_from_descriptor(descriptor)
    payload = build_descriptor_payload(state)
    assert payload["outputs"][0]["file_type"] == "csv_plot"
    assert payload["outputs"][0]["xaxis_column"] == 1
    assert payload["outputs"][0]["yaxis_column"] == 2
    assert payload["outputs"][0]["plot_title"] == "Trace over time"


def test_execution_backend_round_trips() -> None:
    """The ``execution_backend`` block keeps its kind + extras."""
    descriptor = {"execution_backend": {"kind": "hpc_batch", "queue": "cpu_short"}}
    state = state_from_descriptor(descriptor)
    payload = build_descriptor_payload(state)
    assert payload["execution_backend"] == {"kind": "hpc_batch", "queue": "cpu_short"}


def test_table_input_round_trips_with_columns_and_default_rows() -> None:
    """A ``type: table`` input keeps its columns + default_rows on round-trip."""
    descriptor = {
        "inputs": [
            {
                "name": "components",
                "label": "Components",
                "type": "table",
                "columns": [
                    {"name": "material", "label": "Material", "type": "string"},
                    {"name": "fraction", "label": "Fraction", "type": "float"},
                ],
                "default_rows": 3,
            }
        ],
    }
    state = state_from_descriptor(descriptor)
    assert state["inputs"][0]["columns"][0]["name"] == "material"
    assert state["inputs"][0]["default_rows"] == 3

    payload = build_descriptor_payload(state)
    assert payload["inputs"][0]["type"] == "table"
    assert payload["inputs"][0]["columns"] == [
        {"name": "material", "label": "Material", "type": "string"},
        {"name": "fraction", "label": "Fraction", "type": "float"},
    ]
    assert payload["inputs"][0]["default_rows"] == 3


def test_table_output_round_trips_with_columns() -> None:
    """A ``type: table`` output keeps its columns on round-trip (no default_rows)."""
    descriptor = {
        "outputs": [
            {
                "name": "summary",
                "label": "Summary",
                "type": "table",
                "columns": [
                    {"name": "metric", "label": "Metric", "type": "string"},
                    {"name": "value", "label": "Value", "type": "float"},
                ],
            }
        ],
    }
    state = state_from_descriptor(descriptor)
    payload = build_descriptor_payload(state)
    assert payload["outputs"][0]["type"] == "table"
    assert len(payload["outputs"][0]["columns"]) == 2
    assert "default_rows" not in payload["outputs"][0]


# --- New-field round-trip tests -----------------------------------------


def test_widget_slider_round_trip() -> None:
    """``widget: slider`` on a numeric input survives state ↔ payload."""
    descriptor = {
        "inputs": [
            {
                "name": "speed",
                "label": "Speed",
                "type": "float",
                "minimum": 0.0,
                "maximum": 100.0,
                "precision": 0.1,
                "widget": "slider",
            }
        ],
    }
    state = state_from_descriptor(descriptor)
    assert state["inputs"][0]["widget"] == "slider"
    payload = build_descriptor_payload(state)
    assert payload["inputs"][0]["widget"] == "slider"


def test_widget_field_default_omitted_from_payload() -> None:
    """The default ``widget="field"`` value is dropped from the payload to keep JSON tidy."""
    descriptor = {
        "inputs": [
            {"name": "n", "label": "N", "type": "integer"},  # no widget specified → "field"
        ],
    }
    state = state_from_descriptor(descriptor)
    payload = build_descriptor_payload(state)
    assert "widget" not in payload["inputs"][0]


def test_choice_widget_radio_round_trip() -> None:
    """``choice_widget: radio`` on a select input survives the round trip."""
    descriptor = {
        "inputs": [
            {
                "name": "mode",
                "label": "Mode",
                "type": "select",
                "options": ["fast", "slow"],
                "choice_widget": "radio",
            }
        ],
    }
    state = state_from_descriptor(descriptor)
    assert state["inputs"][0]["choice_widget"] == "radio"
    payload = build_descriptor_payload(state)
    assert payload["inputs"][0]["choice_widget"] == "radio"


def test_choice_widget_dropdown_default_omitted() -> None:
    """Default ``choice_widget="dropdown"`` is omitted from the payload."""
    descriptor = {
        "inputs": [
            {
                "name": "mode",
                "label": "Mode",
                "type": "select",
                "options": ["a", "b"],
            }
        ],
    }
    state = state_from_descriptor(descriptor)
    payload = build_descriptor_payload(state)
    assert "choice_widget" not in payload["inputs"][0]


def test_file_input_accepted_types_and_size_round_trip() -> None:
    """File-input accept list + max size MB survive."""
    descriptor = {
        "inputs": [
            {
                "name": "upload",
                "label": "Upload",
                "type": "file",
                "accepted_file_types": ["csv", "xlsx"],
                "max_file_size_mb": 50,
            }
        ],
    }
    state = state_from_descriptor(descriptor)
    assert state["inputs"][0]["accepted_file_types"] == ["csv", "xlsx"]
    assert state["inputs"][0]["max_file_size_mb"] == 50
    payload = build_descriptor_payload(state)
    assert payload["inputs"][0]["accepted_file_types"] == ["csv", "xlsx"]
    assert payload["inputs"][0]["max_file_size_mb"] == 50.0


def test_file_input_with_no_max_size() -> None:
    """A file input with no ``max_file_size_mb`` doesn't add the key to the payload."""
    descriptor = {
        "inputs": [
            {"name": "upload", "label": "Upload", "type": "file"},
        ],
    }
    state = state_from_descriptor(descriptor)
    payload = build_descriptor_payload(state)
    assert "max_file_size_mb" not in payload["inputs"][0]
    assert "accepted_file_types" not in payload["inputs"][0]


def test_table_column_extras_round_trip() -> None:
    """Per-column unit / min / max / precision / default survive on table inputs."""
    descriptor = {
        "inputs": [
            {
                "name": "components",
                "label": "Components",
                "type": "table",
                "columns": [
                    {"name": "material", "label": "Material", "type": "string", "default": "steel"},
                    {
                        "name": "fraction",
                        "label": "Fraction",
                        "type": "float",
                        "unit": "%",
                        "minimum": 0.0,
                        "maximum": 100.0,
                        "precision": 0.01,
                        "default": 0.5,
                    },
                ],
                "default_rows": 2,
            }
        ],
    }
    state = state_from_descriptor(descriptor)
    fraction = state["inputs"][0]["columns"][1]
    assert fraction["unit_choice"] == "%"
    assert fraction["minimum"] == 0.0
    assert fraction["maximum"] == 100.0
    assert fraction["precision"] == 0.01
    assert fraction["default"] == 0.5

    payload = build_descriptor_payload(state)
    cols = payload["inputs"][0]["columns"]
    assert cols[0] == {
        "name": "material",
        "label": "Material",
        "type": "string",
        "default": "steel",
    }
    assert cols[1] == {
        "name": "fraction",
        "label": "Fraction",
        "type": "float",
        "unit": "%",
        "minimum": 0.0,
        "maximum": 100.0,
        "precision": 0.01,
        "default": 0.5,
    }


def test_sum_check_targets_persist_as_list_of_two_or_more() -> None:
    """Multiselect sibling list with three targets round-trips intact."""
    descriptor = {
        "inputs": [
            {
                "name": "p1",
                "label": "P1",
                "type": "float",
                "sum_check": {
                    "value": 100.0,
                    "sum_parameters": ["p1", "p2", "p3"],
                },
            }
        ],
    }
    state = state_from_descriptor(descriptor)
    assert state["inputs"][0]["sum_check_targets"] == ["p1", "p2", "p3"]
    payload = build_descriptor_payload(state)
    assert payload["inputs"][0]["sum_check"]["sum_parameters"] == ["p1", "p2", "p3"]


# --- §16.16 per-input dependency round-trip ----------------------------


def _two_inputs(*extra_inputs: dict) -> dict:
    base = [
        {"name": "a", "label": "A", "type": "integer"},
        {"name": "b", "label": "B", "type": "integer"},
    ]
    return {"inputs": base + list(extra_inputs)}


def test_no_dependencies_yields_empty_list() -> None:
    payload = build_descriptor_payload(state_from_descriptor(_two_inputs()))
    assert payload["dependencies"] == []


def test_hide_rule_round_trips_exactly() -> None:
    descriptor = _two_inputs()
    descriptor["dependencies"] = [
        {
            "when": "a == 1",
            "effects": [{"kind": "hide", "fields": ["b"], "on_hide": "keep_default"}],
        }
    ]
    state = state_from_descriptor(descriptor)
    # The rule lands on the input it affects (b), not a.
    assert state["inputs"][0]["dependencies"] == []
    (rule,) = state["inputs"][1]["dependencies"]
    assert rule["when"] == "a == 1"
    (outcome,) = rule["outcomes"]
    assert outcome["effect"] == "hide"
    assert outcome["on_hide"] == "keep_default"
    payload = build_descriptor_payload(state)
    assert payload["dependencies"] == descriptor["dependencies"]


def test_multiple_outcomes_on_one_rule_round_trip() -> None:
    """A rule with several effects on its field round-trips as one Dependency.

    Regression for the "if a == 1 then b = 2 *and* b read-only" ask:
    one ``when`` carries an ordered effect list, and reload groups the
    descriptor rule's effects back into a single multi-outcome editor
    rule (the exact inverse of the collect step).
    """
    descriptor = _two_inputs()
    descriptor["dependencies"] = [
        {
            "when": "a == 1",
            "effects": [
                {"kind": "set_value", "field": "b", "value": 2},
                {"kind": "disable", "field": "b"},
            ],
        }
    ]
    state = state_from_descriptor(descriptor)
    (rule,) = state["inputs"][1]["dependencies"]
    assert rule["when"] == "a == 1"
    assert [o["effect"] for o in rule["outcomes"]] == ["set_value", "disable"]
    assert rule["outcomes"][0]["value"] == "2"
    payload = build_descriptor_payload(state)
    assert payload["dependencies"] == [
        {
            "when": "a == 1",
            "effects": [
                {"kind": "set_value", "field": "b", "value": 2},
                {"kind": "disable", "field": "b"},
            ],
        }
    ]


def test_outcome_with_no_formable_effect_is_dropped_but_rule_kept() -> None:
    """A blank ``set_options`` outcome is skipped; the rule's other outcome stays."""
    state = state_from_descriptor(_two_inputs())
    state["inputs"][1]["dependencies"] = [
        {
            "when": "a == 1",
            "outcomes": [
                {"effect": "set_options", "options_csv": ""},  # no options → dropped
                {"effect": "disable"},
            ],
        }
    ]
    deps = build_descriptor_payload(state)["dependencies"]
    assert deps == [{"when": "a == 1", "effects": [{"kind": "disable", "field": "b"}]}]


def test_set_value_coerces_to_field_type() -> None:
    descriptor = _two_inputs()
    descriptor["dependencies"] = [
        {"when": "a == 1", "effects": [{"kind": "set_value", "field": "b", "value": 2}]}
    ]
    payload = build_descriptor_payload(state_from_descriptor(descriptor))
    eff = payload["dependencies"][0]["effects"][0]
    assert eff == {"kind": "set_value", "field": "b", "value": 2}
    assert isinstance(eff["value"], int)


def test_set_min_max_drops_absent_bound() -> None:
    descriptor = _two_inputs()
    descriptor["dependencies"] = [
        {"when": "a == 1", "effects": [{"kind": "set_min_max", "field": "b", "minimum": 5}]}
    ]
    payload = build_descriptor_payload(state_from_descriptor(descriptor))
    assert payload["dependencies"][0]["effects"][0] == {
        "kind": "set_min_max",
        "field": "b",
        "minimum": 5,
    }


def test_formula_min_max_round_trips() -> None:
    descriptor = _two_inputs()
    descriptor["dependencies"] = [
        {
            "when": "True",
            "effects": [{"kind": "set_min_max", "field": "b", "minimum_expr": "a"}],
        }
    ]
    state = state_from_descriptor(descriptor)
    # Distributed onto input b as a formula-source minimum, blank "always".
    rule = state["inputs"][1]["dependencies"][0]
    assert rule["when"] == ""
    assert rule["outcomes"][0]["minimum_source"] == "formula"
    assert rule["outcomes"][0]["minimum_expr"] == "a"
    # And rebuilds to the same effect (with the always-rule stored as True).
    payload = build_descriptor_payload(state)
    assert payload["dependencies"] == [
        {"when": "True", "effects": [{"kind": "set_min_max", "field": "b", "minimum_expr": "a"}]}
    ]


def test_set_value_formula_round_trips() -> None:
    descriptor = _two_inputs()
    descriptor["dependencies"] = [
        {"when": "a == 1", "effects": [{"kind": "set_value", "field": "b", "value_expr": "a * 2"}]}
    ]
    payload = build_descriptor_payload(state_from_descriptor(descriptor))
    assert payload["dependencies"][0]["effects"][0] == {
        "kind": "set_value",
        "field": "b",
        "value_expr": "a * 2",
    }


def test_require_round_trips() -> None:
    descriptor = _two_inputs()
    descriptor["dependencies"] = [
        {
            "when": "True",
            "effects": [
                {"kind": "require", "field": "b", "expr": "b > a", "message": "B must exceed A."}
            ],
        }
    ]
    state = state_from_descriptor(descriptor)
    outcome = state["inputs"][1]["dependencies"][0]["outcomes"][0]
    assert outcome["effect"] == "require"
    assert outcome["require_expr"] == "b > a"
    assert outcome["require_message"] == "B must exceed A."
    payload = build_descriptor_payload(state)
    assert payload["dependencies"][0]["effects"][0] == {
        "kind": "require",
        "field": "b",
        "expr": "b > a",
        "message": "B must exceed A.",
    }


def test_require_without_message_omits_it() -> None:
    descriptor = _two_inputs()
    descriptor["dependencies"] = [
        {"when": "True", "effects": [{"kind": "require", "field": "b", "expr": "b > a"}]}
    ]
    payload = build_descriptor_payload(state_from_descriptor(descriptor))
    assert payload["dependencies"][0]["effects"][0] == {
        "kind": "require",
        "field": "b",
        "expr": "b > a",
    }


def test_set_options_round_trips() -> None:
    descriptor = {
        "inputs": [
            {"name": "a", "label": "A", "type": "integer"},
            {
                "name": "finish",
                "label": "Finish",
                "type": "select",
                "options": ["raw", "matte"],
            },
        ],
        "dependencies": [
            {
                "when": "a == 1",
                "effects": [{"kind": "set_options", "field": "finish", "options": ["x", "y"]}],
            }
        ],
    }
    payload = build_descriptor_payload(state_from_descriptor(descriptor))
    assert payload["dependencies"][0]["effects"][0]["options"] == ["x", "y"]


def test_multi_target_show_splits_to_equivalent_rules() -> None:
    descriptor = _two_inputs({"name": "c", "label": "C", "type": "integer"})
    descriptor["dependencies"] = [
        {"when": "a == 1", "effects": [{"kind": "show", "fields": ["b", "c"]}]}
    ]
    payload = build_descriptor_payload(state_from_descriptor(descriptor))
    # One Dependency per affected field; behaviourally identical.
    targets = sorted(d["effects"][0]["fields"][0] for d in payload["dependencies"])
    assert targets == ["b", "c"]
    assert all(d["when"] == "a == 1" for d in payload["dependencies"])


def test_blank_when_means_always() -> None:
    # A blank condition is the "always apply" shorthand — stored as the DSL
    # literal ``True`` so the rule always fires (e.g. an unconditional
    # "B's minimum is A"). The reverse map turns ``True`` back into blank.
    state = state_from_descriptor(_two_inputs())
    state["inputs"][1]["dependencies"] = [{"when": "", "effect": "hide", "on_hide": "drop"}]
    deps = build_descriptor_payload(state)["dependencies"]
    assert deps == [
        {"when": "True", "effects": [{"kind": "hide", "fields": ["b"], "on_hide": "drop"}]}
    ]


def test_empty_rule_with_no_effect_is_omitted() -> None:
    # A rule that forms no valid effect is still dropped entirely.
    state = state_from_descriptor(_two_inputs())
    state["inputs"][1]["dependencies"] = [{"when": "a == 1", "outcomes": []}]
    assert build_descriptor_payload(state)["dependencies"] == []


# --- Render smoke (catches NiceGUI build errors in the card layout) -----


@pytest.fixture
def nicegui_client() -> object:
    client = Client(page("/"))
    with client:
        yield client


def test_inputs_tab_renders_card_layout(nicegui_client: object) -> None:  # noqa: ARG001
    """Every input type + a dependency rule + sum-check render without error."""
    descriptor = {
        "input_groups": [{"name": "g", "label": "Group", "column": 1, "row": 1, "sub_columns": 2}],
        "inputs": [
            {"name": "s", "label": "S", "type": "string", "group": "g"},
            {"name": "n", "label": "N", "type": "integer", "minimum": 0},
            {"name": "f", "label": "F", "type": "float"},
            {"name": "b", "label": "B", "type": "boolean"},
            {"name": "sel", "label": "Sel", "type": "select", "options": ["x", "y"]},
            {
                "name": "ms",
                "label": "MS",
                "type": "multiselect",
                "options": ["a", "b"],
            },
            {"name": "fl", "label": "Fl", "type": "file"},
            {
                "name": "t",
                "label": "T",
                "type": "table",
                "columns": [{"name": "c1", "label": "C1", "type": "string"}],
            },
            {
                "name": "p1",
                "label": "P1",
                "type": "float",
                "sum_check": {"value": 100.0, "sum_parameters": ["p1", "p2"]},
            },
            {"name": "p2", "label": "P2", "type": "float"},
        ],
        "dependencies": [
            {
                "when": "n == 1",
                "effects": [{"kind": "hide", "fields": ["s"], "on_hide": "keep_default"}],
            },
            {
                "when": "b == True",
                "effects": [{"kind": "set_value", "field": "f", "value": 1.5}],
            },
            # Formula-driven bound + value + a validity rule — exercises the
            # new outcome param widgets (source toggle, require fields).
            {
                "when": "True",
                "effects": [{"kind": "set_min_max", "field": "f", "minimum_expr": "n"}],
            },
            {"when": "True", "effects": [{"kind": "set_value", "field": "p2", "value_expr": "p1"}]},
            {"when": "True", "effects": [{"kind": "require", "field": "f", "expr": "f >= n"}]},
        ],
    }
    state = state_from_descriptor(descriptor)
    # Must not raise while building the card / sub-card widget tree.
    render_descriptor_wizard(
        state,
        wiring=UpstreamWiringContext(
            all_tools={"other": "Other Tool"}, candidates={"other": "Other Tool"}
        ),
    )
    # The hide rule landed on input "s"; the set_value on "f".
    s_field = next(f for f in state["inputs"] if f["name"] == "s")
    assert s_field["dependencies"][0]["outcomes"][0]["effect"] == "hide"
    # The formula min-max distributed onto "f" as a formula-source outcome.
    f_field = next(f for f in state["inputs"] if f["name"] == "f")
    f_effects = {o["effect"] for r in f_field["dependencies"] for o in r["outcomes"]}
    assert {"set_min_max", "require"} <= f_effects


# --- material input type (ADR-0007) -----------------------------------


def test_material_input_round_trips_and_validates() -> None:
    """A material input survives state ⇄ payload AND the produced descriptor
    passes ``ToolDescriptor`` validation (which forbids options/default)."""
    descriptor = {
        "inputs": [
            {
                "name": "fluid",
                "label": "Working fluid",
                "type": "material",
                "material_class": "Fluid",
                "required_properties": ["thermal_conductivity", "density"],
            }
        ],
    }
    state = state_from_descriptor(descriptor)
    field_state = state["inputs"][0]
    assert field_state["type"] == "material"
    assert field_state["material_class"] == "Fluid"
    assert field_state["required_properties"] == ["thermal_conductivity", "density"]
    assert field_state["required_properties_csv"] == "thermal_conductivity, density"

    payload = build_descriptor_payload(state)
    inp = payload["inputs"][0]
    assert inp["type"] == "material"
    assert inp["material_class"] == "Fluid"
    assert inp["required_properties"] == ["thermal_conductivity", "density"]
    # The wizard must NOT emit options / default on a material field.
    assert inp["options"] is None
    assert inp["default"] is None

    # The produced descriptor passes full ToolDescriptor validation
    # (which runs _validate_material_constraints).
    full = {
        "alias": "demo",
        "name": "Demo",
        "version": "1.0.0",
        "description": "Demo.",
        "main_script": "main.py",
        **payload,
    }
    td = ToolDescriptor.model_validate(full)
    assert td.inputs[0].type == "material"
    assert td.inputs[0].material_class == "Fluid"
    assert td.inputs[0].required_properties == ["thermal_conductivity", "density"]


def test_required_properties_csv_flush_updates_underlying_list() -> None:
    state = state_from_descriptor(
        {"inputs": [{"name": "f", "label": "F", "type": "material", "material_class": "Fluid"}]}
    )
    field = state["inputs"][0]
    # User-typed CSV with whitespace + an empty token → trimmed + dropped.
    field["required_properties_csv"] = "k1, k2 , ,k3"
    _flush_required_properties_csv(field)
    assert field["required_properties"] == ["k1", "k2", "k3"]


def test_switching_away_from_material_clears_material_fields() -> None:
    state = state_from_descriptor(
        {
            "inputs": [
                {
                    "name": "f",
                    "label": "F",
                    "type": "material",
                    "material_class": "Fluid",
                    "required_properties": ["x"],
                }
            ]
        }
    )
    field = state["inputs"][0]
    field["type"] = "string"
    _normalise_type_dependent_state(field)
    assert field["material_class"] is None
    assert field["required_properties"] == []
    assert field["required_properties_csv"] == ""


# --- Presets tab (wizard-authored tool presets) ------------------------


def test_empty_state_starts_with_no_presets() -> None:
    """A fresh wizard state has an empty presets list — symmetric with inputs."""
    state = empty_state()
    assert state["presets"] == []


def test_empty_preset_has_blank_name_description_and_values() -> None:
    """``empty_preset()`` produces the blank shape the Add Preset button uses."""
    preset = empty_preset()
    assert preset == {"name": "", "description": "", "values": {}}


def test_presets_round_trip_through_descriptor() -> None:
    """A descriptor with presets survives state → payload unchanged.

    Pins the wizard's authoring shape against the descriptor's storage
    shape — both use ``{name, description, values}`` so the round-trip
    is a defensive copy, not a reshape.
    """
    descriptor = {
        "inputs": [
            {"name": "temperature", "label": "Temperature", "type": "float", "unit": "K"},
            {"name": "material", "label": "Material", "type": "select", "options": ["A", "B"]},
        ],
        "presets": [
            {
                "name": "Cold start",
                "description": "Room-temp baseline",
                "values": {"temperature": 293.15, "material": "A"},
            },
            {
                "name": "Hot run",
                "description": "",
                "values": {"temperature": 873.15},
            },
        ],
    }
    state = state_from_descriptor(descriptor)
    payload = build_descriptor_payload(state)
    assert payload["presets"] == descriptor["presets"]


def test_presets_strip_inputs_no_longer_in_descriptor() -> None:
    """A preset that names an input the descriptor no longer has drops it.

    Mirrors the legacy file loader's tolerant behaviour — the operator
    may rename / delete an input after authoring a preset; the runtime
    resolver already ignores unmatched keys, and we keep the persisted
    descriptor clean at save time.
    """
    state = state_from_descriptor(
        {
            "inputs": [{"name": "kept", "label": "Kept", "type": "string"}],
            "presets": [
                {
                    "name": "Stale",
                    "values": {"kept": "yes", "removed": "ghost", "also_removed": 42},
                }
            ],
        }
    )
    payload = build_descriptor_payload(state)
    assert payload["presets"][0]["values"] == {"kept": "yes"}


def test_presets_strip_empty_values() -> None:
    """Empty / None / empty-list values mean 'preset doesn't override this'.

    The runtime resolver picks descriptor defaults for inputs absent
    from the preset's values map. To keep the persisted descriptor
    minimal we strip these sentinels at save time.
    """
    state = state_from_descriptor(
        {
            "inputs": [
                {"name": "a", "label": "A", "type": "string"},
                {"name": "b", "label": "B", "type": "float"},
                {"name": "c", "label": "C", "type": "multiselect", "options": ["x", "y"]},
                {"name": "d", "label": "D", "type": "string"},
            ],
            "presets": [
                {
                    "name": "P",
                    "values": {
                        "a": "kept",
                        "b": None,
                        "c": [],
                        "d": "",
                    },
                }
            ],
        }
    )
    payload = build_descriptor_payload(state)
    assert payload["presets"][0]["values"] == {"a": "kept"}


def test_preset_round_trip_validates_against_tool_descriptor() -> None:
    """A wizard-authored preset survives ``ToolDescriptor`` validation."""
    state = empty_state()
    state["inputs"] = [
        {
            "name": "x",
            "label": "X",
            "type": "integer",
            "unit_choice": UNIT_NONE_VALUE,
            "unit_custom": "",
        }
    ]
    state["presets"] = [{"name": "Default", "description": "Starting point", "values": {"x": 10}}]
    payload = build_descriptor_payload(state)
    full = {
        "alias": "demo",
        "name": "Demo",
        "version": "1.0.0",
        "description": "Demo.",
        "main_script": "main.py",
        **payload,
    }
    td = ToolDescriptor.model_validate(full)
    assert len(td.presets) == 1
    assert td.presets[0].name == "Default"
    assert td.presets[0].description == "Starting point"
    assert td.presets[0].values == {"x": 10}


def test_presets_tab_renders_for_a_tool_with_inputs(nicegui_client: object) -> None:  # noqa: ARG001
    """Smoke-test that the Presets tab builds its widget tree without raising.

    Covers the per-type editor dispatch across every input type the
    tab supports (string / integer / float / boolean / select /
    multiselect) plus the disabled-row case (file / table / material).
    """
    descriptor = {
        "inputs": [
            {"name": "s", "label": "S", "type": "string"},
            {"name": "i", "label": "I", "type": "integer", "minimum": 0, "maximum": 100},
            {"name": "f", "label": "F", "type": "float", "precision": 0.1},
            {"name": "b", "label": "B", "type": "boolean"},
            {"name": "sel", "label": "Sel", "type": "select", "options": ["x", "y"]},
            {"name": "ms", "label": "MS", "type": "multiselect", "options": ["a", "b"]},
            {"name": "fl", "label": "Fl", "type": "file"},
            {
                "name": "t",
                "label": "T",
                "type": "table",
                "columns": [{"name": "c", "label": "C", "type": "string"}],
            },
        ],
        "presets": [
            {
                "name": "Demo",
                "description": "Demo preset",
                "values": {"s": "hello", "i": 5, "b": True, "sel": "x", "ms": ["a"]},
            }
        ],
    }
    state = state_from_descriptor(descriptor)
    # Must not raise while building the tabs + per-type editor widgets.
    render_descriptor_wizard(state, wiring=UpstreamWiringContext())


def test_presets_tab_renders_with_no_inputs_yet(nicegui_client: object) -> None:  # noqa: ARG001
    """The Presets tab handles the empty-inputs case gracefully.

    An operator who opens the Presets tab before defining any inputs
    on the Inputs tab should see a helpful hint rather than a crash.
    """
    state = empty_state()
    state["presets"] = [{"name": "Empty preset", "description": "", "values": {}}]
    render_descriptor_wizard(state, wiring=UpstreamWiringContext())


def test_preset_input_label_text_and_unit_split_cleanly() -> None:
    """The persistent label renderer needs label + unit as separate pieces.

    Pins ``_preset_input_label_text`` (label only) and
    ``_preset_input_unit_str`` (unit only) so the live presets tab
    can style the two independently (bold label, muted unit) without
    re-parsing a combined string.
    """
    state = state_from_descriptor(
        {
            "inputs": [
                {"name": "t", "label": "Temperature", "type": "float", "unit": "K"},
                {"name": "n", "label": "Count", "type": "integer"},
                {"name": "u", "label": "Untyped", "type": "string", "unit": "custom_widget"},
            ],
        }
    )
    by_name = {f["name"]: f for f in state["inputs"]}
    # Known unit → both pieces resolve.
    assert _preset_input_label_text(by_name["t"]) == "Temperature"
    assert _preset_input_unit_str(by_name["t"]) == "K"
    assert _preset_input_label(by_name["t"]) == "Temperature (K)"
    # No unit → text-only label, no unit string.
    assert _preset_input_label_text(by_name["n"]) == "Count"
    assert _preset_input_unit_str(by_name["n"]) is None
    assert _preset_input_label(by_name["n"]) == "Count"
    # Custom unit (a free-form string the operator typed) round-trips.
    custom = by_name["u"]
    assert _preset_input_unit_str(custom) == "custom_widget"
    assert _preset_input_label(custom) == "Untyped (custom_widget)"


def test_group_inputs_for_presets_orders_groups_by_column_row() -> None:
    """The presets tab visits groups in the same ``(column, row)`` order the live form does.

    Mirrors the input form's layout planner so the operator's mental
    model lines up — section 1, then section 2, then ungrouped at the
    end. Inputs whose ``group`` references a name that doesn't exist
    in ``input_groups`` fall through to the Ungrouped bucket.
    """
    state = state_from_descriptor(
        {
            "input_groups": [
                {"name": "b", "label": "Section B", "column": 2, "row": 1},
                {"name": "a", "label": "Section A", "column": 1, "row": 1},
                {"name": "c", "label": "Section C", "column": 1, "row": 2},
            ],
            "inputs": [
                {"name": "in_b", "label": "InB", "type": "string", "group": "b"},
                {"name": "in_a", "label": "InA", "type": "string", "group": "a"},
                {"name": "in_c", "label": "InC", "type": "string", "group": "c"},
                {"name": "in_orphan", "label": "Orphan", "type": "string", "group": "nope"},
                {"name": "in_none", "label": "NoGroup", "type": "string"},
            ],
        }
    )
    blocks = _group_inputs_for_presets(state)
    # Order: (1,1) Section A, (1,2) Section C, (2,1) Section B, then Ungrouped.
    labels = [label for label, _ in blocks]
    assert labels == ["Section A", "Section C", "Section B", "Ungrouped"]
    # Ungrouped collects both genuinely-ungrouped + dangling-reference inputs.
    ungrouped_names = [f["name"] for f in blocks[-1][1]]
    assert set(ungrouped_names) == {"in_orphan", "in_none"}


def test_group_inputs_for_presets_handles_no_groups_defined() -> None:
    """A descriptor that declared inputs but no groups → single Ungrouped block."""
    state = state_from_descriptor(
        {
            "inputs": [
                {"name": "a", "label": "A", "type": "string"},
                {"name": "b", "label": "B", "type": "integer"},
            ],
        }
    )
    blocks = _group_inputs_for_presets(state)
    assert len(blocks) == 1
    assert blocks[0][0] == "Ungrouped"
    assert [f["name"] for f in blocks[0][1]] == ["a", "b"]


def test_group_inputs_for_presets_returns_empty_when_no_inputs() -> None:
    """Operator hasn't defined any inputs yet → no blocks (the tab's hint surfaces instead)."""
    state = empty_state()
    assert _group_inputs_for_presets(state) == []


def test_presets_tab_renders_with_grouped_inputs(nicegui_client: object) -> None:  # noqa: ARG001
    """The presets tab builds its sub-cards without raising when inputs are grouped.

    Covers the per-group sub-card path that's the user-visible
    behaviour change for this revision. One group with two inputs +
    one ungrouped input should produce two sub-cards.
    """
    descriptor = {
        "input_groups": [
            {"name": "thermal", "label": "Thermal", "column": 1, "row": 1},
        ],
        "inputs": [
            {"name": "t", "label": "Temperature", "type": "float", "unit": "K", "group": "thermal"},
            {"name": "k", "label": "Conductivity", "type": "float", "group": "thermal"},
            {"name": "n", "label": "Count", "type": "integer"},  # ungrouped
        ],
        "presets": [
            {
                "name": "Hot",
                "description": "",
                "values": {"t": 800.0, "k": 0.5, "n": 10},
            }
        ],
    }
    state = state_from_descriptor(descriptor)
    render_descriptor_wizard(state, wiring=UpstreamWiringContext())


# --- the surrogate uncertainty link survives editing -----------------------


def _surrogate_descriptor(group: str | None = None) -> dict:
    return {
        "alias": "surrogate_x_1",
        "name": "X surrogate",
        "version": "1.0.0",
        "short_description": "x",
        "main_script": "orchestrator.py",
        "output_groups": (
            [{"name": "thermal", "label": "Thermal", "column": 1, "row": 1}] if group else []
        ),
        "outputs": [
            {"name": "mass", "label": "Mass", "type": "number", "unit": "g", "group": group},
            {
                "name": "mass_std",
                "label": "Mass (1-sigma)",
                "type": "number",
                "uncertainty_for": "mass",
            },
        ],
    }


def test_the_uncertainty_link_survives_the_editor() -> None:
    """The reported bug: grouping outputs lost ``value ± std`` reporting.

    Grouping was a red herring — the allowlist in ``_output_to_descriptor``
    dropped ``uncertainty_for`` on **any** save, so merely opening the editor
    to add a group unpaired every surrogate uncertainty and turned each std
    into an ordinary row of its own.
    """
    descriptor = _surrogate_descriptor()
    payload = build_descriptor_payload(state_from_descriptor(descriptor))
    std = next(o for o in payload["outputs"] if o["name"] == "mass_std")
    assert std["uncertainty_for"] == "mass"
    ToolDescriptor.model_validate({**descriptor, **payload})


def test_the_uncertainty_link_survives_being_grouped() -> None:
    """Grouping the *parent* must not disturb the pairing."""
    descriptor = _surrogate_descriptor(group="thermal")
    payload = build_descriptor_payload(state_from_descriptor(descriptor))
    by_name = {o["name"]: o for o in payload["outputs"]}
    assert by_name["mass"]["group"] == "thermal"
    assert by_name["mass_std"]["uncertainty_for"] == "mass"
    ToolDescriptor.model_validate({**descriptor, **payload})


def test_a_non_uncertainty_output_emits_no_link() -> None:
    """The key must not leak onto ordinary outputs."""
    payload = build_descriptor_payload(state_from_descriptor(_surrogate_descriptor()))
    mass = next(o for o in payload["outputs"] if o["name"] == "mass")
    assert "uncertainty_for" not in mass
