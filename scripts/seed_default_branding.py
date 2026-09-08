#!/usr/bin/env python
"""Seed bundled default branding (header icon + favicon) into a fresh install.

Idempotent: only writes a DB row when the matching ``runtime_setting`` row is
null/missing, so re-running the installer over an admin's customised branding
is a no-op. The on-disk file at ``<icons_dir>/<key>.<ext>`` follows the same
naming convention as ``GeneralSettingsService.set_image``, so a later admin
upload via the UI overwrites the seeded file cleanly.

Reads ``MATOFX_CONFIG`` from the environment. Assumes the super-user created
in INSTALL.md step B11 has user id 1 (i.e. you ran
``matofx admin create-first-user`` before this script). Edit ``ACTOR_USER_ID``
below if your install diverges.

Source files come from ``<BUNDLE>/branding/``. Either or both may be omitted —
missing files are reported and skipped.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ACTOR_USER_ID = 1


# Source basename in the bundle's branding/ directory → catalog key.
# We accept multiple source extensions per key (PNG / SVG for header,
# ICO / PNG for favicon) and pick the first one we find. The extension
# is preserved on the destination so the served mime type is correct.
SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("header_icon_path", ("header_icon.png", "header_icon.svg")),
    ("favicon_path", ("favicon.ico", "favicon.png")),
)


# Mime sniffing keyed off source filename suffix. Kept tiny and explicit
# rather than pulling in the stdlib's `mimetypes` to avoid surprising
# OS-registry-driven differences between Windows and Linux installers.
MIME_BY_SUFFIX: dict[str, str] = {
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def _bundle_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _find_source(branding_dir: Path, candidates: tuple[str, ...]) -> Path | None:
    for name in candidates:
        candidate = branding_dir / name
        if candidate.is_file():
            return candidate
    return None


async def main() -> int:
    from matofx.audit import AuditWriter, ensure_chain_head
    from matofx.config import load_settings
    from matofx.config.general_service import (
        load_general_setting_overrides,
        make_general_settings_service,
    )
    from matofx.db import make_engine_scope, make_session_factory, session_scope

    settings = load_settings()  # raises ConfigError with actionable message if mis-set

    branding_dir = _bundle_root() / "branding"
    if not branding_dir.is_dir():
        print(
            f"No branding/ directory in bundle ({branding_dir}); nothing to seed.",
        )
        return 0

    # No-op AuditWriter: install-time seeding doesn't need a signed audit
    # entry, and threading the real writer through here would couple the
    # installer to the audit-key bootstrap order. The set_image call still
    # writes one record via session.add — which is exactly what we want.
    audit = AuditWriter(signing_key=None, key_id=None)
    service = make_general_settings_service(audit)

    seeded: list[str] = []
    skipped: list[str] = []

    async with make_engine_scope(settings.db) as engine:
        factory = make_session_factory(engine)
        async with session_scope(factory) as session:
            await ensure_chain_head(session)

            # Read existing overrides up-front so we can decide per key
            # without round-tripping the DB once per file.
            overrides = await load_general_setting_overrides(session)

            for key, candidates in SOURCES:
                if overrides.get(key):
                    skipped.append(
                        f"{key}: already set to {overrides[key]!r} (admin upload preserved)"
                    )
                    continue

                source = _find_source(branding_dir, candidates)
                if source is None:
                    skipped.append(
                        f"{key}: no source file in {branding_dir} "
                        f"(looked for: {', '.join(candidates)})"
                    )
                    continue

                mime = MIME_BY_SUFFIX.get(source.suffix.lower())
                if mime is None:
                    skipped.append(
                        f"{key}: unsupported source suffix {source.suffix!r} on {source.name}"
                    )
                    continue

                data = source.read_bytes()
                # set_image performs the catalog mime-allowlist check, the
                # per-key size cap, and the on-disk write to
                # <icons_dir>/<key>.<ext> — exactly what an admin upload
                # via the UI does, so seeded defaults and admin re-uploads
                # share the same storage convention.
                basename = await service.set_image(
                    session,
                    actor_user_id=ACTOR_USER_ID,
                    actor_role="super_user",
                    key=key,
                    filename=source.name,
                    data=data,
                    mime_type=mime,
                    branding_dir=settings.branding.icons_dir,
                    max_bytes=(
                        settings.branding.favicon_max_upload_bytes
                        if key == "favicon_path"
                        else settings.branding.header_icon_max_upload_bytes
                    ),
                )
                seeded.append(f"{key}: seeded {source.name} -> {basename}")

    for line in seeded:
        print(line)
    for line in skipped:
        print(line)
    if not seeded and not skipped:
        print("Branding seed: nothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
