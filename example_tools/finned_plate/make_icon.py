"""Regenerate ``finned_plate.png``, the tool icon.

Run once; the PNG is committed alongside the bundle. Kept in the bundle so the icon can
be reproduced rather than being an opaque binary nobody can regenerate.

    .venv/Scripts/python.exe example_tools/finned_plate/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

_SS = 4
_SIZE = 128

_PLATE = (64, 92, 116)
_PLATE_EDGE = (44, 68, 90)
_FIN = (150, 166, 182)
_FIN_EDGE = (108, 126, 144)
_COMPONENT = (196, 62, 52)
_AIR = (64, 161, 164)


def main() -> int:
    """Draw the icon and write it next to this script."""
    n = _SIZE * _SS
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    left, right = int(n * 0.13), int(n * 0.87)
    fin_top, base_top, base_bottom = int(n * 0.26), int(n * 0.60), int(n * 0.74)

    # Fin array, drawn to a believable pitch.
    count = 9
    span = right - left
    pitch = span / count
    fin_w = max(pitch * 0.26, 2 * _SS)
    for i in range(count):
        x = left + (i + 0.5) * pitch - fin_w / 2
        draw.rectangle([x, fin_top, x + fin_w, base_top], fill=_FIN, outline=_FIN_EDGE, width=_SS)

    # Baseplate.
    draw.rounded_rectangle(
        [left, base_top, right, base_bottom],
        radius=3 * _SS,
        fill=_PLATE,
        outline=_PLATE_EDGE,
        width=2 * _SS,
    )

    # Component on the underside.
    cw = span * 0.30
    cx = (left + right) / 2
    draw.rounded_rectangle(
        [cx - cw / 2, base_bottom, cx + cw / 2, base_bottom + int(n * 0.09)],
        radius=2 * _SS,
        fill=_COMPONENT,
    )

    # Airflow.
    head = int(n * 0.045)
    for frac in (0.38, 0.72):
        y = fin_top + (base_top - fin_top) * frac
        x1 = left - int(n * 0.02)
        x0 = int(n * 0.02)
        draw.line([(x0, y), (x1, y)], fill=_AIR, width=3 * _SS)
        draw.polygon([(x1 + head, y), (x1, y - head * 0.62), (x1, y + head * 0.62)], fill=_AIR)

    out = Path(__file__).with_name("finned_plate.png")
    img.resize((_SIZE, _SIZE), Image.LANCZOS).save(out, "PNG")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
