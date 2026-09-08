"""The signed licence manifest and the states it can be in (brief §C16).

## Shape

```json
{
  "licence": {
    "schema_version": 1,
    "licence_id": "LIC-2026-0043",
    "customer": "Northwind Aerospace Ltd",
    "environment": "production",
    "install_id": "inst_7Q2M4KJ9",
    "licence_type": "csl",
    "n_slots": 10,
    "issued_at": "2026-01-14T00:00:00Z",
    "expires_at": "2027-01-14T00:00:00Z",
    "grace_period_days": 30,
    "features": {"parametric_studies": true},
    "compliance_report_months": 3,
    "machine_binding": "<sha256 hex>",
    "banner_text": null,
    "report_key_seed": "<64 hex>",
    "report_key_id": "<16 hex>"
  },
  "issuer": {"key_id": "funis-2026-01", "alg": "ed25519", "signature": "<hex>"}
}
```

The issuer signature covers the canonical bytes of the ``licence``
object alone, so envelope fields can be added later without
invalidating manifests already issued.

**Forwards-compatible by construction** (§C16): unknown keys inside
``licence`` are preserved and ignored, so a newer manifest still
validates on an older install. ``schema_version`` guards the reverse —
an install refuses a manifest whose *major* shape it cannot read rather
than silently misinterpreting it.

## States, not exceptions

`active → grace → expired` is a timeline every valid licence walks. None
of them is an error: §C16 forbids a hard stop on expiry, so the states
carry ``read_only`` and the enforcement layer (slice 6) acts on it.
Tampering is the separate, fail-closed path — see
:mod:`matofx.licence.errors`.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from matofx.licence.errors import LicenceInvalidError

MANIFEST_SCHEMA_VERSION: Final[int] = 1
"""Highest manifest schema version this build can read."""

LicenceType = Literal["csl", "fal"]
LicenceState = Literal["active", "grace", "expired"]

CSL: Final[str] = "csl"
"""Concurrent Session Licence — N sessions at once."""

FAL: Final[str] = "fal"
"""Floating Access Licence — N access-rights assignments."""

# Deliberately duplicated in :mod:`matofx.compliance.schema`: `licence`
# imports `compliance` (canonical bytes, issuer trust), so importing
# back would be a cycle. Both sides are pinned by the same Literal.

_REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "licence_id",
    "customer",
    "licence_type",
    "n_slots",
    "issued_at",
    "expires_at",
)
_VALID_TYPES: Final[frozenset[str]] = frozenset({"csl", "fal"})
_DEFAULT_GRACE_DAYS: Final[int] = 30

DEFAULT_COMPLIANCE_MONTHS: Final[int] = 3
"""Reporting cadence assumed when a manifest predates the field.

Every licence issued before this existed carries no opinion, and
quarterly is the cadence those agreements were written around. Chosen
here rather than at each call site so there is one answer to "how often,
if nobody said?"."""


@dataclass(frozen=True)
class LicenceManifest:
    """A parsed, structurally-valid licence manifest.

    Structural validity only — :mod:`matofx.licence.loader` decides
    whether it is *trusted* (issuer signature, machine binding), and
    :meth:`evaluate` decides what state it is in right now.
    """

    licence_id: str
    customer: str
    environment: str
    licence_type: LicenceType
    n_slots: int
    issued_at: _dt.datetime
    expires_at: _dt.datetime
    grace_period_days: int
    features: dict[str, bool]
    compliance_report_months: int = DEFAULT_COMPLIANCE_MONTHS
    """How often this customer is expected to produce a compliance report.

    A reporting cadence is a term of the agreement, not a site setting —
    so it is issued with the licence rather than left in the customer's
    own configuration, where an administrator could quietly extend the
    interval that exists to prompt them. ``0`` turns the reminder off,
    which is how a customer who does not want one is shipped.

    It only ever drives a banner. Nothing about MATOFX stops working
    because a report is overdue, and nothing here is enforcement."""

    install_id: str | None = None
    machine_binding: str | None = None
    banner_text: str | None = None
    report_key_seed: str | None = None
    report_key_id: str | None = None
    schema_version: int = MANIFEST_SCHEMA_VERSION
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def grace_ends_at(self) -> _dt.datetime:
        """When read-only mode begins if the licence isn't renewed."""
        return self.expires_at + _dt.timedelta(days=self.grace_period_days)

    def enabled_features(self) -> list[str]:
        """Feature slugs the manifest turns on, sorted."""
        return sorted(slug for slug, on in self.features.items() if on)

    def evaluate(self, now: _dt.datetime) -> LicenceStatus:
        """Where this licence sits on the active → grace → expired timeline."""
        if now < self.expires_at:
            return LicenceStatus(
                state="active",
                manifest=self,
                days_remaining=_days_between(now, self.expires_at),
                read_only=False,
            )
        if now < self.grace_ends_at:
            return LicenceStatus(
                state="grace",
                manifest=self,
                days_remaining=_days_between(now, self.grace_ends_at),
                read_only=False,
            )
        return LicenceStatus(state="expired", manifest=self, days_remaining=0, read_only=True)


@dataclass(frozen=True)
class LicenceStatus:
    """A manifest plus where it stands right now."""

    state: LicenceState
    manifest: LicenceManifest
    days_remaining: int
    read_only: bool

    @property
    def banner(self) -> str | None:
        """Plain-English warning for the dashboard, or ``None`` when active.

        Written for the person who has to act on it: what happened, by
        when, and what stops working. Slice 6 renders it.
        """
        if self.state == "active":
            return None
        if self.state == "grace":
            return (
                f"Your MATOFX licence expired on {self.manifest.expires_at:%d %B %Y}. "
                f"You have {self.days_remaining} day(s) left before the site becomes "
                "read-only. Contact your supplier to renew."
            )
        return (
            f"Your MATOFX licence expired on {self.manifest.expires_at:%d %B %Y} and the "
            "grace period has ended. The site is read-only: existing work can be viewed "
            "and downloaded, but nothing new can be submitted or changed."
        )


def parse_manifest(payload: dict[str, Any], *, source: str) -> LicenceManifest:
    """Validate the ``licence`` object's structure.

    Raises:
        LicenceInvalidError: missing / unusable fields, an unknown
            licence type, a non-positive slot count, or a manifest
            schema version this build cannot read. Every message names
            ``source`` so an operator knows which file to fix.
    """
    version = _int_or_none(payload.get("schema_version"))
    if version is None:
        version = MANIFEST_SCHEMA_VERSION
    if version > MANIFEST_SCHEMA_VERSION:
        raise LicenceInvalidError(
            f"{source}: licence manifest schema version {version} is newer than this "
            f"MATOFX build understands (up to {MANIFEST_SCHEMA_VERSION}). Upgrade MATOFX, "
            "or ask your supplier to re-issue for this version."
        )

    missing = [f for f in _REQUIRED_FIELDS if payload.get(f) in (None, "")]
    if missing:
        raise LicenceInvalidError(
            f"{source}: licence manifest is missing required field(s): "
            f"{', '.join(missing)}. Ask your supplier to re-issue it."
        )

    licence_type = str(payload["licence_type"]).lower()
    if licence_type not in _VALID_TYPES:
        raise LicenceInvalidError(
            f"{source}: unknown licence_type {payload['licence_type']!r} — expected "
            "'csl' (concurrent sessions) or 'fal' (floating access)."
        )

    n_slots = _int_or_none(payload.get("n_slots"))
    if n_slots is None or n_slots < 1:
        raise LicenceInvalidError(
            f"{source}: n_slots must be a positive whole number, got {payload.get('n_slots')!r}."
        )

    grace = _int_or_none(payload.get("grace_period_days"))
    if grace is None:
        grace = _DEFAULT_GRACE_DAYS
    if grace < 0:
        raise LicenceInvalidError(f"{source}: grace_period_days cannot be negative.")

    compliance_months = _int_or_none(payload.get("compliance_report_months"))
    if compliance_months is None:
        compliance_months = DEFAULT_COMPLIANCE_MONTHS
    if compliance_months < 0:
        raise LicenceInvalidError(
            f"{source}: compliance_report_months cannot be negative "
            "(use 0 to turn the reminder off)."
        )

    issued_at = _timestamp(payload["issued_at"], field_name="issued_at", source=source)
    expires_at = _timestamp(payload["expires_at"], field_name="expires_at", source=source)
    if expires_at <= issued_at:
        raise LicenceInvalidError(
            f"{source}: expires_at ({expires_at:%Y-%m-%d}) is not after issued_at "
            f"({issued_at:%Y-%m-%d}). Ask your supplier to re-issue it."
        )

    known = {
        "schema_version",
        "licence_id",
        "customer",
        "environment",
        "install_id",
        "licence_type",
        "n_slots",
        "issued_at",
        "expires_at",
        "grace_period_days",
        "features",
        "compliance_report_months",
        "machine_binding",
        "banner_text",
        "report_key_seed",
        "report_key_id",
    }
    return LicenceManifest(
        licence_id=str(payload["licence_id"]),
        customer=str(payload["customer"]),
        environment=str(payload.get("environment") or "production"),
        licence_type=licence_type,  # type: ignore[arg-type]
        n_slots=n_slots,
        issued_at=issued_at,
        expires_at=expires_at,
        grace_period_days=grace,
        features=_features(payload.get("features"), source=source),
        compliance_report_months=compliance_months,
        install_id=_str_or_none(payload.get("install_id")),
        machine_binding=_str_or_none(payload.get("machine_binding")),
        banner_text=_str_or_none(payload.get("banner_text")),
        report_key_seed=_str_or_none(payload.get("report_key_seed")),
        report_key_id=_str_or_none(payload.get("report_key_id")),
        schema_version=version,
        # Unknown keys are kept, not dropped: they are inside the signed
        # bytes, and a future field must survive a round-trip through an
        # older install without changing what the signature covers.
        extra={k: v for k, v in payload.items() if k not in known},
    )


def _features(value: Any, *, source: str) -> dict[str, bool]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise LicenceInvalidError(f"{source}: 'features' must be a map of slug → true/false.")
    return {str(slug): bool(enabled) for slug, enabled in value.items()}


def _timestamp(value: Any, *, field_name: str, source: str) -> _dt.datetime:
    try:
        parsed = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise LicenceInvalidError(
            f"{source}: {field_name} {value!r} is not an ISO-8601 timestamp."
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.UTC)


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _days_between(now: _dt.datetime, later: _dt.datetime) -> int:
    """Whole days from ``now`` to ``later``, floored at zero."""
    delta = later - now
    return max(0, delta.days + (1 if delta.seconds or delta.microseconds else 0))
