"""Side-effect import of every MATOFX ORM module.

Importing this module registers every mapped table against the shared
:data:`matofx.db.metadata.metadata`. Any long-lived process that flushes
ORM objects must import it (the API gets the full set transitively via
its routers; the standalone runner does not).

Why this exists: SQLAlchemy resolves *all* foreign keys across the
metadata when it sorts tables for a flush. A process that imports only
the ORM modules it directly uses will blow up with
``NoReferencedTableError`` the first time a registered table carries a
foreign key to a table whose module was never imported — e.g. the
deployed runner crashed flushing ``approval_request`` because
``tool_vv_record`` (:mod:`matofx.vv.orm`) was absent from the metadata.
The runner previously patched these one at a time (``studies.orm``,
``workflows.orm``); this module replaces that whack-a-mole with the
complete set.

When adding a new ORM module, add its import here.
``tests/db/test_all_orm.py`` fails if a module defining
``__tablename__`` is missing from this list.
"""

# ruff: noqa: F401

from __future__ import annotations

import matofx.approvals.orm
import matofx.audit.orm
import matofx.compliance.orm
import matofx.favourites.orm
import matofx.hierarchy.orm
import matofx.kpi.orm
import matofx.library.orm
import matofx.licence.orm
import matofx.materials.orm
import matofx.migrate.orm
import matofx.models.orm
import matofx.studies.cohort.orm
import matofx.studies.optimisation.orm
import matofx.studies.orm
import matofx.summaries.orm
import matofx.tools.exec_orm
import matofx.tools.orm
import matofx.tutorials.orm
import matofx.users.orm
import matofx.users.role_orm
import matofx.users.team_orm
import matofx.vv.orm
import matofx.workflows.orm
