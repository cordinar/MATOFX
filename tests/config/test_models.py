"""Tests for the typed Settings model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from matofx.config import AuditSettings, DbSettings, LogSettings, Settings


def test_environment_is_required() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings()  # type: ignore[call-arg]
    assert "environment" in str(exc_info.value)


def test_default_settings_round_trip() -> None:
    s = Settings(environment="dev")
    dumped = s.model_dump()
    s2 = Settings.model_validate(dumped)
    assert s == s2


def test_log_level_must_be_known() -> None:
    with pytest.raises(ValidationError):
        LogSettings(level="VERBOSE")  # type: ignore[arg-type]


def test_db_dialect_must_be_known() -> None:
    with pytest.raises(ValidationError):
        DbSettings.from_url("oracle://x", dialect="oracle")


def test_db_pool_size_bounds() -> None:
    with pytest.raises(ValidationError):
        DbSettings(pool_size=0)
    with pytest.raises(ValidationError):
        DbSettings(pool_size=500)


def test_db_url_required_non_empty() -> None:
    with pytest.raises(ValidationError):
        DbSettings.from_url("")


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        AuditSettings(unexpected="oops")  # type: ignore[call-arg]


def test_settings_frozen_top_level() -> None:
    s = Settings(environment="dev")
    with pytest.raises(ValidationError):
        s.environment = "prod"


def test_settings_model_copy_for_test_overrides() -> None:
    s = Settings(environment="dev")
    s2 = s.model_copy(update={"environment": "test"})
    assert s.environment == "dev"
    assert s2.environment == "test"
