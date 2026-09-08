#!/usr/bin/env python
"""Install the three example tools + a local StorageLocation against the live install.

Idempotent-ish: re-running raises on duplicate StorageLocation/tool names; for
a clean re-seed, drop the rows or recreate the DB.

Reads MATOFX_CONFIG from the environment. Assumes the super-user created in
B11 has user id 1 (i.e. you ran `matofx admin create-first-user` immediately
after migrating). Edit ACTOR_USER_ID below if not.

Locations:
    - StorageLocation `local` -> jobs root configured below (defaults match
      the INSTALL.md production locations).
    - Tools installed from <BUNDLE>/example_tools/<alias>/ at state=released.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ACTOR_USER_ID = 1


def _bundle_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_jobs_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "matofx" / "jobs"
    return Path("/srv/matofx/jobs")


async def main() -> int:
    from matofx.audit import AuditWriter, ensure_chain_head
    from matofx.config import load_settings
    from matofx.db import make_engine_scope, make_session_factory, session_scope
    from matofx.tools.exec_orm import StorageLocation
    from matofx.tools.loader import install_from_path
    from matofx.tools.service import make_tool_service

    load_settings()  # raises ConfigError with actionable message if mis-set

    bundle = _bundle_root()
    example_tools = bundle / "example_tools"
    if not example_tools.is_dir():
        sys.stderr.write(f"example_tools/ not found under {bundle}\n")
        return 1

    jobs_dir = _default_jobs_dir()
    if not jobs_dir.is_dir():
        sys.stderr.write(
            f"Jobs directory {jobs_dir} does not exist. Create it per INSTALL.md B3.\n"
        )
        return 1

    from matofx.config import load_settings as _ls

    settings = _ls()
    audit = AuditWriter(signing_key=None, key_id=None)

    async with make_engine_scope(settings.db) as engine:
        factory = make_session_factory(engine)
        async with session_scope(factory) as session:
            await ensure_chain_head(session)
            session.add(StorageLocation(name="local", mount_path=str(jobs_dir)))
            await session.flush()
            ts = make_tool_service(audit)
            for alias in (
                "material_properties",
                "colour_check",
                "heat_transfer",
                "cad_upload",
            ):
                tool = await install_from_path(
                    session,
                    ts,
                    actor_user_id=ACTOR_USER_ID,
                    bundle_dir=example_tools / alias,
                )
                await ts.promote_state(
                    session, actor_user_id=ACTOR_USER_ID, alias=tool.alias, new_state="test"
                )
                await ts.promote_state(
                    session, actor_user_id=ACTOR_USER_ID, alias=tool.alias, new_state="released"
                )
                print(f"installed + released: {tool.alias}")
            print(f"storage_location: local -> {jobs_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
