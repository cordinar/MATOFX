"""Process-wide cache of branding values for the UI chrome.

The chrome (``matofx.ui.pages._chrome``) is rendered synchronously on
every page (the per-page ``render_shell()`` call). The values it
needs — the saved app title, the header-icon basename, the favicon
basename — live in the ``runtime_setting`` table, which only async
SQLAlchemy can read. Awaiting per page would force every page
handler to ``await render_shell()`` (~25 call sites); instead we
keep a tiny module-level dict updated by two seams:

* At app startup (FastAPI lifespan) we run an initial refresh so the
  first request to the chrome sees the saved values.
* After every successful save / reset / image upload in
  :mod:`matofx.api.routers.general_settings`, the route refreshes
  the cache so the next page load reflects the change.

When the cache hasn't been populated yet (e.g. the import-only path
that mounts the UI from a unit test), readers fall back to the
catalog defaults via :data:`_DEFAULTS` — chrome always has something
sensible to render.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from matofx.config.general_service import load_general_setting_overrides, load_general_settings

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class BrandingSnapshot:
    """Tiny view of the branding-related General Settings rows.

    Carries the cleaned strings the chrome renders. Filenames are
    just basenames — the chrome composes the served URL itself, so
    the snapshot stays portable across mount layouts.

    ``*_version`` is a cache-bust token derived from the on-disk
    file's mtime at refresh time. Browsers cache favicons and inline
    images aggressively; without a token that changes per upload,
    a re-uploaded logo would keep showing the previous bytes until
    the user manually cleared cache.
    """

    app_display_title: str
    header_icon_basename: str | None
    header_icon_version: str | None
    favicon_basename: str | None
    favicon_version: str | None
    maintenance_banner_text: str
    maintenance_window_iso: str
    support_email_footer: str
    # The admin-set install default timezone (an IANA name) or None when
    # unset. None means "fall through to the env var / UTC" in the
    # formatter — distinct from an explicit "UTC" choice. Carried on this
    # snapshot to reuse its startup + on-save refresh seams.
    default_timezone: str | None


_DEFAULTS: BrandingSnapshot = BrandingSnapshot(
    app_display_title="MATOFX",
    header_icon_basename=None,
    header_icon_version=None,
    favicon_basename=None,
    favicon_version=None,
    maintenance_banner_text="",
    maintenance_window_iso="",
    support_email_footer="",
    default_timezone=None,
)


_current: BrandingSnapshot = _DEFAULTS


def get_branding() -> BrandingSnapshot:
    """Return the latest cached branding snapshot.

    Always safe to call: before the first refresh, returns
    :data:`_DEFAULTS` (which matches the catalog defaults), so the
    chrome renders the same shape it always has.
    """
    return _current


async def refresh_branding_cache(
    session: AsyncSession,
    *,
    icons_dir: Path | None = None,
) -> BrandingSnapshot:
    """Re-read the branding rows from the DB and publish a new snapshot.

    ``icons_dir`` is the configured branding upload root
    (``settings.branding.icons_dir``); when supplied, the file mtime
    of each saved image is folded into the snapshot as a cache-bust
    token so the chrome can append ``?v=<token>`` to its image URLs.
    Tests that don't care about cache-busting can omit it — the
    snapshot just carries ``None`` versions, and the chrome falls
    back to the basename.

    Returns the snapshot it published so callers can short-circuit a
    follow-up :func:`get_branding` call. Each call publishes a new
    :class:`BrandingSnapshot`; readers always see a coherent set
    (no torn read across two rows).
    """
    global _current  # noqa: PLW0603 — module-level cache pattern
    snapshot = await load_general_settings(session)
    # Override-only for the timezone: an unset row must fall through to
    # the env var / UTC in the formatter, not pin the catalog "UTC"
    # default over a deploy's env configuration.
    overrides = await load_general_setting_overrides(session)
    title = snapshot.get("app_display_title")
    header = snapshot.get("header_icon_path")
    favicon = snapshot.get("favicon_path")
    banner = snapshot.get("maintenance_banner_text")
    window = snapshot.get("maintenance_window_iso")
    support = snapshot.get("support_email_footer")
    tz_override = overrides.get("default_timezone")
    header_basename = str(header) if isinstance(header, str) and header else None
    favicon_basename = str(favicon) if isinstance(favicon, str) and favicon else None
    _current = BrandingSnapshot(
        app_display_title=str(title) if isinstance(title, str) and title else "MATOFX",
        header_icon_basename=header_basename,
        header_icon_version=_file_version(icons_dir, header_basename),
        favicon_basename=favicon_basename,
        favicon_version=_file_version(icons_dir, favicon_basename),
        maintenance_banner_text=str(banner) if isinstance(banner, str) else "",
        maintenance_window_iso=str(window) if isinstance(window, str) else "",
        support_email_footer=str(support) if isinstance(support, str) else "",
        default_timezone=str(tz_override) if isinstance(tz_override, str) and tz_override else None,
    )
    return _current


def _file_version(icons_dir: Path | None, basename: str | None) -> str | None:
    """Stringified mtime of ``<icons_dir>/<basename>`` for cache-bust.

    Returns ``None`` when either input is missing or the file isn't
    on disk yet — the chrome then falls back to the basename, which
    matches the previous behaviour.
    """
    if icons_dir is None or not basename:
        return None
    with contextlib.suppress(OSError):
        return str((icons_dir / basename).stat().st_mtime_ns)
    return None
