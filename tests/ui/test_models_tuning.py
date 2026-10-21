"""Pure helpers behind the per-result tuning table on the models page (ADR-0022)."""

from __future__ import annotations

from matofx.ui.pages.models import (
    _config_label,
    _tuning_overrides,
    _tuning_warning,
    _version_method_label,
)

_CANDIDATES = [
    {"key": "poly|as_measured|as_measured", "method": "poly", "rmse": 0.09},
    {"key": "gp|log|log", "method": "gp", "output_scale": "log", "input_scale": "log", "rmse": 0.2},
    {"key": "gp|as_measured|as_measured", "method": "gp", "rmse": 0.4},
]


def _tuning(pick: str) -> dict:
    method, out, inp = pick.split("|")
    decision = {
        "key": pick,
        "mode": "automatic",
        "method": method,
        "output_scale": out,
        "input_scale": inp,
    }
    return {"mass": {"candidates": _CANDIDATES, "decisions": {"gp": decision}}}


def test_only_changed_choices_are_overrides() -> None:
    tuning = _tuning("poly|as_measured|as_measured")
    assert _tuning_overrides(tuning, "gp", ["mass"], {"mass": "poly|as_measured|as_measured"}) == (
        {},
        "automatic",
    )
    overrides, _ = _tuning_overrides(tuning, "gp", ["mass"], {"mass": "gp|log|log"})
    assert overrides == {"mass": {"method": "gp", "output_scale": "log", "input_scale": "log"}}


def test_warning_shows_for_any_choice_worse_than_the_best() -> None:
    auto = "gp|as_measured|as_measured"
    assert _tuning_warning(_CANDIDATES, "gp|log|log", auto) is not None
    assert "Polynomial, as measured" in (_tuning_warning(_CANDIDATES, "gp|log|log", auto) or "")
    # Choosing the best yourself is fine; MATOFX's own pick never warns.
    assert _tuning_warning(_CANDIDATES, "poly|as_measured|as_measured", auto) is None
    assert _tuning_warning(_CANDIDATES, auto, auto) is None


def test_mixed_methods_label() -> None:
    same = {"surrogate_method": "gp", "responses": [{"name": "a", "method": "gp"}]}
    mixed = {
        "surrogate_method": "gp_matern",
        "responses": [
            {"name": "a", "method": "poly"},
            {"name": "b", "method": "gp_matern"},
            {"name": "f", "kind": "field", "method": "gp"},
        ],
    }
    old = {"surrogate_method": "gbm", "responses": [{"name": "a", "kind": "scalar"}]}
    assert _version_method_label(same) == "Gaussian process"
    assert _version_method_label(mixed) == "Mixed methods (primary: Gaussian process (Matérn))"
    assert _version_method_label(old) == "Gradient boosting"


def test_config_label_reads_as_words() -> None:
    assert _config_label({"method": "gp", "output_scale": "log", "input_scale": "log"}) == (
        "Gaussian process, log inputs + output"
    )
    assert _config_label(None) == "—"


def test_tags_and_before_tuning_scores() -> None:
    from matofx.ui.pages.models import _before_tuning, _is_tuned, _tuned_tag  # noqa: PLC0415

    assert _tuned_tag(
        {"method": "gp", "output_scale": "as_measured", "log_factors": ["x"]}, "gp"
    ) == ("log inputs")
    assert _tuned_tag({"method": "poly", "output_scale": "as_measured"}, "gp") == "polynomial"
    assert _tuned_tag({"method": "gp", "output_scale": "as_measured"}, "gp") == ""
    # The before-tuning table and the suggested starting method use pre-tuning scores.
    assert _before_tuning({"mean_q2": 0.9985, "baseline_mean_q2": 0.9526}, "mean") == 0.9526
    assert _before_tuning({"mean_q2": 0.97}, "mean") == 0.97
    assert _is_tuned({"r": {"decisions": {"gp": {"mode": "automatic"}}}})
    assert not _is_tuned({"r": {"decisions": {"gp": {"mode": "off"}}}})


def test_fit_used_list_puts_matofx_pick_first_then_lowest_error() -> None:
    from matofx.ui.pages.models import _fit_options  # noqa: PLC0415

    scored = {
        "gp_matern|log|log": {
            "method": "gp_matern",
            "output_scale": "log",
            "input_scale": "log",
            "rmse": 0.0226,
        },
        "gp|as_measured|as_measured": {"method": "gp", "rmse": 0.0291},
        "poly|as_measured|as_measured": {"method": "poly", "rmse": 0.035},
    }
    options = _fit_options(scored, "gp|as_measured|as_measured", "gp|as_measured|as_measured")
    keys = list(options)
    assert keys == [
        "gp|as_measured|as_measured",
        "gp_matern|log|log",
        "poly|as_measured|as_measured",
    ]
    assert options[keys[0]].startswith("★ ")
    assert options[keys[0]].endswith("(before tuning)")
