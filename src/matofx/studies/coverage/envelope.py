"""The declared design envelope (ADR-0020 §3).

The region a model claims to be trustworthy over. It is **declared**, never
inferred from the training set's bounding box: ad-hoc jobs do not fill a
hyperrectangle, and hunting for holes inside their bounding box would
confidently flag the corner where thickness = 8 mm meets heat load = 12 W —
a region empty because nobody would ever build it.

Bounds start at the observed spread and are then user-editable (§11); the
result doubles as the model's applicable range (§10) and is the object the
predict-time guard enforces (§8).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

import numpy as np

from matofx.studies._intsnap import snap_int
from matofx.studies.coverage.errors import CoverageError
from matofx.studies.factors import DistributionVary, FileListVary, RangeVary, SubsetVary
from matofx.studies.surrogate.ingest import TIME_FACTOR

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from matofx.studies.coverage.points import PointSet
    from matofx.studies.factors import Factor, FactorSpec
    from matofx.tools.descriptor import InputField, ToolDescriptor

FactorKind = Literal["continuous", "integer", "categorical", "pinned", "derived"]
Scale = Literal["linear", "log"]

_DESCRIPTOR_CATEGORICAL_TYPES = frozenset({"select", "multiselect", "boolean", "file", "material"})
_METRIC_KINDS = frozenset({"continuous", "integer"})


@dataclass(frozen=True, slots=True)
class FactorOverride:
    """One row of the envelope form (§11) — each field ``None`` means "keep".

    Narrowing is the *expected* action ("we ran 1-8 mm but only trust 2-6"),
    which is why every bound is editable rather than merely displayed.
    """

    lo: float | None = None
    hi: float | None = None
    scale: Scale | None = None
    levels: tuple[str, ...] | None = None
    hold: Any = None
    """Pin a *varying* axis at this value — the inverse of promotion (§11.5).

    A user filling gaps may care about two inputs out of eight, and a
    space-filling walk needs runs growing with dimension, so being able to
    take an axis out of the batch is what keeps the count affordable.

    Demoting rather than merely freezing the walk is deliberate: a ``pinned``
    axis is already excluded from the joint radius, the pairwise sweep and the
    suggestions, so the whole engine treats it correctly with no new special
    case. That also makes the coverage figures describe the **narrowed** space
    — which is the honest reading of "I only care about these two", and why
    the UI offers it as an explicit scope rather than doing it silently."""


@dataclass(frozen=True, slots=True)
class FactorEnvelope:
    """One axis of the declared region."""

    name: str
    kind: FactorKind
    scale: Scale = "linear"
    lo: float | None = None
    hi: float | None = None
    levels: tuple[str, ...] | None = None
    pinned_value: Any = None
    varying_kind: FactorKind | None = None
    """What a ``pinned`` axis would become if it were varied (§11.5).

    An input held at one value throughout has no range of its own, so the
    engine records it as ``pinned`` and no suggestion moves it. That is right
    about the archive and wrong about the *future*: "this was always 60" is a
    fact about what has been run, not a decision that it must stay 60, and a
    user filling gaps may well want the batch to open it up.

    Recorded when the envelope is built, because the answer needs the
    descriptor -- whether the input is a whole number is a descriptor fact
    (§3.1), and by the time an override arrives the descriptor is long gone.
    ``None`` for an axis with no numeric type to return to (a pinned material
    name), which therefore cannot be opened up at all."""

    precision: float | None = None
    """The input's own increment, from the descriptor (e.g. ``0.01``).

    A suggested run has to be a value somebody could actually submit.
    Proposing an air velocity of ``4.360013149188567`` for an input that
    steps in hundredths is not a rounding blemish on the display -- it is a
    design point the form would reject, quoted to fifteen digits of precision
    the tool does not have. ``None`` where the descriptor names no step, in
    which case the raw value stands."""

    declared_lo: float | None = None
    declared_hi: float | None = None
    """The bounds the **tool** allows, from its descriptor (§4.5).

    Distinct from :attr:`lo` / :attr:`hi`, which default to the range the runs
    happened to cover. That default makes every coverage figure self-
    referential: "98% covered" has always meant *98% of the range you chose to
    run*, and can never say that you explored a tenth of what the tool
    accepts. On the demo heat-sink archive ``base_temperature_rise`` is pinned
    at 60 out of a declared 10-120 -- invisible in every reading the panel had.

    ``None`` where the descriptor sets no bound, which is a real answer and
    not zero: an unbounded input has no denominator, so no percentage of it
    can be honestly quoted."""

    label: str = ""
    """The descriptor's human-readable label for this input.

    **The single place coverage learns what to call an axis**
    ([[feedback_use_labels_not_names]]). Every surface — the per-factor
    strip, the headline, the warnings, the scatter axes, the suggested-runs
    table — reads :attr:`display_name` rather than resolving a label of its
    own, because a second lookup is a second thing to drift.

    Empty where no descriptor was available (an ad-hoc cohort whose tool has
    been removed); :attr:`display_name` then falls back to the raw name,
    which is worse than a label but better than a blank.
    """

    declared_levels: tuple[str, ...] = ()
    """Every option the descriptor offers for a ``select`` input.

    The categorical counterpart of :attr:`declared_lo` / :attr:`declared_hi`,
    and needed for the same reason: without it a categorical row can only
    describe the levels that happen to have been run, which is the
    self-referential reading §4.5 exists to undo. "No stated range" was
    literally wrong for an input whose options are enumerated in the
    descriptor — the options *are* the range.

    Empty for a numeric input, and for a categorical whose descriptor could
    not be resolved."""

    @property
    def display_name(self) -> str:
        """What a user should see for this axis — never the internal name."""
        return self.label or self.name

    @property
    def observed_levels(self) -> tuple[str, ...]:
        """The levels these runs actually used, pinned or varying."""
        if self.levels:
            return tuple(str(level) for level in self.levels)
        if self.pinned_value is not None:
            return (str(self.pinned_value),)
        return ()

    @property
    def actionable(self) -> bool:
        """Whether a suggested design point could move this axis (§4.1).

        ``False`` for ``derived`` (a snapshot index arrives as solver output)
        and ``pinned`` (it never varied, so the model cannot move along it at
        all). The UI offers the right remedy instead of a suggestion button.
        """
        return self.kind not in ("derived", "pinned")

    def lattice(self) -> tuple[int, ...] | None:
        """Every attainable integer value inside the bounds, or ``None``."""
        if self.kind != "integer" or self.lo is None or self.hi is None:
            return None
        return tuple(range(math.ceil(self.lo), math.floor(self.hi) + 1))

    def to_json_obj(self) -> dict[str, Any]:
        """A JSON-safe view of this axis (ADR-0020 §9)."""
        return {
            "name": self.name,
            "kind": self.kind,
            "scale": self.scale,
            "lo": self.lo,
            "hi": self.hi,
            "levels": list(self.levels) if self.levels is not None else None,
            "pinned_value": self.pinned_value,
            "label": self.label,
            "declared_levels": list(self.declared_levels),
            "declared_lo": self.declared_lo,
            "declared_hi": self.declared_hi,
            "precision": self.precision,
            "varying_kind": self.varying_kind,
        }

    @classmethod
    def from_json_obj(cls, raw: Mapping[str, Any]) -> FactorEnvelope:
        """Rebuild an axis from its stored form, tolerating unknown keys."""
        levels = raw.get("levels")
        return cls(
            name=str(raw.get("name") or ""),
            kind=raw.get("kind", "continuous"),
            scale=raw.get("scale", "linear"),
            lo=_as_float(raw.get("lo")),
            hi=_as_float(raw.get("hi")),
            levels=tuple(str(v) for v in levels) if levels is not None else None,
            pinned_value=raw.get("pinned_value"),
            # Absent in reports written before labels were carried (§9's
            # forward/backward tolerance): those fall back to the raw name.
            label=str(raw.get("label") or ""),
            declared_levels=tuple(str(v) for v in (raw.get("declared_levels") or ())),
            declared_lo=_as_float(raw.get("declared_lo")),
            declared_hi=_as_float(raw.get("declared_hi")),
            precision=_as_float(raw.get("precision")),
            varying_kind=raw.get("varying_kind"),
        )


@dataclass(frozen=True, slots=True)
class Envelope:
    """The ordered axes of the declared region, plus the geometry helpers.

    ``normalise`` maps continuous and integer axes to ``[0,1]`` — essential,
    because raw units make distance meaningless when thickness is in mm and
    load in N — honouring each axis's ``scale`` so a log-sampled factor is
    measured in log space (§3.2).
    """

    factors: tuple[FactorEnvelope, ...] = ()

    # --- lookup ----------------------------------------------------------

    def by_name(self, name: str) -> FactorEnvelope:
        """The axis called ``name``, or raise."""
        for factor in self.factors:
            if factor.name == name:
                return factor
        known = ", ".join(f.name for f in self.factors) or "none"
        raise CoverageError(f"There's no input called '{name}' in this design (known: {known}).")

    @property
    def metric_axes(self) -> tuple[str, ...]:
        """The axes the joint distance metrics run over (§4.1).

        Continuous and integer only: ``derived`` and ``pinned`` axes sit in
        the model's input space but not the *design* space, and folding them
        into the joint radius would report a hole no design point can fill.
        """
        return tuple(f.name for f in self.factors if f.kind in _METRIC_KINDS)

    @property
    def categorical_axes(self) -> tuple[str, ...]:
        """The categorical axes, in envelope order."""
        return tuple(f.name for f in self.factors if f.kind == "categorical")

    def column_index(self, name: str) -> int:
        """Column of ``name`` in :meth:`normalise`'s matrix."""
        axes = self.metric_axes
        if name not in axes:
            raise CoverageError(f"'{name}' isn't one of the measured axes ({', '.join(axes)}).")
        return axes.index(name)

    def lattice(self, name: str) -> tuple[int, ...] | None:
        """Attainable integer values on ``name``, or ``None`` if not integer."""
        return self.by_name(name).lattice()

    def to_json_obj(self) -> list[dict[str, Any]]:
        """The declared region, JSON-safe (ADR-0020 §9).

        Stored **inside** the report and nowhere else: it is the single source
        for the predict-time guard (§8) and the V&V applicable range (§10), and
        a second copy is a second thing to fall out of step.
        """
        return [f.to_json_obj() for f in self.factors]

    @classmethod
    def from_json_obj(cls, raw: Sequence[Mapping[str, Any]]) -> Envelope:
        """Rebuild a declared region from its stored form.

        Tolerant by necessity: a stored report is a database row that outlives
        the release that wrote it, so anything malformed yields an *empty*
        envelope — which vouches for nothing — rather than an exception on a
        read path.
        """
        if not isinstance(raw, list | tuple):
            return cls()
        return cls(
            factors=tuple(FactorEnvelope.from_json_obj(f) for f in raw if isinstance(f, dict))
        )

    # --- geometry --------------------------------------------------------

    def normalise(self, points: PointSet) -> np.ndarray:
        """Training points as an ``(n, len(metric_axes))`` matrix in ``[0,1]``.

        Out-of-envelope coordinates fall outside ``[0,1]`` and are **never
        clamped** — clamping would pile them onto the boundary and manufacture
        false duplicates (§1.3). Callers drop them; they do not squash them.
        """
        axes = self.metric_axes
        if not axes or not points.rows:
            return np.zeros((len(points.rows), len(axes)), dtype=float)
        # Read through `points.values`, which falls back to `constants` for an
        # input that never varied. `rows` carries only the *varied* factors, so
        # `row.get` returns None for a constant — harmless while such an axis
        # was excluded from every measurement, and wrong the moment one can be
        # opened up and swept (§11.5): its column normalised to all-NaN, the
        # axis dropped out of the joint radius, and every pair containing it
        # went silently unjudged.
        columns = [[self._unit(self.by_name(a), v) for v in points.values(a)] for a in axes]
        return np.array(columns, dtype=float).T

    def denormalise(self, matrix: np.ndarray) -> list[dict[str, Any]]:
        """Normalised rows back to whole, submittable input points.

        The inverse of :meth:`normalise` on the metric axes, plus every
        ``pinned`` factor at its pinned value — a suggested run has to be a
        complete input set, not just the axes the metric happened to use.
        ``derived`` axes are **omitted entirely** (§1.2.1): nobody can run a
        job at a snapshot index, so a design point must not carry one.
        Integer axes come back whole, via ``snap_int``.
        """
        axes = self.metric_axes
        pinned = {f.name: f.pinned_value for f in self.factors if f.kind == "pinned"}
        categorical = [f for f in self.factors if f.kind == "categorical"]
        out: list[dict[str, Any]] = []
        for i, row in enumerate(np.atleast_2d(matrix)):
            point: dict[str, Any] = dict(pinned)
            for j, name in enumerate(axes):
                factor = self.by_name(name)
                raw = self._raw(factor, float(row[j]))
                point[name] = (
                    snap_int(raw, factor.lo, factor.hi)
                    if factor.kind == "integer"
                    else _snap_precision(raw, factor)
                )
            # Every categorical axis gets a level, cycled across the batch.
            # Omitting them produced points that failed this envelope's own
            # `contains` check and were missing an input the tool requires —
            # a suggested run nobody could actually submit. Cycling rather
            # than fixing one level also spreads a batch over the options,
            # which is what an imbalanced categorical axis needs.
            for factor in categorical:
                levels = factor.levels or ()
                if levels:
                    point[factor.name] = levels[i % len(levels)]
            out.append(point)
        return out

    def contains(self, point: Mapping[str, Any]) -> bool:
        """Whether ``point`` sits inside the declared region (§8's cheap half).

        Deterministic and model-free. A missing factor is **not** guessed —
        an incomplete point cannot be claimed as inside. A ``pinned`` factor
        supplied at a different value is outside: predicting along a
        never-varied input is pure extrapolation, and silence there would be
        the dangerous answer.
        """
        return all(
            factor.name in point and _axis_contains(factor, point[factor.name])
            for factor in self.factors
        )

    def sample(self, n: int, *, seed: int) -> np.ndarray:
        """``n`` candidate points over the metric axes, normalised to ``[0,1]``.

        A Latin-hypercube-style stratified sample rather than a grid — grid
        cost explodes past ~4 factors.

        **Integer axes are snapped to their lattice**, from slice 1 (§3.3). If
        candidates were continuous, the joint covering radius would measure
        distance to points like ply count 2.7 and report holes at 1.5 / 2.5 on
        a lattice the per-factor row has just called complete — the two halves
        of the report contradicting each other before any suggestion exists.
        """
        axes = self.metric_axes
        if n <= 0 or not axes:
            return np.zeros((max(n, 0), len(axes)), dtype=float)

        rng = np.random.default_rng(seed)
        out = np.empty((n, len(axes)), dtype=float)
        for j, name in enumerate(axes):
            strata = (rng.permutation(n) + rng.random(n)) / n
            factor = self.by_name(name)
            out[:, j] = strata if factor.kind != "integer" else self._snap_column(factor, strata)
        return out

    # --- internals -------------------------------------------------------

    def _snap_column(self, factor: FactorEnvelope, unit: np.ndarray) -> np.ndarray:
        """Round a normalised column onto the axis's integer lattice."""
        lattice = factor.lattice()
        if not lattice:
            return unit
        raw = [snap_int(self._raw(factor, float(u)), factor.lo, factor.hi) for u in unit]
        return np.array([self._unit(factor, v) for v in raw], dtype=float)

    def _raw(self, factor: FactorEnvelope, unit: float) -> float:
        """Inverse of :meth:`_unit` — normalised position back to real units."""
        lo, hi = factor.lo, factor.hi
        if lo is None or hi is None or hi == lo:
            return lo if lo is not None else 0.0
        if factor.scale == "log":
            return float(math.exp(math.log(lo) + unit * (math.log(hi) - math.log(lo))))
        return float(lo + unit * (hi - lo))

    def unit_value(self, factor: FactorEnvelope, value: Any) -> float:
        """One coordinate on one axis, in ``[0,1]`` — the public :meth:`_unit`.

        For a measure that works a single axis at a time rather than over the
        whole matrix — §4.6's within-option coverage, which places a run on
        one numeric axis inside one level of a choice. It must place that run
        exactly where every other reading places it, so it calls the same
        normalisation rather than repeating the arithmetic.
        """
        return self._unit(factor, value)

    def _unit(self, factor: FactorEnvelope, value: Any) -> float:
        """One coordinate mapped into ``[0,1]`` (outside allowed, never clamped)."""
        number = _as_float(value)
        lo, hi = factor.lo, factor.hi
        if number is None:
            # A missing / non-finite coordinate is **not a point**. Returning
            # 0.0 here placed the row at the bottom of the axis — a run that
            # does not exist, sitting in exactly the region the analysis is
            # meant to report as empty, and flattering the score for it. NaN
            # propagates instead, and every metric drops the row.
            return float("nan")
        if lo is None or hi is None or hi == lo:
            # A zero-width axis genuinely contributes nothing to distance, and
            # every row sits at the same place on it.
            return 0.0
        if factor.scale == "log":
            if number <= 0.0:
                return 0.0
            return (math.log(number) - math.log(lo)) / (math.log(hi) - math.log(lo))
        return (number - lo) / (hi - lo)


# --- construction ---------------------------------------------------------


def build_envelope(
    points: PointSet,
    *,
    factor_spec: FactorSpec | None = None,
    descriptor: ToolDescriptor | None = None,
    overrides: Mapping[str, FactorOverride] | None = None,
) -> Envelope:
    """Resolve every axis's kind, domain and scale, then apply user overrides.

    ``descriptor`` is not a convenience: ``FactorType`` is
    ``numeric | select | bool | file``, so whether a numeric input is an
    **integer** is a descriptor fact (``InputFieldType``) and §3.1's table
    cannot be resolved without it. Absent a descriptor an integer axis
    degrades to ``continuous`` — a real loss (§3.3) but never a wrong answer.
    Integer-ness is **never guessed** from whole-valued observations: a
    thickness that happens to have been run at 1, 2, 3 mm is not a lattice.
    """
    descriptor = descriptor if descriptor is not None else points.descriptor
    fields = _descriptor_fields(descriptor)
    resolved: list[FactorEnvelope] = [
        _resolve_factor(name, points, factor_spec, fields) for name in points.factor_names
    ]
    resolved += [
        FactorEnvelope(
            name=name,
            kind="pinned",
            pinned_value=value,
            # What it would be if somebody chose to vary it (§11.5). Decided
            # here because it takes the descriptor, which an override never
            # sees.
            varying_kind=_varying_kind(name, fields),
        )
        for name, value in sorted(points.constants.items())
        if name not in points.factor_names
    ]
    # **The one place a label is attached** ([[feedback_use_labels_not_names]]).
    # Every downstream surface reads ``FactorEnvelope.display_name``; resolving
    # the descriptor again anywhere else is the second copy that drifts.
    resolved = [
        replace(
            f,
            label=_label_for(f.name, fields),
            declared_levels=_declared_levels(f.name, fields),
            declared_lo=_declared_bound(f.name, fields, "minimum"),
            declared_hi=_declared_bound(f.name, fields, "maximum"),
            precision=_as_float(getattr(fields.get(f.name), "precision", None)),
            varying_kind=f.varying_kind or _varying_kind(f.name, fields),
        )
        for f in resolved
    ]
    return Envelope(
        factors=tuple(_apply_override(f, (overrides or {}).get(f.name)) for f in resolved)
    )


def _varying_kind(name: str, fields: Mapping[str, InputField]) -> FactorKind | None:
    """The kind a held-constant input would take if it were varied.

    **The test is whether the descriptor declares a domain the form would
    accept**, not whether the input is a number. That correction matters: the
    rule was numeric-only, so a material or a solver choice held constant
    could never be opened up, and — less obviously — a *discrete numeric*
    input authored as a ``select`` of ``6 / 8 / 10`` silently lost the ability
    too, despite its content being perfectly numeric.

    Promotable, because the descriptor bounds or enumerates the domain:

    * ``integer`` / ``float`` — a number line, from the declared bounds;
    * ``select`` / ``boolean`` — a set of levels, from the declared options.
      A boolean enumerates itself.

    Not promotable, and each for its own reason. ``material`` may not
    enumerate options at all (the frozen-options rule), so there is genuinely
    nothing to sweep. ``multiselect``'s domain is the power set of its
    options, which is not a thing to space-fill. ``string``, ``file`` and
    ``table`` are open domains; inventing a range would propose runs the form
    rejects.
    """
    field = fields.get(name)
    if field is None:
        return None
    kinds: dict[str, FactorKind] = {
        "integer": "integer",
        "float": "continuous",
        "select": "categorical",
        "boolean": "categorical",
    }
    return kinds.get(str(getattr(field, "type", "")))


def _declared_bound(name: str, fields: Mapping[str, InputField], which: str) -> float | None:
    """The tool's own ``minimum`` / ``maximum`` for ``name`` (§4.5).

    Only a **literal** bound is taken. ``minimum_expr`` / ``maximum_expr``
    are formulas over sibling inputs, so the allowed range is a different
    interval on every row -- there is no single denominator to quote, and
    inventing one would misreport how much of the input's range was explored.
    """
    field = fields.get(name)
    if field is None:
        return None
    if getattr(field, "minimum_expr", None) or getattr(field, "maximum_expr", None):
        return None
    return _as_float(getattr(field, which, None))


def _label_for(name: str, fields: Mapping[str, InputField]) -> str:
    """The descriptor's label for ``name``, or ``""`` when there is no field.

    ``""`` rather than the name itself so :attr:`FactorEnvelope.display_name`
    owns the fallback — one rule, in one place.
    """
    field = fields.get(name)
    return str(field.label) if field is not None and field.label else ""


def _declared_levels(name: str, fields: Mapping[str, Any]) -> tuple[str, ...]:
    """The descriptor's enumerated options for ``name``, or ``()``.

    A ``select`` input has them from its descriptor, and a ``boolean`` from
    its own type. A free-text or numeric input genuinely has no option list,
    and inventing one would be the overclaim this feature exists to prevent.
    """
    field = fields.get(name)
    # A boolean enumerates itself. The strings are the ones ``str()`` produces
    # for the stored values, because that is what every level comparison
    # downstream matches against.
    if str(getattr(field, "type", "")) == "boolean":
        return ("False", "True")
    options = getattr(field, "options", None)
    if not options:
        return ()
    return tuple(str(option) for option in options)


def _resolve_factor(
    name: str,
    points: PointSet,
    factor_spec: FactorSpec | None,
    fields: Mapping[str, InputField],
) -> FactorEnvelope:
    """One varied factor's kind, domain and scale (§3.1 / §3.4)."""
    observed = [v for v in points.values(name) if v is not None]
    declared = _declared(factor_spec, name)

    if name in points.derived:
        lo, hi = _observed_span(observed)
        return FactorEnvelope(name=name, kind="derived", lo=lo, hi=hi)

    if _is_categorical(name, points, declared, fields):
        levels = _levels(name, observed, declared, fields)
        return FactorEnvelope(name=name, kind="categorical", levels=levels)

    kind: FactorKind = "integer" if _is_integer(name, fields) else "continuous"
    lo, hi = _numeric_domain(observed, declared)
    return FactorEnvelope(name=name, kind=kind, scale=_scale(declared), lo=lo, hi=hi)


def _declared(factor_spec: FactorSpec | None, name: str) -> Factor | None:
    """The ``Factor`` a study declared for ``name``, or ``None`` (ad-hoc)."""
    if factor_spec is None:
        return None
    factor = factor_spec.factors.get(name)
    return factor if factor is not None and factor.is_varied else None


def _is_categorical(
    name: str, points: PointSet, declared: Factor | None, fields: Mapping[str, InputField]
) -> bool:
    """Whether ``name`` is a levels axis rather than a numeric one (§3.4).

    ``ColumnVary`` is a pure marker with no declared domain, so it resolves by
    the factor's *underlying type* — never by bucketing observed values.
    Bucketing a continuous numeric would be actively harmful: a run table with
    thickness 1.0 / 2.0 / 3.5 / 4.0 is a sparse continuous sample, and calling
    it four balanced levels would report perfect coverage on an axis that is
    barely sampled.
    """
    if declared is not None and declared.type is not None:
        return declared.type in ("select", "bool", "file")
    descriptor_field = fields.get(name)
    if descriptor_field is not None:
        return descriptor_field.type in _DESCRIPTOR_CATEGORICAL_TYPES
    return name not in points.numeric_names


def _levels(
    name: str, observed: Sequence[Any], declared: Factor | None, fields: Mapping[str, InputField]
) -> tuple[str, ...]:
    """The categorical domain — declared where one exists, else observed.

    The descriptor's full option list wins over the observed values: a level
    nobody ran at all is the 40-CFRP/2-GFRP failure one step further on, and
    it must show as a zero count rather than vanish.
    """
    vary = declared.vary if declared is not None else None
    if isinstance(vary, SubsetVary):
        return tuple(str(o) for o in vary.options)
    if isinstance(vary, FileListVary):
        return tuple(str(h) for h in vary.cas_hashes)
    if declared is not None and declared.type == "bool":
        return ("False", "True")

    descriptor_field = fields.get(name)
    if descriptor_field is not None:
        if descriptor_field.type == "boolean":
            return ("False", "True")
        if descriptor_field.options:
            return tuple(str(o) for o in descriptor_field.options)

    seen: list[str] = []
    for value in observed:
        level = level_label(value)
        if level not in seen:
            seen.append(level)
    return tuple(sorted(seen))


def _is_integer(name: str, fields: Mapping[str, InputField]) -> bool:
    """Whether the descriptor declares ``name`` an integer input (§3.3)."""
    descriptor_field = fields.get(name)
    return descriptor_field is not None and descriptor_field.type == "integer"


def _numeric_domain(
    observed: Sequence[Any], declared: Factor | None
) -> tuple[float | None, float | None]:
    """Declared bounds where a study set them, else the observed spread."""
    vary = declared.vary if declared is not None else None
    if isinstance(vary, RangeVary):
        return float(vary.min), float(vary.max)
    if isinstance(vary, DistributionVary) and vary.low is not None and vary.high is not None:
        return float(vary.low), float(vary.high)
    return _observed_span(observed)


def _observed_span(observed: Sequence[Any]) -> tuple[float | None, float | None]:
    """``(min, max)`` over the usable numeric values, or ``(None, None)``."""
    numbers = [n for n in (_as_float(v) for v in observed) if n is not None]
    return (min(numbers), max(numbers)) if numbers else (None, None)


def _scale(declared: Factor | None) -> Scale:
    """``log`` for a ``loguniform`` factor, else ``linear`` (§3.2).

    Inferred only where a study recorded the distribution. An ad-hoc archive
    carries no record of how someone chose their values, so it defaults to
    ``linear`` and the user overrides on the envelope form — a factor spanning
    several orders of magnitude is the visible clue. ``normal`` stays linear:
    unbounded in principle, but the envelope supplies real bounds and a linear
    measure over those is correct.
    """
    vary = declared.vary if declared is not None else None
    if isinstance(vary, DistributionVary) and vary.dist == "loguniform":
        return "log"
    return "linear"


def _apply_override(factor: FactorEnvelope, override: FactorOverride | None) -> FactorEnvelope:
    """Apply one envelope-form row, validating what the user asked for.

    **A real range on a held-constant axis opens it up** (§11.5). "Always 60"
    is a fact about the runs so far, not a rule; a user filling gaps can ask
    for the batch to vary it, and the way they say so is by giving it bounds.
    The axis then joins the design space properly -- counted in the joint
    radius, swept in the pairwise pass, plotted, and moved by suggestions --
    rather than being a special case each of those has to know about.

    Refused where the descriptor declares no domain to return to
    (:attr:`FactorEnvelope.varying_kind` is ``None``) — see
    :func:`_varying_kind` for which types qualify and why.

    A **choice** is opened up by giving it levels rather than bounds. Two or
    more are required for the same reason a numeric promotion requires
    ``hi > lo``: one level is what it already was, so promoting to it would
    report an axis as varying that no suggestion can move.
    """
    if override is None:
        return factor
    if override.hold is not None and factor.kind in ("continuous", "integer", "categorical"):
        # Out of the design space, at a stated value. Not a zero-width range:
        # a `lo == hi` axis stays "varying" to every reader while contributing
        # nothing to distance, which is the worst of both.
        #
        # Only a *varying* axis. Holding a ``derived`` one would demote it to
        # ``pinned``, and a pinned axis rides along in every suggested run —
        # which is precisely what §1.2.1 forbids, because nobody can run a job
        # at a snapshot index. ``pinned`` is already held, so it is a no-op.
        return FactorEnvelope(
            name=factor.name,
            kind="pinned",
            pinned_value=override.hold,
            label=factor.label,
            declared_lo=factor.declared_lo,
            declared_hi=factor.declared_hi,
            declared_levels=factor.declared_levels,
            precision=factor.precision,
            # So it can be opened up again without re-reading the descriptor.
            varying_kind=factor.varying_kind or factor.kind,
        )
    if (
        factor.kind == "pinned"
        and factor.varying_kind == "categorical"
        and override.levels is not None
        and len(override.levels) > 1
    ):
        return FactorEnvelope(
            name=factor.name,
            kind="categorical",
            levels=tuple(override.levels),
            label=factor.label,
            declared_levels=factor.declared_levels,
            varying_kind=factor.varying_kind,
            # **Kept, not dropped.** ``PointSet.rows`` carries only the varied
            # factors, so the value the archive actually used lives once in
            # ``constants``. The engine reads through ``PointSet.values`` and
            # is fine; a *chart* reading the raw rows is not, and without this
            # every existing run lost its value on the promoted axis and
            # disappeared from that axis's panels — reported as "the previous
            # Al runs are not showing in the pairwise plots".
            pinned_value=factor.pinned_value,
        )
    if (
        factor.kind == "pinned"
        and override.lo is not None
        and override.hi is not None
        and override.hi > override.lo
        and factor.varying_kind in ("continuous", "integer")
    ):
        return FactorEnvelope(
            name=factor.name,
            kind=factor.varying_kind,
            scale=override.scale or factor.scale,
            lo=override.lo,
            hi=override.hi,
            label=factor.label,
            declared_lo=factor.declared_lo,
            declared_hi=factor.declared_hi,
            precision=factor.precision,
            varying_kind=factor.varying_kind,
        )
    lo = override.lo if override.lo is not None else factor.lo
    hi = override.hi if override.hi is not None else factor.hi
    scale = override.scale or factor.scale
    levels = override.levels if override.levels is not None else factor.levels

    if lo is not None and hi is not None and lo > hi:
        raise CoverageError(
            f"The range for '{factor.name}' has its lowest value ({lo}) above its "
            f"highest ({hi}). Swap them, or widen the range."
        )
    if scale == "log" and (lo is None or lo <= 0.0):
        raise CoverageError(
            f"'{factor.name}' can only be measured on a log scale if every value in "
            f"its range is greater than zero (this range starts at {lo}). Use a "
            f"linear scale, or raise the lowest value."
        )
    return FactorEnvelope(
        name=factor.name,
        kind=factor.kind,
        scale=scale,
        lo=lo,
        hi=hi,
        levels=levels,
        pinned_value=factor.pinned_value,
        # An override changes the *domain*, never what the input is called,
        # nor what the tool itself allows.
        label=factor.label,
        declared_lo=factor.declared_lo,
        declared_hi=factor.declared_hi,
        precision=factor.precision,
        varying_kind=factor.varying_kind,
    )


def snap_to_step(
    value: float,
    step: float | None,
    *,
    lo: float | None = None,
    hi: float | None = None,
) -> float:
    """Round ``value`` onto an increment of ``step``, clamped to ``[lo, hi]``.

    **The one implementation of "a value this input could actually take".**
    Split out of :func:`_snap_precision` so the *held* value of an axis a
    user has taken out of a batch (§11.5) goes through the same arithmetic as
    a suggested coordinate. It did not: holds were emitted as the raw
    midpoint of the axis, so an input stepping in halves over an observed
    2.0-3.5 put ``2.75`` on every run in the batch — the same unsubmittable
    value :attr:`FactorEnvelope.precision` exists to prevent, arrived at by
    the one route that skipped the snapping.

    ``step`` of ``None`` or zero means the descriptor names no increment, in
    which case the raw value stands and only the clamp applies.
    """
    snapped = float(value)
    if step is not None and step > 0.0:
        snapped = round(snapped / step) * step
        # Rounding to a step of 0.01 in binary floating point lands on
        # 4.3600000000000003; the digit count of the step is the honest
        # precision.
        digits = max(0, -math.floor(math.log10(step))) if step < 1.0 else 0
        snapped = round(snapped, digits + 1)
    if lo is not None:
        snapped = max(snapped, lo)
    if hi is not None:
        snapped = min(snapped, hi)
    return float(snapped)


def _snap_precision(value: float, factor: FactorEnvelope) -> float:
    """Round to the input's own increment, and to its own number of digits.

    A design point must be a value the tool would accept. Left raw, the
    greedy walk proposed ``4.360013149188567`` for an input stepping in
    hundredths -- unsubmittable, and quoted to a precision the tool does not
    claim to have. Clamped back inside the axis afterwards, because rounding
    near a bound can step just outside it.
    """
    return snap_to_step(value, factor.precision, lo=factor.lo, hi=factor.hi)


def _descriptor_fields(descriptor: ToolDescriptor | None) -> dict[str, InputField]:
    """``{input name: field}`` for the tool, or empty when none was supplied."""
    return {f.name: f for f in descriptor.inputs} if descriptor is not None else {}


def _axis_contains(factor: FactorEnvelope, value: Any) -> bool:
    """Whether one coordinate sits inside one axis's declared domain."""
    if factor.kind == "pinned":
        return bool(value == factor.pinned_value)
    if factor.kind == "categorical":
        return level_label(value) in (factor.levels or ())
    number = _as_float(value)
    if number is None:
        return False
    below = factor.lo is not None and number < factor.lo
    above = factor.hi is not None and number > factor.hi
    return not (below or above)


def level_label(value: Any) -> str:
    """A categorical value as the string every level is keyed by.

    **The single source for this.** Anything that counts, groups or displays a
    categorical value must go through it: the envelope's declared levels, the
    per-level counts in the diagnostics, and the balance row all key on the
    same string, and a second stringification anywhere would make a declared
    level read "0 runs" while the same value appeared under another name.

    A ``material`` input is a dict, whose ``str()`` is an unreadable repr of
    the whole record. Where one carries a human name, that is the level —
    "Iron" rather than ``{'material_id': 1, 'name': 'Iron', ...}`` — because
    these strings are shown to users in the level-balance row and in the
    "barely any runs used X" warning.
    """
    if isinstance(value, dict):
        for key in ("name", "label", "alias", "id"):
            if value.get(key) is not None:
                return str(value[key])
        return json.dumps(value, sort_keys=True, default=str)
    return str(value)


def _as_float(value: Any) -> float | None:
    """``float(value)`` for a real number (not bool / string), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


__all__ = [
    "TIME_FACTOR",
    "Envelope",
    "FactorEnvelope",
    "FactorKind",
    "FactorOverride",
    "Scale",
    "build_envelope",
    "level_label",
]
