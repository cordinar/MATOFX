"""Predicted-field viewer for a fitted field surrogate (ADR-0008 §4.5).

Renders a ``kind='field'`` :class:`SurrogateModelVersion` interactively: one
control per factor — a slider bounded to the frozen training range for a
numeric factor, a picker over the fitted levels for a categorical one — plus
a **time** slider with play/pause when the surrogate is time-resolved. They
drive a live ``field-predict`` call whose reconstructed nodal field is drawn
as a Plotly trace with an optional plus/minus 1-sigma uncertainty band.
Below sit the POD diagnostics: a scree (modal-energy) bar, the reconstruction
RMSE, and the current mode-coefficient bar.

Every control and axis reads the source tool's **label**, never the frozen
identifier — ``version.factors`` carries identifiers only, so the caller
passes the label map in.

Geometry note: a ``.npy`` field carries no mesh, so the field is drawn as
value-vs-node — the honest universal view, and the **fallback only**. A
geometry-bearing bundle (VTK / CGNS / Ensight) exposes ``has_geometry`` and gets
a coloured 3-D ``Mesh3d`` surface instead (design §4.5). The two are not shown
together: the line's x-axis is the mesh's internal node ordering, which means
something for a 1-D profile and nothing for a 59,000-node solid.

The data-shaping helpers are pure (no NiceGUI) so they unit-test without a
browser; :func:`render_field_viewer` wires them to the live page.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import plotly.graph_objects as go

from matofx.studies.surrogate.ingest import TIME_FACTOR
from matofx.ui._coverage_panel import MODES_EXPLAINER, modes_count_note, support_view
from matofx.ui.theme import themed_plotly

if TYPE_CHECKING:
    from matofx.ui.api_client import ApiClient


def _bound(factor: dict[str, Any], key: str, default: float) -> float:
    """A factor's ``min``/``max`` as a float, falling back to ``default``."""
    raw = factor.get(key)
    return float(raw) if raw is not None else default


def _split_time_factor(
    factors: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Partition frozen factors into ``(design_factors, time_factor_or_None)``."""
    design: list[dict[str, Any]] = []
    time_factor: dict[str, Any] | None = None
    for f in factors:
        if str(f.get("name")) == TIME_FACTOR:
            time_factor = f
        else:
            design.append(f)
    return design, time_factor


def _levels(factor: dict[str, Any]) -> list[str]:
    """A factor's categorical levels, or ``[]`` when it is numeric.

    A varied ``select`` (a material, a solver choice) is a real factor with
    no ``min``/``max``, so anything that treats every factor as a number has
    to ask this first.
    """
    return [str(v) for v in (factor.get("levels") or []) if str(v)]


def _initial_point(factors: list[dict[str, Any]]) -> dict[str, Any]:
    """The viewer's first frame: each range's midpoint, each choice's first level.

    A categorical factor has no range to take the midpoint of; giving it one
    anyway sent ``material=0.5`` to a model that had only ever seen
    ``Aluminium`` / ``Magnesium``, and the viewer opened on a predict error.
    """
    point: dict[str, Any] = {}
    for f in factors:
        name = str(f.get("name"))
        levels = _levels(f)
        if levels:
            point[name] = levels[0]
            continue
        lo = _bound(f, "min", 0.0)
        hi = _bound(f, "max", 1.0)
        point[name] = (lo + hi) / 2.0
    return point


def _field_line_figure(
    field: list[float],
    field_std: list[float] | None,
    *,
    label: str,
    y_range: tuple[float, float] | None = None,
) -> go.Figure:
    """Value-vs-node line for the reconstructed field (plus a 1-sigma band)."""
    x = list(range(len(field)))
    fig = go.Figure()
    if field_std:
        upper = [v + s for v, s in zip(field, field_std, strict=False)]
        lower = [v - s for v, s in zip(field, field_std, strict=False)]
        fig.add_trace(
            go.Scatter(
                x=[*x, *x[::-1]],
                y=[*upper, *lower[::-1]],
                fill="toself",
                fillcolor="rgba(44,127,184,0.18)",
                line={"width": 0},
                name="+/-1 sigma",
                hoverinfo="skip",
            )
        )
    fig.add_trace(
        go.Scatter(
            x=x,
            y=field,
            mode="lines",
            line={"color": "#2c7fb8"},
            name=label,
            hovertemplate="node %{x}<br>value %{y:.4g}<extra></extra>",
        )
    )
    layout: dict[str, Any] = {
        "title": label,
        "xaxis_title": "mesh node",
        "yaxis_title": label,
        "height": 320,
        "margin": {"l": 50, "r": 20, "t": 40, "b": 40},
    }
    if y_range is not None:
        layout["yaxis"] = {"range": list(y_range)}
    fig.update_layout(**layout)
    return fig


def _mode_axis(count: int) -> dict[str, Any]:
    """X-axis config for a per-mode bar chart: one tick per mode, no half-modes.

    A mode is the 1st, 2nd, 3rd pattern — there is no mode 1.5, and Plotly's
    automatic ticks put one there whenever the fit retained only two or three.
    """
    return {
        "title": "POD mode",
        "tickmode": "array",
        "tickvals": list(range(1, count + 1)),
    }


def _scree_figure(mode_energy_spectrum: list[float]) -> go.Figure:
    """Per-mode explained-variance bar (the POD scree plot)."""
    modes = list(range(1, len(mode_energy_spectrum) + 1))
    fig = go.Figure(
        go.Bar(x=modes, y=mode_energy_spectrum, marker={"color": "#2c7fb8"}, name="energy")
    )
    fig.update_layout(
        title="Modal energy (scree)",
        xaxis=_mode_axis(len(mode_energy_spectrum)),
        yaxis_title="explained variance",
        height=260,
        margin={"l": 50, "r": 20, "t": 40, "b": 40},
    )
    return fig


def _field_mesh_figure(
    points: list[list[float]],
    faces: tuple[list[int], list[int], list[int]],
    intensity: list[float],
    *,
    label: str,
    c_range: tuple[float, float] | None = None,
) -> go.Figure:
    """Coloured 3-D surface (Plotly ``Mesh3d``) of the field over its mesh.

    ``points`` are full node coordinates, ``intensity`` the per-node field
    (aligned by index), ``faces`` the surface triangles. The viridis colour
    map matches design §4.5; ``c_range`` pins the colour scale so frames stay
    comparable across slider / time moves.
    """
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    zs = [p[2] for p in points]
    i, j, k = faces
    mesh_kwargs: dict[str, Any] = {
        "x": xs,
        "y": ys,
        "z": zs,
        "i": i,
        "j": j,
        "k": k,
        "intensity": intensity,
        "colorscale": "Viridis",
        "showscale": True,
        "colorbar": {"title": label},
        "name": label,
    }
    if c_range is not None:
        mesh_kwargs["cmin"], mesh_kwargs["cmax"] = c_range
    fig = go.Figure(go.Mesh3d(**mesh_kwargs))
    fig.update_layout(
        title=label,
        height=480,
        margin={"l": 0, "r": 0, "t": 40, "b": 0},
        scene={
            # Preserve true proportions, but pull the camera back from Plotly's
            # default eye (1.25) so an elongated field (e.g. a tall stirred-tank)
            # isn't clipped at the top and bottom.
            "aspectmode": "data",
            "camera": {"eye": {"x": 1.7, "y": 1.7, "z": 1.7}},
        },
    )
    return fig


def _field_frame_figure(
    data: dict[str, Any], frame: list[float], label: str, c_range: tuple[float, float] | None
) -> go.Figure:
    """One frame as a coloured ``Mesh3d`` (geometry) or value-vs-node line (``.npy``).

    Shared by the Job page's per-run field viewer and the Compare grid's
    side-by-side field animations, so a transient ``field`` output looks the
    same whether you open one run or compare several.
    """
    if data.get("has_geometry") and data.get("points"):
        faces = (data.get("i") or [], data.get("j") or [], data.get("k") or [])
        return _field_mesh_figure(data["points"], faces, frame, label=label, c_range=c_range)
    return _field_line_figure(frame, None, label=label, y_range=c_range)


def _field_animation_figure(
    data: dict[str, Any], frames: list[list[float]], label: str, c_range: tuple[float, float] | None
) -> go.Figure:
    """The whole transient series as a single self-contained Plotly animation.

    One Plotly frame per timestep over the first step's geometry/axes, plus a
    play/pause button and a step slider. Exported to standalone HTML this *is*
    "the whole series as one animation" — it plays (and, for a 3-D mesh,
    rotates) in any browser. Frame data reuses :func:`_field_frame_figure`, so
    the downloaded animation looks exactly like the inline viewer.
    """
    has_geometry = bool(data.get("has_geometry") and data.get("points"))
    fig: go.Figure = _field_frame_figure(data, frames[0], label, c_range)
    fig.frames = [
        go.Frame(data=_field_frame_figure(data, fr, label, c_range).data, name=str(i))
        for i, fr in enumerate(frames)
    ]
    # ``redraw=True`` is required for 3-D (Mesh3d) frames and harmless for the
    # 2-D line, so the same controls drive both field kinds.
    play = {"frame": {"duration": 500, "redraw": True}, "fromcurrent": True}
    pause = {"frame": {"duration": 0, "redraw": False}, "mode": "immediate"}
    # Controls stack vertically in a roomy bottom margin (not above the plot,
    # where they used to overlap the title): the play/pause buttons on their
    # own line, the full-width step slider clearly below them. They used to
    # share one horizontal band (buttons left, slider starting at x=0.12),
    # which overlapped once the figure was narrow — the side-by-side compare
    # columns and the report. Stacking is width-independent. ``b`` is generous
    # so the slider's tick + "step N" labels aren't clipped, and the extra
    # height keeps the plot from being squeezed.
    margin = (
        {"l": 0, "r": 0, "t": 50, "b": 170}
        if has_geometry
        else {
            "l": 55,
            "r": 25,
            "t": 50,
            "b": 170,
        }
    )
    # Constant uirevision so the user's rotation / zoom survives each frame
    # redraw during playback (Mesh3d otherwise snaps the camera back).
    fig.update_layout(
        uirevision="field-anim",
        scene_uirevision="field-anim",
        height=(560 if has_geometry else 460),
        margin=margin,
        updatemenus=[
            {
                "type": "buttons",
                "direction": "left",
                "showactive": False,
                "x": 0.0,
                "xanchor": "left",
                "y": 0,
                "yanchor": "top",
                "pad": {"t": 50, "r": 10},
                "buttons": [
                    {"label": "▶ Play", "method": "animate", "args": [None, play]},
                    {"label": "⏸ Pause", "method": "animate", "args": [[None], pause]},
                ],
            }
        ],
        sliders=[
            {
                "active": 0,
                "x": 0.0,
                "len": 1.0,
                "y": 0,
                "xanchor": "left",
                "yanchor": "top",
                "pad": {"t": 135, "b": 10},
                "currentvalue": {"prefix": "step "},
                "steps": [
                    {
                        "label": str(i),
                        "method": "animate",
                        "args": [
                            [str(i)],
                            {"frame": {"duration": 0, "redraw": True}, "mode": "immediate"},
                        ],
                    }
                    for i in range(len(frames))
                ],
            }
        ],
    )
    return fig


def _field_animation_filename(filename: str) -> str:
    """A friendly ``.html`` download name, with any trailing frame index dropped."""
    leaf = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in leaf:
        leaf = leaf.rsplit(".", 1)[0]
    # Strip a trailing ``_0000``-style frame counter so the series name reads
    # as the field, not frame zero ("temperature_0000" -> "temperature").
    stem = leaf.rstrip("0123456789").rstrip("_") or leaf or "field"
    return f"{stem}_animation.html"


def _coefficient_figure(mode_coefficients: list[float]) -> go.Figure:
    """Current modal-coefficient bar for the predicted point.

    **A negative bar is normal, not a fault.** The reconstruction is
    ``field = average field + Σ coefficient * pattern`` (see
    :meth:`FieldSurrogate.predict`): the snapshots are mean-centred before the
    SVD, so a coefficient is a *signed* displacement from the average field, and
    a pattern's direction is only defined up to a sign by the decomposition. A
    negative coefficient means this design leans the opposite way along that
    pattern from the average — roughly half of all designs do, on any mode.

    The zero line is drawn explicitly for that reason: the reference the bars
    are measured against is the average field, and it needs to be visible for a
    bar below it to read as "the other way" rather than as "wrong".
    """
    modes = list(range(1, len(mode_coefficients) + 1))
    fig = go.Figure(
        go.Bar(x=modes, y=mode_coefficients, marker={"color": "#7fcdbb"}, name="coefficient")
    )
    fig.update_layout(
        title="Mode coefficients at this point",
        xaxis=_mode_axis(len(mode_coefficients)),
        yaxis={"title": "leaning from the average field", "zeroline": True, "zerolinewidth": 2},
        height=260,
        margin={"l": 50, "r": 20, "t": 40, "b": 40},
    )
    return fig


def _resolve_viewer_quality(
    version: dict[str, Any], response_name: str | None
) -> tuple[str | None, dict[str, Any]]:
    """Pick the field response to view + its quality block (ADR-0018 combined)."""
    field_responses = [
        r for r in (version.get("responses") or []) if r.get("kind") == "field" and r.get("name")
    ]
    resolved = response_name or (str(field_responses[0].get("name")) if field_responses else None)
    q = (version.get("quality") or {}).get(str(resolved))
    return resolved, (q if isinstance(q, dict) else {})


async def render_field_viewer(
    client: ApiClient,
    model_id: int,
    version: dict[str, Any],
    *,
    response_label: str,
    response_name: str | None = None,
    factor_labels: Mapping[str, str] | None = None,
) -> None:
    """Mount the interactive predicted-field viewer for one field output.

    Called inside an open NiceGUI container (an expansion on the model
    detail page). Builds the controls + plots, fetches the first frame, and
    re-predicts on every control change (throttled) and on time playback.
    ``response_name`` selects which field of a combined model (ADR-0018); when
    ``None`` the version's first/only field is used.

    ``factor_labels`` maps each factor's identifier to the source tool's own
    label + unit ("Fin height (mm)"); the frozen ``version.factors`` carry only
    identifiers, so without it every control read ``fin_height``.
    """
    from nicegui import ui  # noqa: PLC0415 - keep import lazy for headless tests

    vid = int(version.get("id") or 0)
    factors = [dict(f) for f in (version.get("factors") or [])]
    resolved_name, quality = _resolve_viewer_quality(version, response_name)
    spectrum = [float(v) for v in (quality.get("mode_energy_spectrum") or [])]
    recon_rmse = quality.get("recon_rmse")
    n_nodes = int(quality.get("n_nodes") or 0)

    design_factors, time_factor = _split_time_factor(factors)
    point = _initial_point(factors)
    state: dict[str, Any] = {
        "fixed_range": False,
        "y_range": None,
        "c_range": None,
        "playing": False,
        "geometry": None,
    }
    mesh_plot: Any = None

    with ui.column().classes("w-full gap-3"):
        ui.label(
            f"Predicted field over {n_nodes} mesh nodes. Move the controls below to "
            "reconstruct the field anywhere in the trained design space."
        ).style("color: #888; font-size: 0.82rem;")

        # The ADR-0020 §8 support verdict for wherever the sliders currently
        # sit. This viewer is a *live* prediction, and the one place a user
        # actively drags controls out of the region the runs covered — pushing
        # `time` past the last trained snapshot is the textbook case — so it
        # is the surface that most needs to say when that has happened.
        support_row = ui.row().classes("items-center gap-2 w-full")

        # 3-D coloured mesh (filled once geometry is fetched) and, below it, the
        # value-vs-node line. The line is the **fallback**, not a companion: its
        # x-axis is the mesh's internal node ordering, which says something
        # about a 1-D profile and nothing at all about a 59,000-node solid. It
        # is the honest universal view when a `.npy` field carries no mesh, and
        # noise beside a real geometry — so it is hidden once the mesh view is
        # built, and stays hidden while it is the same data on real coordinates.
        mesh_container = ui.column().classes("w-full")
        field_plot = ui.plotly(themed_plotly(_field_line_figure([], None, label=response_label)))
        field_plot.classes("w-full")

        async def _redraw() -> None:
            try:
                result = await client.field_predict(model_id, vid, point, resolved_name)
            except Exception as exc:  # surface any predict failure inline
                ui.notify(f"Couldn't predict the field: {exc}", type="negative")
                return
            _draw_support(ui, support_row, result.get("support"))
            field = [float(v) for v in (result.get("field") or [])]
            field_std = result.get("field_std")
            std = [float(v) for v in field_std] if field_std else None
            if state["fixed_range"] and field and state["y_range"] is None:
                state["y_range"] = (min(field), max(field))
                state["c_range"] = (min(field), max(field))
            y_range = state["y_range"] if state["fixed_range"] else None
            if mesh_plot is None:
                field_plot.update_figure(
                    themed_plotly(
                        _field_line_figure(field, std, label=response_label, y_range=y_range)
                    )
                )
            if mesh_plot is not None and state["geometry"] is not None:
                pts, faces = state["geometry"]
                c_range = state["c_range"] if state["fixed_range"] else None
                mesh_plot.update_figure(
                    themed_plotly(
                        _field_mesh_figure(pts, faces, field, label=response_label, c_range=c_range)
                    )
                )
            coeff_plot.update_figure(
                themed_plotly(
                    _coefficient_figure([float(v) for v in result.get("mode_coefficients", [])])
                )
            )

        _build_factor_controls(ui, design_factors, point, _redraw, factor_labels)

        # Time slider + play/pause for a time-resolved surrogate.
        if time_factor is not None:
            await _build_time_controls(ui, time_factor, point, state, _redraw)

        _build_fixed_range_switch(ui, state, _redraw)

        coeff_plot = _render_diagnostics(ui, spectrum, recon_rmse, quality)
        mesh_plot = await _build_mesh_view(
            ui, client, model_id, vid, mesh_container, state, response_label, resolved_name
        )
        if mesh_plot is not None:
            field_plot.set_visibility(False)
        await _redraw()


def _build_fixed_range_switch(ui: Any, state: dict[str, Any], redraw: Any) -> None:
    """Pin the value range across frames, so playback is comparable frame to frame.

    Clears the cached range on every toggle: the next redraw recomputes it from
    the field currently on screen, which is what makes "fix it *here*" mean the
    design you are looking at rather than whichever one happened to be first.
    """

    def _toggle(e: Any) -> None:
        state["fixed_range"] = bool(e.value)
        state["y_range"] = None  # recompute on next redraw
        state["c_range"] = None
        ui.timer(0.0, redraw, once=True)

    ui.switch("Fix value range across frames", on_change=_toggle).props("dense")


def _render_diagnostics(
    ui: Any, spectrum: list[float], recon_rmse: Any, quality: dict[str, Any]
) -> Any:
    """Scree + reconstruction-RMSE panel; returns the live mode-coefficient plot.

    Led by an explanation, because two charts axed "POD mode" tell a reader who
    has not met proper-orthogonal decomposition nothing at all.

    Each caption sits **above** its own chart in a column of its own. They used
    to be flex siblings of the charts in one row — four items sharing a line —
    so the captions ate the width, both plots were squeezed to a slice of an
    axis, and neither was readable enough to tell what it was showing.
    ``min-w-0`` lets a column shrink below its content's natural width (a flex
    item defaults to ``min-width: auto``, which is what let the plots overflow
    rather than fit), and the row wraps to one chart per line when narrow.

    The two captions are **bottom-aligned in a shared reserved height**, so the
    charts start on the same line whatever the captions wrap to. Letting each
    caption size itself puts the two plots at different heights the moment one
    runs to an extra line, and two bar charts that should be read against each
    other are much harder to compare when their baselines disagree. The caveat
    about a *negative* coefficient sits under the whole row rather than in one
    caption, both because it is what pushed the captions out of step and because
    it is a fact about reading the chart, not about what the chart shows.
    """
    ui.label(MODES_EXPLAINER).style("color: var(--matofx-muted-text); font-size: 0.82rem;")
    caption = (
        "color: var(--matofx-muted-text); font-size: 0.78rem; min-height: 2.8rem; "
        "display: flex; align-items: flex-end;"
    )
    with ui.row().classes("w-full flex-wrap gap-4 items-stretch"):
        if spectrum:
            with ui.column().classes("flex-1 min-w-0 gap-1").style("min-width: 280px;"):
                ui.label(
                    "How much of the variation each pattern accounts for — the first few "
                    "usually carry nearly all of it."
                ).style(caption)
                ui.plotly(themed_plotly(_scree_figure(spectrum))).classes("w-full")
        with ui.column().classes("flex-1 min-w-0 gap-1").style("min-width: 280px;"):
            ui.label(
                "How far this design leans from the average field along each pattern — "
                "these move as you change the controls above."
            ).style(caption)
            coeff_plot = ui.plotly(themed_plotly(_coefficient_figure([]))).classes("w-full")
    ui.label(
        "On the right-hand chart, zero means this design matches the average field. A bar "
        "below the line just means it leans the other way along that pattern, which is as "
        "ordinary as leaning above it."
    ).style("color: var(--matofx-muted-text); font-size: 0.78rem;")
    note = modes_count_note(quality.get("modes_retained"), quality.get("mode_energy"))
    if note:
        ui.label(note).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
    if recon_rmse is not None:
        ui.label(
            f"Reconstruction RMSE over the training snapshots: {float(recon_rmse):.4g} "
            f"({quality.get('modes_retained', '?')} modes retained)."
        ).style("color: #888; font-size: 0.8rem;")
    return coeff_plot


def _draw_support(ui: Any, row: Any, support: Any) -> None:
    """Redraw the support badge for wherever the sliders currently sit (§8).

    Nothing is drawn when there is no verdict — a version fitted before
    coverage existed has none, and an empty badge invites the reader to assume
    the blank means fine.
    """
    row.clear()
    view = support_view(support if isinstance(support, dict) else None)
    if view is None:
        return
    chip_label, colour, note = view
    with row:
        ui.badge(chip_label).props(f"color={colour}").style(
            "font-size: 0.8rem; padding: 0.3rem 0.6rem"
        )
        if note:
            ui.label(note).style("color: #888; font-size: 0.8rem;")


async def _build_mesh_view(
    ui: Any,
    client: ApiClient,
    model_id: int,
    version_id: int,
    mesh_container: Any,
    state: dict[str, Any],
    label: str,
    response_name: str | None = None,
) -> Any:
    """Fetch the reference-mesh surface once and build the coloured 3-D view.

    Geometry is a best-effort enhancement: any failure (or a ``.npy`` field
    with no mesh) leaves the value-vs-node line as the sole view. Returns the
    ``Mesh3d`` plot element (or ``None``); the caller's ``_redraw`` updates its
    intensity each frame.
    """
    try:
        geo = await client.field_geometry(model_id, version_id, response_name)
    except Exception:  # never block the viewer on the geometry enhancement
        return None
    if not (geo.get("has_geometry") and geo.get("points")):
        return None
    faces = (geo.get("i", []), geo.get("j", []), geo.get("k", []))
    state["geometry"] = (geo["points"], faces)
    zeros = [0.0] * len(geo["points"])
    with mesh_container:
        return ui.plotly(
            themed_plotly(_field_mesh_figure(geo["points"], faces, zeros, label=label))
        ).classes("w-full")


def _build_factor_controls(
    ui: Any,
    design_factors: list[dict[str, Any]],
    point: dict[str, Any],
    redraw: Any,
    factor_labels: Mapping[str, str] | None,
) -> None:
    """One control per design factor, wrapping across the width of the viewer.

    Each is captioned with the source tool's label; a factor the descriptor no
    longer declares falls back to its identifier rather than going unlabelled.
    """
    labels = dict(factor_labels or {})
    with ui.row().classes("w-full flex-wrap gap-4"):
        for f in design_factors:
            name = str(f.get("name"))
            _factor_control(ui, f, point, redraw, label=labels.get(name, name))


def _factor_control(
    ui: Any,
    factor: dict[str, Any],
    point: dict[str, Any],
    redraw: Any,
    *,
    label: str,
) -> None:
    """One labelled factor control that updates ``point`` and re-predicts.

    A numeric factor gets a slider across its training range; a **categorical**
    one gets a picker over the levels it was fitted on. Rendering a choice as a
    slider produced ``material = 0.5``, which the model can only refuse — the
    two kinds of factor need two kinds of control, not one with a fudge.
    """
    name = str(factor.get("name"))
    levels = _levels(factor)
    if levels:
        _categorical_picker(ui, name, levels, point, redraw, label=label)
        return
    lo = _bound(factor, "min", 0.0)
    hi = _bound(factor, "max", 1.0)
    step = (hi - lo) / 100 if hi > lo else 1.0
    with ui.column().classes("gap-0").style("min-width: 220px;"):
        with ui.row().classes("items-center gap-2"):
            ui.label(label).style("font-size: 0.78rem; color: #888;")
            val_lbl = ui.label(f"{point.get(name, lo):.4g}").style(
                "font-size: 0.78rem; font-weight: 600;"
            )
        sl = ui.slider(min=lo, max=hi, value=point.get(name, (lo + hi) / 2.0), step=step)

        def _on_slide(
            _e: Any = None, _name: str = name, _sl: Any = sl, _lbl: Any = val_lbl
        ) -> None:
            point[_name] = float(_sl.value)
            _lbl.text = f"{float(_sl.value):.4g}"
            ui.timer(0.0, redraw, once=True)

        sl.on("update:model-value", _on_slide, throttle=0.2)


def _categorical_picker(
    ui: Any,
    name: str,
    levels: list[str],
    point: dict[str, Any],
    redraw: Any,
    *,
    label: str,
) -> None:
    """A dropdown over the levels a categorical factor was fitted on."""
    with ui.column().classes("gap-0").style("min-width: 220px;"):
        ui.label(label).style("font-size: 0.78rem; color: #888;")
        sel = ui.select(levels, value=point.get(name, levels[0])).props("dense outlined")

        def _on_pick(_e: Any = None) -> None:
            point[name] = str(sel.value)
            ui.timer(0.0, redraw, once=True)

        sel.on_value_change(_on_pick)


async def _build_time_controls(
    ui: Any,
    time_factor: dict[str, Any],
    point: dict[str, float],
    state: dict[str, Any],
    redraw: Any,
) -> None:
    """Time slider + play/pause that steps a time-resolved surrogate's field."""
    lo = _bound(time_factor, "min", 0.0)
    hi = _bound(time_factor, "max", 1.0)
    point[TIME_FACTOR] = lo
    with ui.row().classes("w-full items-center gap-3"):
        ui.icon("schedule").style("color: #888;")
        ui.label("Time").style("font-size: 0.82rem; color: #888;")
        t_lbl = ui.label(f"{lo:.4g}").style("font-size: 0.78rem; font-weight: 600;")
        t_slider = ui.slider(min=lo, max=hi, value=lo, step=1.0).classes("flex-1")

        def _on_time(_e: Any = None) -> None:
            point[TIME_FACTOR] = float(t_slider.value)
            t_lbl.text = f"{float(t_slider.value):.4g}"
            ui.timer(0.0, redraw, once=True)

        t_slider.on("update:model-value", _on_time, throttle=0.2)

        def _tick() -> None:
            nxt = point.get(TIME_FACTOR, lo) + 1.0
            if nxt > hi:
                nxt = lo
            t_slider.value = nxt  # moves the handle; a programmatic set won't fire the event
            _on_time()  # ...so apply the new time + redraw directly

        timer = ui.timer(0.8, _tick, active=False)

        def _toggle_play() -> None:
            state["playing"] = not state["playing"]
            timer.active = state["playing"]
            play_btn.props(f"icon={'pause' if state['playing'] else 'play_arrow'}")

        play_btn = ui.button(icon="play_arrow", on_click=_toggle_play).props("flat dense round")


__all__ = ["render_field_viewer"]
