"""The factor specification (``factor_spec`` JSON, design §3).

A study's ``factor_spec`` declares, per tool input, whether it is held
**constant** (every generated point carries the same value) or
**varied**, and — when varied — over what domain. The shape mirrors
designs/parametric-studies.md §3 exactly:

```jsonc
{
  "thermal_conductivity": {"role": "vary", "type": "numeric",
                           "vary": {"kind": "range", "min": 0.1, "max": 2.5}},
  "component_a_pct":      {"role": "vary", "type": "numeric",
                           "vary": {"kind": "list", "values": [10, 20, 30]}},
  "mesh_density":         {"role": "vary", "type": "select",
                           "vary": {"kind": "subset", "options": ["coarse","fine"]}},
  "use_radiation":        {"role": "vary", "type": "bool"},
  "geometry_file":        {"role": "vary", "type": "file",
                           "vary": {"kind": "file_list", "cas_hashes": ["…"]}},
  "ambient_temp":         {"role": "constant", "value": 20.0}
}
```

This module is **pure** — parsing + validation + mode-compatibility
gating only, no DB/IO. It is exhaustively unit-tested (H40).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from matofx.studies.errors import FactorSpecError, ModeNotApplicableError
from matofx.studies.orm import STUDY_MODES, StudyMode
from matofx.tools.descriptor import ToolDescriptor

# --- Mode groupings (design §1 / §3 / §4) --------------------------------

# Modes whose factors must be enumerable (no continuous span): the
# explicit-value mode. ``grid`` also accepts a numeric range (it
# discretises it), so it is handled separately.
_LIST_MODE: Literal["list"] = "list"
_GRID_MODE: Literal["grid"] = "grid"
# User-authored run matrix — exact rows, no combination (design §2/§3,
# 2026-05-19 amendment). Distinct from the cross-product ``list``.
_TABLE_MODE: Literal["table"] = "table"
# Two-level screening designs — continuous numeric only (design §1).
_SCREENING_MODES: frozenset[str] = frozenset({"frac_factorial", "plackett_burman"})
# Space-filling — numeric (range/distribution) + categorical by binning.
_SPACE_FILLING_MODES: frozenset[str] = frozenset({"lhs", "sobol", "random"})
# Response-surface — every varied factor must be a continuous numeric
# range (design §3: the UI greys these out otherwise).
_RSM_MODES: frozenset[str] = frozenset({"ccd", "box_behnken"})
# File factors are valid ONLY in the enumerable / authored modes —
# explicit list, full-factorial grid, and the user-authored run table
# (design §3, the §C agreement) — never continuous/RSM, never a
# response-plot axis.
_FILE_MODES: frozenset[str] = frozenset({"list", "grid", "table"})
# Box-Behnken needs >=3 continuous numeric factors. Local copy of the
# generator's design rule (importing generate.py here would cycle).
_BOX_BEHNKEN_MIN_NUMERIC = 3


class _Frozen(BaseModel):
    """Frozen, extra-forbidding base — mirrors the descriptor models."""

    model_config = ConfigDict(frozen=True, extra="forbid")


GridSpacing = Literal["linear", "log_min", "log_max", "edges", "center"]
"""How a numeric range becomes ``grid`` levels (design §3, 2026-05-19
amendment). All five are monotonic warps ``g(t)`` of the *normalised
position* ``t∈[0,1]`` affine-mapped into ``[min,max]`` — the log/cosine
is applied to the position, never to the data values, so there is **no
sign / ``min>0`` constraint** (works on any range, incl. negative or
spanning zero). ``linear`` even · ``log_min``/``log_max`` dense near
min/max · ``edges``/``center`` dense at the ends/middle."""


class RangeVary(_Frozen):
    """A continuous numeric span ``[min, max]`` (inclusive).

    ``levels`` / ``spacing`` are **grid-only**: how the span is
    discretised in ``grid`` mode. ``levels`` defaults to the global
    ``SamplingParams.points_per_factor`` when ``None``. Every other
    mode reads only ``min`` / ``max`` and ignores them (design §3/§4).
    """

    kind: Literal["range"] = "range"
    min: float
    max: float
    levels: int | None = Field(default=None, ge=2, le=1000)
    spacing: GridSpacing = "linear"

    @model_validator(mode="after")
    def _ordered(self) -> RangeVary:
        if self.max <= self.min:
            raise ValueError("range 'max' must be greater than 'min'")
        # Deliberately NO min>0 check for log spacing: spacing warps
        # the normalised position, not a logarithm of the values
        # (design §3 / ADR-0001 2026-05-19 amendment).
        return self


class ListVary(_Frozen):
    """An explicit set of numeric values."""

    kind: Literal["list"] = "list"
    values: list[float] = Field(min_length=1)


class DistributionVary(_Frozen):
    """A probability distribution — only for lhs/sobol/random (design §3)."""

    kind: Literal["distribution"] = "distribution"
    dist: Literal["uniform", "normal", "loguniform"] = "uniform"
    low: float | None = None
    high: float | None = None
    mean: float | None = None
    std: float | None = None

    @model_validator(mode="after")
    def _coherent(self) -> DistributionVary:
        if self.dist in ("uniform", "loguniform"):
            if self.low is None or self.high is None:
                raise ValueError(f"{self.dist} distribution needs 'low' and 'high'")
            if self.high <= self.low:
                raise ValueError("distribution 'high' must be greater than 'low'")
            if self.dist == "loguniform" and self.low <= 0:
                raise ValueError("loguniform distribution needs 'low' > 0")
        else:  # normal
            if self.mean is None or self.std is None:
                raise ValueError("normal distribution needs 'mean' and 'std'")
            if self.std <= 0:
                raise ValueError("normal distribution needs 'std' > 0")
        return self


class SubsetVary(_Frozen):
    """A chosen subset of a select input's descriptor options."""

    kind: Literal["subset"] = "subset"
    options: list[str] = Field(min_length=1)


class FileListVary(_Frozen):
    """A discrete set of uploaded-file CAS hashes (design §3)."""

    kind: Literal["file_list"] = "file_list"
    cas_hashes: list[str] = Field(min_length=1)


class ColumnVary(_Frozen):
    """A run-table column (``table`` mode only — design §3 amendment).

    ``table`` studies take their values from the explicit rows in
    ``sampling_params.runs``; an input marked Vary is just a *column*
    with no per-factor domain. A pure marker — :meth:`FactorSpec.
    validate_for_mode` enforces that ``column`` appears **only** in
    ``table`` and that **every** varied ``table`` factor is a ``column``.
    """

    kind: Literal["column"] = "column"


VarySpec = Annotated[
    RangeVary | ListVary | DistributionVary | SubsetVary | FileListVary | ColumnVary,
    Field(discriminator="kind"),
]

FactorRole = Literal["vary", "constant"]
FactorType = Literal["numeric", "select", "bool", "file"]

# Which vary ``kind`` each factor ``type`` may carry.
_TYPE_KINDS: dict[FactorType, frozenset[str]] = {
    "numeric": frozenset({"range", "list", "distribution", "column"}),
    "select": frozenset({"subset", "column"}),
    "bool": frozenset(),  # implicit {False, True}, or a run-table column
    "file": frozenset({"file_list", "column"}),
}


class Factor(_Frozen):
    """One tool input's role in the study."""

    role: FactorRole
    type: FactorType | None = None
    """The factor's type. Required when ``role == "vary"``; omitted for
    a constant (a held-constant input carries only its value — design
    §3's ``{"role": "constant", "value": ...}`` shape)."""

    value: Any = None
    """The fixed value when ``role == "constant"``."""

    vary: VarySpec | None = None
    """The variation domain when ``role == "vary"``. ``None`` is only
    valid for a varied ``bool`` (implicitly ``{False, True}``)."""

    @model_validator(mode="after")
    def _coherent(self) -> Factor:
        if self.role == "constant":
            if self.vary is not None:
                raise ValueError("a constant factor must not carry a 'vary' block")
            return self
        # role == "vary"
        ftype = self.type
        if ftype is None:
            raise ValueError("a varied factor needs a 'type'")
        if ftype == "bool":
            # A bool sweeps implicitly over {false, true} (omit 'vary'),
            # unless it is a run-table column (per-run values come from
            # the rows, not a domain).
            if self.vary is not None and self.vary.kind != "column":
                raise ValueError(
                    "a varied bool is implicit {false, true}; omit 'vary' "
                    "(only a run-table 'column' may be set)"
                )
            return self
        if self.vary is None:
            raise ValueError(f"a varied '{ftype}' factor needs a 'vary' block")
        allowed = _TYPE_KINDS[ftype]
        if self.vary.kind not in allowed:
            raise ValueError(
                f"a '{ftype}' factor cannot vary by '{self.vary.kind}' "
                f"(allowed: {', '.join(sorted(allowed)) or 'none'})"
            )
        return self

    @property
    def is_varied(self) -> bool:
        """True when this factor contributes a sweep dimension."""
        return self.role == "vary"


class FactorSpec(_Frozen):
    """The whole per-input specification, keyed by tool input name."""

    factors: dict[str, Factor]

    @classmethod
    def from_obj(cls, raw: object) -> FactorSpec:
        """Parse the ``factor_spec`` JSON shape (design §3).

        ``raw`` is the persisted ``{input_name: {...}}`` mapping.
        Raises :class:`FactorSpecError` with a plain-English message on
        any malformed entry (the message is safe to surface in the UI).
        """
        if not isinstance(raw, dict) or not raw:
            raise FactorSpecError(
                "The factor specification must be a non-empty mapping of input names to roles."
            )
        try:
            spec = cls(factors={str(k): Factor.model_validate(v) for k, v in raw.items()})
        except ValidationError as exc:
            first = exc.errors()[0]
            loc = ".".join(str(p) for p in first.get("loc", ()) if p != "factors")
            where = f" ({loc})" if loc else ""
            raise FactorSpecError(
                f"The factor specification is invalid{where}: {first.get('msg', 'bad value')}."
            ) from exc
        if not any(f.is_varied for f in spec.factors.values()):
            raise FactorSpecError(
                "A study needs at least one varied input — every factor is currently held constant."
            )
        return spec

    @property
    def varied(self) -> dict[str, Factor]:
        """The varied factors, preserving spec order (sweep dimensions)."""
        return {n: f for n, f in self.factors.items() if f.is_varied}

    @property
    def constants(self) -> dict[str, Any]:
        """``{input_name: value}`` for every held-constant factor."""
        return {n: f.value for n, f in self.factors.items() if not f.is_varied}

    def validate_for_mode(self, mode: StudyMode) -> None:
        """Gate the chosen mode against the varied factors (design §3/§9).

        Raises :class:`ModeNotApplicableError` (plain-English, UI-safe)
        when the mode cannot express these factors — the same reason
        the UI shows when it greys the mode out.
        """
        varied = self.varied
        for name, f in varied.items():
            if f.type == "file" and mode not in _FILE_MODES:
                raise ModeNotApplicableError(
                    f"'{name}' is a file input — file factors can only be "
                    f"swept by an explicit list, a full-factorial grid, or "
                    f"a run table, not by '{mode}'."
                )
            is_dist = f.vary is not None and f.vary.kind == "distribution"
            if is_dist and mode not in _SPACE_FILLING_MODES:
                raise ModeNotApplicableError(
                    f"'{name}' varies by a probability distribution, which "
                    f"only the space-filling modes (Latin-Hypercube, Sobol, "
                    f"random) can sample — not '{mode}'."
                )

        # Run-table <-> column correspondence (design §3 amendment).
        self._reject_misplaced_columns(mode, varied)

        if mode in _RSM_MODES or mode in _SCREENING_MODES:
            label = "Response-surface designs" if mode in _RSM_MODES else "Screening designs"
            for name, f in varied.items():
                if not (f.type == "numeric" and f.vary is not None and f.vary.kind == "range"):
                    raise ModeNotApplicableError(
                        f"{label} need every varied input to be a continuous "
                        f"numeric range; '{name}' is not."
                    )

        if mode == _LIST_MODE:
            for name, f in varied.items():
                kind = f.vary.kind if f.vary is not None else "bool"
                if kind in ("range", "distribution"):
                    raise ModeNotApplicableError(
                        f"The explicit-list mode needs each varied input to "
                        f"be an explicit set of values; '{name}' is a "
                        f"continuous {kind}. Use a grid or a space-filling "
                        f"mode instead."
                    )

    def _reject_misplaced_columns(self, mode: StudyMode, varied: dict[str, Factor]) -> None:
        """Enforce the run-table <-> column rule (design §3 amendment).

        A ``column`` domain is valid only in ``table``; and every
        varied ``table`` factor must be a ``column`` (its per-run
        values come from ``sampling_params.runs``, not a domain).

        Args:
            mode: the chosen study mode.
            varied: the varied factors (``FactorSpec.varied``).

        Raises:
            ModeNotApplicableError: on any misplaced ``column``.
        """
        if mode == _TABLE_MODE:
            for name, f in varied.items():
                if f.vary is None or f.vary.kind != "column":
                    raise ModeNotApplicableError(
                        f"Run-table studies take each run's values from the "
                        f"rows you enter; mark '{name}' as a run-table column "
                        f"or choose another mode."
                    )
            return
        for name, f in varied.items():
            if f.vary is not None and f.vary.kind == "column":
                raise ModeNotApplicableError(
                    f"'{name}' is a run-table column - only valid in the "
                    f"run-table mode, not '{mode}'."
                )


def available_modes(descriptor: ToolDescriptor) -> dict[StudyMode, str | None]:
    """Which sampling modes the tool's *input types* can support (design §9.3).

    Pure pre-filter for the create-form mode selector: maps every
    :data:`~matofx.studies.orm.StudyMode` to ``None`` (offerable) or a
    plain-English reason it is greyed out (F30). It inspects input
    *types* only and is **not** the final gate —
    :meth:`FactorSpec.validate_for_mode` re-checks the user's actual
    Constant/Vary selection at preview and submit (the two-layer rule).

    Args:
        descriptor: the tool's resolved descriptor.

    Returns:
        ``{mode: None | reason}`` for every :data:`STUDY_MODES` entry.
    """
    n_numeric = sum(1 for i in descriptor.inputs if i.type in ("integer", "float"))
    has_numeric = n_numeric > 0
    has_categorical = any(i.type in ("boolean", "select", "multiselect") for i in descriptor.inputs)
    reasons: dict[StudyMode, str | None] = {}
    for mode in STUDY_MODES:
        if mode in (_TABLE_MODE, _LIST_MODE, _GRID_MODE):
            reasons[mode] = None
        elif mode == "box_behnken":
            reasons[mode] = (
                None
                if n_numeric >= _BOX_BEHNKEN_MIN_NUMERIC
                else (
                    f"Box-Behnken needs at least {_BOX_BEHNKEN_MIN_NUMERIC} "
                    f"continuous numeric inputs; this tool has {n_numeric}."
                )
            )
        elif mode in _SCREENING_MODES or mode in _RSM_MODES:
            reasons[mode] = (
                None
                if has_numeric
                else (
                    "Screening and response-surface designs need at least "
                    "one continuous numeric input; this tool has none — use "
                    "a run table, value list, or grid."
                )
            )
        elif mode in _SPACE_FILLING_MODES:
            reasons[mode] = (
                None
                if (has_numeric or has_categorical)
                else (
                    "Space-filling sampling needs at least one numeric or "
                    "categorical input; this tool has none it can sample."
                )
            )
        else:  # pragma: no cover - exhaustive over STUDY_MODES
            reasons[mode] = None
    return reasons
