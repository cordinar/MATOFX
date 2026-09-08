"""Tests for ``matofx.tools.forms.seeders``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nicegui import Client
from nicegui.page import page

from matofx.tools.descriptor import InputField, ToolDescriptor
from matofx.tools.exec.contracts import InputsDocument, InputValue
from matofx.tools.forms import (
    Preset,
    from_descriptor_defaults,
    from_previous_job_inputs,
    load_presets,
    render_form,
)


def _descriptor(**fields: Any) -> ToolDescriptor:
    base: dict[str, Any] = {
        "alias": "demo",
        "name": "Demo",
        "version": "1.0.0",
        "description": "Demo.",
        "main_script": "main.py",
    }
    base.update(fields)
    return ToolDescriptor(**base)


# --- load_presets --------------------------------------------------------


def test_load_presets_reads_v2_toml_with_name_description_and_values(tmp_path: Path) -> None:
    (tmp_path / "presets.toml").write_text(
        '[[preset]]\nname = "Glass at 200°C"\ndescription = "Default glass"\n'
        "[preset.values]\n"
        "object_temperature = 200\n"
        "density_left = 2500\n",
        encoding="utf-8",
    )
    d = _descriptor()
    presets = load_presets(tmp_path, d)
    assert presets == [
        Preset(
            name="Glass at 200°C",
            description="Default glass",
            values={"object_temperature": 200, "density_left": 2500},
        )
    ]


def test_load_presets_reads_legacy_alias_presets_json_when_toml_absent(tmp_path: Path) -> None:
    # The legacy format is a flat ``{name: {fields}}`` dict; description
    # is unavailable so it lands as an empty string.
    (tmp_path / "demo_presets.json").write_text(
        '{"Preset A": {"x": 1, "y": 2}, "Preset B": {"x": 9}}',
        encoding="utf-8",
    )
    d = _descriptor(presets_filename="demo_presets.json")
    presets = load_presets(tmp_path, d)
    assert {p.name for p in presets} == {"Preset A", "Preset B"}
    a = next(p for p in presets if p.name == "Preset A")
    assert a.description == ""
    assert a.values == {"x": 1, "y": 2}


def test_v2_toml_takes_precedence_over_legacy_json(tmp_path: Path) -> None:
    (tmp_path / "presets.toml").write_text(
        '[[preset]]\nname = "TOML"\n[preset.values]\nx = 1\n', encoding="utf-8"
    )
    (tmp_path / "demo_presets.json").write_text('{"JSON": {"x": 99}}', encoding="utf-8")
    d = _descriptor(presets_filename="demo_presets.json")
    presets = load_presets(tmp_path, d)
    # Only the TOML entry comes back — we don't merge across formats.
    assert [p.name for p in presets] == ["TOML"]


def test_load_presets_returns_empty_when_no_file_present(tmp_path: Path) -> None:
    d = _descriptor()
    assert load_presets(tmp_path, d) == []


def test_load_presets_returns_empty_for_malformed_toml(tmp_path: Path) -> None:
    # A half-edited preset file shouldn't crash the workspace; surface
    # the issue through `matofx tool validate` instead.
    (tmp_path / "presets.toml").write_text("not = valid = toml", encoding="utf-8")
    d = _descriptor()
    assert load_presets(tmp_path, d) == []


def test_load_presets_skips_entries_missing_name(tmp_path: Path) -> None:
    (tmp_path / "presets.toml").write_text(
        '[[preset]]\n[preset.values]\nx = 1\n[[preset]]\nname = "OK"\n[preset.values]\ny = 2\n',
        encoding="utf-8",
    )
    d = _descriptor()
    presets = load_presets(tmp_path, d)
    assert [p.name for p in presets] == ["OK"]


def test_load_presets_skips_entries_with_non_dict_values(tmp_path: Path) -> None:
    (tmp_path / "presets.toml").write_text(
        '[[preset]]\nname = "Bad"\nvalues = "not a table"\n'
        '[[preset]]\nname = "Good"\n[preset.values]\nx = 1\n',
        encoding="utf-8",
    )
    d = _descriptor()
    presets = load_presets(tmp_path, d)
    assert [p.name for p in presets] == ["Good"]


def test_legacy_json_skips_non_dict_entries(tmp_path: Path) -> None:
    (tmp_path / "demo_presets.json").write_text(
        '{"Bad": "not a dict", "Good": {"x": 1}}', encoding="utf-8"
    )
    d = _descriptor(presets_filename="demo_presets.json")
    presets = load_presets(tmp_path, d)
    assert [p.name for p in presets] == ["Good"]


# --- Descriptor-authored presets (wizard's Presets tab) -----------------


def test_descriptor_presets_take_precedence_over_bundle_files(tmp_path: Path) -> None:
    """The wizard-authored ``descriptor.presets`` wins over any bundle file.

    Once a tool has been re-saved through the wizard the bundle's
    legacy presets file is stale — surfacing the wizard's edits is the
    expected behaviour.
    """
    (tmp_path / "presets.toml").write_text(
        '[[preset]]\nname = "FromFile"\n[preset.values]\nx = 99\n',
        encoding="utf-8",
    )
    d = _descriptor(
        presets=[
            {
                "name": "FromDescriptor",
                "description": "Authored on the wizard",
                "values": {"x": 1, "y": 2},
            }
        ]
    )
    presets = load_presets(tmp_path, d)
    assert [p.name for p in presets] == ["FromDescriptor"]
    assert presets[0].description == "Authored on the wizard"
    assert presets[0].values == {"x": 1, "y": 2}


def test_load_presets_falls_back_to_bundle_files_when_descriptor_presets_empty(
    tmp_path: Path,
) -> None:
    """An empty ``descriptor.presets`` list doesn't shadow legacy bundle files.

    Tools that haven't been re-saved through the wizard yet keep
    working: ``presets = []`` is treated as "no in-descriptor presets,
    look in the bundle".
    """
    (tmp_path / "presets.toml").write_text(
        '[[preset]]\nname = "Legacy"\n[preset.values]\nx = 1\n', encoding="utf-8"
    )
    d = _descriptor(presets=[])
    presets = load_presets(tmp_path, d)
    assert [p.name for p in presets] == ["Legacy"]


def test_descriptor_presets_returned_as_runtime_preset_dataclass(tmp_path: Path) -> None:
    """The wizard's Pydantic shape converts into the runtime ``Preset`` dataclass.

    The two shapes share the same three fields; the convertor in
    ``load_presets`` defensively copies the ``values`` dict so an
    end-user form mutation can't reach back into the descriptor.
    """
    d = _descriptor(presets=[{"name": "A", "description": "first", "values": {"x": 1}}])
    result = load_presets(tmp_path, d)
    assert result == [Preset(name="A", description="first", values={"x": 1})]
    # Mutating the returned values must not bleed back into the descriptor.
    result[0].values["x"] = 999
    assert d.presets[0].values == {"x": 1}


# --- from_previous_job_inputs --------------------------------------------


def test_from_previous_job_inputs_handles_raw_dict_shape() -> None:
    # The API layer typically hands us the parsed JSON dict, not the
    # typed InputsDocument — both must work.
    raw = {
        "schema_version": 1,
        "job_id": 42,
        "tool_alias": "demo",
        "tool_version": "1.0.0",
        "inputs": [
            {"name": "x", "type": "float", "label": "X", "value": 3.14},
            {"name": "y", "type": "string", "label": "Y", "value": "hello"},
        ],
    }
    assert from_previous_job_inputs(raw) == {"x": 3.14, "y": "hello"}


def test_from_previous_job_inputs_handles_typed_document() -> None:
    doc = InputsDocument(
        job_id=1,
        tool_alias="demo",
        tool_version="1.0.0",
        inputs=[
            InputValue(name="x", label="X", type="float", value=3.14),
            InputValue(name="y", label="Y", type="string", value="hello"),
        ],
    )
    assert from_previous_job_inputs(doc) == {"x": 3.14, "y": "hello"}


def test_from_previous_job_inputs_skips_entries_missing_name() -> None:
    raw = {
        "inputs": [
            {"value": 1},  # no name → skipped
            {"name": "x", "value": 2},
        ]
    }
    assert from_previous_job_inputs(raw) == {"x": 2}


def test_from_previous_job_inputs_returns_empty_for_malformed_doc() -> None:
    assert from_previous_job_inputs({}) == {}
    assert from_previous_job_inputs({"inputs": "not a list"}) == {}
    assert from_previous_job_inputs({"inputs": ["not a dict"]}) == {}


def test_from_previous_job_inputs_propagates_none_values() -> None:
    # Optional fields legitimately serialise as null; preserve that.
    raw = {"inputs": [{"name": "comment", "value": None}]}
    assert from_previous_job_inputs(raw) == {"comment": None}


# --- from_descriptor_defaults --------------------------------------------


def test_from_descriptor_defaults_includes_only_inputs_with_defaults() -> None:
    d = _descriptor(
        inputs=[
            InputField(name="t", label="T", type="float", default=25),
            InputField(name="active", label="Active", type="boolean", default=True),
            # No default → excluded.
            InputField(name="comment", label="Comment", type="string", required=False),
        ]
    )
    assert from_descriptor_defaults(d) == {"t": 25, "active": True}


def test_from_descriptor_defaults_handles_no_inputs() -> None:
    assert from_descriptor_defaults(_descriptor()) == {}


def test_from_descriptor_defaults_preserves_list_default() -> None:
    d = _descriptor(
        inputs=[
            InputField(
                name="tags",
                label="Tags",
                type="multiselect",
                options=["a", "b", "c"],
                default=["a", "c"],
                required=False,
            ),
        ]
    )
    assert from_descriptor_defaults(d) == {"tags": ["a", "c"]}


# --- Round-trip through FormHandle.seed (light integration) --------------


def test_preset_round_trips_through_form_handle_seed(tmp_path: Path) -> None:
    """A loaded preset's values seed a FormHandle correctly."""
    (tmp_path / "presets.toml").write_text(
        '[[preset]]\nname = "P"\n[preset.values]\nt = 100\n', encoding="utf-8"
    )
    d = _descriptor(
        inputs=[InputField(name="t", label="T", type="float", default=25, minimum=0, maximum=300)],
        presets_filename="demo_presets.json",
    )
    presets = load_presets(tmp_path, d)
    assert len(presets) == 1

    with Client(page("/")):
        handle = render_form(d)
        assert handle.state["t"] == 25  # initial = descriptor default
        handle.seed(presets[0].values)
        assert handle.state["t"] == 100
