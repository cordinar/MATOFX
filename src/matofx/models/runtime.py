"""Runner-side entry point for a synthesised surrogate tool (ADR-0008 §7.2).

The auto-generated orchestrator (``surrogate_tool_alias`` tool) imports
:func:`load_surrogate_model_version` and calls it with the canonical
``bundle_path`` + ``expected_sha256`` baked in at synthesis. This runs in
the tool's run subprocess — which has **no database** — so the load is a
pure, sha256-verified **disk** read of the model bundle (the same
``load_scalar_bundle`` the in-process path uses), returning a
:class:`~matofx.models.service.ModelRuntime` ready to predict.

Non-negotiables honoured: the bundle sha256 is verified on every load
(#2); ``pickle.load`` is only ever reached via ``joblib`` inside
``load_scalar_bundle`` (#3); and this runs in the run-subprocess context,
never the web process (#9).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from matofx.models.bundle import (
    combined_field_reference_mesh_path,
    field_reference_mesh_path,
    load_combined_bundle,
    load_coverage_report,
    load_field_bundle,
    load_scalar_bundle,
)
from matofx.models.service import ModelRuntime

if TYPE_CHECKING:
    from matofx.studies.surrogate.field import FieldSurrogate


def load_surrogate_model_version(
    *,
    bundle_path: str | Path,
    expected_sha256: str,
    version_id: int | None = None,  # noqa: ARG001 - carried for diagnostics / lineage
) -> ModelRuntime:
    """Load + sha256-verify a model bundle from disk → a predict-ready runtime.

    ``version_id`` is carried for diagnostics / lineage only (the load is
    keyed on the path + sha). Raises
    :class:`~matofx.models.errors.ModelBundleCorruptError` on a sha
    mismatch — the orchestrator never returns predictions from a corrupt
    bundle.
    """
    surrogates = load_scalar_bundle(Path(bundle_path), expected_sha256)
    return ModelRuntime(responses=list(surrogates.keys()), _surrogates=surrogates)


def load_field_model_version(
    *,
    bundle_path: str | Path,
    expected_sha256: str,
    version_id: int | None = None,  # noqa: ARG001 - carried for diagnostics / lineage
) -> FieldSurrogate:
    """Load + sha256-verify a **field** model bundle → a predict-ready surrogate.

    Same integrity contract as :func:`load_surrogate_model_version` (sha
    verified on every load, joblib-only deserialisation); returns the POD
    :class:`~matofx.studies.surrogate.field.FieldSurrogate` whose
    ``predict({factor: value})`` reconstructs the nodal field. Used by both
    the synthesised field-tool orchestrator and the predicted-field viewer's
    (off-event-loop) predict endpoint.
    """
    return load_field_bundle(Path(bundle_path), expected_sha256)


def _write_field_file(
    out_dir: Path, name: str, field: list[float], file_format: str, mesh_path: Path | None
) -> str:
    """Write one predicted nodal field to disk; return the working-dir-relative path.

    A geometry-bearing format (vtu/vtk/…) is written as the reference mesh with
    the field attached as a point-data array named ``name`` (so a downstream
    consumer reads it via the descriptor's ``field_node_array_name``). A bare
    ``npy`` field (or a missing mesh) is written as a plain ``.npy`` value array.

    The reference mesh is a copy of a *training run's* output file, so it still
    carries that run's solver arrays. They are cleared before the prediction is
    attached: left in, a stale ``temperature`` array (identical whatever the
    inputs) sat beside the real ``temperature_field`` prediction, and ParaView
    or a downstream tool reading by name silently got the wrong field.
    """
    arr = np.asarray(field, dtype=float)
    fmt = file_format.lower().lstrip(".")
    if fmt != "npy" and mesh_path is not None:
        import pyvista  # noqa: PLC0415 - vendored alongside the field path

        mesh = pyvista.read(str(mesh_path))
        mesh.clear_data()
        mesh.point_data[name] = arr
        out_path = out_dir / f"{name}.{fmt}"
        mesh.save(str(out_path))
        return f"outputs/{name}.{fmt}"
    np.save(out_dir / f"{name}.npy", arr)
    return f"outputs/{name}.npy"


def _field_output_meta(
    bundle_dir: Path, manifest: dict[str, Any], kind: str, name: str
) -> tuple[str, Path | None]:
    """``(file_format, reference_mesh_path)`` for one field output of a bundle."""
    if kind == "combined":
        meta = (manifest.get("field_models") or {}).get(name) or {}
        fmt = str(meta.get("file_format") or "npy")
        return fmt, combined_field_reference_mesh_path(bundle_dir, name)
    fmt = str((manifest.get("field") or {}).get("file_format") or "npy")
    return fmt, field_reference_mesh_path(bundle_dir)


_log = logging.getLogger(__name__)


def _support_outputs(
    bundle_dir: Path, scalar_surrs: dict[str, Any], point: dict[str, float]
) -> list[dict[str, Any]]:
    """The ADR-0020 §8.1 support outputs, or ``[]`` for a pre-coverage bundle.

    **This is the case the guard exists for.** An exported surrogate runs
    unattended inside a workflow: nobody is watching a badge, so the only
    thing that can catch a runaway extrapolation is a number in
    ``outputs.json`` that an ADR-0017 validity rule can bound.

    Hence two outputs rather than one. ``support_sigma`` is **numeric**
    because output validity operates on numeric scalars and field arrays,
    never on status strings; ``support_status`` is the readable half for a
    human opening the job record.

    Best-effort by construction: a guard that could throw would turn a caveat
    into a failed job, so anything unexpected here yields no outputs rather
    than an exception.
    """
    stored = load_coverage_report(bundle_dir)
    if not stored:
        return []
    try:
        from matofx.studies.coverage import (  # noqa: PLC0415
            CoverageReport,
            edge_threshold_from,
            evaluate_support,
        )
        from matofx.studies.coverage.gp import relative_sigma  # noqa: PLC0415

        report = CoverageReport.from_json_obj(stored)
        threshold = edge_threshold_from(stored)
        sigma: float | None = None
        if scalar_surrs:
            # The first response with a posterior sigma stands for the point's
            # support: the factor space is shared, so distance-to-data is the same
            # for all of them, and one number is what a validity rule can bound.
            # Since ADR-0022 a model can mix methods, so it is the first *GP*
            # result — a polynomial or boosted one has no sigma to give.
            first = next((s for s in scalar_surrs.values() if s.has_variance), None)
            values = relative_sigma(first, [point]) if first is not None else None
            sigma = float(values[0]) if values else None
        support = evaluate_support(report.envelope, point, sigma=sigma, edge_threshold=threshold)
    except Exception:  # a guard must never fail the job it is annotating
        _log.warning("support could not be evaluated for this prediction", exc_info=True)
        return []

    # ``OutputValue`` permits name / label / type / unit / value / filename and
    # nothing else, so ``support.reason`` cannot ride along as its own key: an
    # extra key fails validation for the *whole* document, taking every scalar in
    # it down too. The status stays the bare token consumers compare against
    # (``inside`` / ``extrapolation`` / ``far_extrapolation`` / ``outside``); the
    # human-readable reason is rendered from the coverage report by the UI badge,
    # which is the surface that actually shows it.
    out: list[dict[str, Any]] = [
        {
            "name": "support_status",
            "label": "Prediction support",
            "type": "string",
            "value": support.status,
        }
    ]
    if support.sigma is not None:
        out.append(
            {
                "name": "support_sigma",
                "label": "Prediction uncertainty (relative)",
                "type": "number",
                "value": float(support.sigma),
            }
        )
    return out


def run_surrogate_tool(
    *,
    bundle_path: str | Path,
    expected_sha256: str,
    version_id: int,  # noqa: ARG001 - carried for diagnostics / lineage
    tool_alias: str,
    point: dict[str, float],
    out_dir: str | Path,
) -> int:
    """Run an exported surrogate tool: predict, write field files + outputs.json.

    The unified runtime for a synthesised tool of any kind (ADR-0018): a
    ``scalar`` version emits number outputs (+ ``<name>_std`` for GP
    uncertainty); a ``field``/``combined`` version additionally reconstructs
    each field and writes it (``.vtu`` on the baked reference mesh, else
    ``.npy``) as a ``file_type='field'`` output. The bundle is sha256-verified
    on load (#2); runs in the tool's run subprocess (#9). Returns a 0 exit code.
    """
    bundle_dir = Path(bundle_path)
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    kind = str(manifest.get("kind") or "scalar")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    scalar_surrs: dict[str, Any] = {}
    field_surrs: dict[str, FieldSurrogate] = {}
    if kind == "scalar":
        scalar_surrs = load_scalar_bundle(bundle_dir, expected_sha256)
    elif kind == "combined":
        scalar_surrs, field_surrs = load_combined_bundle(bundle_dir, expected_sha256)
    elif kind == "field":
        fname = next(
            (
                str(r.get("name"))
                for r in (manifest.get("responses") or [])
                if r.get("kind") == "field" and r.get("name")
            ),
            "field",
        )
        field_surrs = {fname: load_field_bundle(bundle_dir, expected_sha256)}

    outputs: list[dict[str, Any]] = []
    if scalar_surrs:
        runtime = ModelRuntime(responses=list(scalar_surrs), _surrogates=scalar_surrs)
        preds = runtime.predict(point)
        stds = runtime.predict_std(point)
        for name, value in preds.items():
            outputs.append({"name": name, "label": name, "type": "number", "value": value})
            std = stds.get(name)
            if std is not None:
                outputs.append(
                    {
                        "name": f"{name}_std",
                        "label": f"{name} (1-sigma)",
                        "type": "number",
                        "value": std,
                    }
                )

    for name, surrogate in field_surrs.items():
        prediction = surrogate.predict(point)
        field = [float(v) for v in prediction.field.tolist()]
        fmt, mesh_path = _field_output_meta(bundle_dir, manifest, kind, name)
        rel = _write_field_file(out, name, field, fmt, mesh_path)
        # Only the keys ``OutputValue`` permits (name / label / type / unit /
        # value / filename). The field metadata — ``file_type``,
        # ``field_format``, ``field_node_array_name`` — is a **descriptor**
        # concern and is already declared there by ``tool_synthesis``; writing
        # it here as well made the whole document fail ``OutputsDocument``
        # validation (``extra_forbidden``), so the runner recorded an empty
        # result and every optimisation evaluation was silently dropped.
        outputs.append(
            {
                "name": name,
                "label": name,
                "type": "file",
                "filename": rel,
            }
        )

    outputs.extend(_support_outputs(bundle_dir, scalar_surrs, point))

    (out / "outputs.json").write_text(
        json.dumps({"schema_version": 1, "tool_alias": tool_alias, "outputs": outputs}, indent=2),
        encoding="utf-8",
    )
    return 0
