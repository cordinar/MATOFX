"""Per-result surrogate tuning: method, output scale and input scale (ADR-0022).

A scalar result can be fitted many ways, and the benchmark behind ADR-0022 showed
no single way suits every result: additive results (a mass) fit best as measured,
often by a polynomial; power-law results (a temperature rise) fit best by a
Gaussian process on logged inputs and output. Picking by one cross-validation
score is noisy, though — it made a quarter of results worse — so a result only
leaves its **baseline** when an alternative is *clearly and consistently* better.

The baseline is the model's starting method exactly as measured — "before tuning".
Every alternative, log output included, has to be clearly and consistently better
than it to be used.

:class:`ResultTuner` evaluates a result's candidates once and can then decide for
any primary method (Fit & compare re-renders when the user changes the kept
method), sharing fitted candidates and repeated-check scores between decisions.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np

from matofx.studies.surrogate import (
    GP_KINDS,
    OutputScale,
    SourceRef,
    Surrogate,
    SurrogateKind,
    fit_surrogate,
    repeated_cv_rmse,
)

TuningMode = Literal["automatic", "off"]
"""*Automatic*: the ADR-0022 rule. *Off*: one method, as measured (pre-ADR-0021)."""

InputScale = Literal["as_measured", "log"]
TuningSource = Literal["auto", "mode", "user"]

TUNING_MODES: tuple[str, ...] = ("automatic", "off")
METHODS: tuple[str, ...] = ("gp", "gp_matern", "poly", "gbm")

WIN_MARGIN = 0.9
"""A switch needs the mean repeated-CV error to be at most 90% of the baseline's."""

REPEATS: tuple[int, ...] = (10, 11, 12)
"""Seeds of the shuffled 5-fold repeats; a switch must win every one."""

UNSTABLE_RATIO = 3.0
"""A baseline whose repeated-check errors differ by more than this is unstable, and
is then judged on average and worst case rather than repeat by repeat."""


@dataclass(frozen=True, slots=True)
class Config:
    """One way to fit a result."""

    method: SurrogateKind
    output_scale: OutputScale = "as_measured"
    input_scale: InputScale = "as_measured"

    @property
    def key(self) -> str:
        """Stable identifier, ``method|output_scale|input_scale``."""
        return f"{self.method}|{self.output_scale}|{self.input_scale}"

    @classmethod
    def from_key(cls, key: str) -> Config:
        """Inverse of :attr:`key`."""
        method, out, inp = key.split("|")
        return cls(method, out, inp)  # type: ignore[arg-type]

    def to_json(self) -> dict[str, str]:
        """The three choices as plain JSON."""
        return {
            "method": self.method,
            "output_scale": self.output_scale,
            "input_scale": self.input_scale,
        }


@dataclass(frozen=True, slots=True)
class CandidateScore:
    """A candidate's displayed (single) cross-validated quality, result's units."""

    config: Config
    rmse: float | None
    q2: float | None
    message: str | None = None
    """Why it couldn't be fitted, when it couldn't."""

    def to_json(self) -> dict[str, Any]:
        """Flat JSON for a comparison table row."""
        return {
            **self.config.to_json(),
            "key": self.config.key,
            "rmse": self.rmse,
            "q2": self.q2,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class TuningDecision:
    """Which configuration a result uses, and the evidence for it."""

    config: Config
    source: TuningSource
    mode: TuningMode
    primary_method: SurrogateKind
    baseline: Config
    auto_config: Config | None
    """MATOFX's own pick (automatic mode), even when the user overrode it."""
    log_factors: tuple[str, ...]
    candidates: tuple[CandidateScore, ...]
    rmse: float | None
    q2: float | None
    baseline_rmse: float | None
    reason: str
    warning: str | None

    def to_json(self) -> dict[str, Any]:
        """JSON for a version's ``quality`` row and the compare response."""
        return {
            **self.config.to_json(),
            "key": self.config.key,
            "source": self.source,
            "mode": self.mode,
            "primary_method": self.primary_method,
            "baseline": self.baseline.to_json(),
            "auto": self.auto_config.to_json() if self.auto_config else None,
            "log_factors": list(self.log_factors),
            "candidates": [c.to_json() for c in self.candidates],
            "rmse": self.rmse,
            "q2": self.q2,
            "baseline_rmse": self.baseline_rmse,
            "reason": self.reason,
            "warning": self.warning,
        }


@dataclass
class ResultTuner:
    """Candidates for one scalar result, fitted once and reused across decisions."""

    factor_columns: Mapping[str, Sequence[float | None]]
    response: Sequence[float | None]
    methods: Sequence[SurrogateKind]
    mode: TuningMode = "automatic"
    log_eligible: Sequence[str] = ()
    """Factors that may be logged: positive training values and declared minimum."""
    categorical_columns: Mapping[str, Sequence[Any]] | None = None
    source_ref: SourceRef | None = None
    progress: Callable[[], None] | None = None
    """Called once per candidate fit, for a background job's progress bar."""

    _fits: dict[str, Surrogate] = field(default_factory=dict)
    _repeats: dict[tuple[str, int], float] = field(default_factory=dict)

    # --- evaluation -------------------------------------------------------------

    def planned(self) -> list[Config]:
        """Every configuration the mode evaluates, before fitting."""
        if self.mode == "off":
            return [Config(m) for m in self.methods]
        out: list[Config] = []
        for m in self.methods:
            inputs: tuple[InputScale, ...] = (
                ("as_measured", "log") if m in GP_KINDS and self.log_eligible else ("as_measured",)
            )
            for inp in inputs:
                for scale in ("as_measured", "log"):
                    out.append(Config(m, scale, inp))
        return out

    def evaluate(self, extra: Sequence[Config] = ()) -> None:
        """Fit every planned configuration (plus ``extra``, e.g. an override)."""
        for config in [*self.planned(), *extra]:
            self.fit(config)

    def fit(self, config: Config) -> Surrogate:
        """The fitted surrogate for ``config``, fitting it on first use."""
        if config.key not in self._fits:
            self._fits[config.key] = fit_surrogate(
                self.factor_columns,
                self.response,
                kind=config.method,
                source_ref=self.source_ref,
                categorical_columns=self.categorical_columns,
                output_scale=config.output_scale,
                log_factors=self._log_factors(config),
            )
            if self.progress is not None:
                self.progress()
        return self._fits[config.key]

    def scores(self) -> list[CandidateScore]:
        """Displayed quality of every fitted configuration, best first."""
        out = []
        for key, s in self._fits.items():
            q = s.quality
            out.append(
                CandidateScore(
                    Config.from_key(key),
                    q.rmse_cv if s.fitted else None,
                    q.q2 if s.fitted else None,
                    None if s.fitted else q.message,
                )
            )
        return sorted(out, key=lambda c: (c.rmse is None, c.rmse if c.rmse is not None else 0.0))

    # --- decisions --------------------------------------------------------------

    def baseline(self, method: SurrogateKind) -> tuple[Config, str]:
        """``method`` exactly as measured — what "before tuning" means everywhere.

        It used to include ADR-0021's automatic log-output choice, so a table
        labelled "before tuning" showed "(log output)". Log output is now simply
        one of the tuning options, held to the same "clearly and consistently
        better" rule as the others.
        """
        measured = Config(method)
        fit = self.fit(measured)
        if not fit.fitted:
            return measured, fit.quality.message or "Not fitted."
        return measured, "Fitted as measured." if self.mode == "off" else ""

    def decide(
        self, primary: SurrogateKind, override: Config | None = None
    ) -> tuple[Surrogate, TuningDecision]:
        """The configuration this result uses when ``primary`` is the model's method."""
        baseline, baseline_reason = self.baseline(primary)
        auto: Config | None = None
        reason = baseline_reason
        if self.mode == "automatic":
            auto, reason = self._auto(baseline, baseline_reason)
        if override is not None:
            chosen, source = override, "user"
            reason = f"Chosen by you ({describe(override)})."
            if auto is not None and auto != override:
                reason += f" MATOFX would pick {describe(auto)}."
        elif auto is not None:
            chosen, source = auto, "auto"
        else:
            chosen, source = baseline, "mode"

        surrogate = self.fit(chosen)
        if not surrogate.fitted:
            # An override (or a stale refit choice) that can't be fitted on today's
            # runs falls back to the baseline rather than failing the whole model.
            reason = (
                f"{describe(chosen)} couldn't be fitted ({surrogate.quality.message}); "
                f"used {describe(baseline)}."
            )
            chosen, source, surrogate = baseline, "mode", self.fit(baseline)
        scores = self.scores()
        best = next((c for c in scores if c.rmse is not None), None)
        warning = None
        if (
            source == "user"
            and best is not None
            and surrogate.quality.rmse_cv is not None
            and best.rmse is not None
            and surrogate.quality.rmse_cv > best.rmse
        ):
            warning = (
                f"Higher error than the best combination tried: {describe(best.config)} "
                f"({best.rmse:.4g} against {surrogate.quality.rmse_cv:.4g})."
            )
        base_fit = self._fits.get(baseline.key)
        return surrogate, TuningDecision(
            config=chosen,
            source=source,  # type: ignore[arg-type]
            mode=self.mode,
            primary_method=primary,
            baseline=baseline,
            auto_config=auto,
            log_factors=surrogate.log_factors,
            candidates=tuple(scores),
            rmse=surrogate.quality.rmse_cv,
            q2=surrogate.quality.q2,
            baseline_rmse=base_fit.quality.rmse_cv if base_fit is not None else None,
            reason=reason,
            warning=warning,
        )

    def _auto(self, baseline: Config, baseline_reason: str) -> tuple[Config, str]:
        self.evaluate()
        base = self.fit(baseline)
        if not base.fitted:
            return baseline, baseline_reason
        contenders = sorted(
            (
                c
                for c in self.scores()
                if c.config != baseline and _lower(c.rmse, base.quality.rmse_cv)
            ),
            key=lambda c: c.rmse,  # type: ignore[arg-type, return-value]
        )
        for contender in contenders:
            won = self._strict(baseline, contender.config)
            if won is not None:
                return won
        if contenders:
            return baseline, (
                f"{_sentence(describe(baseline))} kept: nothing else was clearly and "
                "consistently more accurate."
            )
        return baseline, (
            baseline_reason
            or f"{_sentence(describe(baseline))} was as accurate as anything else tried."
        )

    def _strict(self, baseline: Config, contender: Config) -> tuple[Config, str] | None:
        """``(contender, reason)`` if it clearly and consistently beats ``baseline``.

        Normally the contender must be at least 10% better on average **and** better
        in every repeated check. An **unstable** baseline — one whose own repeated
        checks differ by more than :data:`UNSTABLE_RATIO` — is judged on average and
        worst case instead: "better in every repeat" would let a fit that sometimes
        collapses survive on the one repeat where it happened not to. Seen on real
        data: Matérn on a 20-run channel air velocity scored 0.049 / 0.071 / 0.272
        against a smooth GP's 0.045 / 0.077 / 0.074, and was kept at a displayed
        Q² of 0.35.
        """
        base_scores = [self._repeat(baseline, seed) for seed in REPEATS]
        finite = [v for v in base_scores if math.isfinite(v)]
        spread = (
            max(finite) / min(finite)
            if len(finite) == len(base_scores) and min(finite) > 0
            else 1.0
        )
        if spread > UNSTABLE_RATIO:
            return self._strict_unstable(baseline, contender, base_scores, spread)
        cont_scores = []
        for seed, b in zip(REPEATS, base_scores, strict=True):
            c = self._repeat(contender, seed)
            if not (math.isfinite(c) and c < b):
                return None
            cont_scores.append(c)
        mean_b, mean_c = float(np.mean(base_scores)), float(np.mean(cont_scores))
        if math.isfinite(mean_b) and not mean_c < WIN_MARGIN * mean_b:
            return None
        if not math.isfinite(mean_b):
            return contender, f"{_sentence(describe(contender))}: only it could be checked."
        pct = round(100.0 * (1.0 - mean_c / mean_b))
        return contender, (
            f"{_sentence(describe(contender))} was {pct}% more accurate than "
            f"{describe(baseline)}, and better in all {len(REPEATS)} repeated checks."
        )

    def _strict_unstable(
        self, baseline: Config, contender: Config, base_scores: list[float], spread: float
    ) -> tuple[Config, str] | None:
        """The :meth:`_strict` rule for an unstable baseline: average and worst case."""
        cont_scores = []
        for seed in REPEATS:
            c = self._repeat(contender, seed)
            if not math.isfinite(c):
                return None
            cont_scores.append(c)
        mean_b, mean_c = float(np.mean(base_scores)), float(np.mean(cont_scores))
        if not (mean_c < WIN_MARGIN * mean_b and max(cont_scores) < max(base_scores)):
            return None
        pct = round(100.0 * (1.0 - mean_c / mean_b))
        return contender, (
            f"{_sentence(describe(contender))} was {pct}% more accurate on average and "
            f"steadier than {describe(baseline)}, whose accuracy varied {spread:.0f}-fold "
            "between repeated checks."
        )

    def _repeat(self, config: Config, seed: int) -> float:
        key = (config.key, seed)
        if key not in self._repeats:
            self._repeats[key] = repeated_cv_rmse(
                self.factor_columns,
                self.response,
                kind=config.method,
                output_scale=config.output_scale,
                seeds=(seed,),
                categorical_columns=self.categorical_columns,
                log_factors=self._log_factors(config),
            )[0]
        return self._repeats[key]

    def _log_factors(self, config: Config) -> tuple[str, ...]:
        return tuple(self.log_eligible) if config.input_scale == "log" else ()


_METHOD_WORDS = {
    "gp": "Gaussian process",
    "gp_matern": "Gaussian process (Matérn)",
    "poly": "polynomial",
    "gbm": "gradient boosting",
}


def describe(config: Config) -> str:
    """Plain words for a configuration, e.g. 'Gaussian process, log inputs + output'."""
    method = _METHOD_WORDS.get(config.method, config.method)
    parts = []
    if config.input_scale == "log" and config.output_scale == "log":
        parts.append("log inputs + output")
    elif config.input_scale == "log":
        parts.append("log inputs")
    elif config.output_scale == "log":
        parts.append("log output")
    else:
        parts.append("as measured")
    return f"{method}, {parts[0]}"


def log_eligible_factors(
    factor_columns: Mapping[str, Sequence[float | None]],
    declared_minimums: Mapping[str, float | None] | None = None,
) -> tuple[str, ...]:
    """Numeric factors that may be logged (ADR-0022 §6).

    Every recorded value must be positive. When the tool's declared minimums are
    given, the factor must also **declare** a minimum above zero: an input the
    tool allows to be 0 (or doesn't bound at all) could reach an exported
    surrogate as a value with no logarithm. ``None`` (no descriptor to hand)
    checks the recorded values only — for scripts and tests.
    """
    out = []
    for name, column in factor_columns.items():
        values = [float(v) for v in column if v is not None]
        if not values or min(values) <= 0.0:
            continue
        if declared_minimums is not None:
            lo = declared_minimums.get(name)
            if lo is None or float(lo) <= 0.0:
                continue
        out.append(str(name))
    return tuple(out)


def _sentence(text: str) -> str:
    """Upper-case the first letter only (``str.capitalize`` lower-cases the rest)."""
    return text[:1].upper() + text[1:]


def _lower(candidate: float | None, reference: float | None) -> bool:
    return candidate is not None and reference is not None and candidate < reference


__all__ = [
    "METHODS",
    "REPEATS",
    "TUNING_MODES",
    "UNSTABLE_RATIO",
    "WIN_MARGIN",
    "CandidateScore",
    "Config",
    "InputScale",
    "ResultTuner",
    "TuningDecision",
    "TuningMode",
    "TuningSource",
    "describe",
    "log_eligible_factors",
]
