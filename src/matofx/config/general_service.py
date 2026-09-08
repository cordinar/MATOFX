"""Loader and admin service for the General Settings catalog.

Two pieces:

- :func:`load_general_settings` — permissive *reader* that callers
  use to obtain effective values. Builds a ``dict[str, Any]`` keyed
  by every catalog key; missing or malformed DB rows fall back to
  the catalog default. Never raises.
- :class:`GeneralSettingsService` — strict *writer* for the admin
  UI. Validates against the catalog, role-gates each mutation
  against :attr:`SettingSpec.requires_role`, redacts secret values
  on read, and writes one audit record per mutation.

Both consult :data:`matofx.config.general_settings.GENERAL_SETTINGS_CATALOG`
so a typo in either path can't drift from the other.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import select

from matofx.audit.schema import RecordInput
from matofx.config.general_errors import (
    GeneralSettingsAccessDeniedError,
    ImageUploadRejectedError,
    InvalidGeneralSettingValueError,
    UnknownGeneralSettingKeyError,
)
from matofx.config.general_settings import GENERAL_SETTINGS_CATALOG
from matofx.runner.settings import SettingSpec
from matofx.tools.exec_orm import RuntimeSetting
from matofx.users.roles import UserRole, has_role_at_least

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter


SECRET_REDACTED_SENTINEL: Final[str] = "***REDACTED***"  # noqa: S105 - a redaction sentinel, not a secret
"""Value substituted for secret-kind settings on read and in audit payloads."""

_DOMAIN: Final[str] = "general"


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


async def load_general_setting_overrides(session: AsyncSession) -> dict[str, Any]:
    """Return only the keys that have an explicit DB row.

    Unlike :func:`load_general_settings` — which back-fills every
    catalog key with the catalog default — this returns the
    operator-saved overrides exclusively. Consumers that want
    "DB-or-TOML-baseline" semantics (the auth overlay, the email
    notifier) use this so an unset key falls through to whatever
    the operator put in ``matofx.toml`` rather than the catalog
    default.

    Malformed JSON rows are silently skipped (same forgiving
    posture as :func:`load_general_settings`).
    """
    rows = (
        (await session.execute(select(RuntimeSetting).where(RuntimeSetting.domain == _DOMAIN)))
        .scalars()
        .all()
    )
    out: dict[str, Any] = {}
    for row in rows:
        try:
            out[row.key] = json.loads(row.value_json)
        except (TypeError, ValueError):
            continue
    return out


async def load_general_setting_delegations(session: AsyncSession) -> dict[str, str]:
    """Return ``{key: delegated_to_role}`` for rows with an active delegation.

    Keys whose ``runtime_setting.delegated_to_role`` is NULL — or
    whose catalog spec doesn't permit delegation at all — are
    omitted. Used by the role-gate check in
    :meth:`GeneralSettingsService.set` to widen who may write a
    given row, and by the API to surface the delegation state to
    the UI alongside the value snapshot.

    Never raises; an unreachable DB returns an empty map.
    """
    rows = (
        await session.execute(
            select(RuntimeSetting.key, RuntimeSetting.delegated_to_role).where(
                RuntimeSetting.domain == _DOMAIN,
                RuntimeSetting.delegated_to_role.is_not(None),
            )
        )
    ).all()
    out: dict[str, str] = {}
    for row in rows:
        key = str(row[0])
        role = row[1]
        spec = GENERAL_SETTINGS_CATALOG.get(key)
        if spec is None or spec.delegatable_to is None:
            # Stale row — catalog removed the spec, or the spec was
            # downgraded to non-delegatable. Ignore so the runtime
            # gate doesn't honour a delegation the catalog disallows.
            continue
        if not isinstance(role, str):
            continue
        # Defence-in-depth: only honour exactly the role the catalog
        # advertised as delegatable. A stored "regular_user" on a
        # spec that says delegatable_to="administrator" is dropped.
        if role != spec.delegatable_to:
            continue
        out[key] = role
    return out


async def load_general_settings(session: AsyncSession) -> dict[str, Any]:
    """Return ``{key: effective_value}`` for every General Settings key.

    The effective value is the DB override when present and JSON-
    decodable, otherwise the catalog default. Secret values are
    returned **as stored** (not redacted) — this loader is for
    server-side consumers that need the real value to act on. The
    admin-facing :meth:`GeneralSettingsService.get_all` performs the
    redaction.

    Consumers that should fall through to a TOML baseline rather
    than the catalog default (notifier, auth overlay) use
    :func:`load_general_setting_overrides` instead.

    Never raises. A malformed JSON value falls back to the catalog
    default rather than taking the consumer down.
    """
    raw = await load_general_setting_overrides(session)
    return {
        key: _coerce_for_read(raw.get(key, spec.default), spec)
        for key, spec in GENERAL_SETTINGS_CATALOG.items()
    }


def _coerce_for_read(value: object, spec: SettingSpec) -> Any:  # noqa: PLR0911 - a flat dispatch; splitting it would hide the mapping
    """Best-effort coerce a JSON-decoded value to the spec's expected shape.

    A wrong-typed override returns the catalog default so the loader
    is forgiving — admin typos shouldn't crash the auth check or
    the GC job. Strict validation is the admin service's job.
    """
    if spec.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return spec.default
        return _clamp_numeric(value, spec)
    if spec.kind == "float":
        if isinstance(value, bool) or not isinstance(value, int | float):
            return spec.default
        return _clamp_numeric(float(value), spec)
    if spec.kind in ("str", "secret"):
        return value if isinstance(value, str) else spec.default
    if spec.kind == "bool":
        return value if isinstance(value, bool) else spec.default
    if spec.kind == "enum":
        if not isinstance(value, str) or value not in (spec.choices or ()):
            return spec.default
        return value
    if spec.kind == "image":
        # Filename or None. Anything else → fall back to default (None).
        return value if isinstance(value, str) else spec.default
    return spec.default


def _clamp_numeric(value: int | float, spec: SettingSpec) -> int | float:
    if spec.min_value is None or spec.max_value is None:
        return value
    if value < spec.min_value or value > spec.max_value:
        return spec.default if isinstance(spec.default, int | float) else value
    return value


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeneralSettingsService:
    """Admin CRUD for the General Settings catalog.

    Stateless beyond the injected :class:`AuditWriter`. Callers from
    the API layer construct one per request (or share one — both
    are safe).

    Permission model: every mutation requires the actor's role to be
    at least :attr:`SettingSpec.requires_role`. There is no separate
    permission slug — the role threshold IS the gate, and it varies
    per row. Reads are open to any caller the API layer authorises;
    secret values are masked on read regardless.
    """

    audit: AuditWriter

    async def get_all(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        actor_role: UserRole,
    ) -> dict[str, Any]:
        """Return ``{key: value}`` for every catalog entry.

        Secret values are replaced with :data:`SECRET_REDACTED_SENTINEL`
        so the wire shape never carries the real value. Image values
        return the stored basename (or ``None``) — the API tacks on a
        served URL when packaging the response.

        ``actor_user_id`` and ``actor_role`` are accepted but not
        currently used for read-gating (any caller the API layer
        authorises may read). They're plumbed in so a future site-
        level "read requires admin" rule can land without changing
        the call sites.
        """
        del actor_user_id, actor_role  # Reserved for future read gates.

        effective = await load_general_settings(session)

        return {
            key: (
                SECRET_REDACTED_SENTINEL
                if spec.kind == "secret" and effective[key] is not None
                else effective[key]
            )
            for key, spec in GENERAL_SETTINGS_CATALOG.items()
        }

    async def set(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        actor_role: UserRole,
        key: str,
        value: object,
    ) -> None:
        """Validate ``value`` against the catalog and write it.

        Image keys reject through this method — callers must use
        :meth:`set_image` so the file payload is handled properly.

        Honours per-row delegation: when the catalog spec marks the
        setting ``delegatable_to=<role>`` *and* a super-user has set
        ``runtime_setting.delegated_to_role = <role>`` for this key,
        callers of that role bypass the catalog's ``requires_role``
        gate for the value mutation. The delegation itself stays a
        super-user-only action (:meth:`set_delegation`).

        Raises:
            UnknownGeneralSettingKeyError: key isn't in the catalog.
            GeneralSettingsAccessDeniedError: actor's role is too low.
            InvalidGeneralSettingValueError: value's type or range is wrong.
        """
        spec = _spec_or_raise(key)
        delegated_to = await self._read_delegation(session, key=key, spec=spec)
        _require_role_for_set(spec, actor_role, delegated_to)
        if spec.kind == "image":
            raise InvalidGeneralSettingValueError(
                key,
                "image settings must be updated via the image upload endpoint, "
                "not the value endpoint.",
            )
        coerced = _validate(spec, value)
        await self._write(session, key=key, value=coerced, actor_user_id=actor_user_id)
        await self._audit(
            session,
            actor_user_id=actor_user_id,
            action="general_setting.set",
            spec=spec,
            value=coerced,
        )

    async def reset(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        actor_role: UserRole,
        key: str,
    ) -> None:
        """Restore ``key`` to the catalog default.

        Image keys are also resettable — clears the DB override so
        the bundled default applies. The on-disk uploaded file isn't
        deleted (kept in case the admin re-applies the same upload);
        a future housekeeping task can prune orphaned files.

        Honours the same per-row delegation as :meth:`set` so a
        delegated administrator can revert a value they were
        permitted to change.
        """
        spec = _spec_or_raise(key)
        delegated_to = await self._read_delegation(session, key=key, spec=spec)
        _require_role_for_set(spec, actor_role, delegated_to)
        await self._write(session, key=key, value=spec.default, actor_user_id=actor_user_id)
        await self._audit(
            session,
            actor_user_id=actor_user_id,
            action="general_setting.reset",
            spec=spec,
            value=spec.default,
        )

    async def set_delegation(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        actor_role: UserRole,
        key: str,
        delegated_to_role: str | None,
    ) -> None:
        """Grant or revoke per-row delegation. Super-user only.

        Pass ``delegated_to_role=None`` to revoke. The role string
        must match the catalog spec's :attr:`SettingSpec.delegatable_to`
        exactly; any other value is rejected so the delegation can't
        widen beyond what the catalog advertises.

        Audited as ``general_setting.delegate`` (grant or revoke
        recorded in the payload) so an external reviewer can trace
        every authority change.

        Raises:
            UnknownGeneralSettingKeyError: key isn't in the catalog.
            GeneralSettingsAccessDeniedError: actor isn't a super-user,
                or the catalog spec isn't delegatable, or the
                ``delegated_to_role`` doesn't match what the spec permits.
        """
        spec = _spec_or_raise(key)
        # Only super-users grant / revoke delegation. has_role_at_least
        # is hierarchical so super_user >= super_user → True; everyone
        # else is denied.
        if not has_role_at_least(actor_role, "super_user"):
            raise GeneralSettingsAccessDeniedError(spec.key, "super_user")
        if spec.delegatable_to is None:
            raise GeneralSettingsAccessDeniedError(spec.key, "super_user")
        if delegated_to_role is not None and delegated_to_role != spec.delegatable_to:
            raise GeneralSettingsAccessDeniedError(spec.key, spec.delegatable_to)

        await self._write_delegation(
            session,
            key=key,
            delegated_to_role=delegated_to_role,
            actor_user_id=actor_user_id,
        )
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="general_setting.delegate",
                resource_type="general_setting",
                resource_id=spec.key,
                outcome="success",
                payload={
                    "delegated_to_role": delegated_to_role,
                    "delegatable_to": spec.delegatable_to,
                },
            ),
        )

    async def set_image(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        actor_role: UserRole,
        key: str,
        filename: str,
        data: bytes,
        mime_type: str,
        branding_dir: Path,
        max_bytes: int,
    ) -> str:
        """Validate an uploaded image, write the bytes to disk, and store the basename.

        ``branding_dir`` is the configured upload root — files land
        at ``<branding_dir>/<key>.<ext>`` where ``ext`` is derived
        from ``filename``. The DB row stores just the resulting
        basename so the system stays portable across mounts.

        Returns the stored basename (e.g. ``"header_icon_path.png"``).

        Raises:
            UnknownGeneralSettingKeyError: key isn't in the catalog.
            GeneralSettingsAccessDeniedError: actor's role is too low.
            InvalidGeneralSettingValueError: key isn't an image kind.
            ImageUploadRejectedError: mime or size validation failed.
        """
        spec = _spec_or_raise(key)
        delegated_to = await self._read_delegation(session, key=key, spec=spec)
        _require_role_for_set(spec, actor_role, delegated_to)
        if spec.kind != "image":
            raise InvalidGeneralSettingValueError(
                key, "this setting isn't an image; use the standard set endpoint."
            )

        if spec.accepted_mime_types and mime_type not in spec.accepted_mime_types:
            allowed = ", ".join(spec.accepted_mime_types)
            raise ImageUploadRejectedError(
                key,
                f"got mime type {mime_type!r}; expected one of: {allowed}.",
            )
        if len(data) > max_bytes:
            raise ImageUploadRejectedError(
                key,
                f"upload is {len(data):,} bytes; cap is {max_bytes:,} bytes.",
            )
        if len(data) == 0:
            raise ImageUploadRejectedError(key, "upload is empty.")

        ext = _safe_extension(filename, mime_type)
        basename = f"{key}{ext}"
        target = branding_dir / basename
        branding_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

        await self._write(session, key=key, value=basename, actor_user_id=actor_user_id)
        await self._audit(
            session,
            actor_user_id=actor_user_id,
            action="general_setting.image_upload",
            spec=spec,
            value=basename,
        )
        return basename

    # ----- helpers ------------------------------------------------------

    async def _write(
        self,
        session: AsyncSession,
        *,
        key: str,
        value: Any,
        actor_user_id: int,
    ) -> None:
        row = await session.get(RuntimeSetting, (_DOMAIN, key))
        now = _utcnow()
        if row is None:
            session.add(
                RuntimeSetting(
                    domain=_DOMAIN,
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

    async def _write_delegation(
        self,
        session: AsyncSession,
        *,
        key: str,
        delegated_to_role: str | None,
        actor_user_id: int,
    ) -> None:
        """Persist a per-row delegation grant on ``runtime_setting``.

        Creates a placeholder row carrying just the delegation if no
        row exists yet (the key still falls back to the catalog
        default for ``value_json`` — we serialise ``spec.default`` so
        the row is well-formed and the value side of the catalog
        still reads the default value). Updates the existing row
        otherwise. ``delegated_to_role=None`` clears the grant.
        """
        row = await session.get(RuntimeSetting, (_DOMAIN, key))
        now = _utcnow()
        if row is None:
            spec = _spec_or_raise(key)
            session.add(
                RuntimeSetting(
                    domain=_DOMAIN,
                    key=key,
                    value_json=json.dumps(spec.default),
                    delegated_to_role=delegated_to_role,
                    updated_by_user_id=actor_user_id,
                    updated_at=now,
                )
            )
        else:
            row.delegated_to_role = delegated_to_role
            row.updated_by_user_id = actor_user_id
            row.updated_at = now
        await session.flush()

    async def _read_delegation(
        self,
        session: AsyncSession,
        *,
        key: str,
        spec: SettingSpec,
    ) -> str | None:
        """Read the persisted delegation grant for ``key``.

        Returns the stored ``delegated_to_role`` only when the catalog
        spec permits delegation *and* the stored value matches the
        permitted role exactly — defence-in-depth against a catalog
        downgrade that left a stale delegation in the DB.
        """
        if spec.delegatable_to is None:
            return None
        row = await session.get(RuntimeSetting, (_DOMAIN, key))
        if row is None or row.delegated_to_role is None:
            return None
        if row.delegated_to_role != spec.delegatable_to:
            return None
        return row.delegated_to_role

    async def _audit(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        action: str,
        spec: SettingSpec,
        value: Any,
    ) -> None:
        # Redact secrets in the audit payload — the audit log itself
        # is integrity-protected but operators may inspect it freely
        # via the Admin → Activity page, and a stored secret in there
        # would defeat the redaction we apply to GETs.
        payload_value: Any = (
            SECRET_REDACTED_SENTINEL if spec.kind == "secret" and value is not None else value
        )
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action=action,
                resource_type="general_setting",
                resource_id=spec.key,
                outcome="success",
                payload={"value": payload_value},
            ),
        )


def make_general_settings_service(audit: AuditWriter) -> GeneralSettingsService:
    """Factory: build a :class:`GeneralSettingsService`."""
    return GeneralSettingsService(audit=audit)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _spec_or_raise(key: str) -> SettingSpec:
    spec = GENERAL_SETTINGS_CATALOG.get(key)
    if spec is None:
        raise UnknownGeneralSettingKeyError(key)
    return spec


def _require_role(spec: SettingSpec, actor_role: UserRole) -> None:
    """Strict baseline role gate. Used for paths that ignore delegation."""
    if not has_role_at_least(actor_role, spec.requires_role):
        raise GeneralSettingsAccessDeniedError(spec.key, spec.requires_role)


def _require_role_for_set(
    spec: SettingSpec,
    actor_role: UserRole,
    delegated_to: str | None,
) -> None:
    """Delegation-aware role gate for value mutations.

    Passes when *any* of these hold:

    * The actor is at or above the catalog spec's ``requires_role``
      (the usual hierarchy check).
    * The setting is delegatable, the row carries an active
      delegation, and the actor is at or above the delegated role.

    The delegation can only widen access, never narrow it — a
    super-user always retains the right to mutate any setting
    because they're above every other role in the rank order.
    """
    if has_role_at_least(actor_role, spec.requires_role):
        return
    if (
        spec.delegatable_to is not None
        and delegated_to is not None
        and delegated_to == spec.delegatable_to
        and has_role_at_least(actor_role, spec.delegatable_to)
    ):
        return
    raise GeneralSettingsAccessDeniedError(spec.key, spec.requires_role)


def _validate(spec: SettingSpec, value: object) -> Any:
    """Type-check, range-check, and coerce ``value`` against ``spec``.

    Returns the value the DB row should store. Raises
    :class:`InvalidGeneralSettingValueError` with a plain-English
    reason when the value fails validation; the message is safe to
    render directly in the admin UI.
    """
    if spec.kind == "int":
        return _validate_int(spec, value)
    if spec.kind == "float":
        return _validate_float(spec, value)
    if spec.kind == "bool":
        return _validate_bool(spec, value)
    if spec.kind == "str":
        return _validate_str(spec, value)
    if spec.kind == "secret":
        return _validate_secret(spec, value)
    if spec.kind == "enum":
        return _validate_enum(spec, value)
    # Image kinds are rejected earlier in set(); this branch is unreachable
    # in practice but keeps mypy happy on the return-type analysis.
    raise InvalidGeneralSettingValueError(
        spec.key, f"unsupported kind '{spec.kind}' for value mutation."
    )


def _validate_int(spec: SettingSpec, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidGeneralSettingValueError(
            spec.key,
            f"expected a whole number, got {type(value).__name__} ({value!r}).",
        )
    if spec.min_value is not None and value < spec.min_value:
        raise InvalidGeneralSettingValueError(
            spec.key,
            f"value {value} is below the minimum {int(spec.min_value)} {spec.unit}.".rstrip(),
        )
    if spec.max_value is not None and value > spec.max_value:
        raise InvalidGeneralSettingValueError(
            spec.key,
            f"value {value} is above the maximum {int(spec.max_value)} {spec.unit}.".rstrip(),
        )
    return value


def _validate_float(spec: SettingSpec, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidGeneralSettingValueError(
            spec.key,
            f"expected a number, got {type(value).__name__} ({value!r}).",
        )
    coerced = float(value)
    if spec.min_value is not None and coerced < spec.min_value:
        raise InvalidGeneralSettingValueError(
            spec.key,
            f"value {coerced} is below the minimum {spec.min_value} {spec.unit}.".rstrip(),
        )
    if spec.max_value is not None and coerced > spec.max_value:
        raise InvalidGeneralSettingValueError(
            spec.key,
            f"value {coerced} is above the maximum {spec.max_value} {spec.unit}.".rstrip(),
        )
    return coerced


def _validate_bool(spec: SettingSpec, value: object) -> bool:
    if not isinstance(value, bool):
        raise InvalidGeneralSettingValueError(
            spec.key,
            f"expected true or false, got {type(value).__name__} ({value!r}).",
        )
    return value


def _validate_str(spec: SettingSpec, value: object) -> str:
    if not isinstance(value, str):
        raise InvalidGeneralSettingValueError(
            spec.key,
            f"expected text, got {type(value).__name__} ({value!r}).",
        )
    return value


def _validate_secret(spec: SettingSpec, value: object) -> str | None:
    # Empty string / None both clear the secret. Anything else must be a string.
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise InvalidGeneralSettingValueError(
            spec.key,
            f"expected text, got {type(value).__name__}.",
        )
    return value


def _validate_enum(spec: SettingSpec, value: object) -> str:
    if not isinstance(value, str):
        raise InvalidGeneralSettingValueError(
            spec.key,
            f"expected one of {list(spec.choices or ())}, got {type(value).__name__}.",
        )
    if spec.choices and value not in spec.choices:
        raise InvalidGeneralSettingValueError(
            spec.key,
            f"'{value}' isn't one of the allowed choices: {', '.join(spec.choices)}.",
        )
    return value


_MIME_EXTENSIONS: Final[dict[str, str]] = {
    "image/png": ".png",
    "image/x-icon": ".ico",
    "image/svg+xml": ".svg",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
}


def _safe_extension(filename: str, mime_type: str) -> str:
    """Choose a safe file extension based on mime type, falling back to filename.

    Prefers mime-derived extension so a misnamed upload (e.g. a PNG
    uploaded as ``logo.bin``) still gets a sensible file on disk.
    Falls back to the filename's own extension only when the mime
    type isn't in our known list.
    """
    by_mime = _MIME_EXTENSIONS.get(mime_type)
    if by_mime:
        return by_mime
    suffix = Path(filename).suffix.lower()
    return suffix if suffix else ""
