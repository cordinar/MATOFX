"""Pretty-printing helpers for the UI.

The brief locks two project-wide rules that drive every label / status
text / timestamp rendering in the dashboard:

- **§F30 end-user UX rule** — friendly, plain-English everywhere.
- **§G38 i18n / timezone** — UTC is canonical on the wire and in the
  audit log; user-facing surfaces render the user's locale-time + tz
  abbreviation followed by the UTC equivalent in brackets.

This module is the single seam every UI page goes through to format
those values, so the workspace, job result page, my-jobs list, etc.
all read the same way.

## What lives here

- :func:`format_user_datetime` — UTC ``datetime`` (or ISO-8601 string)
  →
  ::

      08 May 2026 14:32:11 BST (13:32:11 UTC)

  When the display timezone is itself UTC the bracket is suppressed
  (``"08 May 2026 14:32:11 UTC"``) — repeating the same time twice
  reads as a bug.

- :func:`format_job_id` — ``int`` → zero-padded seven-digit string
  (legacy ``f"{id:07d}"`` convention).

- :func:`format_job_state` — closed-vocabulary state code → friendly
  label (``"created"`` → ``"New"``). Tolerates unknown values
  gracefully (title-case fallback) so a state added in a later phase
  doesn't crash the UI before the formatter is updated.

## Timezone source (per §G38)

Per-user timezone preference is a §G38 / Phase 2 feature; it isn't
shipped yet. For now we read the install-default timezone from
``MATOFX_DEFAULT_TIMEZONE`` (env) or fall back to ``"UTC"``. The
seam is :func:`_resolve_display_timezone` — when the per-user
preference layer arrives, it plugs in there with no other call-site
changes.
"""

from __future__ import annotations

import math
import os
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DEFAULT_TZ_ENV_VAR = "MATOFX_DEFAULT_TIMEZONE"

_EMPTY = "—"
"""Em-dash placeholder for missing values; reads cleanly in any locale."""

_FRIENDLY_JOB_STATE: dict[str, str] = {
    # The five v1 job states from :data:`matofx.hierarchy.orm.JOB_STATES`.
    "created": "New",
    "queued": "Queued",
    "running": "Running",
    "complete": "Complete",
    "failed": "Failed",
    # Visible-but-not-actionable states (per the user's Q4 answer):
    # Pending Approval / Denied land with §D21 approvals; running_repeat
    # lands with the legacy AOS-repeat backend. Adding the labels now
    # so the workspace renders them sensibly the moment the data layer
    # surfaces them.
    "pending_approval": "Pending approval",
    "denied": "Denied",
    # Post-approval (results-gate) states, §16.15.
    "pending_results": "Results pending approval",
    "results_withheld": "Results withheld",
    "running_repeat": "Re-running",
    "cancelled": "Cancelled",
}


# --- Timestamps ----------------------------------------------------------


def format_user_datetime(
    value: datetime | str | None,
    *,
    install_tz: str | None = None,
) -> str:
    """Render a UTC timestamp as ``"<local> <tz> (<utc> UTC)"``.

    ``value`` accepts:

    - A timezone-aware ``datetime`` (any zone — converted to UTC then
      to display tz).
    - A naive ``datetime`` (assumed UTC; canonical wire-format storage
      per §G38).
    - An ISO-8601 string (``"2026-05-08T13:32:11+00:00"`` and friends);
      parsed with :meth:`datetime.fromisoformat`.
    - ``None`` → returns ``"—"`` so the caller doesn't need to branch.

    ``install_tz`` is an IANA timezone (``"Europe/London"``,
    ``"Asia/Tokyo"``, …); when omitted the function reads the
    ``MATOFX_DEFAULT_TIMEZONE`` env var, falling back to ``"UTC"``.
    Unknown timezones quietly fall back to UTC — the workspace must
    keep rendering even when the install's config is mid-edit.

    Output examples::

        # install_tz = "Europe/London", value = 2026-05-08 13:32:11 UTC
        "08 May 2026 14:32:11 BST (13:32:11 UTC)"

        # install_tz = "UTC" (or unset)
        "08 May 2026 13:32:11 UTC"

        # value is None
        "—"
    """
    parsed = _coerce_to_aware_utc(value)
    if parsed is None:
        return _EMPTY

    display_zone = _resolve_display_timezone(install_tz)
    local_dt = parsed.astimezone(display_zone)

    # ``%Z`` returns the locale-aware abbreviation: BST/GMT/JST/UTC etc.
    # ``%d %b %Y`` gives the user-requested ``08 May 2026`` shape.
    local_text = local_dt.strftime("%d %b %Y %H:%M:%S %Z")

    # When the display zone is literally UTC the bracket would just
    # repeat the same time + label — suppress it. GMT-in-winter does
    # NOT trigger this: the times match but the labels (``GMT`` vs
    # ``UTC``) are genuinely informative, so we still show both.
    if display_zone.key == "UTC":
        return local_text

    utc_text = parsed.strftime("%H:%M:%S UTC")
    return f"{local_text} ({utc_text})"


# --- Job ID & state ------------------------------------------------------


def format_job_id(value: int | str | None) -> str:
    """``1234`` → ``"0001234"`` (legacy zero-padded seven-digit convention).

    Tolerates string inputs (the API serialises ids as ints but a
    callsite working with raw query-string params may receive a
    string). ``None`` and unparseable inputs return :data:`_EMPTY`.
    """
    if value is None:
        return _EMPTY
    try:
        as_int = int(value)
    except (TypeError, ValueError):
        return _EMPTY
    return f"{as_int:07d}"


def format_job_state(state: str | None) -> str:
    """Closed-vocabulary state code → friendly label.

    Unknown states fall back to title-case (``"weird_value"`` →
    ``"Weird value"``) so a state added in a later phase doesn't crash
    the UI; once we update the mapping the friendly label takes over.
    """
    if not state:
        return _EMPTY
    friendly = _FRIENDLY_JOB_STATE.get(state)
    if friendly is not None:
        return friendly
    return state.replace("_", " ").capitalize()


# --- numbers -------------------------------------------------------------


_DEFAULT_SIG_FIGS = 5
"""Significant figures used when a value has no declared descriptor precision.

Keeps a raw double (``3.141592653589793``) readable (``3.1416``) without
pretending to a fixed decimal count the tool never specified."""

_WHOLE_NUMBER_LIMIT = 1e16
"""Above this magnitude ``int(float)`` loses integer precision, so a whole
value is left to significant-figure formatting rather than shown as an int."""


def format_number(value: Any, *, precision: float | None = None) -> str:
    """Format a numeric value at a sensible length for display.

    The single seam every results table / summary should go through so
    numbers never render as full-precision doubles. When the tool
    descriptor declares a display ``precision`` for the value (a step like
    ``0.01`` → two decimals) that wins ([[feedback_use_labels_not_names]]'s
    numeric cousin — respect the author's intent); otherwise the value is
    trimmed to :data:`_DEFAULT_SIG_FIGS` significant figures. Non-numeric
    input is returned as its string; ``None`` → the em-dash placeholder.
    """
    if value is None:
        return _EMPTY
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(f):
        return str(value)
    if precision is not None and precision > 0:
        decimals = max(0, -math.floor(math.log10(precision)))
        return f"{f:.{decimals}f}"
    if f == int(f) and abs(f) < _WHOLE_NUMBER_LIMIT:
        return str(int(f))  # a whole number reads as an integer, no ".0"
    return f"{f:.{_DEFAULT_SIG_FIGS}g}"


# --- Internals -----------------------------------------------------------


def _coerce_to_aware_utc(value: datetime | str | None) -> datetime | None:
    """Normalise ``value`` to a timezone-aware UTC ``datetime``.

    Returns ``None`` for missing / unparseable input.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # ``fromisoformat`` in 3.11+ understands the trailing ``Z`` form
        # (``"...T13:32:11Z"``) — rewrite earlier-format inputs first
        # so we accept either spelling.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        # Naive → treat as UTC per §G38 ("Stored in DB as UTC").
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _current_user_timezone() -> str | None:
    """The signed-in user's chosen IANA timezone, or ``None``.

    Reads the per-browser preference cached at login (and refreshed when
    the user changes it on their Account page). Returns ``None`` when
    there's no preference (``"auto"`` — inherit the install default) or
    when we're outside a request/client context (background render,
    unit test) where ``app.storage.user`` isn't available.
    """
    try:
        from matofx.ui.state import current_timezone  # noqa: PLC0415 — avoid import cycle

        tz = current_timezone()
    except Exception:  # no client context, or storage unavailable
        return None
    if not tz or tz == "auto":
        return None
    return tz


def _resolve_display_timezone(install_tz: str | None) -> ZoneInfo:
    """Pick the timezone we render times in.

    Resolution order:

    1. Caller-supplied ``install_tz`` argument.
    2. The signed-in user's own timezone (Account page), when set to a
       concrete zone rather than ``"auto"``.
    3. The admin-set ``default_timezone`` General Setting (cached on the
       branding snapshot; ``None`` when the admin hasn't set it).
    4. ``MATOFX_DEFAULT_TIMEZONE`` env var.
    5. ``"UTC"``.

    Unknown timezone identifiers degrade silently to UTC — getting
    the timestamp visible matters more than blocking the page on a
    typo'd config value.
    """
    from matofx.ui.branding_cache import get_branding  # noqa: PLC0415 — avoid import cycle

    candidate = (
        install_tz
        or _current_user_timezone()
        or get_branding().default_timezone
        or os.environ.get(_DEFAULT_TZ_ENV_VAR)
        or "UTC"
    )
    try:
        return ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")
