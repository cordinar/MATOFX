"""MATOFX documentation system — single-source Markdown, in-app + standalone.

Authored once as Markdown under ``matofx/docs/<set>/`` (``public`` /
``user`` / ``admin``), this package discovers and parses those files into
a :class:`~matofx.docs.models.DocTree`. The in-app ``/help`` hub renders
the tree natively (no extra runtime dependency); the release pipeline
builds the same source into a standalone HTML site + per-set PDFs.

See ``decisions/0014-documentation-system.md`` for the decision and
``designs/documentation.md`` for the build-level spec.
"""

from matofx.docs.loader import DocError, get_doc_tree, load_docs
from matofx.docs.models import (
    SET_ADMIN,
    SET_PUBLIC,
    SET_TITLES,
    SET_USER,
    DocSet,
    DocTopic,
    DocTree,
)
from matofx.docs.search import search_topics, searchable_text

#: Human-facing documentation release label, shown in the in-app help
#: footer and the standalone build. Deliberately distinct from the
#: package ``__version__`` (a PEP 440 dev version) — this is the
#: marketing release name customers see on the documentation.
DOC_RELEASE = "2026 R2.0.0"

__all__ = [
    "DOC_RELEASE",
    "SET_ADMIN",
    "SET_PUBLIC",
    "SET_TITLES",
    "SET_USER",
    "DocError",
    "DocSet",
    "DocTopic",
    "DocTree",
    "get_doc_tree",
    "load_docs",
    "search_topics",
    "searchable_text",
]
