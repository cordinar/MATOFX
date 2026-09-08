"""Shared Summary card for result pages.

Renders the auto-generated results summary (``designs/auto-summaries.md``)
for any surface — job, workflow run, parametric study, optimisation study — by
calling ``GET /api/summaries/{kind}/{id}``. Best-effort: any API failure or an
empty summary collapses to nothing rather than breaking the host page.

When enrichment is enabled and the result carries guard-passed LLM prose, the
card shows that text with an "AI-assisted summary" tag (the honest provenance
signal); otherwise it shows the deterministic templated baseline.
"""

from __future__ import annotations

from nicegui import ui

from matofx.ui.api_client import ApiClient, ApiError


async def render_summary_card(client: ApiClient, kind: str, surface_id: int) -> bool:
    """Render the Summary card for one result, or nothing on any failure.

    Returns ``True`` when a card was actually drawn, ``False`` when there was
    nothing to show (API failure, or an in-flight surface whose summary 404s).
    Callers that re-render on a poll use this to stop retrying once the summary
    lands — see ``workflow_run._refresh_summary_card``.
    """
    try:
        data = await client.get_summary(kind, surface_id)
    except ApiError:
        return False

    enriched = data.get("enriched")
    templated = data.get("templated") or {}
    chosen = enriched if isinstance(enriched, dict) and enriched.get("paragraphs") else templated
    paragraphs = [str(p) for p in (chosen.get("paragraphs") or []) if str(p).strip()]
    if not paragraphs:
        return False
    is_ai = chosen is enriched

    with ui.card().classes("w-full"), ui.column().classes("w-full gap-1"):
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            ui.label("Summary").style("font-size: 1rem; font-weight: 600;")
            if is_ai:
                ui.label("AI-assisted summary").classes("text-xs").style(
                    "color: var(--q-primary); opacity: 0.85;"
                )
        for para in paragraphs:
            ui.label(para).classes("text-sm").style("line-height: 1.5;")
    return True
