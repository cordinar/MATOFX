"""Unit tests for the login post-auth redirect round-trip.

Covers the security-sensitive ``?next=`` validation (no open
redirect, no login loop) and ``_post_login_target``'s ``/ui`` mount-
prefix stripping that lets a deep link from a notification email
survive the sign-in detour.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from matofx.ui.pages import _chrome
from matofx.ui.pages.login import _safe_next


@pytest.mark.parametrize(
    ("dest", "expected"),
    [
        ("/my-jobs?job=32", "/my-jobs?job=32"),
        ("/home", "/home"),
        ("/admin/send-notifications", "/admin/send-notifications"),
        ("", "/home"),
        ("//evil.example.com", "/home"),  # protocol-relative open redirect
        ("https://evil.example.com", "/home"),  # absolute URL
        ("/login", "/home"),  # would loop
        ("/login/", "/home"),
        ("relative/path", "/home"),  # not rooted
    ],
)
def test_safe_next_blocks_open_redirect_and_loops(dest: str, expected: str) -> None:
    assert _safe_next(dest) == expected


def _fake_context(path: str | None, query: str = "") -> SimpleNamespace:
    request = (
        None
        if path is None
        else SimpleNamespace(url=SimpleNamespace(path=path, query=query))
    )
    return SimpleNamespace(client=SimpleNamespace(request=request))


@pytest.mark.parametrize(
    ("path", "query", "expected"),
    [
        ("/ui/my-jobs", "job=32", "/my-jobs?job=32"),  # mount prefix stripped
        ("/ui/my-jobs", "", "/my-jobs"),
        ("/my-jobs", "job=7", "/my-jobs?job=7"),  # already mount-relative
        ("/ui/admin/send-notifications", "", "/admin/send-notifications"),
        ("/ui/login", "", None),  # never round-trip back to login
        ("/ui", "", None),  # bare mount → nothing useful
        ("/ui/", "", None),
        (None, "", None),  # no request object
    ],
)
def test_post_login_target_strips_ui_prefix(
    monkeypatch: pytest.MonkeyPatch, path: str | None, query: str, expected: str | None
) -> None:
    monkeypatch.setattr(_chrome, "context", _fake_context(path, query))
    assert _chrome._post_login_target() == expected


def test_post_login_target_swallows_context_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Boom:
        @property
        def client(self) -> object:
            raise RuntimeError("no client context")

    monkeypatch.setattr(_chrome, "context", _Boom())
    assert _chrome._post_login_target() is None


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/ui/my-jobs", "/my-jobs"),  # mount prefix stripped
        ("/my-jobs", "/my-jobs"),  # already mount-relative
        ("/ui/studies/42", "/studies/42"),  # detail path preserved
        ("/ui/admin/vv-records", "/admin/vv-records"),
        ("/ui", None),  # bare mount has no usable remainder
        ("/ui/", "/"),  # trailing slash collapses to root
        (None, None),  # no request object
    ],
)
def test_current_nav_path_strips_ui_prefix(
    monkeypatch: pytest.MonkeyPatch, path: str | None, expected: str | None
) -> None:
    monkeypatch.setattr(_chrome, "context", _fake_context(path))
    assert _chrome._current_nav_path() == expected


def test_current_nav_path_swallows_context_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Boom:
        @property
        def client(self) -> object:
            raise RuntimeError("no client context")

    monkeypatch.setattr(_chrome, "context", _Boom())
    assert _chrome._current_nav_path() is None


_NAV_TARGETS = ["/home", "/studies", "/admin/tools", "/admin/vv-records", "/vv-records"]


@pytest.mark.parametrize(
    ("role_name", "expected"),
    [
        ("regular_user", "Regular User"),  # plain title-case
        ("finance_team_lead", "Finance Team Lead"),  # custom role, same rule
        # Overrides — title-case reads badly ("Vv Curator") or ambiguously
        # ("Model Curator" next to the tool / material catalogues).
        ("vv_curator", "Verification & Validation (V&V) curator"),
        ("model_curator", "Surrogate Model Curator"),
        ("", ""),
    ],
)
def test_format_role_label(role_name: str, expected: str) -> None:
    assert _chrome.format_role_label(role_name) == expected


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("/home", "/home"),  # exact match
        ("/studies/42", "/studies"),  # detail page lights up its parent
        ("/admin/vv-records", "/admin/vv-records"),  # nested beats shorter sibling
        ("/vv-records", "/vv-records"),  # the curator page, not the admin inbox
        ("/admin/tools-archive", None),  # segment boundary — not /admin/tools
        ("/nowhere", None),  # nothing matches
        (None, None),  # no current path
    ],
)
def test_active_target_longest_prefix(current: str | None, expected: str | None) -> None:
    assert _chrome._active_target(_NAV_TARGETS, current) == expected
