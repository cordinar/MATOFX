"""Per-user UI state helpers.

All state lives in NiceGUI's :data:`app.storage.user` — a per-browser-
session dict that survives reloads as long as the storage cookie is
intact. The bearer token returned by ``POST /api/auth/login`` is the
only thing we keep around between page loads; everything else is
fetched fresh on demand so the UI never shows a stale view of the
hierarchy.
"""

from __future__ import annotations

from nicegui import app

_TOKEN_KEY = "auth_token"  # noqa: S105 — storage-key constant, not a credential
_USER_ID_KEY = "user_id"
_USERNAME_KEY = "username"
_FIRST_NAME_KEY = "first_name"
_ROLE_KEY = "role"
_APPROVER_KEY = "is_approver"
_PERMISSIONS_KEY = "permissions"
_THEME_KEY = "theme"
_LOCALE_KEY = "locale"
_TIMEZONE_KEY = "timezone"
_IDLE_TIMEOUT_KEY = "session_idle_timeout_minutes"
_BREAKGLASS_KEY = "breakglass_notice"
_SIDEBAR_EXPANDED_KEY = "sidebar_expanded_sections"
# Bumped to ``_v2`` so any per-browser blob written during early
# development (notably an unpinned ``sidebar_pinned: false`` left over
# from testing) is orphaned rather than overriding the pinned default.
# The real system of record is the DB (``user.ui_preferences``); this
# per-session mirror is repopulated from it at the next login.
_UI_PREFERENCES_KEY = "ui_preferences_v2"

# Defaults for the dashboard-shell preferences blob (home-redesign §9).
# A missing key in the stored blob falls back to these so the chrome
# renders sensibly before the user has ever touched a control.
_DEFAULT_SIDEBAR_PINNED = True
_DEFAULT_SIDEBAR_WIDTH = 248
_SIDEBAR_MIN_WIDTH = 188
_SIDEBAR_MAX_WIDTH = 460
_DEFAULT_QUICK_ACCESS_FILTER = "all"
_VALID_QUICK_ACCESS_FILTERS: frozenset[str] = frozenset({"tool", "workflow", "all"})

# Conservative fallback used only when the login response is missing the
# field (older API, bad parse). Matches the default in AuthSettings so
# the keepalive timer still does something sensible.
_DEFAULT_IDLE_TIMEOUT_MINUTES = 30

# Roles whose users see the "Admin Space" group in the sidebar.
_ADMIN_ROLES: frozenset[str] = frozenset({"super_user", "administrator"})

_DEFAULT_THEME = "dark"
_VALID_THEMES: frozenset[str] = frozenset({"light", "dark"})

_DEFAULT_LOCALE = "auto"
_DEFAULT_TIMEZONE = "auto"


def store_login(
    token: str,
    user_id: int,
    username: str,
    role: str = "",
    theme: str = _DEFAULT_THEME,
    first_name: str = "",
    locale: str = _DEFAULT_LOCALE,
    timezone: str = _DEFAULT_TIMEZONE,
    is_approver: bool = False,
    idle_timeout_minutes: int = _DEFAULT_IDLE_TIMEOUT_MINUTES,
    permissions: list[str] | None = None,
    ui_preferences: dict[str, object] | None = None,
) -> None:
    """Stash the credentials returned by ``POST /api/auth/login``.

    ``permissions`` is the user's full effective set (a super-user
    receives every slug via the sentinel; everyone else gets the union
    of base-role + capability-role permissions ``/api/auth/me``
    computes). Cached here so the nav chrome can gate items
    synchronously without re-hitting the API on every page render.
    """
    app.storage.user[_TOKEN_KEY] = token
    app.storage.user[_USER_ID_KEY] = user_id
    app.storage.user[_USERNAME_KEY] = username
    app.storage.user[_FIRST_NAME_KEY] = first_name
    app.storage.user[_ROLE_KEY] = role
    app.storage.user[_APPROVER_KEY] = bool(is_approver)
    app.storage.user[_PERMISSIONS_KEY] = list(permissions or [])
    store_ui_preferences(ui_preferences or {})
    store_idle_timeout_minutes(idle_timeout_minutes)
    store_theme(theme)
    store_locale(locale)
    store_timezone(timezone)


def clear_login() -> None:
    """Drop the stored token + identity (used by the logout handler)."""
    for key in (
        _TOKEN_KEY,
        _USER_ID_KEY,
        _USERNAME_KEY,
        _FIRST_NAME_KEY,
        _ROLE_KEY,
        _APPROVER_KEY,
        _PERMISSIONS_KEY,
        _THEME_KEY,
        _LOCALE_KEY,
        _TIMEZONE_KEY,
        _IDLE_TIMEOUT_KEY,
        _BREAKGLASS_KEY,
        _UI_PREFERENCES_KEY,
    ):
        app.storage.user.pop(key, None)


def store_theme(theme: str) -> None:
    """Persist the user's chosen theme into per-browser storage.

    Unknown values fall back to the default so a stale storage cookie
    can't poison the chrome — the value still reaches the API as
    ``theme`` on the next save, and the API's Literal validation will
    reject anything off-list.
    """
    app.storage.user[_THEME_KEY] = theme if theme in _VALID_THEMES else _DEFAULT_THEME


def current_theme() -> str:
    """Return the stored theme — defaults to ``'dark'``."""
    value = app.storage.user.get(_THEME_KEY)
    if isinstance(value, str) and value in _VALID_THEMES:
        return value
    return _DEFAULT_THEME


def current_token() -> str | None:
    """Return the stored bearer token (``None`` when the user isn't logged in)."""
    value = app.storage.user.get(_TOKEN_KEY)
    return value if isinstance(value, str) else None


def current_username() -> str | None:
    """Return the stored username (``None`` when not logged in)."""
    value = app.storage.user.get(_USERNAME_KEY)
    return value if isinstance(value, str) else None


def current_user_id() -> int | None:
    """Return the stored user id (``None`` when not logged in).

    Stored at login alongside the token; read by surfaces that must filter a
    list to what *this* user owns — a project picker offering projects they
    cannot write to only moves the refusal one click later.
    """
    value = app.storage.user.get(_USER_ID_KEY)
    return value if isinstance(value, int) else None


def store_first_name(first_name: str) -> None:
    """Persist the user's first name for the home-page greeting."""
    app.storage.user[_FIRST_NAME_KEY] = first_name or ""


def current_first_name() -> str:
    """Return the stored first name (empty when not set)."""
    value = app.storage.user.get(_FIRST_NAME_KEY)
    return value if isinstance(value, str) else ""


def store_locale(locale: str) -> None:
    """Persist the user's chosen locale (BCP-47 tag or ``'auto'``)."""
    app.storage.user[_LOCALE_KEY] = (locale or _DEFAULT_LOCALE).strip() or _DEFAULT_LOCALE


def current_locale() -> str:
    """Return the stored locale — defaults to ``'auto'``."""
    value = app.storage.user.get(_LOCALE_KEY)
    return value if isinstance(value, str) and value else _DEFAULT_LOCALE


def store_timezone(timezone: str) -> None:
    """Persist the user's chosen timezone (IANA id or ``'auto'``)."""
    app.storage.user[_TIMEZONE_KEY] = (timezone or _DEFAULT_TIMEZONE).strip() or _DEFAULT_TIMEZONE


def current_timezone() -> str:
    """Return the stored timezone — defaults to ``'auto'``."""
    value = app.storage.user.get(_TIMEZONE_KEY)
    return value if isinstance(value, str) and value else _DEFAULT_TIMEZONE


def store_idle_timeout_minutes(minutes: int) -> None:
    """Persist the effective idle-session-timeout policy from the login response.

    Drives the per-tab keepalive timer's proactive logout: once
    client-side idle exceeds this many minutes the chrome forces a
    redirect to ``/login`` instead of waiting for the next user action
    to surface a server-side 401.
    """
    try:
        coerced = int(minutes)
    except (TypeError, ValueError):
        coerced = _DEFAULT_IDLE_TIMEOUT_MINUTES
    if coerced < 1:
        coerced = _DEFAULT_IDLE_TIMEOUT_MINUTES
    app.storage.user[_IDLE_TIMEOUT_KEY] = coerced


def current_idle_timeout_minutes() -> int:
    """Return the stored idle-session-timeout policy (defaults to 30)."""
    value = app.storage.user.get(_IDLE_TIMEOUT_KEY)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return _DEFAULT_IDLE_TIMEOUT_MINUTES


def current_role() -> str:
    """Return the stored role (empty string when not logged in)."""
    value = app.storage.user.get(_ROLE_KEY)
    return value if isinstance(value, str) else ""


def is_super_user() -> bool:
    """``True`` only for the Super User role (excludes Administrator).

    Distinct from :func:`is_admin` (which is Super User *or*
    Administrator): used to gate the Super-User-tier nav entries
    (shield-marked) that an administrator must not see.
    """
    return current_role() == "super_user"


def is_admin() -> bool:
    """``True`` when the logged-in user can see the Admin Space group."""
    return current_role() in _ADMIN_ROLES


def current_is_approver() -> bool:
    """``True`` when the logged-in user carries the approver flag.

    Cached at login (like the role) so the sidebar can gate the
    Approvals link synchronously without a /me round-trip per page.
    The live pending *count* still comes from the API (home card).
    """
    return bool(app.storage.user.get(_APPROVER_KEY, False))


def current_permissions() -> frozenset[str]:
    """The logged-in user's effective permission slugs (cached at login).

    A super-user receives every slug via the API's sentinel; everyone
    else gets the union of their base role + capability roles. Used by
    the nav chrome to gate feature surfaces (File library, Materials,
    …) synchronously — pages still do their own permission check on
    the API side, this is purely for hiding entries the user can't use.
    """
    value = app.storage.user.get(_PERMISSIONS_KEY)
    if isinstance(value, list):
        return frozenset(str(p) for p in value)
    return frozenset()


def has_permission(slug: str) -> bool:
    """``True`` when ``slug`` is in :func:`current_permissions`."""
    return slug in current_permissions()


def is_logged_in() -> bool:
    """Convenience: ``True`` when a bearer token is currently stored."""
    return current_token() is not None


def is_sidebar_section_expanded(label: str, *, default: bool) -> bool:
    """Return the stored expansion state for a sidebar group.

    Falls back to ``default`` when nothing has been stored yet (first
    load after login). Stored as a per-user dict keyed by section label
    so the rail keeps its shape across page navigations.
    """
    sections = app.storage.user.get(_SIDEBAR_EXPANDED_KEY)
    if isinstance(sections, dict):
        value = sections.get(label)
        if isinstance(value, bool):
            return value
    return default


def store_sidebar_section_expanded(label: str, expanded: bool) -> None:
    """Persist whether a sidebar group is currently expanded."""
    sections = app.storage.user.get(_SIDEBAR_EXPANDED_KEY)
    if not isinstance(sections, dict):
        sections = {}
    sections[label] = bool(expanded)
    app.storage.user[_SIDEBAR_EXPANDED_KEY] = sections


# ----- dashboard-shell preferences (home-redesign §9) ----------------
#
# The system of record for these is the server (``user.ui_preferences``,
# loaded into the login response). We mirror the blob into per-browser
# storage so the chrome can read it synchronously on every page render
# without a /me round-trip; the write-through to the API is the chrome's
# job (it calls ``ApiClient.set_ui_preferences`` and then updates this
# mirror). Each accessor falls back to a sensible default when its key
# is absent so a brand-new user (empty blob) still renders correctly.


def store_ui_preferences(prefs: dict[str, object]) -> None:
    """Replace the cached shell-preferences blob (mirror of the server)."""
    app.storage.user[_UI_PREFERENCES_KEY] = dict(prefs) if isinstance(prefs, dict) else {}


def current_ui_preferences() -> dict[str, object]:
    """Return the cached shell-preferences blob (empty dict when unset)."""
    value = app.storage.user.get(_UI_PREFERENCES_KEY)
    return dict(value) if isinstance(value, dict) else {}


def _merge_ui_preference(key: str, value: object) -> None:
    """Write one key into the cached blob, leaving the rest intact."""
    prefs = current_ui_preferences()
    prefs[key] = value
    app.storage.user[_UI_PREFERENCES_KEY] = prefs


def current_sidebar_pinned() -> bool:
    """Whether the sidebar is pinned open (vs. the hover icon-rail)."""
    value = current_ui_preferences().get("sidebar_pinned")
    return bool(value) if isinstance(value, bool) else _DEFAULT_SIDEBAR_PINNED


def store_sidebar_pinned(pinned: bool) -> None:
    """Update the cached pinned flag (server write-through is the caller's)."""
    _merge_ui_preference("sidebar_pinned", bool(pinned))


def current_sidebar_width() -> int:
    """Pinned-rail width in px, clamped to the design's 188-460 range."""
    value = current_ui_preferences().get("sidebar_width")
    if isinstance(value, int) and not isinstance(value, bool):
        return max(_SIDEBAR_MIN_WIDTH, min(_SIDEBAR_MAX_WIDTH, value))
    return _DEFAULT_SIDEBAR_WIDTH


def store_sidebar_width(width: int) -> None:
    """Update the cached rail width (clamped); server write-through is the caller's."""
    clamped = max(_SIDEBAR_MIN_WIDTH, min(_SIDEBAR_MAX_WIDTH, int(width)))
    _merge_ui_preference("sidebar_width", clamped)


def current_quick_access_filter() -> str:
    """Quick Access kind filter — ``'tool'`` / ``'workflow'`` / ``'all'``."""
    value = current_ui_preferences().get("quick_access_filter")
    if isinstance(value, str) and value in _VALID_QUICK_ACCESS_FILTERS:
        return value
    return _DEFAULT_QUICK_ACCESS_FILTER


def store_quick_access_filter(kind: str) -> None:
    """Update the cached Quick Access filter; server write-through is the caller's."""
    if kind in _VALID_QUICK_ACCESS_FILTERS:
        _merge_ui_preference("quick_access_filter", kind)


def is_sidebar_group_collapsed(label: str, *, default: bool = False) -> bool:
    """Return the cached collapse state for a sidebar group.

    Distinct from :func:`is_sidebar_section_expanded` (the legacy
    per-browser expand store): the redesigned grouped sidebar persists
    collapse state server-side in the ``ui_preferences`` blob so it
    follows the user across devices.
    """
    groups = current_ui_preferences().get("sidebar_groups_collapsed")
    if isinstance(groups, dict):
        value = groups.get(label)
        if isinstance(value, bool):
            return value
    return default


def store_sidebar_group_collapsed(label: str, collapsed: bool) -> None:
    """Update one group's cached collapse state; server write-through is the caller's."""
    groups = current_ui_preferences().get("sidebar_groups_collapsed")
    merged = dict(groups) if isinstance(groups, dict) else {}
    merged[label] = bool(collapsed)
    _merge_ui_preference("sidebar_groups_collapsed", merged)


def current_sidebar_groups_collapsed() -> dict[str, bool]:
    """Return the full cached per-group collapse map (may be empty)."""
    groups = current_ui_preferences().get("sidebar_groups_collapsed")
    if isinstance(groups, dict):
        return {str(k): bool(v) for k, v in groups.items() if isinstance(k, str)}
    return {}


def store_breakglass_notice(slots_in_use: int | None, n_slots: int | None) -> None:
    """Remember that this login was admitted **over** the licence cap.

    Stored rather than shown at login time because ``ui.notify`` dies
    with the page that raises it — the notice has to survive the
    navigation into the dashboard, where the chrome renders it as a
    banner and clears it (ADR-0019 §C13).
    """
    app.storage.user[_BREAKGLASS_KEY] = {"in_use": slots_in_use, "n_slots": n_slots}


def take_breakglass_notice() -> dict[str, object] | None:
    """Return the pending break-glass notice **once**, then forget it.

    Read-and-clear: the banner is about the moment of admission, not a
    standing condition, so it must not follow the user around all day.
    """
    value = app.storage.user.pop(_BREAKGLASS_KEY, None)
    return dict(value) if isinstance(value, dict) else None
