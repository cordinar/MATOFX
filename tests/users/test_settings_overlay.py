"""Tests for the auth DB-overlay helpers.

The overlay is the seam that makes the General Settings page's
session-timeout / lockout values take effect at runtime. The
critical invariant: when the admin has explicitly saved a value the
DB wins; when no row exists the TOML-loaded :class:`AuthSettings`
baseline wins.

This distinction matters because :func:`load_general_settings` is
permissive (back-fills every key with the catalog default), but the
overlay helpers must NOT use those catalog defaults — otherwise an
install with `AuthSettings(session_idle_timeout_minutes=5)` in
``matofx.toml`` and no admin override would silently use the
catalog's 30-minute default instead of the operator's 5.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.config import AuthSettings, DbSettings
from matofx.db import make_engine, make_session_factory, session_scope
from matofx.tools.exec_orm import RuntimeSetting
from matofx.users import effective_lockout_policy, effective_session_policy
from matofx.users.passwords import validate_password_complexity
from tests._migrated_db import reset_postgres_schema

# ---- DB fixtures (mirror tests/config/test_general_settings.py) --------


def _alembic_config(url: str) -> Config:
    repo_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "src" / "matofx" / "db" / "migrations"))
    cfg.cmd_opts = type("Args", (), {"x": [f"url={url}"]})()
    return cfg


def _sync_url(settings: DbSettings) -> str:
    raw = settings.url.get_secret_value()
    if settings.dialect == "sqlite":
        return raw
    if raw.startswith("postgresql+asyncpg:"):
        return "postgresql:" + raw[len("postgresql+asyncpg:") :]
    if raw.startswith("postgres:"):
        return "postgresql:" + raw[len("postgres:") :]
    return raw


@pytest.fixture(
    params=[
        pytest.param("sqlite", id="sqlite"),
        pytest.param("postgres", id="postgres", marks=pytest.mark.postgres),
    ],
)
def overlay_db_settings(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[DbSettings]:
    dialect: str = request.param
    if dialect == "sqlite":
        settings = DbSettings.from_url(f"sqlite:///{tmp_path / 'o.db'}", dialect="sqlite")
    else:
        pg_url = os.environ.get("MATOFX_TEST_POSTGRES_URL")
        if not pg_url:
            pytest.skip("MATOFX_TEST_POSTGRES_URL not set")
        settings = DbSettings.from_url(pg_url, dialect="postgres")
    if dialect == "postgres":
        reset_postgres_schema(settings)
    command.upgrade(_alembic_config(_sync_url(settings)), "head")
    try:
        yield settings
    finally:
        if dialect == "postgres":
            # Not `downgrade base`: see tests/_migrated_db.py.
            reset_postgres_schema(settings)


@pytest.fixture
async def overlay_engine(overlay_db_settings: DbSettings) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(overlay_db_settings)
    try:
        yield engine
    finally:
        await engine.dispose()


# ---- effective_session_policy ------------------------------------------


@pytest.mark.asyncio
async def test_session_policy_uses_toml_baseline_when_no_override(
    overlay_engine: AsyncEngine,
) -> None:
    """No DB row → TOML defaults win, NOT catalog defaults."""
    factory = make_session_factory(overlay_engine)
    baseline = AuthSettings(
        session_idle_timeout_minutes=5,  # deliberately non-default
        session_hard_timeout_hours=2,
    )
    async with session_scope(factory) as session:
        policy = await effective_session_policy(session, baseline)
    assert policy.idle_timeout_minutes == 5
    assert policy.hard_timeout_hours == 2


@pytest.mark.asyncio
async def test_session_policy_honours_db_override(overlay_engine: AsyncEngine) -> None:
    """A row in ``runtime_setting`` (domain='general') overrides the baseline."""
    factory = make_session_factory(overlay_engine)
    baseline = AuthSettings(session_idle_timeout_minutes=5, session_hard_timeout_hours=2)
    async with session_scope(factory) as session:
        session.add(
            RuntimeSetting(
                domain="general",
                key="session_idle_timeout_minutes",
                value_json=json.dumps(20),
            )
        )
        await session.commit()
    async with session_scope(factory) as session:
        policy = await effective_session_policy(session, baseline)
    # idle was overridden; hard fell back to the TOML baseline.
    assert policy.idle_timeout_minutes == 20
    assert policy.hard_timeout_hours == 2


@pytest.mark.asyncio
async def test_session_policy_ignores_malformed_db_value(
    overlay_engine: AsyncEngine,
) -> None:
    factory = make_session_factory(overlay_engine)
    baseline = AuthSettings(session_idle_timeout_minutes=5, session_hard_timeout_hours=2)
    async with session_scope(factory) as session:
        session.add(
            RuntimeSetting(
                domain="general",
                key="session_idle_timeout_minutes",
                value_json="{not-json",
            )
        )
        await session.commit()
    async with session_scope(factory) as session:
        policy = await effective_session_policy(session, baseline)
    # Falls back to baseline.
    assert policy.idle_timeout_minutes == 5


# ---- effective_lockout_policy ------------------------------------------


@pytest.mark.asyncio
async def test_lockout_policy_uses_baseline_when_no_override(
    overlay_engine: AsyncEngine,
) -> None:
    factory = make_session_factory(overlay_engine)
    baseline = AuthSettings(failed_login_lockout_threshold=3, failed_login_lockout_minutes=60)
    async with session_scope(factory) as session:
        policy = await effective_lockout_policy(session, baseline)
    assert policy.threshold == 3
    assert policy.minutes == 60


@pytest.mark.asyncio
async def test_lockout_policy_honours_db_override(overlay_engine: AsyncEngine) -> None:
    factory = make_session_factory(overlay_engine)
    baseline = AuthSettings(failed_login_lockout_threshold=3, failed_login_lockout_minutes=60)
    async with session_scope(factory) as session:
        session.add(
            RuntimeSetting(
                domain="general",
                key="failed_login_lockout_threshold",
                value_json=json.dumps(10),
            )
        )
        await session.commit()
    async with session_scope(factory) as session:
        policy = await effective_lockout_policy(session, baseline)
    assert policy.threshold == 10
    assert policy.minutes == 60  # baseline


# ---- validate_password_complexity --------------------------------------


def _strict_snapshot() -> dict[str, object]:
    """Catalog-default snapshot: all four classes required, default symbols."""
    return {
        "password_require_uppercase": True,
        "password_require_lowercase": True,
        "password_require_digit": True,
        "password_require_symbol": True,
        "password_allowed_symbols": "!@#$%^&*()-_=+[]{};:,.<>?/\\|~`",
    }


def test_complexity_accepts_compliant_password() -> None:
    assert validate_password_complexity("SecurePass1!", _strict_snapshot()) is None


def test_complexity_rejects_missing_uppercase() -> None:
    err = validate_password_complexity("securepass1!", _strict_snapshot())
    assert err is not None
    assert "uppercase" in err.lower()


def test_complexity_rejects_missing_lowercase() -> None:
    err = validate_password_complexity("SECUREPASS1!", _strict_snapshot())
    assert err is not None
    assert "lowercase" in err.lower()


def test_complexity_rejects_missing_digit() -> None:
    err = validate_password_complexity("SecurePass!", _strict_snapshot())
    assert err is not None
    assert "digit" in err.lower()


def test_complexity_rejects_missing_symbol() -> None:
    err = validate_password_complexity("SecurePass1", _strict_snapshot())
    assert err is not None
    assert "symbol" in err.lower()


def test_complexity_rejects_disallowed_character() -> None:
    """A char not in [a-zA-Z0-9] + allowed_symbols is rejected even when
    every required class is otherwise satisfied."""
    snapshot = _strict_snapshot()
    snapshot["password_allowed_symbols"] = "!"  # narrow set
    err = validate_password_complexity("SecurePass1$", snapshot)
    assert err is not None
    assert "isn't allowed" in err or "not allowed" in err.lower()


def test_complexity_permissive_when_all_classes_disabled() -> None:
    """With every require_* off + a permissive symbol set, any password passes."""
    snapshot = {
        "password_require_uppercase": False,
        "password_require_lowercase": False,
        "password_require_digit": False,
        "password_require_symbol": False,
        "password_allowed_symbols": "!@#",
    }
    assert validate_password_complexity("simple", snapshot) is None
    assert validate_password_complexity("ALLCAPS", snapshot) is None
    assert validate_password_complexity("1234567", snapshot) is None
    # Disallowed char still rejected:
    err = validate_password_complexity("simple$", snapshot)
    assert err is not None
