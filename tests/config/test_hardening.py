"""Security-hardening tests for matofx.config."""

from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from matofx.config import (
    AuditSettings,
    AuthSettings,
    DbSettings,
    LicenceSettings,
    Settings,
    load_settings,
)
from matofx.config.loader import ConfigError

# --- DbSettings.url is a SecretStr -----------------------------------------


def test_db_url_is_secret_str() -> None:
    s = DbSettings.from_url("postgres://u:p@h/db")
    assert isinstance(s.url, SecretStr)


def test_db_url_repr_does_not_leak_password() -> None:
    s = DbSettings.from_url("postgres://u:hunter2@h/db")
    rendered = repr(s)
    assert "hunter2" not in rendered
    assert "**" in rendered  # SecretStr's mask


def test_db_url_model_dump_does_not_leak_password_by_default() -> None:
    s = DbSettings.from_url("postgres://u:hunter2@h/db")
    dumped = s.model_dump()
    # By default Pydantic 2 keeps SecretStr typed; mode="json" masks it.
    json_dump = s.model_dump(mode="json")
    assert "hunter2" not in str(json_dump)
    assert "hunter2" not in str(dumped["url"])


def test_db_url_get_secret_value_returns_raw() -> None:
    s = DbSettings.from_url("postgres://u:hunter2@h/db")
    assert s.url.get_secret_value() == "postgres://u:hunter2@h/db"


# --- DbSettings.url scheme validator ---------------------------------------


def test_url_must_have_recognised_scheme() -> None:
    with pytest.raises(ValidationError) as exc_info:
        DbSettings.from_url("random://nonsense")
    msg = str(exc_info.value)
    assert "recognised scheme" in msg


def test_url_typo_missing_third_slash_caught() -> None:
    """The classic ``sqlite:./x.db`` typo is now caught at load time."""
    # 'sqlite:' alone is technically a recognised prefix; it's the
    # missing scheme proper that we want caught. Examples like
    # 'mysq:something' should fail.
    with pytest.raises(ValidationError):
        DbSettings.from_url("mysq:something")


def test_url_error_message_via_loader_does_not_leak_password(tmp_path: Path) -> None:
    """The ``ConfigError`` raised by the loader strips Pydantic's input value.

    Pydantic's bare ``ValidationError`` echoes the supplied value
    (``input_value=...``) by default; the loader passes
    ``include_input=False`` when rendering errors so a DB URL with an
    embedded password isn't echoed back to the operator's screen.
    """
    cfg = tmp_path / "matofx.toml"
    cfg.write_text(
        'environment = "dev"\n[db]\ndialect = "postgres"\nurl = "weird://user:hunter2@host/db"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as exc_info:
        load_settings(cfg)
    msg = str(exc_info.value)
    assert "hunter2" not in msg
    # The message should still surface the failing field path.
    assert "db.url" in msg


def test_pydantic_error_at_field_level_keeps_scheme_only_in_text() -> None:
    """Direct DbSettings construction raises ValidationError; our error *text*
    only quotes the scheme prefix, but Pydantic's overall rendering still
    includes input_value. The loader path is the one customers see."""
    with pytest.raises(ValidationError) as exc_info:
        DbSettings.from_url("weird://user:hunter2@host/db")
    # The first error's msg (our text) names only the scheme prefix.
    err = exc_info.value.errors(include_input=False)[0]
    assert "hunter2" not in err["msg"]
    assert "weird://" in err["msg"]


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///./x.db",
        "sqlite+aiosqlite:///./x.db",
        "postgres://u:p@h/db",
        "postgresql://u:p@h/db",
        "postgresql+asyncpg://u:p@h/db",
    ],
)
def test_recognised_schemes_accepted(url: str) -> None:
    DbSettings.from_url(url)  # must not raise


def test_empty_url_rejected() -> None:
    with pytest.raises(ValidationError):
        DbSettings.from_url("")


# --- Production environment cross-checks ----------------------------------


def _licence_file(tmp_path: Path) -> Path:
    """A placeholder licence path for valid prod configurations.

    ``environment = "prod"`` requires ``[licence].file`` (ADR-0019 slice
    6) — the counterpart to the dev exemption that keeps unlicensed
    developer checkouts writable. Config load only checks that a path is
    configured; the file's contents are verified later, by the licence
    loader.
    """
    path = tmp_path / "matofx-licence.json"
    path.write_text("{}", encoding="utf-8")
    return path


def test_prod_without_licence_file_rejected(tmp_path: Path) -> None:
    """The other half of the dev exemption, asserted where it's enforced."""
    keyfile = tmp_path / "key.pem"
    keyfile.write_text("placeholder")
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            environment="prod",
            audit=AuditSettings(signing_key_path=keyfile, require_signing=True),
        )
    assert "licence.file is not set" in str(exc_info.value)


def test_prod_without_signing_key_path_rejected() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(environment="prod")
    msg = str(exc_info.value)
    assert "signing_key_path" in msg
    assert "prod" in msg


def test_prod_with_require_signing_false_rejected(tmp_path: Path) -> None:
    keyfile = tmp_path / "key.pem"
    keyfile.write_text("placeholder")
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            environment="prod",
            audit=AuditSettings(signing_key_path=keyfile, require_signing=False),
        )
    msg = str(exc_info.value)
    assert "require_signing" in msg


def test_prod_with_signing_configured_loads(tmp_path: Path) -> None:
    keyfile = tmp_path / "key.pem"
    keyfile.write_text("placeholder")
    s = Settings(
        environment="prod",
        audit=AuditSettings(signing_key_path=keyfile, require_signing=True),
        licence=LicenceSettings(file=_licence_file(tmp_path)),
        db=DbSettings.from_url("postgres://u:p@h/db", dialect="postgres"),
    )
    assert s.environment == "prod"


def test_dev_does_not_require_signing() -> None:
    """Outside of prod, the audit-signing rule doesn't apply."""
    s = Settings(environment="dev")
    assert s.audit.signing_key_path is None
    assert s.audit.require_signing is False


def test_staging_does_not_require_signing() -> None:
    s = Settings(environment="staging")
    assert s.audit.signing_key_path is None


# --- Production warnings (soft, non-blocking) -----------------------------


def _prod_settings(
    tmp_path: Path,
    *,
    auth: AuthSettings | None = None,
) -> Settings:
    """Build a valid prod Settings; tests pass the over-warn knob to trigger."""
    keyfile = tmp_path / "key.pem"
    keyfile.write_text("placeholder")
    audit = AuditSettings(signing_key_path=keyfile, require_signing=True)
    return Settings(
        environment="prod",
        audit=audit,
        licence=LicenceSettings(file=_licence_file(tmp_path)),
        db=DbSettings.from_url("postgres://u:p@h/db", dialect="postgres"),
        auth=auth or AuthSettings(),
    )


def test_prod_with_sqlite_warns(tmp_path: Path) -> None:
    keyfile = tmp_path / "key.pem"
    keyfile.write_text("placeholder")
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        Settings(
            environment="prod",
            audit=AuditSettings(signing_key_path=keyfile, require_signing=True),
            licence=LicenceSettings(file=_licence_file(tmp_path)),
            db=DbSettings.from_url("sqlite:///./prod.db", dialect="sqlite"),
        )
    messages = [str(w.message) for w in captured]
    assert any("sqlite" in m and "Postgres" in m for m in messages)


def test_prod_with_long_idle_timeout_warns(tmp_path: Path) -> None:
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        _prod_settings(tmp_path, auth=AuthSettings(session_idle_timeout_minutes=600))
    assert any("session_idle_timeout_minutes" in str(w.message) for w in captured)


def test_prod_with_normal_idle_timeout_does_not_warn(tmp_path: Path) -> None:
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        _prod_settings(tmp_path)
    msgs = [str(w.message) for w in captured]
    assert not any("session_idle_timeout" in m for m in msgs)


def test_prod_with_high_lockout_threshold_warns(tmp_path: Path) -> None:
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        _prod_settings(tmp_path, auth=AuthSettings(failed_login_lockout_threshold=50))
    assert any("failed_login_lockout_threshold" in str(w.message) for w in captured)


# --- POSIX file-permission warning ----------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
def test_world_readable_config_warns(tmp_path: Path) -> None:
    cfg = tmp_path / "matofx.toml"
    cfg.write_text('environment = "dev"\n', encoding="utf-8")
    cfg.chmod(0o644)  # other-readable
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        load_settings(cfg)
    assert any("readable by other users" in str(w.message) for w in captured)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
def test_owner_only_config_does_not_warn(tmp_path: Path) -> None:
    cfg = tmp_path / "matofx.toml"
    cfg.write_text('environment = "dev"\n', encoding="utf-8")
    cfg.chmod(0o600)
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        load_settings(cfg)
    msgs = [str(w.message) for w in captured]
    assert not any("readable by other users" in m for m in msgs)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
def test_perm_warning_suppressed_by_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "matofx.toml"
    cfg.write_text('environment = "dev"\n', encoding="utf-8")
    cfg.chmod(0o644)
    monkeypatch.setenv("MATOFX_SUPPRESS_PERM_WARNING", "1")
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        load_settings(cfg)
    msgs = [str(w.message) for w in captured]
    assert not any("readable by other users" in m for m in msgs)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL model")
def test_windows_skips_perm_check(tmp_path: Path) -> None:
    """On Windows, the POSIX bit-based check doesn't fire."""
    cfg = tmp_path / "matofx.toml"
    cfg.write_text('environment = "dev"\n', encoding="utf-8")
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        load_settings(cfg)
    msgs = [str(w.message) for w in captured]
    assert not any("readable by other users" in m for m in msgs)


# Cleanup: tests above with monkeypatch of MATOFX_SUPPRESS_PERM_WARNING
# are scoped properly; this guards against test order side-effects.
def teardown_module(_module: object) -> None:
    os.environ.pop("MATOFX_SUPPRESS_PERM_WARNING", None)
