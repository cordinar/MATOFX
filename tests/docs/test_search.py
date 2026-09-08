"""Tests for documentation search matching."""

from __future__ import annotations

from matofx.docs.models import DocTopic
from matofx.docs.search import search_topics, searchable_text


def _topic(slug: str, title: str, summary: str = "", body: str = "") -> DocTopic:
    return DocTopic(
        set_name="user",
        slug=slug,
        title=title,
        audience="user",
        capability=None,
        summary=summary,
        order=0,
        filename=f"00-{slug}.md",
        body=body,
    )


def test_searchable_text_is_lowercased_and_spans_fields() -> None:
    text = searchable_text(_topic("x", "Title", summary="Summary", body="BodyWord"))
    assert "title" in text
    assert "summary" in text
    assert "bodyword" in text


def test_blank_query_matches_nothing() -> None:
    topics = [_topic("a", "Alpha"), _topic("b", "Beta")]
    assert search_topics(topics, "") == []
    assert search_topics(topics, "   ") == []


def test_match_is_case_insensitive_and_spans_body() -> None:
    topics = [
        _topic("a", "Submitting a job", body="Fill in the form and click submit."),
        _topic("b", "Comparing jobs", body="Overlay results."),
    ]
    hits = search_topics(topics, "FORM")
    assert [t.slug for t in hits] == ["a"]


def test_all_terms_must_match() -> None:
    topics = [
        _topic("a", "Submitting a job", summary="queue and run"),
        _topic("b", "Job approvals", summary="approve before it runs"),
    ]
    # Both terms appear only in 'a'.
    assert [t.slug for t in search_topics(topics, "submitting queue")] == ["a"]
    # 'queue' isn't in 'b', so requiring both excludes everything.
    assert search_topics(topics, "approvals queue") == []


def test_results_preserve_input_order() -> None:
    topics = [_topic("a", "Job one"), _topic("b", "Job two"), _topic("c", "Job three")]
    assert [t.slug for t in search_topics(topics, "job")] == ["a", "b", "c"]
