"""Per-user favourites service.

List / add / remove with idempotent semantics. The service honours
the ``favourites_enabled`` general setting on mutations (raising
:class:`FavouritesDisabledError` when off); reads always succeed and
return ``enabled`` so the UI can decide whether to draw the stars
without an error round-trip.

Sort order returned to callers is alphabetical by alias. The UI
re-sorts by display name once it has the live tool / workflow lists
in hand (display name lives in those catalogs, not here).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from matofx.favourites.errors import (
    FavouritesDisabledError,
    FavouritesUnknownKindError,
)
from matofx.favourites.orm import FAVOURITE_KINDS, UserFavourite

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


_FEATURE_KEY = "favourites_enabled"


@dataclass(frozen=True)
class FavouritesSnapshot:
    """A single user's favourites + the site-wide enabled flag.

    ``tools`` and ``workflows`` are alphabetical alias lists. Empty
    lists are valid: a fresh user has no favourites; a disabled
    feature still returns whatever rows the user has saved (the
    flag tells the UI to hide the stars, not the data).
    """

    enabled: bool
    tools: list[str] = field(default_factory=list)
    workflows: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FavouritesService:
    """List / add / remove operations on :class:`UserFavourite`.

    Stateless — construct one per request, or share a single
    instance across the process. Both are safe.

    Mutations consult the live ``favourites_enabled`` general
    setting on every call; admins disabling the feature take effect
    immediately for the next ``add`` / ``remove`` without a process
    restart. Reads ignore the flag (the snapshot carries it) so the
    UI can decide rendering without a domain-error round-trip.
    """

    async def snapshot(self, session: AsyncSession, *, user_id: int) -> FavouritesSnapshot:
        """Return the user's current favourites + the site-wide flag.

        Both lists are alphabetical by alias. Never raises on a
        well-formed call — even with the feature disabled, the
        existing rows are returned so the UI can choose to render
        nothing without losing data.
        """
        enabled = await self._enabled(session)
        rows = (
            await session.execute(
                select(UserFavourite.kind, UserFavourite.alias)
                .where(UserFavourite.user_id == user_id)
                .order_by(UserFavourite.kind, UserFavourite.alias)
            )
        ).all()
        tools: list[str] = []
        workflows: list[str] = []
        for kind, alias in rows:
            if kind == "tool":
                tools.append(alias)
            elif kind == "workflow":
                workflows.append(alias)
        return FavouritesSnapshot(enabled=enabled, tools=tools, workflows=workflows)

    async def add(
        self, session: AsyncSession, *, user_id: int, kind: str, alias: str
    ) -> FavouritesSnapshot:
        """Idempotently star ``alias`` of ``kind`` for the user.

        Re-favouriting an already-favourited item is a no-op (the
        composite PK makes the second INSERT a no-op rather than a
        constraint violation). Returns the updated snapshot.

        Raises:
            FavouritesDisabledError: the admin toggle is off.
            FavouritesUnknownKindError: ``kind`` isn't tool/workflow.
        """
        await self._require_enabled(session)
        self._validate_kind(kind)
        existing = await session.get(UserFavourite, (user_id, kind, alias))
        if existing is None:
            session.add(UserFavourite(user_id=user_id, kind=kind, alias=alias))
            await session.flush()
        return await self.snapshot(session, user_id=user_id)

    async def remove(
        self, session: AsyncSession, *, user_id: int, kind: str, alias: str
    ) -> FavouritesSnapshot:
        """Idempotently un-star ``alias`` of ``kind`` for the user.

        Removing a not-favourited item is a no-op (the DELETE simply
        affects zero rows). Returns the updated snapshot.

        Raises:
            FavouritesDisabledError: the admin toggle is off.
            FavouritesUnknownKindError: ``kind`` isn't tool/workflow.
        """
        await self._require_enabled(session)
        self._validate_kind(kind)
        await session.execute(
            delete(UserFavourite).where(
                UserFavourite.user_id == user_id,
                UserFavourite.kind == kind,
                UserFavourite.alias == alias,
            )
        )
        await session.flush()
        return await self.snapshot(session, user_id=user_id)

    # ----- helpers ------------------------------------------------------

    @staticmethod
    def _validate_kind(kind: str) -> None:
        if kind not in FAVOURITE_KINDS:
            raise FavouritesUnknownKindError(kind)

    @staticmethod
    async def _enabled(session: AsyncSession) -> bool:
        # Imported lazily (not at module top) to keep ``matofx.favourites``
        # free of a module-level dependency on ``config.general_service``.
        # The favourites ORM is pulled in by ``matofx.db.all_orm`` for mapper
        # registration, and an eager import here can re-enter a half-built
        # ``general_service`` under some import orders (a circular-import crash).
        from matofx.config.general_service import load_general_settings  # noqa: PLC0415

        snapshot = await load_general_settings(session)
        value = snapshot.get(_FEATURE_KEY, False)
        return bool(value)

    @classmethod
    async def _require_enabled(cls, session: AsyncSession) -> None:
        if not await cls._enabled(session):
            raise FavouritesDisabledError()


def make_favourites_service() -> FavouritesService:
    """Factory matching the rest of the service-layer style."""
    return FavouritesService()
