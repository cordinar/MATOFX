"""User-facing errors for the General Settings admin service.

Plain English; safe to render directly to admins via the UI. The
shape parallels :mod:`matofx.runner.errors` so the API's exception
handlers can treat the two domains uniformly.
"""

from __future__ import annotations


class GeneralSettingsError(Exception):
    """Base class for General Settings admin errors."""


class GeneralSettingsAccessDeniedError(GeneralSettingsError):
    """The actor's role isn't high enough to mutate this setting.

    Surfaces when an administrator tries to edit a row whose
    ``requires_role='super_user'`` (e.g. session timeout, password
    policy). The message names which role is required so the admin
    knows who to ask.
    """

    def __init__(self, key: str, required_role: str) -> None:
        """Construct with the offending key and the role threshold."""
        super().__init__(
            f"Editing '{key}' requires the '{required_role}' role. "
            "Ask a super-user if you need this changed."
        )
        self.key = key
        self.required_role = required_role


class UnknownGeneralSettingKeyError(GeneralSettingsError):
    """The supplied key isn't in the General Settings catalog."""

    def __init__(self, key: str) -> None:
        """Construct with the offending key."""
        super().__init__(f"'{key}' isn't a recognised general setting.")
        self.key = key


class InvalidGeneralSettingValueError(GeneralSettingsError):
    """A value failed catalog validation (type, range, choice, or format)."""

    def __init__(self, key: str, reason: str) -> None:
        """Construct with the key and a plain-English reason."""
        super().__init__(f"'{key}' isn't a valid value: {reason}")
        self.key = key
        self.reason = reason


class ImageUploadRejectedError(GeneralSettingsError):
    """An uploaded image failed validation (mime, size, or kind mismatch)."""

    def __init__(self, key: str, reason: str) -> None:
        """Construct with the target key and a plain-English reason."""
        super().__init__(f"Couldn't upload image for '{key}': {reason}")
        self.key = key
        self.reason = reason
