"""Tests for the idle / expired-session round-trip back to the login page.

Three pieces co-operate and each is covered here:

* :mod:`matofx.ui.api_client` fires an "unauthenticated" hook when an
  *authenticated* call comes back 401 — and must NOT fire it for the
  login form's own 401 (wrong password), which carries no bearer token.
* :func:`matofx.ui.pages._chrome._handle_expired_session` tears down the
  stored token and navigates to ``/login?...&reason=expired``.
* The login page turns ``?reason=`` into the plain-English notice that
  explains why the user is back on the sign-in screen.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from matofx.ui.api_client import ApiClient, ApiError, set_unauthenticated_hook
from matofx.ui.pages import _chrome
from matofx.ui.pages.login import _sign_out_notice


@pytest.fixture
def hook_calls() -> Iterator[list[ApiError]]:
    """Install a recording unauthenticated hook, restoring the real one after."""
    calls: list[ApiError] = []
    set_unauthenticated_hook(calls.append)
    yield calls
    # The dashboard chrome's real hook is registered at import time; put
    # it back so later tests see production wiring.
    set_unauthenticated_hook(_chrome._handle_expired_session)


@pytest.mark.asyncio
async def test_revoked_token_fires_unauthenticated_hook(
    app: FastAPI, seeded: dict[str, Any], hook_calls: list[ApiError]
) -> None:
    """A call with a dead bearer token is what bounces the user to /login."""
    auth = await ApiClient(app).login(username=seeded["username"], password=seeded["password"])
    client = ApiClient(app, token=auth["access_token"])
    await client.logout()  # revoke it server-side, as an idle logout does

    with pytest.raises(ApiError) as ei:
        await client.me()

    assert ei.value.status_code == 401
    assert len(hook_calls) == 1
    assert hook_calls[0].status_code == 401


@pytest.mark.asyncio
async def test_bad_password_does_not_fire_hook(
    app: FastAPI, seeded: dict[str, Any], hook_calls: list[ApiError]
) -> None:
    """The login form's own 401 must not be read as an expired session."""
    with pytest.raises(ApiError) as ei:
        await ApiClient(app).login(username=seeded["username"], password="wrong-password-1234")

    assert ei.value.status_code == 401
    assert hook_calls == []


@pytest.mark.asyncio
async def test_other_errors_do_not_fire_hook(
    app: FastAPI, seeded: dict[str, Any], hook_calls: list[ApiError]
) -> None:
    """Only 401 signals a dead session — a 404 leaves the user where they are."""
    auth = await ApiClient(app).login(username=seeded["username"], password=seeded["password"])
    client = ApiClient(app, token=auth["access_token"])

    with pytest.raises(ApiError):
        await client.get_tool("no-such-tool-alias")

    assert hook_calls == []


@pytest.mark.parametrize(
    ("target", "reason", "expected"),
    [
        (None, "", "/login"),
        ("/my-jobs?job=32", "", "/login?next=%2Fmy-jobs%3Fjob%3D32"),
        (None, "idle", "/login?reason=idle"),
        ("/studies/42", "expired", "/login?next=%2Fstudies%2F42&reason=expired"),
    ],
)
def test_login_url_composition(target: str | None, reason: str, expected: str) -> None:
    assert _chrome._login_url(target=target, reason=reason) == expected


def _install_fake_client(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture ``ui.navigate.to`` targets from the chrome module."""
    navigated: list[str] = []
    monkeypatch.setattr(
        _chrome.ui, "navigate", SimpleNamespace(to=lambda target: navigated.append(target))
    )
    return navigated


def test_expired_session_clears_state_and_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    navigated = _install_fake_client(monkeypatch)
    cleared: list[bool] = []
    monkeypatch.setattr(_chrome, "is_logged_in", lambda: True)
    monkeypatch.setattr(_chrome, "clear_login", lambda: cleared.append(True))
    monkeypatch.setattr(_chrome, "_post_login_target", lambda: "/my-jobs")

    _chrome._handle_expired_session(ApiError(401, "Your session has expired."))

    assert cleared == [True]
    assert navigated == ["/login?next=%2Fmy-jobs&reason=expired"]


def test_expired_session_is_idempotent_once_signed_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parallel 401s from one page render must not queue up redirects."""
    navigated = _install_fake_client(monkeypatch)
    monkeypatch.setattr(_chrome, "is_logged_in", lambda: False)
    monkeypatch.setattr(_chrome, "clear_login", lambda: pytest.fail("should not clear twice"))

    _chrome._handle_expired_session(ApiError(401, "Your session has expired."))

    assert navigated == []


@pytest.mark.parametrize(
    ("reason", "has_notice"),
    [
        ("idle", True),
        ("expired", True),
        ("EXPIRED", True),  # case-insensitive
        ("", False),
        ("bogus", False),
    ],
)
def test_sign_out_notice_lookup(reason: str, has_notice: bool) -> None:
    assert bool(_sign_out_notice(reason)) is has_notice
