"""UI-side install-feature checks.

The per-customer feature toggle (``features.parametric_studies`` et al)
is install configuration, not per-user state — so unlike
:mod:`matofx.ui.state` (login-cached, per-browser) these read the live
:class:`~matofx.config.Settings` off the mounted app.

Used to make a feature's nav entry + homepage card *absent* when the
flag is off (design §12), matching the API, which already 404s every
route for a disabled feature. Defaults to "off" if settings can't be
resolved — the safe direction (surface hidden, never wrongly shown).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from matofx.features import Feature, feature_enabled
from matofx.ui._holder import get_app

if TYPE_CHECKING:
    from matofx.config import Settings


def _settings() -> Settings:
    """The mounted app's settings, with the licensed features resolved live.

    Goes through the same helper the API dependency uses, so the sidebar
    and the routes it links to can never disagree about what this licence
    enables — which is what would happen if one re-read the licence file
    and the other kept startup's answer.
    """
    from matofx.licence.live import live_settings  # noqa: PLC0415

    return live_settings(get_app().state.settings)


def parametric_studies_enabled() -> bool:
    """``True`` when ``features.parametric_studies`` is on for this install."""
    with contextlib.suppress(Exception):
        settings = _settings()
        return feature_enabled(settings.features, Feature.PARAMETRIC_STUDIES)
    return False


def file_library_enabled() -> bool:
    """``True`` when ``features.file_library`` is on for this install."""
    with contextlib.suppress(Exception):
        settings = _settings()
        return feature_enabled(settings.features, Feature.FILE_LIBRARY)
    return False


def materials_database_enabled() -> bool:
    """``True`` when ``features.materials_database`` is on for this install."""
    with contextlib.suppress(Exception):
        settings = _settings()
        return feature_enabled(settings.features, Feature.MATERIALS_DATABASE)
    return False


def vv_tracking_enabled() -> bool:
    """``True`` when ``features.vv_tracking`` is on for this install."""
    with contextlib.suppress(Exception):
        settings = _settings()
        return feature_enabled(settings.features, Feature.VV_TRACKING)
    return False


def kpi_extraction_enabled() -> bool:
    """``True`` when ``features.kpi_extraction`` is on for this install."""
    with contextlib.suppress(Exception):
        settings = _settings()
        return feature_enabled(settings.features, Feature.KPI_EXTRACTION)
    return False


def surrogate_family_enabled() -> bool:
    """``True`` when ``features.surrogate_family`` is on for this install."""
    with contextlib.suppress(Exception):
        settings = _settings()
        return feature_enabled(settings.features, Feature.SURROGATE_FAMILY)
    return False


def optimisation_framework_enabled() -> bool:
    """``True`` when ``features.optimisation_framework`` is on for this install."""
    with contextlib.suppress(Exception):
        settings = _settings()
        return feature_enabled(settings.features, Feature.OPTIMISATION_FRAMEWORK)
    return False


def design_space_coverage_enabled() -> bool:
    """``True`` when ``features.design_space_coverage`` is on (ADR-0020 §16).

    A narrower gate than ``surrogate_family``: an install can have cohorts
    and surrogates without having bought coverage, in which case the panel
    is absent rather than empty — matching the API, which 404s the route.
    """
    with contextlib.suppress(Exception):
        settings = _settings()
        return feature_enabled(settings.features, Feature.DESIGN_SPACE_COVERAGE)
    return False
