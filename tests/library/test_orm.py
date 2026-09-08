"""ORM + constraint tests for the file-library data model (slice 1).

Schema correctness — the closed-vocabulary CHECKs, the project-scope
CHECK, version ordinal uniqueness, tool-assignment uniqueness, and the
provenance composite PK. Migration ``0045`` round-trips on both engines
via ``tests/db/test_alembic.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.db import make_session_factory, session_scope
from matofx.library import (
    JobLibraryFileUse,
    LibraryFile,
    LibraryFileTool,
    LibraryFileVersion,
)

from .conftest import Basics


def _personal_file(basics: Basics, **overrides: object) -> LibraryFile:
    fields: dict[str, object] = {
        "name": "Baseline geometry",
        "description": "shared STEP",
        "scope": "personal",
        "owner_user_id": basics.user_id,
        "created_by_user_id": basics.user_id,
    }
    fields.update(overrides)
    return LibraryFile(**fields)


def _version(
    file_id: int, basics: Basics, *, ordinal: int = 1, **overrides: object
) -> LibraryFileVersion:
    fields: dict[str, object] = {
        "library_file_id": file_id,
        "ordinal": ordinal,
        "stored_path": f"/mnt/lib/{file_id}/{ordinal}/geom.step",
        "original_filename": "geom.step",
        "content_hash": "a" * 64,
        "size_bytes": 1024,
        "extension": "step",
        "provenance": "exported from CAD",
        "created_by_user_id": basics.user_id,
    }
    fields.update(overrides)
    return LibraryFileVersion(**fields)


@pytest.mark.asyncio
async def test_create_file_version_and_tool_assignment(
    library_engine: AsyncEngine, basics: Basics
) -> None:
    """A personal file with one immutable version + a tool assignment."""
    factory = make_session_factory(library_engine)
    async with session_scope(factory) as session:
        lf = _personal_file(basics, tags=["cad", "baseline"], file_kind="geometry")
        session.add(lf)
        await session.flush()
        session.add(_version(lf.id, basics))
        session.add(
            LibraryFileTool(library_file_id=lf.id, tool_alias="cad_upload", field_name="geometry")
        )
        await session.flush()
        file_id = lf.id

    async with session_scope(factory) as session:
        loaded = await session.get(LibraryFile, file_id)
        assert loaded is not None
        assert loaded.scope == "personal"
        assert loaded.state == "active"  # default
        assert loaded.version == 1  # optimistic-lock default
        assert loaded.tags == ["cad", "baseline"]
        versions = (
            (
                await session.execute(
                    select(LibraryFileVersion).where(LibraryFileVersion.library_file_id == file_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(versions) == 1
        assert versions[0].ordinal == 1
        assert versions[0].publish_state == "draft"  # default
        assert versions[0].content_hash == "a" * 64


@pytest.mark.asyncio
async def test_scope_check_rejects_unknown_value(
    library_engine: AsyncEngine, basics: Basics
) -> None:
    factory = make_session_factory(library_engine)
    with pytest.raises(IntegrityError):
        async with session_scope(factory) as session:
            session.add(_personal_file(basics, scope="department"))


@pytest.mark.asyncio
async def test_state_check_rejects_unknown_value(
    library_engine: AsyncEngine, basics: Basics
) -> None:
    factory = make_session_factory(library_engine)
    with pytest.raises(IntegrityError):
        async with session_scope(factory) as session:
            session.add(_personal_file(basics, state="deleted"))


@pytest.mark.asyncio
async def test_publish_state_check_rejects_unknown_value(
    library_engine: AsyncEngine, basics: Basics
) -> None:
    factory = make_session_factory(library_engine)
    async with session_scope(factory) as session:
        lf = _personal_file(basics)
        session.add(lf)
        await session.flush()
        file_id = lf.id
    with pytest.raises(IntegrityError):
        async with session_scope(factory) as session:
            session.add(_version(file_id, basics, publish_state="live"))


@pytest.mark.asyncio
async def test_project_scope_requires_project_id(
    library_engine: AsyncEngine, basics: Basics
) -> None:
    """project scope ⇔ project_id non-null (the portable iff CHECK)."""
    factory = make_session_factory(library_engine)
    # project scope without a project_id is rejected.
    with pytest.raises(IntegrityError):
        async with session_scope(factory) as session:
            session.add(_personal_file(basics, scope="project", project_id=None))


@pytest.mark.asyncio
async def test_personal_scope_forbids_project_id(
    library_engine: AsyncEngine, basics: Basics
) -> None:
    factory = make_session_factory(library_engine)
    # personal scope with a project_id set is rejected.
    with pytest.raises(IntegrityError):
        async with session_scope(factory) as session:
            session.add(_personal_file(basics, scope="personal", project_id=basics.project_id))


@pytest.mark.asyncio
async def test_project_scope_with_project_id_ok(
    library_engine: AsyncEngine, basics: Basics
) -> None:
    factory = make_session_factory(library_engine)
    async with session_scope(factory) as session:
        session.add(_personal_file(basics, scope="project", project_id=basics.project_id))
        await session.flush()  # no raise


@pytest.mark.asyncio
async def test_version_ordinal_unique_per_file(library_engine: AsyncEngine, basics: Basics) -> None:
    factory = make_session_factory(library_engine)
    async with session_scope(factory) as session:
        lf = _personal_file(basics)
        session.add(lf)
        await session.flush()
        file_id = lf.id
        session.add(_version(file_id, basics, ordinal=1))
        await session.flush()
    with pytest.raises(IntegrityError):
        async with session_scope(factory) as session:
            session.add(_version(file_id, basics, ordinal=1))  # duplicate ordinal


@pytest.mark.asyncio
async def test_tool_assignment_unique(library_engine: AsyncEngine, basics: Basics) -> None:
    factory = make_session_factory(library_engine)
    async with session_scope(factory) as session:
        lf = _personal_file(basics)
        session.add(lf)
        await session.flush()
        file_id = lf.id
        session.add(
            LibraryFileTool(library_file_id=file_id, tool_alias="cad_upload", field_name="geometry")
        )
        await session.flush()
    with pytest.raises(IntegrityError):
        async with session_scope(factory) as session:
            session.add(
                LibraryFileTool(
                    library_file_id=file_id, tool_alias="cad_upload", field_name="geometry"
                )
            )


@pytest.mark.asyncio
async def test_job_library_file_use_provenance(library_engine: AsyncEngine, basics: Basics) -> None:
    """Composite PK (job_id, field_name): one library version per field per job."""
    factory = make_session_factory(library_engine)
    async with session_scope(factory) as session:
        lf = _personal_file(basics)
        session.add(lf)
        await session.flush()
        v = _version(lf.id, basics)
        session.add(v)
        await session.flush()
        version_id = v.id
        session.add(
            JobLibraryFileUse(
                job_id=basics.job_id,
                field_name="geometry",
                library_file_version_id=version_id,
            )
        )
        await session.flush()

    with pytest.raises(IntegrityError):
        async with session_scope(factory) as session:
            session.add(
                JobLibraryFileUse(
                    job_id=basics.job_id,
                    field_name="geometry",  # duplicate (job_id, field_name)
                    library_file_version_id=version_id,
                )
            )


@pytest.mark.asyncio
async def test_retire_is_a_state_change_not_a_delete(
    library_engine: AsyncEngine, basics: Basics
) -> None:
    factory = make_session_factory(library_engine)
    async with session_scope(factory) as session:
        lf = _personal_file(basics)
        session.add(lf)
        await session.flush()
        file_id = lf.id
    async with session_scope(factory) as session:
        loaded = await session.get(LibraryFile, file_id)
        assert loaded is not None
        loaded.state = "retired"
    async with session_scope(factory) as session:
        reloaded = await session.get(LibraryFile, file_id)
        assert reloaded is not None  # row persists
        assert reloaded.state == "retired"
