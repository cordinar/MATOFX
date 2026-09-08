"""Unit tests for the header help-link helper in ``matofx.ui.pages._chrome``."""

from __future__ import annotations

from matofx.ui.pages._chrome import _help_target


def test_help_target_root_when_no_topic() -> None:
    assert _help_target(None) == "/help"
    assert _help_target("") == "/help"


def test_help_target_deep_links_to_topic() -> None:
    assert _help_target("account-page") == "/help?topic=account-page"


def test_help_target_url_encodes_slashes() -> None:
    assert _help_target("jobs/submitting") == "/help?topic=jobs%2Fsubmitting"
