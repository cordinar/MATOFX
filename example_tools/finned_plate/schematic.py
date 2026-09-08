"""To-scale design schematic for the Finned Cold Plate.

This is the picture that makes the **inputs** visible
(`designs/finned-plate-tool.md` §7a). Two of the four design variables — fin count and
fin height — are geometry, so a results contour alone cannot show what a design *is* or
how two runs differ. Every job therefore carries a drawing of the design it ran, at a
single shared scale, so a crowded design looks crowded and a tall one looks tall.

Drawing conventions, which are the honest part:

* the **baseplate is painted with the solved temperature field** on the mid-plane
  slice through the component — hot underneath the heat source, cooling outward and
  upward — because that spreading is the thing the finite-element model exists to
  compute, and a single flat colour would hide it;
* the **fins are drawn in neutral grey**, because they are a lumped surface
  conductance, not a meshed solid;
* the **component footprint** is outlined on the underside, directly under the hot
  spot it causes;
* a **colour bar** fixes the scale, so a shade means the same temperature in every
  run and two designs can be compared by eye;
* a caption states both facts in one line.

That colour/grey split is standard finite-element post-processing idiom and reads
correctly before anyone reaches the caption: coloured means solved, grey means not.

Pillow (HPND) only — no plotting stack needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from plate_mesh import COMPONENT_SIDE_M, PLATE_WIDTH_M

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fin_model import Material

# Three separate scales, kept distinct so the layout maths never has to change:
#
# ``_W`` x ``_H``   the *layout* coordinate space every position below is written in;
# ``_OUT_SCALE``    how much bigger the saved PNG is than that space;
# ``_SS``           how much bigger the internal canvas is than that space.
#
# The image is drawn at ``_SS`` and downsampled to ``_OUT_SCALE``, so the ratio between
# them (here 2x) is the true supersampling — that is what keeps text and fin edges crisp
# rather than merely large. Font sizes and line widths are all written as multiples of
# ``_SS``, so they stay proportional automatically when these change.
_W, _H = 780, 452
_OUT_SCALE = 3  # saved at 2340 x 1356 — legible full-screen and when projected
_SS = 6  # internal canvas 4680 x 2712, downsampled 2:1

_INK = (33, 52, 64, 255)
_MUTED = (96, 112, 124, 255)
_FIN = (176, 186, 196, 255)
_FIN_EDGE = (120, 132, 144, 255)
_OUTLINE = (60, 80, 92, 255)
_AIR = (38, 105, 140, 255)
_COMPONENT = (196, 62, 52, 255)

# Cool -> hot ramp for the baseplate, matching the heat_sink schematic's palette.
_STOPS: tuple[tuple[float, tuple[int, int, int]], ...] = (
    (0.0, (69, 117, 180)),
    (0.5, (254, 178, 76)),
    (1.0, (215, 48, 39)),
)

#: Temperature range the plate colour is normalised against, so the same shade means
#: the same temperature in every run — essential when two designs are compared on a
#: slide.
_T_COLD, _T_HOT = 25.0, 125.0


def _colour(temp_c: float) -> tuple[int, int, int]:
    t = (temp_c - _T_COLD) / (_T_HOT - _T_COLD)
    t = min(1.0, max(0.0, t))
    for i in range(len(_STOPS) - 1):
        p0, c0 = _STOPS[i]
        p1, c1 = _STOPS[i + 1]
        if t <= p1:
            f = 0.0 if p1 == p0 else (t - p0) / (p1 - p0)
            return tuple(int(a + (b - a) * f) for a, b in zip(c0, c1, strict=True))  # type: ignore[return-value]
    return _STOPS[-1][1]


def _colour_array(temps: np.ndarray) -> np.ndarray:
    """Vectorised form of :func:`_colour` — maps an array of degC to RGB."""
    t = np.clip((temps - _T_COLD) / (_T_HOT - _T_COLD), 0.0, 1.0)
    stops = np.array([p for p, _ in _STOPS])
    cols = np.array([c for _, c in _STOPS], dtype=float)
    out = np.empty((*t.shape, 3))
    for channel in range(3):
        out[..., channel] = np.interp(t, stops, cols[:, channel])
    return out.round().astype(np.uint8)


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = (["arialbd.ttf", "Arial Bold.ttf"] if bold else ["arial.ttf", "Arial.ttf"]) + [
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    ]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


def _draw_colour_bar(img: Image.Image, font: object) -> None:
    """Draw the fixed temperature scale under the geometry summary.

    Never autoscaled per run: the whole point is that the same shade means the same
    temperature in two different schematics, so two designs can be compared by eye
    instead of re-reading an axis each time.
    """
    bw = img.width
    cb_w, cb_h = 260 * _SS, 11 * _SS
    cb_x0, cb_y0 = bw // 2 - cb_w // 2, 404 * _SS
    ramp = _colour_array(np.linspace(_T_COLD, _T_HOT, 256)[None, :])
    img.paste(Image.fromarray(ramp, "RGB").resize((cb_w, cb_h), Image.BILINEAR), (cb_x0, cb_y0))

    draw = ImageDraw.Draw(img)
    draw.rectangle(
        [cb_x0, cb_y0, cb_x0 + cb_w, cb_y0 + cb_h], outline=(150, 160, 170), width=1 * _SS
    )
    mid_y = cb_y0 + cb_h // 2
    draw.text((cb_x0 - 8 * _SS, mid_y), f"{_T_COLD:.0f}", font=font, fill=_MUTED, anchor="rm")  # type: ignore[arg-type]
    draw.text(
        (cb_x0 + cb_w + 8 * _SS, mid_y),
        f"{_T_HOT:.0f} degC",
        font=font,  # type: ignore[arg-type]
        fill=_MUTED,
        anchor="lm",
    )


def write_schematic(
    path: Path,
    *,
    fin_count: int,
    fin_height_mm: float,
    base_thickness_mm: float,
    fan_duty_pct: float,
    material: Material,
    t_source: float,
    mass_g: float,
    velocity_ms: float,
    turbulent: bool,
    section: np.ndarray,
) -> None:
    """Draw this run's cold plate, end-on, to scale.

    ``section`` is the solved mid-plane temperature slice from
    :func:`plate_mesh.cross_section_grid` — ``(nz, ny)``, row 0 the finned top face.
    """
    bw, bh = _W * _SS, _H * _SS
    img = Image.new("RGB", (bw, bh), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, bw - 1, bh - 1], outline=(208, 216, 228), width=2 * _SS)

    f_title = _font(21 * _SS, bold=True)
    f_head = _font(14 * _SS, bold=True)
    f_small = _font(11 * _SS)
    f_cap = _font(10 * _SS)

    draw.text((bw // 2, 30 * _SS), "Finned Cold Plate", font=f_title, fill=_INK, anchor="mm")
    draw.text(
        (bw // 2, 56 * _SS),
        f"T = {t_source:.0f} degC      mass = {mass_g:.0f} g      "
        f"air = {velocity_ms:.1f} m/s ({'turbulent' if turbulent else 'laminar'})",
        font=f_head,
        fill=_MUTED,
        anchor="mm",
    )

    # --- Drawing region ----------------------------------------------------
    rx0, ry0, rx1, ry1 = 52 * _SS, 88 * _SS, 728 * _SS, 372 * _SS
    below = 34 * _SS  # room under the plate for the component box and its label
    plate_w_mm = PLATE_WIDTH_M * 1000.0
    total_h_mm = base_thickness_mm + fin_height_mm
    # Fit the whole stack (base + fins) inside the region, then anchor it to a FIXED
    # baseline. Sizing by a percentage of headroom instead lets the tallest designs
    # overrun the headline above; fitting the real stack height cannot. The fixed
    # baseline is also what lets two runs be compared at a glance — the plate sits in
    # the same place every time, and only the design above it changes.
    scale = min((rx1 - rx0) / plate_w_mm, (ry1 - ry0 - below) / total_h_mm) * 0.96

    plate_w = plate_w_mm * scale
    base_h = base_thickness_mm * scale
    fin_h = fin_height_mm * scale
    cx = (rx0 + rx1) / 2
    x0 = cx - plate_w / 2
    base_bottom = ry1 - below
    base_top = base_bottom - base_h
    fin_top = base_top - fin_h

    # Fins — neutral grey: modelled, not meshed.
    pitch = plate_w / fin_count
    fin_w = max((1.0 * scale), 1.6 * _SS)  # 1 mm fins, with a visibility floor
    for i in range(fin_count):
        fx = x0 + (i + 0.5) * pitch - fin_w / 2
        draw.rectangle([fx, fin_top, fx + fin_w, base_top], fill=_FIN, outline=_FIN_EDGE)

    # Baseplate — painted with the SOLVED field on this slice. The grid is coarse
    # (one row per element layer) and is upsampled bilinearly, which is honest: it is
    # exactly the resolution the solve produced, smoothed for display only.
    px0, py0 = round(x0), round(base_top)
    px1, py1 = round(x0 + plate_w), round(base_bottom)
    field = Image.fromarray(_colour_array(section), "RGB").resize(
        (max(px1 - px0, 1), max(py1 - py0, 1)), Image.BILINEAR
    )
    img.paste(field, (px0, py0))
    draw.rectangle([px0, py0, px1, py1], outline=_OUTLINE, width=1 * _SS)

    # Component footprint, outlined on the underside.
    comp_w = (COMPONENT_SIDE_M * 1000.0) * scale
    comp_h = 9 * _SS
    draw.rectangle(
        [cx - comp_w / 2, base_bottom, cx + comp_w / 2, base_bottom + comp_h],
        fill=(255, 255, 255),
        outline=_COMPONENT,
        width=2 * _SS,
    )
    draw.text(
        (cx, base_bottom + comp_h + 13 * _SS),
        f"component {COMPONENT_SIDE_M * 1000:.0f} x {COMPONENT_SIDE_M * 1000:.0f} mm",
        font=f_small,
        fill=_COMPONENT,
        anchor="mm",
    )

    # Airflow arrows through the channels.
    head = 9 * _SS
    for frac in (0.35, 0.72):
        ay = fin_top + fin_h * frac if fin_h > 24 * _SS else fin_top - 14 * _SS
        ax1 = x0 - 8 * _SS
        ax0 = max(rx0 - 22 * _SS, ax1 - 46 * _SS)
        draw.line([(ax0, ay), (ax1, ay)], fill=_AIR, width=3 * _SS)
        draw.polygon(
            [(ax1 + head, ay), (ax1, ay - head * 0.65), (ax1, ay + head * 0.65)], fill=_AIR
        )
    draw.text(
        # Clamped to the region: a 45 mm fin puts fin_top near the top edge.
        (30 * _SS, max(fin_top - 24 * _SS, ry0 - 6 * _SS)),
        f"fan {fan_duty_pct:.0f} %",
        font=f_small,
        fill=_AIR,
        anchor="lm",
    )

    _draw_colour_bar(img, f_cap)

    # --- Geometry summary + the honesty caption ---------------------------
    draw.text(
        (bw // 2, 384 * _SS),
        f"{fin_count} fins  |  {fin_height_mm:.0f} mm tall  |  base {base_thickness_mm:.1f} mm  "
        f"|  {material.name} (k = {material.conductivity:.0f} W/m/K)",
        font=f_small,
        fill=_MUTED,
        anchor="mm",
    )
    draw.text(
        (bw // 2, 436 * _SS),
        "Solved: temperature through the baseplate.  Fins drawn to scale, modelled as a "
        "surface conductance rather than meshed.",
        font=f_cap,
        fill=(140, 150, 160),
        anchor="mm",
    )

    out = img.resize((_W * _OUT_SCALE, _H * _OUT_SCALE), Image.LANCZOS)
    out.save(path, "PNG", optimize=True)
