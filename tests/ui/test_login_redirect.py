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
    request = None if path is None else SimpleNamespace(url=SimpleNamespace(path=path, query=query))
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


# ---- nav targets that distinguish themselves by a query ---------------
#
# "V&V records inbox" is the only nav entry whose target carries a query
# (``/vv-records?tab=queue`` — the same page as "V&V records", opened on
# its Review queue tab). Reported 2026-09-21: clicking it showed the
# queue, but the sidebar highlighted the parent "V&V records" instead.
#
# Two things had to line up for that. ``_current_nav_path`` returned
# ``request.url.path`` and dropped the query, so the current path was
# always plain ``/vv-records``; and ``_active_target`` compared whole
# strings, so a target containing "?" could never equal or prefix a
# query-less path. The inbox entry was therefore unreachable as an
# active target — not occasionally, but always.

_VV_TARGETS = ["/vv-records", "/vv-records?tab=queue", "/admin/vv-records"]


@pytest.mark.parametrize(
    ("path", "query", "expected"),
    [
        ("/ui/vv-records", "tab=queue", "/vv-records?tab=queue"),
        ("/ui/vv-records", "", "/vv-records"),
        ("/vv-records", "tab=queue", "/vv-records?tab=queue"),
        ("/ui/studies/42", "", "/studies/42"),  # unchanged without a query
    ],
)
def test_current_nav_path_keeps_the_query(
    monkeypatch: pytest.MonkeyPatch, path: str, query: str, expected: str
) -> None:
    """The highlight can't tell two entries apart without it."""
    monkeypatch.setattr(_chrome, "context", _fake_context(path, query))
    assert _chrome._current_nav_path() == expected


def test_the_inbox_entry_lights_on_its_own_tab() -> None:
    """The reported bug, in one assertion."""
    assert _chrome._active_target(_VV_TARGETS, "/vv-records?tab=queue") == "/vv-records?tab=queue"


def test_the_plain_entry_lights_when_no_tab_is_pinned() -> None:
    assert _chrome._active_target(_VV_TARGETS, "/vv-records") == "/vv-records"


def test_a_different_tab_falls_back_to_the_plain_entry() -> None:
    """A query the inbox doesn't claim must not blank the sidebar.

    Matching the query too strictly would leave *nothing* highlighted on
    ``?tab=tool`` — a worse bug than the one being fixed.
    """
    assert _chrome._active_target(_VV_TARGETS, "/vv-records?tab=tool") == "/vv-records"


def test_extra_query_parameters_do_not_unpin_the_inbox() -> None:
    """The target pins the parameters it names, and ignores the rest."""
    assert (
        _chrome._active_target(_VV_TARGETS, "/vv-records?tab=queue&page=2")
        == "/vv-records?tab=queue"
    )


def test_a_detail_path_still_lights_the_plain_entry() -> None:
    assert _chrome._active_target(_VV_TARGETS, "/vv-records/42") == "/vv-records"


def test_a_query_on_a_sibling_path_does_not_reach_across() -> None:
    """``?tab=queue`` is only meaningful under the target that declares it."""
    assert _chrome._active_target(_VV_TARGETS, "/admin/vv-records?tab=queue") == (
        "/admin/vv-records"
    )


def test_every_nav_target_can_actually_become_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards the whole nav, not just the one entry that was reported.

    Walks every real nav target through the same two steps the sidebar
    does — land on that URL, derive the current path, match it — so a
    target nobody can ever reach fails here instead of silently
    highlighting something else. Both halves of the reported bug are in
    the path: run against the old code, the query is dropped at step one
    and the inbox entry resolves to its parent.
    """
    all_targets = [item.target for group in _chrome._NAV_GROUPS for item in group.items]
    assert any("?" in t for t in all_targets), (
        "no query-bearing nav target left — this guard needs rewiring"
    )
    for target in all_targets:
        path, _, query = target.partition("?")
        monkeypatch.setattr(_chrome, "context", _fake_context(f"/ui{path}", query))
        current = _chrome._current_nav_path()
        assert _chrome._active_target(all_targets, current) == target, (
            f"clicking {target!r} highlights something else"
        )
