"""Conftest for the summaries tests — re-exports shared API fixtures.

The pure narrative/facts tests need none of these; the collector tests
(``test_collect_*``) use the migrated-DB ``api_factory`` + ``seeded`` fixtures
to exercise collection against real rows.
"""

from __future__ import annotations

from tests._api_fixtures import *  # noqa: F403  -- re-export for fixture discovery
