"""Slice 7 — enrichment orchestration in ``SummaryService`` (DB-backed).

A fake :class:`LlmClient` drives the four outcomes: a faithful completion is
cached + audited ``summary.generated``; a hallucinated number, an ``LlmError``,
and an unexpected error each fall back to the templated baseline + audit
``summary.enrichment_failed`` — and the public method never raises. A cache hit
must not call the model again.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from matofx.audit import AuditWriter, ensure_chain_head
from matofx.audit.orm import AuditLog
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.llm.client import LlmRequest, LlmResponse
from matofx.llm.errors import LlmTimeoutError
from matofx.summaries import EnrichmentContext, SummaryFacts, SummaryService

pytestmark = pytest.mark.asyncio


class FakeClient:
    """A scripted LlmClient: returns ``text``, or raises ``exc``, counting calls."""

    provider = "openai_compatible"
    model = "fake-1"

    def __init__(self, *, text: str = "", exc: Exception | None = None) -> None:
        self.text = text
        self.exc = exc
        self.calls = 0

    async def generate(self, _request: LlmRequest) -> LlmResponse:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return LlmResponse(text=self.text, model="fake-1", input_tokens=10, output_tokens=20)


def _facts(surface_id: int = 555) -> SummaryFacts:
    return SummaryFacts(
        surface_kind="job",
        surface_id=surface_id,
        title="enrich-job",
        state="complete",
        duration_seconds=252.0,
        details={"tool_label": "Heat sink", "exit_code": 0},
    )


# Faithful: only uses numbers present in the templated baseline (4, 12).
_FAITHFUL = "The Heat sink job finished successfully, taking 4 minutes and 12 seconds."
# Hallucinated: introduces a derived percentage the draft never stated.
_HALLUCINATED = "The Heat sink job completed in 4 min 12 s, a 30% improvement."


async def _ctx(audit: AuditWriter, **client_kwargs: Any) -> EnrichmentContext:
    return EnrichmentContext(client=FakeClient(**client_kwargs), audit=audit, actor="user:1")


async def _audit_actions(factory: SessionFactory) -> list[str]:
    async with session_scope(factory) as session:
        rows = (
            await session.execute(
                select(AuditLog.action).where(AuditLog.resource_type == "summary")
            )
        ).scalars()
        return list(rows)


async def _ensure_chain(factory: SessionFactory) -> None:
    async with session_scope(factory) as session:
        await ensure_chain_head(session)


async def test_faithful_completion_is_enriched_and_audited(
    api_factory: SessionFactory, api_audit: AuditWriter
) -> None:
    await _ensure_chain(api_factory)
    svc = SummaryService()
    ctx = await _ctx(api_audit, text=_FAITHFUL)
    async with session_scope(api_factory) as session:
        cached = await svc.build(session, _facts(), enrichment=ctx)
    assert cached.status == "enriched"
    assert cached.enriched is not None
    assert "4 minutes and 12 seconds" in " ".join(cached.enriched.paragraphs)
    assert "summary.generated" in await _audit_actions(api_factory)


async def test_hallucination_falls_back_and_audits(
    api_factory: SessionFactory, api_audit: AuditWriter
) -> None:
    await _ensure_chain(api_factory)
    svc = SummaryService()
    ctx = await _ctx(api_audit, text=_HALLUCINATED)
    async with session_scope(api_factory) as session:
        cached = await svc.build(session, _facts(surface_id=556), enrichment=ctx)
    assert cached.status == "fallback"
    assert cached.enriched is None
    assert "completed successfully" in " ".join(cached.templated.paragraphs)
    assert "summary.enrichment_failed" in await _audit_actions(api_factory)


async def test_llm_error_falls_back(api_factory: SessionFactory, api_audit: AuditWriter) -> None:
    await _ensure_chain(api_factory)
    svc = SummaryService()
    ctx = await _ctx(api_audit, exc=LlmTimeoutError("slow"))
    async with session_scope(api_factory) as session:
        cached = await svc.build(session, _facts(surface_id=557), enrichment=ctx)
    assert cached.status == "fallback"
    assert cached.enriched is None


async def test_unexpected_error_never_raises(
    api_factory: SessionFactory, api_audit: AuditWriter
) -> None:
    await _ensure_chain(api_factory)
    svc = SummaryService()
    ctx = await _ctx(api_audit, exc=ValueError("boom"))
    async with session_scope(api_factory) as session:
        cached = await svc.build(session, _facts(surface_id=558), enrichment=ctx)
    assert cached.status == "fallback"  # did not raise


async def test_cache_hit_does_not_call_the_model(
    api_factory: SessionFactory, api_audit: AuditWriter
) -> None:
    await _ensure_chain(api_factory)
    svc = SummaryService()
    client = FakeClient(text=_FAITHFUL)
    ctx = EnrichmentContext(client=client, audit=api_audit, actor="user:1")
    facts = _facts(surface_id=559)
    async with session_scope(api_factory) as session:
        await svc.build(session, facts, enrichment=ctx)
    async with session_scope(api_factory) as session:
        await svc.build(session, facts, enrichment=ctx)
    assert client.calls == 1  # second build was a cache hit
