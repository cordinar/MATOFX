"""Data model for the in-app / standalone documentation tree.

A *topic* is one Markdown file (one chapter of a guide); a *set* is a
directory of topics with a shared audience (``public`` / ``user`` /
``admin``); the *tree* is all three sets discovered on disk. The model
is pure data — discovery + parsing live in
:mod:`matofx.docs.loader`, rendering lives in the ``/help`` page — so
the gating and lookup logic here is unit-testable without NiceGUI.
"""

from __future__ import annotations

from dataclasses import dataclass

SET_PUBLIC = "public"
SET_USER = "user"
SET_ADMIN = "admin"

#: Directory name → human-facing set title, used for the TOC headings
#: and the standalone build. Order here is the canonical set order.
SET_TITLES: dict[str, str] = {
    SET_PUBLIC: "Getting started",
    SET_USER: "User guide",
    SET_ADMIN: "Administrator guide",
}


@dataclass(frozen=True)
class DocTopic:
    """One documentation topic — a single Markdown chapter file.

    Attributes:
        set_name: The set this topic belongs to (``public`` / ``user`` /
            ``admin``); always equal to the topic's directory and its
            ``audience`` front-matter.
        slug: Stable anchor (e.g. ``jobs/submitting``) — the contract a
            page's ``help_topic`` targets and the ``?topic=`` query value.
        title: Human-facing chapter title.
        audience: ``public`` / ``user`` / ``admin``; validated to match
            :attr:`set_name` at load time.
        capability: Optional capability/flag this chapter is about (e.g.
            ``approver``), for "if you have this capability" call-outs.
            ``None`` for general chapters.
        summary: One-line description used by search and the TOC.
        order: Sort key from the filename's numeric prefix (``10-...``).
        filename: Source filename, used as a stable tiebreak in sorting.
        body: The Markdown body with the front-matter block stripped.
    """

    set_name: str
    slug: str
    title: str
    audience: str
    capability: str | None
    summary: str
    order: int
    filename: str
    body: str


@dataclass(frozen=True)
class DocSet:
    """An ordered group of topics sharing one audience."""

    name: str
    title: str
    topics: tuple[DocTopic, ...]

    def first(self) -> DocTopic | None:
        """Return the first topic in display order, or ``None`` when empty."""
        return self.topics[0] if self.topics else None

    def topic(self, slug: str) -> DocTopic | None:
        """Return this set's topic with ``slug``, or ``None``.

        Used to resolve a ``?topic=`` value *within a single set* — the
        unauthenticated public-docs route relies on this so a slug from
        the ``user`` / ``admin`` guides can't be rendered before sign-in.
        """
        for topic in self.topics:
            if topic.slug == slug:
                return topic
        return None


@dataclass(frozen=True)
class DocTree:
    """The full set of discovered documentation, keyed by set."""

    sets: tuple[DocSet, ...]

    def get_set(self, name: str) -> DocSet | None:
        """Return the named set, or ``None`` when it doesn't exist."""
        for doc_set in self.sets:
            if doc_set.name == name:
                return doc_set
        return None

    def topic(self, slug: str) -> DocTopic | None:
        """Return the topic with ``slug`` from any set, or ``None``."""
        for doc_set in self.sets:
            for topic in doc_set.topics:
                if topic.slug == slug:
                    return topic
        return None

    def public_set(self) -> DocSet | None:
        """Return the login-screen ``public`` set, or ``None``."""
        return self.get_set(SET_PUBLIC)

    def in_app_sets(self, *, include_admin: bool) -> tuple[DocSet, ...]:
        """Return the sets shown in the in-app Help hub, in order.

        The login-screen ``public`` "Getting started" set is listed
        **first** so the guide is discoverable inside Help Documentation
        as well as on the pre-login page; the ``user`` guide always
        follows; the ``admin`` guide is included only when
        ``include_admin`` is true (the caller passes
        :func:`matofx.ui.state.is_admin`). The public set leaks nothing
        post-login — it is already reachable unauthenticated.
        """
        result: list[DocSet] = []
        public_set = self.get_set(SET_PUBLIC)
        if public_set is not None:
            result.append(public_set)
        user_set = self.get_set(SET_USER)
        if user_set is not None:
            result.append(user_set)
        if include_admin:
            admin_set = self.get_set(SET_ADMIN)
            if admin_set is not None:
                result.append(admin_set)
        return tuple(result)

    def visible_topic(self, slug: str, *, include_admin: bool) -> DocTopic | None:
        """Return ``slug`` only when it sits in an in-app-visible set.

        Gates direct ``?topic=`` access: an ``admin`` slug requested by a
        non-admin resolves to ``None`` so the page can fall back to the
        hub root rather than leak an admin chapter.
        """
        for doc_set in self.in_app_sets(include_admin=include_admin):
            for topic in doc_set.topics:
                if topic.slug == slug:
                    return topic
        return None
