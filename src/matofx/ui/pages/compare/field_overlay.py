"""Field-output value object for the Compare workspace.

A ``file_type = "field"`` output (e.g. the transient-plate tool's
time-resolved ``.vtp`` series) renders on the Job page as a coloured
3-D ``Mesh3d`` surface (or value-vs-node line for ``.npy``) with a
play/pause + step slider.

:class:`JobFieldOutput` is the cheap carrier the Compare page builds by
fetching each selected job's field frames from the parsed ``field-frames``
endpoint. The page hands the frames to the cell renderer so the animation
draws **inline in the Outputs card** (the same place every other output
lives — :func:`matofx.ui.pages.compare.render._render_field_row`), and
passes the same objects to the report builder so the **HTML** report
embeds the animation too. Keeping the value object here (no NiceGUI
import) means both consumers share one shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class JobFieldOutput:
    """One ``field`` output produced by one selected job.

    ``data`` is the parsed ``field-frames`` payload — the shared mesh
    (``has_geometry`` / ``points`` / ``i,j,k``) plus the per-timestep
    ``frames`` — fed straight into the shared figure builder.
    """

    job_id: int
    job_label: str
    field_name: str
    """Descriptor output name — the natural grouping key across jobs."""

    field_label: str
    """Human label for the chart title (the descriptor's ``label``)."""

    filename: str
    """The on-disk filename under ``outputs/`` — first frame of the series."""

    data: dict[str, Any]
    """Parsed ``field-frames`` payload: shared mesh + per-frame values."""
