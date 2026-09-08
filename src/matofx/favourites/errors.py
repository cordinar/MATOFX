"""User-facing errors for the favourites service.

Plain English; safe to render directly to the end user. Shape
parallels :mod:`matofx.config.general_errors` so the API exception
handler can treat the domain uniformly.
"""

from __future__ import annotations


class FavouritesError(Exception):
    """Base class for favourites domain errors."""


class FavouritesDisabledError(FavouritesError):
    """The Quick Access favourites feature is turned off site-wide.

    Surfaces on mutation attempts when an administrator has set
    ``favourites_enabled=false`` in General Settings. Reads still
    work — they return an empty snapshot with ``enabled=False`` so
    the UI can hide the stars without an error path.
    """

    def __init__(self) -> None:
        """Construct with a fixed end-user message."""
        super().__init__(
            "Favourites are turned off site-wide. Ask an administrator "
            "to enable them on the General Settings page."
        )


class FavouritesUnknownKindError(FavouritesError):
    """The supplied favourite kind isn't a recognised Quick Access surface."""

    def __init__(self, kind: str) -> None:
        """Construct with the offending kind."""
        super().__init__(f"'{kind}' isn't a recognised favourite kind. Use 'tool' or 'workflow'.")
        self.kind = kind
