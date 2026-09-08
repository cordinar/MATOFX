"""Slice 3 — the templated parametric-study narrative (pure).

One prose snapshot per ``analysis.kind`` so each DOE/analysis methodology's
phrasing is pinned, plus the opening range clause.
"""

from __future__ import annotations

from matofx.summaries import SummaryFacts, render_parametric


def _facts(analysis: dict[str, object], **overrides: object) -> SummaryFacts:
    details: dict[str, object] = {
        "design_mode": "lhs",
        "generated": 64,
        "completed": 64,
        "factors": ["Fin height", "Fin spacing", "Base thickness"],
        "responses": [
            {
                "label": "Peak temperature",
                "unit": "°C",
                "min": 78.1,
                "max": 112.6,
                "mean": 94.2,
                "min_run": 41,
                "max_run": 17,
            }
        ],
        "analysis": analysis,
    }
    details.update(overrides)
    return SummaryFacts(
        surface_kind="parametric_study",
        surface_id=9,
        title="Heat-sink sweep",
        state="complete",
        details=details,
    )


def test_opening_clause_states_run_count_and_range() -> None:
    text = render_parametric(_facts({"kind": "none"}))
    opening = text.paragraphs[0]
    assert "This LHS study ran 64 of 64 points" in opening
    assert "varying Fin height, Fin spacing and Base thickness" in opening
    assert "Peak temperature ranged 78.1 to 112.6 °C (mean 94.2 °C)" in opening
    assert "lowest value came from run #41, the highest from run #17" in opening


def test_src_regression_paragraph() -> None:
    analysis = {
        "kind": "src_regression",
        "response": "Peak temperature",
        "r2": 0.88,
        "factors": [
            {"factor": "Fin spacing", "src": 0.74, "sign": "negative"},
            {"factor": "Fin height", "src": 0.41, "sign": "negative"},
            {"factor": "Base thickness", "src": 0.12, "sign": "positive"},
        ],
    }
    para = render_parametric(_facts(analysis)).paragraphs[1]
    assert "standardised-regression sensitivity ranking" in para
    assert "model R-squared 0.88" in para
    assert "Fin spacing first (|SRC| = 0.74)" in para
    assert "Fin height (0.41)" in para
    # direction of the strongest effect (sign of the SRC)
    assert "Raising fin spacing lowers peak temperature." in para


def test_secondary_responses_with_drivers() -> None:
    responses = [
        {"label": "Peak temperature", "unit": "°C", "min": 78.1, "max": 112.6},
        {
            "label": "Mass",
            "unit": "kg",
            "min": 0.12,
            "max": 0.34,
            "top_driver": {"label": "Base thickness", "sign": "positive"},
        },
        {
            "label": "Pressure drop",
            "unit": "Pa",
            "min": 5.0,
            "max": 40.0,
            "top_driver": {"label": "Fin spacing", "sign": "negative"},
        },
    ]
    text = render_parametric(_facts({"kind": "none"}, responses=responses))
    joined = " ".join(text.paragraphs)
    assert "Other recorded responses:" in joined
    assert "mass ranged 0.12 to 0.34 kg (raised by base thickness)" in joined
    assert "pressure drop ranged 5 to 40 Pa (lowered by fin spacing)" in joined


def test_sobol_paragraph() -> None:
    analysis = {
        "kind": "sobol_global",
        "response": "Peak temperature",
        "indices": [
            {"factor": "Fin spacing", "s1": 0.61, "st": 0.72},
            {"factor": "Fin height", "s1": 0.18, "st": 0.29},
            {"factor": "Base thickness", "s1": 0.04, "st": 0.06},
        ],
    }
    para = render_parametric(_facts(analysis, design_mode="sobol")).paragraphs[1]
    assert "Fin spacing as the dominant driver" in para
    assert "first-order S1 = 0.61, total-order St = 0.72" in para
    assert "Fin height (S1 = 0.18, St = 0.29)" in para


def test_response_surface_paragraph() -> None:
    analysis = {
        "kind": "response_surface",
        "response": "Peak temperature",
        "unit": "°C",
        "r2": 0.97,
        "r2_adj": 0.94,
        "stationary": {"Fin height": 12.0, "Fin spacing": 3.2},
        "stationary_kind": "minimum",
        "stationary_value": 74.8,
    }
    para = render_parametric(_facts(analysis, design_mode="ccd")).paragraphs[1]
    assert "quadratic model fits peak temperature with R-squared 0.97" in para
    assert "adjusted 0.94" in para
    assert "minimum point at Fin height 12 and Fin spacing 3.2" in para
    assert "predicted peak temperature of 74.8 °C" in para


def test_screening_paragraph() -> None:
    analysis = {
        "kind": "screening_effects",
        "response": "Peak temperature",
        "unit": "°C",
        "effects": [
            {"factor": "Fin spacing", "effect": -18.4},
            {"factor": "Fin height", "effect": -11.2},
            {"factor": "Fan flow rate", "effect": -9.7},
            {"factor": "Base thickness", "effect": -0.3},
        ],
        "aliased": False,
    }
    para = render_parametric(_facts(analysis, design_mode="plackett_burman")).paragraphs[1]
    assert "most influential factors are" in para
    assert "Fin spacing (effect -18.4 °C)" in para
    assert "the remaining 1 factor show effects below the noise threshold" in para
    assert "main-effect estimates only and do not resolve interactions" in para


def test_anova_paragraph() -> None:
    analysis = {
        "kind": "anova",
        "response": "Peak temperature",
        "terms": [
            {"term": "Fin spacing", "contribution": 64.0},
            {"term": "Fin height", "contribution": 22.0},
            {"term": "Base thickness", "contribution": 9.0},
        ],
        "residual": 5.0,
    }
    para = render_parametric(_facts(analysis, design_mode="grid")).paragraphs[1]
    assert "ANOVA attributes the variation mainly to" in para
    assert "Fin spacing (64%)" in para
    assert "5% unexplained" in para


def test_extreme_runs_show_design_coordinates() -> None:
    response = {
        "label": "Peak temperature",
        "unit": "°C",
        "min": 78.1,
        "max": 112.6,
        "mean": 94.2,
        "min_run": 41,
        "max_run": 17,
        "min_design": [
            {"label": "Fin height", "value": 15.0, "unit": "mm"},
            {"label": "Fin spacing", "value": 2.0, "unit": "mm"},
        ],
        "max_design": [{"label": "Fin height", "value": 6.0, "unit": "mm"}],
    }
    text = render_parametric(_facts({"kind": "none"}, responses=[response]))
    opening = text.paragraphs[0]
    assert "run #41 (fin height 15 mm and fin spacing 2 mm)" in opening
    assert "run #17 (fin height 6 mm)" in opening


def test_skipped_invalid_points_are_flagged() -> None:
    text = render_parametric(_facts({"kind": "none"}, skipped_invalid=3))
    assert "3 candidate points skipped as failing input validation" in text.paragraphs[0]


def test_none_kind_has_no_second_paragraph() -> None:
    text = render_parametric(_facts({"kind": "none"}))
    assert len(text.paragraphs) == 1
