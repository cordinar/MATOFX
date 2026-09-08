"""Team-management service.

Build once at process startup with :func:`make_team_service`. Every
mutation records an audit event and is gated on the
``teams.manage`` permission (default: super_user, administrator).

Read-side methods (``list_teams``, ``list_user_teams``,
``list_team_members``) are not permission-gated — team membership is
visible to anyone authenticated. Tighten in Phase 2 if customers need
team confidentiality.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from matofx.audit.schema import RecordInput
from matofx.users.errors import (
    AlreadyInTeamError,
    DuplicateTeamError,
    NotInTeamError,
    PermissionDeniedError,
    TeamInUseError,
    TeamNotFoundError,
)
from matofx.users.orm import User
from matofx.users.permissions import Permission, user_has_permission
from matofx.users.team_orm import Team, UserTeam

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter


@dataclass(frozen=True)
class TeamService:
    """CRUD for teams and team membership."""

    audit: AuditWriter

    # ---- read ----

    async def list_teams(self, session: AsyncSession) -> list[Team]:
        """Return every team in the install, ordered by name."""
        return list((await session.execute(select(Team).order_by(Team.name))).scalars())

    async def get_by_name(self, session: AsyncSession, name: str) -> Team:
        """Look up a team by name; raises if it doesn't exist."""
        team = (
            await session.execute(select(Team).where(Team.name == name.strip()))
        ).scalar_one_or_none()
        if team is None:
            raise TeamNotFoundError(name)
        return team

    async def list_user_teams(self, session: AsyncSession, user_id: int) -> list[Team]:
        """Return every team the user is a member of, ordered by team name."""
        stmt = (
            select(Team)
            .join(UserTeam, UserTeam.team_id == Team.id)
            .where(UserTeam.user_id == user_id)
            .order_by(Team.name)
        )
        return list((await session.execute(stmt)).scalars())

    async def list_team_members(self, session: AsyncSession, team_id: int) -> list[User]:
        """Return every user in a team, ordered by username."""
        stmt = (
            select(User)
            .join(UserTeam, UserTeam.user_id == User.id)
            .where(UserTeam.team_id == team_id)
            .order_by(User.username)
        )
        return list((await session.execute(stmt)).scalars())

    # ---- create / delete ----

    async def create_team(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        name: str,
        description: str = "",
    ) -> Team:
        """Create a new team. Requires the ``teams.manage`` permission."""
        await self._require_manage(session, actor_user_id)
        name = name.strip()
        if not name:
            raise DuplicateTeamError("")
        team = Team(name=name, description=description, created_by_user_id=actor_user_id)
        session.add(team)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateTeamError(name) from exc

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="users.team.create",
                resource_type="team",
                resource_id=str(team.id),
                outcome="success",
                payload={"name": team.name},
            ),
        )
        return team

    async def update_team(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        team_id: int,
        name: str,
        description: str,
    ) -> Team:
        """Rename a team and/or rewrite its description. Requires ``teams.manage``.

        Memberships are untouched — they bind by ``team.id`` — so a rename
        carries no risk of orphaning a reference. The new name can't
        collide with another team (:exc:`DuplicateTeamError`).
        """
        await self._require_manage(session, actor_user_id)
        team = await session.get(Team, team_id)
        if team is None:
            raise TeamNotFoundError(team_id)
        name = name.strip()
        if not name:
            raise DuplicateTeamError("")

        old_name = team.name
        team.name = name
        team.description = description
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateTeamError(name) from exc

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="users.team.update",
                resource_type="team",
                resource_id=str(team_id),
                outcome="success",
                payload={"old_name": old_name, "new_name": name},
            ),
        )
        return team

    async def delete_team(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        team_id: int,
    ) -> None:
        """Delete an empty team. Refuses if any user is still a member."""
        await self._require_manage(session, actor_user_id)
        team = await session.get(Team, team_id)
        if team is None:
            raise TeamNotFoundError(team_id)

        members = (
            (await session.execute(select(UserTeam).where(UserTeam.team_id == team_id)))
            .scalars()
            .all()
        )
        if members:
            raise TeamInUseError(team.name, len(members))

        await session.delete(team)
        await session.flush()
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="users.team.delete",
                resource_type="team",
                resource_id=str(team_id),
                outcome="success",
                payload={"name": team.name},
            ),
        )

    # ---- membership ----

    async def add_user_to_team(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        user_id: int,
        team_id: int,
    ) -> UserTeam:
        """Add a user to a team. Requires ``teams.manage``."""
        await self._require_manage(session, actor_user_id)
        team = await session.get(Team, team_id)
        if team is None:
            raise TeamNotFoundError(team_id)
        user = await session.get(User, user_id)
        if user is None:
            raise TeamNotFoundError(user_id)

        existing = await session.get(UserTeam, (user_id, team_id))
        if existing is not None:
            raise AlreadyInTeamError(user.username, team.name)

        membership = UserTeam(
            user_id=user_id,
            team_id=team_id,
            joined_by_user_id=actor_user_id,
        )
        session.add(membership)
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="users.team.assign",
                resource_type="user",
                resource_id=str(user_id),
                outcome="success",
                payload={"team_id": team_id, "team_name": team.name},
            ),
        )
        return membership

    async def add_users_to_team(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        team_id: int,
        user_ids: Iterable[int],
    ) -> int:
        """Add many users to one team in a single call. Requires ``teams.manage``.

        Powers the multi-select *Add* on the team's card: the operator
        picks several users and presses *Add* once. Users already in the
        team are skipped silently (not an error); each genuine addition is
        audited individually via :meth:`add_user_to_team`. Returns the
        number of users actually added.
        """
        await self._require_manage(session, actor_user_id)
        team = await session.get(Team, team_id)
        if team is None:
            raise TeamNotFoundError(team_id)
        added = 0
        for user_id in user_ids:
            if await session.get(UserTeam, (user_id, team_id)) is not None:
                continue  # Already a member — skip rather than error.
            await self.add_user_to_team(
                session,
                actor_user_id=actor_user_id,
                user_id=user_id,
                team_id=team_id,
            )
            added += 1
        return added

    async def remove_user_from_team(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        user_id: int,
        team_id: int,
    ) -> None:
        """Remove a user from a team. Requires ``teams.manage``."""
        await self._require_manage(session, actor_user_id)
        team = await session.get(Team, team_id)
        if team is None:
            raise TeamNotFoundError(team_id)
        user = await session.get(User, user_id)
        if user is None:
            raise TeamNotFoundError(user_id)

        existing = await session.get(UserTeam, (user_id, team_id))
        if existing is None:
            raise NotInTeamError(user.username, team.name)

        await session.execute(
            delete(UserTeam).where(
                UserTeam.user_id == user_id,
                UserTeam.team_id == team_id,
            )
        )
        await session.flush()

        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action="users.team.unassign",
                resource_type="user",
                resource_id=str(user_id),
                outcome="success",
                payload={"team_id": team_id, "team_name": team.name},
            ),
        )

    async def set_user_teams(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        user_id: int,
        team_ids: Iterable[int],
    ) -> None:
        """Replace the user's team set with ``team_ids``. Requires ``teams.manage``."""
        await self._require_manage(session, actor_user_id)
        target_set = set(team_ids)

        current = {
            row.team_id
            for row in (
                await session.execute(select(UserTeam).where(UserTeam.user_id == user_id))
            ).scalars()
        }

        for team_id in current - target_set:
            await self.remove_user_from_team(
                session,
                actor_user_id=actor_user_id,
                user_id=user_id,
                team_id=team_id,
            )
        for team_id in target_set - current:
            await self.add_user_to_team(
                session,
                actor_user_id=actor_user_id,
                user_id=user_id,
                team_id=team_id,
            )

    # ---- internals ----

    async def _require_manage(self, session: AsyncSession, actor_user_id: int) -> None:
        if not await user_has_permission(session, actor_user_id, Permission.TEAMS_MANAGE):
            raise PermissionDeniedError(Permission.TEAMS_MANAGE.value)


def make_team_service(audit: AuditWriter) -> TeamService:
    """Factory: build a :class:`TeamService` from the audit writer."""
    return TeamService(audit=audit)
