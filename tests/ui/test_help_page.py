"""Unit tests for the pure helpers in ``matofx.ui.pages.help``.

The page handler runs inside a NiceGUI page context the ASGI test
transport can't fully drive (see ``test_mount.py`` / ``test_home.py``),
so we exercise the pure topic-selection helper here; the role gating it
relies on lives on :class:`matofx.docs.models.DocTree` and is covered in
``tests/docs/test_loader.py``.
"""

from __future__ import annotations

from matofx.docs.models import DocSet, DocTopic
from matofx.ui.pages.help import _default_topic


def _topic(slug: str, set_name: str = "user") -> DocTopic:
    return DocTopic(
        set_name=set_name,
        slug=slug,
        title=slug.title(),
        audience=set_name,
        capability=None,
        summary="",
        order=0,
        filename=f"00-{slug}.md",
        body="Body.",
    )


def test_default_topic_picks_first_topic_of_first_nonempty_set() -> None:
    sets = (
        DocSet(name="user", title="User guide", topics=(_topic("a"), _topic("b"))),
        DocSet(name="admin", title="Admin guide", topics=(_topic("c", "admin"),)),
    )
    chosen = _default_topic(sets)
    assert chosen is not None
    assert chosen.slug == "a"


def test_default_topic_skips_empty_leading_set() -> None:
    sets = (
        DocSet(name="user", title="User guide", topics=()),
        DocSet(name="admin", title="Admin guide", topics=(_topic("c", "admin"),)),
    )
    chosen = _default_topic(sets)
    assert chosen is not None
    assert chosen.slug == "c"


def test_default_topic_returns_none_when_all_empty() -> None:
    sets = (DocSet(name="user", title="User guide", topics=()),)
    assert _default_topic(sets) is None
