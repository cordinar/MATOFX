"""Unit tests for the descriptor-wizard KPI editor helpers (slice 11)."""

from __future__ import annotations

from matofx.ui.pages._kpi_wizard_helpers import (
    category_autocomplete_options,
    clear_kpi_overrides,
    count_declared_kpis,
    disabled_checkbox_tooltip,
    kpi_eligible,
    kpis_declared_badge_text,
)

# ---- kpi_eligible ----------------------------------------------------


def test_kpi_eligible_only_for_number_type() -> None:
    assert kpi_eligible("number") is True
    assert kpi_eligible("string") is False
    assert kpi_eligible("file") is False
    assert kpi_eligible("table") is False
    assert kpi_eligible(None) is False


# ---- count_declared_kpis --------------------------------------------


def test_count_kpis_zero_when_none_flagged() -> None:
    outputs = [
        {"name": "x", "type": "number"},
        {"name": "y", "type": "string"},
    ]
    assert count_declared_kpis(outputs) == 0


def test_count_kpis_counts_only_truthy_flags() -> None:
    outputs = [
        {"name": "a", "type": "number", "kpi": True},
        {"name": "b", "type": "number", "kpi": False},
        {"name": "c", "type": "number", "kpi": True},
        {"name": "d", "type": "number"},  # absent kpi key
    ]
    assert count_declared_kpis(outputs) == 2


def test_count_kpis_handles_garbage_entries() -> None:
    """Malformed entries are skipped, not crashed."""
    assert count_declared_kpis([None, "junk", {"kpi": True}]) == 1  # type: ignore[list-item]


# ---- clear_kpi_overrides --------------------------------------------


def test_clear_overrides_removes_all_kpi_keys() -> None:
    field = {
        "name": "x",
        "kpi": True,
        "kpi_label": "Peak",
        "kpi_unit": "K",
        "kpi_category": "thermal",
    }
    clear_kpi_overrides(field)
    assert "kpi_label" not in field
    assert "kpi_unit" not in field
    assert "kpi_category" not in field
    # `kpi` itself is untouched — caller owns the flag toggle.
    assert field["kpi"] is True


def test_clear_overrides_is_idempotent_on_missing_keys() -> None:
    field = {"name": "x"}
    clear_kpi_overrides(field)
    assert field == {"name": "x"}


# ---- kpis_declared_badge_text ---------------------------------------


def test_badge_text_zero_uses_plural() -> None:
    assert kpis_declared_badge_text([]) == "0 KPIs declared"


def test_badge_text_one_uses_singular() -> None:
    assert kpis_declared_badge_text([{"kpi": True}]) == "1 KPI declared"


def test_badge_text_many_uses_plural() -> None:
    assert (
        kpis_declared_badge_text([{"kpi": True}, {"kpi": True}, {"kpi": True}]) == "3 KPIs declared"
    )


# ---- category_autocomplete_options ----------------------------------


def test_autocomplete_dedups_and_preserves_install_order() -> None:
    assert category_autocomplete_options(["thermal", "structural", "thermal"]) == [
        "thermal",
        "structural",
    ]


def test_autocomplete_surfaces_current_value_first() -> None:
    """The current field value lands at the front of the picker."""
    assert category_autocomplete_options(
        ["thermal", "structural"], current_value="manufacturing"
    ) == ["manufacturing", "thermal", "structural"]


def test_autocomplete_skips_current_value_if_already_in_pool() -> None:
    """No duplicates when the current value is already in the install list."""
    assert category_autocomplete_options(["thermal", "structural"], current_value="thermal") == [
        "thermal",
        "structural",
    ]


def test_autocomplete_skips_garbage_categories() -> None:
    assert category_autocomplete_options(
        ["thermal", "", None, 42]  # type: ignore[list-item]
    ) == ["thermal"]


# ---- disabled_checkbox_tooltip --------------------------------------


def test_tooltip_text_matches_validator_message() -> None:
    """The tooltip echoes the slice-1 validator's `type='number'` rule."""
    assert "scalar-numeric" in disabled_checkbox_tooltip()
    assert "number" in disabled_checkbox_tooltip()
