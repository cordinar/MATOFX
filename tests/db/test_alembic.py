"""End-to-end Alembic tests on each available dialect.

Validates that ``alembic upgrade head`` and ``alembic downgrade base``
both succeed and are idempotent. Phase 0 has only a no-op baseline
migration; the test still exercises the full Alembic pipe so the wiring
is proven before real migrations land in Phase 1.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from matofx.config import DbSettings


def _alembic_config(url: str) -> Config:
    repo_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "src" / "matofx" / "db" / "migrations"))
    cfg.cmd_opts = type("Args", (), {"x": [f"url={url}"]})()
    return cfg


def _sync_url(settings: DbSettings) -> str:
    raw = settings.url.get_secret_value()
    if settings.dialect == "sqlite":
        if raw.startswith("sqlite+aiosqlite:"):
            return "sqlite:" + raw[len("sqlite+aiosqlite:") :]
        return raw
    if raw.startswith("postgresql+asyncpg:"):
        return "postgresql:" + raw[len("postgresql+asyncpg:") :]
    if raw.startswith("postgres:"):
        return "postgresql:" + raw[len("postgres:") :]
    return raw


def test_alembic_upgrade_and_downgrade(db_settings: DbSettings) -> None:
    cfg = _alembic_config(_sync_url(db_settings))
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    # Idempotent re-up.
    command.upgrade(cfg, "head")


def test_migrating_does_not_disable_application_loggers(db_settings: DbSettings) -> None:
    """Running a migration must not switch the app's logging off.

    Alembic's ``env.py`` calls ``fileConfig``, whose ``disable_existing_
    loggers`` defaults to **True** — which disables every logger already
    created and not named in ``alembic.ini``, i.e. the whole ``matofx.*``
    tree. That matters in-process: ``matofx migrate`` and the demo
    launcher's auto-upgrade both migrate inside a process that has
    already imported the application, so a migration silently took its
    logging out for the remainder of the run.

    It first showed up as a UI test that passed alone and failed after
    anything that migrated a database, which looked like an ordering
    quirk in the test rather than the application-visible defect it was.
    Asserting the invariant here puts the check where the cause is.
    """
    import logging  # noqa: PLC0415

    probe = logging.getLogger("matofx.ui.app")
    probe.warning("pre-migration probe")  # ensure the logger exists
    assert probe.disabled is False

    command.upgrade(_alembic_config(_sync_url(db_settings)), "head")

    assert probe.disabled is False, (
        "alembic's fileConfig disabled the application's loggers — pass "
        "disable_existing_loggers=False in src/matofx/db/migrations/env.py"
    )
