"""Finned heat-sink performance model — fast analytic demo tool.

A deliberately lightweight, closed-form model of a forced-convection
plate-fin heat sink. It runs in microseconds (no time-stepping solve)
so it is ideal for parametric studies: hundreds of runs finish in a
few seconds, and every output is smooth and finite across the whole
design box — no numerical divergence to poison the statistics.

The physics is a reduced-order engineering model, not a validated CFD
solve. It captures the behaviours that make design-of-experiments
interesting:

* **Strong, differing main effects** — air speed dominates dissipation,
  conductivity drives fin efficiency, geometry drives mass.
* **Curvature + an interior optimum** — packing more fins onto a fixed
  base first helps (more area) then hurts (the channels choke airflow,
  so the per-fin heat-transfer coefficient collapses). Dissipation and
  dissipation-per-gram both peak at an interior fin count / base width,
  which is exactly what response-surface and surrogate optimisation are
  for.
* **Interactions** — fin efficiency couples height, thickness and
  conductivity; pressure drop couples speed and spacing.
* **Competing objectives** — cooling vs. mass vs. pumping power give a
  non-trivial Pareto front.
* **Monotone-but-nonlinear responses** (thermal resistance ~ 1/Q,
  pumping power ~ v^3) so Pearson and Spearman sensitivities differ.

Outputs: seven scalars, two CSV curves (fin temperature + heat-flux
profiles, for curve-overlay), one interactive Plotly HTML figure, and a
PNG schematic drawn to scale from this run's geometry and coloured by
its computed fin temperature profile.

Depends on numpy (BSD-3), pandas (BSD-3), plotly (MIT), Pillow (HPND).
All permissive.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from PIL import Image, ImageDraw, ImageFont

# --- Fixed model constants -------------------------------------------------
# These are tool-author choices, not end-user inputs (per the SPDM
# "expose only what the engineer should tune" rule). They set the scale
# of the results; the seven descriptor inputs are the design variables.

_FLOW_DEPTH: float = 0.06  # fin length in the flow direction, m
_BASE_THICKNESS: float = 0.005  # solid baseplate thickness, m
_MATERIAL_DENSITY: float = 2700.0  # kg/m^3 (aluminium); conductivity is the design lever
_H_NATURAL: float = 5.0  # natural-convection floor, W/m^2K (keeps Q > 0 everywhere)
_CONV_SCALE: float = 12.0  # forced-convection coefficient scale
_VELOCITY_EXP: float = 0.8  # turbulent flat-plate exponent on air speed
_CHAR_GAP: float = 0.0015  # characteristic channel gap for the choking term, m
_RHO_AIR: float = 1.2  # kg/m^3
_DP_SCALE: float = 0.05  # lumped channel pressure-drop coefficient
_T_AMBIENT: float = 25.0  # degC — base for absolute fin temperatures
_PROFILE_POINTS: int = 60  # samples along the fin for the CSV / HTML curves
_MIN_GAP: float = 1.0e-4  # numerical floor for divisions by the channel gap, m

# Temperature colour ramp for the schematic (cool ambient -> hot base).
_TEMP_STOPS: tuple[tuple[float, tuple[int, int, int]], ...] = (
    (0.0, (69, 117, 180)),  # cool blue
    (0.5, (254, 178, 76)),  # amber
    (1.0, (215, 48, 39)),  # hot red
)


def _read_inputs() -> dict[str, Any]:
    payload = json.loads(Path("inputs/inputs.json").read_text(encoding="utf-8"))
    return {entry["name"]: entry["value"] for entry in payload["inputs"]}


def _flow_factor(gap: float) -> float:
    """Fraction of forced convection retained as the channel narrows.

    Wide channels (gap >> characteristic) keep full forced convection;
    as fins crowd together the boundary layers merge and airflow chokes,
    so the coefficient falls off as ``gap**2``. This is what produces an
    interior optimum in fin count / base width.
    """
    g = max(gap, 0.0)
    return g * g / (g * g + _CHAR_GAP * _CHAR_GAP)


def _temp_colour(t: float) -> tuple[int, int, int]:
    """Map a normalised temperature ``t`` in [0, 1] to an RGB colour."""
    t = min(1.0, max(0.0, t))
    for i in range(len(_TEMP_STOPS) - 1):
        p0, c0 = _TEMP_STOPS[i]
        p1, c1 = _TEMP_STOPS[i + 1]
        if t <= p1:
            f = 0.0 if p1 == p0 else (t - p0) / (p1 - p0)
            return tuple(int(a + (b - a) * f) for a, b in zip(c0, c1, strict=True))  # type: ignore[return-value]
    return _TEMP_STOPS[-1][1]


def _load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Best-effort TrueType font, degrading to the bundled default."""
    names = (["arialbd.ttf", "Arial Bold.ttf"] if bold else ["arial.ttf", "Arial.ttf"]) + [
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    ]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)  # Pillow >= 10.1 accepts a size
    except TypeError:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


def _write_schematic(out_dir: Path, *, geom: dict[str, float], perf: dict[str, float]) -> None:
    """Draw a to-scale PNG of this run's heat sink, coloured by temperature.

    The fin count, fin height/thickness and base width are drawn to a
    single shared scale (so a crowded or tall design *looks* crowded or
    tall), and every fin is shaded by the run's own ``cosh`` temperature
    profile — an efficient fin stays red to the tip, an inefficient one
    fades to blue. A colour bar maps shade to absolute temperature and
    the headline scalars are annotated.
    """
    ss = 3
    w, h = 760, 470
    bw, bh = w * ss, h * ss
    img = Image.new("RGBA", (bw, bh), (255, 255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, bw - 1, bh - 1], outline=(208, 216, 228, 255), width=3 * ss)

    f_title = _load_font(22 * ss, bold=True)
    f_head = _load_font(15 * ss, bold=True)
    f_small = _load_font(11 * ss)

    draw.text((bw // 2, 30 * ss), "Finned Heat Sink", font=f_title, fill=(33, 52, 64, 255), anchor="mm")
    headline = (
        f"Q = {perf['heat_dissipation']:.0f} W      "
        f"η = {perf['fin_efficiency']:.0f}%      "
        f"R = {perf['thermal_resistance']:.2f} K/W      "
        f"mass = {perf['mass']:.0f} g"
    )
    draw.text((bw // 2, 58 * ss), headline, font=f_head, fill=(70, 90, 105, 255), anchor="mm")

    # Drawing region for the sink (final px 90..520 x 95..405; leaves
    # room for the colour bar on the right and the summary text below).
    bx0, by0 = 90 * ss, 95 * ss
    bx1, by1 = 520 * ss, 405 * ss
    region_w = bx1 - bx0
    region_h = by1 - by0

    base_w_mm = geom["base_width_mm"]
    base_th_mm = geom["base_thickness_mm"]
    fin_h_mm = geom["fin_height_mm"]
    fin_t_mm = geom["fin_thickness_mm"]
    n = max(int(round(geom["fin_count"])), 1)

    phys_w = base_w_mm
    phys_h = base_th_mm + fin_h_mm
    scale = min(region_w / phys_w, region_h / phys_h) * 0.92

    sink_w = phys_w * scale
    base_h_px = base_th_mm * scale
    fin_h_px = fin_h_mm * scale
    sink_total_h = base_h_px + fin_h_px
    cx = (bx0 + bx1) / 2
    sink_x0 = cx - sink_w / 2
    sink_bottom = by0 + (region_h + sink_total_h) / 2  # vertically centred in the region
    base_top = sink_bottom - base_h_px
    fin_top = base_top - fin_h_px

    pitch_px = (base_w_mm / n) * scale
    fin_w_px = max(fin_t_mm * scale, 2.0)

    m = perf["m"]
    fin_h_m = geom["fin_height_mm"] / 1000.0
    temp_rise = perf["temp_rise"]
    cosh_mlf = float(np.cosh(perf["mLf"]))

    # Vertical temperature ramp shared by every fin + the baseplate.
    col = np.zeros((bh, 3), np.uint8)
    for y in range(int(fin_top), int(sink_bottom) + 1):
        if y < base_top:
            x_phys = (base_top - y) / scale / 1000.0  # m above the base
            temp = _T_AMBIENT + temp_rise * float(np.cosh(m * (fin_h_m - x_phys))) / cosh_mlf
            t_norm = (temp - _T_AMBIENT) / temp_rise if temp_rise else 1.0
        else:
            t_norm = 1.0  # baseplate is at the hottest (base) temperature
        col[y] = _temp_colour(t_norm)
    metal = np.repeat(col[:, None, :], bw, axis=1)
    metal_img = Image.fromarray(metal, "RGB").convert("RGBA")

    # Mask of fins (rounded tops) + baseplate.
    mask = Image.new("L", (bw, bh), 0)
    md = ImageDraw.Draw(mask)
    fin_rects: list[tuple[float, float]] = []
    for i in range(n):
        fc = sink_x0 + (i + 0.5) * pitch_px
        x0 = fc - fin_w_px / 2
        x1 = fc + fin_w_px / 2
        md.rounded_rectangle([x0, fin_top, x1, base_top + 1], radius=min(fin_w_px / 2, 14 * ss), fill=255)
        fin_rects.append((x0, x1))
    md.rounded_rectangle([sink_x0, base_top, sink_x0 + sink_w, sink_bottom], radius=8 * ss, fill=255)

    img = Image.composite(metal_img, img, mask)
    draw = ImageDraw.Draw(img)

    # Crisp outlines.
    oc = (60, 80, 92, 255)
    for x0, x1 in fin_rects:
        draw.rounded_rectangle(
            [x0, fin_top, x1, base_top + 1], radius=min(fin_w_px / 2, 14 * ss), outline=oc, width=ss
        )
    draw.rounded_rectangle(
        [sink_x0, base_top, sink_x0 + sink_w, sink_bottom], radius=8 * ss, outline=oc, width=2 * ss
    )

    # Airflow arrows entering from the left.
    acol = (38, 105, 140, 255)
    head = 12 * ss
    a_w = 5 * ss
    for frac in (0.32, 0.62):
        ay = int(fin_top + fin_h_px * frac)
        ax1 = sink_x0 - 6 * ss
        ax0 = max(bx0 + 4 * ss, ax1 - 70 * ss)
        draw.line([(ax0, ay), (ax1, ay)], fill=acol, width=a_w)
        draw.polygon([(ax1 + head, ay), (ax1, ay - head * 0.7), (ax1, ay + head * 0.7)], fill=acol)
    draw.text(
        (bx0 + 4 * ss, int(fin_top) - 16 * ss),
        f"Air {geom['air_velocity']:.1f} m/s",
        font=f_small,
        fill=acol,
        anchor="lm",
    )

    # Colour bar (ambient -> base temperature), to the right of the sink.
    cb_x0, cb_x1 = 560 * ss, 592 * ss
    cb_y0, cb_y1 = 120 * ss, 380 * ss
    cb_h = cb_y1 - cb_y0
    bar = np.zeros((cb_h, 1, 3), np.uint8)
    for j in range(cb_h):
        bar[j, 0] = _temp_colour(1.0 - j / max(cb_h - 1, 1))  # top = hot
    bar_img = Image.fromarray(np.repeat(bar, cb_x1 - cb_x0, axis=1), "RGB")
    img.paste(bar_img, (cb_x0, cb_y0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([cb_x0, cb_y0, cb_x1, cb_y1], outline=(120, 130, 140, 255), width=ss)
    base_temp = _T_AMBIENT + temp_rise
    draw.text((cb_x1 + 12 * ss, cb_y0), f"{base_temp:.0f}", font=f_small, fill=(60, 70, 80, 255), anchor="lm")
    draw.text(
        (cb_x1 + 12 * ss, (cb_y0 + cb_y1) // 2),
        f"{(_T_AMBIENT + base_temp) / 2:.0f}",
        font=f_small,
        fill=(60, 70, 80, 255),
        anchor="lm",
    )
    draw.text((cb_x1 + 12 * ss, cb_y1), f"{_T_AMBIENT:.0f}", font=f_small, fill=(60, 70, 80, 255), anchor="lm")
    draw.text(
        (cb_x0, cb_y0 - 18 * ss), "Temp (degC)", font=f_small, fill=(60, 70, 80, 255), anchor="lm"
    )

    # Geometry summary along the bottom.
    summary = (
        f"{n} fins  |  {fin_h_mm:.0f} x {fin_t_mm:.1f} mm fins  |  "
        f"base {base_w_mm:.0f} mm  |  k = {geom['thermal_conductivity']:.0f} W/m/K"
    )
    draw.text((bw // 2, 440 * ss), summary, font=f_small, fill=(90, 100, 110, 255), anchor="mm")

    icon = img.convert("RGB").resize((w, h), Image.LANCZOS)
    icon.save(out_dir / "heat_sink_schematic.png", "PNG")


def main() -> int:
    """Evaluate the heat-sink model and write scalars + curves + a Plotly HTML."""
    inputs = _read_inputs()

    # Design variables (convert mm -> m where applicable).
    fin_height = float(inputs["fin_height"]) / 1000.0  # Lf, m
    fin_thickness = float(inputs["fin_thickness"]) / 1000.0  # t, m
    fin_count = float(inputs["fin_count"])  # N (kept continuous for smooth surfaces)
    base_width = float(inputs["base_width"]) / 1000.0  # W, m
    air_velocity = float(inputs["air_velocity"])  # v, m/s
    conductivity = float(inputs["thermal_conductivity"])  # k, W/mK
    temp_rise = float(inputs["base_temperature_rise"])  # dT, degC above ambient

    # Channel geometry: fins of pitch W/N leave a gap of (pitch - thickness).
    pitch = base_width / fin_count
    gap = pitch - fin_thickness  # may go negative when fins jam; handled below

    # Convection coefficient: a natural-convection floor plus a forced
    # term that the channel-choking factor scales down as gaps close.
    h_forced = _CONV_SCALE * air_velocity**_VELOCITY_EXP * _flow_factor(gap)
    h_eff = _H_NATURAL + h_forced

    # Straight-fin efficiency (classic tanh(mL)/(mL)).
    m = np.sqrt(2.0 * h_eff / (conductivity * fin_thickness))
    mLf = m * fin_height  # noqa: N806 — physical convention
    fin_efficiency = float(np.tanh(mLf) / mLf)

    # Heat-transfer areas (both fin faces + the exposed base between fins).
    area_fins = fin_count * 2.0 * fin_height * _FLOW_DEPTH
    area_base = _FLOW_DEPTH * max(base_width - fin_count * fin_thickness, 0.0)
    area_effective = fin_efficiency * area_fins + area_base

    # Performance.
    conductance = h_eff * area_effective  # W/K
    heat_dissipation = conductance * temp_rise  # W
    thermal_resistance = temp_rise / heat_dissipation  # K/W  (== 1 / conductance)

    # Mass of solid baseplate + fins.
    volume = _FLOW_DEPTH * (
        base_width * _BASE_THICKNESS + fin_count * fin_thickness * fin_height
    )
    mass_g = _MATERIAL_DENSITY * volume * 1000.0  # grams
    specific_dissipation = heat_dissipation / mass_g  # W/g

    # Airflow penalties.
    gap_eff = max(gap, _MIN_GAP)
    pressure_drop = (
        _DP_SCALE * _RHO_AIR * air_velocity**2 * (_FLOW_DEPTH / gap_eff)
    )  # Pa
    flow_area = fin_count * gap_eff * _FLOW_DEPTH  # free cross-section, m^2
    volumetric_flow = air_velocity * flow_area  # m^3/s
    pumping_power = pressure_drop * volumetric_flow  # W

    # --- Curves along the fin (base x=0 -> tip x=Lf) -----------------------
    x = np.linspace(0.0, fin_height, _PROFILE_POINTS)
    temp_profile = _T_AMBIENT + temp_rise * np.cosh(m * (fin_height - x)) / np.cosh(mLf)
    heat_flux = h_eff * (temp_profile - _T_AMBIENT)  # W/m^2
    x_mm = x * 1000.0

    out_dir = Path("outputs")
    out_dir.mkdir(exist_ok=True)

    pd.DataFrame(
        {"Position": x_mm, "Temperature": temp_profile}
    ).to_csv(out_dir / "fin_temperature_profile.csv", index=False)
    pd.DataFrame(
        {"Position": x_mm, "HeatFlux": heat_flux}
    ).to_csv(out_dir / "fin_heat_flux_profile.csv", index=False)

    # --- Interactive HTML figure ------------------------------------------
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=x_mm,
            y=temp_profile,
            mode="lines+markers",
            name="Fin temperature",
            line={"color": "#c0392b", "width": 3},
        )
    )
    fig.add_hline(
        y=_T_AMBIENT,
        line={"color": "#2980b9", "dash": "dash"},
        annotation_text="Ambient",
        annotation_position="bottom right",
    )
    fig.update_layout(
        title=(
            f"Fin temperature profile — Q = {heat_dissipation:.1f} W, "
            f"η = {fin_efficiency * 100:.0f}%"
        ),
        xaxis_title="Distance from base [mm]",
        yaxis_title="Temperature [degC]",
        plot_bgcolor="white",
        paper_bgcolor="white",
        xaxis={"showgrid": True, "gridcolor": "#eee"},
        yaxis={"showgrid": True, "gridcolor": "#eee"},
    )
    fig.write_html(out_dir / "fin_temperature_profile.html", include_plotlyjs="cdn")

    # --- PNG schematic of this run's design -------------------------------
    _write_schematic(
        out_dir,
        geom={
            "fin_height_mm": float(inputs["fin_height"]),
            "fin_thickness_mm": float(inputs["fin_thickness"]),
            "fin_count": fin_count,
            "base_width_mm": float(inputs["base_width"]),
            "base_thickness_mm": _BASE_THICKNESS * 1000.0,
            "air_velocity": air_velocity,
            "thermal_conductivity": conductivity,
        },
        perf={
            "heat_dissipation": heat_dissipation,
            "fin_efficiency": fin_efficiency * 100.0,
            "thermal_resistance": thermal_resistance,
            "mass": mass_g,
            "m": float(m),
            "mLf": float(mLf),
            "temp_rise": temp_rise,
        },
    )

    document = {
        "schema_version": 1,
        "tool_alias": "heat_sink",
        "outputs": [
            {
                "name": "heat_dissipation",
                "label": "Heat dissipation",
                "type": "number",
                "unit": "W",
                "value": round(heat_dissipation, 2),
            },
            {
                "name": "thermal_resistance",
                "label": "Thermal resistance",
                "type": "number",
                "unit": "K/W",
                "value": round(thermal_resistance, 4),
            },
            {
                "name": "fin_efficiency",
                "label": "Fin efficiency",
                "type": "number",
                "unit": "%",
                "value": round(fin_efficiency * 100.0, 2),
            },
            {
                "name": "specific_dissipation",
                "label": "Dissipation per mass",
                "type": "number",
                "unit": "W/g",
                "value": round(specific_dissipation, 4),
            },
            {
                "name": "mass",
                "label": "Heat-sink mass",
                "type": "number",
                "unit": "g",
                "value": round(mass_g, 2),
            },
            {
                "name": "pressure_drop",
                "label": "Air-side pressure drop",
                "type": "number",
                "unit": "Pa",
                "value": round(pressure_drop, 2),
            },
            {
                "name": "pumping_power",
                "label": "Pumping power",
                "type": "number",
                "unit": "W",
                "value": round(pumping_power, 4),
            },
            {
                "name": "design_schematic",
                "label": "Design schematic",
                "type": "file",
                "filename": "heat_sink_schematic.png",
            },
            {
                "name": "temperature_profile_plot",
                "label": "Fin temperature profile",
                "type": "file",
                "filename": "fin_temperature_profile.html",
            },
            {
                "name": "temperature_profile_csv",
                "label": "Fin temperature profile (data)",
                "type": "file",
                "filename": "fin_temperature_profile.csv",
            },
            {
                "name": "heat_flux_csv",
                "label": "Fin heat-flux profile (data)",
                "type": "file",
                "filename": "fin_heat_flux_profile.csv",
            },
        ],
    }
    (out_dir / "outputs.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
