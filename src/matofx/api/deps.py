"""FastAPI dependency-injection seams.

Single source of truth for how the API gets:

- the active :class:`Settings` (loaded once at startup, stashed on
  ``app.state``);
- a fresh :class:`AsyncSession` per request (auto-committing the
  transaction on success, rolling back on exception);
- the configured services (one instance per process, cheap dataclasses);
- the authenticated user, when the route requires auth.

All routers consume these via ``Depends(...)`` rather than reaching
into ``app.state`` directly — keeps tests free to swap any layer with
``app.dependency_overrides``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.approvals import ApprovalService, make_approval_service
from matofx.audit import AuditWriter, ensure_chain_head
from matofx.config import Settings
from matofx.config.general_service import (
    GeneralSettingsService,
    make_general_settings_service,
)
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.favourites import FavouritesService, make_favourites_service
from matofx.hierarchy.service import (
    JobService,
    ProjectService,
    ScenarioService,
    make_job_service,
    make_project_service,
    make_scenario_service,
)
from matofx.library import LibraryService, make_library_service
from matofx.lineage import LineageService, make_lineage_service
from matofx.live_jobs import LiveJobsService, make_live_jobs_service
from matofx.materials import MaterialsService, make_materials_service
from matofx.migrate import (
    ExportService,
    ImportService,
    TrustedPeerService,
    make_export_service,
    make_import_service,
    make_trusted_peer_service,
)
from matofx.migrate.signing import KeyPair
from matofx.models import ModelService, make_model_service
from matofx.notify import (
    BroadcastService,
    Notifier,
    make_broadcast_service,
    make_notifier,
)
from matofx.runner.admin_service import (
    RuntimeSettingsService,
    make_runtime_settings_service,
)
from matofx.secrets import SecretsStore, make_secrets_store
from matofx.studies import (
    ResultsService,
    SeriesService,
    StudyService,
    make_results_service,
    make_series_service,
    make_study_service,
)
from matofx.studies.cohort import CohortService, make_cohort_service
from matofx.studies.optimisation import OptimisationService, make_optimisation_service
from matofx.summaries import SummaryService, make_summary_service
from matofx.tools.hosts_service import (
    AosHostsService,
    HpcClustersService,
    make_aos_hosts_service,
    make_hpc_clusters_service,
)
from matofx.tools.paths_service import (
    PythonEnvsService,
    StorageLocationsService,
    make_python_envs_service,
    make_storage_locations_service,
)
from matofx.tools.service import ToolService, make_tool_service
from matofx.tutorials.service import TutorialService, make_tutorial_service
from matofx.usage import (
    DigestService,
    UsageService,
    make_digest_service,
    make_usage_service,
)
from matofx.users.login_throttle import IpLoginThrottle
from matofx.users.orm import User
from matofx.users.passwords import make_password_hasher
from matofx.users.role_service import RoleService, make_role_service
from matofx.users.service import (
    SessionService,
    UserService,
    make_session_service,
    make_user_service,
)
from matofx.users.team_service import TeamService, make_team_service
from matofx.vv import VVService, make_vv_service
from matofx.workflows.graph_service import (
    WorkflowGraphService,
    make_workflow_graph_service,
)
from matofx.workflows.run_service import WorkflowRunService, make_workflow_run_service
from matofx.workflows.service import WorkflowService, make_workflow_service

# --- raw process-wide things ------------------------------------------


def get_settings(request: Request) -> Settings:
    """Return the :class:`Settings` for this request.

    The stored object is startup's, with one part resolved live: the
    **licensed feature set**, re-read whenever the licence file on disk
    changes. A licence issued mid-term to switch a feature on used to do
    nothing until the server was restarted, while the slot count from the
    same file applied immediately — see :mod:`matofx.licence.live`.

    Costs an ``os.stat`` per request in the steady state.
    """
    from matofx.licence.live import live_settings  # noqa: PLC0415

    return live_settings(request.app.state.settings)


def get_factory(request: Request) -> SessionFactory:
    """Return the :class:`SessionFactory` stashed on ``app.state``."""
    return request.app.state.factory  # type: ignore[no-any-return]


def get_audit(request: Request) -> AuditWriter:
    """Return the :class:`AuditWriter` stashed on ``app.state``."""
    return request.app.state.audit  # type: ignore[no-any-return]


def get_login_throttle(request: Request) -> IpLoginThrottle:
    """Return the process-wide per-IP login throttle from ``app.state``."""
    return request.app.state.login_throttle  # type: ignore[no-any-return]


# --- per-request session ----------------------------------------------


async def get_db(
    factory: Annotated[SessionFactory, Depends(get_factory)],
) -> AsyncIterator[AsyncSession]:
    """Yield a fresh DB session per request via :func:`session_scope`.

    On a clean exit the session commits; on any exception it rolls
    back. The audit chain head is ensured idempotently before the
    request body runs so any audited mutation lands on a valid chain.
    """
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        yield session


# --- service factories (cheap; constructed per request) ---------------


def get_user_service(
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> UserService:
    """Build a :class:`UserService` from the ambient settings + audit writer."""
    return make_user_service(settings.auth, audit, hasher=make_password_hasher(settings.auth))


def get_session_service(
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> SessionService:
    """Build a :class:`SessionService`."""
    return make_session_service(settings.auth, audit)


def get_project_service(audit: Annotated[AuditWriter, Depends(get_audit)]) -> ProjectService:
    """Build a :class:`ProjectService`."""
    return make_project_service(audit)


def get_scenario_service(audit: Annotated[AuditWriter, Depends(get_audit)]) -> ScenarioService:
    """Build a :class:`ScenarioService`."""
    return make_scenario_service(audit)


def get_job_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> JobService:
    """Build a :class:`JobService`.

    Threads ``Settings.environment`` through to the service so the
    staged working-dir path includes the env segment (``jobs/staging/…``,
    ``jobs/prod/…``, etc.). Stops dev / staging / prod folders piling
    up next to each other on a shared mount.
    """
    return make_job_service(audit, environment=settings.environment)


def get_tool_service(audit: Annotated[AuditWriter, Depends(get_audit)]) -> ToolService:
    """Build a :class:`ToolService`."""
    return make_tool_service(audit)


def get_materials_service(audit: Annotated[AuditWriter, Depends(get_audit)]) -> MaterialsService:
    """Build a :class:`MaterialsService` (ADR-0007)."""
    return make_materials_service(audit)


def get_vv_service(audit: Annotated[AuditWriter, Depends(get_audit)]) -> VVService:
    """Build a :class:`VVService` (ADR-0011)."""
    return make_vv_service(audit)


def get_library_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> LibraryService:
    """Build a :class:`LibraryService` (ADR-0006).

    Threads ``Settings.environment`` for the on-disk byte path segment
    (only used by create/add-version; the approvals dispatch needs none).
    """
    return make_library_service(audit, environment=settings.environment)


def get_workflow_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> WorkflowService:
    """Build a :class:`WorkflowService`."""
    return make_workflow_service(audit)


def get_workflow_run_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> WorkflowRunService:
    """Build a :class:`WorkflowRunService`."""
    return make_workflow_run_service(audit)


def get_workflow_graph_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> WorkflowGraphService:
    """Build a :class:`WorkflowGraphService` (read-only structure-graph assembly)."""
    return make_workflow_graph_service(audit)


def get_study_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> StudyService:
    """Build a :class:`StudyService`.

    Threads ``Settings.environment`` through (like
    :func:`get_job_service`) — ``StudyService.submit`` / ``retry_child``
    enqueue children via ``JobService.queue_for_runner``, which derives
    the staged working-dir path segment from the environment.
    """
    return make_study_service(audit, environment=settings.environment)


def get_results_service(
    study_service: Annotated[StudyService, Depends(get_study_service)],
) -> ResultsService:
    """Build a :class:`ResultsService` over the study service.

    The read side of the parametric_study_result store (2-PS-b0 /
    ADR-0001 §10.0). Delegates ACL to ``StudyService.get`` (project
    VIEW), so it picks up whatever environment ``get_study_service``
    is wired with.
    """
    return make_results_service(study_service)


def get_series_service(
    study_service: Annotated[StudyService, Depends(get_study_service)],
) -> SeriesService:
    """Build a :class:`SeriesService` (csv_plot series / aggregates, 2-PS-b1)."""
    return make_series_service(study_service)


def get_cohort_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> CohortService:
    """Build a :class:`CohortService` (job cohorts — ADR-0008 §3)."""
    return make_cohort_service(audit)


def get_optimisation_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> OptimisationService:
    """Build an :class:`OptimisationService` (optimisation studies — ADR-0009)."""
    return make_optimisation_service(audit)


def get_summary_service() -> SummaryService:
    """Build the (stateless) :class:`SummaryService` (auto-summaries)."""
    return make_summary_service()


def get_model_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ModelService:
    """Build a :class:`ModelService` (surrogate Model registry — ADR-0008 §5).

    Wired to the configured ``models.models_dir`` bundle root + the
    install environment, mirroring ``get_study_service``.
    """
    return make_model_service(
        audit, models_root=settings.models.models_dir, environment=settings.environment
    )


def get_tutorial_service(
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> TutorialService:
    """Build a :class:`TutorialService` wired to the configured tutorials root."""
    return make_tutorial_service(
        audit,
        tutorials_root=settings.tutorials.tutorials_dir,
        max_upload_bytes=settings.tutorials.max_upload_bytes,
    )


def get_runtime_settings_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> RuntimeSettingsService:
    """Build a :class:`RuntimeSettingsService`."""
    return make_runtime_settings_service(audit)


def get_general_settings_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> GeneralSettingsService:
    """Build a :class:`GeneralSettingsService`."""
    return make_general_settings_service(audit)


def get_favourites_service() -> FavouritesService:
    """Build a :class:`FavouritesService`."""
    return make_favourites_service()


def get_usage_service() -> UsageService:
    """Build a :class:`UsageService` (read-only; no audit / no settings)."""
    return make_usage_service()


def get_digest_service(
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> DigestService:
    """Build a :class:`DigestService` (emails the usage report on demand)."""
    return make_digest_service(audit, settings.email)


def get_live_jobs_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> LiveJobsService:
    """Build a :class:`LiveJobsService` (Live Jobs in-flight list + bulk cancel)."""
    return make_live_jobs_service(audit)


def get_lineage_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> LineageService:
    """Build a :class:`LineageService` (read-only provenance graph assembly)."""
    return make_lineage_service(audit)


def get_approval_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> ApprovalService:
    """Build an :class:`ApprovalService` (list / count / get for the inbox)."""
    return make_approval_service(audit)


def get_notifier(
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> Notifier:
    """Build the process notifier (EmailNotifier or NoopNotifier).

    ``EmailSettings.enabled=False`` → no-op, so API call sites can
    fire approval notifications unconditionally without a None-check.
    """
    return make_notifier(settings.email, audit)


def get_broadcast_service(
    settings: Annotated[Settings, Depends(get_settings)],
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> BroadcastService:
    """Build a :class:`BroadcastService`.

    Threads the process ``EmailSettings`` baseline (overlaid per-call
    with admin DB overrides) and ``branding.icons_dir`` — the same
    directory :class:`GeneralSettingsService.set_image` writes the
    saved header/footer template images into — so the broadcast can
    read them back for inline embedding.
    """
    return make_broadcast_service(audit, settings.email, settings.branding.icons_dir)


def get_role_service(audit: Annotated[AuditWriter, Depends(get_audit)]) -> RoleService:
    """Build a :class:`RoleService`."""
    return make_role_service(audit)


def get_team_service(audit: Annotated[AuditWriter, Depends(get_audit)]) -> TeamService:
    """Build a :class:`TeamService`."""
    return make_team_service(audit)


def get_storage_locations_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> StorageLocationsService:
    """Build a :class:`StorageLocationsService`."""
    return make_storage_locations_service(audit)


def get_python_envs_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> PythonEnvsService:
    """Build a :class:`PythonEnvsService`."""
    return make_python_envs_service(audit)


def get_aos_hosts_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> AosHostsService:
    """Build an :class:`AosHostsService`."""
    return make_aos_hosts_service(audit)


def get_hpc_clusters_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> HpcClustersService:
    """Build an :class:`HpcClustersService`."""
    return make_hpc_clusters_service(audit)


def get_secrets_store(
    settings: Annotated[Settings, Depends(get_settings)],
) -> SecretsStore:
    """Build the configured :class:`~matofx.secrets.SecretsStore`.

    Cheap to construct per request (the encrypted-file backend just
    re-builds a Fernet); used by the host/cluster admin endpoints to
    report whether each row's ``secret_ref`` is populated.
    """
    return make_secrets_store(settings.secrets)


def get_trusted_peer_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> TrustedPeerService:
    """Build a :class:`TrustedPeerService` for the migrate surface."""
    return make_trusted_peer_service(audit)


def get_export_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ExportService:
    """Build a :class:`ExportService` for the migrate surface."""
    return make_export_service(audit, workflow_icons_dir=settings.workflows.icons_dir)


def get_import_service(
    audit: Annotated[AuditWriter, Depends(get_audit)],
    trust_service: Annotated[TrustedPeerService, Depends(get_trusted_peer_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ImportService:
    """Build a :class:`ImportService` for the migrate surface.

    Imported tool bundles land under ``settings.tools.bundles_dir`` —
    the same directory locally-installed bundles use — so a cross-
    install import is byte-compatible with the rest of the tool
    catalog from the runner's perspective.
    """
    return make_import_service(
        audit,
        trust_service=trust_service,
        bundle_root=settings.tools.bundles_dir,
        workflow_icons_dir=settings.workflows.icons_dir,
    )


def get_install_key_pair(
    audit: Annotated[AuditWriter, Depends(get_audit)],
) -> KeyPair:
    """Return the install's Ed25519 key pair, reused from the audit writer.

    The migrate surface piggy-backs on the per-install audit signing
    key (per PROJECT_BRIEF.md §Q4 — "signed by the per-install Ed25519
    key"). When audit is configured without a key, this dep raises 503
    so the operator gets a clear "configure audit signing first"
    message rather than a generic crash.
    """
    if audit.signing_key is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "This install has no audit signing key configured. "
                "The migration feature reuses the audit key to sign "
                "exported bundles; configure 'audit.signing_key_path' "
                "in matofx.toml (or run `matofx rotate signing-key "
                "--init`) and restart the server."
            ),
        )
    return KeyPair.from_private_key(audit.signing_key)


# --- auth -------------------------------------------------------------


_BEARER_PREFIX = "Bearer "


async def get_current_user(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """Resolve the bearer token in ``Authorization`` to a live :class:`User`.

    Raises 401 when the header is missing / malformed; surfaces the
    domain auth errors for the rest (the registered exception handler
    maps them to 401 / 403).

    Also stashes the resolved session's id on ``request.state.session_id``.
    Routes that act *on sessions* need to know which one is the caller's —
    Admin → Active sessions must not offer to sign an administrator out
    of the page they are using (ADR-0019 slice 4). This is the only
    place that knows it, because it is the only place that resolves the
    bearer token to a row.
    """
    if not authorization or not authorization.startswith(_BEARER_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header. Send 'Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[len(_BEARER_PREFIX) :].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Empty bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Domain auth errors (SessionInvalidError / SessionExpiredError /
    # AccountDisabledError) propagate to the domain-error handler,
    # which maps them to 401 / 403.
    auth_session = await session_service.validate(db, token=token)
    request.state.session_id = auth_session.id
    user = await db.get(User, auth_session.user_id)
    if user is None:
        # Defence-in-depth — validate already checked, but guard the
        # narrow window where the user was deleted between validate
        # and this fetch.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session belongs to a user that no longer exists.",
        )
    return user


async def enforce_password_complexity(
    db: AsyncSession,
    plaintext: str,
) -> None:
    """Run the site's password-complexity policy against ``plaintext``.

    Reads the catalog snapshot (live; admin saves take effect on next
    call) and raises :class:`matofx.users.errors.WeakPasswordError` on
    any failure. Callers run this at the boundary where a user submits
    a password — sign-up, change-password, admin reset — keeping the
    data-layer :class:`UserService` free of site-policy concerns.

    Returns ``None`` on success.
    """
    # Lazy imports to keep the dependency footprint clean for headless
    # API tests that don't exercise password mutation paths.
    from matofx.config.general_service import load_general_settings  # noqa: PLC0415
    from matofx.users.errors import WeakPasswordError  # noqa: PLC0415
    from matofx.users.passwords import validate_password_complexity  # noqa: PLC0415

    snapshot = await load_general_settings(db)
    reason = validate_password_complexity(plaintext, snapshot)
    if reason is not None:
        raise WeakPasswordError(reason)


async def get_current_user_role(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> str:
    """Resolve ``current_user.role_id`` to the role's name string.

    Returned as ``str`` (rather than the ``UserRole`` Literal alias)
    to allow custom roles a customer may have added later. Most
    consumers compare via :func:`matofx.users.roles.has_role_at_least`
    which accepts any ``str``; unknown names are treated as below
    every privilege threshold.
    """
    from matofx.users.role_orm import Role  # noqa: PLC0415

    role = await db.get(Role, current_user.role_id)
    if role is None:
        # Shouldn't happen — role_id FK is RESTRICT — but stay safe.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account's role no longer exists.",
        )
    return role.name
