"""Tutorials — uploadable PDF + video help material per tool, per workflow, or general.

Tutorials are admin-curated assets surfaced on the Home page so end
users can read / watch context without leaving the dashboard. Each
tutorial belongs to exactly one scope:

* **Tool** — ``tool_alias`` set, ``workflow_alias`` NULL.
* **Workflow** — ``workflow_alias`` set, ``tool_alias`` NULL.
* **General** — both NULL (e.g. *How to get started*).

Tool *and* workflow tutorials are capped at one active document + one
active video per scope; general tutorials have no cap (an install
might ship several complementary high-level guides). A DB-level CHECK
forbids both scope columns being set on the same row.

The :class:`Tutorial` ORM, :class:`TutorialService` (CRUD + activation
cascade), and :func:`tutorials_dir` (filesystem helper) live here.
The HTTP surface lives in :mod:`matofx.api.routers.tutorials`; the
admin UI page is :mod:`matofx.ui.pages.admin_tutorials`.
"""

from matofx.tutorials.orm import Tutorial
from matofx.tutorials.service import TutorialService, make_tutorial_service
from matofx.tutorials.storage import tutorials_dir

__all__ = [
    "Tutorial",
    "TutorialService",
    "make_tutorial_service",
    "tutorials_dir",
]
