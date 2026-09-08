"""Read-only mode: one middleware over every mutating route (ADR-0019 slice 6).

Two independent things put a site into read-only mode, and they arrive
here as one answer:

- **The licence** — missing, or expired past its grace period (§C16).
- **The audit chain** — a failed integrity check, until a Super User
  acknowledges it (§C14).

## Why a middleware rather than a dependency

This codebase had no middleware at all before this, so it was a real
choice. A per-route dependency is more precise, but it fails **open**:
the day someone adds a router and forgets the dependency, an unlicensed
install can write again and nobody notices. Gating centrally by HTTP
method fails **closed** — a genuinely read-only-safe POST that isn't on
the allow-list breaks loudly and immediately, which is the error you
want to have.

## What read-only means

The maintainer's rules (ADR-0019 §C12), which are narrower than "block
all writes":

- Every **already-registered** user may sign in and **view results**.
- No new user registration, and no access-rights or role changes.
- No job / study / optimisation submission, no edits.
- Work already running finishes normally — the runner is a separate
  process and is deliberately not gated here.

The allow-list is therefore not "harmless writes" but "the writes that
must keep working for the site to be usable read-only, and for an
administrator to be able to fix the cause".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import FastAPI
    from starlette.requests import Request
    from starlette.responses import Response

_log = logging.getLogger(__name__)

_MUTATING_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Paths that keep working in read-only mode. Prefix-matched, so a
#: trailing id (``/api/admin/sessions/12/revoke``) is covered by its
#: stem. Each entry earns its place:
_ALLOWED_PREFIXES: Final[tuple[str, ...]] = (
    # Signing in and out. §C12 is explicit that every registered user
    # may still sign in and view results; blocking logout would strand
    # them, and under CSL would hold licence places open.
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/touch",
    # Lifting an integrity-failure read-only, which a Super User must be
    # able to do *while* the site is read-only.
    "/api/admin/integrity",
    # Freeing a licence place.
    "/api/admin/sessions",
    # Generating a compliance report. ADR §B9 is explicit that report
    # generation never fails closed — a failing chain check or a broken
    # licence must still produce a report *saying so*, and read-only mode
    # is triggered by exactly those two conditions. Blocking it here
    # would withhold the evidence at the one moment the supplier is
    # certain to ask for it. Listed as the full collection path rather
    # than the `/api/admin/compliance` stem, so a future mutating route
    # elsewhere under compliance is not silently pre-approved.
    "/api/admin/compliance/reports",
    # Per-user display preferences: theme, locale, timezone, sidebar.
    # These are *personal view state*, not governed data — a read-only
    # site that won't let someone switch to dark mode is just broken.
    "/api/auth/me/theme",
    "/api/auth/me/preferences",
    "/api/auth/me/regional",
    "/api/auth/me/ui-preferences",
)
# Deliberately absent: the bare `/api/admin/licence` and
# `/api/admin/compliance` stems. Both were once listed to keep licence
# installation and report generation working while degraded, when
# neither had any mutating HTTP route at all — they exempted nothing
# while silently pre-approving whatever landed beneath them later.
# Licence installation is still CLI-only (`matofx licence install`);
# report generation now has a real route and is listed as the exact
# collection path above. `test_readonly_allowlist.py` fails if the bare
# stems return, or if an exempt prefix stops matching any route.

_READ_ONLY_STATUS: Final[int] = 503


def install_readonly_middleware(app: FastAPI) -> None:
    """Gate mutating requests while the site is read-only."""

    @app.middleware("http")
    async def _readonly_gate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in _MUTATING_METHODS:
            return await call_next(request)
        path = request.url.path
        if any(path.startswith(prefix) for prefix in _ALLOWED_PREFIXES):
            return await call_next(request)

        reason = await _read_only_reason(request)
        if reason is None:
            return await call_next(request)
        _log.info("Refused %s %s — site is read-only (%s)", request.method, path, reason.code)
        return JSONResponse(
            status_code=_READ_ONLY_STATUS,
            content={"detail": reason.message, "read_only_reason": reason.code},
        )


class _Reason:
    """Why the site is read-only, in a shape the response can carry."""

    __slots__ = ("code", "message")

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message


def _licence_enforced(settings: object) -> bool:
    """Whether a licence problem should degrade *this* install.

    An install with **no ``[licence].file`` configured at all** has never
    been licensed — a developer checkout, a test harness, a CI run — and
    making it read-only would mean nothing worked out of the box. An
    install that *has* a configured path whose file is missing or expired
    is a genuine lapse, and is degraded.

    Production closes the loophole from the other end: the config model
    refuses to load ``environment = "prod"`` without ``[licence].file``,
    so "just never configure it" is not a way to run a customer site
    unlicensed for ever.
    """
    licence = getattr(settings, "licence", None)
    configured = getattr(licence, "file", None) is not None
    return configured or getattr(settings, "environment", "") == "prod"


def _licence_reason(resolution: object) -> _Reason:
    """Why the licence blocks writes — lapsed and invalid read differently.

    A licence that does not verify is not an administrative lapse and
    must not read like one: naming it separately is what sends an
    administrator to the *file* rather than to buy a renewal.
    """
    if getattr(resolution, "fail_closed", False):
        return _Reason(
            "licence_invalid",
            getattr(resolution, "problem", None)
            or (
                "The site is read-only because its licence file is not valid — "
                "it has been modified, or was not issued by your supplier. "
                "Restore the licence your supplier sent."
            ),
        )
    return _Reason(
        "licence",
        getattr(resolution, "banner", None)
        or (
            "The site is read-only because its licence is missing or expired. "
            "Existing work can be viewed and downloaded."
        ),
    )


def _foreign_install(request: Request, resolution: object) -> _Reason | None:
    """Refuse writes under a licence issued for a *different* install.

    ADR-0019 §A3 binds a licence to one install, and §16.57 built
    `verify_install_binding` to enforce it — but only at API startup and
    on the runner's hourly tick. Neither is the request path, so copying
    a validly-signed licence belonging to another install over the
    configured file on a **running** server applied its slots and
    features until somebody restarted. Verified: a 10-slot install
    served 50 slots the moment a foreign 50-slot licence was dropped in,
    with no complaint. A multi-install customer has both files already.

    Same shape as the two bypasses already closed — a check that exists
    but does not run where it is needed (§16.57's `environment = "dev"`,
    §16.61's tampered licence at runtime). Comparing a cached id costs a
    string compare, so there is no reason for the request path to be the
    one place that trusts the file.

    Skips when either side is unknown: a manifest with no ``install_id``
    predates binding, and an install with no cached identity has nothing
    to compare — the same early-outs `verify_install_binding` takes, so
    an honest legacy licence is never refused.
    """
    expected = getattr(request.app.state, "install_id", None)
    status = getattr(resolution, "status", None)
    manifest = getattr(status, "manifest", None)
    claimed = getattr(manifest, "install_id", None)
    if not expected or not claimed or claimed == expected:
        return None
    return _Reason(
        "licence_invalid",
        "The site is read-only because its licence file was issued for a different "
        "MATOFX install. A licence belongs to one install and cannot be moved. "
        "Restore this install's own licence.",
    )


async def _read_only_reason(request: Request) -> _Reason | None:
    """``None`` when writable; otherwise why not.

    **The licence half fails closed.** Deciding whether a licence permits
    writes is the whole point of the gate; if that decision itself
    errors, treating the site as writable would let an unlicensed install
    keep working by breaking the check. So a failure to resolve the
    licence is reported as read-only.

    **Everything else fails open.** An unexpected error somewhere in the
    integrity lookup must not turn every write into a 500 — the licence
    startup gate has already refused to serve on tampered material, so
    the residual risk is small and the alternative is a site-wide
    outage caused by a transient database blip.
    """
    from matofx.compliance.integrity import current_status  # noqa: PLC0415
    from matofx.db import session_scope  # noqa: PLC0415
    from matofx.licence import resolve_licence  # noqa: PLC0415

    settings = request.app.state.settings
    try:
        resolution = resolve_licence(settings)
        enforced = _licence_enforced(settings)
    except Exception:
        _log.exception("Could not resolve the licence; refusing writes")
        return _Reason(
            "licence",
            "The site is read-only because its licence could not be checked. Contact "
            "your administrator; the server log records why.",
        )

    try:
        if enforced and (mismatch := _foreign_install(request, resolution)) is not None:
            return mismatch
        if resolution.read_only and enforced:
            return _licence_reason(resolution)
        async with session_scope(request.app.state.factory) as session:
            integrity = await current_status(session)
        if integrity.read_only:
            return _Reason(
                "integrity",
                "The site is read-only because the audit-record integrity check failed. "
                "A Super User must acknowledge it before new work can be submitted.",
            )
    except Exception:
        _log.exception("Could not determine read-only mode; allowing the request")
        return None
    return None
