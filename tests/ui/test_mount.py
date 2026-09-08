"""Smoke tests for mounting the NiceGUI shell onto the FastAPI app.

These don't drive browser interactions (NiceGUI's testing harness is
heavyweight and flaky on Windows) — instead they prove:

- All page modules import without errors (decorators fire).
- ``mount_ui`` registers the shell on a real FastAPI app without raising.
- ``make_app(..., mount_ui_shell=True)`` produces an app whose route
  table includes ``/ui/login`` (the only public-without-auth UI page).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from matofx.config import AuditSettings, AuthSettings, DbSettings, LogSettings, Settings


@pytest.fixture
def settings(tmp_path):  # type: ignore[no-untyped-def]
    return Settings(
        environment="test",
        log=LogSettings(),
        db=DbSettings.from_url(f"sqlite:///{tmp_path / 'mount.db'}", dialect="sqlite"),
        audit=AuditSettings(),
        auth=AuthSettings(
            argon2_time_cost=1,
            argon2_memory_cost_kib=8192,
            argon2_parallelism=1,
            password_min_length=8,
        ),
    )


@pytest.mark.parametrize(
    "module",
    [
        "matofx.ui.pages._chrome",
        "matofx.ui.pages._stub",
        "matofx.ui.pages.login",
        "matofx.ui.pages.home",
        "matofx.ui.pages.dashboard",
        "matofx.ui.pages.my_jobs",
        "matofx.ui.pages.compare",
        "matofx.ui.pages.project",
        "matofx.ui.pages.scenario",
        "matofx.ui.pages.submit",
        "matofx.ui.pages.job",
        "matofx.ui.pages.account",
        "matofx.ui.pages.admin",
        "matofx.ui.pages.admin_users",
        "matofx.ui.pages.admin_teams",
        "matofx.ui.pages.admin_tools",
        "matofx.ui.pages.admin_usage",
        "matofx.ui.pages.demo",
    ],
)
def test_page_module_imports(module: str) -> None:
    """Every page module imports cleanly (registers its @ui.page route)."""
    importlib.import_module(module)


def test_resolve_storage_secret_prefers_override() -> None:
    """An explicit override wins over settings (used by tests)."""
    from matofx.ui.app import _resolve_storage_secret  # noqa: PLC0415

    assert _resolve_storage_secret(None, "explicit-secret") == "explicit-secret"


def test_resolve_storage_secret_reads_settings(settings: Settings) -> None:
    """A configured ``ui_session_secret`` is used so cookies survive restart."""
    from pydantic import SecretStr  # noqa: PLC0415

    from matofx.ui.app import _resolve_storage_secret  # noqa: PLC0415

    configured = settings.model_copy(
        update={"auth": settings.auth.model_copy(update={"ui_session_secret": SecretStr("stable")})}
    )
    assert _resolve_storage_secret(configured, None) == "stable"


def test_resolve_storage_secret_falls_back_to_ephemeral(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """With no configured secret, a per-process token is generated and warned about."""
    from matofx.ui.app import _resolve_storage_secret  # noqa: PLC0415

    with caplog.at_level("WARNING"):
        secret = _resolve_storage_secret(settings, None)
    assert secret  # non-empty random token
    assert any("ui_session_secret" in r.message for r in caplog.records)


def test_make_app_with_ui_shell_registers_login(settings: Settings) -> None:
    """``make_app(..., mount_ui_shell=True)`` exposes the /ui/login route."""
    from matofx.api.app import make_app  # noqa: PLC0415

    app = make_app(settings, mount_ui_shell=True)
    paths = [getattr(r, "path", "") for r in app.routes]
    # NiceGUI mounts under /ui as a sub-app; the path mounts are
    # composed dynamically. We at least assert the mount point exists.
    assert any(str(p).startswith("/ui") for p in paths), (
        f"expected a /ui route to be registered; got {paths}"
    )


def test_no_ui_prefix_in_page_or_navigate_paths() -> None:
    """Catch the ``/ui`` mount-prefix bug at the source level.

    NiceGUI's ``mount_path="/ui"`` prefixes every ``@ui.page("/...")``
    AND every ``ui.navigate.to("/...")``. Including ``/ui`` in either
    argument double-prefixes (``/ui/ui/...``) and 404s.

    Two real bugs landed by hand by getting this wrong:
    1. Page decorators included ``/ui`` → no page registered at the
       expected URL → NiceGUI 404.
    2. ``ui.navigate.to("/ui/dashboard")`` → URL became
       ``/ui/ui/dashboard`` → NiceGUI 404 after login.

    This test scans the page modules and refuses any literal
    ``"/ui/..."`` or ``f"/ui/..."`` argument inside ``@ui.page(...)``
    or ``ui.navigate.to(...)``. The ``ui.link(target="/api/...")``
    pattern is allowed because it's an HTML anchor href, not a
    NiceGUI client-router navigation.
    """
    import re  # noqa: PLC0415

    pages_dir = Path(__file__).resolve().parents[2] / "src" / "matofx" / "ui" / "pages"
    page_pattern = re.compile(r'@ui\.page\(\s*[\'"]/ui/')
    nav_pattern = re.compile(r'ui\.navigate\.to\(\s*f?[\'"]/ui/')
    offences: list[str] = []
    for py_file in pages_dir.glob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for match in page_pattern.finditer(text):
            offences.append(f"{py_file.name}: @ui.page with /ui prefix at offset {match.start()}")
        for match in nav_pattern.finditer(text):
            offences.append(
                f"{py_file.name}: ui.navigate.to with /ui prefix at offset {match.start()}"
            )
    assert not offences, "Found mount-prefix bugs:\n  " + "\n  ".join(offences)


# NB: NiceGUI's process-global state means we can only build the
# mounted app ONCE per Python process — a second call to
# make_app(mount_ui_shell=True) raises "Cannot add middleware after
# an application has started". So all UI URL probes share one app +
# one client below.
#
# Only sync ``@ui.page`` handlers are probed here. Async handlers
# (home, dashboard, my_jobs, account, project, scenario, submit, job)
# need NiceGUI's event loop fully initialised; under the ASGI test
# transport it isn't, so calling them would crash inside
# ``nicegui.background_tasks.create``. Those pages are exercised
# end-to-end via the manual demo run + via the api_client tests
# which prove the underlying API endpoints work.
_SYNC_SIDEBAR_URLS: tuple[str, ...] = (
    "/ui/login",
    # /ui/compare became async with the Compare workspace port — it
    # awaits the visible-jobs and tool-detail endpoints during page
    # render. Async pages aren't probable under the ASGI test
    # transport (NiceGUI's background_tasks.create asserts the loop
    # is up), so it lives alongside /ui/admin/users etc. and is
    # exercised through tests/ui/compare/* + the lifecycle test in
    # tests/api/test_jobs.py.
    # /ui/admin/users, /ui/admin/teams, /ui/admin/tools, /ui/admin/paths,
    # /ui/admin/explore-db, /ui/admin/workflows are async (real CRUD
    # / explorer pages) — exercised via tests/api/test_admin.py +
    # tests/api/test_admin_paths.py + tests/api/test_admin_db_explorer.py
    # which prove the underlying endpoints work end-to-end.
    # /ui/my-workflows is async too — exercised via
    # tests/api/test_custom_workflows_api.py.
    # /ui/admin/usage became async with the Usage Statistics workspace
    # (it awaits usage filters / overview during render), so like the
    # other async pages it can't be probed under the ASGI test transport
    # — its endpoints are exercised via the usage api_client tests and
    # the manual demo run.
    # The same page with the sign-out reason the dashboard chrome sends
    # when it signs an idle user out — proves the extra query param is
    # accepted (NiceGUI maps it onto the handler's ``reason`` argument)
    # and that the notice actually renders.
    "/ui/login?reason=idle",
    "/ui/admin/import",
    "/ui/demo/cad",
    "/ui/demo/gltf",
    "/ui/demo/web",
    "/ui/demo/pdf",
)


@pytest.mark.asyncio
async def test_every_sync_sidebar_url_serves_200(settings: Settings) -> None:
    """Every sync sidebar URL is a registered NiceGUI page.

    Catches missing @ui.page decorators, stale typos in the sidebar's
    nav target list, and the /ui-prefix bug class.
    """
    import httpx  # noqa: PLC0415
    from httpx import ASGITransport  # noqa: PLC0415

    from matofx.api.app import make_app  # noqa: PLC0415

    app = make_app(settings, mount_ui_shell=True)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        results = []
        for url in _SYNC_SIDEBAR_URLS:
            response = await c.get(url)
            results.append((url, response.status_code, response.text))

    bad = [(url, code) for url, code, _ in results if code != 200]
    assert not bad, f"non-200 responses: {bad}"

    # Specific content check on the login page so the "200 from a 404 page"
    # failure mode would still flag. The card heading + button + page title
    # all read "Sign in" — a string a 404/error body wouldn't carry.
    login_body = next(text for url, _, text in results if url == "/ui/login")
    assert "Sign in" in login_body
    # No ?reason= → no sign-out notice; with one → the explanation is on
    # the page (a toast can't survive the navigation that gets them here).
    assert "been inactive" not in login_body
    idle_body = next(text for url, _, text in results if url == "/ui/login?reason=idle")
    assert "been inactive" in idle_body
