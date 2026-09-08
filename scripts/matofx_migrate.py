#!/usr/bin/env python
"""Standalone Alembic runner for an installed MATOFX wheel.

Why this exists:
    `alembic.ini` in the source tree uses a relative `script_location`
    that only works when alembic is invoked from the project root. After
    `pip install matofx-<version>.whl`, the migrations directory lives
    inside the installed package — wherever site-packages happens to be.
    This script locates it programmatically and points alembic at it,
    so operators don't need a copy of `alembic.ini` on the target box.

Usage:
    python matofx_migrate.py upgrade head
    python matofx_migrate.py current
    python matofx_migrate.py history
    python matofx_migrate.py downgrade -1     # one step back
    python matofx_migrate.py stamp head       # mark head without running

Reads the database URL from the running MATOFX configuration. Set
`MATOFX_CONFIG` to point at your matofx.toml before invoking.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config


def _configure_logging() -> None:
    """Surface Alembic's ``Running upgrade ...`` progress lines.

    Alembic emits per-migration progress at INFO level on the ``alembic``
    logger. Python's default unconfigured-logger floor is WARNING, which
    is why `upgrade head` looks like it does nothing on a fresh DB. A
    minimal handler on stderr is enough to make it talk.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)-5.5s [%(name)s] %(message)s"))
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(handler)
    logging.getLogger("alembic").setLevel(logging.INFO)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def _resolve_migrations_dir() -> Path:
    import matofx

    pkg_dir = Path(matofx.__file__).resolve().parent
    candidate = pkg_dir / "db" / "migrations"
    if not candidate.is_dir():
        sys.stderr.write(
            f"Could not find migrations directory at {candidate}. "
            "Is the matofx wheel installed correctly?\n"
        )
        sys.exit(1)
    return candidate


def _build_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_resolve_migrations_dir()))
    cfg.set_main_option(
        "file_template",
        "%%(year)d%%(month).2d%%(day).2d_%%(rev)s_%%(slug)s",
    )
    cfg.set_main_option("timezone", "UTC")
    cfg.set_main_option("path_separator", "os")
    cfg.set_main_option("version_path_separator", "os")
    cfg.set_main_option("sourceless", "false")
    return cfg


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    _configure_logging()
    cfg = _build_config()
    op = argv[0]

    try:
        if op == "upgrade":
            target = argv[1] if len(argv) > 1 else "head"
            command.upgrade(cfg, target)
        elif op == "downgrade":
            target = argv[1] if len(argv) > 1 else "-1"
            command.downgrade(cfg, target)
        elif op == "current":
            command.current(cfg, verbose=True)
        elif op == "history":
            command.history(cfg, verbose=True)
        elif op == "heads":
            command.heads(cfg, verbose=True)
        elif op == "stamp":
            target = argv[1] if len(argv) > 1 else "head"
            command.stamp(cfg, target)
        elif op == "check":
            command.check(cfg)
        else:
            sys.stderr.write(f"Unknown command: {op}\n")
            print(__doc__)
            return 2
    except Exception as exc:
        sys.stderr.write(f"matofx_migrate: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
