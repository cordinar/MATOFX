"""Tests for the documentation loader, model gating, and shipped content."""

from __future__ import annotations

from pathlib import Path

import pytest

from matofx.docs import (
    SET_ADMIN,
    SET_PUBLIC,
    SET_USER,
    DocError,
    get_doc_tree,
    load_docs,
)


def _write(path: Path, *, title: str, slug: str, audience: str, body: str = "Body.") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {title}\naudience: {audience}\ncapability: null\n"
        f"slug: {slug}\nsummary: A summary.\n---\n\n{body}\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# Shipped content
# --------------------------------------------------------------------------


def test_real_content_loads_and_is_consistent() -> None:
    tree = load_docs()

    # All three sets are present.
    assert {s.name for s in tree.sets} == {SET_PUBLIC, SET_USER, SET_ADMIN}

    # Every topic's audience matches its set; slugs are unique tree-wide.
    seen: set[str] = set()
    for doc_set in tree.sets:
        for topic in doc_set.topics:
            assert topic.audience == doc_set.name
            assert topic.slug not in seen
            seen.add(topic.slug)
            assert topic.title
            assert topic.body.strip()

    # A couple of known slice-1 chapters exist.
    assert tree.topic("welcome") is not None
    assert tree.topic("account-page") is not None
    assert tree.topic("admin-overview") is not None


def test_get_doc_tree_is_cached() -> None:
    assert get_doc_tree() is get_doc_tree()


# --------------------------------------------------------------------------
# Ordering + parsing
# --------------------------------------------------------------------------


def test_topics_sorted_by_filename_numeric_prefix(tmp_path: Path) -> None:
    _write(tmp_path / SET_USER / "20-third.md", title="Third", slug="third", audience=SET_USER)
    _write(tmp_path / SET_USER / "00-first.md", title="First", slug="first", audience=SET_USER)
    _write(tmp_path / SET_USER / "10-second.md", title="Second", slug="second", audience=SET_USER)

    tree = load_docs(tmp_path)
    user = tree.get_set(SET_USER)
    assert user is not None
    assert [t.slug for t in user.topics] == ["first", "second", "third"]


def test_body_strips_front_matter(tmp_path: Path) -> None:
    _write(
        tmp_path / SET_USER / "00-x.md",
        title="X",
        slug="x",
        audience=SET_USER,
        body="# Heading\n\nSome **text**.",
    )
    topic = load_docs(tmp_path).topic("x")
    assert topic is not None
    assert topic.body.startswith("# Heading")
    assert "audience:" not in topic.body


# --------------------------------------------------------------------------
# Validation errors
# --------------------------------------------------------------------------


def test_missing_front_matter_raises(tmp_path: Path) -> None:
    (tmp_path / SET_USER).mkdir(parents=True)
    (tmp_path / SET_USER / "00-x.md").write_text("# No front matter\n", encoding="utf-8")
    with pytest.raises(DocError, match="missing '---'"):
        load_docs(tmp_path)


def test_missing_required_field_raises(tmp_path: Path) -> None:
    (tmp_path / SET_USER).mkdir(parents=True)
    (tmp_path / SET_USER / "00-x.md").write_text(
        "---\naudience: user\nslug: x\n---\n\nBody.\n", encoding="utf-8"
    )
    with pytest.raises(DocError, match="title"):
        load_docs(tmp_path)


def test_audience_must_match_directory(tmp_path: Path) -> None:
    _write(tmp_path / SET_USER / "00-x.md", title="X", slug="x", audience=SET_ADMIN)
    with pytest.raises(DocError, match="doesn't match its directory"):
        load_docs(tmp_path)


def test_duplicate_slug_raises(tmp_path: Path) -> None:
    _write(tmp_path / SET_USER / "00-a.md", title="A", slug="dup", audience=SET_USER)
    _write(tmp_path / SET_ADMIN / "00-b.md", title="B", slug="dup", audience=SET_ADMIN)
    with pytest.raises(DocError, match="already used"):
        load_docs(tmp_path)


# --------------------------------------------------------------------------
# In-app gating
# --------------------------------------------------------------------------


def test_in_app_sets_include_public_first_and_gate_admin(tmp_path: Path) -> None:
    _write(tmp_path / SET_PUBLIC / "00-w.md", title="W", slug="w", audience=SET_PUBLIC)
    _write(tmp_path / SET_USER / "00-u.md", title="U", slug="u", audience=SET_USER)
    _write(tmp_path / SET_ADMIN / "00-a.md", title="A", slug="a", audience=SET_ADMIN)
    tree = load_docs(tmp_path)

    # The Getting started (public) set is discoverable in the hub too, and
    # is ordered first; the admin guide stays gated on include_admin.
    user_view = [s.name for s in tree.in_app_sets(include_admin=False)]
    assert user_view == [SET_PUBLIC, SET_USER]

    admin_view = [s.name for s in tree.in_app_sets(include_admin=True)]
    assert admin_view == [SET_PUBLIC, SET_USER, SET_ADMIN]

    assert tree.public_set() is not None


def test_visible_topic_hides_admin_from_non_admin(tmp_path: Path) -> None:
    _write(tmp_path / SET_USER / "00-u.md", title="U", slug="u", audience=SET_USER)
    _write(tmp_path / SET_ADMIN / "00-a.md", title="A", slug="a", audience=SET_ADMIN)
    tree = load_docs(tmp_path)

    assert tree.visible_topic("a", include_admin=False) is None
    assert tree.visible_topic("a", include_admin=True) is not None
    assert tree.visible_topic("u", include_admin=False) is not None


def test_docset_topic_lookup_is_scoped_to_the_set(tmp_path: Path) -> None:
    # Backs the public-docs route: a slug from another set must not
    # resolve within the public set (no leaking user/admin docs pre-login).
    _write(tmp_path / SET_PUBLIC / "00-w.md", title="W", slug="welcome-x", audience=SET_PUBLIC)
    _write(tmp_path / SET_USER / "00-u.md", title="U", slug="user-x", audience=SET_USER)
    public = load_docs(tmp_path).public_set()
    assert public is not None
    assert public.topic("welcome-x") is not None
    assert public.topic("user-x") is None  # a user slug never resolves in the public set
