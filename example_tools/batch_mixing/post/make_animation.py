"""Render the blend as an MP4 from a run's alpha snapshots.

MATOFX plays MP4 inline (HTML5 <video controls>), and H.264 is far smaller and
crisper than an animated GIF. Each frame is a two-panel view of the y<0 section
coloured by the additive fraction (fixed 0..1 scale): the left panel side-on
(cut plane facing the camera), the right panel angled + tilted down so the
additive layer on the tank top is visible. Encoding uses imageio + the bundled
imageio-ffmpeg (no system ffmpeg / sudo needed).

    python make_animation.py --series <dir> --out blend.mp4
      <dir> holds either  *.vtm.series (+ <name>/internal.vtu)  or  alpha_step_*.vtu
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path

import imageio.v2 as imageio
import numpy as np
import pyvista as pv

pv.OFF_SCREEN = True
pv.global_theme.allow_empty_mesh = True
FIELD = "alpha.additive"


def _frames(series_dir: Path) -> list[tuple[float, str]]:
    ser = glob.glob(str(series_dir / "*.vtm.series"))
    if ser:
        js = json.loads(Path(ser[0]).read_text(encoding="utf-8"))
        out = []
        for f in js["files"]:
            d = series_dir / f["name"].replace(".vtm", "")
            if (d / "internal.vtu").is_file():
                out.append((float(f["time"]), str(d / "internal.vtu")))
        return sorted(out)
    vtus = sorted(glob.glob(str(series_dir / "alpha_step_*.vtu")))
    return [(float(i), v) for i, v in enumerate(vtus)]


def make_movie(series_dir: str | Path, out_path: str | Path, fps: int = 12) -> str:
    """Render the section blend to an H.264 MP4. Returns the output path."""
    series_dir = Path(series_dir)
    frames = _frames(series_dir)
    if not frames:
        raise SystemExit("no snapshots found for animation")
    zc = float(pv.read(frames[0][1]).bounds[4] + pv.read(frames[0][1]).bounds[5]) / 2.0

    writer = imageio.get_writer(
        str(out_path), fps=fps, codec="libx264", quality=8, macro_block_size=None,
        pixelformat="yuv420p",                       # browser-decodable H.264
        ffmpeg_params=["-movflags", "+faststart"],   # web-streamable (moov up front)
    )
    try:
        for t, vtu in frames:
            m = pv.read(vtu)
            clip = m.clip(normal="y", origin=(0, 0, zc))
            pl = pv.Plotter(shape=(1, 2), off_screen=True, window_size=(1280, 760), border=False)
            pl.set_background("white")
            # Left: side section (cut plane facing the camera — the familiar view).
            pl.subplot(0, 0)
            pl.add_mesh(clip, scalars=FIELD, cmap="YlGnBu", clim=(0, 1), show_scalar_bar=False)
            pl.add_text(f"t = {t:5.1f} s", font_size=12, color="black")
            pl.camera_position = "xz"
            pl.reset_camera()
            pl.camera.zoom(0.85)
            # Right: angled + tilted down so the additive layer on TOP is visible.
            pl.subplot(0, 1)
            pl.add_mesh(clip, scalars=FIELD, cmap="YlGnBu", clim=(0, 1), show_scalar_bar=True,
                        scalar_bar_args=dict(title="additive", color="black", n_labels=3))
            pl.camera_position = "yz"
            pl.reset_camera()
            pl.camera.azimuth = -45
            pl.camera.elevation = 25
            pl.camera.zoom(0.8)
            img = np.asarray(pl.screenshot(return_img=True))
            pl.close()
            # libx264 needs even dimensions; crop the last row/col if odd.
            h, w = img.shape[:2]
            img = img[: h - (h % 2), : w - (w % 2)]
            writer.append_data(img)
    finally:
        writer.close()
    return str(out_path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Render a Batch Mixing blend MP4.")
    ap.add_argument("--series", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fps", type=int, default=12)
    a = ap.parse_args()
    print("wrote", make_movie(a.series, a.out, a.fps))
