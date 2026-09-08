"""Tests for the per-customer feature flag mechanism."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from matofx.config import FeatureSettings
from matofx.features import Feature, UnknownFeatureError, feature_enabled


def test_default_features_empty() -> None:
    settings = FeatureSettings()
    assert settings.enabled == []


def test_known_feature_enabled() -> None:
    settings = FeatureSettings(enabled=[Feature.MULTIPLE_LANDING_DASHBOARDS.value])
    assert feature_enabled(settings, Feature.MULTIPLE_LANDING_DASHBOARDS) is True


def test_unenabled_feature_returns_false() -> None:
    settings = FeatureSettings()
    assert feature_enabled(settings, Feature.MULTIPLE_LANDING_DASHBOARDS) is False


def test_unknown_slug_rejected_at_settings_validation() -> None:
    with pytest.raises(ValidationError) as exc_info:
        FeatureSettings(enabled=["ui.something_made_up"])
    msg = str(exc_info.value)
    assert "Unknown feature slug" in msg
    assert "ui.something_made_up" in msg


def test_unknown_slug_at_query_time_raises() -> None:
    settings = FeatureSettings()
    with pytest.raises(UnknownFeatureError):
        feature_enabled(settings, "ui.brand_new_thing")


def test_string_argument_accepted() -> None:
    """``feature_enabled`` accepts either ``Feature`` or a slug string."""
    settings = FeatureSettings(enabled=[Feature.MULTIPLE_LANDING_DASHBOARDS.value])
    assert feature_enabled(settings, "ui.multiple_landing_dashboards") is True


def test_duplicates_in_enabled_list_normalised() -> None:
    settings = FeatureSettings(
        enabled=[
            Feature.MULTIPLE_LANDING_DASHBOARDS.value,
            Feature.MULTIPLE_LANDING_DASHBOARDS.value,
        ]
    )
    assert settings.enabled == [Feature.MULTIPLE_LANDING_DASHBOARDS.value]


def test_settings_round_trip_through_settings_validation() -> None:
    """Loading a Settings with [features].enabled works end-to-end."""
    from matofx.config import Settings  # noqa: PLC0415

    s = Settings(
        environment="dev",
        features=FeatureSettings(enabled=[Feature.MULTIPLE_LANDING_DASHBOARDS.value]),
    )
    assert feature_enabled(s.features, Feature.MULTIPLE_LANDING_DASHBOARDS) is True


# ---- KPI extraction (ADR-0012, slice 0) ------------------------------


def test_kpi_extraction_in_catalog() -> None:
    """``KPI_EXTRACTION`` is a known feature slug."""
    assert Feature.KPI_EXTRACTION.value == "kpi_extraction"
    assert Feature.KPI_EXTRACTION in set(Feature)


def test_kpi_extraction_disabled_by_default() -> None:
    """Off by default — surfaces stay hidden until the operator opts in."""
    settings = FeatureSettings()
    assert feature_enabled(settings, Feature.KPI_EXTRACTION) is False


def test_kpi_extraction_enabled_when_listed() -> None:
    """Enabled when ``kpi_extraction`` is in the ``[features].enabled`` list."""
    settings = FeatureSettings(enabled=[Feature.KPI_EXTRACTION.value])
    assert feature_enabled(settings, Feature.KPI_EXTRACTION) is True


def test_kpi_extraction_string_form_works() -> None:
    """``feature_enabled`` accepts the slug string for the KPI feature."""
    settings = FeatureSettings(enabled=[Feature.KPI_EXTRACTION.value])
    assert feature_enabled(settings, "kpi_extraction") is True


def test_kpi_extraction_round_trip_through_settings() -> None:
    """Loading a Settings with ``kpi_extraction`` works end-to-end."""
    from matofx.config import Settings  # noqa: PLC0415

    s = Settings(
        environment="dev",
        features=FeatureSettings(enabled=[Feature.KPI_EXTRACTION.value]),
    )
    assert feature_enabled(s.features, Feature.KPI_EXTRACTION) is True
