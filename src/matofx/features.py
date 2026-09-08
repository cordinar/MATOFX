"""Per-customer feature flags.

Per memory rule 3 and §F31 of the brief, every non-essential feature
is gated by ``feature_enabled(settings, "name")`` so Funis can ship
the same artefact to every customer and toggle features at install
time via the signed licence manifest.

This module is the application-side primitive. The signed licence
manifest (which lands with the licence subsystem in Phase 2) is the
authoritative source in production; for dev / test, the
``[features]`` section in ``matofx.toml`` provides the same shape.

## Catalog discipline

Features form a **closed catalog**, mirroring the permissions design:

- Adding a new feature slug requires a code change — every gate point
  in the codebase consults a known slug, so a flag the code never
  checks can't gate anything.
- *Enabling* an existing slug for a given customer is install-time
  configuration: a row in their licence manifest, or an entry in
  ``[features].enabled`` for dev.
- Unknown slugs in configuration are rejected with a clear message,
  the same way unknown permissions are.

## Adding a new feature

1. Add a new member to :class:`Feature` below.
2. Document what it gates (one-line docstring on the enum member).
3. Add the gate point — ``if feature_enabled(settings.features, Feature.X): ...``.
4. Update ``etc/matofx.example.toml`` to mention the new slug under ``[features].enabled``.
5. Decide whether the feature is enabled for the pilot customer; if
   yes, add it to their licence manifest at ship time.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from matofx.config.models import FeatureSettings


class Feature(StrEnum):
    """Closed catalog of customer-toggleable features.

    Members are added as the corresponding feature lands. The enum
    starts deliberately small — speculative entries violate the
    "don't add what you don't use" rule. The first real entry below
    establishes the pattern; future Phase 1 / Phase 2 features extend
    the enum at the same time their gate points land.
    """

    MULTIPLE_LANDING_DASHBOARDS = "ui.multiple_landing_dashboards"
    """Multiple per-role landing dashboards (§I46 — behind feature flag)."""

    PARAMETRIC_STUDIES = "parametric_studies"
    """Parametric studies — fan a tool over a generated input matrix
    (§G39a / ADR-0001). Off ⇒ the nav entry, homepage card, API
    routes and cron are all absent (project-wide toggle rule, design
    §12). Slug matches the brief's ``[features.parametric_studies]``
    licence-manifest catalog entry."""

    MATERIALS_DATABASE = "materials_database"
    """Custom materials properties database (ADR-0007). Off ⇒ the
    ``/api/materials`` router 404s, the Materials nav entry / catalog
    UI are absent, and the ``material`` descriptor input type can't be
    picked. The data model + service exist regardless (a tool with a
    ``material`` field still validates at install); this flag gates the
    *surfaces*, per the project-wide toggle rule."""

    FILE_LIBRARY = "file_library"
    """Governed file library (ADR-0006). Off ⇒ the ``/api/library``
    router 404s, the Library nav entry / workspace are absent, and the
    "Pick from library" file-source mode is hidden in tool forms. The
    data model + service exist regardless; this flag gates the
    *surfaces*, per the project-wide toggle rule."""

    VV_TRACKING = "vv_tracking"
    """V&V (Verification & Validation) record tracking (ADR-0011). Off
    ⇒ the ``/api/vv`` router 404s, the V&V nav entry / catalog UI are
    absent, V&V badges across submit / Compare / lineage / KPI surfaces
    disappear, and the per-project ``vv_required_for_submit`` policy is
    a no-op. The data model + service exist regardless (the
    ``tool_version.bundle_hash`` cohort key is always written, since it
    has uses beyond V&V); this flag gates the *surfaces* per the
    project-wide toggle rule."""

    SURROGATE_FAMILY = "surrogate_family"
    """Surrogate family — job cohorts, surrogate models, field surrogates,
    and the model registry (ADR-0008). Off ⇒ the workspace-level
    ``/api/cohorts`` router 404s, the *Job Cohorts* page + cohort
    detail page are absent, and (in later phases) the Models catalogue,
    the field-output descriptor kind, and the surrogate-as-tool synthesis
    are all absent. The data model + ``CohortService`` exist regardless;
    this flag gates the *surfaces* per the project-wide toggle rule."""

    OPTIMISATION_FRAMEWORK = "optimisation_framework"
    """Optimisation framework — adaptive design-space studies (ADR-0009).
    Off ⇒ the ``/api/projects/{pid}/optimisation-studies`` +
    ``/api/optimisation-studies`` routers 404, the Studies *Optimisation*
    tab + create wizard + study detail page are absent, the strategy
    registry is hidden, and the runner daemon's optimisation-study scan
    is a no-op. The data model + ``OptimisationService`` exist regardless
    (the schema always migrates); this flag gates the *surfaces* per the
    project-wide toggle rule. The ``surrogate`` / ``surrogate_assisted_real``
    run modes additionally require ``surrogate_family`` on (the Model
    registry dependency)."""

    KPI_EXTRACTION = "kpi_extraction"
    """KPI extraction + per-tool results warehouse (ADR-0012). Off ⇒ the
    ``/api/tools/{alias}/kpis``, ``/results``, ``/results/timeseries``
    and ``/results/export.csv`` routes 404; the Results-warehouse nav
    entry + ``/results/{tool_alias}`` page are absent; the descriptor
    wizard hides the "Mark as KPI" affordance; the per-job HTML report
    skips its headline-KPI block. Descriptors that carry
    ``kpi = true`` still validate (the flag never blocks tool install
    so a customer can turn the flag on later without re-promoting);
    this flag gates the *surfaces* per the project-wide toggle rule.
    The post-completion extraction hook in ``runner.supervisor``
    short-circuits when the flag is off — no rows written, so toggling
    on later starts populating ``job_kpi_value`` from new completions.
    Back-fill of historical jobs is a one-shot admin action (slice 4)."""

    DESIGN_SPACE_COVERAGE = "design_space_coverage"
    """Design-space coverage — is this archive fit to learn from? (ADR-0020).
    Off ⇒ the coverage panel on cohort preview and the model detail page are
    absent, the "suggest runs to fill the gaps" affordance is hidden, and the
    predict-time ``support`` block is omitted from prediction responses. The
    engine (``matofx.studies.coverage``) and its stored reports exist
    regardless — a report computed while the flag was on stays readable, and
    a customer turning the flag on later starts seeing verdicts without a
    re-fit; this flag gates the *surfaces* per the project-wide toggle rule.
    Requires ``surrogate_family`` for the per-response half (the model
    registry dependency); the response-agnostic design pass stands alone."""


class UnknownFeatureError(Exception):
    """Raised when a feature slug not in :class:`Feature` is referenced."""

    def __init__(self, slug: str) -> None:
        """Construct with the offending slug."""
        super().__init__(
            f"{slug!r} is not a feature MATOFX knows about. "
            "Features are defined by the application; ask your supplier "
            "to add it in a future release if you need a new toggle."
        )
        self.slug = slug


def feature_enabled(settings: FeatureSettings, feature: Feature | str) -> bool:
    """Return ``True`` when ``feature`` is enabled for this install.

    The check is a simple membership test — no DB round-trip. Pass
    the loaded :class:`matofx.config.FeatureSettings` from the parent
    :class:`matofx.config.Settings`.

    Examples:
        >>> from matofx.features import Feature, feature_enabled
        >>> if feature_enabled(settings.features, Feature.MULTIPLE_LANDING_DASHBOARDS):
        ...     ...

    Unknown slugs raise :class:`UnknownFeatureError`. Callers should
    pass a :class:`Feature` enum value rather than a string when the
    feature is referenced from production code; the string overload
    exists for diagnostics and admin tooling.
    """
    slug = feature.value if isinstance(feature, Feature) else feature
    if slug not in {f.value for f in Feature}:
        raise UnknownFeatureError(slug)
    return slug in settings.enabled
