"""Shared builders for the design-space-coverage tests (ADR-0020, slice 1).

Pure in-memory factories — no DB, no fitted model. Slice 1 is the
response-agnostic path (`designs/design-space-coverage.md` §13), so every
point set here is built by hand as the :class:`TrainingData` that
``studies/surrogate/ingest.py`` would have produced, per §1.1's rule that
coverage consumes the *training set*, never raw cohort membership.
"""

from __future__ import annotations

from typing import Any

from matofx.studies.factors import FactorSpec
from matofx.studies.surrogate.ingest import FieldTrainingData, TrainingData
from matofx.tools.descriptor import InputField, OutputField, ToolDescriptor


def descriptor(*inputs: InputField) -> ToolDescriptor:
    """A minimal tool descriptor carrying ``inputs`` (integer-ness lives here).

    ``FactorSpec`` only knows ``numeric | select | bool | file`` — whether a
    numeric input is an *integer* is a descriptor fact (``InputFieldType``),
    so the envelope needs the descriptor to resolve the integer lattice
    (§3.1 / §3.3).
    """
    return ToolDescriptor(
        alias="coverage_probe",
        name="Coverage Probe",
        version="1.0.0",
        description="a tool used to exercise the coverage engine",
        main_script="static/main.py",
        inputs=list(inputs),
        outputs=[OutputField(name="peak_temperature", label="Peak temperature", type="number")],
    )


def num(name: str, lo: float, hi: float) -> InputField:
    """A continuous float input."""
    return InputField(
        name=name, label=name.replace("_", " ").title(), type="float", minimum=lo, maximum=hi
    )


def integer(name: str, lo: int, hi: int) -> InputField:
    """An integer input — the ordered-discrete third treatment (§3.3)."""
    return InputField(
        name=name,
        label=name.replace("_", " ").title(),
        type="integer",
        minimum=float(lo),
        maximum=float(hi),
        precision=1.0,
    )


def select(name: str, *options: str) -> InputField:
    """A select input — categorical levels."""
    return InputField(
        name=name, label=name.replace("_", " ").title(), type="select", options=list(options)
    )


def boolean(name: str) -> InputField:
    """A boolean input — two categorical levels."""
    return InputField(name=name, label=name.replace("_", " ").title(), type="boolean")


def training(
    rows: list[dict[str, Any]],
    *,
    response: list[float | None] | None = None,
    constants: dict[str, Any] | None = None,
    n_excluded_invalid: int = 0,
    n_excluded_filtered: int = 0,
    excluded_reason_counts: dict[str, int] | None = None,
    source_kind: str = "cohort",
) -> TrainingData:
    """Build the :class:`TrainingData` ``ingest.collect_training_data`` would emit.

    ``rows`` are whole input dicts; numeric values land in
    ``factor_columns`` and everything else in ``categorical_columns``,
    mirroring ingest's own numeric/non-numeric split. Inputs shared with one
    value across every row are lifted into ``input_constants`` (ingest's
    ``_constant_inputs``) unless ``constants`` is given explicitly.
    """
    from matofx.studies.surrogate import SourceRef  # noqa: PLC0415

    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)

    def _numeric(value: Any) -> bool:
        return not isinstance(value, bool) and isinstance(value, int | float)

    numeric_keys = [k for k in keys if all(_numeric(r[k]) for r in rows if k in r)]
    other_keys = [k for k in keys if k not in numeric_keys]

    if constants is None:
        constants = {}
        for key in keys:
            values = [r.get(key) for r in rows]
            if (
                len(rows) > 1
                and all(k in r for r in rows for k in (key,))
                and all(v == values[0] for v in values)
            ):
                constants[key] = values[0]

    varied_numeric = [k for k in numeric_keys if k not in constants]
    varied_other = [k for k in other_keys if k not in constants]

    n = len(rows)
    return TrainingData(
        factor_columns={k: [float(r[k]) for r in rows] for k in varied_numeric},
        categorical_columns={k: [r[k] for r in rows] for k in varied_other},
        response=list(response) if response is not None else [1.0] * n,
        n_rows=n,
        n_complete=n,
        source_ref=SourceRef(source_kind=source_kind, source_id=1, n_points=n),
        input_constants=dict(constants),
        n_excluded_invalid=n_excluded_invalid,
        n_excluded_filtered=n_excluded_filtered,
        excluded_reason_counts=dict(excluded_reason_counts or {}),
    )


def field_training(
    rows: list[dict[str, Any]],
    *,
    time_resolved: bool = True,
    source_kind: str = "cohort",
) -> FieldTrainingData:
    """A time-resolved :class:`FieldTrainingData` — the demo's transient shape.

    ``rows`` already carry the reserved ``time`` column (the 0-based snapshot
    index ``ingest.TIME_FACTOR`` documents), one row per ``(run, timestep)``.
    """
    from matofx.studies.surrogate import SourceRef  # noqa: PLC0415

    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    n = len(rows)
    return FieldTrainingData(
        factor_columns={k: [float(r[k]) for r in rows] for k in keys},
        snapshots=[],
        n_rows=n,
        n_complete=n,
        source_ref=SourceRef(source_kind=source_kind, source_id=1, n_points=n),
        time_resolved=time_resolved,
        n_nodes=4,
    )


def spec(raw: dict[str, Any]) -> FactorSpec:
    """Parse a ``factor_spec`` mapping (study-sourced factor domains)."""
    return FactorSpec.from_obj(raw)


def grid(
    values_a: list[float], values_b: list[float], *, a: str = "thickness", b: str = "heat_load"
) -> list[dict[str, Any]]:
    """A full-factorial point list over two continuous factors."""
    return [{a: va, b: vb} for va in values_a for vb in values_b]


# --- slice 2: fitted models -----------------------------------------------


def fit(
    rows: list[dict[str, Any]],
    response: list[float],
    *,
    kind: str = "gp",
) -> Any:
    """Fit a scalar :class:`Surrogate` over ``rows``' numeric inputs."""
    from matofx.studies.surrogate import fit_surrogate  # noqa: PLC0415

    keys = [k for k in rows[0] if isinstance(rows[0][k], int | float)]
    cols = {k: [float(r[k]) for r in rows] for k in keys}
    return fit_surrogate(cols, list(response), kind=kind)  # type: ignore[arg-type]


def fit_field(
    rows: list[dict[str, Any]],
    snapshots: list[Any],
    *,
    kind: str = "gp",
) -> Any:
    """Fit a POD :class:`FieldSurrogate` over ``rows`` + ``snapshots``."""
    from matofx.studies.surrogate.field import fit_field_surrogate  # noqa: PLC0415

    keys = [k for k in rows[0] if isinstance(rows[0][k], int | float)]
    cols = {k: [float(r[k]) for r in rows] for k in keys}
    return fit_field_surrogate(snapshots, cols, surrogate_kind=kind)


def two_factor_rows(n_a: int = 5, n_b: int = 5) -> list[dict[str, Any]]:
    """A clean full-factorial grid over ``thickness`` x ``heat_load``."""
    import numpy as np  # noqa: PLC0415

    return [
        {"thickness": float(a), "heat_load": float(b)}
        for a in np.linspace(1.0, 9.0, n_a)
        for b in np.linspace(1.0, 9.0, n_b)
    ]


def response_over(rows: list[dict[str, Any]], fn: Any) -> list[float]:
    """``[fn(row) for row in rows]`` as plain floats."""
    return [float(fn(r)) for r in rows]
