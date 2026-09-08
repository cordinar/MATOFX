"""Tests for the config loader."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from matofx.config import ConfigError, Settings, load_settings


def write_toml(tmp_path: Path, content: str) -> Path:
    f = tmp_path / "matofx.toml"
    f.write_text(content, encoding="utf-8")
    return f


def test_environment_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("matofx.config.loader.candidate_paths", list)
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    msg = str(exc_info.value)
    assert "environment" in msg


def test_defaults_apply_once_environment_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("matofx.config.loader.candidate_paths", list)
    monkeypatch.setenv("MATOFX__ENVIRONMENT", "dev")
    settings = load_settings()
    assert isinstance(settings, Settings)
    assert settings.environment == "dev"
    assert settings.log.level == "INFO"
    assert settings.db.dialect == "sqlite"


def test_loads_from_explicit_path(tmp_path: Path) -> None:
    # Use 'staging' rather than 'prod' so the production safety checks
    # (which require audit.signing_key_path + require_signing) don't
    # block this test of the bare loader path.
    cfg = write_toml(
        tmp_path,
        """
        environment = "staging"

        [log]
        level = "WARNING"

        [db]
        dialect = "postgres"
        url = "postgres://u:p@db:5432/matofx"
        pool_size = 50
        """,
    )
    settings = load_settings(cfg)
    assert settings.environment == "staging"
    assert settings.log.level == "WARNING"
    assert settings.db.dialect == "postgres"
    assert settings.db.url.get_secret_value() == "postgres://u:p@db:5432/matofx"
    assert settings.db.pool_size == 50


def test_env_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = write_toml(tmp_path, 'environment = "dev"\n[log]\nlevel = "INFO"\n')
    monkeypatch.setenv("MATOFX__ENVIRONMENT", "staging")
    monkeypatch.setenv("MATOFX__LOG__LEVEL", "DEBUG")
    settings = load_settings(cfg)
    assert settings.environment == "staging"
    assert settings.log.level == "DEBUG"


def test_env_only_no_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("matofx.config.loader.candidate_paths", list)
    monkeypatch.setenv("MATOFX__ENVIRONMENT", "dev")
    monkeypatch.setenv("MATOFX__DB__POOL_SIZE", "25")
    settings = load_settings()
    assert settings.db.pool_size == 25


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("True", True),
        ("YES", True),
        ("on", True),
        ("1", True),
        ("false", False),
        ("FALSE", False),
        ("no", False),
        ("off", False),
        ("0", False),
    ],
)
def test_env_boolean_coercion(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected: bool,
) -> None:
    monkeypatch.setattr("matofx.config.loader.candidate_paths", list)
    monkeypatch.setenv("MATOFX__ENVIRONMENT", "dev")
    monkeypatch.setenv("MATOFX__AUDIT__REQUIRE_SIGNING", raw)
    settings = load_settings()
    assert settings.audit.require_signing is expected


def test_env_integer_coercion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("matofx.config.loader.candidate_paths", list)
    monkeypatch.setenv("MATOFX__ENVIRONMENT", "dev")
    monkeypatch.setenv("MATOFX__DB__STATEMENT_TIMEOUT_MS", "60000")
    settings = load_settings()
    assert settings.db.statement_timeout_ms == 60000


def test_unknown_field_rejected(tmp_path: Path) -> None:
    cfg = write_toml(tmp_path, 'rubbish = "no"\n')
    with pytest.raises(ConfigError) as exc_info:
        load_settings(cfg)
    assert "rubbish" in str(exc_info.value)


def test_invalid_value_message_is_actionable(tmp_path: Path) -> None:
    cfg = write_toml(tmp_path, 'environment = "dev"\n[log]\nlevel = "VERBOSE"\n')
    with pytest.raises(ConfigError) as exc_info:
        load_settings(cfg)
    msg = str(exc_info.value)
    assert "log.level" in msg
    assert "matofx.toml" in msg or "MATOFX__" in msg


def test_nonexistent_explicit_path_raises(tmp_path: Path) -> None:
    bogus = tmp_path / "does_not_exist.toml"
    with pytest.raises(ConfigError) as exc_info:
        load_settings(bogus)
    assert str(bogus) in str(exc_info.value)


def test_invalid_toml_raises(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.toml"
    cfg.write_text("this is = not toml = at all\n[unclosed", encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        load_settings(cfg)
    assert "TOML" in str(exc_info.value)


def test_settings_are_frozen() -> None:
    s = Settings(environment="dev")
    with pytest.raises(ValidationError):
        s.environment = "prod"


def test_example_config_loads() -> None:
    """The shipped etc/matofx.example.toml must always load cleanly."""
    repo_root = Path(__file__).resolve().parents[2]
    example = repo_root / "etc" / "matofx.example.toml"
    assert example.is_file(), f"missing example config at {example}"
    settings = load_settings(example)
    assert settings.environment in {"dev", "test", "staging", "prod"}


def test_relative_paths_resolve_against_config_dir(tmp_path: Path) -> None:
    """A relative ``bundles_dir`` / ``tutorials_dir`` / ``signing_key_path``
    in a config file resolves against that file's directory, not the
    process CWD. Stops the surprise where a service running under NSSM
    (CWD=C:\\Windows\\System32) writes uploads to that system path.
    """
    cfg = write_toml(
        tmp_path,
        """
        environment = "staging"

        [audit]
        signing_key_path = "audit.key"
        require_signing = false

        [tools]
        bundles_dir = "tools"

        [tutorials]
        tutorials_dir = "tutorials"
        """,
    )
    settings = load_settings(cfg)
    assert settings.tools.bundles_dir == (tmp_path / "tools").resolve()
    assert settings.tutorials.tutorials_dir == (tmp_path / "tutorials").resolve()
    assert settings.audit.signing_key_path == (tmp_path / "audit.key").resolve()


def test_relative_defaults_resolve_against_config_dir(tmp_path: Path) -> None:
    """Even the *built-in* relative defaults follow the same anchor —
    a config file with no ``[tools]`` / ``[tutorials]`` sections still
    gets sensible absolute paths beside it instead of inheriting the
    service's working directory.
    """
    cfg = write_toml(tmp_path, 'environment = "staging"\n')
    settings = load_settings(cfg)
    assert settings.tools.bundles_dir == (tmp_path / "tools").resolve()
    assert settings.tutorials.tutorials_dir == (tmp_path / "tutorials").resolve()


def test_absolute_paths_are_left_alone(tmp_path: Path) -> None:
    """Absolute paths in the config are honoured verbatim — the operator
    has been explicit, don't second-guess them.
    """
    explicit = (tmp_path / "elsewhere" / "bundles").resolve()
    cfg = write_toml(
        tmp_path,
        f"""
        environment = "staging"

        [tools]
        bundles_dir = "{explicit.as_posix()}"
        """,
    )
    settings = load_settings(cfg)
    assert settings.tools.bundles_dir == explicit
