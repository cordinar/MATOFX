"""User-facing errors for the runner's admin surface.

Plain English; safe to render directly to admins via the UI.
"""

from __future__ import annotations


class RuntimeSettingsError(Exception):
    """Base class for runtime-settings admin errors."""


class RuntimeSettingsAccessDeniedError(RuntimeSettingsError):
    """The actor isn't allowed to manage runtime settings.

    Surfaces when a user without
    :attr:`matofx.users.permissions.Permission.RUNTIME_SETTINGS_MANAGE`
    calls into :class:`RuntimeSettingsService`.
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "You don't have permission to manage runtime settings. "
            "Ask an administrator if you need access."
        )


class UnknownSettingKeyError(RuntimeSettingsError):
    """The supplied key isn't in the runtime-settings catalog."""

    def __init__(self, key: str) -> None:
        """Construct with the offending key."""
        super().__init__(f"'{key}' isn't a recognised runtime setting.")
        self.key = key


class InvalidSettingValueError(RuntimeSettingsError):
    """A value failed catalog validation (type or range)."""

    def __init__(self, key: str, reason: str) -> None:
        """Construct with the key and a plain-English reason."""
        super().__init__(f"'{key}' isn't a valid value: {reason}")
        self.key = key
        self.reason = reason
