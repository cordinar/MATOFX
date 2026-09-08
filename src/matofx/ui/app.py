"""NiceGUI app factory + mount.

The matofx UI is mounted onto the matofx FastAPI app via
:func:`nicegui.ui.run_with`, so a single ``matofx-api`` process
serves both ``/api/*`` (REST) and ``/ui/*`` (browser pages).

The page modules under :mod:`matofx.ui.pages` register their routes
at import time, so importing them here is what wires the UI in.
"""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING, Any

from nicegui import ui

from matofx.ui._holder import set_app

if TYPE_CHECKING:
    from fastapi import FastAPI

    from matofx.config import Settings

# Import the page modules so their @ui.page decorators fire.
from matofx.ui.pages import (  # noqa: F401  -- import-for-side-effect
    account,
    admin,
    admin_audit,
    admin_compliance,
    admin_custom_workflows,
    admin_explore_db,
    admin_general_settings,
    admin_lineage,
    admin_migrate,
    admin_paths,
    admin_project_metadata,
    admin_send_notifications,
    admin_sessions,
    admin_teams,
    admin_tool_edit,
    admin_tool_new,
    admin_tools,
    admin_tutorials,
    admin_usage,
    admin_user_edit,
    admin_user_new,
    admin_users,
    admin_vv_records,
    admin_workflows,
    approvals,
    cohorts,
    compare,
    dashboard,
    demo,
    help,
    home,
    job,
    job_search,
    library,
    login,
    materials,
    models,
    my_jobs,
    my_workflows,
    optimisation,
    project,
    results,
    scenario,
    studies,
    submit,
    tutorials,
    vv_records,
    workflow_run,
)

_log = logging.getLogger(__name__)


def mount_ui(
    fastapi_app: FastAPI,
    *,
    settings: Settings | None = None,
    storage_secret: str | None = None,
) -> None:
    """Mount the NiceGUI shell on ``fastapi_app`` under ``/ui``.

    Args:
        fastapi_app: The :class:`fastapi.FastAPI` instance returned
            by :func:`matofx.api.app.make_app`.
        settings: Validated app settings. The UI session cookie's signing
            secret and ``Secure`` / ``SameSite`` flags are read from
            ``settings.auth`` (``ui_session_secret`` /
            ``ui_session_cookie_secure`` / ``ui_session_cookie_same_site``).
        storage_secret: Explicit override for the cookie-signing secret;
            takes precedence over ``settings``. Mainly for tests.

    The session cookie (NiceGUI's ``app.storage.user``) carries the
    bearer token, so a *stable* secret matters: an unset secret is
    randomised per process, which logs everyone out on restart and breaks
    signature validation across workers. Configure ``ui_session_secret``
    in production. ``HttpOnly`` is always on (Starlette's
    ``SessionMiddleware``); ``Secure`` / ``SameSite`` are pinned from
    settings.
    """
    secret = _resolve_storage_secret(settings, storage_secret)
    session_kwargs: dict[str, Any] = {
        "https_only": settings.auth.ui_session_cookie_secure if settings is not None else False,
        "same_site": settings.auth.ui_session_cookie_same_site if settings is not None else "lax",
    }

    set_app(fastapi_app)
    # Demo group static-asset mount. Has to happen on the OUTER
    # FastAPI app (not NiceGUI's sub-app) so the page handlers can
    # use absolute ``/_demo-asset/...`` URLs that bypass the ``/ui``
    # mount path. Called eagerly here — not via NiceGUI's
    # ``on_startup`` hook, because we hit a timing problem where that
    # hook didn't fire under the matofx app's lifespan and the demo
    # PDFs / GLTF files all 404'd. Direct call at mount time
    # guarantees the route is alive before the first request.
    from matofx.ui.pages._demo_assets import (  # noqa: PLC0415
        mount_demo_static_route,
    )

    mount_demo_static_route(fastapi_app)

    # Packaged brand assets (the default login logo). Mounted on the
    # outer app for the same reason as the demo assets — absolute
    # ``/matofx-assets/...`` URLs that don't depend on the ``/ui`` mount.
    from matofx.ui.static_assets import mount_static_assets  # noqa: PLC0415

    mount_static_assets(fastapi_app)

    ui.run_with(
        fastapi_app,
        mount_path="/ui",
        title="MATOFX",
        storage_secret=secret,
        session_middleware_kwargs=session_kwargs,
        favicon=None,
        # NiceGUI's default reconnect_timeout is 3.0s: if the websocket drops
        # for longer, the server purges the client and the browser is forced to
        # reload (see nicegui.js finishHandshake / try_reconnect). The UI and
        # API share one event loop, so a brief stall during heavy client-side
        # plot rendering or a synchronous surrogate fit can exceed 3s and, on a
        # page that auto-renders, turns into an endless reload loop. 30s gives a
        # comfortable margin so a short stall resumes the same session instead
        # of reloading. (Root cause of the parametric-studies profiler
        # "renders → refreshes" loop, 2026-07-04.)
        reconnect_timeout=30.0,
    )


def _resolve_storage_secret(settings: Settings | None, override: str | None) -> str:
    """Pick the cookie-signing secret: explicit override → settings → ephemeral.

    Falling through to an ephemeral per-process secret is fine for local
    dev but logs everyone out on restart, so warn loudly when it happens
    on a configured install.
    """
    if override:
        return override
    if settings is not None and settings.auth.ui_session_secret is not None:
        return settings.auth.ui_session_secret.get_secret_value()
    _log.warning(
        "No ui_session_secret configured — signing the UI session cookie with an "
        "ephemeral per-process secret. Sessions will be dropped on restart and won't "
        "validate across workers. Set [auth].ui_session_secret in production."
    )
    return secrets.token_urlsafe(32)
