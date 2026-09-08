"""Feature gating from the licence manifest (ADR-0019 slice 3, brief §C16).

`features.py` has always been the application-side primitive — every
gate point asks ``feature_enabled(settings.features, Feature.X)``. This
module supplies the *authoritative source* those settings come from in
production: the signed licence manifest, rather than the ``[features]``
block in ``matofx.toml``.

The switch happens at **one chokepoint** (:func:`apply_licence_features`,
called when the app is built), so no gate point changes and nothing else
has to know where the answer came from — which is brief §C16's rule that
the licence module is the single authority.

## When the manifest wins, and when it doesn't

Two deliberate fallbacks, both chosen so a licence problem can never
silently strip a customer's features:

- **No usable licence** → keep the TOML settings. A missing licence
  already costs the customer read-only mode; also blanking their feature
  list would be a second, more confusing penalty for the same lapse.
- **Manifest carries no ``features`` map (or an empty one)** → keep the
  TOML settings. §C16 ships every install with everything enabled, so an
  absent map means "this licence expresses no opinion", not "nothing is
  licensed". Reading it the other way would turn a sparse manifest into
  a total outage.

A **non-empty** map is authoritative: exactly the slugs set ``true``,
and nothing else.

Slugs the manifest names but this build doesn't know are **ignored**,
not rejected — a newer licence must keep working on an older install
(§C16 forwards-compatibility). That is the opposite of the rule for
``[features]`` in TOML, where an unknown slug is a typo worth failing
on; here it is a legitimately newer product.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from matofx.features import Feature

if TYPE_CHECKING:
    from matofx.config.models import FeatureSettings, Settings
    from matofx.licence.service import LicenceResolution

_log = logging.getLogger(__name__)


def effective_feature_settings(
    settings: Settings, resolution: LicenceResolution
) -> FeatureSettings:
    """The feature set this install should actually run with."""
    from matofx.config.models import FeatureSettings as _FeatureSettings  # noqa: PLC0415

    if resolution.status is None:
        return settings.features
    manifest_features = resolution.status.manifest.features
    if not manifest_features:
        return settings.features

    known = {f.value for f in Feature}
    requested = {slug for slug, enabled in manifest_features.items() if enabled}
    unknown = sorted(requested - known)
    if unknown:
        _log.info(
            "Licence enables feature(s) this build does not have: %s. Ignoring them; "
            "upgrade MATOFX to use them.",
            ", ".join(unknown),
        )
    return _FeatureSettings(enabled=sorted(requested & known))


def apply_licence_features(settings: Settings, resolution: LicenceResolution) -> Settings:
    """Return ``settings`` with its feature set resolved from the licence.

    Called once where the app is built, so every downstream
    ``feature_enabled(settings.features, …)`` reads the licensed set
    without a single gate point changing.
    """
    features = effective_feature_settings(settings, resolution)
    if features == settings.features:
        return settings
    return settings.model_copy(update={"features": features})
