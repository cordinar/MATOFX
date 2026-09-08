"""Auth router — login / logout / me + self-service preferences and password.

POST /api/auth/login — verify credentials, mint a session, return a
bearer token. POST /api/auth/logout — revoke the bearer token.
GET /api/auth/me — surface the authenticated user's identity, team
membership, and notification preferences for the Account page.
POST /api/auth/me/preferences — toggle the per-user email opt-in.
POST /api/auth/me/theme — flip the dashboard light/dark theme.
POST /api/auth/me/change-password — self-service password change.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Header, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api.deps import (
    enforce_password_complexity,
    get_current_user,
    get_db,
    get_login_throttle,
    get_notifier,
    get_session_service,
    get_settings,
    get_team_service,
    get_user_service,
)
from matofx.api.schemas import (
    CurrentUserResponse,
    CurrentUserTeamMembership,
    LoginRequest,
    LoginResponse,
    MePasswordChange,
    MePreferencesUpdate,
    MeRegionalSettingsUpdate,
    MeSummaryEnrichmentUpdate,
    MeThemeUpdate,
    MeUiPreferencesUpdate,
)
from matofx.config import Settings
from matofx.licence import resolve_licence
from matofx.licence.errors import LicenceSlotsExhaustedError
from matofx.licence.slots import admit_session, confirm_admission, release_slot
from matofx.notify import Notifier
from matofx.users.errors import InvalidCredentialsError
from matofx.users.login_throttle import IpLoginThrottle
from matofx.users.orm import User
from matofx.users.service import SessionService, UserService
from matofx.users.settings_overlay import effective_session_policy
from matofx.users.team_service import TeamService

router = APIRouter()

_log = logging.getLogger(__name__)

_BEARER_PREFIX = "Bearer "


_ALERT_BUDGET_SECONDS: Final[float] = 5.0
"""Total wall-clock an alert may take before the sign-in gives up on it.

Not per recipient — the whole thing. SMTP's own timeout defaults to 30s
and `notify_licence_alert` walks recipients sequentially, so an
unreachable relay blocked the login for 30s *per administrator*: five
admins, two and a half minutes. Measured, not theorised.

The worst case is the one that matters: a break-glass admission fires an
alert, and that is a Super User trying to reach a site whose licence is
already broken — quite possibly the same outage that took the mail relay
with it. Making them wait minutes to get in, in order to send mail about
the fact they are getting in, inverts the priority completely.
"""


async def _alert_licence(
    notifier: Notifier,
    db: AsyncSession,
    *,
    kind: str,
    summary: str,
    detail: str,
) -> None:
    """Send a licence alert without ever failing or delaying the request.

    Every caller is on the sign-in path. An alert that breaks a login is
    strictly worse than a login nobody was told about, and one that
    *stalls* a login is worse still — a hang reads as an outage, while a
    missing email reads as a missing email.

    Bounded rather than detached: the notifier needs this request's
    session, so handing it to a background task would race the session's
    teardown. A short budget keeps it inside the request without letting
    it own the request's latency.
    """
    try:
        await asyncio.wait_for(
            notifier.notify_licence_alert(db, kind=kind, summary=summary, detail=detail),
            timeout=_ALERT_BUDGET_SECONDS,
        )
    except TimeoutError:
        # The event is still in the audit log and the next compliance
        # report; only the email is lost.
        _log.warning(
            "licence alert timed out after %.0fs — sign-in continued",
            _ALERT_BUDGET_SECONDS,
            extra={"kind": kind},
        )
    except Exception:  # pragma: no cover - defensive
        _log.exception("licence alert failed", extra={"kind": kind})


def _client_ip(request: Request) -> str:
    """Best-effort source IP for rate limiting.

    Uses Starlette's ``request.client.host``, which uvicorn rewrites from
    ``X-Forwarded-For`` only when proxy headers are trusted (run the
    daemon behind a reverse proxy with ``--forwarded-allow-ips``). We
    deliberately don't parse ``X-Forwarded-For`` ourselves — an untrusted
    header is attacker-controlled and would defeat the limit.
    """
    client = request.client
    return client.host if client is not None else "unknown"


@router.post("/login", response_model=LoginResponse, status_code=status.HTTP_200_OK)
async def login(
    body: LoginRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    user_service: Annotated[UserService, Depends(get_user_service)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
    throttle: Annotated[IpLoginThrottle, Depends(get_login_throttle)],
    settings: Annotated[Settings, Depends(get_settings)],
    notifier: Annotated[Notifier, Depends(get_notifier)],
) -> LoginResponse:
    """Verify credentials, create a session, return the bearer token.

    A per-IP failed-login throttle (defence in depth over the per-account
    lockout) blocks an IP that sprays guesses across many usernames.
    """
    client_ip = _client_ip(request)
    throttle.check(client_ip)
    try:
        user = await user_service.authenticate(db, username=body.username, password=body.password)
    except InvalidCredentialsError:
        # Wrong password / unknown / locked user — count it against the IP.
        # A disabled-account error (raised only after a *correct* password)
        # isn't a brute-force signal, so it isn't counted.
        throttle.record_failure(client_ip)
        raise
    throttle.record_success(client_ip)

    # Licence slot gate (ADR-0019 slice 4). Runs *after* the password is
    # verified — so a refusal can never leak whether an account exists —
    # and *before* the session row is created, so a refused login leaves
    # nothing behind. The licence module owns the decision; this router
    # only acts on it (§C16).
    resolution = resolve_licence(settings)
    decision = await admit_session(
        db,
        settings=settings,
        resolution=resolution,
        user=user,
        role_name=await _role_name(db, user),
        audit=request.app.state.audit,
        ip_address=client_ip,
    )
    if not decision.admitted:
        # A refused sign-in is the clearest live signal that a site has
        # outgrown its licence, and until now it was visible only in the
        # audit log and the next compliance report — up to a year away.
        await _alert_licence(
            notifier,
            db,
            kind="slots_exhausted",
            summary=f"a sign-in was refused — all {decision.n_slots} places are in use",
            detail=(
                f"{user.username} could not sign in because every place on this "
                f"licence was occupied ({decision.in_use} of {decision.n_slots}). "
                "Free a place from Active sessions, or talk to your supplier "
                "about more places."
            ),
        )
        raise LicenceSlotsExhaustedError(decision.message or "All places are in use.")

    created = await session_service.create_session(db, user_id=user.id, ip_address=client_ip)
    # The decision above was provisional: counting and inserting are two
    # statements, so concurrent logins at the boundary can all be told
    # there is room. Re-check now the row exists, which may withdraw it.
    decision = await confirm_admission(
        db,
        settings=settings,
        resolution=resolution,
        user=user,
        decision=decision,
        session_id=created.session_id,
        audit=request.app.state.audit,
        ip_address=client_ip,
    )
    if not decision.admitted:
        raise LicenceSlotsExhaustedError(decision.message or "All places are in use.")

    if decision.outcome == "breakglass":
        # §C13 admits a Super User over the cap so nobody is locked out
        # of a site that may need its licence fixing. Unlimited by
        # design — a count cap would lock out the very person it exists
        # to admit — so the bounds are duration (BREAKGLASS_MAX_HOURS)
        # and being *seen*, which is what this alert is for.
        await _alert_licence(
            notifier,
            db,
            kind="breakglass",
            summary=f"{user.username} was admitted over the licence limit",
            detail=(
                f"This site is licensed for {decision.n_slots} places and "
                f"{decision.in_use} were already in use. {user.username} is a Super "
                "User, so MATOFX let them in rather than lock an administrator out "
                "of a site that may need its licence fixing.\n\n"
                "This is intentional and is recorded, but it does mean the site is "
                "currently over its licence. The session ends automatically within "
                "12 hours."
            ),
        )

    # Surface the effective idle-timeout so the dashboard's keepalive
    # timer can redirect to /login proactively rather than letting the
    # user discover the expiry via cascading 401s. Under a CSL licence
    # it is also what returns this slot to the pool when they walk away.
    policy = await effective_session_policy(db, session_service.settings)
    return LoginResponse(
        access_token=created.token,
        expires_at=created.expires_at,
        user_id=created.user_id,
        session_idle_timeout_minutes=policy.idle_timeout_minutes,
        breakglass=decision.outcome == "breakglass",
        # +1 for the session just created; None when unmetered.
        slots_in_use=None if decision.n_slots is None else decision.in_use + 1,
        n_slots=decision.n_slots,
    )


async def _role_name(db: AsyncSession, user: User) -> str:
    """The user's base-role name (``""`` when unset) — for break-glass."""
    from matofx.users.role_orm import Role  # noqa: PLC0415

    if user.role_id is None:
        return ""
    role = await db.get(Role, user.role_id)
    return role.name if role is not None else ""


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    session_service: Annotated[SessionService, Depends(get_session_service)],
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str, Header()],
) -> None:
    """Revoke the bearer token presented in the Authorization header.

    Under a CSL licence this is also what returns the concurrent-session
    slot to the pool, so the release is recorded alongside the
    revocation (ADR-0019 slice 4).
    """
    token = authorization[len(_BEARER_PREFIX) :].strip()
    row = await session_service.peek(db, token=token)
    await session_service.revoke(db, token=token, actor=f"user:{current_user.id}")
    if row is not None:
        await release_slot(
            db,
            audit=request.app.state.audit,
            resolution=resolve_licence(settings),
            user_id=current_user.id,
            session_id=row.id,
            actor=f"user:{current_user.id}",
            reason="signed out",
        )


@router.post("/touch", status_code=status.HTTP_204_NO_CONTENT)
async def touch(
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    """Mark the bearer-token session as recently active.

    Called by the UI's per-minute heartbeat while the operator is
    moving the mouse / typing inside the tab. ``get_current_user``
    runs ``SessionService.validate``, which refreshes
    ``last_activity_at`` as a side effect — that's the whole point of
    this endpoint. The body is empty.

    A request from an expired session returns 401 (the standard
    handler); the UI then surfaces the same "session expired" toast
    the rest of the dashboard uses.
    """


@router.get("/me", response_model=CurrentUserResponse)
async def me(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    team_service: Annotated[TeamService, Depends(get_team_service)],
) -> CurrentUserResponse:
    """Return the authenticated user's identity + teams + preferences + permissions."""
    from matofx.users.permissions import effective_permissions  # noqa: PLC0415
    from matofx.users.role_orm import Role  # noqa: PLC0415

    role_name = "unknown"
    if current_user.role_id is not None:
        role = await db.get(Role, current_user.role_id)
        if role is not None:
            role_name = role.name

    permissions = sorted(await effective_permissions(db, current_user.id))

    # Only approvers can be listed on a tool, so skip the lookup
    # entirely for everyone else.
    approver_tools: list[str] = []
    if current_user.is_approver:
        from matofx.tools.approvers import list_tools_referencing_approver  # noqa: PLC0415

        tools = await list_tools_referencing_approver(db, username=current_user.username)
        approver_tools = sorted(t.name for t in tools)

    teams = await team_service.list_user_teams(db, current_user.id)
    return CurrentUserResponse(
        user_id=current_user.id,
        username=current_user.username,
        email=current_user.email,
        full_name=current_user.full_name,
        first_name=current_user.first_name,
        last_name=current_user.last_name,
        role=role_name,
        permissions=permissions,
        is_approver=current_user.is_approver,
        approver_tools=approver_tools,
        email_notifications_enabled=current_user.email_notifications_enabled,
        summary_enrichment_enabled=current_user.summary_enrichment_enabled,
        theme_preference=current_user.theme_preference,  # type: ignore[arg-type]
        locale_preference=current_user.locale_preference,
        timezone_preference=current_user.timezone_preference,
        ui_preferences=dict(current_user.ui_preferences or {}),
        teams=[CurrentUserTeamMembership(id=t.id, name=t.name) for t in teams],
        created_at=current_user.created_at,
        last_login_at=current_user.last_login_at,
    )


@router.post("/me/preferences", response_model=CurrentUserResponse)
async def update_my_preferences(
    body: MePreferencesUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    user_service: Annotated[UserService, Depends(get_user_service)],
    team_service: Annotated[TeamService, Depends(get_team_service)],
) -> CurrentUserResponse:
    """Update the caller's preferences. Returns the refreshed /me payload."""
    await user_service.set_email_notifications_enabled(
        db,
        user_id=current_user.id,
        enabled=body.email_notifications_enabled,
        actor=f"user:{current_user.id}",
    )
    return await me(db=db, current_user=current_user, team_service=team_service)


@router.post("/me/summary-preferences", response_model=CurrentUserResponse)
async def update_my_summary_preferences(
    body: MeSummaryEnrichmentUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    user_service: Annotated[UserService, Depends(get_user_service)],
    team_service: Annotated[TeamService, Depends(get_team_service)],
) -> CurrentUserResponse:
    """Flip the caller's AI-summary opt-in. Returns the refreshed /me payload."""
    await user_service.set_summary_enrichment_enabled(
        db,
        user_id=current_user.id,
        enabled=body.summary_enrichment_enabled,
        actor=f"user:{current_user.id}",
    )
    return await me(db=db, current_user=current_user, team_service=team_service)


@router.post("/me/theme", response_model=CurrentUserResponse)
async def update_my_theme(
    body: MeThemeUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    user_service: Annotated[UserService, Depends(get_user_service)],
    team_service: Annotated[TeamService, Depends(get_team_service)],
) -> CurrentUserResponse:
    """Flip the caller's dashboard theme. Returns the refreshed /me payload."""
    await user_service.set_theme_preference(
        db,
        user_id=current_user.id,
        theme=body.theme,
        actor=f"user:{current_user.id}",
    )
    return await me(db=db, current_user=current_user, team_service=team_service)


@router.post("/me/regional-settings", response_model=CurrentUserResponse)
async def update_my_regional_settings(
    body: MeRegionalSettingsUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    user_service: Annotated[UserService, Depends(get_user_service)],
    team_service: Annotated[TeamService, Depends(get_team_service)],
) -> CurrentUserResponse:
    """Persist the caller's locale + timezone preference.

    Returns the refreshed /me payload so the client doesn't need a
    follow-up round trip. Validation lives in the service layer:
    unknown locales / timezones surface as a 400 ``ValueError``.
    """
    from fastapi import HTTPException  # noqa: PLC0415

    try:
        await user_service.set_regional_preferences(
            db,
            user_id=current_user.id,
            locale=body.locale,
            timezone=body.timezone,
            actor=f"user:{current_user.id}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await me(db=db, current_user=current_user, team_service=team_service)


@router.post("/me/ui-preferences", response_model=CurrentUserResponse)
async def update_my_ui_preferences(
    body: MeUiPreferencesUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    user_service: Annotated[UserService, Depends(get_user_service)],
    team_service: Annotated[TeamService, Depends(get_team_service)],
) -> CurrentUserResponse:
    """Merge the caller's dashboard-shell preferences (home §9).

    Partial: only the fields the chrome actually changed are sent, so a
    drag-resize writes ``sidebar_width`` alone without disturbing the
    pin / collapse / Quick-Access keys. Returns the refreshed /me
    payload so the client can keep its cached blob in step.
    """
    await user_service.set_ui_preferences(
        db,
        user_id=current_user.id,
        sidebar_pinned=body.sidebar_pinned,
        sidebar_width=body.sidebar_width,
        sidebar_groups_collapsed=body.sidebar_groups_collapsed,
        quick_access_filter=body.quick_access_filter,
        actor=f"user:{current_user.id}",
    )
    return await me(db=db, current_user=current_user, team_service=team_service)


@router.post("/me/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_my_password(
    body: MePasswordChange,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    user_service: Annotated[UserService, Depends(get_user_service)],
) -> None:
    """Self-service password change.

    Verifies the supplied current password before validating and
    storing the new one. Existing sessions are *not* revoked — the
    user keeps their bearer token; the next sign-in elsewhere will
    use the new password.
    """
    await enforce_password_complexity(db, body.new_password)
    await user_service.change_password_self(
        db,
        user_id=current_user.id,
        current_password=body.current_password,
        new_password=body.new_password,
    )
