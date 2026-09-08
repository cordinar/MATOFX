"""Conftest for the favourites service tests — re-exports shared fixtures.

We reuse the same alembic-migrated DB + seeded super-user fixtures
the API tests use; the service tests bypass HTTP and call the
service directly, but they still need a real users.id to attach
favourites to.
"""

from __future__ import annotations

from tests._api_fixtures import *  # noqa: F403 -- re-export for fixture discovery
