"""Shared chrome — top header + persistent left sidebar.

Mirrors the legacy MATOFX shell: a teal banner across the top with the
logo + user identity + logout, plus a left drawer with grouped nav
items (Dashboard / Admin Space / My Account / Demo). The Admin Space
group is shown only to users whose role is in
:data:`matofx.ui.state._ADMIN_ROLES`.

Pages call :func:`render_shell` at the top of their builder. It puts
both the header and the drawer in place, then returns control to the
page to render its body inside the main content area. The drawer can
be collapsed / re-opened from the hamburger button on the header.

The shell is theme-aware: the user's stored ``theme_preference``
(``light`` or ``dark``) drives the body class and a set of CSS
variables shipped from :mod:`matofx.ui.theme`. Toggling the theme
from the Account page flips ``body--dark``, which in turn re-points
the variables — every panel re-tints live without a page reload.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import SupportsInt, cast
from urllib.parse import quote

from nicegui import context, ui

from matofx.ui._background import spawn
from matofx.ui._features import (
    file_library_enabled,
    kpi_extraction_enabled,
    materials_database_enabled,
    optimisation_framework_enabled,
    parametric_studies_enabled,
    surrogate_family_enabled,
    vv_tracking_enabled,
)
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError, set_unauthenticated_hook
from matofx.ui.branding_cache import get_branding
from matofx.ui.state import (
    clear_login,
    current_first_name,
    current_idle_timeout_minutes,
    current_is_approver,
    current_locale,
    current_sidebar_pinned,
    current_sidebar_width,
    current_theme,
    current_timezone,
    current_token,
    current_username,
    has_permission,
    is_admin,
    is_logged_in,
    is_sidebar_group_collapsed,
    is_super_user,
    store_sidebar_group_collapsed,
    store_sidebar_pinned,
    store_sidebar_width,
    take_breakglass_notice,
)
from matofx.ui.theme import DARK_PALETTE, LIGHT_PALETTE


@dataclass(frozen=True)
class _NavItem:
    """One sidebar link plus the gates that decide whether it renders.

    Capitalisation convention for every sidebar label: sentence case
    (only the first letter capitalised) — except the keywords ``Job``,
    ``Jobs``, ``Workflow``, ``Workflows``, ``User`` and acronyms
    (``CAD``, ``3D``, ``PDF``, ``V&V``), which stay capitalised
    wherever they appear.
    """

    label: str
    icon: str
    target: str
    """Mount-relative path the click navigates to (no ``/ui`` prefix)."""
    su: bool = False
    """Admin-tier entry — visible to Administrator + Super User only,
    marked with a **person-with-gear** icon (home-redesign §3). When
    ``super_tier`` is also set the marker becomes a **shield** instead
    (see below)."""
    super_tier: bool = False
    """Marks an ``su`` entry as **Super-User-only**: visible to the Super
    User role alone (hidden from Administrators) and marked with a
    **shield** rather than the person-with-gear."""
    approver_only: bool = False
    """Shown only to users carrying the approver flag (the Approvals
    entry; §3 footnote 1)."""
    feature: str | None = None
    """Name of a feature-flag predicate in :data:`_FEATURE_FLAGS`; the
    entry is absent when the flag is off (§3 'Feature flag' column)."""
    permission: str | tuple[str, ...] | None = None
    """Optional permission slug(s) additionally required to see the entry.

    A single slug requires exactly that permission; a tuple requires
    **any one** of the slugs. Currently unused — the studies + governed
    surfaces are feature-gated only (a user without the create permission
    still sees the item but gets a view-only catalogue, each page hiding
    its own create UI). Retained for any future per-permission nav gate."""


@dataclass(frozen=True)
class _NavGroup:
    """One collapsible sidebar group: a heading + its items + gates."""

    label: str
    items: tuple[_NavItem, ...]
    su_group: bool = False
    """Whole group is Super-User-only (the Admin group); the heading
    carries a padlock and the group is ordered to the bottom of the
    rail (§3)."""
    open_by_default: bool = False


# Feature-flag predicates referenced by ``_NavItem.feature``. Resolved
# at render time so a per-customer toggle hides the entry (and the API
# 404s the route) without a code change.
_FEATURE_FLAGS: dict[str, Callable[[], bool]] = {
    "parametric_studies": parametric_studies_enabled,
    "optimisation_framework": optimisation_framework_enabled,
    "surrogate_family": surrogate_family_enabled,
    "file_library": file_library_enabled,
    "materials_database": materials_database_enabled,
    "vv_tracking": vv_tracking_enabled,
    "kpi_extraction": kpi_extraction_enabled,
}


# Thin line-art icons ported verbatim from the locked mockup
# (``home_designs/home_v3.html``) so the rail matches it exactly, with no
# dependency on an external icon webfont. Each value is the inner SVG
# markup; :func:`_nav_icon_html` wraps it in a stroked ``<svg>``.
_NAV_SVG: dict[str, str] = {
    "home": '<path d="M3 10.5 12 3l9 7.5M5 9.5V21h14V9.5"/>',
    "folder": (
        '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'
    ),
    # A console window with a ``>`` prompt + cursor underscore — the
    # "execute / run" glyph for the Execution environments page.
    "console": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/>',
    "my_jobs": '<path d="M9 11l3 3 8-8M20 12v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9"/>',
    "workflow": (
        '<circle cx="6" cy="6" r="2.4"/><circle cx="18" cy="6" r="2.4"/>'
        '<circle cx="12" cy="18" r="2.4"/>'
        '<path d="M6 8.4v3a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2v-3M12 13.4v2.2"/>'
    ),
    "inspector": (
        '<path d="M4 6h12M4 10h8M4 14h6"/><circle cx="17" cy="15" r="3.5"/>'
        '<path d="m20 18 1.5 1.5"/>'
    ),
    "metadata": (
        '<path d="M3 7h18M3 12h18M3 17h18"/><circle cx="7" cy="7" r="1"/>'
        '<circle cx="13" cy="12" r="1"/><circle cx="9" cy="17" r="1"/>'
    ),
    # Material-style "compare": a split panel — rounded square, centre
    # divider, right half hatched to echo the filled page (matches the
    # in-page Compare tab icon).
    "compare": (
        '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M12 4v16"/>'
        '<path d="M13 18l5-5M13 14l5-5M13 10l5-5"/>'
    ),
    "cohorts": '<path d="M3 3h6v6H3zM15 3h6v6h-6zM3 15h6v6H3zM15 15h6v6h-6z"/>',
    "bars": '<path d="M4 19V5M4 19h16M8 16V9m4 7V6m4 10v-5"/>',
    "target": '<circle cx="12" cy="12" r="3"/><path d="M12 3v3m0 12v3M3 12h3m12 0h3"/>',
    "trend": (
        '<path d="M4 17l5-5 4 3 7-8"/><circle cx="4" cy="17" r="1.4"/>'
        '<circle cx="9" cy="12" r="1.4"/><circle cx="13" cy="15" r="1.4"/>'
    ),
    "folder_doc": (
        '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'
        '<path d="M9 13h6"/>'
    ),
    "database": (
        '<ellipse cx="12" cy="6" rx="7" ry="3"/>'
        '<path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/>'
    ),
    "db_search": (
        '<ellipse cx="10" cy="6" rx="6" ry="2.6"/>'
        '<path d="M4 6v8c0 1.4 2.7 2.6 6 2.6M4 10c0 1.4 2.7 2.6 6 2.6"/>'
        '<circle cx="16" cy="16" r="3.2"/><path d="m19 19 1.5 1.5"/>'
    ),
    "check_circle": '<path d="M9 12l2 2 4-4"/><circle cx="12" cy="12" r="9"/>',
    "bookmark": '<path d="M5 4h14v16l-7-3-7 3z"/>',
    "table": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M9 9v11M3 14h18"/>',
    "shield_check": (
        '<path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/><path d="M9 12l2 2 3.5-3.5"/>'
    ),
    "inbox": '<path d="M3 13l3-8h12l3 8M3 13v6h18v-6M3 13h5l1.5 2.5h5L16 13h5"/>',
    "user": '<circle cx="12" cy="8" r="3.4"/><path d="M5 20c0-3.3 3.1-5.5 7-5.5s7 2.2 7 5.5"/>',
    "users": (
        '<circle cx="8" cy="9" r="2.6"/><circle cx="16" cy="9" r="2.6"/>'
        '<path d="M3 19c0-2.5 2.2-4 5-4s5 1.5 5 4M13 19c0-2.5 2-4 4.5-4s4.5 1.5 4.5 4"/>'
    ),
    "wrench": (
        '<path d="M14.5 5.5a3.5 3.5 0 0 0-4.7 4.3L4 15.6 8.4 20l5.8-5.8a3.5 3.5 0 0 0 4.3-4.7l-2.3 2.3-2-2z"/>'  # noqa: E501 - one SVG path, nothing to split
    ),
    "blocks": (
        '<rect x="3" y="3" width="6" height="6" rx="1"/>'
        '<rect x="15" y="15" width="6" height="6" rx="1"/><path d="M9 6h6a3 3 0 0 1 3 3v6"/>'
    ),
    "arrows": '<path d="M4 8h12l-3-3M20 16H8l3 3"/>',
    "mail": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/>',
    "cap": '<path d="M4 5l8-2 8 2-8 2zM12 7v6M6 9v4c0 1.4 2.7 2.5 6 2.5s6-1.1 6-2.5V9"/>',
    "chart": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    "gear": (
        '<circle cx="12" cy="12" r="3"/>'
        '<path d="M19 12a7 7 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7 7 0 0 0-2-1.2L14 2h-4l-.5 2.6'
        "a7 7 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6A7 7 0 0 0 5 12c0 .4 0 .8.1 1.2l-2 1.6 2 3.4 2.4-1"
        'a7 7 0 0 0 2 1.2L10 22h4l.5-2.6a7 7 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2z"/>'
    ),
    "help": (
        '<circle cx="12" cy="12" r="9"/>'
        '<path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.8.4-1 .8-1 1.7M12 17h.01"/>'
    ),
    # Tier markers (replace the old padlock).
    "shield": '<path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/>',
    "user_gear": (
        '<circle cx="9" cy="8" r="3"/><path d="M3 19c0-3 2.7-5 6-5"/>'
        '<circle cx="17.5" cy="16.5" r="2.3"/>'
        '<path d="M17.5 13.8v1.1m0 4.2v-1.1m2.9-1.5h-1.1m-3.6 0h-1.1"/>'
    ),
}


def _nav_icon_html(key: str, *, size: int = 18) -> str | None:
    """Wrap a :data:`_NAV_SVG` entry in a thin stroked ``<svg>`` (or ``None``)."""
    inner = _NAV_SVG.get(key)
    if inner is None:
        return None
    return (
        f'<svg viewBox="0 0 24 24" width="{size}" height="{size}" fill="none" '
        'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
        'stroke-linejoin="round" style="flex:none;display:block;">'
        f"{inner}</svg>"
    )


# The grouped navigation model (home-redesign §3). Groups render
# top→bottom in this order; the Admin group is flagged ``su_group`` and
# floated to the bottom of the rail by :func:`_render_sidebar`.
_NAV_GROUPS: tuple[_NavGroup, ...] = (
    _NavGroup(
        "Dashboard",
        (
            _NavItem("Home", "home", "/home"),
            _NavItem("Projects", "folder", "/dashboard"),
            _NavItem("My Jobs", "my_jobs", "/my-jobs"),
            _NavItem("My custom Workflows", "workflow", "/my-workflows"),
            _NavItem("Custom Workflows inspector", "inspector", "/admin/custom-workflows", su=True),
            _NavItem("Project metadata", "metadata", "/admin/project-metadata", su=True),
        ),
        open_by_default=True,
    ),
    _NavGroup(
        "Studies",
        (
            _NavItem("Compare Jobs", "compare", "/compare"),
            # Studies + governed surfaces are gated by the feature flag only:
            # the item shows whenever the customer has the feature on, and a
            # user without the create permission sees a view-only catalogue
            # (each page hides its own create UI). The create APIs still
            # enforce the permission server-side.
            _NavItem("Job Cohorts", "cohorts", "/cohorts", feature="surrogate_family"),
            _NavItem("Parametric Studies", "bars", "/studies", feature="parametric_studies"),
            _NavItem("Optimisation", "target", "/optimisation", feature="optimisation_framework"),
            _NavItem("Surrogate Models", "trend", "/models", feature="surrogate_family"),
        ),
    ),
    _NavGroup(
        "Libraries",
        (
            _NavItem("File Library", "folder_doc", "/library", feature="file_library"),
            _NavItem("Materials database", "database", "/materials", feature="materials_database"),
            _NavItem("Explore MATOFX database", "db_search", "/admin/explore-db", su=True),
        ),
    ),
    _NavGroup(
        "Governance",
        (
            _NavItem("Approvals", "check_circle", "/approvals", approver_only=True),
            _NavItem(
                "V&V records",
                "bookmark",
                "/vv-records",
                feature="vv_tracking",
                permission=("vv.create", "vv.curate"),
            ),
            _NavItem("Results warehouse", "table", "/results", feature="kpi_extraction"),
            _NavItem("Audit log", "shield_check", "/admin/audit", su=True),
            _NavItem(
                "V&V records inbox",
                "inbox",
                "/vv-records?tab=queue",
                feature="vv_tracking",
                permission="vv.curate",
            ),
        ),
    ),
    # Admin — entire group Super-User-only, ordered to the bottom of the
    # rail. Items are listed in the design's §3 order (not alphabetised:
    # the table order is the on-screen order).
    _NavGroup(
        "Admin",
        (
            _NavItem("New/edit User", "user", "/admin/users", su=True),
            _NavItem("Active sessions", "users", "/admin/sessions", su=True),
            # Sits beside Active sessions because both are licence
            # surfaces (ADR-0019). Gated on the same right the page and
            # the API enforce, so it never appears as a link that 403s.
            _NavItem(
                "Licence & compliance",
                "folder_doc",
                "/admin/compliance",
                su=True,
                permission="audit.read",
            ),
            _NavItem("Edit Teams, Groups & Permissions", "users", "/admin/teams", su=True),
            _NavItem("New/edit tool", "wrench", "/admin/tools", su=True),
            _NavItem("New/edit Workflow", "blocks", "/admin/workflows", su=True),
            # The only Admin-group page the Administrator role can't use —
            # ``migrate.tools_workflows`` is Super-User-only — so it's the
            # one shield entry (verified against the role's permissions).
            _NavItem(
                "Migrate tools/Workflows", "arrows", "/admin/migrate", su=True, super_tier=True
            ),
            _NavItem("Send notifications", "mail", "/admin/send-notifications", su=True),
            _NavItem("Tutorial management", "cap", "/admin/tutorials", su=True),
            _NavItem("Usage statistics", "chart", "/admin/usage", su=True),
            _NavItem("Lineage", "workflow", "/admin/lineage", su=True),
            _NavItem("Execution environments", "console", "/admin/paths", su=True),
            _NavItem("General settings", "gear", "/admin/general-settings", su=True),
        ),
        su_group=True,
    ),
)

# Help — pinned to the very bottom of the rail (below Admin), always
# visible to every user regardless of role. Mirrors the header help
# selector but as a permanent destination (ADR-0014).
_HELP_GROUP: _NavGroup = _NavGroup(
    "Help",
    (
        _NavItem("Help Documentation", "help", "/help"),
        _NavItem("Tutorials", "cap", "/tutorials"),
    ),
)


def render_shell(help_topic: str | None = None) -> ui.dark_mode:
    """Render the grouped sidebar + slim utility header (home-redesign).

    Args:
        help_topic: Optional documentation slug (e.g. ``account-page``)
            for the header's help selector to deep-link into. When
            ``None`` the **Help documentation** entry opens the hub
            root. A slug that doesn't resolve (or that the user can't
            see) falls back to the hub root on the ``/help`` side.

    The shell is now two pieces (the legacy teal banner is gone): a
    grouped, gated, pinnable + resizable left sidebar (§3) carrying the
    brand, the nav, and the account/logout footer; and a slim utility
    header (§4) carrying the global split-search, theme toggle and help
    selector. Page bodies render the greeting / summary / content below.

    Returns the :class:`ui.dark_mode` element so the Account page can
    flip the theme live without instantiating a second one.
    """
    dark = ui.dark_mode(value=(current_theme() == "dark"))
    _apply_brand_colours()
    _inject_theme_css()
    _inject_plotly_theme_hook()
    _inject_shell_css()
    _inject_branding_head()
    drawer = _render_sidebar()
    _install_sidebar_scroll_persistence(drawer)
    _install_sidebar_pin_resize(drawer)
    _render_header(dark, help_topic=help_topic)
    _render_maintenance_banner()
    _render_readonly_banner()
    _render_breakglass_banner()
    _render_support_footer()
    _install_session_keepalive()
    return dark


def _apply_brand_colours() -> None:
    """Point Quasar's brand colours at the mockup palette (home-redesign).

    Without this, every ``color=primary`` button / toggle / badge renders
    in Quasar's default blue while the rest of the chrome is teal — a
    big part of why the app read as "the old GUI". Set once per page
    render (``ui.colors`` is per-client). The teal is fixed across both
    themes so a live light/dark toggle doesn't desync it; the
    per-theme accent still drives custom CSS via ``--matofx-accent``.
    """
    # primary/secondary/accent = MATOFX brand teals. The positive /
    # negative / warning / info slots stay functional status colours
    # (Complete = green, Failed = red, Queued = amber, Running = blue) —
    # those are exceptions to the brand palette, by design.
    ui.colors(
        primary="#40A1A4",
        secondary="#6DCDC4",
        accent="#2D7682",
        positive="#3fb950",
        negative="#f0716b",
        warning="#d6a93b",
        info="#5a9fe0",
    )


def render_header_only() -> ui.dark_mode:
    """Header without the sidebar — used by the login page."""
    dark = ui.dark_mode(value=(current_theme() == "dark"))
    _apply_brand_colours()
    _inject_theme_css()
    _inject_branding_head()
    branding = get_branding()
    with (
        ui.header()
        .classes(replace="row items-center justify-between")
        .style(f"background-color: {DARK_PALETTE.accent}; padding: 0.6rem 1.25rem;")
    ):
        with ui.row().classes("items-center gap-3"):
            if branding.header_icon_basename:
                bust = branding.header_icon_version or branding.header_icon_basename
                ui.html(
                    f'<img src="/api/branding/header_icon_path?v={bust}" '
                    f'alt="" style="height: 32px;">',
                    sanitize=False,
                )
            ui.label(branding.app_display_title).style(
                "color: white; font-size: 1.2rem; font-weight: 700;"
            )
    return dark


def render_public_docs_header() -> ui.dark_mode:
    """New-UI slim header for the pre-login documentation page.

    Unlike :func:`render_header_only` (the login page's dark teal
    banner), this matches the redesigned shell: the brand wordmark on a
    themed bar, the Segoe-UI typeface and palette variables the rest of
    the app uses (via :func:`_inject_shell_css`), and a right-aligned
    **Sign in** button. Used by the public ``/getting-started`` route so
    the Getting Started guide reads like the new UI rather than the
    legacy banner.
    """
    dark = ui.dark_mode(value=(current_theme() == "dark"))
    _apply_brand_colours()
    _inject_theme_css()
    _inject_shell_css()
    _inject_branding_head()
    branding = get_branding()
    title = branding.app_display_title or "MATOFX"
    mark = (title.strip()[:1] or "M").upper()
    with (
        ui.header()
        .classes(replace="row items-center justify-between no-wrap")
        .style(
            "background-color: var(--matofx-drawer-bg); "
            "border-bottom: 1px solid var(--matofx-card-border); "
            "padding: 0.55rem 1.25rem; box-shadow: none;"
        )
    ):
        with ui.row().classes("items-center no-wrap").style("gap: 0.6rem;"):
            with (
                ui.element("div")
                .classes("items-center justify-center")
                .style(
                    "width: 32px; height: 32px; border-radius: 9px; flex: none; display: flex; "
                    "background: linear-gradient(135deg, #6DCDC4, #2D7682); color: #fff; "
                    "font-weight: 800; font-size: 16px;"
                )
            ):
                ui.label(mark)
            ui.html(_brand_title_html(title), sanitize=False)
        ui.button("Sign in", icon="login", on_click=lambda: ui.navigate.to("/login")).props(
            "unelevated dense color=primary"
        )
    return dark


def _inject_branding_head() -> None:
    """Set the page title + favicon ``<link>`` from the cached branding.

    NiceGUI's process-wide ``title`` and ``favicon`` are set once at
    ``ui.run_with`` time and can't be changed at runtime; injecting
    here per page picks up admin saves on the next page load with no
    process restart.
    """
    branding = get_branding()
    ui.page_title(branding.app_display_title)
    if branding.favicon_basename:
        # NiceGUI bakes its own ``<link rel="shortcut icon">`` into the
        # page head when the app is constructed with ``favicon=None``,
        # and there's no public seam to suppress it (see
        # ``nicegui/favicon.py:get_favicon_url``). Emit our link with
        # both ``rel`` values so it overrides NiceGUI's regardless of
        # which one the browser picks, and strip any non-branding
        # favicon link from the DOM in case the browser already
        # latched onto NiceGUI's during initial parse. Bust on file
        # mtime so a re-uploaded favicon (same basename, new bytes)
        # actually replaces the cached one in the browser tab.
        bust = branding.favicon_version or branding.favicon_basename
        href = f"/api/branding/favicon_path?v={bust}"
        ui.add_head_html(
            f'<link rel="icon" href="{href}">'
            f'<link rel="shortcut icon" href="{href}">'
            "<script>"
            "document.querySelectorAll('link[rel~=\"icon\"]')"
            f".forEach(l => {{ if (!l.href.includes('/api/branding/')) l.remove(); }});"
            "</script>"
        )


def _inject_theme_css() -> None:
    """Ship a single style block carrying both palettes as CSS variables.

    The variables are defined twice — once at ``:root`` for the light
    palette, then re-pointed at ``body.body--dark`` for the dark
    palette. NiceGUI's :func:`ui.dark_mode` toggles ``body--dark`` on
    the client, which re-evaluates the variables and re-tints the page
    instantly without a reload. The retrofit rules at the bottom keep
    the small set of inline ``color: #555`` / ``color: #888`` legacy
    page styles readable on the dark side without per-page edits.
    """
    light = LIGHT_PALETTE
    dark = DARK_PALETTE
    ui.add_head_html(
        "<style>"
        # Light defaults — applied whenever body.body--dark is absent.
        ":root, body {"
        f"  --matofx-page-bg: {light.page_bg};"
        f"  --matofx-drawer-bg: {light.drawer_bg};"
        f"  --matofx-card-bg: {light.card_bg};"
        f"  --matofx-dialog-bg: {light.dialog_bg};"
        f"  --matofx-card-border: {light.card_border};"
        f"  --matofx-body-text: {light.body_text};"
        f"  --matofx-muted-text: {light.muted_text};"
        f"  --matofx-label-text: {light.label_text};"
        f"  --matofx-error-text: {light.error_text};"
        f"  --matofx-drawer-item-text: {light.drawer_item_text};"
        f"  --matofx-drawer-group-label: {light.drawer_group_label};"
        f"  --matofx-accent: {light.accent};"
        # Brand light-teal, used in small doses for accents / highlights.
        "  --matofx-accent-bright: #2D7682;"
        "}"
        # Dark overrides — applied while NiceGUI's ui.dark_mode() is on.
        "body.body--dark {"
        f"  --matofx-page-bg: {dark.page_bg};"
        f"  --matofx-drawer-bg: {dark.drawer_bg};"
        f"  --matofx-card-bg: {dark.card_bg};"
        f"  --matofx-dialog-bg: {dark.dialog_bg};"
        f"  --matofx-card-border: {dark.card_border};"
        f"  --matofx-body-text: {dark.body_text};"
        f"  --matofx-muted-text: {dark.muted_text};"
        f"  --matofx-label-text: {dark.label_text};"
        f"  --matofx-error-text: {dark.error_text};"
        f"  --matofx-drawer-item-text: {dark.drawer_item_text};"
        f"  --matofx-drawer-group-label: {dark.drawer_group_label};"
        "  --matofx-accent-bright: #6DCDC4;"
        "}"
        # Page background follows the active palette.
        "body, .q-page { background-color: var(--matofx-page-bg) !important; }"
        # Cards: solid panel, hairline rule line (no shadow, ever — durable
        # user pref), rounded to match the redesign mockup's panels.
        ".q-card {"
        " box-shadow: none !important;"
        " border: 1px solid var(--matofx-card-border) !important;"
        " background-color: var(--matofx-card-bg);"
        " border-radius: 14px;"
        "}"
        # Plots: frame every Plotly chart with the same hairline rule the
        # cards use and give it the card surface as its background, so a
        # chart reads as part of its panel (grey in dark, white in light)
        # rather than a darker rectangle cut into it. Plotly itself adds
        # the ``js-plotly-plot`` class to the chart's root div; its
        # paper/plot backgrounds are tinted to match in
        # ``theme.apply_theme_to_plotly``.
        ".js-plotly-plot {"
        " border: 1px solid var(--matofx-card-border);"
        " border-radius: 10px;"
        " background-color: var(--matofx-card-bg);"
        "}"
        # Modal dialogs: force the dialog card fully opaque so text from
        # the page behind doesn't bleed through. The regular card_bg is
        # transparent on the dark theme (looks right on the page, but
        # reads as a confusing overlay inside a modal); dialog_bg is a
        # solid colour per palette so every dialog reads as its own
        # surface.
        ".q-dialog .q-card {"
        " background-color: var(--matofx-dialog-bg) !important;"
        " opacity: 1 !important;"
        "}"
        # Dark-only retrofits for the small set of inline colours that
        # legacy pages still hard-code. Originals are correct on light
        # mode; we only re-paint them when the dark class is on.
        'body.body--dark [style*="color: #555"] { color: '
        f"{dark.label_text} !important; }}"
        'body.body--dark [style*="color: #888"] { color: '
        f"{dark.muted_text} !important; }}"
        'body.body--dark [style*="color: #c0392b"] { color: '
        f"{dark.error_text} !important; }}"
        "body.body--dark .hover\\:bg-gray-50:hover {"
        " background-color: rgba(255,255,255,0.06) !important;"
        "}"
        # Active sidebar entry — accent-tinted row with an inset accent
        # bar so the operator can see which page they're on. An inset
        # box-shadow (not a real border) avoids nudging the icon/label
        # sideways. ``* !important`` overrides the per-item inline colour
        # carried by the icon and label so they pick up the accent too.
        # Active nav row (mockup): a soft teal wash + a 3px accent bar at
        # the rail's left edge (via ::before, rounded outer corners), and
        # the icon + label tinted with the accent.
        ".matofx-nav-active {"
        " background-color: color-mix(in srgb, var(--matofx-accent) 13%, transparent) !important;"
        " position: relative;"
        "}"
        ".matofx-nav-active::before {"
        " content: ''; position: absolute; left: -0.6rem; top: 6px; bottom: 6px;"
        " width: 3px; border-radius: 0 3px 3px 0; background: var(--matofx-accent);"
        "}"
        ".matofx-nav-active * {"
        " color: var(--matofx-accent) !important;"
        " font-weight: 600 !important;"
        "}"
        # Hide the browser-native number-input spin buttons on every
        # ui.number widget. Without this, a click on the up/down arrow
        # (or a stray click on the right edge of the field) silently
        # decrements/increments by the descriptor's ``precision`` step
        # — turning 25 into 24.99 when precision is 0.01. Users edit
        # with the keyboard, so we just remove the affordance entirely.
        ".q-field--auto-height input[type=number]::-webkit-inner-spin-button,"
        ".q-field--auto-height input[type=number]::-webkit-outer-spin-button,"
        "input[type=number]::-webkit-inner-spin-button,"
        "input[type=number]::-webkit-outer-spin-button {"
        " -webkit-appearance: none;"
        " margin: 0;"
        "}"
        "input[type=number] { -moz-appearance: textfield; }"
        # AG Grid (the Studies results matrix). NiceGUI 3.x ships AG Grid's
        # Theming API (quartz + a variable colour scheme switched light/dark
        # by the ``data-ag-theme-mode`` attribute it sets on the grid root
        # from ``body.body--dark``) — so the legacy ``ag-theme-balham`` CSS
        # classes don't exist; the hook is the ``--ag-*`` theme-parameter
        # variables on the grid root (``.nicegui-aggrid``), which the
        # variable scheme reads. Point them at the palette so the matrix
        # matches the cards (one surface, not odd greys), framed by the
        # card hairline.
        ".nicegui-aggrid {"
        " --ag-background-color: var(--matofx-card-bg);"
        " --ag-foreground-color: var(--matofx-body-text);"
        " --ag-header-background-color: "
        "color-mix(in srgb, var(--matofx-body-text) 6%, var(--matofx-card-bg));"
        " --ag-header-text-color: var(--matofx-muted-text);"
        " --ag-border-color: var(--matofx-card-border);"
        " --ag-row-border-color: var(--matofx-card-border);"
        " --ag-accent-color: var(--matofx-accent);"
        " --ag-row-hover-color: color-mix(in srgb, var(--matofx-accent) 12%, transparent);"
        " --ag-selected-row-background-color: "
        "color-mix(in srgb, var(--matofx-accent) 16%, transparent);"
        " --ag-odd-row-background-color: "
        "color-mix(in srgb, var(--matofx-body-text) 3%, var(--matofx-card-bg));"
        " --ag-font-size: 13px;"
        " border: 1px solid var(--matofx-card-border); border-radius: 10px; overflow: hidden;"
        "}"
        # Quasar tables (the Studies stat tables — sensitivity / ANOVA /
        # best runs) restyled to the card idiom: transparent surface so the
        # card shows through, muted hairline header + rows, accent hover —
        # all theme-tracked.
        ".q-table__card, .q-table__container, .q-table {"
        " background-color: transparent !important; box-shadow: none !important;"
        "}"
        ".q-table { color: var(--matofx-body-text); }"
        ".q-table thead th {"
        " color: var(--matofx-muted-text); font-weight: 600;"
        " border-bottom: 1px solid var(--matofx-card-border);"
        "}"
        ".q-table tbody td { border-bottom: 1px solid var(--matofx-card-border); }"
        ".q-table tbody tr:hover {"
        " background-color: color-mix(in srgb, var(--matofx-accent) 8%, transparent);"
        "}"
        "</style>"
    )


def _inject_plotly_theme_hook() -> None:
    """Re-theme Plotly charts live when the user toggles light / dark.

    ``apply_theme_to_plotly`` leaves the paper/plot backgrounds transparent
    (the chrome CSS paints them, so they re-tint for free), but the figure's
    **font colour** and the template's **gridlines** are baked at render
    time and would otherwise stay frozen after a no-reload theme flip — the
    "text + grid lines don't follow the toggle" symptom. A single
    MutationObserver on ``body``'s class list (the same hook AG Grid uses)
    relayouts every on-page chart's font + axis colours to the new side of
    the palette, covering 2-D axes and 3-D scenes, top-level and subplot
    axes alike. Trace / marker colours are left alone (palette-neutral by
    design); backgrounds are handled by CSS.
    """
    light_font = json.dumps(LIGHT_PALETTE.body_text)
    dark_font = json.dumps(DARK_PALETTE.body_text)
    ui.add_head_html(
        "<script>(function(){"
        "if (window.__matofxPlotlyThemeHook) return;"
        "window.__matofxPlotlyThemeHook = true;"
        # NiceGUI 3.x loads Plotly as an ES module ("nicegui-plotly") and keeps
        # it on the component — it is *never* on window.Plotly, so the old
        # window.Plotly guard always bailed and no chart re-themed. Resolve the
        # module ourselves (via the page import map) and cache it; the plot
        # component already imported it, so this promise resolves immediately.
        "var P=window.Plotly||null;"
        "import('nicegui-plotly').then(function(m){P=m.Plotly||m.default||P;})"
        ".catch(function(){});"
        "function colors(){var d=document.body.classList.contains('body--dark');"
        f"return d?{{font:{dark_font},grid:'rgba(255,255,255,0.10)',"
        "zero:'rgba(255,255,255,0.22)',line:'rgba(255,255,255,0.28)'}:"
        f"{{font:{light_font},grid:'rgba(7,32,63,0.10)',"
        "zero:'rgba(7,32,63,0.20)',line:'rgba(7,32,63,0.28)'};}"
        "function ax(u,p,c){u[p+'.gridcolor']=c.grid;u[p+'.zerolinecolor']=c.zero;"
        "u[p+'.linecolor']=c.line;u[p+'.tickcolor']=c.line;"
        "u[p+'.tickfont.color']=c.font;u[p+'.title.font.color']=c.font;}"
        "function retheme(){var pl=P||window.Plotly;if(!pl)return;var c=colors();"
        "document.querySelectorAll('.js-plotly-plot').forEach(function(gd){try{"
        # Drive off Plotly's computed _fullLayout, not the user layout: charts
        # that never set an axis title have no xaxis/yaxis key in gd.layout, so
        # iterating that skipped their gridlines/ticks on a theme flip.
        "var L=gd._fullLayout||gd.layout||{};"
        "var u={'font.color':c.font,'legend.font.color':c.font};"
        "Object.keys(L).forEach(function(k){if(/^[xyz]axis[0-9]*$/.test(k))ax(u,k,c);});"
        "if(L.scene){['xaxis','yaxis','zaxis'].forEach(function(a){"
        "if(L.scene[a])ax(u,'scene.'+a,c);});}"
        "pl.relayout(gd,u);}catch(e){}});}"
        "var o=new MutationObserver(function(m){for(var i=0;i<m.length;i++){"
        "if(m[i].attributeName==='class'){retheme();break;}}});"
        # add_head_html runs this script in <head>, before <body> is parsed —
        # so document.body is null here and observe() would throw, leaving the
        # observer unattached (the real reason charts never followed the toggle).
        # Defer attaching until the body exists.
        "function startObs(){o.observe(document.body,{attributes:true,attributeFilter:['class']});}"
        "if(document.body){startObs();}else{window.addEventListener('DOMContentLoaded',startObs);}"
        "})();</script>"
    )


def _post_login_target() -> str | None:
    """The page the user was trying to reach, for the login round-trip.

    Returns a NiceGUI-relative path+query (``/my-jobs?job=32``) so a
    deep link opened while logged out survives the sign-in detour.
    NiceGUI is mounted at ``/ui`` and ``ui.navigate.to`` is mount-
    relative, so any ``/ui`` prefix on the raw request path is
    stripped. ``None`` when the request can't be read or the target
    isn't a safe internal GET (so the caller falls back to plain
    ``/login`` — never worse than today's behaviour).
    """
    try:
        request = context.client.request
    except Exception:  # pragma: no cover - defensive: no request context
        return None
    if request is None:
        return None
    path = request.url.path
    for prefix in ("/ui/", "/ui"):
        if path == prefix or path.startswith(prefix):
            path = path[len(prefix) - 1 :] or "/"
            break
    if not path.startswith("/") or path.startswith("//"):
        return None
    if path.rstrip("/") in ("", "/login"):
        return None
    query = request.url.query
    return f"{path}?{query}" if query else path


def _current_nav_path() -> str | None:
    """The current page as a mount-relative path (no query), or ``None``.

    Mirrors the ``/ui`` prefix-stripping in :func:`_post_login_target` so
    the result is directly comparable to each :attr:`_NavItem.target`.
    Used to highlight the active entry in the sidebar.
    """
    try:
        request = context.client.request
    except Exception:  # pragma: no cover - defensive: no request context
        return None
    if request is None:
        return None
    path = request.url.path
    for prefix in ("/ui/", "/ui"):
        if path == prefix or path.startswith(prefix):
            path = path[len(prefix) - 1 :] or "/"
            break
    return path if path.startswith("/") else None


def _active_target(targets: Iterable[str], current: str | None) -> str | None:
    """Longest segment-aware prefix match of ``current`` among ``targets``.

    A target matches when the current path equals it or sits beneath it
    (``/studies`` lights up on ``/studies/42``). Matching on segment
    boundaries (``target + "/"``) stops ``/admin/tools`` lighting up on a
    sibling ``/admin/tools-archive``; the longest match wins so a nested
    ``/admin/vv-records`` beats a hypothetical parent ``/admin``.
    """
    if not current:
        return None
    best: str | None = None
    for target in targets:
        matches = current == target or current.startswith(target.rstrip("/") + "/")
        if matches and (best is None or len(target) > len(best)):
            best = target
    return best


def _login_url(*, target: str | None = None, reason: str = "") -> str:
    """Compose ``/login`` with the optional ``next=`` / ``reason=`` params.

    ``reason`` (``idle`` / ``expired``) makes the sign-in page explain
    *why* the user landed back there — a toast raised immediately
    before navigating away is destroyed with the old page, so the
    message has to travel in the URL.
    """
    params: list[str] = []
    if target:
        params.append(f"next={quote(target, safe='')}")
    if reason:
        params.append(f"reason={reason}")
    return f"/login?{'&'.join(params)}" if params else "/login"


def require_logged_in() -> bool:
    """Return ``True`` when a user is logged in.

    Pages call this at the top of their builder; if it returns
    ``False`` the page redirects to ``/login`` and the caller bails
    out of the rest of its body. The originally-requested page is
    passed through as ``?next=`` so login can return the user there
    (deep links from notification emails rely on this).

    Note this is a *local* check (is a bearer token stored?), not a
    server-side one. A token that expired while the tab was shut still
    passes here; the session is validated for real by the keepalive's
    first tick and by :func:`_handle_expired_session`, which bounces
    the user to ``/login`` on the first authenticated 401.
    """
    if is_logged_in():
        return True
    ui.navigate.to(_login_url(target=_post_login_target()))
    return False


def _handle_expired_session(_error: ApiError) -> None:
    """Land the user on ``/login`` when the API rejects their token.

    Registered process-wide as the API client's unauthenticated hook
    (see :func:`matofx.ui.api_client.set_unauthenticated_hook`), so it
    fires on the *first* authenticated 401 from any page — the token
    was revoked, the account was disabled, or (the common case) the
    session aged past the idle / hard timeout while the tab was closed
    or the keepalive couldn't run. Without it the page kept its dead
    token and every subsequent call surfaced "Your session has
    expired." on a screen the user could no longer do anything with.

    Idempotent by construction: the first call clears the stored token,
    so parallel 401s from the same page render fall out at the
    ``is_logged_in`` guard instead of queuing up more redirects.
    """
    if not is_logged_in():
        return
    target = _post_login_target()
    clear_login()
    ui.navigate.to(_login_url(target=target, reason="expired"))


#: Default teal-tinted background + accent foreground for the icon chip,
#: pulled from the theme variables so the chip tracks light/dark. The
#: ``0.14`` alpha matches the home-page tiles and section-card headers.
_ACK_NOTE_MIN_CHARS = 10
"""Matches the API's minimum, so the dialog rejects a too-short note
before the round trip rather than after it."""

_CHIP_BG = "rgba(64,161,164,0.14)"
_CHIP_FG = "var(--matofx-accent)"


def icon_chip(
    icon: str | None = None,
    *,
    image_url: str | None = None,
    size: int = 30,
    radius: int = 8,
    icon_size: str = "1.05rem",
    bg: str = _CHIP_BG,
    fg: str = _CHIP_FG,
) -> ui.element:
    """The teal rounded icon-chip used across the redesigned shell.

    The single source of truth for the little coloured square that sits
    to the left of a card title (Account/Help section headers), a home
    action tile, or a Quick-Access row. Pass an ``icon`` name, or an
    ``image_url`` for a custom logo (Quick-Access rows render a tool's
    icon image). ``size`` / ``radius`` / ``icon_size`` and the
    ``bg`` / ``fg`` colours are overridable so the larger home tiles
    (38px, accent fill on the primary tile) reuse the same builder.

    Returns the chip element so a caller can chain ``.classes`` /
    ``.style`` if it needs to.
    """
    chip = (
        ui.element("div")
        .classes("items-center justify-center")
        .style(
            f"width: {size}px; height: {size}px; border-radius: {radius}px; flex: none; "
            f"display: flex; overflow: hidden; background: {bg}; color: {fg};"
        )
    )
    with chip:
        if image_url:
            ui.image(image_url).style("width: 100%; height: 100%; object-fit: contain;")
        elif icon:
            ui.icon(icon).style(f"font-size: {icon_size};")
    return chip


def card_header(icon: str, title: str, *, ellipsis: bool = False) -> None:
    """Icon-chip + title header row, matching the home-page cards.

    Renders into the current parent — wrap it yourself in a ``ui.card``
    when you manage the body separately (the Help page's multi-pane
    layout does this), or use :func:`section_card` for the common
    card-plus-body case. ``ellipsis`` truncates a long title to one line.
    """
    with (
        ui.row()
        .classes("items-center no-wrap w-full")
        .style(
            "gap: 0.6rem; padding: 0.7rem 1rem; border-bottom: 1px solid var(--matofx-card-border);"
        )
    ):
        icon_chip(icon)
        title_style = "font-size: 0.95rem; font-weight: 650; color: var(--matofx-body-text);"
        if ellipsis:
            title_style += " white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
        ui.label(title).style(title_style)


@contextlib.contextmanager
def section_card(icon: str, title: str) -> Iterator[None]:
    """A themed card with an icon-chip header — the standard section block.

    Yields inside the card body so callers add their rows/controls with
    ``with section_card("badge", "Identity"):``. Colours come from the
    theme CSS variables, so each card tracks the active light/dark theme.
    """
    with ui.card().classes("w-full").style("padding: 0; gap: 0;"):
        card_header(icon, title)
        with ui.column().classes("w-full").style("padding: 1rem; gap: 0.65rem;"):
            yield


_ROLE_LABEL_OVERRIDES: dict[str, str] = {
    # The plain title-case of ``vv_curator`` ("Vv Curator") reads badly;
    # spell the capability out in full wherever it's shown (group panel,
    # permission-editor picker, user add/edit forms).
    "vv_curator": "Verification & Validation (V&V) curator",
    # "Model Curator" is ambiguous next to the tool/material catalogues —
    # the capability governs *surrogate* models specifically.
    "model_curator": "Surrogate Model Curator",
}
"""Display-label overrides for roles whose title-cased name reads poorly.

Keyed by the canonical snake-case role name. The stored DB name is
unchanged — this only affects what the operator sees."""


def format_role_label(role_name: str) -> str:
    """Render a stored role name as the user-facing label.

    The DB stores roles in canonical snake-case (``regular_user``,
    ``super_user``) so they can be referenced unambiguously across
    the codebase. The dashboard surfaces them title-cased with spaces
    (``Regular User``, ``Super User``) so non-modeller users see plain
    English. Custom roles created at runtime go through the same
    transformation, which means an admin who registers
    ``finance_team_lead`` will see it rendered as ``Finance Team Lead``
    automatically. A small override table (:data:`_ROLE_LABEL_OVERRIDES`)
    handles the handful of names whose title-case reads badly.
    """
    if not role_name:
        return ""
    if role_name in _ROLE_LABEL_OVERRIDES:
        return _ROLE_LABEL_OVERRIDES[role_name]
    return role_name.replace("_", " ").title()


BASE_ROLE_PRIORITY: tuple[str, ...] = (
    "super_user",
    "administrator",
    "beta_tester",
    "regular_user",
)
"""Display order for the four built-in base roles, highest privilege
first. Any other (custom) base role sorts after these, alphabetically by
its user-facing label. Used wherever base roles are listed for the
operator (user add/edit, the group pickers) so the order is consistent."""


def order_base_role_names(names: list[str]) -> list[str]:
    """Order base-role names: the four built-ins first, then others A→Z."""
    priority = {name: i for i, name in enumerate(BASE_ROLE_PRIORITY)}
    return sorted(
        names,
        key=lambda n: (priority.get(n, len(BASE_ROLE_PRIORITY)), format_role_label(n).lower()),
    )


def render_capabilities_card(capability_role_names: list[str], *, selected: list[str]) -> ui.select:
    """Render the Capabilities card (ADR-0005) and return its multi-select.

    Capabilities stack on top of the single base role, so they live in
    their own card — visually distinct from the base-role picker — on the
    user add/edit forms. ``selected`` pre-populates the picker (empty for
    a new user). The caller decides whether to render this at all (only
    when the actor can manage roles and capabilities exist).
    """
    with ui.card().classes("w-full"):
        ui.label("Capabilities").style("font-size: 1.1rem; font-weight: 600;")
        ui.label(
            "Stacked on top of the base role — e.g. let a regular user curate the "
            "file library or materials database without granting admin rights."
        ).style("color: #888; font-size: 0.85rem;")
        options = {name: format_role_label(name) for name in capability_role_names}
        picker = (
            ui.select(options=options, multiple=True, label="Capability roles", with_input=True)
            .classes("w-full")
            .props("dense use-chips")
        )
        picker.value = list(selected)
    return picker


# ----- internals -----------------------------------------------------


def _render_maintenance_banner() -> None:
    """Render a top-of-page yellow banner when an admin has set banner text.

    The banner sits below the header bar and above the page content so
    every user sees it on every page. ``maintenance_window_iso`` (when
    set) is appended in parentheses as a hint about timing.
    """
    branding = get_branding()
    text = branding.maintenance_banner_text.strip()
    if not text:
        return
    window = branding.maintenance_window_iso.strip()
    full = f"{text}  ({window})" if window else text
    with (
        ui.row()
        .classes("w-full items-center justify-center")
        .style(
            "background-color: #fff3cd; color: #856404; "
            "border-bottom: 1px solid #ffeeba; "
            "padding: 0.5rem 1rem; font-size: 0.95rem; font-weight: 500;"
        )
    ):
        ui.icon("warning").style("color: #856404;")
        ui.label(full)


def _render_readonly_banner() -> None:
    """Say plainly when the site is read-only, and why (ADR-0019 slice 6).

    Rendered on every page for **every** user, not just admins: the
    difference between "the button did nothing" and "the site is in a
    known state, here's why" is the whole point. A Super User facing an
    unacknowledged integrity failure also gets the control to lift it.

    Best-effort and deliberately silent on failure — a chrome banner
    must never be the thing that stops a page rendering.
    """
    if not is_logged_in():
        return
    # render_shell() is sync and is called from both sync and async page
    # builders, so the fetch can't be awaited here. Reserve the space and
    # fill it from a one-shot timer — the same idiom the keepalive's
    # first tick uses.
    container = ui.column().classes("w-full").style("gap: 0;")

    async def _populate() -> None:
        try:
            client = ApiClient(get_app(), token=current_token())
            mode = await client.get_site_mode()
        except Exception:
            return
        with container:
            _render_mode_banner(mode)
            # Same payload, same fetch: the reminder rides along rather
            # than costing a second round-trip on every page render.
            _render_compliance_banner(mode)

    ui.timer(0.2, _populate, once=True)


def _render_mode_banner(mode: dict[str, object]) -> None:
    """Paint the banner from an already-fetched site-mode payload."""
    banner_text = str(mode.get("banner") or "")
    if not banner_text:
        return
    read_only = bool(mode.get("read_only"))
    # Read-only is red (work is blocked); an acknowledged integrity
    # failure is amber (writable, but the record stands).
    background, foreground, border = (
        ("#fdecea", "#8a1c13", "#f5c6c3") if read_only else ("#fff3cd", "#856404", "#ffeeba")
    )
    with (
        ui.row()
        .classes("w-full items-center justify-center no-wrap")
        .style(
            f"background-color: {background}; color: {foreground}; "
            f"border-bottom: 1px solid {border}; "
            "padding: 0.5rem 1rem; font-size: 0.92rem; font-weight: 500; gap: 0.6rem;"
        )
    ):
        ui.icon("lock" if read_only else "gpp_maybe").style(f"color: {foreground};")
        ui.label(banner_text).style("line-height: 1.35;")
        if mode.get("can_acknowledge"):
            ui.button("Acknowledge…", on_click=_open_acknowledge_dialog).props(
                "flat dense no-caps"
            ).style(f"color: {foreground}; font-weight: 600;")


def _render_compliance_banner(mode: dict[str, object]) -> None:
    """Amber prompt when a compliance report is due (and only then).

    **This one is a reminder, not a state.** It sits in the same strip as
    the read-only and integrity banners, which say the site is degraded —
    this one says nothing is wrong and nothing has stopped. Someone who
    ignores it entirely keeps working exactly as before. The wording
    carries that, because an administrator who thinks the site is about
    to lock will either panic or resent it, and neither produces the
    report their supplier asked for.

    Shown only to users who can actually generate one — the API decides
    that, on the same ``audit.read`` right the compliance page is gated
    on, so the banner and the page it points at can never disagree about
    who is allowed in.

    It clears for everyone the moment a report exists, without any
    dismissal state: it is derived from a database fact on every render,
    so two administrators can never see different answers, and there is
    no "dismissed" flag to get stuck on.
    """
    if not mode.get("compliance_due"):
        return
    text = str(mode.get("compliance_banner") or "")
    if not text:
        return
    with (
        ui.row()
        .classes("w-full items-center justify-center no-wrap")
        .style(
            "background-color: #fff3cd; color: #856404; "
            "border-bottom: 1px solid #ffeeba; "
            "padding: 0.5rem 1rem; font-size: 0.92rem; font-weight: 500; gap: 0.6rem;"
        )
    ):
        ui.icon("event_note").style("color: #856404;")
        ui.label(text).style("line-height: 1.35;")
        ui.button("Generate report", on_click=lambda: ui.navigate.to("/admin/compliance")).props(
            "flat dense no-caps color=warning"
        ).style("color: #856404; font-weight: 600;")


def _open_acknowledge_dialog() -> None:
    """Collect the required explanation before lifting read-only.

    The note is mandatory and lands in the compliance report, so the
    dialog says so — an acknowledgement whose reason is "ok" tells the
    person reading it six months later nothing at all.
    """
    with ui.dialog() as dialog, ui.card().style("min-width: 460px; padding: 1.3rem;"):
        ui.label("Acknowledge the integrity failure").style("font-size: 1.1rem; font-weight: 650;")
        ui.label(
            "This lifts read-only mode. The failure itself stays in the record and will "
            "appear in the next compliance report, together with your explanation — so "
            "write something the reader will understand months from now."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem; line-height: 1.4;")
        note = (
            ui.textarea(
                label="What happened? (required)",
                placeholder="e.g. Restored from the 03:00 backup after the disk failure on node 2.",
            )
            .props("outlined autogrow")
            .classes("w-full")
        )
        message = ui.label("").style("color: #c0392b; font-size: 0.82rem;")

        async def submit() -> None:
            text = (note.value or "").strip()
            if len(text) < _ACK_NOTE_MIN_CHARS:
                message.text = (
                    f"Please write at least {_ACK_NOTE_MIN_CHARS} characters explaining why."
                )
                return
            try:
                client = ApiClient(get_app(), token=current_token())
                await client.acknowledge_integrity_failure(text)
            except ApiError as exc:
                message.text = exc.detail or "Couldn't acknowledge that."
                return
            dialog.close()
            ui.notify(
                "Integrity failure acknowledged - the site is writable again.",
                type="warning",
            )
            ui.navigate.reload()

        with ui.row().classes("w-full justify-end").style("gap: 0.5rem;"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
            ui.button("Acknowledge", on_click=submit).props("unelevated no-caps color=negative")
    dialog.open()


def _render_breakglass_banner() -> None:
    """Explain a Super User's over-cap admission, once (ADR-0019 §C13).

    Break-glass keeps an administrator able to get in when every licence
    place is taken — but an unexplained over-cap admission is exactly
    what later surfaces as a *breach* in a compliance report. So the
    person it happened to is told, at the moment it happened, and
    pointed at the page where they can free a real place.

    Read-and-clear: this is about the moment of admission, not a
    standing condition, so it must not follow them around all day.
    """
    notice = take_breakglass_notice()
    if not notice:
        return
    in_use = notice.get("in_use")
    n_slots = notice.get("n_slots")
    counts = f" ({in_use} of {n_slots} places in use)" if in_use and n_slots else ""
    with (
        ui.row()
        .classes("w-full items-center justify-center no-wrap")
        .style(
            "background-color: #fff3cd; color: #856404; "
            "border-bottom: 1px solid #ffeeba; "
            "padding: 0.5rem 1rem; font-size: 0.92rem; font-weight: 500; gap: 0.6rem;"
        )
    ):
        ui.icon("vpn_key").style("color: #856404;")
        ui.label(
            "Every licence place was already in use — you were signed in anyway "
            f"because you're a Super User{counts}. This is recorded and will appear "
            "in the next compliance report."
        ).style("line-height: 1.35;")
        ui.button("Free a place", on_click=lambda: ui.navigate.to("/admin/sessions")).props(
            "flat dense no-caps color=warning"
        ).style("color: #856404; font-weight: 600;")


def _render_support_footer() -> None:
    """Render the support-contact footer when an admin has set the address.

    Sits at the bottom of the viewport via Quasar's ``q-footer``. The
    line is small and muted so it doesn't compete with page content.
    """
    branding = get_branding()
    email = branding.support_email_footer.strip()
    if not email:
        return
    with (
        ui.footer(fixed=False)
        .classes("w-full items-center justify-center")
        .style(
            "background-color: transparent; color: var(--matofx-muted-text); "
            "padding: 0.5rem 1rem; font-size: 0.8rem;"
        )
    ):
        ui.html(
            f'Support: <a href="mailto:{email}" '
            f'style="color: var(--matofx-muted-text); text-decoration: underline;">'
            f"{email}</a>",
            sanitize=False,
        )


def _help_target(help_topic: str | None) -> str:
    """Mount-relative URL for the header "?" button.

    Deep-links to ``/help?topic=<slug>`` when a topic is supplied, else
    the hub root. The slug is URL-encoded so values like
    ``jobs/submitting`` survive as a query value.
    """
    if help_topic:
        return f"/help?topic={quote(help_topic, safe='')}"
    return "/help"


def _render_header(dark: ui.dark_mode, *, help_topic: str | None = None) -> None:
    """The theme + help cluster, pinned to the top-right of the content (§4).

    The legacy teal banner is gone and the **global search now lives in
    the page body, below the greeting** (matching the mockup), so this
    shell piece is only the two utility controls floated to the top-right
    corner. The brand + account/logout live on the sidebar; the clock and
    the legacy bell are gone.
    """
    with (
        ui.row()
        .classes("items-center no-wrap matofx-topright")
        .style("position: fixed; top: 0.7rem; right: 1.25rem; z-index: 1000; gap: 0.3rem;")
    ):
        ui.button(
            icon="light_mode" if current_theme() == "dark" else "dark_mode",
            on_click=lambda _e=None, _d=dark: _toggle_theme(_d),
        ).props("flat round dense color=primary").classes("matofx-ibtn").tooltip(
            "Toggle light / dark theme"
        )
        _render_help_selector(help_topic)


def render_global_search() -> None:
    """Public entry so a page body can place the global search itself.

    The home page renders this just under the greeting (mockup order);
    other pages can call it too if they want the bar.
    """
    _render_global_search()


def _render_help_selector(help_topic: str | None) -> None:
    """Help (?) button → **hover** menu: Help documentation · Tutorials (§4).

    A flat CSS hover dropdown (no click, no shadow) matching the
    mockup's ``.hwrap`` / ``.hmenu``.
    """
    with ui.element("div").classes("matofx-hwrap"):
        ui.button(icon="help").props("flat round dense color=primary").tooltip("Help & learning")
        with ui.element("div").classes("matofx-hmenu"):
            with (
                ui.element("div")
                .classes("matofx-tileitem")
                .on("click", lambda _e=None, _t=help_topic: ui.navigate.to(_help_target(_t)))
            ):
                ui.icon("menu_book").style("color: var(--matofx-muted-text); font-size: 1rem;")
                ui.label("Help Documentation")
            with (
                ui.element("div")
                .classes("matofx-tileitem")
                .on("click", lambda: ui.navigate.to("/tutorials"))
            ):
                ui.icon("school").style("color: var(--matofx-muted-text); font-size: 1rem;")
                ui.label("Tutorials")


async def _toggle_theme(dark: ui.dark_mode) -> None:
    """Flip the dashboard theme and persist it server-side (survives logout).

    Mirrors the Account page's toggle: write through to
    ``/api/auth/me/theme`` so the choice follows the user across
    devices, then flip the local cache + the live ``ui.dark_mode``
    element so the page re-tints without a reload.
    """
    from matofx.ui.state import store_theme  # noqa: PLC0415

    new_theme = "light" if current_theme() == "dark" else "dark"
    token = current_token()
    if token:
        client = ApiClient(get_app(), token=token)
        with contextlib.suppress(Exception):
            await client.set_theme_preference(theme=new_theme)
    store_theme(new_theme)
    dark.set_value(new_theme == "dark")


# Global-search scope labels → the API ``scope`` value. ``All`` fans
# out across every type. Off-feature scopes are dropped from the
# dropdown by :func:`_search_scopes` (home-redesign §6, Decision D).
_SEARCH_SCOPES: tuple[tuple[str, str, str | None], ...] = (
    ("All", "all", None),
    ("Jobs", "job", None),
    ("Projects", "project", None),
    ("Studies", "study", "parametric_studies"),
    ("Optimisations", "optimisation", "optimisation_framework"),
    ("Tools", "tool", None),
    ("Workflows", "workflow", None),
    ("Materials", "material", "materials_database"),
    ("Files", "file", "file_library"),
)

# Per-result-type icon for the search dropdown rows.
_SEARCH_TYPE_ICONS: dict[str, str] = {
    "job": "assignment",
    "project": "folder",
    "study": "science",
    "optimisation": "tune",
    "tool": "build",
    "workflow": "account_tree",
    "material": "dataset",
    "file": "description",
}


def _search_scopes() -> dict[str, str]:
    """Scope-label → scope-value map, hiding off-feature scopes (§6, D)."""
    out: dict[str, str] = {}
    for label, value, feature in _SEARCH_SCOPES:
        if feature is not None and not _FEATURE_FLAGS[feature]():
            continue
        out[label] = value
    return out


def _render_global_search() -> None:
    """The header's split global-search: query field + ``in:`` scope (§6).

    Quick-find / jump-to across entities — distinct from the structured
    Job Search page. Typing (debounced) hits ``/api/search``, which
    fans out server-side with the same ACL / feature-flag / role / approver
    gating the rest of the API enforces, and returns hits already grouped
    and labelled (never aliases). Clicking a hit navigates to its detail
    route; ``Enter`` opens the top hit. A transport / endpoint hiccup
    degrades to "no results" rather than breaking the header.
    """
    scopes = _search_scopes()
    state: dict[str, object] = {"hits": [], "timer": None}

    with (
        ui.row()
        .classes("items-center no-wrap matofx-search")
        .style(
            "width: 100%; min-width: 0; height: 50px; "
            "background-color: var(--matofx-card-bg); "
            "border: 1px solid var(--matofx-card-border); border-radius: 13px; "
            "padding: 0 0.35rem 0 0.9rem; gap: 0.5rem;"
        )
    ):
        ui.icon("search").style("color: var(--matofx-muted-text); font-size: 1.2rem;")
        query = (
            ui.input(placeholder="Search…")
            .props("dense borderless")
            .style("flex: 1 1 0; min-width: 0; font-size: 0.95rem;")
        )
        ui.label("Ctrl K").style(
            "font-size: 0.72rem; color: var(--matofx-muted-text); flex: none; "
            "border: 1px solid var(--matofx-card-border); border-radius: 6px; "
            "padding: 1px 7px;"
        )
        with (
            ui.row()
            .classes("items-center no-wrap")
            .style(
                "flex: none; gap: 0.4rem; padding-left: 0.7rem; "
                "border-left: 1px solid var(--matofx-card-border); height: 26px;"
            )
        ):
            ui.label("in").style("font-size: 0.78rem; color: var(--matofx-muted-text);")
            scope = (
                ui.select(options=list(scopes.keys()), value="All")
                .props("dense borderless options-dense")
                .style("min-width: 5.5rem;")
                .tooltip("Limit the search to one kind of item")
            )
        go_btn = (
            ui.button("Search")
            .props("unelevated dense color=primary")
            .style("flex: none; border-radius: 9px; height: 36px;")
        )

        with (
            ui.menu()
            # ``no-focus``: the results menu must NOT grab keyboard focus when
            # it opens — otherwise the user can't keep typing (and has to press
            # Esc to return focus to the input). ``no-refocus`` avoids yanking
            # focus around when it closes.
            .props('no-parent-event no-focus no-refocus anchor="bottom left" self="top left"')
            .style("min-width: 420px; max-width: 560px;") as menu
        ):
            results_box = ui.column().classes("w-full gap-0")

    async def _run_search() -> None:
        text = (query.value or "").strip()
        if not text:
            menu.close()
            return
        token = current_token()
        if not token:
            return
        client = ApiClient(get_app(), token=token)
        try:
            hits = await client.search(query=text, scope=scopes.get(scope.value or "All", "all"))
        except Exception:
            hits = []
        state["hits"] = hits
        results_box.clear()
        with results_box:
            if not hits:
                ui.label(f'No matches for "{text}".').style(
                    "color: var(--matofx-muted-text); padding: 0.75rem 0.9rem; font-size: 0.85rem;"
                )
            else:
                _render_search_hits(hits)
        menu.open()

    def _render_search_hits(hits: list[dict[str, object]]) -> None:
        """Render grouped search hits (icon + label + context + status)."""
        last_type: str | None = None
        for hit in hits:
            hit_type = str(hit.get("type") or "")
            if hit_type != last_type:
                ui.label(str(hit.get("type_label") or hit_type.title())).style(
                    "font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.08em; "
                    "color: var(--matofx-muted-text); padding: 0.5rem 0.9rem 0.2rem;"
                )
                last_type = hit_type
            route = str(hit.get("route") or "")
            with (
                ui.row()
                .classes("items-center w-full no-wrap cursor-pointer hover:bg-gray-50")
                .style("padding: 0.4rem 0.9rem; gap: 0.6rem;")
                .on("click", lambda _e=None, _r=route: _open_search_hit(_r))
            ):
                ui.icon(_SEARCH_TYPE_ICONS.get(hit_type, "search")).style(
                    "color: var(--matofx-accent); font-size: 1.1rem;"
                )
                with ui.column().classes("gap-0").style("flex: 1 1 0; min-width: 0;"):
                    ui.label(str(hit.get("label") or "")).style(
                        "font-size: 0.85rem; font-weight: 600; white-space: nowrap; "
                        "overflow: hidden; text-overflow: ellipsis;"
                    )
                    sub = str(hit.get("sublabel") or "")
                    if sub:
                        ui.label(sub).style(
                            "font-size: 0.72rem; color: var(--matofx-muted-text); "
                            "white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
                        )
                status = str(hit.get("status") or "")
                if status:
                    ui.badge(status).props("color=grey").style("font-size: 0.65rem;")

    def _open_search_hit(route: str) -> None:
        menu.close()
        if route:
            ui.navigate.to(route)

    def _on_enter() -> None:
        hits = state["hits"]
        if isinstance(hits, list) and hits:
            _open_search_hit(str(hits[0].get("route") or ""))

    def _debounce() -> None:
        # Fire ~300ms after the last keystroke so the fan-out stays
        # cheap. The timer lives in this component's ``state`` (not a
        # module global) so concurrent tabs / users never cancel each
        # other's pending search.
        existing = state.get("timer")
        if existing is not None:
            with contextlib.suppress(Exception):
                cast("ui.timer", existing).cancel()
        state["timer"] = ui.timer(0.3, _run_search, once=True)

    query.on_value_change(lambda _e: _debounce())
    scope.on_value_change(lambda _e: _run_search())
    query.on("keydown.enter", lambda _e: _on_enter())
    go_btn.on("click", _run_search)
    # Ctrl/⌘-K focuses the search box from anywhere on the page.
    query_dom_id = f"c{query.id}"
    ui.run_javascript(
        "document.addEventListener('keydown', function(e) {"
        "  if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {"
        "    e.preventDefault();"
        f"    const el = document.getElementById({query_dom_id!r});"
        "    const inp = el && el.querySelector('input');"
        "    if (inp) inp.focus();"
        "  }"
        "});"
    )


def _render_live_clock() -> None:
    """Top-banner clock — current time in the user's locale + timezone.

    Renders a span the page builds at first paint, then ships a tiny
    JS snippet that re-formats ``new Date()`` once a second using the
    browser's :class:`Intl.DateTimeFormat`. We use the browser's clock
    deliberately so a long-lived tab doesn't drift relative to the
    user's wall clock the way a server-pushed value would.

    Locale and timezone values come from per-user storage; the literal
    ``'auto'`` falls through to the browser's defaults (i.e. JS sees
    ``undefined`` and Intl picks them up).
    """
    locale = current_locale()
    timezone = current_timezone()
    locale_js = "undefined" if locale == "auto" else json.dumps(locale)
    tz_js = "undefined" if timezone == "auto" else json.dumps(timezone)
    ui.html(
        '<span id="matofx-clock" style="color: white; min-width: 22ch; text-align: right;"></span>',
        sanitize=False,
    )
    # Run-once per page render. ``setInterval`` is recreated each time
    # the chrome rebuilds; we clear the previous handle so navigations
    # don't pile up timers in the same DOM.
    ui.run_javascript(
        f"""
        (function() {{
          const opts = {{
            weekday: 'short',
            year: 'numeric',
            month: 'short',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            timeZoneName: 'short',
          }};
          const tz = {tz_js};
          if (tz !== undefined) opts.timeZone = tz;
          const loc = {locale_js};
          let fmt;
          try {{
            fmt = new Intl.DateTimeFormat(loc, opts);
          }} catch (e) {{
            fmt = new Intl.DateTimeFormat(undefined, opts);
          }}
          function tick() {{
            const el = document.getElementById('matofx-clock');
            if (!el) return;
            el.textContent = fmt.format(new Date());
          }}
          if (window._matofxClockTimer) {{
            clearInterval(window._matofxClockTimer);
          }}
          tick();
          window._matofxClockTimer = setInterval(tick, 1000);
        }})();
        """
    )


def _nav_item_visible(item: _NavItem) -> bool:
    """Resolve a nav item's gates (home-redesign §3 + §10).

    An item shows only when every gate it declares passes: Super-User
    (role), approver flag, the per-customer feature flag, and (for the
    three curator workspaces) the curate capability. All gates default
    to "no gate" so an unmarked item shows for everyone.
    """
    if item.super_tier and not is_super_user():
        # Super-User-tier (shield) entries are hidden from Administrators.
        return False
    if item.su and not item.super_tier and not is_admin():
        return False
    if item.approver_only and not current_is_approver():
        return False
    if item.feature is not None and not _FEATURE_FLAGS[item.feature]():
        return False
    if item.permission is None:
        return True
    slugs = (item.permission,) if isinstance(item.permission, str) else item.permission
    # A tuple is satisfied by holding ANY of the slugs (create-or-curate).
    return any(has_permission(slug) for slug in slugs)


def _visible_groups() -> list[tuple[_NavGroup, list[_NavItem]]]:
    """Filter the nav model down to what the caller can see (§3)."""
    out: list[tuple[_NavGroup, list[_NavItem]]] = []
    for group in _NAV_GROUPS:
        if group.su_group and not is_admin():
            continue
        items = [it for it in group.items if _nav_item_visible(it)]
        if items:
            out.append((group, items))
    return out


def _render_sidebar() -> ui.left_drawer:
    """The grouped, gated, resizable sidebar (§3).

    Groups render top→bottom in the design's order; the Admin group is
    floated to the bottom (a flex spacer pushes it + the footer down).
    The brand sits at the top; the footer carries the account avatar +
    a logout control.

    The sidebar is rendered **always-expanded** (full width) — the
    pinned/hover icon-rail variant from the mockup is deferred until the
    rest of the layout is locked in, because Quasar's ``mini`` mode kept
    collapsing the rail to an empty strip. The width is still
    drag-resizable and persisted (§9).
    """
    width = current_sidebar_width()
    drawer = (
        ui.left_drawer(value=True, fixed=True, bordered=True)
        .props(f"width={width} no-swipe-open no-swipe-close")
        .style("background-color: var(--matofx-drawer-bg); padding: 0;")
    )
    # Apply the unpinned icon-rail state from the stored preference by
    # toggling the body class the shell CSS keys off (pure CSS hover —
    # no fragile Quasar mini mode). Pinned (default) leaves it full.
    if current_sidebar_pinned():
        ui.query("body").classes(remove="matofx-railed")
    else:
        ui.query("body").classes(add="matofx-railed")

    current = _current_nav_path()
    groups = _visible_groups()
    # Help always sits at the very bottom; include its targets so /help
    # and /tutorials highlight correctly.
    help_items = list(_HELP_GROUP.items)
    all_targets = [item.target for _g, items in groups for item in items]
    all_targets += [it.target for it in help_items]
    active = _active_target(all_targets, current)
    active_group_label = next(
        (
            g.label
            for g, items in [*groups, (_HELP_GROUP, help_items)]
            if any(it.target == active for it in items)
        ),
        None,
    )

    non_admin = [(g, items) for g, items in groups if not g.su_group]
    admin = [(g, items) for g, items in groups if g.su_group]

    with (
        drawer,
        ui.column()
        .classes("w-full no-wrap")
        .style("min-height: 100%; gap: 0; padding: 0.6rem 0.6rem 0.4rem;"),
    ):
        _render_sidebar_brand(drawer)
        for group, items in non_admin:
            _render_group(
                group,
                items,
                active_target=active,
                force_open=(group.label == active_group_label),
            )
        # Push the Admin group + Help + footer to the bottom of the rail.
        ui.space()
        for group, items in admin:
            _render_group(
                group,
                items,
                active_target=active,
                force_open=(group.label == active_group_label),
            )
        _render_group(
            _HELP_GROUP,
            help_items,
            active_target=active,
            force_open=(_HELP_GROUP.label == active_group_label),
        )
        _render_sidebar_footer()
    return drawer


def _brand_title_html(title: str) -> str:
    """Render the brand wordmark like the mockup: teal ``OFX`` + a ™ superscript.

    Any trailing trademark glyph already in the title is stripped and
    re-added as a proper superscript. ``OFX`` (when present) is tinted
    with the accent, matching ``MAT`` + teal ``OFX`` + ``SPDM™``.
    """
    import html as _html  # noqa: PLC0415

    base = title.strip().rstrip("™").replace("(TM)", "").replace("(tm)", "").strip()
    safe = _html.escape(base)
    safe = safe.replace("OFX", '<span style="color: var(--matofx-accent-bright);">OFX</span>', 1)
    return (
        '<span style="font-size: 0.97rem; font-weight: 700; letter-spacing: 0.3px; '
        'white-space: nowrap; color: var(--matofx-body-text);">'
        f"{safe}"
        '<sup style="font-size: 0.6em; font-weight: 600; vertical-align: 0.32em; '
        'margin-left: 0.5px;">™</sup>'
        "</span>"
    )


def _render_sidebar_brand(drawer: ui.left_drawer) -> None:
    """Brand row at the top of the rail: compact mark + title + pin toggle.

    The pin button toggles the unpinned **icon-rail** state by flipping
    the ``body.matofx-railed`` class the shell CSS keys off (a pure-CSS
    hover-expand rail — see :func:`_inject_shell_css`), then persists the
    choice to the server-side preferences blob (§9).
    """
    del drawer  # the CSS rail keys off a body class, not the drawer element
    branding = get_branding()
    title = branding.app_display_title or "MATOFX"
    mark = (title.strip()[:1] or "M").upper()
    with (
        ui.row()
        .classes("items-center w-full no-wrap")
        .style("gap: 0.6rem; padding: 0.2rem 0.2rem 0.9rem;")
    ):
        # Compact brand mark — a teal rounded square with the title's
        # initial (matches the mockup). The deployment's full wordmark
        # logo is wide and breaks the rail, so it isn't shown here; it
        # still appears on the login screen.
        with (
            ui.element("div")
            .classes("items-center justify-center matofx-brandmark")
            .style(
                "width: 32px; height: 32px; border-radius: 9px; flex: none; display: flex; "
                "background: linear-gradient(135deg, #6DCDC4, #2D7682); color: #fff; "
                "font-weight: 800; font-size: 16px;"
            )
        ):
            ui.label(mark)
        ui.html(_brand_title_html(title), sanitize=False).classes("matofx-nav-lbl").style(
            "flex: 1 1 0; min-width: 0; overflow: hidden;"
        )
        pin_color = "color=primary" if current_sidebar_pinned() else "color=grey"
        pin_btn = (
            ui.button(icon="push_pin")
            .props(f"flat round dense size=sm {pin_color}")
            .classes("matofx-nav-lbl")
            .tooltip("Pin / unpin the sidebar (unpinned = icon rail that expands on hover)")
        )

        def _toggle_pin() -> None:
            new_pinned = not current_sidebar_pinned()
            store_sidebar_pinned(new_pinned)
            if new_pinned:
                ui.query("body").classes(remove="matofx-railed")
                pin_btn.props("color=primary")
            else:
                ui.query("body").classes(add="matofx-railed")
                pin_btn.props(remove="color=primary")
                pin_btn.props("color=grey")
            _persist_ui_pref(sidebar_pinned=new_pinned)

        pin_btn.on("click", _toggle_pin)


def _render_sidebar_footer() -> None:
    """Footer: avatar + name → account settings, then a logout control (§7.4).

    Both the avatar and the name open ``/account``; a vertical divider
    separates them from a right-aligned logout control (door-out icon +
    "Logout", red on hover). In the collapsed icon-rail only the avatar
    remains (handled by the brand-text hide rule).
    """
    first = current_first_name() or (current_username() or "")
    initials = (first[:2] or "?").upper()
    with (
        ui.row()
        .classes("items-center w-full no-wrap matofx-sidebar-foot")
        .style(
            "gap: 0.5rem; padding: 0.6rem 0.3rem 0.2rem; "
            "border-top: 1px solid var(--matofx-card-border); margin-top: 0.4rem;"
        )
    ):
        avatar = (
            ui.element("div")
            .classes("items-center justify-center")
            .style(
                "width: 30px; height: 30px; border-radius: 50%; flex: none; "
                "display: flex; background: var(--matofx-accent); color: #fff; "
                "font-size: 0.72rem; font-weight: 700; cursor: pointer;"
            )
            .on("click", lambda: ui.navigate.to("/account"))
            .tooltip("My Account Settings")
        )
        with avatar:
            ui.label(initials)
        with (
            ui.column()
            .classes("gap-0 matofx-foot-text cursor-pointer")
            .style("flex: 1 1 0; min-width: 0;")
            .on("click", lambda: ui.navigate.to("/account"))
            .tooltip("My Account Settings")
        ):
            ui.label(first or "Account").style(
                "font-size: 0.8rem; font-weight: 600; white-space: nowrap; "
                "overflow: hidden; text-overflow: ellipsis; color: var(--matofx-body-text);"
            )
            ui.label("Account & preferences").style(
                "font-size: 0.66rem; color: var(--matofx-muted-text);"
            )
        ui.element("div").classes("matofx-foot-div").style(
            "width: 1px; align-self: stretch; background: var(--matofx-card-border);"
        )
        with (
            ui.row()
            .classes("items-center matofx-logout-btn cursor-pointer")
            .style("gap: 0.35rem; padding: 0.25rem 0.4rem; border-radius: 6px;")
            .on("click", _logout)
            .tooltip("Log out")
        ):
            ui.icon("logout").style("font-size: 1rem;")
            ui.label("Logout").classes("matofx-foot-text").style(
                "font-size: 0.78rem; font-weight: 600;"
            )


def _persist_ui_pref(**changes: object) -> None:
    """Write a dashboard-shell preference through to the server (§9).

    Fire-and-forget: the local cache was already updated by the caller
    (so the UI reflects the change instantly), and this just durably
    persists it via ``/api/auth/me/ui-preferences`` so it survives
    logout and follows the user across devices. A transport hiccup is
    swallowed — a preference that fails to persist is a cosmetic loss,
    never an error worth interrupting the user for.
    """
    token = current_token()
    if not token:
        return
    client = ApiClient(get_app(), token=token)

    async def _write() -> None:
        with contextlib.suppress(Exception):
            await client.set_ui_preferences(**changes)  # type: ignore[arg-type]

    spawn(_write())


def _install_sidebar_pin_resize(drawer: ui.left_drawer) -> None:
    """Drag-to-resize the pinned sidebar (188–460px), persisted server-side.

    A thin handle straddles the drawer's right edge (visible only while
    pinned). Dragging live-updates the drawer width + the page content's
    left padding client-side (no server round-trip per frame); on release
    the final width is emitted once and written through to the
    preferences blob (§9), so it survives logout and reloads at that
    width. Window-level listeners are installed once per tab and look the
    drawer up live, so they keep working across the per-navigation rebuild.
    """
    del drawer  # the JS finds the drawer by a stable selector, not its id
    ui.on("matofx_sidebar_width", lambda e: _on_sidebar_resized(e.args))
    ui.run_javascript(
        """
        (function() {
          const MIN = 188, MAX = 460;
          // Look the drawer up by a STABLE selector each time (the
          // per-page DOM id changes on navigation; there is only one
          // left drawer, so `.q-drawer` is reliable).
          function qdrawer() { return document.querySelector('.q-drawer'); }
          function pageContainer() { return document.querySelector('.q-page-container'); }
          // Resize is only meaningful when pinned (full width); in the
          // unpinned icon-rail the width is CSS-driven on hover.
          function pinned() { return !document.body.classList.contains('matofx-railed'); }
          let handle = document.getElementById('matofx-resizer');
          if (!handle) {
            handle = document.createElement('div');
            handle.id = 'matofx-resizer';
            handle.style.cssText =
              'position:fixed;top:0;height:100vh;width:8px;cursor:col-resize;z-index:2500;';
            document.body.appendChild(handle);
          }
          function place() {
            const d = qdrawer();
            if (!d || !pinned()) { handle.style.display = 'none'; return; }
            handle.style.display = 'block';
            handle.style.left = (d.getBoundingClientRect().width - 4) + 'px';
          }
          place();
          // Reposition after Quasar finishes laying the drawer out.
          requestAnimationFrame(place);
          if (!window._matofxResizerHook) {
            window._matofxResizerHook = true;
            let dragging = false;
            handle.addEventListener('mousedown', function(e) {
              if (!pinned()) return;
              e.preventDefault();
              dragging = true;
              document.body.style.userSelect = 'none';
              document.body.style.cursor = 'col-resize';
            });
            window.addEventListener('mousemove', function(e) {
              if (!dragging) return;
              const d = qdrawer();
              if (!d) return;
              const w = Math.max(MIN, Math.min(MAX, e.clientX));
              d.style.width = w + 'px';
              const pc = pageContainer();
              if (pc) pc.style.paddingLeft = w + 'px';
              handle.style.left = (w - 4) + 'px';
            });
            window.addEventListener('mouseup', function() {
              if (!dragging) return;
              dragging = false;
              document.body.style.userSelect = '';
              document.body.style.cursor = '';
              const d = qdrawer();
              if (!d) return;
              const w = Math.round(d.getBoundingClientRect().width);
              if (window.emitEvent) window.emitEvent('matofx_sidebar_width', w);
            });
            window.addEventListener('resize', place);
          }
        })();
        """
    )


def _on_sidebar_resized(width: object) -> None:
    """Persist a drag-resized sidebar width (clamped) to the prefs blob."""
    try:
        value = int(cast("SupportsInt", width))
    except (TypeError, ValueError):
        return
    store_sidebar_width(value)
    _persist_ui_pref(sidebar_width=max(188, min(460, value)))


def _inject_shell_css() -> None:
    """Sidebar + header CSS that the redesigned shell needs (home-redesign).

    Layered on top of the palette variables shipped by
    :func:`_inject_theme_css`. Covers nav-item hover, the icon-rail
    label-hiding when the drawer is in Quasar ``mini`` mode (unpinned,
    not hovered), and the footer logout hover tint.
    """
    ui.add_head_html(
        "<style>"
        # Match the mockup's typeface (Quasar ships Roboto by default).
        "body, .q-card, .q-drawer, .q-menu, input, button, .q-field, .matofx-nav-item {"
        " font-family: 'Segoe UI', -apple-system, system-ui, 'Helvetica Neue', sans-serif;"
        "}"
        # Buttons: sentence case (the mockup never SHOUTS), rounded, and a
        # touch less heavy than Quasar's default.
        ".q-btn { text-transform: none !important; font-weight: 600; border-radius: 8px; }"
        ".q-btn .q-btn__content { letter-spacing: normal; }"
        # Small outlined teal buttons (Launch / Review / Tutorials / New)
        # read like the mockup's pill buttons.
        ".q-btn--outline { border-radius: 8px; }"
        # Nav items: sentence case (NOT the group heading's uppercase,
        # which would otherwise cascade down), quiet hover wash.
        ".matofx-nav-item, .matofx-nav-lbl, .matofx-foot-text {"
        " text-transform: none !important; letter-spacing: normal !important;"
        "}"
        ".matofx-nav-item { font-size: 12px; font-weight: 500; }"
        ".matofx-nav-item:hover { background-color: rgba(127,127,127,0.10); }"
        # Tighten the gap between a group heading and its page names.
        ".matofx-nav-group .q-expansion-item__content { padding: 0 !important; }"
        ".matofx-nav-group > .q-expansion-item__container > .q-item {"
        " min-height: 0 !important; padding-top: 8px !important; padding-bottom: 0 !important; }"
        ".matofx-nav-group .q-expansion-item__container > .q-item .q-item__section { padding: 0; }"
        # Trim the nav rows' own vertical padding so the group reads tight.
        ".matofx-nav-item { padding-top: 4px !important; padding-bottom: 4px !important; }"
        # Sidebar scrollbar — same thin style as the table scroll areas.
        ".q-drawer .q-drawer__content::-webkit-scrollbar { width: 9px; }"
        ".q-drawer .q-drawer__content::-webkit-scrollbar-thumb {"
        " background: rgba(127,127,127,0.32); border-radius: 5px;"
        " border: 2px solid var(--matofx-drawer-bg); }"
        ".q-drawer .q-drawer__content::-webkit-scrollbar-track { background: transparent; }"
        # Header help: hover-reveal dropdown (mockup .hwrap / .hmenu).
        ".matofx-hwrap { position: relative; display: flex; align-items: center; }"
        ".matofx-hmenu { position: absolute; top: calc(100% + 6px); right: 0; min-width: 220px;"
        " background: var(--matofx-dialog-bg); border: 1px solid var(--matofx-card-border);"
        " border-radius: 12px; padding: 5px; z-index: 1100; display: none; }"
        ".matofx-hmenu::before { content: ''; position: absolute; left: 0; right: 0;"
        " top: -10px; height: 10px; }"
        ".matofx-hwrap:hover .matofx-hmenu { display: block; }"
        # Footer logout: red wash on hover, matching the mockup.
        ".matofx-logout-btn:hover { background-color: rgba(240,113,107,0.14);"
        " color: var(--matofx-error-text) !important; }"
        ".matofx-logout-btn:hover * { color: var(--matofx-error-text) !important; }"
        # The drag handle highlights on hover / during a drag.
        "#matofx-resizer:hover { background: var(--matofx-accent); opacity: 0.5; }"
        # ----- pinnable icon-rail (CSS-driven, no Quasar mini) ----------
        # When the sidebar is unpinned, ``body.matofx-railed`` is set.
        # The drawer shrinks to an icon strip and the page content's left
        # padding follows it; hovering the rail expands it to full width
        # as an overlay (the padding stays narrow, so the page doesn't
        # reflow). Pinned (default) leaves everything full-width.
        ".q-drawer { transition: width .16s ease; }"
        "body.matofx-railed .q-drawer { width: 66px !important; overflow: hidden; }"
        "body.matofx-railed .q-page-container { padding-left: 66px !important; }"
        "body.matofx-railed .q-drawer:hover { width: 252px !important;"
        " box-shadow: 6px 0 26px rgba(0,0,0,.4); z-index: 3000; }"
        # Rail (unpinned + not hovered): hide labels / group headers /
        # footer text, centre the item icons.
        "body.matofx-railed .q-drawer:not(:hover) .matofx-nav-lbl,"
        "body.matofx-railed .q-drawer:not(:hover) .matofx-foot-text,"
        "body.matofx-railed .q-drawer:not(:hover) .matofx-foot-div,"
        "body.matofx-railed .q-drawer:not(:hover) .matofx-logout-btn,"
        "body.matofx-railed .q-drawer:not(:hover) .matofx-nav-group >"
        " .q-expansion-item__container > .q-item { display: none !important; }"
        "body.matofx-railed .q-drawer:not(:hover) .matofx-nav-item {"
        " justify-content: center; padding-left: 0; padding-right: 0; }"
        # ----- dropdown menus: remove Quasar's shadow (mockup is flat) --
        ".matofx-flatmenu { box-shadow: none !important;"
        " border: 1px solid var(--matofx-card-border) !important; }"
        # ----- CSS-hover tile dropdowns (close on mouse-leave, no JS) ---
        ".matofx-tile { position: relative; overflow: visible; }"
        ".matofx-tilemenu { position: absolute; top: calc(100% + 6px); left: 0;"
        " min-width: 230px; background: var(--matofx-dialog-bg);"
        " border: 1px solid var(--matofx-card-border); border-radius: 12px;"
        " padding: 5px; z-index: 50; display: none; }"
        # Invisible bridge across the trigger→menu gap so hover doesn't drop.
        ".matofx-tilemenu::before { content: ''; position: absolute; left: 0; right: 0;"
        " top: -10px; height: 10px; }"
        ".matofx-tile:hover .matofx-tilemenu { display: block; }"
        ".matofx-tileitem { display: flex; align-items: center; gap: 0.6rem;"
        " padding: 8px 11px; border-radius: 8px; cursor: pointer; font-size: 13px; }"
        ".matofx-tileitem:hover { background: var(--matofx-accent); color: #fff; }"
        ".matofx-tileitem:hover * { color: #fff !important; }"
        # ----- Quick Access segmented pill (mockup .qaseg) --------------
        ".matofx-qaseg { display: flex; background: var(--matofx-page-bg);"
        " border: 1px solid var(--matofx-card-border); border-radius: 8px;"
        " overflow: hidden; }"
        ".matofx-qaseg .seg-btn { padding: 3px 11px; font-size: 11px;"
        " color: var(--matofx-muted-text); cursor: pointer; font-weight: 600;"
        " user-select: none; line-height: 18px; }"
        ".matofx-qaseg .seg-btn.on { background: var(--matofx-accent); color: #fff; }"
        # ----- My-Work tables (mockup CSS grid; uniform row heights) ----
        # Column header sits INSIDE the scroll area (sticky) so it shares
        # the rows' exact content width — no scrollbar offset, perfect
        # column alignment. ``width: 100%`` forces the grids to fill the
        # card (NiceGUI's column defaults to align-items:start, which
        # would otherwise shrink-wrap them to their content).
        ".matofx-colhead { display: grid; width: 100%; align-items: center;"
        " column-gap: 14px; padding: 9px 15px;"
        " border-bottom: 1px solid var(--matofx-card-border);"
        " font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.7px;"
        " color: var(--matofx-muted-text); font-weight: 700; position: sticky; top: 0;"
        " z-index: 2; background: color-mix(in srgb, #808080 7%, var(--matofx-card-bg)); }"
        ".matofx-tablescroll { width: 100%; overflow-y: auto; flex: 1 1 0; min-height: 0; }"
        ".matofx-erow { display: grid; width: 100%; align-items: center; column-gap: 14px;"
        " padding: 10px 15px; font-size: 12.5px; cursor: pointer;"
        " border-bottom: 1px solid var(--matofx-card-border); }"
        ".matofx-erow:hover { background: rgba(127,127,127,0.06); }"
        # Name cell: always two lines (label + sub), so the sub being
        # empty still reserves its line and every row is the same height.
        ".matofx-ename { display: flex; flex-direction: column; min-width: 0; }"
        ".matofx-nlink { color: var(--matofx-body-text); font-weight: 600;"
        " white-space: nowrap; overflow: hidden; text-overflow: ellipsis; line-height: 17px; }"
        ".matofx-nsub { font-size: 10.5px; color: var(--matofx-muted-text);"
        " white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"
        " line-height: 15px; min-height: 15px; }"
        ".matofx-col { color: var(--matofx-muted-text); font-size: 12px;"
        " white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }"
        ".matofx-pill { display: inline-block; font-size: 9.5px; font-weight: 600;"
        " padding: 2px 9px; border-radius: 20px; white-space: nowrap; }"
        ".matofx-pill.p-run { background: rgba(90,159,224,0.16); color: #5a9fe0; }"
        ".matofx-pill.p-ok { background: rgba(63,185,80,0.15); color: #3fb950; }"
        ".matofx-pill.p-q { background: rgba(214,169,59,0.16); color: #d6a93b; }"
        ".matofx-pill.p-fail { background: rgba(240,113,107,0.15); color: #f0716b; }"
        ".matofx-pill.p-cr { background: rgba(122,130,139,0.18); color: #9aa0a6; }"
        ".matofx-tablefoot { flex: none; padding: 10px 15px;"
        " border-top: 1px solid var(--matofx-card-border); font-size: 12px;"
        " color: var(--matofx-accent); font-weight: 600; display: flex;"
        " align-items: center; gap: 5px; cursor: pointer; }"
        # Thin scrollbar for the table scroll areas (mockup .scroll).
        ".matofx-scroll::-webkit-scrollbar { width: 9px; }"
        ".matofx-scroll::-webkit-scrollbar-thumb { background: rgba(127,127,127,0.32);"
        " border-radius: 5px; border: 2px solid var(--matofx-card-bg); }"
        ".matofx-scroll::-webkit-scrollbar-track { background: transparent; }"
        # ----- Quick Access rows: Launch (hover menu) + Tutorials buttons
        ".matofx-qarow:hover { background: rgba(127,127,127,0.04); }"
        ".matofx-lbtn { height: 28px; border: 1px solid var(--matofx-accent);"
        " color: var(--matofx-accent); background: transparent; border-radius: 7px;"
        " padding: 0 11px; font-size: 11.5px; font-weight: 600; display: flex;"
        " align-items: center; gap: 5px; cursor: pointer; white-space: nowrap; }"
        ".matofx-lbtn:hover { background: rgba(64,161,164,0.14); }"
        ".matofx-tbtn { height: 28px; border: 1px solid var(--matofx-card-border);"
        " color: var(--matofx-muted-text); background: transparent; border-radius: 7px;"
        " padding: 0 10px; font-size: 11.5px; font-weight: 600; display: flex;"
        " align-items: center; gap: 5px; cursor: pointer; white-space: nowrap; }"
        ".matofx-tbtn:hover { border-color: var(--matofx-accent); color: var(--matofx-accent); }"
        ".matofx-launchwrap { position: relative; }"
        ".matofx-launchmenu { position: absolute; top: calc(100% + 6px); right: 0;"
        " min-width: 188px; background: var(--matofx-dialog-bg);"
        " border: 1px solid var(--matofx-card-border); border-radius: 10px; padding: 5px;"
        " z-index: 60; display: none; }"
        ".matofx-launchmenu::before { content: ''; position: absolute; left: 0; right: 0;"
        " top: -9px; height: 9px; }"
        ".matofx-launchwrap:hover .matofx-launchmenu { display: block; }"
        ".matofx-launchitem { display: flex; align-items: center; gap: 9px;"
        " padding: 8px 10px; border-radius: 7px; font-size: 12px; cursor: pointer;"
        " white-space: nowrap; }"
        ".matofx-launchitem:hover { background: var(--matofx-accent); color: #fff; }"
        ".matofx-launchitem:hover * { color: #fff !important; }"
        "</style>"
    )


def _install_sidebar_scroll_persistence(drawer: ui.left_drawer) -> None:
    """Keep the sidebar's scroll offset across page navigations.

    The whole drawer is rebuilt on every navigation (each page calls
    :func:`render_shell`), so a fresh DOM node always starts scrolled to
    the top — clicking a link low in the rail (e.g. a Demo sub-section)
    would snap the rail back up and force the user to scroll down again.

    We persist the scroll offset in the browser's ``sessionStorage``
    (keyed per tab, reset when the tab closes) rather than round-tripping
    through ``app.storage.user``: it's purely presentational, must survive
    the synchronous rebuild, and shouldn't bleed across devices/tabs the
    way section-expansion state does. The restore is deferred two frames
    so Quasar has laid the expansion panels out and ``scrollTop`` sticks.
    """
    # NiceGUI renders each element with the DOM id ``c{id}``; the
    # scrollable region of a Quasar QDrawer is ``.q-drawer__content``.
    drawer_dom_id = f"c{drawer.id}"
    ui.run_javascript(
        f"""
        (function() {{
          const KEY = 'matofx_sidebar_scroll';
          const drawer = document.getElementById({drawer_dom_id!r});
          if (!drawer) return;
          const content = drawer.querySelector('.q-drawer__content') || drawer;
          const saved = sessionStorage.getItem(KEY);
          if (saved !== null) {{
            requestAnimationFrame(function() {{
              requestAnimationFrame(function() {{ content.scrollTop = parseFloat(saved); }});
            }});
          }}
          content.addEventListener('scroll', function() {{
            sessionStorage.setItem(KEY, content.scrollTop);
          }}, {{ passive: true }});
        }})();
        """
    )


def _render_group(
    group: _NavGroup,
    items: list[_NavItem],
    *,
    active_target: str | None = None,
    force_open: bool = False,
) -> None:
    """One collapsible sidebar group (home-redesign §3).

    Every group is **expanded by default** (matching the locked mockup,
    which shows all sections open); the user's own collapse choices are
    then remembered via the server-side preferences blob (§9) so
    navigating between pages doesn't snap a group shut. ``force_open``
    opens the group on load when it contains the active page so the
    highlight is visible — without persisting, so the user's own toggling
    still wins next time. A Super-User-only group (Admin) and any SU item
    carry a padlock. The entry whose target equals ``active_target`` gets
    the ``matofx-nav-active`` accent class.
    """
    collapsed = is_sidebar_group_collapsed(group.label, default=False)
    expansion = ui.expansion(group.label, value=(not collapsed) or force_open).classes(
        "w-full matofx-nav-group"
    )
    expansion.on_value_change(
        lambda e, _l=group.label: store_sidebar_group_collapsed(_l, not bool(e.value))
    )
    expansion.style(
        "color: var(--matofx-body-text); font-size: 0.68rem; font-weight: 700; "
        "text-transform: uppercase; letter-spacing: 0.08em;"
    )
    expansion.props('header-class="q-px-sm q-py-xs" dense')
    if group.su_group:
        # The Admin group's heading carries the admin (person-with-gear)
        # marker. Heading text matches the other groups (white).
        with expansion.add_slot("header"):
            with ui.row().classes("items-center w-full no-wrap").style("gap: 0.4rem;"):
                ui.label(group.label).style(
                    "font-size: 0.68rem; font-weight: 700; text-transform: uppercase; "
                    "letter-spacing: 0.08em; color: var(--matofx-body-text);"
                )
                ui.html(_nav_icon_html("user_gear", size=15) or "", sanitize=False).classes(
                    "matofx-nav-lbl"
                ).style("color: var(--matofx-muted-text); margin-left: auto; display: flex;")
    with expansion, ui.column().classes("w-full gap-0"):
        for item in items:
            row = (
                ui.row()
                .classes("items-center w-full no-wrap cursor-pointer rounded matofx-nav-item")
                .style(
                    "padding: 0.35rem 0.6rem; gap: 0.55rem; color: var(--matofx-drawer-item-text);"
                )
                .on("click", lambda _e=None, _t=item.target: ui.navigate.to(_t))
                .tooltip(item.label)
            )
            if active_target is not None and item.target == active_target:
                row.classes("matofx-nav-active")
            with row:
                svg = _nav_icon_html(item.icon, size=18)
                if svg is not None:
                    ui.html(svg, sanitize=False).style(
                        "color: var(--matofx-drawer-item-text); flex: none; display: flex;"
                    )
                else:  # safety net for any unmapped key
                    ui.icon(item.icon).style(
                        "color: var(--matofx-drawer-item-text); font-size: 1.05rem; flex: none;"
                    )
                ui.label(item.label).classes("matofx-nav-lbl").style(
                    "font-size: 0.8rem; color: var(--matofx-drawer-item-text); "
                    "white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1 1 0;"
                )
                if item.su:
                    # Shield = Super-User tier; person-with-gear = Admin
                    # (+SU) tier — replaces the old padlock (home-redesign).
                    tier_key = "shield" if item.super_tier else "user_gear"
                    tier_tip = "Super User only" if item.super_tier else "Admin / Super User only"
                    ui.html(_nav_icon_html(tier_key, size=14) or "", sanitize=False).classes(
                        "matofx-nav-lbl"
                    ).style("color: var(--matofx-muted-text); flex: none; display: flex;").tooltip(
                        tier_tip
                    )


def _install_session_keepalive() -> None:
    """Keep the bearer-token session alive while the operator is active.

    Three pieces co-operate:

    * A client-side activity tracker writes ``window._matofxLastActivity``
      to ``Date.now()`` whenever the user moves the mouse, scrolls,
      taps a key, or touches the screen — throttled to once a second so
      the listener is cheap on busy pages.
    * A server-side :class:`ui.timer` fires every 60 seconds and asks
      the browser how long it has been idle (the age is computed *in
      the browser*, against its own clock — comparing a browser
      ``Date.now()`` against the server's clock would misfire by
      whatever the two disagree by). If the activity is within the
      last ~90 seconds it POSTs ``/api/auth/touch`` to refresh
      ``last_activity_at`` on the session row.
    * The same timer also detects *prolonged* idleness: once the
      client-side idle age passes the configured
      ``session_idle_timeout_minutes``, it revokes the bearer token,
      clears local state, and navigates to ``/login`` so the user
      lands on the sign-in page instead of bumping into 401 errors
      on whatever they try next.
    * A one-shot tick right after the page renders validates the
      stored token against the API. That covers the case the periodic
      timer can't see: a session that expired while the tab was closed
      (or while the browser was asleep and NiceGUI had pruned the
      client), where the browser still holds a token that the server
      has already retired. The 401 goes through
      :func:`_handle_expired_session` → ``/login``.

    Net effect: a user who is actually using the tab keeps their
    session indefinitely (up to the hard timeout), while a user who
    walks away — or comes back to a stale tab — is signed out and
    returned to the login page. The ``passive`` + ``capture`` listener
    flags mean the tracker doesn't interfere with anything the page
    itself wants to do with those events.
    """
    ui.add_head_html(
        "<script>"
        "(function(){"
        "  if (window._matofxActivityHook) return;"
        "  window._matofxActivityHook = true;"
        "  window._matofxLastActivity = Date.now();"
        "  var throttle = 0;"
        "  function bump(){"
        "    var now = Date.now();"
        "    if (now - throttle < 1000) return;"
        "    throttle = now;"
        "    window._matofxLastActivity = now;"
        "  }"
        "  var events = ['mousemove','keydown','click','scroll','touchstart'];"
        "  for (var i = 0; i < events.length; i++) {"
        "    document.addEventListener(events[i], bump,"
        "      {passive: true, capture: true});"
        "  }"
        "})();"
        "</script>"
    )

    heartbeat_seconds = 60.0
    activity_window_ms = 90_000  # heartbeat interval + grace

    async def _heartbeat() -> None:
        if not is_logged_in():
            return
        try:
            # Idle age in ms, measured entirely against the browser's
            # own clock so server/browser clock skew can't sign an
            # active user out early (or keep a walked-away one alive).
            raw_age = await ui.run_javascript(
                "return Math.max(0, Date.now() - (window._matofxLastActivity || Date.now()));"
            )
        except Exception:
            # No client connected, or the JS call timed out. Either
            # way: skip this tick — the next one will retry.
            return
        try:
            age_ms = float(raw_age or 0)
        except (TypeError, ValueError):
            return
        idle_threshold_ms = current_idle_timeout_minutes() * 60_000
        if age_ms >= idle_threshold_ms:
            # Server-side validate() will already be on the brink of
            # rejecting this session; pre-empt the cascading 401s and
            # bounce the user to the sign-in page with a clear notice.
            await _logout_idle()
            return
        if age_ms > activity_window_ms:
            return
        await _touch_session()

    ui.timer(heartbeat_seconds, _heartbeat)
    # One-shot validation of the stored token just after the page
    # renders. A page load *is* activity, so touching here is honest —
    # and if the token died while the tab was shut, the 401 bounces the
    # user to /login before they start filling in a form they'd lose.
    ui.timer(1.0, _touch_session, once=True)


async def _touch_session() -> None:
    """POST ``/api/auth/touch`` with the stored token, ignoring failures.

    Failures are deliberately swallowed: a 401 has already run the
    expired-session hook (which navigates to ``/login``) by the time
    the :class:`ApiError` reaches us, and anything else is a transient
    blip the next heartbeat retries.
    """
    token = current_token()
    if not token:
        return
    client = ApiClient(get_app(), token=token)
    with contextlib.suppress(Exception):
        await client.touch()


async def _logout() -> None:
    """Revoke the bearer token via the API and clear local storage."""
    token = current_token()
    if token:
        client = ApiClient(get_app(), token=token)
        # Even if revoke fails (network blip, expired token, etc.), we
        # still want to drop the local state so the user is signed out
        # from this browser. The session entry will be reaped by the
        # idle-timeout sweep.
        with contextlib.suppress(Exception):
            await client.logout()
    clear_login()
    ui.notify("You've been signed out.", type="info")
    ui.navigate.to("/login")


async def _logout_idle() -> None:
    """Auto-logout path invoked by the keepalive timer on idle expiry.

    Same teardown as :func:`_logout` (revoke + clear local state +
    redirect), but the sign-in page is told *why* the user just landed
    there — via ``?reason=idle``, not a toast: navigating away destroys
    the page the toast lives on, so it would flash at best. The page
    they were on rides along as ``?next=`` so signing back in returns
    them to it.
    """
    token = current_token()
    target = _post_login_target()
    if token:
        client = ApiClient(get_app(), token=token)
        with contextlib.suppress(Exception):
            await client.logout()
    clear_login()
    ui.navigate.to(_login_url(target=target, reason="idle"))


# Any authenticated 401 anywhere in the dashboard lands the user back on
# the sign-in page rather than leaving them on a page whose every call
# now fails. Registered at import (the UI imports this module for the
# shell, so it's always in place before a page renders).
set_unauthenticated_hook(_handle_expired_session)
