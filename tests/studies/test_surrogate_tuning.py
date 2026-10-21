"""Per-result tuning: log inputs and the method x scale choice (ADR-0022).

Synthetic shapes replay what the benchmark behind the ADR found: a power law in
the inputs is best learned by a Gaussian process on logged inputs and output, an
exactly quadratic additive result by a polynomial, and a noisy near-tie should
keep the model's own method.
"""

from __future__ import annotations

import numpy as np
import pytest

from matofx.studies.surrogate import Surrogate, fit_surrogate
from matofx.studies.surrogate.tuning import (
    Config,
    ResultTuner,
    describe,
    log_eligible_factors,
)

_RNG = np.random.default_rng(1)
_N = 30
_FINS = _RNG.uniform(2.0, 12.0, _N)
_FAN = _RNG.uniform(1.0, 10.0, _N)
_COLS = {"fins": list(_FINS), "fan": list(_FAN)}
_POWER = [300.0 * f**-1.2 * v**-0.6 for f, v in zip(_FINS, _FAN, strict=True)]
_QUAD = [100.0 + 3.0 * f * f + 2.0 * v * f for f, v in zip(_FINS, _FAN, strict=True)]


# --- log inputs in the core -----------------------------------------------------


def test_log_inputs_predict_in_real_units_and_help_a_power_law() -> None:
    plain = fit_surrogate(_COLS, _POWER, kind="gp", output_scale="log")
    logged = fit_surrogate(
        _COLS, _POWER, kind="gp", output_scale="log", log_factors=["fins", "fan"]
    )
    assert set(logged.log_factors) == {"fins", "fan"}
    assert logged.quality.rmse_cv < plain.quality.rmse_cv  # type: ignore[operator]
    means, _ = logged.predict([{"fins": 6.0, "fan": 5.0}])
    assert means[0] == pytest.approx(300.0 * 6.0**-1.2 * 5.0**-0.6, rel=0.02)
    # Real units stay real: the coverage gauge and exported form read these.
    assert logged.ranges["fins"][0] == pytest.approx(min(_FINS))
    assert logged.training_inputs["fins"] == pytest.approx(list(_FINS))


def test_a_non_positive_input_is_refused_not_turned_into_nan() -> None:
    logged = fit_surrogate(_COLS, _POWER, kind="gp", log_factors=["fins"])
    with pytest.raises(ValueError, match="must be above 0"):
        logged.predict([{"fins": 0.0, "fan": 5.0}])


def test_log_inputs_refused_when_a_training_value_is_not_positive() -> None:
    cols = {"fins": [0.0, *_FINS[1:]], "fan": list(_FAN)}
    surrogate = fit_surrogate(cols, _POWER, kind="gp", log_factors=["fins"])
    assert not surrogate.fitted
    assert "Log inputs aren't possible" in (surrogate.quality.message or "")


def test_unknown_or_constant_log_factors_are_ignored() -> None:
    surrogate = fit_surrogate(_COLS, _POWER, kind="gp", log_factors=["nope"])
    assert surrogate.fitted
    assert surrogate.log_factors == ()


def test_a_bundle_written_before_adr_0022_has_no_logged_inputs() -> None:
    surrogate = fit_surrogate(_COLS, _POWER, kind="gp")
    before = surrogate.predict([{"fins": 6.0, "fan": 5.0}])
    state = dict(surrogate.__dict__)
    del state["log_factors"]
    restored = Surrogate.__new__(Surrogate)
    restored.__setstate__(state)
    assert restored.log_factors == ()
    assert restored.predict([{"fins": 6.0, "fan": 5.0}]) == before


def test_conditioning_a_log_input_fit_keeps_real_unit_training_inputs() -> None:
    logged = fit_surrogate(
        _COLS, _POWER, kind="gp", output_scale="log", log_factors=["fins", "fan"]
    )
    gap = [{"fins": 11.5, "fan": 1.2}]
    copy = logged.conditioned_on(gap)
    assert copy.log_factors == logged.log_factors
    assert copy.training_inputs["fins"][-1] == pytest.approx(11.5)
    before, after = logged.relative_sigma(gap), copy.relative_sigma(gap)
    assert before is not None
    assert after is not None
    assert after[0] < before[0]


def test_log_eligibility_respects_declared_minimums() -> None:
    assert log_eligible_factors(_COLS) == ("fins", "fan")
    assert log_eligible_factors(_COLS, {"fins": 0.0, "fan": 0.5}) == ("fan",)
    # A tool that declares no minimum for an input can't have it logged.
    assert log_eligible_factors(_COLS, {"fins": 1.0}) == ("fins",)
    assert log_eligible_factors({"x": [0.0, 1.0]}) == ()


# --- the tuner ------------------------------------------------------------------


def _tuner(y, methods=("gp", "poly"), mode="automatic"):
    return ResultTuner(
        _COLS, list(y), methods=list(methods), mode=mode, log_eligible=log_eligible_factors(_COLS)
    )


@pytest.fixture(scope="module")
def power_tuner() -> ResultTuner:
    """One evaluated tuner for the power law, shared (tuning is many fits)."""
    return _tuner(_POWER)


@pytest.fixture(scope="module")
def quad_tuner() -> ResultTuner:
    return _tuner(_QUAD, methods=("gp_matern", "poly"))


def test_off_mode_is_the_primary_method_as_measured() -> None:
    tuner = _tuner(_POWER, mode="off", methods=("gp", "gp_matern", "poly", "gbm"))
    surrogate, decision = tuner.decide("gp_matern")
    assert decision.config == Config("gp_matern")
    assert decision.source == "mode"
    assert surrogate.output_scale == "as_measured"
    assert surrogate.log_factors == ()
    assert {c.config for c in decision.candidates} <= {
        Config(m) for m in ("gp", "gp_matern", "poly", "gbm")
    }


def test_log_inputs_are_offered_to_gaussian_processes_only() -> None:
    planned = _tuner(_POWER, methods=("gp", "gp_matern", "poly", "gbm")).planned()
    assert any(c.method == "gp" and c.input_scale == "log" for c in planned)
    assert not any(c.method in ("poly", "gbm") and c.input_scale == "log" for c in planned)
    assert len(planned) == 12


def test_a_power_law_moves_to_logged_inputs_and_output(power_tuner: ResultTuner) -> None:
    surrogate, decision = power_tuner.decide("gp")
    assert decision.source == "auto"
    assert decision.config.input_scale == "log"
    assert decision.config.output_scale == "log"
    assert surrogate.log_factors
    assert decision.rmse < decision.baseline_rmse  # type: ignore[operator]
    assert "more accurate" in decision.reason
    assert decision.warning is None


def test_an_exact_quadratic_moves_to_a_polynomial(quad_tuner: ResultTuner) -> None:
    _, decision = quad_tuner.decide("gp_matern")
    assert decision.config.method == "poly"
    assert decision.source == "auto"


def test_nothing_changes_when_only_the_primary_method_is_available() -> None:
    _, decision = _tuner(_QUAD, methods=("gbm",)).decide("gbm")
    assert decision.config.method == "gbm"
    assert decision.config.input_scale == "as_measured"


def test_one_evaluation_serves_every_primary_method() -> None:
    calls = {"n": 0}
    tuner = ResultTuner(
        _COLS,
        _POWER,
        methods=["gp", "poly"],
        log_eligible=("fins", "fan"),
        progress=lambda: calls.__setitem__("n", calls["n"] + 1),
    )
    tuner.decide("gp")
    after_first = calls["n"]
    tuner.decide("poly")
    assert calls["n"] == after_first  # no refits for the second decision
    assert after_first == len(tuner.planned())


def test_a_user_choice_worse_than_the_best_warns_at_any_margin(power_tuner: ResultTuner) -> None:
    tuner = power_tuner
    _, auto = tuner.decide("gp")
    worse = next(
        c.config
        for c in auto.candidates
        if c.rmse is not None
        and c.rmse > min(x.rmse for x in auto.candidates if x.rmse is not None)
    )
    _, decision = tuner.decide("gp", override=worse)
    assert decision.source == "user"
    assert decision.config == worse
    assert decision.warning is not None
    assert "Higher error than the best combination" in decision.warning
    assert "MATOFX would pick" in decision.reason


def test_choosing_the_best_combination_yourself_does_not_warn(power_tuner: ResultTuner) -> None:
    tuner = power_tuner
    _, auto = tuner.decide("gp")
    best = auto.candidates[0].config
    _, decision = tuner.decide("gp", override=best)
    assert decision.warning is None


def test_matofx_own_pick_never_warns_even_when_it_is_not_the_lowest_score(
    power_tuner: ResultTuner, quad_tuner: ResultTuner
) -> None:
    for tuner, primary in ((power_tuner, "gp"), (power_tuner, "poly"), (quad_tuner, "gp_matern")):
        _, decision = tuner.decide(primary)  # type: ignore[arg-type]
        assert decision.warning is None


def test_an_unfittable_override_falls_back_to_the_baseline() -> None:
    cols = {"a": list(_FINS[:8]), "b": list(_FAN[:8]), "c": list(_FINS[:8] * 2 + _FAN[:8])}
    y = list(np.asarray(_POWER[:8]))
    tuner = ResultTuner(cols, y, methods=["gp", "poly"], log_eligible=())
    surrogate, decision = tuner.decide("gp", override=Config("poly"))
    assert surrogate.fitted
    assert decision.config.method == "gp"
    assert "couldn't be fitted" in decision.reason


def test_decision_serialises_with_candidates(power_tuner: ResultTuner) -> None:
    _, decision = power_tuner.decide("gp")
    data = decision.to_json()
    assert data["method"] in ("gp", "poly")
    assert data["baseline"]["method"] == "gp"
    assert len(data["candidates"]) == 6
    assert {"key", "rmse", "q2", "method", "output_scale", "input_scale"} <= set(
        data["candidates"][0]
    )


def test_describe_reads_as_plain_words() -> None:
    assert (
        describe(Config("gp_matern", "log", "log"))
        == "Gaussian process (Matérn), log inputs + output"
    )
    assert describe(Config("poly")) == "polynomial, as measured"


# --- the baseline: ADR-0021's output-scale rule, kept inside the tuner ---------------


def test_before_tuning_is_always_as_measured_and_log_output_is_a_tuning_choice() -> None:
    """A table labelled "before tuning" must not already show a log scale."""
    power = ResultTuner(_COLS, _POWER, methods=["gp"])
    assert power.baseline("gp")[0] == Config("gp")
    _, decision = power.decide("gp")
    assert decision.baseline == Config("gp")
    assert decision.config == Config("gp", "log")
    mass = ResultTuner(
        _COLS,
        [100.0 + 20.0 * f + 3.0 * v for f, v in zip(_FINS, _FAN, strict=True)],
        methods=["gp"],
    )
    assert mass.decide("gp")[1].config == Config("gp")


def test_a_result_with_non_positive_runs_keeps_output_as_measured() -> None:
    y = [p - 5.0 for p in _POWER]
    tuner = ResultTuner(_COLS, y, methods=["gp", "poly"], log_eligible=("fins", "fan"))
    surrogate, decision = tuner.decide("gp")
    assert surrogate.fitted
    assert decision.config.output_scale == "as_measured"
    assert all(c.rmse is None for c in decision.candidates if c.config.output_scale == "log")


def test_repeats_stop_at_the_first_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    from matofx.studies.surrogate import tuning  # noqa: PLC0415

    calls: list[str] = []

    def _scores(*_args: object, kind: str, **_kwargs: object) -> list[float]:
        calls.append(kind)
        return [1.0] if kind == "gp" else [2.0]

    monkeypatch.setattr(tuning, "repeated_cv_rmse", _scores)
    tuner = ResultTuner(_COLS, _POWER, methods=["gp", "poly"])
    assert tuner._strict(Config("gp"), Config("poly")) is None
    # The (stable) baseline's repeats are all scored; the contender stops at its first loss.
    assert calls == ["gp", "gp", "gp", "poly"]


def test_a_switch_must_clear_the_ten_percent_margin(monkeypatch: pytest.MonkeyPatch) -> None:
    from matofx.studies.surrogate import tuning  # noqa: PLC0415

    def _scores(*_args: object, kind: str, **_kwargs: object) -> list[float]:
        return [1.0] if kind == "gp" else [0.95]

    monkeypatch.setattr(tuning, "repeated_cv_rmse", _scores)
    tuner = ResultTuner(_COLS, _POWER, methods=["gp", "poly"])
    assert tuner._strict(Config("gp"), Config("poly")) is None


def test_reasons_keep_method_names_capitalised() -> None:
    """``str.capitalize`` turned "Gaussian process (Matérn)" into "(matérn)"."""
    from matofx.studies.surrogate.tuning import _sentence  # noqa: PLC0415

    assert (
        _sentence("gaussian process (Matérn), log inputs")
        == "Gaussian process (Matérn), log inputs"
    )


def _repeat_scores(monkeypatch: pytest.MonkeyPatch, scores: dict[str, list[float]]) -> None:
    from matofx.studies.surrogate import tuning  # noqa: PLC0415

    def _scores(
        *_args: object, kind: str, seeds: tuple[int, ...], **_kwargs: object
    ) -> list[float]:
        return [scores[kind][tuning.REPEATS.index(seeds[0])]]

    monkeypatch.setattr(tuning, "repeated_cv_rmse", _scores)


def test_an_unstable_baseline_is_judged_on_average_and_worst_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real Matérn air-velocity case: it wins one repeat narrowly but swings 5-fold."""
    _repeat_scores(
        monkeypatch, {"gp_matern": [0.0493, 0.0706, 0.2719], "gp": [0.045, 0.0767, 0.0744]}
    )
    tuner = ResultTuner(_COLS, _POWER, methods=["gp_matern", "gp"])
    won = tuner._strict(Config("gp_matern"), Config("gp"))
    assert won is not None
    assert "steadier" in won[1]


def test_a_stable_baseline_still_needs_every_repeat(monkeypatch: pytest.MonkeyPatch) -> None:
    _repeat_scores(monkeypatch, {"gp_matern": [0.05, 0.07, 0.09], "gp": [0.02, 0.08, 0.03]})
    tuner = ResultTuner(_COLS, _POWER, methods=["gp_matern", "gp"])
    assert tuner._strict(Config("gp_matern"), Config("gp")) is None


def test_an_unstable_baseline_is_kept_if_the_contender_is_no_steadier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repeat_scores(monkeypatch, {"gp_matern": [0.05, 0.07, 0.30], "gp": [0.01, 0.02, 0.35]})
    tuner = ResultTuner(_COLS, _POWER, methods=["gp_matern", "gp"])
    assert tuner._strict(Config("gp_matern"), Config("gp")) is None
