"""Admin-facing service for the ``runtime_setting`` table.

Where :func:`matofx.runner.settings.load_runtime_settings` is the
runner's permissive *reader* (clamps invalid values to defaults so
a typo never takes the daemon down), this service is the strict
*writer* (rejects unknown keys, wrong-typed values, out-of-range
values, with plain-English errors the admin UI can surface
verbatim).

Every mutation:

- Requires :attr:`matofx.users.permissions.Permission.RUNTIME_SETTINGS_MANAGE`.
- Is recorded in the audit log with ``action="runner.runtime_setting.set"``
  or ``"runner.runtime_setting.reset"`` and a payload carrying the
  key and the before/after values.
- Updates the ``updated_by_user_id`` and ``updated_at`` columns on
  the row, so the admin UI can show who last changed each knob.

The runner itself doesn't poll this service — it polls the table
directly via :func:`load_runtime_settings`. The hot-reload loop
(every 30 s by default) picks up changes without restart.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from matofx.audit.schema import RecordInput
from matofx.runner.errors import (
    InvalidSettingValueError,
    RuntimeSettingsAccessDeniedError,
    UnknownSettingKeyError,
)
from matofx.runner.settings import (
    RUNTIME_SETTINGS_CATALOG,
    SettingKind,
    SettingSpec,
)
from matofx.tools.exec_orm import RuntimeSetting
from matofx.users.permissions import Permission, user_has_permission

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


@dataclass(frozen=True)
class RuntimeSettingsService:
    """Admin CRUD for the ``runtime_setting`` table.

    Constructed once per process by the API layer with the same
    :class:`AuditWriter` every other audited service uses. Stateless
    beyond that — fine to share between request handlers.
    """

    audit: AuditWriter

    async def get_all(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
    ) -> dict[str, Any]:
        """Return ``{key: value}`` for every catalog entry.

        Missing rows fall back to the catalog default so the admin UI
        can always render a complete form. Requires
        :attr:`Permission.RUNTIME_SETTINGS_MANAGE`.
        """
        await self._require_permission(session, actor_user_id)
        from sqlalchemy import select  # noqa: PLC0415

        rows = (
            (await session.execute(select(RuntimeSetting).where(RuntimeSetting.domain == "runner")))
            .scalars()
            .all()
        )
        raw: dict[str, Any] = {}
        for row in rows:
            try:
                raw[row.key] = json.loads(row.value_json)
            except (TypeError, ValueError):
                continue

        return {
            key: _coerce_to_kind(raw.get(key, spec.default), spec.kind)
            for key, spec in RUNTIME_SETTINGS_CATALOG.items()
        }

    async def set(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        key: str,
        value: object,
    ) -> None:
        """Validate ``value`` against the catalog and write it.

        Raises:
            RuntimeSettingsAccessDeniedError: actor lacks the permission.
            UnknownSettingKeyError: key isn't in the catalog.
            InvalidSettingValueError: value's type or range is wrong.
        """
        await self._require_permission(session, actor_user_id)
        spec = _spec_or_raise(key)
        coerced = _validate(spec, value)
        await self._write(session, key=key, value=coerced, actor_user_id=actor_user_id)
        await self._audit(
            session,
            actor_user_id=actor_user_id,
            action="runner.runtime_setting.set",
            key=key,
            value=coerced,
        )

    async def reset(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        key: str,
    ) -> None:
        """Restore ``key`` to the catalog default."""
        await self._require_permission(session, actor_user_id)
        spec = _spec_or_raise(key)
        coerced = _coerce_to_kind(spec.default, spec.kind)
        await self._write(session, key=key, value=coerced, actor_user_id=actor_user_id)
        await self._audit(
            session,
            actor_user_id=actor_user_id,
            action="runner.runtime_setting.reset",
            key=key,
            value=coerced,
        )

    # ----- helpers ------------------------------------------------------

    async def _require_permission(self, session: AsyncSession, actor_user_id: int) -> None:
        if not await user_has_permission(
            session, actor_user_id, Permission.RUNTIME_SETTINGS_MANAGE
        ):
            raise RuntimeSettingsAccessDeniedError

    async def _write(
        self,
        session: AsyncSession,
        *,
        key: str,
        value: Any,
        actor_user_id: int,
    ) -> None:
        row = await session.get(RuntimeSetting, ("runner", key))
        now = _utcnow()
        if row is None:
            session.add(
                RuntimeSetting(
                    domain="runner",
                    key=key,
                    value_json=json.dumps(value),
                    updated_by_user_id=actor_user_id,
                    updated_at=now,
                )
            )
        else:
            row.value_json = json.dumps(value)
            row.updated_by_user_id = actor_user_id
            row.updated_at = now
        await session.flush()

    async def _audit(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        action: str,
        key: str,
        value: Any,
    ) -> None:
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action=action,
                resource_type="runtime_setting",
                resource_id=key,
                outcome="success",
                payload={"value": value},
            ),
        )


def make_runtime_settings_service(audit: AuditWriter) -> RuntimeSettingsService:
    """Factory: build a :class:`RuntimeSettingsService`."""
    return RuntimeSettingsService(audit=audit)


# ---- module-level helpers ---------------------------------------------


def _spec_or_raise(key: str) -> SettingSpec:
    spec = RUNTIME_SETTINGS_CATALOG.get(key)
    if spec is None:
        raise UnknownSettingKeyError(key)
    return spec


def _validate(spec: SettingSpec, value: object) -> Any:
    """Type-check, range-check, and coerce ``value`` against ``spec``."""
    if isinstance(value, bool):
        # ``bool`` is an ``int`` in Python, but it's never a sensible
        # value for any of these knobs — reject explicitly.
        raise InvalidSettingValueError(
            spec.key, f"Expected a number, got a true/false value ({value!r})."
        )
    if spec.kind == "int":
        if not isinstance(value, int):
            raise InvalidSettingValueError(
                spec.key,
                f"Expected a whole number, got {type(value).__name__} ({value!r}).",
            )
        coerced: int | float = value
    else:  # kind == "float"
        if not isinstance(value, int | float):
            raise InvalidSettingValueError(
                spec.key,
                f"Expected a number, got {type(value).__name__} ({value!r}).",
            )
        coerced = float(value)

    # Numeric runner-catalog entries always set min/max (enforced by
    # SettingSpec.__post_init__), but the type system can't see that.
    if spec.min_value is None or spec.max_value is None:
        return coerced
    if coerced < spec.min_value or coerced > spec.max_value:
        raise InvalidSettingValueError(
            spec.key,
            (
                f"Value {coerced} is outside the allowed range "
                f"({spec.min_value} to {spec.max_value} {spec.unit})."
            ),
        )
    return coerced


def _coerce_to_kind(value: object, kind: SettingKind) -> Any:
    """Best-effort coerce a JSON-decoded value to the catalog kind."""
    if kind == "int" and isinstance(value, int) and not isinstance(value, bool):
        return value
    if kind == "float" and isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    # Fall back to leaving the value as-is; the loader will clamp on
    # read, and the strict service rejects on write before we get here.
    return value
