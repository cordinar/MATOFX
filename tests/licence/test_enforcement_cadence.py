"""Startup gate, periodic re-check, and feature gating (ADR-0019 slice 3).

Three things, each with a specific way of going wrong:

- **Startup** must refuse to serve a *tampered* licence and must NOT
  refuse a merely missing or expired one.
- **The re-check** must never raise — a licence that changes under a
  running install cannot be allowed to kill jobs mid-flight.
- **Feature gating** must never silently strip features because of a
  licence problem, which is the failure mode that would look like a
  product outage rather than a licensing one.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.audit import ensure_chain_head
from matofx.audit.orm import AuditLog
from matofx.config import FeatureSettings, LicenceSettings, Settings
from matofx.features import Feature, feature_enabled
from matofx.licence import (
    LicenceInvalidError,
    apply_licence_features,
    check_licence_at_startup,
    effective_feature_settings,
    recheck_licence,
    resolve_licence,
)
from tests.licence.conftest import write_licence

_TOML_FEATURES = FeatureSettings(enabled=[Feature.FILE_LIBRARY.value])


# ----- startup gate ---------------------------------------------------


def test_startup_accepts_a_valid_licence(settings: Settings) -> None:
    resolution = check_licence_at_startup(settings)
    assert resolution.status is not None
    assert resolution.status.state == "active"


def test_startup_refuses_a_tampered_licence(
    settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """Fail closed: the process must not serve a request on edited terms."""
    path = write_licence(tmp_path / "bad.json", issuer_private=issuer, tamper=True)
    tampered = settings.model_copy(update={"licence": LicenceSettings(file=path)})
    with pytest.raises(LicenceInvalidError, match="altered since it was signed"):
        check_licence_at_startup(tampered)


def test_startup_tolerates_a_missing_licence(settings: Settings) -> None:
    """A renewal lapse must not stop the site starting (§C16)."""
    none_configured = settings.model_copy(update={"licence": LicenceSettings(file=None)})
    resolution = check_licence_at_startup(none_configured)
    assert resolution.status is None
    assert resolution.read_only is True


def test_startup_tolerates_an_expired_licence(
    settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    past = (_dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(days=400)).isoformat()
    path = write_licence(
        tmp_path / "old.json",
        issuer_private=issuer,
        issued_at="2020-01-01T00:00:00Z",
        expires_at=past.replace("+00:00", "Z"),
    )
    expired = settings.model_copy(update={"licence": LicenceSettings(file=path)})
    resolution = check_licence_at_startup(expired)
    assert resolution.status is not None
    assert resolution.status.state == "expired"
    assert resolution.read_only is True


# ----- periodic re-check ----------------------------------------------


@pytest.mark.asyncio
async def test_recheck_records_every_tick(session: AsyncSession, settings: Settings) -> None:
    """The steady drum of ok-events is itself the evidence a report counts."""
    await ensure_chain_head(session)
    previous = await recheck_licence(session, settings=settings)
    await recheck_licence(session, settings=settings, previous=previous)

    count = (
        await session.execute(
            sa.select(sa.func.count(AuditLog.id)).where(
                AuditLog.action == "licence.validated.ok"
            )
        )
    ).scalar_one()
    assert count == 2


@pytest.mark.asyncio
async def test_recheck_does_not_raise_when_the_licence_goes_bad(
    session: AsyncSession, settings: Settings, licence_file: Path
) -> None:
    """A file edited under a running install must not kill it mid-flight."""
    await ensure_chain_head(session)
    good = await recheck_licence(session, settings=settings)
    assert good.status is not None

    licence_file.write_text("not json at all", encoding="utf-8")
    after = await recheck_licence(session, settings=settings, previous=good)

    assert after.status is None
    assert after.fail_closed is True
    failed = (
        await session.execute(
            sa.select(AuditLog).where(AuditLog.action == "licence.validated.failed")
        )
    ).scalars().one()
    assert "not valid JSON" in (failed.reason or "")


@pytest.mark.asyncio
async def test_recheck_notices_a_replaced_licence(
    session: AsyncSession, settings: Settings, licence_file: Path, issuer: Ed25519PrivateKey
) -> None:
    """Renewing in place is picked up without a restart."""
    await ensure_chain_head(session)
    first = await recheck_licence(session, settings=settings)
    assert first.status is not None
    assert first.status.manifest.n_slots == 10

    write_licence(licence_file, issuer_private=issuer, n_slots=40)
    after = await recheck_licence(session, settings=settings, previous=first)
    assert after.status is not None
    assert after.status.manifest.n_slots == 40


# ----- feature gating -------------------------------------------------


def test_manifest_features_become_authoritative(
    settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    path = write_licence(
        tmp_path / "f.json",
        issuer_private=issuer,
        features={
            Feature.PARAMETRIC_STUDIES.value: True,
            Feature.MATERIALS_DATABASE.value: True,
            Feature.FILE_LIBRARY.value: False,
        },
    )
    licensed = settings.model_copy(
        update={"licence": LicenceSettings(file=path), "features": _TOML_FEATURES}
    )
    applied = apply_licence_features(licensed, resolve_licence(licensed))

    assert feature_enabled(applied.features, Feature.PARAMETRIC_STUDIES)
    assert feature_enabled(applied.features, Feature.MATERIALS_DATABASE)
    # Enabled in TOML, but the licence says no — the licence wins.
    assert not feature_enabled(applied.features, Feature.FILE_LIBRARY)


def test_unknown_manifest_slugs_are_ignored_not_rejected(
    settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """A newer licence must keep working on an older build (§C16)."""
    path = write_licence(
        tmp_path / "f.json",
        issuer_private=issuer,
        features={Feature.PARAMETRIC_STUDIES.value: True, "warp_drive": True},
    )
    licensed = settings.model_copy(update={"licence": LicenceSettings(file=path)})
    applied = apply_licence_features(licensed, resolve_licence(licensed))
    assert applied.features.enabled == [Feature.PARAMETRIC_STUDIES.value]


def test_missing_licence_keeps_the_toml_features(settings: Settings) -> None:
    """A licence lapse already costs read-only; it must not also blank features."""
    none_configured = settings.model_copy(
        update={"licence": LicenceSettings(file=None), "features": _TOML_FEATURES}
    )
    applied = apply_licence_features(none_configured, resolve_licence(none_configured))
    assert applied.features == _TOML_FEATURES


def test_manifest_without_features_keeps_the_toml_features(
    settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """An absent map means "no opinion", not "nothing is licensed"."""
    path = write_licence(tmp_path / "f.json", issuer_private=issuer, features={})
    licensed = settings.model_copy(
        update={"licence": LicenceSettings(file=path), "features": _TOML_FEATURES}
    )
    applied = apply_licence_features(licensed, resolve_licence(licensed))
    assert applied.features == _TOML_FEATURES


def test_all_false_manifest_disables_everything(
    settings: Settings, tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    """A map that is present but turns everything off IS an opinion."""
    path = write_licence(
        tmp_path / "f.json",
        issuer_private=issuer,
        features={Feature.PARAMETRIC_STUDIES.value: False},
    )
    licensed = settings.model_copy(
        update={"licence": LicenceSettings(file=path), "features": _TOML_FEATURES}
    )
    applied = apply_licence_features(licensed, resolve_licence(licensed))
    assert applied.features.enabled == []


def test_apply_is_identity_when_nothing_changes(settings: Settings) -> None:
    """No pointless copy of a frozen Settings tree."""
    none_configured = settings.model_copy(update={"licence": LicenceSettings(file=None)})
    resolution = resolve_licence(none_configured)
    assert apply_licence_features(none_configured, resolution) is none_configured


def test_effective_features_returns_settings_object(settings: Settings) -> None:
    resolution = resolve_licence(settings)
    features = effective_feature_settings(settings, resolution)
    assert isinstance(features, FeatureSettings)
