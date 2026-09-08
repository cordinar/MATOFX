"""The Results-tab input-importance routing (Option 2).

Designed studies lead with ANOVA; space-filling / table / list studies lead
with the regression Sensitivity; Sobol is always supplementary (Advanced), so
it never leads.
"""

from __future__ import annotations

import pytest

from matofx.ui.pages.studies import (
    _importance_renderers,
    _render_anova_card,
    _render_sensitivity_card,
    _render_sobol_card,
)


@pytest.mark.parametrize(
    "mode", ["grid", "frac_factorial", "plackett_burman", "ccd", "box_behnken"]
)
def test_designed_studies_lead_with_anova(mode: str) -> None:
    lead, others = _importance_renderers(mode)
    assert lead is _render_anova_card
    assert _render_sensitivity_card in others
    assert _render_sobol_card in others


@pytest.mark.parametrize("mode", ["lhs", "sobol", "random", "table", "list", ""])
def test_space_filling_and_table_lead_with_sensitivity(mode: str) -> None:
    lead, others = _importance_renderers(mode)
    assert lead is _render_sensitivity_card
    assert _render_anova_card in others
    assert _render_sobol_card in others


@pytest.mark.parametrize("mode", ["grid", "lhs", "ccd", "random"])
def test_sobol_is_always_supplementary(mode: str) -> None:
    lead, others = _importance_renderers(mode)
    assert lead is not _render_sobol_card
    assert _render_sobol_card in others


def test_lead_is_never_in_the_others_list() -> None:
    for mode in ("grid", "lhs", "ccd", "random", "table"):
        lead, others = _importance_renderers(mode)
        assert lead not in others
        assert len(others) == 2  # the two non-lead importance methods
