"""Helper for "Coming in Phase 2" placeholder pages.

The legacy MATOFX has many features we know we'll need but haven't
ported yet. Rather than show a 404 (or worse, hide the nav item) we
register every nav target as a real page that explains what'll be
there. Keeps the sidebar shape recognisable to pilot users from day
one and lets them try every link without surprises.
"""

from __future__ import annotations

from nicegui import ui

from matofx.ui.pages._chrome import render_shell, require_logged_in


def render_placeholder(*, title: str, blurb: str, icon: str = "construction") -> None:
    """Render the standard "Coming in Phase 2" body.

    Pages that haven't been built yet call this from inside their own
    ``@ui.page(...)`` handler.
    """
    if not require_logged_in():
        return
    render_shell()
    with (
        ui.column()
        .classes("w-full max-w-3xl mx-auto p-6 gap-4 items-center text-center")
        .style("padding-top: 4rem;")
    ):
        ui.icon(icon).style("font-size: 4rem; color: #40A1A4;")
        ui.label(title).style("font-size: 1.6rem; font-weight: 600;")
        ui.label(blurb).style("color: #555; max-width: 36rem;")
        ui.label("This page is on the v1.0 roadmap; it's not built yet.").style(
            "color: #888; font-size: 0.85rem; margin-top: 1rem;"
        )
