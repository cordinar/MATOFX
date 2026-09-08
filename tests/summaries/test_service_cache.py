"""Slice 5 — the ``result_summary`` cache + ``SummaryService``.

Exercises render-on-miss, reuse-on-hit, and facts-hash invalidation against a
real migrated DB (so it also proves migration 0071 applies).
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import pytest
from sqlalchemy import func, select

from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.summaries import SummaryFacts, SummaryService, SummaryText
from matofx.summaries.orm import ResultSummary
from matofx.summaries.service import CachedSummary

pytestmark = pytest.mark.asyncio


def _facts(**overrides: Any) -> SummaryFacts:
    base: dict[str, Any] = {
        "surface_kind": "job",
        "surface_id": 4242,
        "title": "cache-test",
        "state": "complete",
        "duration_seconds": 252.0,
        "details": {"tool_label": "Heat sink", "exit_code": 0},
    }
    base.update(overrides)
    return SummaryFacts(**base)


async def _row_count(factory: SessionFactory) -> int:
    async with session_scope(factory) as session:
        return int(
            (await session.execute(select(func.count(ResultSummary.id)))).scalar_one()
        )


async def _row_generated_at(factory: SessionFactory, surface_id: int) -> Any:
    async with session_scope(factory) as session:
        return (
            await session.execute(
                select(ResultSummary.generated_at).where(
                    ResultSummary.surface_id == surface_id
                )
            )
        ).scalar_one()


async def test_renders_and_caches_on_miss(api_factory: SessionFactory) -> None:
    svc = SummaryService()
    async with session_scope(api_factory) as session:
        cached = await svc.build(session, _facts())
    assert cached.status == "templated"
    assert cached.enriched is None
    assert "completed successfully" in " ".join(cached.templated.paragraphs)
    assert await _row_count(api_factory) == 1


async def test_second_build_is_a_cache_hit(api_factory: SessionFactory) -> None:
    svc = SummaryService()
    async with session_scope(api_factory) as session:
        await svc.build(session, _facts())
    before = await _row_generated_at(api_factory, 4242)
    async with session_scope(api_factory) as session:
        second = await svc.build(session, _facts())
    after = await _row_generated_at(api_factory, 4242)
    # A cache hit does not rewrite the row, and returns the stored timestamp.
    assert before == after
    assert second.generated_at == after
    assert await _row_count(api_factory) == 1


async def test_facts_change_invalidates_the_cache(api_factory: SessionFactory) -> None:
    svc = SummaryService()
    async with session_scope(api_factory) as session:
        first = await svc.build(session, _facts())
    # A different fact (longer duration) → different hash → re-render in place.
    async with session_scope(api_factory) as session:
        second = await svc.build(session, _facts(duration_seconds=999.0))
    assert second.generated_at >= first.generated_at
    assert "16 min" in " ".join(second.templated.paragraphs)
    # Still one row per (surface_kind, surface_id) — overwrite, not insert.
    assert await _row_count(api_factory) == 1


async def test_distinct_surfaces_get_distinct_rows(api_factory: SessionFactory) -> None:
    svc = SummaryService()
    async with session_scope(api_factory) as session:
        await svc.build(session, _facts(surface_id=1))
        await svc.build(session, _facts(surface_id=2))
    assert await _row_count(api_factory) == 2


async def test_cached_render_for_prefers_enriched_when_present() -> None:
    templated = SummaryText(title="Summary", paragraphs=("plain",))
    enriched = SummaryText(title="Summary", paragraphs=("fancy",))
    cached = CachedSummary(templated, enriched, "enriched", _dt.datetime.now(tz=_dt.UTC))
    assert cached.render_for(prefer_enriched=True) is enriched
    assert cached.render_for(prefer_enriched=False) is templated
    plain_only = CachedSummary(templated, None, "templated", _dt.datetime.now(tz=_dt.UTC))
    assert plain_only.render_for(prefer_enriched=True) is templated
