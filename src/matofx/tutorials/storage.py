"""Filesystem helpers for the tutorial uploads.

Every uploaded tutorial lands at::

    <tutorials_dir>/<id>_<sanitised-original-filename>

The id prefix gives us a guaranteed-unique on-disk name even when an
admin re-uploads a file with the same name, and the trailing
human-readable suffix means the staged files are still browsable if
an operator opens the directory.
"""

from __future__ import annotations

import re
from pathlib import Path

DOCUMENT_EXTENSIONS: frozenset[str] = frozenset({".pdf"})
"""File extensions accepted for ``media_type='document'`` uploads.

PDFs only for now — the home-page renderer pops them in a new tab
and the browser's built-in viewer handles the rest. Adding e.g.
``.epub`` would mean also picking a renderer."""

VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp4", ".webm", ".ogg", ".ogv", ".mov", ".m4v", ".mkv", ".avi"}
)
"""File extensions accepted for ``media_type='video'`` uploads.

Browser playback varies: ``.mp4`` and ``.webm`` play inline almost
everywhere; ``.mov`` / ``.m4v`` play on most modern browsers; ``.mkv``
and ``.avi`` typically prompt a download instead. We accept all of
them and let the browser pick a sensible default per its own
plumbing."""

_SANITISE = re.compile(r"[^A-Za-z0-9._-]+")
"""Anything outside the safe filename character set collapses to ``_``."""

_MAX_FILENAME_LENGTH = 200
"""Cap the on-disk filename length so deep paths don't blow the OS
limit. Most real uploads are well under this; the cap is a guard
rail not a UX feature."""


def tutorials_dir(root: Path | str) -> Path:
    """Return the absolute tutorials root, creating it on first use.

    ``root`` is whatever the config layer hands us
    (:attr:`matofx.config.TutorialsSettings.tutorials_dir`). The first
    upload after a clean install lazily ``mkdir -p``s the directory
    so operators don't need to pre-create it.
    """
    path = Path(root).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def sanitise_filename(name: str) -> str:
    """Coerce an arbitrary uploaded filename into a safe on-disk leaf.

    Replaces every character outside ``A-Za-z0-9._-`` with an
    underscore, collapses runs, and truncates to
    :data:`_MAX_FILENAME_LENGTH`. The original name is preserved on
    the DB row so the user-facing download still serves the file the
    admin uploaded.
    """
    stem = name.strip().replace(" ", "_")
    cleaned = _SANITISE.sub("_", stem).strip("._-") or "tutorial"
    return cleaned[:_MAX_FILENAME_LENGTH]


def is_allowed_extension(filename: str, media_type: str) -> bool:
    """Return ``True`` when the upload extension matches the chosen media type."""
    suffix = Path(filename).suffix.lower()
    if media_type == "document":
        return suffix in DOCUMENT_EXTENSIONS
    if media_type == "video":
        return suffix in VIDEO_EXTENSIONS
    return False


def storage_path_for(tutorials_root: Path, tutorial_id: int, original_filename: str) -> Path:
    """Compute the absolute on-disk path for a newly-uploaded tutorial.

    The id prefix guarantees uniqueness even if two tutorials carry
    the same uploaded filename; the sanitised suffix keeps the file
    human-recognisable for an operator browsing the directory.
    """
    return tutorials_root / f"{tutorial_id:07d}_{sanitise_filename(original_filename)}"
