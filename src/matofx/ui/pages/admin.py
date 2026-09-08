"""Admin Space pages — Phase 2 placeholders for the legacy admin tree.

This module holds the remaining admin placeholder pages that are
sidebar-visible to administrators today as "Coming in v1.0" stubs so
the pilot user sees the right *shape* immediately. Real
implementations land iteratively over the next sprints.

Each page is gated by :func:`require_logged_in` (the sidebar already
hides the group from non-admins, but defence-in-depth: a regular
user typing ``/ui/admin/users`` directly should still see the
placeholder, not the system internals).
"""

from __future__ import annotations

from nicegui import ui

from matofx.ui.pages._stub import render_placeholder


@ui.page("/admin/import")
def admin_import_page() -> None:
    """Import packaged tools / workflows."""
    render_placeholder(
        title="Import tools and workflows",
        blurb=(
            "Drop a packaged tool or workflow bundle to install it. "
            "Reverses the legacy 'Export' button; useful for moving "
            "tools between dev / staging / prod installs. Lands in v1.0."
        ),
        icon="download",
    )
