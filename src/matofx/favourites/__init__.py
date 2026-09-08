"""Per-user favourites for Quick Access tools and workflows.

Each user can star tools and workflows they reach for often; starred
items float to the top of their Quick Access section on the Home
page (alphabetical within the favourited group, then alphabetical
within the unfavourited group). The feature is gated by the
``favourites_enabled`` general setting — when an administrator turns
it off, stars are hidden and sort reverts to default ordering, but
the underlying rows are preserved so flipping the setting back on
restores every user's preferences.

Public surface:

- :class:`UserFavourite` — ORM row backing the ``user_favourite``
  table; primary key is ``(user_id, kind, alias)``.
- :class:`FavouritesService` — list / add / remove with idempotent
  semantics. Honours the admin toggle by raising
  :class:`FavouritesDisabledError` on mutations when off.
- :class:`FavouritesSnapshot` — read-side dataclass returned by the
  service and serialised onto the wire by the API router.
- :data:`FAVOURITE_KINDS` — the allowed ``kind`` discriminator values
  (``'tool'`` and ``'workflow'``).
"""

from __future__ import annotations

from matofx.favourites.errors import (
    FavouritesDisabledError,
    FavouritesError,
    FavouritesUnknownKindError,
)
from matofx.favourites.orm import FAVOURITE_KINDS, UserFavourite
from matofx.favourites.service import (
    FavouritesService,
    FavouritesSnapshot,
    make_favourites_service,
)

__all__ = [
    "FAVOURITE_KINDS",
    "FavouritesDisabledError",
    "FavouritesError",
    "FavouritesService",
    "FavouritesSnapshot",
    "FavouritesUnknownKindError",
    "UserFavourite",
    "make_favourites_service",
]
