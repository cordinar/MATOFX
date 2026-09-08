"""Slice-1 unit tests: feature flag, permission defaults, vocab drift.

These are pure (no DB) — they pin the contract the rest of the
parametric-studies build depends on.
"""

from __future__ import annotations

from matofx.config import FeatureSettings
from matofx.features import Feature, feature_enabled
from matofx.studies import STUDY_MODES, STUDY_STATES
from matofx.users.permissions import (
    ADMINISTRATOR_DEFAULT_PERMISSIONS,
    BETA_TESTER_DEFAULT_PERMISSIONS,
    REGULAR_USER_DEFAULT_PERMISSIONS,
    SUPER_USER_PERMISSIONS,
    Permission,
)

# Frozen expectations. These mirror designs/parametric-studies.md §1/§8
# and the *frozen literals* in migrations 0032 (states + original modes)
# and 0039 (the `table` mode, 2026-05-19 amendment). If you change a
# tuple in matofx.studies.orm you MUST ship a migration and update these
# — that is the whole point of this guard (cf. JOB_STATES / ck_job_state).
_EXPECTED_STATES = (
    "draft",
    "pending_approval",
    "running",
    "complete",
    "partial",
    "failed",
    "cancelled",
    "rejected",
)
_EXPECTED_MODES = (
    "table",
    "list",
    "grid",
    "frac_factorial",
    "plackett_burman",
    "lhs",
    "sobol",
    "random",
    "ccd",
    "box_behnken",
)


def test_feature_slug_matches_brief_catalog() -> None:
    """The slug must equal the brief's ``[features.parametric_studies]``."""
    assert Feature.PARAMETRIC_STUDIES.value == "parametric_studies"


def test_feature_off_by_default_and_toggles() -> None:
    off = FeatureSettings()
    assert feature_enabled(off, Feature.PARAMETRIC_STUDIES) is False
    on = FeatureSettings(enabled=[Feature.PARAMETRIC_STUDIES.value])
    assert feature_enabled(on, Feature.PARAMETRIC_STUDIES) is True


def test_permission_default_on_for_regular_beta_admin_and_super() -> None:
    """Same ladder as ``custom_workflows.create`` (design §12)."""
    perm = Permission.PARAMETRIC_STUDIES_CREATE
    assert perm.value == "parametric_studies.create"
    assert perm in REGULAR_USER_DEFAULT_PERMISSIONS
    assert perm in BETA_TESTER_DEFAULT_PERMISSIONS
    assert perm in ADMINISTRATOR_DEFAULT_PERMISSIONS
    # super_user holds every catalog permission via the sentinel.
    assert perm in SUPER_USER_PERMISSIONS


def test_study_state_vocab_frozen() -> None:
    assert STUDY_STATES == _EXPECTED_STATES


def test_study_mode_vocab_frozen() -> None:
    assert STUDY_MODES == _EXPECTED_MODES
