"""Admin: V&V records (``/ui/admin/vv-records``) — folded into the workspace.

Historically this was a separate curator-inbox page that duplicated the
workspace's pending list. The 2026-07 rework merged it into the single
V&V register at ``/vv-records`` (which now carries the browsable list
plus a curator "Review queue" tab), so this route simply forwards there
— keeping any existing Admin-Space nav link / bookmark working.

Behind the same ``features.vv_tracking`` flag as the workspace: absent
when the feature is off, in which case we bounce to ``/admin``.
"""

from __future__ import annotations

from nicegui import ui

from matofx.ui._features import vv_tracking_enabled
from matofx.ui.pages._chrome import require_logged_in


@ui.page("/admin/vv-records")
async def admin_vv_records_page() -> None:
    """Redirect the old admin V&V index to the unified workspace."""
    if not require_logged_in():
        return
    ui.navigate.to("/vv-records" if vv_tracking_enabled() else "/admin")
