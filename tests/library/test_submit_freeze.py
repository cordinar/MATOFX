"""Submit-time "Pick from library" promotion (ADR-0006 §6, slice 6).

``JobService._resolve_library_inputs`` turns a file field whose draft
value is a library reference (``{"source": "library", "version_id": N}``)
into a staged upload path — copying the chosen version's bytes into the
draft area so the existing promotion path lands them in the working dir —
and writes a ``job_library_file_use`` provenance row. Tested directly
(rather than through the full staging path) to keep the focus on the
resolve + provenance behaviour. Non-library file values pass through.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from matofx.audit import AuditWriter
from matofx.db import make_session_factory, session_scope
from matofx.hierarchy import make_job_service
from matofx.library import ToolAssignmentSpec, make_library_service
from matofx.library.orm import JobLibraryFileUse
from matofx.library.refs import build_library_ref
from matofx.tools.descriptor import InputField, ToolDescriptor
from matofx.tools.exec_orm import StorageLocation

from .conftest import Basics

_CONTENT = b"solid inlet geometry"
_FILENAME = "inlet.stp"


def _descriptor() -> ToolDescriptor:
    return ToolDescriptor(
        alias="demo",
        name="Demo",
        version="1.0",
        description="Takes a geometry file.",
        main_script="m.py",
        inputs=[InputField(name="geom", label="Geometry", type="file")],
    )


async def _published_library_file(factory: object, *, owner: int, mount: Path) -> int:
    """Seed an active StorageLocation + a published personal file; return version_id."""
    audit = AuditWriter(signing_key=None, key_id=None)
    library = make_library_service(audit)
    async with session_scope(factory) as session:  # type: ignore[arg-type]
        session.add(StorageLocation(name="primary", mount_path=str(mount)))
        await session.flush()
        lib = await library.create_file(
            session,
            actor_user_id=owner,
            name="Inlet",
            description="An inlet duct.",
            original_filename=_FILENAME,
            content=_CONTENT,
            provenance="CAD export",
            tool_assignments=[ToolAssignmentSpec(tool_alias="demo", field_name="geom")],
            scope="personal",
            approval_required=False,
        )
        versions = await library.list_versions(session, actor_user_id=owner, file_id=lib.id)
        return int(versions[0].id)


@pytest.mark.asyncio
async def test_resolve_promotes_bytes_and_writes_provenance(
    library_engine: AsyncEngine, basics: Basics, tmp_path: Path
) -> None:
    mount = tmp_path / "mount"
    mount.mkdir()
    factory = make_session_factory(library_engine)
    version_id = await _published_library_file(factory, owner=basics.user_id, mount=mount)
    jobs = make_job_service(AuditWriter(signing_key=None, key_id=None))

    async with session_scope(factory) as session:
        resolved = await jobs._resolve_library_inputs(
            session,
            basics.job_id,
            basics.user_id,
            _descriptor(),
            {"geom": {"source": "library", "version_id": version_id}},
            mount_path=str(mount),
        )
        # The library reference became a concrete staged path…
        staged = resolved["geom"]
        assert isinstance(staged, str)
        assert Path(staged).read_bytes() == _CONTENT
        assert _FILENAME in staged

    async with session_scope(factory) as session:
        use = await session.get(JobLibraryFileUse, (basics.job_id, "geom"))
        assert use is not None
        assert use.library_file_version_id == version_id


@pytest.mark.asyncio
async def test_resolve_accepts_string_sentinel(
    library_engine: AsyncEngine, basics: Basics, tmp_path: Path
) -> None:
    """The UI form sets the string sentinel (``matofx-library://<id>``)."""
    mount = tmp_path / "mount"
    mount.mkdir()
    factory = make_session_factory(library_engine)
    version_id = await _published_library_file(factory, owner=basics.user_id, mount=mount)
    jobs = make_job_service(AuditWriter(signing_key=None, key_id=None))

    async with session_scope(factory) as session:
        resolved = await jobs._resolve_library_inputs(
            session,
            basics.job_id,
            basics.user_id,
            _descriptor(),
            {"geom": build_library_ref(version_id)},
            mount_path=str(mount),
        )
        assert Path(resolved["geom"]).read_bytes() == _CONTENT

    async with session_scope(factory) as session:
        use = await session.get(JobLibraryFileUse, (basics.job_id, "geom"))
        assert use is not None
        assert use.library_file_version_id == version_id


@pytest.mark.asyncio
async def test_non_library_file_value_passes_through(
    library_engine: AsyncEngine, basics: Basics, tmp_path: Path
) -> None:
    """A typed path / drag-drop upload value is left untouched (no provenance row)."""
    mount = tmp_path / "mount"
    mount.mkdir()
    factory = make_session_factory(library_engine)
    await _published_library_file(factory, owner=basics.user_id, mount=mount)
    jobs = make_job_service(AuditWriter(signing_key=None, key_id=None))

    async with session_scope(factory) as session:
        resolved = await jobs._resolve_library_inputs(
            session,
            basics.job_id,
            basics.user_id,
            _descriptor(),
            {"geom": "C:/already/uploaded/inlet.stp"},
            mount_path=str(mount),
        )
        assert resolved["geom"] == "C:/already/uploaded/inlet.stp"  # unchanged

    async with session_scope(factory) as session:
        use = await session.get(JobLibraryFileUse, (basics.job_id, "geom"))
        assert use is None  # no library version was used
