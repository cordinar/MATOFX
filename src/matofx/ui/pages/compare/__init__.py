"""Compare workspace — side-by-side comparison of finished jobs.

The package replaces the original flat ``pages/compare.py`` placeholder
with a small set of single-purpose modules so each piece can be unit-
tested in isolation:

- :mod:`.diff` — pure ``compute_differing_keys`` over a list of bundles.
- :mod:`.selection` — URL-state parsing + cross-tool guard.
- :mod:`.render` — per-job cell rendering reusing the form-layer groups.
- :mod:`.overlay` — Plotly CSV overlays, theme-aware.
- :mod:`.page` — the :func:`@ui.page("/compare")` shell with Select / Compare tabs.

The page module is the only public entry point; everything else is an
implementation detail it composes.
"""

from __future__ import annotations

from matofx.ui.pages.compare.page import compare_page

__all__ = ["compare_page"]
