"""Documentation search — pure matching over titles, summaries, and bodies.

Kept free of any UI dependency so the in-app hub (server-side filtering
of the in-memory tree) and the future standalone static-site index
(release build) share one definition of "what matches a query". Search
is a simple case-insensitive *all-terms-must-appear* substring match —
the tree is small (dozens of chapters), so there's no need for a ranked
index.
"""

from __future__ import annotations

from collections.abc import Iterable

from matofx.docs.models import DocTopic


def searchable_text(topic: DocTopic) -> str:
    """Return the lower-cased haystack for ``topic`` (title + summary + body)."""
    return f"{topic.title}\n{topic.summary}\n{topic.body}".lower()


def search_topics(topics: Iterable[DocTopic], query: str) -> list[DocTopic]:
    """Return the topics matching ``query``, preserving input order.

    Every whitespace-separated term in ``query`` must appear somewhere in
    a topic's searchable text for it to match. A blank query matches
    nothing (the caller shows the normal table of contents instead).
    """
    terms = query.lower().split()
    if not terms:
        return []
    result: list[DocTopic] = []
    for topic in topics:
        haystack = searchable_text(topic)
        if all(term in haystack for term in terms):
            result.append(topic)
    return result
