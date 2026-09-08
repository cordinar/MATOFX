"""DB-overlay accessors for auth-related settings.

The frozen :class:`matofx.config.AuthSettings` carries baseline
values from ``matofx.toml``. The General Settings catalog (super-
user-editable rows in the ``runtime_setting`` table) can override a
subset of them at runtime. Consumers ask this module for the
*effective* value rather than reading ``AuthSettings`` directly so
admin saves take effect without restarting the API.

Cost: one indexed SELECT against the ``runtime_setting`` table per
call. The table is tiny (catalog rows only) and the query is by
composite PK; per-request overhead is well under a millisecond on
both SQLite and Postgres.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.config import AuthSettings


@dataclass(frozen=True)
class EffectiveSessionPolicy:
    """Effective values for the session-timeout knobs.

    Returned by :func:`effective_session_policy`. Carries everything
    :meth:`matofx.users.SessionService.validate` needs to enforce
    idle and hard timeouts.
    """

    idle_timeout_minutes: int
    hard_timeout_hours: int


@dataclass(frozen=True)
class EffectiveLockoutPolicy:
    """Effective values for failed-login lockout.

    Returned by :func:`effective_lockout_policy`. Carries the
    threshold (consecutive failures) and the lockout duration.
    """

    threshold: int
    minutes: int


async def _load_overrides(session: AsyncSession, keys: tuple[str, ...]) -> dict[str, object]:
    """Read explicit DB overrides for the requested keys only.

    Returns a dict containing only keys whose ``runtime_setting`` row
    exists and decodes to a valid JSON value. Unlike
    :func:`matofx.config.load_general_settings` (which back-fills
    every catalog key with the catalog default), this returns *only*
    the rows an admin has actually saved. Lets callers distinguish
    "admin explicitly set this" from "no override exists yet" so the
    TOML baseline can win in the second case.
    """
    import json  # noqa: PLC0415

    from sqlalchemy import select  # noqa: PLC0415

    from matofx.tools.exec_orm import RuntimeSetting  # noqa: PLC0415

    rows = (
        (
            await session.execute(
                select(RuntimeSetting).where(
                    RuntimeSetting.domain == "general",
                    RuntimeSetting.key.in_(keys),
                )
            )
        )
        .scalars()
        .all()
    )
    out: dict[str, object] = {}
    for row in rows:
        try:
            out[row.key] = json.loads(row.value_json)
        except (TypeError, ValueError):
            continue
    return out


async def effective_session_policy(
    session: AsyncSession,
    defaults: AuthSettings,
) -> EffectiveSessionPolicy:
    """Return the effective idle/hard timeout pair.

    When the admin has saved a value on the Admin → General Settings
    page, that value wins; otherwise the TOML-loaded
    :class:`AuthSettings` baseline applies. We deliberately do *not*
    fall back to the catalog default at this seam — the catalog
    default is the value the admin sees when they open the page, but
    until they save it the runtime should keep using whatever the
    operator put in ``matofx.toml`` (which a careful operator may
    have set deliberately).
    """
    overrides = await _load_overrides(
        session, ("session_idle_timeout_minutes", "session_hard_timeout_hours")
    )
    idle = overrides.get("session_idle_timeout_minutes")
    hard = overrides.get("session_hard_timeout_hours")
    return EffectiveSessionPolicy(
        idle_timeout_minutes=int(idle)
        if isinstance(idle, int) and not isinstance(idle, bool)
        else defaults.session_idle_timeout_minutes,
        hard_timeout_hours=int(hard)
        if isinstance(hard, int) and not isinstance(hard, bool)
        else defaults.session_hard_timeout_hours,
    )


async def effective_lockout_policy(
    session: AsyncSession,
    defaults: AuthSettings,
) -> EffectiveLockoutPolicy:
    """Return the effective failed-login lockout policy.

    Same TOML-baseline-vs-explicit-override rule as
    :func:`effective_session_policy`.
    """
    overrides = await _load_overrides(
        session,
        ("failed_login_lockout_threshold", "failed_login_lockout_minutes"),
    )
    threshold = overrides.get("failed_login_lockout_threshold")
    minutes = overrides.get("failed_login_lockout_minutes")
    return EffectiveLockoutPolicy(
        threshold=int(threshold)
        if isinstance(threshold, int) and not isinstance(threshold, bool)
        else defaults.failed_login_lockout_threshold,
        minutes=int(minutes)
        if isinstance(minutes, int) and not isinstance(minutes, bool)
        else defaults.failed_login_lockout_minutes,
    )
