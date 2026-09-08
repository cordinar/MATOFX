"""Summary orchestration + caching.

:class:`SummaryService` turns a collected :class:`SummaryFacts` into a rendered,
cached :class:`CachedSummary`. The templated baseline is rendered by the pure
per-surface generator chosen from ``facts.surface_kind``; the result is cached in
``result_summary`` keyed by the facts hash, so repeat views of an unchanged
result reuse the row instead of re-rendering (and, once Slice 7 lands, re-hitting
the LLM). A facts change (new hash) overwrites the row.

The collect step (with its per-surface ACL) lives in the API layer; this service
takes the already-collected, already-authorised facts and owns only the render +
cache.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.audit import AuditWriter
from matofx.audit.schema import RecordInput
from matofx.llm.client import LlmClient, LlmResponse
from matofx.llm.errors import LlmError
from matofx.obs import get_logger
from matofx.summaries.errors import UnknownSurfaceKindError
from matofx.summaries.facts import SummaryFacts
from matofx.summaries.grounding import assemble_prompt, run_guards
from matofx.summaries.narrative import (
    SummaryText,
    render_job,
    render_optimisation,
    render_parametric,
    render_workflow,
)
from matofx.summaries.orm import ResultSummary

_log = get_logger(__name__)

_RENDERERS: dict[str, Callable[[SummaryFacts], SummaryText]] = {
    "job": render_job,
    "workflow_run": render_workflow,
    "parametric_study": render_parametric,
    "optimisation_study": render_optimisation,
}


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


@dataclass(frozen=True)
class CachedSummary:
    """A rendered summary plus its cache metadata."""

    templated: SummaryText
    enriched: SummaryText | None
    status: str
    generated_at: _dt.datetime

    def render_for(self, *, prefer_enriched: bool) -> SummaryText:
        """The text to show a viewer: enriched when present + not opted out."""
        if prefer_enriched and self.enriched is not None:
            return self.enriched
        return self.templated


@dataclass(frozen=True)
class EnrichmentContext:
    """What :meth:`SummaryService.build` needs to attempt LLM enrichment.

    Passed only when the install has enrichment configured **and** the
    ``llm_summaries_enabled`` runtime setting is on (Slice 8). It is *not*
    gated on the viewer's opt-out — enrichment runs once per result and both
    texts are cached, so the per-user opt-out merely selects which to render.
    """

    client: LlmClient
    audit: AuditWriter
    actor: str
    actor_type: str = "user"


@dataclass(frozen=True)
class _Enrichment:
    text: SummaryText | None
    status: str
    model: str | None


class SummaryService:
    """Render + cache narratives for terminal results."""

    async def build(
        self,
        session: AsyncSession,
        facts: SummaryFacts,
        *,
        enrichment: EnrichmentContext | None = None,
    ) -> CachedSummary:
        """Return the cached summary for ``facts``, rendering + caching on miss.

        A cache hit requires a row whose ``facts_hash`` matches the current
        facts; any change to the underlying result busts it (and re-runs
        enrichment). The templated render is deterministic, so a concurrent
        insert resolves to the winner's identical row. The enrichment branch is
        **never-raise**: any failure falls back to the templated baseline.
        """
        renderer = _RENDERERS.get(facts.surface_kind)
        if renderer is None:  # pragma: no cover - guarded by the caller's dispatch
            raise UnknownSurfaceKindError(facts.surface_kind)

        facts_hash = facts.facts_hash()
        row = await self._load(session, facts)
        if row is not None and row.facts_hash == facts_hash:
            return _from_row(row)

        templated = renderer(facts)
        result = await self._maybe_enrich(session, facts, templated, enrichment)
        now = _utcnow()
        if row is not None:
            _apply(row, facts_hash, templated, result, now)
            await session.flush()
            return CachedSummary(templated, result.text, result.status, now)

        row = ResultSummary(
            surface_kind=facts.surface_kind, surface_id=facts.surface_id
        )
        _apply(row, facts_hash, templated, result, now)
        session.add(row)
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            # A concurrent request inserted the row first — adopt the winner.
            session.expunge(row)
            existing = await self._load(session, facts)
            if existing is not None:
                return _from_row(existing)
        return CachedSummary(templated, result.text, result.status, now)

    async def _maybe_enrich(
        self,
        session: AsyncSession,
        facts: SummaryFacts,
        templated: SummaryText,
        ctx: EnrichmentContext | None,
    ) -> _Enrichment:
        """Attempt enrichment; fall back to templated on *any* failure."""
        if ctx is None:
            return _Enrichment(None, "templated", None)
        model_label = f"{ctx.client.provider}:{ctx.client.model}"
        try:
            response = await ctx.client.generate(assemble_prompt(facts, templated))
        except LlmError as exc:
            await self._audit_failed(session, ctx, facts, type(exc).__name__)
            return _Enrichment(None, "fallback", model_label)
        except Exception:  # never-raise: enrichment is decorative.
            _log.warning(
                "summary enrichment: unexpected error surface=%s id=%s",
                facts.surface_kind,
                facts.surface_id,
            )
            await self._audit_failed(session, ctx, facts, "unexpected")
            return _Enrichment(None, "fallback", model_label)

        guard = run_guards(response.text, facts, templated)
        if not guard.ok:
            await self._audit_failed(session, ctx, facts, f"guard:{guard.failed}")
            return _Enrichment(None, "fallback", model_label)

        enriched = SummaryText(
            title=templated.title, paragraphs=_split_paragraphs(response.text)
        )
        await self._audit_generated(session, ctx, facts, response)
        return _Enrichment(enriched, "enriched", response.model)

    async def _audit_generated(
        self,
        session: AsyncSession,
        ctx: EnrichmentContext,
        facts: SummaryFacts,
        response: LlmResponse,
    ) -> None:
        await self._record(
            session,
            ctx,
            facts,
            action="summary.generated",
            outcome="success",
            payload={
                "surface_kind": facts.surface_kind,
                "provider": ctx.client.provider,
                "model": response.model,
                "facts_hash": facts.facts_hash(),
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
            },
        )

    async def _audit_failed(
        self,
        session: AsyncSession,
        ctx: EnrichmentContext,
        facts: SummaryFacts,
        reason: str,
    ) -> None:
        await self._record(
            session,
            ctx,
            facts,
            action="summary.enrichment_failed",
            outcome="failure",
            payload={
                "surface_kind": facts.surface_kind,
                "provider": ctx.client.provider,
                "model": ctx.client.model,
                "reason": reason,
            },
        )

    async def _record(
        self,
        session: AsyncSession,
        ctx: EnrichmentContext,
        facts: SummaryFacts,
        *,
        action: str,
        outcome: str,
        payload: dict[str, object],
    ) -> None:
        """Best-effort audit — an audit failure never sinks the enrichment."""
        try:
            await ctx.audit.record(
                session,
                RecordInput(
                    actor=ctx.actor,
                    actor_type=ctx.actor_type,  # type: ignore[arg-type]
                    action=action,
                    resource_type="summary",
                    resource_id=str(facts.surface_id),
                    outcome=outcome,  # type: ignore[arg-type]
                    payload=payload,
                ),
            )
        except Exception:  # audit is best-effort here.
            _log.warning("summary audit failed: action=%s", action)

    async def _load(
        self, session: AsyncSession, facts: SummaryFacts
    ) -> ResultSummary | None:
        return (
            await session.execute(
                select(ResultSummary).where(
                    ResultSummary.surface_kind == facts.surface_kind,
                    ResultSummary.surface_id == facts.surface_id,
                )
            )
        ).scalar_one_or_none()


def _split_paragraphs(text: str) -> tuple[str, ...]:
    """Split LLM prose into paragraphs on blank lines (single-paragraph fallback)."""
    parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    return tuple(parts) if parts else (text.strip(),)


def _apply(
    row: ResultSummary,
    facts_hash: str,
    templated: SummaryText,
    enrichment: _Enrichment,
    now: _dt.datetime,
) -> None:
    """Write the templated baseline + enrichment outcome onto a cache row."""
    row.facts_hash = facts_hash
    row.templated_text = templated.to_dict()
    row.enriched_text = enrichment.text.to_dict() if enrichment.text else None
    row.enrichment_status = enrichment.status
    row.enrichment_model = enrichment.model
    row.generated_at = now


def _from_row(row: ResultSummary) -> CachedSummary:
    enriched = (
        SummaryText.from_dict(row.enriched_text)
        if row.enriched_text is not None
        else None
    )
    return CachedSummary(
        templated=SummaryText.from_dict(row.templated_text),
        enriched=enriched,
        status=row.enrichment_status,
        generated_at=row.generated_at,
    )


def make_summary_service() -> SummaryService:
    """Construct the (stateless) summary service."""
    return SummaryService()


async def enrichment_context_if_enabled(
    session: AsyncSession,
    *,
    settings: Any,
    audit: AuditWriter,
    actor: str,
    actor_type: str = "user",
) -> EnrichmentContext | None:
    """Build an :class:`EnrichmentContext` iff the install has enrichment on.

    Gate order (auto-summaries.md §5.2): a configured ``[llm]`` provider →
    the ``llm_summaries_enabled`` runtime setting on → a constructible client.
    Returns ``None`` otherwise, so callers fall back to templated-only. The
    viewer's opt-out is *not* part of this gate — it only selects which cached
    text gets rendered.
    """
    from matofx.config import load_general_settings  # noqa: PLC0415
    from matofx.llm import make_llm_client  # noqa: PLC0415

    if settings.llm.provider == "disabled":
        return None
    values = await load_general_settings(session)
    if not bool(values.get("llm_summaries_enabled")):
        return None
    client = make_llm_client(settings.llm)
    if client is None:  # pragma: no cover - provider != disabled already checked
        return None
    return EnrichmentContext(
        client=client, audit=audit, actor=actor, actor_type=actor_type
    )
