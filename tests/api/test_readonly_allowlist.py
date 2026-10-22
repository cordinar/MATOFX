"""The read-only allow-list is minimal, and stays minimal.

The middleware exempts routes by **path prefix**, which fails in a
direction nothing else catches: a new route added under an
already-exempt stem inherits the exemption silently, with no code-review
signal and no failing test. The list can only ever get more permissive by
accident.

That is not hypothetical. `/api/admin/compliance` and `/api/admin/licence`
were both exempt while having no mutating route at all — they pre-approved
whatever might land there later, justified by comments describing CLI-only
operations.

So this file pins the *effect* of the list rather than its contents: it
enumerates every route the running app actually exposes, computes which
ones the middleware would wave through, and compares that against an
explicit expected set. Adding a route under an exempt prefix fails here
with a diff naming exactly what it just gained.
"""

from __future__ import annotations

from typing import Protocol

import fastapi.routing
import pytest
from fastapi.routing import APIRoute

from matofx.api.app import make_app
from matofx.api.readonly import _ALLOWED_PREFIXES, _MUTATING_METHODS
from matofx.config import Settings

#: Every mutating route exempt from read-only mode, as (method, path).
#:
#: Each entry must be justifiable as "the site is unusable read-only
#: without it" or "an administrator cannot fix the cause without it".
#: If you are adding to this list, that is the bar.
EXPECTED_EXEMPT: frozenset[tuple[str, str]] = frozenset(
    {
        # Signing in and out. Every registered user may still sign in and
        # view results; blocking logout would strand them and, under CSL,
        # hold licence places open.
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/logout"),
        ("POST", "/api/auth/touch"),
        # Lifting an integrity-failure read-only — necessarily done while
        # the site is read-only.
        ("POST", "/api/admin/integrity/acknowledge"),
        # Freeing a licence place.
        ("POST", "/api/admin/sessions/{session_id}/revoke"),
        # Generating a compliance report. ADR-0019 §B9: report generation
        # never fails closed, and the two things that trigger read-only —
        # an expired licence and a failed integrity check — are precisely
        # the two a supplier will ask for a report about. Withholding it
        # here would destroy the evidence at the moment it is needed.
        ("POST", "/api/admin/compliance/reports"),
        # Personal view state, not governed data.
        ("POST", "/api/auth/me/theme"),
        ("POST", "/api/auth/me/preferences"),
        ("POST", "/api/auth/me/regional-settings"),
        ("POST", "/api/auth/me/ui-preferences"),
    }
)


class _Route(Protocol):
    path: str
    methods: set[str] | None


@pytest.fixture(scope="module")
def app_routes() -> list[_Route]:
    app = make_app(Settings(environment="dev"))
    # Newer FastAPI keeps each included router nested in `app.routes` rather
    # than flattening its routes in; its public helper walks them with their
    # full paths. Older FastAPI has no helper and a flat list.
    iter_route_contexts = getattr(fastapi.routing, "iter_route_contexts", None)
    if iter_route_contexts is None:
        routes: list[_Route] = [r for r in app.routes if isinstance(r, APIRoute)]
    else:
        routes = [
            c for c in iter_route_contexts(app.routes) if isinstance(c.original_route, APIRoute)
        ]
    # With no routes found, every check below would pass without checking anything.
    assert routes, "Found no API routes on the app."
    return routes


def _exempt(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _ALLOWED_PREFIXES)


def test_exempt_routes_are_exactly_the_expected_set(app_routes: list[_Route]) -> None:
    """A new route under an exempt prefix must not slip through unnoticed."""
    actual = {
        (method, route.path)
        for route in app_routes
        for method in sorted(set(route.methods or ()) & _MUTATING_METHODS)
        if _exempt(route.path)
    }

    newly_exempt = actual - EXPECTED_EXEMPT
    assert not newly_exempt, (
        "These mutating routes are exempt from read-only mode but not listed as "
        f"expected: {sorted(newly_exempt)}. A route added under an already-allow-listed "
        "prefix inherits the exemption silently. Either narrow the prefix in "
        "matofx.api.readonly, or add it here with a justification."
    )

    no_longer_exempt = EXPECTED_EXEMPT - actual
    assert not no_longer_exempt, (
        f"These routes were expected to keep working in read-only mode but no longer do: "
        f"{sorted(no_longer_exempt)}. If one was renamed, update both this list and the "
        "prefix — a stale exemption strands whoever needed it, while the site is already "
        "degraded."
    )


def test_no_allow_list_prefix_is_dead(app_routes: list[_Route]) -> None:
    """A prefix that exempts nothing is a trap for the next route added."""
    dead = [
        prefix
        for prefix in _ALLOWED_PREFIXES
        if not any(
            route.path.startswith(prefix) and set(route.methods or ()) & _MUTATING_METHODS
            for route in app_routes
        )
    ]
    assert not dead, (
        f"These read-only exemptions match no mutating route: {dead}. They protect "
        "nothing today and silently pre-approve whatever is added beneath them "
        "tomorrow. Remove them until there is a route that needs them."
    )


def test_no_mutating_work_hides_behind_a_get(app_routes: list[_Route]) -> None:
    """The middleware gates by HTTP method, so a state-changing GET would slip past.

    There should not be any. Nothing checked that before, which is
    exactly how there comes to be one.
    """
    # Matched as whole path segments: `/api/models/approvers` is a GET
    # that lists approvers, and substring matching flags it as "approve".
    verbs = {
        "create",
        "delete",
        "revoke",
        "submit",
        "approve",
        "reject",
        "cancel",
        "archive",
        "acknowledge",
    }
    suspicious = [
        route.path
        for route in app_routes
        if "GET" in (route.methods or ()) and verbs & set(route.path.strip("/").split("/"))
    ]
    assert not suspicious, (
        f"These GET routes look like they mutate state: {suspicious}. Read-only mode "
        "gates by method, so a mutating GET is never blocked. Make it a POST."
    )
