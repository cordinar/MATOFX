"""Server-side reader for study children's ``csv_plot`` files (2-PS-b1).

The data layer behind two 2-PS-b surfaces:

- **Curve-overlay** (2-PS-b4): :meth:`SeriesService.get_series` returns
  each child's ``(x, y)`` series from a declared ``csv_plot`` output,
  **down-sampled** so a 10k-row x 200-child sweep stays a bounded
  payload.
- **Computed response-from-column** (2-PS-b2): :meth:`SeriesService.
  get_aggregate` reduces a chosen column to one scalar per child
  (``last`` / ``max`` / ``min`` / ``mean``) — the deferred half of the
  b0-5 response picker.

Files are read **off disk on the server** (they survive cleanup per the
ADR-0003 retention floor + the descriptor's ``retain_files``), resolved
through the same ``StorageLocation.mount_path`` / ``working_dir_relative``
path the jobs-output endpoints use. ACL is delegated to
:meth:`StudyService.get` (project VIEW).
"""

from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.hierarchy.orm import Job
from matofx.studies.orm import ParametricStudy, StudyResult
from matofx.studies.service import StudyService

_DEFAULT_MAX_POINTS = 500
"""Per-series down-sample cap for the overlay — keeps the payload bounded
regardless of how many rows a tool's ``csv_plot`` file has."""

_MIN_DOWNSAMPLE_POINTS = 2
"""Below this a stride-based down-sample is meaningless; return as-is."""

_AGGREGATES: tuple[str, ...] = ("last", "max", "min", "mean")


@dataclass(frozen=True, slots=True)
class ChildSeries:
    """One child's down-sampled ``(x, y)`` series for the overlay."""

    job_id: int
    x: list[float]
    y: list[float]


@dataclass(frozen=True, slots=True)
class SeriesView:
    """All children's series for one ``csv_plot`` output."""

    study_id: int
    output_name: str
    x_column: int
    y_column: int
    children: list[ChildSeries]


@dataclass(frozen=True, slots=True)
class ChildAggregate:
    """One child's column reduced to a single scalar."""

    job_id: int
    value: float | None


@dataclass(frozen=True, slots=True)
class AggregateView:
    """Per-child aggregate of a ``csv_plot`` column (the computed response)."""

    study_id: int
    output_name: str
    column: int
    aggregate: str
    values: list[ChildAggregate]


# --- pure CSV helpers (no DB/IO beyond the file read) --------------------


def _is_float(token: str) -> bool:
    try:
        float(token)
    except (TypeError, ValueError):
        return False
    return True


def _read_numeric_rows(path: Path) -> list[list[float | None]]:
    """Parse a CSV into numeric rows; a leading non-numeric row is a header.

    Non-numeric cells become ``None`` (kept positional so column
    indexing stays aligned). Empty file → ``[]``.
    """
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return []
    start = 1 if any(not _is_float(c) for c in rows[0]) else 0
    out: list[list[float | None]] = []
    for row in rows[start:]:
        out.append([float(c) if _is_float(c) else None for c in row])
    return out


def _downsample_pairs(
    xs: list[float], ys: list[float], max_points: int
) -> tuple[list[float], list[float]]:
    """Even-stride down-sample, keeping x/y aligned and the endpoints."""
    n = len(xs)
    if n <= max_points or max_points < _MIN_DOWNSAMPLE_POINTS:
        return xs, ys
    stride = (n - 1) / (max_points - 1)
    idx = sorted({round(i * stride) for i in range(max_points)})
    return [xs[i] for i in idx], [ys[i] for i in idx]


def _extract_xy(
    path: Path, x_col: int, y_col: int, max_points: int
) -> tuple[list[float], list[float]]:
    """``(x, y)`` from two columns, skipping rows missing either, down-sampled."""
    xs: list[float] = []
    ys: list[float] = []
    for row in _read_numeric_rows(path):
        if x_col < len(row) and y_col < len(row):
            xv, yv = row[x_col], row[y_col]
            if xv is not None and yv is not None:
                xs.append(xv)
                ys.append(yv)
    return _downsample_pairs(xs, ys, max_points)


def _aggregate(path: Path, column: int, how: str) -> float | None:
    """Reduce one column to a scalar (``last``/``max``/``min``/``mean``)."""
    vals: list[float] = []
    for row in _read_numeric_rows(path):
        if column < len(row):
            cell = row[column]
            if cell is not None:
                vals.append(cell)
    if not vals:
        return None
    if how == "last":
        return vals[-1]
    if how == "max":
        return max(vals)
    if how == "min":
        return min(vals)
    if how == "mean":
        return sum(vals) / len(vals)
    return None


def _csv_filename_for(retained_files: Any, output_name: str) -> str | None:
    """The retained ``csv_plot`` file matching ``output_name`` (or None)."""
    if not isinstance(retained_files, list):
        return None
    for entry in retained_files:
        if (
            isinstance(entry, dict)
            and entry.get("kind") == "csv_plot"
            and str(entry.get("name") or "") == output_name
            and isinstance(entry.get("filename"), str)
            and entry.get("filename")
        ):
            return str(entry["filename"])
    return None


def _resolve_under_outputs(outputs_dir: Path, filename: str) -> Path | None:
    """Map a working-dir-relative descriptor filename to a real file path.

    Mirrors ``jobs.get_job_output``: strips a redundant ``outputs/``
    prefix, normalises backslashes, and guards against path traversal
    outside the job's ``outputs/`` directory.
    """
    cleaned = filename.replace("\\", "/").strip("/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned.startswith("outputs/"):
        cleaned = cleaned[len("outputs/") :]
    if not cleaned or cleaned in (".", "..") or cleaned.startswith("../"):
        return None
    target = (outputs_dir / cleaned).resolve()
    try:
        target.relative_to(outputs_dir.resolve())
    except ValueError:
        return None
    return target if target.is_file() else None


async def _job_outputs_dir(db: AsyncSession, job: Job) -> Path | None:
    """``<mount>/<working_dir_relative>/outputs`` for a staged job, else None."""
    from matofx.tools.exec_orm import StorageLocation  # noqa: PLC0415

    if job.storage_location_id is None or job.working_dir_relative is None:
        return None
    location = await db.get(StorageLocation, job.storage_location_id)
    if location is None:
        return None
    return Path(location.mount_path) / job.working_dir_relative / "outputs"


# --- service -------------------------------------------------------------


class SeriesService:
    """Read ``csv_plot`` series / aggregates for a study's children."""

    def __init__(self, study_service: StudyService) -> None:
        """Compose over a :class:`StudyService` for the ACL gate."""
        self._studies = study_service

    async def _children_with_results(
        self, db: AsyncSession, study: ParametricStudy
    ) -> list[tuple[Job, StudyResult]]:
        # Pin the join to this study's ``parametric`` result rows — a job
        # can also carry a ``cohort`` row (the lazy cohort populate), and a
        # bare ``job_id`` join would fan those out, duplicating every series.
        stmt = (
            select(Job, StudyResult)
            .join(
                StudyResult,
                and_(
                    StudyResult.job_id == Job.id,
                    StudyResult.source_kind == "parametric",
                    StudyResult.source_id == study.id,
                ),
            )
            .where(Job.parametric_study_id == study.id)
            .order_by(Job.id.asc())
        )
        return [(job, result) for job, result in (await db.execute(stmt)).all()]

    async def get_series(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        study_id: int,
        output_name: str,
        x_column: int = 0,
        y_column: int = 1,
        max_points: int = _DEFAULT_MAX_POINTS,
    ) -> SeriesView:
        """Per-child down-sampled ``(x, y)`` series for one ``csv_plot`` output."""
        study = await self._studies.get(db, actor_user_id=actor_user_id, study_id=study_id)
        series: list[ChildSeries] = []
        for job, result in await self._children_with_results(db, study):
            fname = _csv_filename_for(result.retained_files, output_name)
            if fname is None:
                continue
            outputs_dir = await _job_outputs_dir(db, job)
            if outputs_dir is None:
                continue
            path = _resolve_under_outputs(outputs_dir, fname)
            if path is None:
                continue
            xs, ys = await asyncio.to_thread(_extract_xy, path, x_column, y_column, max_points)
            if xs:
                series.append(ChildSeries(job_id=int(job.id), x=xs, y=ys))
        return SeriesView(
            study_id=int(study.id),
            output_name=output_name,
            x_column=x_column,
            y_column=y_column,
            children=series,
        )

    async def get_aggregate(
        self,
        db: AsyncSession,
        *,
        actor_user_id: int,
        study_id: int,
        output_name: str,
        column: int,
        aggregate: str,
    ) -> AggregateView:
        """Per-child reduction of one ``csv_plot`` column to a scalar response."""
        how = aggregate if aggregate in _AGGREGATES else "last"
        study = await self._studies.get(db, actor_user_id=actor_user_id, study_id=study_id)
        values: list[ChildAggregate] = []
        for job, result in await self._children_with_results(db, study):
            fname = _csv_filename_for(result.retained_files, output_name)
            value: float | None = None
            if fname is not None:
                outputs_dir = await _job_outputs_dir(db, job)
                path = _resolve_under_outputs(outputs_dir, fname) if outputs_dir else None
                if path is not None:
                    value = await asyncio.to_thread(_aggregate, path, column, how)
            values.append(ChildAggregate(job_id=int(job.id), value=value))
        return AggregateView(
            study_id=int(study.id),
            output_name=output_name,
            column=column,
            aggregate=how,
            values=values,
        )


def make_series_service(study_service: StudyService) -> SeriesService:
    """Wire up a :class:`SeriesService` over an existing :class:`StudyService`."""
    return SeriesService(study_service)


__all__ = [
    "AggregateView",
    "ChildAggregate",
    "ChildSeries",
    "SeriesService",
    "SeriesView",
    "make_series_service",
]
