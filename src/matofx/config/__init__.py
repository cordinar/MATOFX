"""Configuration loader and typed Settings model.

Public surface:

- :func:`load_settings` — read TOML + env-var overrides, validate, return
  a frozen :class:`Settings`. Call once at process startup.
- :class:`Settings` — top-level configuration object.
- :class:`ConfigError` — raised when loading or validation fails; message
  is plain English with actionable guidance.
"""

# ruff: noqa: I001 - import order below is deliberate (see the comment),
# isort would re-sort it back into a circular import.
from __future__ import annotations

# Import order matters here. The general-settings submodules pull in
# ``matofx.runner.settings`` for the shared ``SettingSpec`` primitive,
# and importing that package transitively re-enters ``matofx.config``
# via ``matofx.runner.daemon`` (which needs ``load_settings``). To
# avoid a half-initialised cycle, we bind ``load_settings`` and the
# typed ``Settings`` models first, then pull in the general-settings
# pieces — by then the daemon's ``from matofx.config import
# load_settings`` resolves cleanly against the already-populated
# config namespace.
from matofx.config.loader import ConfigError, load_settings
from matofx.config.models import (
    CoverageSettings,
    AuditSettings,
    ComplianceSettings,
    LicenceSettings,
    AuthSettings,
    BrandingSettings,
    DbSettings,
    EmailSettings,
    Environment,
    FeatureSettings,
    LlmSettings,
    LogLevel,
    LogSettings,
    ModelsSettings,
    SecretsBackend,
    SecretsSettings,
    Settings,
    ToolsSettings,
    TutorialsSettings,
    UploadsSettings,
)
from matofx.config.general_errors import (
    GeneralSettingsAccessDeniedError,
    GeneralSettingsError,
    ImageUploadRejectedError,
    InvalidGeneralSettingValueError,
    UnknownGeneralSettingKeyError,
)
from matofx.config.general_service import (
    SECRET_REDACTED_SENTINEL,
    GeneralSettingsService,
    load_general_settings,
    make_general_settings_service,
)
from matofx.config.general_settings import (
    GENERAL_SETTINGS_CATALOG,
    NEW_ACCOUNT_ROLE_CHOICES,
    REGISTRATION_MODES,
)

__all__ = [
    "GENERAL_SETTINGS_CATALOG",
    "NEW_ACCOUNT_ROLE_CHOICES",
    "REGISTRATION_MODES",
    "SECRET_REDACTED_SENTINEL",
    "AuditSettings",
    "AuthSettings",
    "BrandingSettings",
    "ComplianceSettings",
    "ConfigError",
    "CoverageSettings",
    "DbSettings",
    "EmailSettings",
    "Environment",
    "FeatureSettings",
    "GeneralSettingsAccessDeniedError",
    "GeneralSettingsError",
    "GeneralSettingsService",
    "ImageUploadRejectedError",
    "InvalidGeneralSettingValueError",
    "LicenceSettings",
    "LlmSettings",
    "LogLevel",
    "LogSettings",
    "ModelsSettings",
    "SecretsBackend",
    "SecretsSettings",
    "Settings",
    "ToolsSettings",
    "TutorialsSettings",
    "UnknownGeneralSettingKeyError",
    "UploadsSettings",
    "load_general_settings",
    "load_settings",
    "make_general_settings_service",
]
