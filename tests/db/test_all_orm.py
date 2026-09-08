"""Tests for :mod:`matofx.db.all_orm` — the complete ORM registry.

Guards against the NoReferencedTableError class of production bug: a
process (the standalone runner, historically) imports only part of the
model set, then the first flush touching a foreign key into an
unregistered table crashes every loop iteration. See the module
docstring of :mod:`matofx.db.all_orm`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import matofx

_SRC_ROOT = Path(matofx.__file__).parent
_TABLENAME_RE = re.compile(r"^\s*__tablename__\s*[:=]", re.MULTILINE)


def _modules_defining_tables() -> set[str]:
    """Module names under ``matofx`` whose source assigns ``__tablename__``."""
    found: set[str] = set()
    for py in _SRC_ROOT.rglob("*.py"):
        rel = py.relative_to(_SRC_ROOT)
        if rel.parts[0] == "db" and "migrations" in rel.parts:
            continue
        if _TABLENAME_RE.search(py.read_text(encoding="utf-8")):
            found.add("matofx." + ".".join(rel.with_suffix("").parts))
    return found


def test_all_orm_imports_every_table_defining_module() -> None:
    """Every module that defines a mapped table is listed in all_orm.

    Fails when a new ORM module is added without registering it, before
    that omission can reach a deployed runner.
    """
    source = (_SRC_ROOT / "db" / "all_orm.py").read_text(encoding="utf-8")
    imported = set(re.findall(r"^import (matofx\.[\w.]+)", source, re.MULTILINE))
    missing = _modules_defining_tables() - imported
    assert not missing, (
        f"ORM modules defining __tablename__ but not imported by "
        f"matofx.db.all_orm: {sorted(missing)}"
    )


def test_metadata_foreign_key_closure_resolves() -> None:
    """With all_orm imported, every FK target table is in the metadata."""
    import matofx.db.all_orm  # noqa: F401, PLC0415
    from matofx.db.metadata import metadata  # noqa: PLC0415

    for table in metadata.tables.values():
        for fk in table.foreign_keys:
            fk.column  # noqa: B018 — raises NoReferencedTableError if absent


def test_supervisor_registers_all_orm() -> None:
    """The supervisor module must import the full ORM registry.

    Losing this import re-opens the deployed ``NoReferencedTableError``
    crash: the standalone runner would again register only the tables
    its direct imports happen to pull in.
    """
    source = (_SRC_ROOT / "runner" / "supervisor.py").read_text(encoding="utf-8")
    assert "from matofx.db import all_orm" in source


def test_runner_process_import_graph_resolves_all_foreign_keys() -> None:
    """Reproduce the deployed runner crash in a fresh interpreter.

    The standalone runner imports config, then the supervisor, and
    registers the full model set via ``matofx.db.all_orm`` at startup.
    In a clean process (no API, no pytest imports) that sequence must
    not circular-import and must yield a metadata whose every foreign
    key resolves — this is exactly what failed in production with
    ``approval_request.tool_vv_record_id`` unable to find
    ``tool_vv_record``.
    """
    code = (
        "import matofx.config\n"
        "import matofx.runner.supervisor\n"
        "from matofx.db import all_orm\n"
        "from matofx.db.metadata import metadata\n"
        "for table in metadata.tables.values():\n"
        "    for fk in table.foreign_keys:\n"
        "        fk.column\n"
        "assert 'tool_vv_record' in metadata.tables\n"
        "assert 'approval_request' in metadata.tables\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
