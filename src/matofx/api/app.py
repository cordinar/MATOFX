"""FastAPI app factory.

Constructs a fully wired :class:`fastapi.FastAPI` instance from a
:class:`matofx.config.Settings`. The app holds:

- the loaded ``Settings`` (``app.state.settings``),
- the engine + session factory (``app.state.factory``; the engine
  itself is closed by the lifespan when the app shuts down),
- a single :class:`AuditWriter` (``app.state.audit``).

Routers consume these via the dependency-injection seams in
:mod:`matofx.api.deps` so tests can swap any of them with
``app.dependency_overrides``.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from matofx.api.errors import install_exception_handlers
from matofx.api.readonly import install_readonly_middleware
from matofx.api.routers import (
    admin,
    admin_db_explorer,
    admin_metadata,
    approvals,
    auth,
    cohorts,
    favourites,
    general_settings,
    home,
    jobs,
    kpi,
    library,
    lineage,
    live_jobs,
    materials,
    migrate,
    models,
    notifications,
    optimisation,
    projects,
    reports,
    studies,
    summaries,
    tools,
    tutorials,
    usage,
    vv,
    workflows,
)
from matofx.audit.writer import make_audit_writer
from matofx.db import make_engine, make_session_factory

if TYPE_CHECKING:
    from matofx.config import Settings


_log = logging.getLogger(__name__)


def make_app(  # noqa: PLR0915 — app factory: a flat sequence of router registrations.
    settings: Settings, *, mount_ui_shell: bool = False
) -> FastAPI:
    """Build the API app from a validated :class:`Settings`.

    Args:
        settings: Validated app settings.
        mount_ui_shell: When ``True``, mount the NiceGUI browser shell
            on ``/ui``. The ``matofx-api`` console script defaults to
            ``True``; tests against the API surface alone leave it
            ``False`` so they don't pull NiceGUI's extra wiring.
    """
    # Licence gate, before anything is wired (ADR-0019 slice 3). A
    # tampered / untrusted / wrong-machine licence raises here and the
    # process never serves a request; a *missing* or expired one only
    # logs, because §C16 forbids a lapsed renewal taking a site down.
    # Feature gating then reads the manifest instead of [features], at
    # this one chokepoint — every `feature_enabled(settings.features, …)`
    # call site downstream is unchanged.
    from matofx.licence import apply_licence_features, check_licence_at_startup  # noqa: PLC0415

    licence_resolution = check_licence_at_startup(settings)
    settings = apply_licence_features(settings, licence_resolution)

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.db)
        app.state.settings = settings
        app.state.audit = make_audit_writer(settings.audit)
        app.state.factory = make_session_factory(engine)
        app.state.licence = licence_resolution
        # Populate the UI chrome's branding cache so the first page
        # render sees the operator's saved title / header icon / favicon
        # rather than the catalog defaults. Best-effort: a failure here
        # leaves the cache on its baked-in defaults and never blocks
        # startup.
        if mount_ui_shell:
            try:
                from matofx.db import session_scope  # noqa: PLC0415
                from matofx.ui.branding_cache import refresh_branding_cache  # noqa: PLC0415

                async with session_scope(app.state.factory) as session:
                    await refresh_branding_cache(session, icons_dir=settings.branding.icons_dir)
            except Exception:  # noqa: S110 — best-effort branding warm-up must not crash boot
                pass
        # Chain integrity at boot (ADR-0019 §C14). The full walk is the
        # runner's hourly job; this is the bounded head-window check, and
        # it exists because an install whose runner never starts was
        # otherwise never checking its chain at all. A failure here does
        # not stop the process — it records the failure, which puts the
        # site into read-only mode with a banner a Super User can act on.
        # Refusing to boot would remove the only surface for doing so.
        # Is this licence even for this install? (ADR-0019 §A3.) The
        # manifest has always carried an install_id and nothing compared
        # it, so a copied licence file ran a second install happily. The
        # check needs the database, which `check_licence_at_startup` does
        # not have — so it lands here, before the first request is
        # served. Deliberately *not* caught: a licence issued for another
        # install is the same class of problem as one issued for another
        # machine, which already refuses to serve.
        from matofx.db import session_scope  # noqa: PLC0415
        from matofx.licence import verify_install_binding  # noqa: PLC0415

        async with session_scope(app.state.factory) as session:
            await verify_install_binding(session, licence_resolution)
            # Cache this install's own id for the request path. Startup is
            # the only place that binding was ever checked, so a licence
            # issued for *another* install could be copied over the
            # configured file on a **running** server and its slots and
            # features applied until somebody restarted — see
            # `matofx.api.readonly`. The id is immutable, so caching it
            # keeps that check a string comparison rather than a
            # per-request query.
            from matofx.compliance.identity import (  # noqa: PLC0415
                ensure_install_identity,
            )

            identity = await ensure_install_identity(session)
            app.state.install_id = identity.install_id

        try:
            from matofx.compliance.integrity import verify_recent  # noqa: PLC0415

            async with session_scope(app.state.factory) as session:
                await verify_recent(session, settings=settings, audit=app.state.audit)
        except Exception:
            _log.exception("Startup integrity check could not run")
        try:
            yield
        finally:
            # The job-state notifier holds one pooled SMTP connection open
            # between sends (relays rate-limit logins); close it so shutdown
            # does not leave a socket to the relay dangling.
            from matofx.notify import close_smtp_pool  # noqa: PLC0415

            with contextlib.suppress(Exception):
                await close_smtp_pool()
            await engine.dispose()

    app = FastAPI(
        title="MATOFX API",
        version="1.0.0",
        description=(
            "REST surface over the MATOFX SPDM hierarchy + tools + jobs. "
            "Bearer-token auth via /api/auth/login."
        ),
        lifespan=_lifespan,
    )

    # Per-IP login throttle is process-wide stateful (not per-request), so
    # it lives on app.state alongside settings/factory/audit. Built eagerly
    # here (not only in the lifespan) so it's present even when a test
    # harness wires app.state directly without running the lifespan.
    from matofx.users.login_throttle import make_login_throttle  # noqa: PLC0415

    app.state.login_throttle = make_login_throttle(settings.auth)

    # Read-only gate (ADR-0019 slice 6). Registered before the routers
    # so it wraps every one of them, including any added later.
    install_readonly_middleware(app)
    install_exception_handlers(app)
    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    app.include_router(tools.router, prefix="/api/tools", tags=["tools"])
    app.include_router(projects.router, prefix="/api", tags=["projects"])
    app.include_router(jobs.router, prefix="/api", tags=["jobs"])
    app.include_router(home.router, prefix="/api", tags=["home"])
    app.include_router(admin.router, prefix="/api", tags=["admin"])
    app.include_router(admin_db_explorer.router, prefix="/api", tags=["admin"])
    app.include_router(admin_metadata.router, prefix="/api", tags=["admin"])
    app.include_router(general_settings.router, prefix="/api", tags=["admin"])
    app.include_router(notifications.router, prefix="/api", tags=["notifications"])
    app.include_router(tutorials.router, prefix="/api", tags=["tutorials"])
    app.include_router(workflows.router, prefix="/api", tags=["workflows"])
    app.include_router(studies.router, prefix="/api", tags=["studies"])
    app.include_router(optimisation.router, prefix="/api", tags=["optimisation"])
    app.include_router(cohorts.router, prefix="/api", tags=["cohorts"])
    app.include_router(models.router, prefix="/api", tags=["models"])
    app.include_router(materials.router, prefix="/api", tags=["materials"])
    app.include_router(library.router, prefix="/api", tags=["library"])
    app.include_router(favourites.router, prefix="/api", tags=["favourites"])
    app.include_router(approvals.router, prefix="/api", tags=["approvals"])
    app.include_router(migrate.router, prefix="/api", tags=["migrate"])
    app.include_router(reports.router, prefix="/api", tags=["reports"])
    app.include_router(usage.router, prefix="/api", tags=["usage"])
    app.include_router(live_jobs.router, prefix="/api", tags=["live_jobs"])
    app.include_router(lineage.router, prefix="/api", tags=["lineage"])
    app.include_router(vv.router, prefix="/api", tags=["vv"])
    app.include_router(kpi.router, prefix="/api", tags=["kpi"])
    app.include_router(summaries.router, prefix="/api", tags=["summaries"])
    if mount_ui_shell:
        # Lazy import — keeps the API-only path free of the NiceGUI
        # dependency tree (handy for headless / CLI / test contexts).
        from matofx.ui import mount_ui  # noqa: PLC0415

        mount_ui(app, settings=settings)
    return app
