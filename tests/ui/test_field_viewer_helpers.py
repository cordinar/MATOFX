"""Pure data-shaping helpers for the predicted-field viewer (ADR-0008 §4.5).

The viewer's figure builders + factor handling are pure (no NiceGUI), so
they test without a browser. Importing the module is safe — ``nicegui`` is
imported lazily inside ``render_field_viewer`` only.
"""

from __future__ import annotations

from matofx.studies.surrogate.ingest import TIME_FACTOR
from matofx.ui._field_surrogate_viewer import (
    _bound,
    _coefficient_figure,
    _field_line_figure,
    _field_mesh_figure,
    _initial_point,
    _levels,
    _mode_axis,
    _scree_figure,
    _split_time_factor,
)


def test_bound_falls_back_to_default() -> None:
    assert _bound({"min": 2.0}, "min", 0.0) == 2.0
    assert _bound({}, "min", -1.0) == -1.0
    assert _bound({"min": None}, "min", 5.0) == 5.0


def test_split_time_factor_partitions() -> None:
    factors = [
        {"name": "fin_height", "min": 1.0, "max": 5.0},
        {"name": TIME_FACTOR, "min": 0.0, "max": 4.0},
        {"name": "h", "min": 10.0, "max": 50.0},
    ]
    design, time_factor = _split_time_factor(factors)
    assert [f["name"] for f in design] == ["fin_height", "h"]
    assert time_factor is not None
    assert time_factor["name"] == TIME_FACTOR


def test_split_time_factor_none_when_steady() -> None:
    design, time_factor = _split_time_factor([{"name": "x", "min": 0.0, "max": 1.0}])
    assert time_factor is None
    assert len(design) == 1


def test_initial_point_is_midpoint() -> None:
    point = _initial_point(
        [{"name": "x", "min": 0.0, "max": 10.0}, {"name": "y", "min": 2.0, "max": 4.0}]
    )
    assert point == {"x": 5.0, "y": 3.0}


def test_levels_distinguishes_a_choice_from_a_number() -> None:
    assert _levels({"name": "material", "levels": ["Aluminium", "Magnesium"]}) == [
        "Aluminium",
        "Magnesium",
    ]
    assert _levels({"name": "fin_height", "min": 1.0, "max": 5.0}) == []


def test_initial_point_starts_a_categorical_on_its_first_level() -> None:
    """A choice has no range to take the midpoint of.

    Treating every factor as numeric sent ``material=0.5`` to a model that had
    only ever seen ``Aluminium`` / ``Magnesium``, so the viewer opened on a
    predict error rather than on a field.
    """
    point = _initial_point(
        [
            {"name": "fin_height", "min": 5.0, "max": 45.0},
            {"name": "material", "levels": ["Aluminium", "Magnesium"]},
        ]
    )
    assert point == {"fin_height": 25.0, "material": "Aluminium"}


def test_field_line_figure_without_std() -> None:
    fig = _field_line_figure([1.0, 2.0, 3.0], None, label="T")
    # One trace (the line) when there's no uncertainty band.
    assert len(fig.data) == 1
    assert list(fig.data[0].y) == [1.0, 2.0, 3.0]


def test_field_line_figure_with_std_adds_band() -> None:
    fig = _field_line_figure([1.0, 2.0], [0.1, 0.2], label="T")
    # A filled band trace plus the line.
    assert len(fig.data) == 2
    assert fig.data[0].fill == "toself"


def test_field_line_figure_fixed_range_sets_axis() -> None:
    fig = _field_line_figure([1.0, 2.0], None, label="T", y_range=(0.0, 10.0))
    assert list(fig.layout.yaxis.range) == [0.0, 10.0]


def test_scree_and_coefficient_figures() -> None:
    scree = _scree_figure([0.7, 0.2, 0.1])
    assert list(scree.data[0].x) == [1, 2, 3]
    assert list(scree.data[0].y) == [0.7, 0.2, 0.1]
    coeff = _coefficient_figure([1.5, -0.5])
    assert list(coeff.data[0].y) == [1.5, -0.5]


def test_field_mesh_figure_builds_mesh3d() -> None:
    points = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    faces = ([0, 0], [1, 1], [2, 3])  # two triangles
    fig = _field_mesh_figure(points, faces, [1.0, 2.0, 3.0, 4.0], label="T")
    mesh = fig.data[0]
    assert mesh.type == "mesh3d"
    assert list(mesh.intensity) == [1.0, 2.0, 3.0, 4.0]
    assert list(mesh.x) == [0.0, 1.0, 0.0, 0.0]
    assert list(mesh.i) == [0, 0]


def test_field_mesh_figure_fixed_colour_range() -> None:
    fig = _field_mesh_figure([[0.0, 0.0, 0.0]], ([], [], []), [1.0], label="T", c_range=(0.0, 5.0))
    assert fig.data[0].cmin == 0.0
    assert fig.data[0].cmax == 5.0


def test_mode_axes_tick_on_whole_modes_only() -> None:
    """There is no mode 1.5.

    Plotly's automatic ticks put one there whenever the fit retained two or
    three modes, which is the common case.
    """
    scree = _scree_figure([0.99, 0.01])
    coeff = _coefficient_figure([-1400.0, 12.0])
    for fig in (scree, coeff):
        assert fig.layout.xaxis.tickmode == "array"
        assert list(fig.layout.xaxis.tickvals) == [1, 2]
        assert fig.layout.xaxis.title.text == "POD mode"


def test_mode_axis_matches_the_number_of_modes() -> None:
    assert list(_mode_axis(4)["tickvals"]) == [1, 2, 3, 4]
    assert list(_mode_axis(0)["tickvals"]) == []
