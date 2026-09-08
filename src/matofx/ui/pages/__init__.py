"""NiceGUI page modules — one per route.

Each module registers its route via ``@ui.page("...")`` at import time.
:func:`matofx.ui.app.mount_ui` imports this package which in turn
imports every page module, fixing the route registration order.
"""

from __future__ import annotations
