"""First-user bootstrap.

A fresh install needs *some* way to get an initial admin account into
the database. This module is what the ``matofx admin create-first-user``
CLI subcommand calls. It refuses to run when any user already exists,
so it can never accidentally clobber an existing install.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from matofx.audit import ensure_chain_head
from matofx.users.orm import User
from matofx.users.service import UserService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class BootstrapError(Exception):
    """Raised when first-user bootstrap can't proceed."""


async def create_first_user(
    session: AsyncSession,
    user_service: UserService,
    *,
    username: str,
    email: str,
    password: str,
    first_name: str,
    last_name: str,
) -> User:
    """Create the install's first super-user.

    Refuses to run if any user already exists — bootstrap is a one-shot
    operation, not a generic create-user. Always sets ``role="super_user"``.
    Ensures the audit chain head is initialised so the bootstrap event
    can be recorded.

    ``first_name`` and ``last_name`` are stored separately (matching the
    admin UI's create-user form) so the home-page greeting and other
    salutations have a real first name to render — without them, every
    bootstrapped install greets the operator by their username.
    """
    if await _any_user_exists(session):
        raise BootstrapError(
            "Cannot bootstrap: at least one user already exists. "
            "Sign in as that user and create more accounts via the admin UI."
        )

    await ensure_chain_head(session)

    return await user_service.create_user(
        session,
        username=username,
        email=email,
        password=password,
        first_name=first_name,
        last_name=last_name,
        role="super_user",
        actor="system:bootstrap",
    )


async def _any_user_exists(session: AsyncSession) -> bool:
    stmt = select(User.id).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None
