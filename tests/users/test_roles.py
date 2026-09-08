"""Tests for the four built-in roles."""

from __future__ import annotations

import pytest

from matofx.users import (
    DEFAULT_ROLE,
    USER_ROLES,
    can_see_test_tools,
    has_role_at_least,
    is_known_role,
)
from matofx.users.permissions import (
    ADMINISTRATOR_DEFAULT_PERMISSIONS,
    PERMISSION_DESCRIPTIONS,
    SUPER_USER_PERMISSIONS,
    Permission,
)


def test_default_role_is_regular_user() -> None:
    assert DEFAULT_ROLE == "regular_user"


def test_every_permission_has_a_ui_description() -> None:
    """The permission editor's helper text comes from PERMISSION_DESCRIPTIONS.

    Attribute docstrings on the enum members aren't readable at runtime,
    so each permission needs an explicit entry here. A new slug added
    without one would silently render with no helper text — guard it.
    """
    assert set(PERMISSION_DESCRIPTIONS) == set(Permission)
    for member, text in PERMISSION_DESCRIPTIONS.items():
        assert text.strip(), member


def test_four_built_in_roles() -> None:
    assert USER_ROLES == (
        "super_user",
        "administrator",
        "beta_tester",
        "regular_user",
    )


@pytest.mark.parametrize(
    ("actual", "threshold", "expected"),
    [
        # super_user satisfies every threshold
        ("super_user", "super_user", True),
        ("super_user", "administrator", True),
        ("super_user", "beta_tester", True),
        ("super_user", "regular_user", True),
        # administrator
        ("administrator", "super_user", False),
        ("administrator", "administrator", True),
        ("administrator", "beta_tester", True),
        ("administrator", "regular_user", True),
        # beta_tester == regular_user for hierarchy rights
        ("beta_tester", "administrator", False),
        ("beta_tester", "beta_tester", True),
        ("beta_tester", "regular_user", True),
        ("regular_user", "beta_tester", True),
        ("regular_user", "regular_user", True),
        ("regular_user", "administrator", False),
    ],
)
def test_role_ordering(actual: str, threshold: str, expected: bool) -> None:
    assert has_role_at_least(actual, threshold) is expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("super_user", True),
        ("administrator", True),
        ("beta_tester", True),
        ("regular_user", False),
        ("hacker", False),
        ("", False),
    ],
)
def test_can_see_test_tools(role: str, expected: bool) -> None:
    """Beta-visibility is orthogonal to the privilege rank."""
    assert can_see_test_tools(role) is expected


def test_unknown_role_satisfies_nothing() -> None:
    """A foreign / corrupted role string never grants privilege."""
    assert has_role_at_least("hacker", "regular_user") is False
    assert has_role_at_least("", "regular_user") is False


def test_is_known_role() -> None:
    assert is_known_role("super_user")
    assert is_known_role("regular_user")
    assert not is_known_role("hacker")
    assert not is_known_role("")


def test_live_jobs_permission_defaults() -> None:
    """Live Jobs: admins see the tab by default but can't cancel until granted.

    ``live_jobs.view`` ships in the administrator default set so existing
    installs surface the tab without fiddling; ``live_jobs.cancel`` is
    deliberately withheld (off by default, grantable per role). The
    super_user sentinel always carries both.
    """
    assert Permission.LIVE_JOBS_VIEW in ADMINISTRATOR_DEFAULT_PERMISSIONS
    assert Permission.LIVE_JOBS_CANCEL not in ADMINISTRATOR_DEFAULT_PERMISSIONS
    assert Permission.LIVE_JOBS_VIEW in SUPER_USER_PERMISSIONS
    assert Permission.LIVE_JOBS_CANCEL in SUPER_USER_PERMISSIONS
