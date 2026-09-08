"""Coverage configuration + feature toggle (ADR-0020 §16, slice 1).

Every tunable lives in ``matofx.config`` — no second layer of defaults at a
function signature (``feedback_no_hardcoded_defaults_outside_config``) — and
the whole feature is gated per customer
(``feedback_per_customer_feature_toggles``).
"""

from __future__ import annotations

import pytest

from matofx.config import CoverageSettings, Settings
from matofx.features import Feature
from matofx.studies.coverage import DesignCoverage
from matofx.studies.coverage.metrics import candidate_resolution

EXPECTED_KEYS = {
    "min_points_for_analysis",
    "cold_start_ratio",
    "duplicate_eps",
    "min_level_points",
    "min_points_per_group",
    "categorical_mismatch_distance",
    "max_abs_correlation",
    "verdict_thresholds",
    "n_candidates",
    "edge_percentile",
    "max_lattice_cardinality",
}


def test_every_declared_config_key_exists() -> None:
    assert set(CoverageSettings.model_fields) >= EXPECTED_KEYS


def test_coverage_hangs_off_the_top_level_settings_object() -> None:
    settings = Settings(environment="dev")
    assert isinstance(settings.coverage, CoverageSettings)


def test_the_defaults_are_sane() -> None:
    d = CoverageSettings()
    assert d.min_points_for_analysis >= 3
    assert d.cold_start_ratio >= 1.0
    assert 0.0 < d.duplicate_eps < 0.5
    assert d.min_level_points >= 1
    assert d.min_points_per_group >= 1
    assert 0.0 < d.categorical_mismatch_distance <= 1.0
    assert 0.5 <= d.max_abs_correlation < 1.0
    assert d.n_candidates >= 256
    assert 50.0 < d.edge_percentile <= 100.0
    assert d.max_lattice_cardinality >= 2


def test_the_verdict_thresholds_are_ordered_inside_the_unit_interval() -> None:
    d = CoverageSettings()
    assert 0.0 < d.verdict_thresholds["thin"] < d.verdict_thresholds["good"] < 1.0


def test_the_settings_model_is_frozen_and_rejects_unknown_keys() -> None:
    with pytest.raises(Exception, match=r"(?i)extra|forbidden|frozen"):
        CoverageSettings(nonsense_key=1)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("min_points_for_analysis", 0),
        ("cold_start_ratio", 0.0),
        ("duplicate_eps", -0.1),
        ("min_level_points", 0),
        ("min_points_per_group", 0),
        ("categorical_mismatch_distance", 0.0),
        ("max_abs_correlation", 1.5),
        ("n_candidates", 0),
        ("edge_percentile", 101.0),
        ("max_lattice_cardinality", 0),
    ],
)
def test_out_of_band_values_are_rejected(field: str, value: object) -> None:
    with pytest.raises(Exception, match=r"(?i)greater|less|input should|valid"):
        CoverageSettings(**{field: value})  # type: ignore[arg-type]


def test_verdict_thresholds_need_both_keys() -> None:
    with pytest.raises(Exception, match=r"(?i)good|thin"):
        CoverageSettings(verdict_thresholds={"good": 0.7})


# --- the feature toggle ---------------------------------------------------


def test_the_feature_slug_is_in_the_closed_catalog() -> None:
    assert Feature.DESIGN_SPACE_COVERAGE.value == "design_space_coverage"


def test_the_feature_can_be_enabled_through_config() -> None:
    settings = Settings(environment="dev", features={"enabled": ["design_space_coverage"]})
    assert "design_space_coverage" in settings.features.enabled


def test_the_engine_itself_is_not_gated_only_its_surfaces() -> None:
    # The data model + engine exist regardless; the flag gates the surfaces,
    # per the project-wide toggle rule. Importing with the flag off must
    # still work, so the cohort service can compute a report the UI hides.
    from matofx.studies import coverage  # noqa: PLC0415

    assert hasattr(coverage, "analyse_design")


# --------------------------------------------------------------------------
# The coarse-estimate threshold is calibrated, not guessed (finding 77)
# --------------------------------------------------------------------------


def test_the_coarse_estimate_warning_fires_where_the_error_is() -> None:
    """The sampled covering radius is **low-biased** — the flattering
    direction, since the archive then looks better covered than it is.

    Measured against a 64x denser probe, six designs per dimension across
    uniform, clustered and Latin-hypercube shapes, the median error is -1.4%
    at two factors, -3.4% at three, -3.8% at four, then **-6.5% at five**,
    -5.7% at six, -8.1% at seven and -9.2% at eight.

    The threshold was 3.0, which warns only from **eight** factors up — so
    five, six and seven varying inputs showed a precise-looking number 6-8%
    optimistic with no caveat at all. It is now set where the evidence puts
    the boundary, and this test is what stops it drifting back.
    """
    settings = CoverageSettings()
    fires_at = {
        d: candidate_resolution(settings.n_candidates, d) < settings.min_candidate_resolution
        for d in range(2, 9)
    }
    # Quiet where the estimate is good to a few per cent...
    assert not any(fires_at[d] for d in (2, 3, 4)), (
        "warning fires where the estimate is within ~4% — a warning that is "
        "always on is one users learn to ignore (§7.1)"
    )
    # ...and speaking up from the point the median error passes 5%.
    assert all(fires_at[d] for d in (5, 6, 7, 8)), (
        "the overall figure is >5% optimistic here and says nothing about it"
    )


def test_the_probe_density_is_recorded_on_the_report() -> None:
    """A stored report outlives the setting that produced it, so two reports
    written under different ``n_candidates`` must not look comparable when
    they are not."""
    design = DesignCoverage(
        criterion="model_free",
        verdict="good",
        message="",
        covering_radius=0.4,
        coverage_score=0.9,
        n_candidates=4096,
    )
    assert DesignCoverage.from_json_obj(design.to_json_obj()).n_candidates == 4096
