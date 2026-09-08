"""Tests for ``matofx.tools.forms.render``.

The render module has two layers:

1. **Pure layout-planning helpers** (``_plan_layout``, ``_plan_group``,
   ``_flex_weights``) — testable directly without NiceGUI.
2. **Widget emission** (``render_form`` and the per-field builders) —
   requires an active NiceGUI client context. We exercise these through
   a ``Client(page('/'))`` fixture and inspect the resulting
   :class:`FormHandle` for correctness (state shape, defaults applied,
   error display, seed loading).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from nicegui import Client
from nicegui.page import page

from matofx.tools.descriptor import InputField, InputGroup, TableColumn, ToolDescriptor
from matofx.tools.forms import (
    FieldError,
    FileInputContext,
    FormValidationError,
    MaterialInputContext,
    render_form,
)
from matofx.tools.forms.render import (
    _OTHER_GROUP_NAME,
    _flex_weights,
    _format_other_job_label,
    _format_output_option,
    _plan_layout,
)

# --- Helpers --------------------------------------------------------------


def _descriptor(*, alias: str = "demo", **fields: Any) -> ToolDescriptor:
    base: dict[str, Any] = {
        "alias": alias,
        "name": "Demo",
        "version": "1.0.0",
        "description": "Demo.",
        "main_script": "main.py",
    }
    base.update(fields)
    return ToolDescriptor(**base)


@pytest.fixture
def nicegui_client() -> Iterator[Client]:
    """Provide an active NiceGUI client so ``ui.*`` widgets can be created.

    Each test gets its own ``Client(page('/'))`` so widgets don't pile
    up across tests.
    """
    client = Client(page("/"))
    with client:
        yield client


# --- Pure helpers ---------------------------------------------------------


def test_plan_layout_buckets_groups_by_column_and_row() -> None:
    d = _descriptor(
        inputs=[
            InputField(name="t", label="T", type="float", group="cond"),
            InputField(name="m", label="M", type="select", options=["a"], group="mat"),
        ],
        input_groups=[
            InputGroup(name="mat", label="Material", column=2, row=1),
            InputGroup(name="cond", label="Conditions", column=1, row=1),
        ],
    )
    plan = _plan_layout(d)
    assert plan.num_columns == 2
    assert [g.name for g in plan.columns[0]] == ["cond"]
    assert [g.name for g in plan.columns[1]] == ["mat"]


def test_plan_layout_orders_within_a_column_by_row() -> None:
    d = _descriptor(
        inputs=[
            InputField(name="a", label="A", type="float", group="g_top"),
            InputField(name="b", label="B", type="float", group="g_bottom"),
        ],
        input_groups=[
            InputGroup(name="g_bottom", label="Bottom", column=1, row=2),
            InputGroup(name="g_top", label="Top", column=1, row=1),
        ],
    )
    plan = _plan_layout(d)
    assert [g.name for g in plan.columns[0]] == ["g_top", "g_bottom"]


def test_plan_layout_synthesises_other_group_for_ungrouped_inputs() -> None:
    d = _descriptor(
        inputs=[
            InputField(name="loose", label="Loose", type="string"),
        ],
        input_groups=[],
    )
    plan = _plan_layout(d)
    assert plan.num_columns == 1
    other = plan.columns[0][0]
    assert other.name == _OTHER_GROUP_NAME
    assert other.label == "Other"
    # The synthetic group's only sub-column carries the loose input.
    assert [f.name for f in other.all_inputs] == ["loose"]


def test_plan_layout_buckets_inputs_by_sub_column() -> None:
    d = _descriptor(
        inputs=[
            InputField(name="a", label="A", type="float", group="g", sub_column=1),
            InputField(name="b", label="B", type="float", group="g", sub_column=2),
            InputField(name="c", label="C", type="float", group="g", sub_column=2),
        ],
        input_groups=[InputGroup(name="g", label="G", column=1, row=1, sub_columns=2)],
    )
    plan = _plan_layout(d)
    g = plan.columns[0][0]
    assert [f.name for f in g.inputs_by_sub_column[0]] == ["a"]
    assert [f.name for f in g.inputs_by_sub_column[1]] == ["b", "c"]


def test_plan_layout_clamps_oversized_sub_column_to_group_max() -> None:
    # The descriptor constructor allows sub_column=3 even when the
    # group only declares sub_columns=1; the layout planner clamps
    # so the input still renders in column 1 instead of disappearing.
    d = _descriptor(
        inputs=[
            InputField(name="a", label="A", type="float", group="g", sub_column=3),
        ],
        input_groups=[InputGroup(name="g", label="G", column=1, row=1, sub_columns=1)],
    )
    plan = _plan_layout(d)
    g = plan.columns[0][0]
    assert [f.name for f in g.inputs_by_sub_column[0]] == ["a"]


def test_plan_layout_handles_descriptor_with_no_inputs() -> None:
    d = _descriptor()
    plan = _plan_layout(d)
    assert plan.num_columns == 1
    assert plan.columns[0] == ()


def test_flex_weights_falls_back_to_even_split_when_layout_missing() -> None:
    # Two columns, no layout dict → 0.5 / 0.5.
    assert _flex_weights({}, 2) == [0.5, 0.5]


def test_flex_weights_uses_descriptor_layout_when_present() -> None:
    weights = _flex_weights({"column_1": 0.6, "column_2": 0.4}, 2)
    assert weights == [0.6, 0.4]


def test_flex_weights_clamps_zero_or_negative_to_a_visible_minimum() -> None:
    # A descriptor with column_1 = 0 would render an invisible column;
    # clamp so the layout still draws sensibly.
    weights = _flex_weights({"column_1": 0.0, "column_2": 1.0}, 2)
    assert weights[0] >= 0.05


def test_flex_weights_single_column_fills_the_form_width() -> None:
    """One column of groups takes the whole form, stale weights and all.

    ``material_properties`` carried ``{"column_1": 0.4, "column_2": 0.6}``
    long after its groups collapsed into one column, and rendered its
    only column at 40% of the width.
    """
    assert _flex_weights({"column_1": 0.4, "column_2": 0.6}, 1) == [1.0]
    assert _flex_weights({}, 1) == [1.0]


def test_flex_weights_normalise_so_the_row_is_filled() -> None:
    """Ratios are preserved; the columns always add up to the full width."""
    weights = _flex_weights({"column_1": 0.3, "column_2": 0.3}, 2)
    assert weights == pytest.approx([0.5, 0.5])


def test_flex_weights_handles_unparseable_layout_value() -> None:
    weights = _flex_weights({"column_1": "wide"}, 2)  # type: ignore[dict-item]
    # Falls back to even split when the value can't cast to float.
    assert weights[0] == 0.5


# --- Widget emission via render_form -------------------------------------


def test_render_form_initialises_state_with_descriptor_defaults(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    # Inputs without a declared group go into the synthetic "Other" group;
    # state seeds from each input's descriptor default.
    d = _descriptor(
        inputs=[
            InputField(name="t", label="T", type="float", default=25, minimum=0, maximum=100),
            InputField(name="active", label="Active", type="boolean", default=True),
        ],
    )
    handle = render_form(d)
    assert handle.state == {"t": 25, "active": True}
    assert sorted(handle.widgets.keys()) == ["active", "t"]


def test_render_form_emits_material_picker_when_context_present(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    """A ``material`` field renders the picker widget (ADR-0007 §6) and binds
    to ``state`` so the rest of the pipeline reads the chosen version id."""

    async def _list(material_class: str, required_props: list[str] | None) -> list[Any]:
        del material_class, required_props
        return []

    d = _descriptor(
        inputs=[InputField(name="fluid", label="Fluid", type="material", material_class="Fluid")]
    )
    handle = render_form(d, material_input_context=MaterialInputContext(list_materials=_list))
    assert "fluid" in handle.widgets
    assert "fluid" in handle.state


def test_render_form_material_field_falls_back_without_context(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    """No material context → the field still renders (plain fallback), no crash."""
    d = _descriptor(
        inputs=[InputField(name="fluid", label="Fluid", type="material", material_class="Fluid")]
    )
    handle = render_form(d)
    assert "fluid" in handle.widgets


def _table_descriptor() -> ToolDescriptor:
    return _descriptor(
        inputs=[
            InputField(
                name="curve",
                label="Curve",
                type="table",
                default_rows=2,
                columns=[
                    TableColumn(name="x", label="X", type="float"),
                    TableColumn(name="y", label="Y", type="float"),
                ],
            )
        ],
    )


def test_table_field_seeds_blank_rows_as_list_of_dicts(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    """A table input starts as ``default_rows`` blank dicts keyed by column."""
    handle = render_form(_table_descriptor())
    assert handle.state["curve"] == [{"x": None, "y": None}, {"x": None, "y": None}]
    # Managed directly via state, so it stays out of ``widgets`` but gets a hook.
    assert "curve" not in handle.widgets
    assert "curve" in handle.seed_hooks


def test_table_field_seed_repopulates_rows(nicegui_client: Client) -> None:  # noqa: ARG001
    """Seeding (preset / previous job) rewrites the grid's rows in place."""
    handle = render_form(_table_descriptor())
    rows_obj = handle.state["curve"]
    handle.seed({"curve": [{"x": 1.0, "y": 10.0}, {"x": 2.0, "y": 20.0}]})
    assert handle.state["curve"] == [{"x": 1.0, "y": 10.0}, {"x": 2.0, "y": 20.0}]
    # Same list object is mutated in place (the grid still references it).
    assert handle.state["curve"] is rows_obj


def test_table_field_reset_to_defaults_restores_initial_rows(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    """Reset-to-defaults falls back to the captured initial (blank) rows."""
    handle = render_form(_table_descriptor())
    handle.seed({"curve": [{"x": 9.0, "y": 9.0}]})
    assert len(handle.state["curve"]) == 1
    handle.seed({}, fill_missing_with_defaults=True)
    assert handle.state["curve"] == [{"x": None, "y": None}, {"x": None, "y": None}]


def test_table_float_cells_are_rounded_to_column_precision(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    """A seeded float cell is snapped to its column's precision step."""
    d = _descriptor(
        inputs=[
            InputField(
                name="curve",
                label="Curve",
                type="table",
                required=False,
                columns=[
                    TableColumn(name="x", label="X", type="float", precision=0.1),
                    TableColumn(name="y", label="Y", type="float", precision=0.01),
                ],
            )
        ],
    )
    handle = render_form(d)
    handle.seed({"curve": [{"x": 2.567, "y": 3.14159}]})
    assert handle.state["curve"] == [{"x": 2.6, "y": 3.14}]


def test_table_cell_out_of_range_blocks_submit_and_paints_label(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    """An out-of-range table cell gates Submit and paints the field's error
    label live — the same treatment a scalar bound gets (never clamped)."""
    d = _descriptor(
        inputs=[
            InputField(
                name="layers",
                label="Layers",
                type="table",
                required=False,
                columns=[
                    TableColumn(name="n", label="Count", type="integer", minimum=1, maximum=10),
                ],
            )
        ],
    )
    handle = render_form(d)
    handle.seed({"layers": [{"n": 99}]})  # above the column maximum
    blockers = handle.submit_blockers(require_job_name=False)
    assert any("Count in row 1 must be at most 10" in b for b in blockers)
    # The field's red label is painted live (seed triggers a revalidate).
    label = handle.error_labels["layers"]
    assert label.visible
    assert "must be at most 10" in label.text

    # In range → gate clears and label hides.
    handle.seed({"layers": [{"n": 5}]})
    assert not any("Count" in b for b in handle.submit_blockers(require_job_name=False))
    assert not handle.error_labels["layers"].visible


def _required_table_descriptor() -> ToolDescriptor:
    return _descriptor(
        inputs=[
            InputField(
                name="curve",
                label="Curve",
                type="table",
                required=True,
                default_rows=2,
                columns=[
                    TableColumn(name="x", label="X", type="float"),
                    TableColumn(name="y", label="Y", type="float"),
                ],
            )
        ],
    )


def test_required_table_with_blank_cells_blocks_submit(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    """A required table of blank rows keeps Submit gated until it's filled."""
    handle = render_form(_required_table_descriptor())
    blockers = handle.submit_blockers(require_job_name=False)
    assert any("Fill in every cell of Curve" in b for b in blockers)

    # Fill one row fully but leave the other blank — still blocked.
    handle.seed({"curve": [{"x": 1.0, "y": 2.0}, {"x": None, "y": None}]})
    assert any(
        "Fill in every cell of Curve" in b for b in handle.submit_blockers(require_job_name=False)
    )

    # Every cell filled → no table blocker.
    handle.seed({"curve": [{"x": 1.0, "y": 2.0}]})
    assert not any("Curve" in b for b in handle.submit_blockers(require_job_name=False))


def test_optional_table_partial_row_blocks_but_blank_rows_are_fine(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    """Optional table: a partly-filled row blocks; fully-blank rows don't."""
    d = _descriptor(
        inputs=[
            InputField(
                name="curve",
                label="Curve",
                type="table",
                required=False,
                default_rows=2,
                columns=[
                    TableColumn(name="x", label="X", type="float"),
                    TableColumn(name="y", label="Y", type="float"),
                ],
            )
        ],
    )
    handle = render_form(d)  # optional table, 2 blank rows
    assert not any("Curve" in b for b in handle.submit_blockers(require_job_name=False))

    handle.seed({"curve": [{"x": 1.0, "y": None}]})  # partly filled row
    assert any(
        "partly-filled row in Curve" in b for b in handle.submit_blockers(require_job_name=False)
    )


def _file_input_context() -> FileInputContext:
    """A no-op FileInputContext so a ``file`` field renders the rich widget."""

    async def _upload(field: str, name: str, data: bytes) -> str:
        del field, name, data
        return ""

    async def _clear(field: str) -> None:
        del field

    async def _list_previous(field: str) -> list[dict[str, Any]]:
        del field
        return []

    async def _fetch(path: str) -> bytes:
        del path
        return b""

    return FileInputContext(
        upload=_upload,
        clear=_clear,
        list_previous=_list_previous,
        fetch_preview=_fetch,
        fetch_cad_preview=_fetch,
    )


def test_render_form_file_field_registers_lock_hook(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    """A ``file`` field rendered with a context registers a lock hook so a
    downstream-wired file input can collapse to its read-only state — the
    file analogue of how scalar inputs lock (ADR-0015 §7a)."""
    d = _descriptor(
        inputs=[InputField(name="mesh", label="Mesh", type="file", accepted_file_types=[".vtu"])]
    )
    handle = render_form(d, file_input_context=_file_input_context())
    assert "mesh" in handle.lock_hooks


def test_render_form_lock_collapses_file_field(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    """Locking a wired file input records it and fires its hook without
    error — the user sees the 'provided automatically' note, not an
    upload/pick/clear widget."""
    d = _descriptor(
        inputs=[InputField(name="mesh", label="Mesh", type="file", accepted_file_types=[".vtu"])]
    )
    handle = render_form(d, file_input_context=_file_input_context())
    handle.lock(["mesh"])
    assert "mesh" in handle.locked_fields


def test_render_form_file_field_with_cross_tool_picker(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    """A file field renders cleanly when the cross-tool output picker is
    wired (``list_other_outputs`` set), still binding to ``state``."""

    async def _list_other(accepted: list[str]) -> list[dict[str, Any]]:
        del accepted
        return []

    ctx = FileInputContext(
        upload=_file_input_context().upload,
        clear=_file_input_context().clear,
        list_previous=_file_input_context().list_previous,
        fetch_preview=_file_input_context().fetch_preview,
        fetch_cad_preview=_file_input_context().fetch_cad_preview,
        list_other_outputs=_list_other,
    )
    d = _descriptor(
        inputs=[InputField(name="mesh", label="Mesh", type="file", accepted_file_types=[".vtu"])]
    )
    handle = render_form(d, file_input_context=ctx)
    assert "mesh" in handle.widgets


def test_format_other_job_label_and_output_option() -> None:
    """The cross-tool picker labels a job + output for human reading."""
    job_label = _format_other_job_label(
        {"job_id": 1234, "project_name": "Heat sink", "scenario_name": "Soak"}
    )
    assert job_label == "Job 0001234 (Heat sink, Soak)"

    # Missing context degrades to bare "Job NNNN"; bad id → "Job ?".
    assert _format_other_job_label({"job_id": 7}) == "Job 0000007"
    assert _format_other_job_label({"job_id": None}) == "Job ?"

    opt = _format_output_option(
        {"output_label": "Temperature field", "filename": "temperature_final.vtu"}
    )
    assert opt == "Temperature field  ·  temperature_final.vtu"
    # Filename falls back to the value's basename.
    opt2 = _format_output_option({"output_name": "temp", "value": "/mnt/job/outputs/t.vtu"})
    assert opt2 == "temp  ·  t.vtu"


def test_render_form_emits_one_error_label_per_field(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    d = _descriptor(
        inputs=[
            InputField(name="a", label="A", type="float", default=1),
            InputField(name="b", label="B", type="float", default=2),
        ],
    )
    handle = render_form(d)
    assert sorted(handle.error_labels.keys()) == ["a", "b"]
    # Error labels start hidden (no error displayed yet).
    for label in handle.error_labels.values():
        assert label.visible is False
        assert label.text == ""


def test_show_errors_marks_only_offending_fields(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    d = _descriptor(
        inputs=[
            InputField(name="a", label="A", type="float", default=1),
            InputField(name="b", label="B", type="float", default=2),
        ],
    )
    handle = render_form(d)
    handle.show_errors(
        FormValidationError([FieldError(name="a", label="A", message="A is required.")])
    )
    assert handle.error_labels["a"].visible is True
    assert handle.error_labels["a"].text == "A is required."
    assert handle.error_labels["b"].visible is False


def test_show_errors_marks_every_sum_check_participant(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    # Sum-check failures attach to the first parameter as the anchor
    # but list every participant; the renderer must mark them all so
    # the user sees the whole group highlighted.
    d = _descriptor(
        inputs=[
            InputField(name="a", label="A", type="float", default=33),
            InputField(name="b", label="B", type="float", default=33),
            InputField(name="c", label="C", type="float", default=34),
        ],
    )
    handle = render_form(d)
    handle.show_errors(
        FormValidationError(
            [
                FieldError(
                    name="a",
                    label="A",
                    message="A, B, C must add up to 100.",
                    sum_parameters=("a", "b", "c"),
                )
            ]
        )
    )
    for name in ("a", "b", "c"):
        assert handle.error_labels[name].visible is True
        assert "must add up to 100" in handle.error_labels[name].text


def test_clear_errors_resets_every_label(nicegui_client: Client) -> None:  # noqa: ARG001
    d = _descriptor(inputs=[InputField(name="a", label="A", type="float", default=1)])
    handle = render_form(d)
    handle.show_errors(FormValidationError([FieldError("a", "A", "X")]))
    handle.clear_errors()
    assert handle.error_labels["a"].visible is False
    assert handle.error_labels["a"].text == ""


def test_seed_loads_partial_values_and_ignores_unknown_fields(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    d = _descriptor(
        inputs=[
            InputField(name="t", label="T", type="float", default=25, minimum=0, maximum=300),
            InputField(name="active", label="Active", type="boolean", default=True),
        ],
    )
    handle = render_form(d)
    # Partial seed: leaves ``active`` at its default; ``unknown_field``
    # is ignored silently (older preset compatible with newer descriptor).
    handle.seed({"t": 100, "unknown_field": "x"})
    assert handle.state["t"] == 100
    assert handle.state["active"] is True


def test_sections_start_collapsed_and_unopened_by_default(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    # Default descriptor → collapsed sections, nothing reviewed yet.
    d = _descriptor(
        inputs=[
            InputField(name="t", label="T", type="float", default=1, group="cond"),
            InputField(name="m", label="M", type="string", group="mat"),
        ],
        input_groups=[
            InputGroup(name="cond", label="Conditions", column=1, row=1),
            InputGroup(name="mat", label="Material", column=1, row=2),
        ],
    )
    handle = render_form(d)
    assert handle.groups == [("cond", "Conditions"), ("mat", "Material")]
    assert handle.opened_groups == set()
    assert handle.all_groups_opened is False
    assert handle.unopened_group_labels() == ["Conditions", "Material"]


def test_opened_once_semantics_via_handle(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    d = _descriptor(
        inputs=[
            InputField(name="t", label="T", type="float", default=1, group="cond"),
            InputField(name="m", label="M", type="string", group="mat"),
        ],
        input_groups=[
            InputGroup(name="cond", label="Conditions", column=1, row=1),
            InputGroup(name="mat", label="Material", column=1, row=2),
        ],
    )
    handle = render_form(d)
    # Simulate the user opening one section.
    handle.opened_groups.add("cond")
    assert handle.all_groups_opened is False
    assert handle.unopened_group_labels() == ["Material"]
    handle.opened_groups.add("mat")
    assert handle.all_groups_opened is True
    assert handle.unopened_group_labels() == []


def test_sections_expanded_are_pre_reviewed(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    # When the tool starts sections expanded, everything is already
    # revealed — the review gate is satisfied from render time.
    d = _descriptor(
        input_groups_start_collapsed=False,
        inputs=[
            InputField(name="t", label="T", type="float", default=1, group="cond"),
        ],
        input_groups=[InputGroup(name="cond", label="Conditions", column=1, row=1)],
    )
    handle = render_form(d)
    assert handle.opened_groups == {"cond"}
    assert handle.all_groups_opened is True
    assert handle.unopened_group_labels() == []


def test_synthetic_other_section_counts_for_the_gate(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    # Ungrouped inputs render under the synthetic "Other" expansion,
    # which must still be opened to satisfy the review gate.
    d = _descriptor(inputs=[InputField(name="loose", label="Loose", type="string")])
    handle = render_form(d)
    assert handle.groups == [(_OTHER_GROUP_NAME, "Other")]
    assert handle.all_groups_opened is False
    assert handle.unopened_group_labels() == ["Other"]


def test_no_sections_means_gate_trivially_satisfied(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    # A descriptor with no inputs emits no expansions; "all opened" is
    # vacuously true so the gate never blocks an input-less tool.
    d = _descriptor()
    handle = render_form(d)
    assert handle.groups == []
    assert handle.all_groups_opened is True
    assert handle.unopened_group_labels() == []


def test_seed_with_fill_missing_resets_unmentioned_fields_to_defaults(
    nicegui_client: Client,  # noqa: ARG001
) -> None:
    # Used by the "Reset to defaults" popover — anything the user
    # already entered must be cleared back to the descriptor's default.
    d = _descriptor(
        inputs=[
            InputField(name="t", label="T", type="float", default=25),
            InputField(name="active", label="Active", type="boolean", default=False),
        ],
    )
    handle = render_form(d)
    # User mutates state, then triggers a "Reset to defaults" with an
    # empty seed and ``fill_missing_with_defaults=True``.
    handle.state["t"] = 999
    handle.state["active"] = True
    handle.seed({}, fill_missing_with_defaults=True)
    assert handle.state["t"] == 25
    assert handle.state["active"] is False
