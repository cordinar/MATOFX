"""Root test configuration.

Registers :mod:`tests._migrated_db` as a plugin so its session-scoped
``migrated_template`` fixture is available to every suite without each
conftest importing it (an import plus a same-named parameter is a
redefinition, which ruff rightly flags).
"""

from __future__ import annotations

pytest_plugins = ["tests._migrated_db"]
