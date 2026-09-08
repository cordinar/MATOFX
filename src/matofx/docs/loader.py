"""Discovery + front-matter parsing for the documentation tree.

Walks the ``public`` / ``user`` / ``admin`` content directories that
ship alongside this module, parses each Markdown file's YAML
front-matter, and builds a :class:`~matofx.docs.models.DocTree`. The
content is read-only and ships with the package, so the default tree is
cached for the process lifetime via :func:`get_doc_tree`.

Front-matter contract (see ``designs/documentation.md`` §2)::

    ---
    title: Submitting a job
    audience: user            # must match the directory the file is in
    capability: null          # null, or e.g. "approver"
    slug: jobs/submitting     # unique across the whole tree
    summary: How to launch a tool and fill in its form.
    ---
    <markdown body…>

A malformed or inconsistent file raises :class:`DocError` at load time
rather than degrading silently — a broken shipped doc is a build defect
we want surfaced, not hidden.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from matofx.docs.models import (
    SET_ADMIN,
    SET_PUBLIC,
    SET_TITLES,
    SET_USER,
    DocSet,
    DocTopic,
    DocTree,
)

#: Sets discovered, in canonical order. Each maps to a sibling directory
#: of this module (``matofx/docs/<name>/*.md``).
_SET_ORDER: tuple[str, ...] = (SET_PUBLIC, SET_USER, SET_ADMIN)

_FILENAME_ORDER_RE = re.compile(r"^(\d+)")
_DEFAULT_ORDER = 9999

#: ``text.split("---", 2)`` on a well-formed file yields exactly three
#: segments: the empty string before the opening fence, the YAML block,
#: and the Markdown body.
_FRONT_MATTER_SEGMENTS = 3


class DocError(Exception):
    """Raised when a documentation file is missing or malformed."""


def _content_root() -> Path:
    """Return the directory holding the ``public`` / ``user`` / ``admin`` trees."""
    return Path(__file__).resolve().parent


def _filename_order(filename: str) -> int:
    """Extract the leading numeric prefix (``10-foo.md`` → ``10``)."""
    match = _FILENAME_ORDER_RE.match(filename)
    return int(match.group(1)) if match else _DEFAULT_ORDER


def _split_front_matter(text: str, *, path: Path) -> tuple[dict[str, Any], str]:
    """Split a ``---``-delimited YAML front-matter block from the body.

    Returns ``(front_matter_dict, body)``. Raises :class:`DocError` when
    the block is missing or doesn't parse to a mapping.
    """
    if not text.startswith("---"):
        raise DocError(f"{path}: missing '---' front-matter block at the top of the file.")
    parts = text.split("---", 2)
    if len(parts) < _FRONT_MATTER_SEGMENTS:
        raise DocError(f"{path}: front-matter block is not closed with a second '---'.")
    try:
        loaded = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        raise DocError(f"{path}: front-matter is not valid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise DocError(f"{path}: front-matter must be a mapping of key: value.")
    return loaded, parts[2].lstrip("\n")


def _require_str(front_matter: dict[str, Any], key: str, *, path: Path) -> str:
    """Return a required non-empty string field from the front-matter."""
    value = front_matter.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DocError(f"{path}: front-matter is missing a non-empty '{key}'.")
    return value.strip()


def _parse_topic(path: Path, set_name: str) -> DocTopic:
    """Parse one Markdown file into a :class:`DocTopic`."""
    front_matter, body = _split_front_matter(path.read_text(encoding="utf-8"), path=path)

    title = _require_str(front_matter, "title", path=path)
    slug = _require_str(front_matter, "slug", path=path)
    audience = _require_str(front_matter, "audience", path=path)
    if audience != set_name:
        raise DocError(
            f"{path}: audience '{audience}' doesn't match its directory '{set_name}'."
        )

    capability_raw = front_matter.get("capability")
    capability = (
        capability_raw.strip()
        if isinstance(capability_raw, str) and capability_raw.strip()
        else None
    )
    summary_raw = front_matter.get("summary")
    summary = summary_raw.strip() if isinstance(summary_raw, str) else ""

    return DocTopic(
        set_name=set_name,
        slug=slug,
        title=title,
        audience=audience,
        capability=capability,
        summary=summary,
        order=_filename_order(path.name),
        filename=path.name,
        body=body,
    )


def load_docs(root: Path | None = None) -> DocTree:
    """Discover and parse the documentation tree under ``root``.

    Args:
        root: Directory containing the ``public`` / ``user`` / ``admin``
            sub-directories. Defaults to the content shipped alongside
            this module. Tests pass a temporary tree.

    Returns:
        A :class:`DocTree` with each set's topics sorted by filename
        numeric prefix then filename.

    Raises:
        DocError: A file is malformed, or a slug is duplicated across the
            tree.
    """
    base = root if root is not None else _content_root()
    sets: list[DocSet] = []
    seen_slugs: dict[str, Path] = {}

    for set_name in _SET_ORDER:
        set_dir = base / set_name
        topics: list[DocTopic] = []
        if set_dir.is_dir():
            for path in sorted(set_dir.glob("*.md")):
                topic = _parse_topic(path, set_name)
                if topic.slug in seen_slugs:
                    raise DocError(
                        f"{path}: slug '{topic.slug}' is already used by "
                        f"{seen_slugs[topic.slug]}."
                    )
                seen_slugs[topic.slug] = path
                topics.append(topic)
            topics.sort(key=lambda t: (t.order, t.filename))
        sets.append(DocSet(name=set_name, title=SET_TITLES[set_name], topics=tuple(topics)))

    return DocTree(sets=tuple(sets))


@lru_cache(maxsize=1)
def get_doc_tree() -> DocTree:
    """Return the shipped documentation tree, built once per process."""
    return load_docs()
