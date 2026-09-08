"""Tests for the General Settings catalog + service.

Three layers exercised here:

1. Catalog shape — every spec is self-consistent (validated at module
   import by ``SettingSpec.__post_init__`` but pinned down here so a
   regression is loud), every section key exists, all four sections
   are present.
2. ``GeneralSettingsService`` — strict validation, per-row role gate,
   secret redaction on read, secret redaction in the audit payload,
   image-via-``set()`` rejection, unknown-key rejection, reset.
3. ``load_general_settings`` — permissive loader: returns the catalog
   default when no DB row exists, falls back on malformed JSON, picks
   up explicit saves.

The fixtures mirror :mod:`tests.runner.conftest` rather than the
config conftest because the service tests need a real alembic-up DB
with the migrations applied through 0026.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.config import (
    GENERAL_SETTINGS_CATALOG,
    AuthSettings,
    DbSettings,
    GeneralSettingsAccessDeniedError,
    GeneralSettingsService,
    ImageUploadRejectedError,
    InvalidGeneralSettingValueError,
    UnknownGeneralSettingKeyError,
    load_general_settings,
    make_general_settings_service,
)
from matofx.config.general_service import SECRET_REDACTED_SENTINEL
from matofx.db import make_engine, make_session_factory, session_scope
from matofx.tools.exec_orm import RuntimeSetting
from matofx.users import make_password_hasher, make_user_service
from tests._migrated_db import postgres_db_settings, sqlite_db_settings

# ---- DB fixtures --------------------------------------------------------


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ],
)
def gs_db_settings(
    request: pytest.FixtureRequest, tmp_path: Path, migrated_template: Path
) -> Iterator[DbSettings]:
    if request.param == "sqlite":
        # Byte copy of the session template (see tests/_migrated_db.py).
        yield sqlite_db_settings(migrated_template, tmp_path, "gs.db")
        return
    yield from postgres_db_settings()


@pytest.fixture
async def gs_engine(gs_db_settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(gs_db_settings)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def gs_service() -> GeneralSettingsService:
    return make_general_settings_service(AuditWriter(signing_key=None, key_id=None))


async def _seed_super_and_admin(factory) -> tuple[int, int]:
    """Return ``(super_user_id, admin_id)``."""
    auth = AuthSettings(
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        password_min_length=8,
    )
    audit = AuditWriter(signing_key=None, key_id=None)
    user_service = make_user_service(auth, audit, hasher=make_password_hasher(auth))
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        su = await user_service.create_user(
            session,
            username="su",
            email="su@example.com",
            password="superpassword",
            full_name="Super User",
            role="super_user",
        )
        admin = await user_service.create_user(
            session,
            username="adm",
            email="adm@example.com",
            password="adminpassword",
            full_name="Admin",
            role="administrator",
        )
        return su.id, admin.id


# ---- Catalog invariants -------------------------------------------------


def test_catalog_has_all_four_section_groups() -> None:
    """Every documented row exists; the catalog size hasn't drifted."""
    expected = {
        # Section 1: Session & Security
        "session_idle_timeout_minutes",
        "session_hard_timeout_hours",
        "password_min_length",
        "password_require_uppercase",
        "password_require_lowercase",
        "password_require_digit",
        "password_require_symbol",
        "password_allowed_symbols",
        "failed_login_lockout_threshold",
        "failed_login_lockout_minutes",
        "auto_disable_inactive_days",
        "auto_disable_sweep_interval_hours",
        "auto_disable_grace_warning_days",
        "registration_mode",
        "new_account_default_role",
        # Section 2: Notifications
        "job_email_notifications_enabled",
        "smtp_host",
        "smtp_port",
        "smtp_username",
        "smtp_password",
        "use_starttls",
        "smtp_from_address",
        "public_url",
        # Approval notifications: master + per-type switches.
        "licence_alert_emails_enabled",
        "approval_email_notifications_enabled",
        "approval_email_jobs",
        "approval_email_library",
        "approval_email_materials",
        "approval_email_models",
        "approval_email_vv",
        "approval_email_studies",
        "approval_email_optimisation",
        # Section 3: Job housekeeping (no audit-log retention — the audit
        # log is a hash chain, retained in full).
        "retain_draft_jobs_days",
        "retain_job_logs_days",
        "retain_completed_workdirs_days",
        "retain_failed_workdirs_days",
        # Section 4: Branding & UX
        "app_display_title",
        "header_icon_path",
        "favicon_path",
        "support_email_footer",
        "default_timezone",
        "maintenance_banner_text",
        "maintenance_window_iso",
        # Added by later branding/broadcast/favourites features —
        # this vocab guard wasn't updated in lockstep. Added here
        # when the b0-6 full-tree regression surfaced the drift
        # (same lockstep-fix pattern used for the runtime_setting
        # guard in pre-b0 b0-1 — pre-existing, NOT this work's).
        "broadcast_header_text",
        "broadcast_footer_text",
        "broadcast_header_image",
        "broadcast_footer_image",
        "favourites_enabled",
        # Section 5: Data governance (ADR-0006 file library / ADR-0007 materials).
        "library_global_publish_approval",
        "materials_change_approval",
        "models_global_publish_approval",
        # ADR-0010 data lineage (shipped 2026-05-27).
        "lineage_max_nodes",
        # Auto-summaries §6 (added 2026-06-11).
        "llm_summaries_enabled",
    }
    assert set(GENERAL_SETTINGS_CATALOG.keys()) == expected


def test_library_publish_approval_defaults_on_and_super_user_gated() -> None:
    """The global-library approval gate defaults ON and is super-user-only."""
    spec = GENERAL_SETTINGS_CATALOG["library_global_publish_approval"]
    assert spec.kind == "bool"
    assert spec.default is True
    assert spec.requires_role == "super_user"


def test_materials_change_approval_defaults_on_and_super_user_gated() -> None:
    """The materials-change approval gate defaults ON and is super-user-only."""
    spec = GENERAL_SETTINGS_CATALOG["materials_change_approval"]
    assert spec.kind == "bool"
    assert spec.default is True
    assert spec.requires_role == "super_user"


def test_section1_rows_require_super_user() -> None:
    """All Session & Security rows must be super-user-gated."""
    super_user_keys = {
        "session_idle_timeout_minutes",
        "session_hard_timeout_hours",
        "password_min_length",
        "password_require_uppercase",
        "password_require_lowercase",
        "password_require_digit",
        "password_require_symbol",
        "password_allowed_symbols",
        "failed_login_lockout_threshold",
        "failed_login_lockout_minutes",
        "registration_mode",
        "new_account_default_role",
    }
    for key in super_user_keys:
        spec = GENERAL_SETTINGS_CATALOG[key]
        assert spec.requires_role == "super_user", f"{key} should require super_user"


def test_retention_rows_use_zero_means_never() -> None:
    """The housekeeping rows all support the 'never delete' sentinel.

    (No audit-log retention — the audit log is a hash chain, kept in full.)
    """
    retention_keys = (
        "retain_draft_jobs_days",
        "retain_job_logs_days",
        "retain_completed_workdirs_days",
        "retain_failed_workdirs_days",
    )
    for key in retention_keys:
        spec = GENERAL_SETTINGS_CATALOG[key]
        assert spec.kind == "int"
        assert spec.zero_means_never is True
        assert spec.min_value == 0


def test_retention_defaults_match_specification() -> None:
    """drafts=30 days; everything else defaults to 0 (never delete)."""
    assert GENERAL_SETTINGS_CATALOG["retain_draft_jobs_days"].default == 30
    for key in (
        "retain_job_logs_days",
        "retain_completed_workdirs_days",
        "retain_failed_workdirs_days",
    ):
        assert GENERAL_SETTINGS_CATALOG[key].default == 0


def test_new_account_default_role_excludes_super_user() -> None:
    """Defaulting new sign-ups to super-user would be a footgun."""
    spec = GENERAL_SETTINGS_CATALOG["new_account_default_role"]
    assert spec.choices is not None
    assert "super_user" not in spec.choices
    assert "administrator" in spec.choices
    assert spec.default == "regular_user"


# ---- load_general_settings (the permissive loader) ---------------------


@pytest.mark.asyncio
async def test_loader_returns_catalog_defaults_on_empty_db(
    gs_engine: AsyncEngine,
) -> None:
    factory = make_session_factory(gs_engine)
    async with session_scope(factory) as session:
        snapshot = await load_general_settings(session)
    # Every catalog key is present; default values match the spec.
    assert set(snapshot.keys()) == set(GENERAL_SETTINGS_CATALOG.keys())
    assert snapshot["session_idle_timeout_minutes"] == 30
    assert snapshot["job_email_notifications_enabled"] is True
    assert snapshot["retain_job_logs_days"] == 0


@pytest.mark.asyncio
async def test_loader_picks_up_saved_values(
    gs_engine: AsyncEngine, gs_service: GeneralSettingsService
) -> None:
    factory = make_session_factory(gs_engine)
    su_id, _ = await _seed_super_and_admin(factory)
    async with session_scope(factory) as session:
        await gs_service.set(
            session,
            actor_user_id=su_id,
            actor_role="super_user",
            key="session_idle_timeout_minutes",
            value=15,
        )
    async with session_scope(factory) as session:
        snapshot = await load_general_settings(session)
    assert snapshot["session_idle_timeout_minutes"] == 15


@pytest.mark.asyncio
async def test_loader_falls_back_on_malformed_json(gs_engine: AsyncEngine) -> None:
    """A row with broken JSON falls through to the catalog default."""
    factory = make_session_factory(gs_engine)
    async with session_scope(factory) as session:
        session.add(
            RuntimeSetting(
                domain="general",
                key="session_idle_timeout_minutes",
                value_json="{not-valid-json",
            )
        )
        await session.commit()
    async with session_scope(factory) as session:
        snapshot = await load_general_settings(session)
    # Catalog default applies (30), bad row is silently ignored.
    assert snapshot["session_idle_timeout_minutes"] == 30


# ---- GeneralSettingsService.get_all ------------------------------------


@pytest.mark.asyncio
async def test_get_all_redacts_secrets(
    gs_engine: AsyncEngine, gs_service: GeneralSettingsService
) -> None:
    factory = make_session_factory(gs_engine)
    su_id, _ = await _seed_super_and_admin(factory)
    async with session_scope(factory) as session:
        await gs_service.set(
            session,
            actor_user_id=su_id,
            actor_role="administrator",
            key="smtp_password",
            value="hunter2",
        )
    async with session_scope(factory) as session:
        snapshot = await gs_service.get_all(
            session, actor_user_id=su_id, actor_role="administrator"
        )
    assert snapshot["smtp_password"] == SECRET_REDACTED_SENTINEL


# ---- GeneralSettingsService.set: role gating ---------------------------


@pytest.mark.asyncio
async def test_admin_cannot_edit_super_user_only_row(
    gs_engine: AsyncEngine, gs_service: GeneralSettingsService
) -> None:
    factory = make_session_factory(gs_engine)
    _, admin_id = await _seed_super_and_admin(factory)
    async with session_scope(factory) as session:
        with pytest.raises(GeneralSettingsAccessDeniedError):
            await gs_service.set(
                session,
                actor_user_id=admin_id,
                actor_role="administrator",
                key="session_idle_timeout_minutes",
                value=10,
            )


@pytest.mark.asyncio
async def test_super_user_can_edit_super_user_row(
    gs_engine: AsyncEngine, gs_service: GeneralSettingsService
) -> None:
    factory = make_session_factory(gs_engine)
    su_id, _ = await _seed_super_and_admin(factory)
    async with session_scope(factory) as session:
        await gs_service.set(
            session,
            actor_user_id=su_id,
            actor_role="super_user",
            key="session_idle_timeout_minutes",
            value=15,
        )
    async with session_scope(factory) as session:
        snapshot = await gs_service.get_all(session, actor_user_id=su_id, actor_role="super_user")
    assert snapshot["session_idle_timeout_minutes"] == 15


@pytest.mark.asyncio
async def test_admin_can_edit_admin_row(
    gs_engine: AsyncEngine, gs_service: GeneralSettingsService
) -> None:
    factory = make_session_factory(gs_engine)
    _, admin_id = await _seed_super_and_admin(factory)
    async with session_scope(factory) as session:
        await gs_service.set(
            session,
            actor_user_id=admin_id,
            actor_role="administrator",
            key="smtp_host",
            value="smtp.example.com",
        )
    async with session_scope(factory) as session:
        snapshot = await gs_service.get_all(
            session, actor_user_id=admin_id, actor_role="administrator"
        )
    assert snapshot["smtp_host"] == "smtp.example.com"


# ---- GeneralSettingsService.set: validation ---------------------------


@pytest.mark.asyncio
async def test_set_rejects_unknown_key(
    gs_engine: AsyncEngine, gs_service: GeneralSettingsService
) -> None:
    factory = make_session_factory(gs_engine)
    su_id, _ = await _seed_super_and_admin(factory)
    async with session_scope(factory) as session:
        with pytest.raises(UnknownGeneralSettingKeyError):
            await gs_service.set(
                session,
                actor_user_id=su_id,
                actor_role="super_user",
                key="not_a_real_setting",
                value=1,
            )


@pytest.mark.asyncio
async def test_set_rejects_out_of_range_int(
    gs_engine: AsyncEngine, gs_service: GeneralSettingsService
) -> None:
    factory = make_session_factory(gs_engine)
    su_id, _ = await _seed_super_and_admin(factory)
    async with session_scope(factory) as session:
        with pytest.raises(InvalidGeneralSettingValueError):
            await gs_service.set(
                session,
                actor_user_id=su_id,
                actor_role="super_user",
                key="session_idle_timeout_minutes",
                value=99999,
            )


@pytest.mark.asyncio
async def test_set_rejects_unknown_enum_choice(
    gs_engine: AsyncEngine, gs_service: GeneralSettingsService
) -> None:
    factory = make_session_factory(gs_engine)
    su_id, _ = await _seed_super_and_admin(factory)
    async with session_scope(factory) as session:
        with pytest.raises(InvalidGeneralSettingValueError):
            await gs_service.set(
                session,
                actor_user_id=su_id,
                actor_role="super_user",
                key="registration_mode",
                value="maybe",
            )


@pytest.mark.asyncio
async def test_set_rejects_image_via_value_endpoint(
    gs_engine: AsyncEngine, gs_service: GeneralSettingsService
) -> None:
    """Images must use ``set_image``; ``set()`` rejects them with a clear message."""
    factory = make_session_factory(gs_engine)
    _, admin_id = await _seed_super_and_admin(factory)
    async with session_scope(factory) as session:
        with pytest.raises(InvalidGeneralSettingValueError):
            await gs_service.set(
                session,
                actor_user_id=admin_id,
                actor_role="administrator",
                key="header_icon_path",
                value="some.png",
            )


# ---- GeneralSettingsService.reset --------------------------------------


@pytest.mark.asyncio
async def test_reset_restores_default(
    gs_engine: AsyncEngine, gs_service: GeneralSettingsService
) -> None:
    factory = make_session_factory(gs_engine)
    su_id, _ = await _seed_super_and_admin(factory)
    async with session_scope(factory) as session:
        await gs_service.set(
            session,
            actor_user_id=su_id,
            actor_role="super_user",
            key="session_idle_timeout_minutes",
            value=15,
        )
    async with session_scope(factory) as session:
        await gs_service.reset(
            session,
            actor_user_id=su_id,
            actor_role="super_user",
            key="session_idle_timeout_minutes",
        )
    async with session_scope(factory) as session:
        snapshot = await gs_service.get_all(session, actor_user_id=su_id, actor_role="super_user")
    assert snapshot["session_idle_timeout_minutes"] == 30


# ---- GeneralSettingsService.set_image ----------------------------------


@pytest.mark.asyncio
async def test_image_upload_writes_file_and_records_basename(
    gs_engine: AsyncEngine,
    gs_service: GeneralSettingsService,
    tmp_path: Path,
) -> None:
    factory = make_session_factory(gs_engine)
    _, admin_id = await _seed_super_and_admin(factory)
    # 1×1 transparent PNG (smallest valid PNG byte sequence).
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c63000100000005000100"
        "0d0a2db40000000049454e44ae426082"
    )
    branding_dir = tmp_path / "branding"
    async with session_scope(factory) as session:
        basename = await gs_service.set_image(
            session,
            actor_user_id=admin_id,
            actor_role="administrator",
            key="header_icon_path",
            filename="logo.png",
            data=png_bytes,
            mime_type="image/png",
            branding_dir=branding_dir,
            max_bytes=8 * 1024 * 1024,
        )
    assert basename == "header_icon_path.png"
    assert (branding_dir / basename).exists()


@pytest.mark.asyncio
async def test_image_upload_rejects_wrong_mime(
    gs_engine: AsyncEngine,
    gs_service: GeneralSettingsService,
    tmp_path: Path,
) -> None:
    factory = make_session_factory(gs_engine)
    _, admin_id = await _seed_super_and_admin(factory)
    async with session_scope(factory) as session:
        with pytest.raises(ImageUploadRejectedError):
            await gs_service.set_image(
                session,
                actor_user_id=admin_id,
                actor_role="administrator",
                key="favicon_path",
                filename="logo.gif",
                data=b"GIF89a...",
                mime_type="image/gif",  # not in accepted_mime_types for favicon
                branding_dir=tmp_path / "branding",
                max_bytes=2 * 1024 * 1024,
            )


@pytest.mark.asyncio
async def test_image_upload_rejects_oversize(
    gs_engine: AsyncEngine,
    gs_service: GeneralSettingsService,
    tmp_path: Path,
) -> None:
    factory = make_session_factory(gs_engine)
    _, admin_id = await _seed_super_and_admin(factory)
    async with session_scope(factory) as session:
        with pytest.raises(ImageUploadRejectedError):
            await gs_service.set_image(
                session,
                actor_user_id=admin_id,
                actor_role="administrator",
                key="header_icon_path",
                filename="big.png",
                data=b"x" * 10,
                mime_type="image/png",
                branding_dir=tmp_path / "branding",
                max_bytes=5,  # forced low cap
            )


# ---- Audit redaction ---------------------------------------------------


@pytest.mark.asyncio
async def test_secret_redacted_in_audit_payload(
    gs_engine: AsyncEngine, gs_service: GeneralSettingsService
) -> None:
    """The audit row records the redaction sentinel, never the plaintext secret."""
    factory = make_session_factory(gs_engine)
    _, admin_id = await _seed_super_and_admin(factory)
    plaintext = "actual-smtp-password"
    async with session_scope(factory) as session:
        await gs_service.set(
            session,
            actor_user_id=admin_id,
            actor_role="administrator",
            key="smtp_password",
            value=plaintext,
        )
    async with session_scope(factory) as session:
        rows = (
            await session.execute(
                sa.text(
                    "SELECT payload FROM audit_log "
                    "WHERE action = 'general_setting.set' "
                    "AND resource_id = 'smtp_password'"
                )
            )
        ).all()
    assert rows, "expected an audit row for the smtp_password mutation"
    for (payload_raw,) in rows:
        payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
        assert plaintext not in json.dumps(payload), "plaintext secret leaked into audit"
        assert SECRET_REDACTED_SENTINEL in json.dumps(payload)
