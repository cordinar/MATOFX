"""Thin async client wrapping the matofx HTTP API.

The UI runs in the same process as the API; the client is just an
``httpx.AsyncClient`` pointed at the local app's ASGI transport so
auth, validation, and access control behave exactly the same way an
external caller would see — no shortcut paths, no second auth model.

Every call surfaces a friendly :class:`ApiError` instead of leaking
``httpx.HTTPStatusError``; the message is the API's ``detail`` body
(which the routers already keep plain-English for end users).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from typing import Any

import httpx
from httpx import ASGITransport
from starlette.applications import Starlette


class ApiError(RuntimeError):
    """Raised when an API call returned a non-2xx status.

    Carries the HTTP status code, the user-facing ``detail`` message,
    and (when the response body was JSON) the full payload — callers
    that need structured error data (e.g. the in-browser editor's
    syntax-error list at 422) read it off ``payload``.
    """

    def __init__(
        self,
        status_code: int,
        detail: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Construct from the API response."""
        super().__init__(detail or f"API call failed (HTTP {status_code})")
        self.status_code = status_code
        self.detail = detail
        self.payload = payload or {}


def _parsed_body(response: httpx.Response) -> tuple[str, dict[str, Any] | None]:
    """Return ``(detail, payload)`` from a response, gracefully handling non-JSON."""
    try:
        body = response.json()
    except ValueError:
        return (response.text or "", None)
    if isinstance(body, dict):
        detail = str(body.get("detail")) if isinstance(body.get("detail"), str) else str(body)
        return (detail, body)
    return (str(body), None)


_HTTP_FIRST_ERROR_STATUS = 400
_HTTP_UNAUTHORIZED = 401
_HTTP_NOT_FOUND = 404


#: Callback invoked when an *authenticated* call comes back 401 — i.e.
#: the bearer token the UI still holds has been revoked or has aged past
#: the idle / hard session timeout server-side. The dashboard chrome
#: registers :func:`matofx.ui.pages._chrome._handle_expired_session`
#: here at import time so any page, on any 401, tears down local state
#: and lands the user on the sign-in page instead of showing a wall of
#: "Your session has expired" errors on a page they can no longer use.
#:
#: Only calls that actually carried an ``Authorization`` header fire it:
#: the login form's own 401 (wrong password) must not trigger a bounce.
_UNAUTHENTICATED_HOOK: Callable[[ApiError], None] | None = None


def set_unauthenticated_hook(hook: Callable[[ApiError], None] | None) -> None:
    """Register (or clear) the expired-session callback. Process-wide."""
    global _UNAUTHENTICATED_HOOK  # noqa: PLW0603 — single process-wide UI hook.
    _UNAUTHENTICATED_HOOK = hook


def _was_authenticated(response: httpx.Response) -> bool:
    """``True`` when the request behind ``response`` carried a bearer token."""
    try:
        request = response.request
    except RuntimeError:  # pragma: no cover - hand-built response in a test
        return False
    return bool(request.headers.get("authorization"))


def _notify_unauthenticated(response: httpx.Response, error: ApiError) -> None:
    """Fire the expired-session hook for an authenticated 401, if registered."""
    if response.status_code != _HTTP_UNAUTHORIZED:
        return
    hook = _UNAUTHENTICATED_HOOK
    if hook is None or not _was_authenticated(response):
        return
    # The hook is UI chrome (navigation + storage). It must never mask
    # the ApiError the caller is about to see, so swallow anything it
    # raises — e.g. being called outside a NiceGUI client context.
    with contextlib.suppress(Exception):
        hook(error)


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < _HTTP_FIRST_ERROR_STATUS:
        return
    detail, payload = _parsed_body(response)
    error = ApiError(response.status_code, detail, payload)
    _notify_unauthenticated(response, error)
    raise error


def _guide_image_mime(filename: str) -> str:
    """Best-effort MIME for a guide-image upload part, from its extension."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
    }.get(ext, "application/octet-stream")


def _encode_kpi_range(entry: tuple[str, float | None, float | None]) -> str:
    """Encode a ``(name, min, max)`` KPI filter as the ``name:min:max`` param."""
    name, vmin, vmax = entry
    return f"{name}:{'' if vmin is None else vmin}:{'' if vmax is None else vmax}"


def _build_results_export_url(
    alias: str,
    fmt: str,
    *,
    version_id: int | None = None,
    completed_from: str | None = None,
    completed_to: str | None = None,
    job_ids: list[int] | None = None,
    kpi_ranges: list[tuple[str, float | None, float | None]] | None = None,
    vv_status: str | None = None,
) -> str:
    """Compose the URL for ``/api/tools/{alias}/results/export.{fmt}``.

    Shared by :meth:`ApiClient.download_tool_results_csv` and the PDF twin
    so both surfaces produce identical query-string encoding. This builds
    the *fetch* URL (used server-side with the bearer header) — never hand
    it to the browser as an ``<a href>``; the endpoint is bearer-only.
    """
    from urllib.parse import urlencode  # noqa: PLC0415

    params: list[tuple[str, str]] = []
    if version_id is not None:
        params.append(("version_id", str(version_id)))
    if completed_from is not None:
        params.append(("completed_from", completed_from))
    if completed_to is not None:
        params.append(("completed_to", completed_to))
    if job_ids is not None:
        params.append(("job_ids", ",".join(str(i) for i in job_ids)))
    for entry in kpi_ranges or ():
        params.append(("kpi_range", _encode_kpi_range(entry)))
    if vv_status and vv_status != "any":
        params.append(("vv_status", vv_status))
    base = f"/api/tools/{alias}/results/export.{fmt}"
    return base if not params else f"{base}?{urlencode(params)}"


class ApiClient:
    """In-process HTTP client for the matofx API.

    Constructed once per UI page interaction (cheap — the underlying
    transport is the same one fastapi tests use). Holds the bearer
    token so callers don't have to thread it through every method.
    """

    def __init__(self, app: Starlette, *, token: str | None = None) -> None:
        """Construct from the live FastAPI app + an optional bearer token."""
        self._app = app
        self._token = token

    def _headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        return {"Authorization": f"Bearer {self._token}"}

    async def _client(self) -> httpx.AsyncClient:
        transport = ASGITransport(app=self._app)
        # In-process ASGI (no real network), but some endpoints do heavy
        # compute — surrogate / field fits (POD + GP over many runs). httpx's
        # 5s default timeout cancels those mid-fit, which surfaces as a
        # spurious client-side error (not an ApiError), with no server
        # traceback and a rolled-back transaction (so nothing is persisted).
        # Use a generous ceiling instead of the default.
        return httpx.AsyncClient(
            transport=transport,
            base_url="http://matofx",
            timeout=httpx.Timeout(600.0),
        )

    # ----- auth ----------------------------------------------------------

    async def login(self, *, username: str, password: str) -> dict[str, Any]:
        """POST /api/auth/login → token + ``session_idle_timeout_minutes`` (drives idle-logout)."""
        async with await self._client() as c:
            response = await c.post(
                "/api/auth/login",
                json={"username": username, "password": password},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def logout(self) -> None:
        """POST /api/auth/logout — revokes the bearer token."""
        async with await self._client() as c:
            response = await c.post("/api/auth/logout", headers=self._headers())
            _raise_for_status(response)

    async def get_site_mode(self) -> dict[str, Any]:
        """GET /api/site/mode → read-only state + banner (any signed-in user)."""
        async with await self._client() as c:
            response = await c.get("/api/site/mode", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def acknowledge_integrity_failure(self, note: str) -> dict[str, Any]:
        """POST /api/admin/integrity/acknowledge — Super User lifts read-only."""
        async with await self._client() as c:
            response = await c.post(
                "/api/admin/integrity/acknowledge",
                headers=self._headers(),
                json={"note": note},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def get_licence_capacity(self) -> dict[str, Any]:
        """GET /api/admin/licence/capacity → floating-assignment capacity."""
        async with await self._client() as c:
            response = await c.get("/api/admin/licence/capacity", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def list_active_sessions(self) -> dict[str, Any]:
        """GET /api/admin/sessions → live sessions + licence capacity."""
        async with await self._client() as c:
            response = await c.get("/api/admin/sessions", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def revoke_active_session(self, session_id: int) -> None:
        """POST /api/admin/sessions/{id}/revoke — sign a user out, freeing a place."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/sessions/{session_id}/revoke", headers=self._headers()
            )
            _raise_for_status(response)

    async def list_compliance_reports(self) -> dict[str, Any]:
        """GET /api/admin/compliance/reports → archived reports + install identity."""
        async with await self._client() as c:
            response = await c.get("/api/admin/compliance/reports", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def generate_compliance_report(self, *, since: str | None = None) -> dict[str, Any]:
        """POST /api/admin/compliance/reports → generate, sign and archive one.

        Slow by nature: the report self-verifies the whole audit chain,
        which on a long-lived install is among the heaviest things an
        admin page asks for. The client's 600s ceiling covers it.
        """
        async with await self._client() as c:
            response = await c.post(
                "/api/admin/compliance/reports",
                json={"since": since},
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    async def fetch_compliance_report(self, counter: int, *, fmt: str = "json") -> bytes:
        """GET a stored report's bytes, for handing to the browser.

        Fetched here rather than linked directly because the download
        endpoint needs the bearer header, which a plain anchor cannot
        carry (it would get a 401) — the same reason the results and audit
        exports do it this way.
        """
        async with await self._client() as c:
            response = await c.get(
                f"/api/admin/compliance/reports/{counter}/document",
                params={"fmt": fmt},
                headers=self._headers(),
            )
            _raise_for_status(response)
            return bytes(response.content)

    async def touch(self) -> None:
        """POST /api/auth/touch — refresh the session's idle timer."""
        async with await self._client() as c:
            response = await c.post("/api/auth/touch", headers=self._headers())
            _raise_for_status(response)

    async def me(self) -> dict[str, Any]:
        """GET /api/auth/me → the authenticated user's identity."""
        async with await self._client() as c:
            response = await c.get("/api/auth/me", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def update_my_preferences(self, *, email_notifications_enabled: bool) -> dict[str, Any]:
        """POST /api/auth/me/preferences → refreshed /me payload."""
        async with await self._client() as c:
            response = await c.post(
                "/api/auth/me/preferences",
                headers=self._headers(),
                json={"email_notifications_enabled": email_notifications_enabled},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def set_theme_preference(self, *, theme: str) -> dict[str, Any]:
        """POST /api/auth/me/theme → refreshed /me payload."""
        async with await self._client() as c:
            response = await c.post(
                "/api/auth/me/theme",
                headers=self._headers(),
                json={"theme": theme},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def set_summary_enrichment_enabled(self, *, enabled: bool) -> dict[str, Any]:
        """POST /api/auth/me/summary-preferences → refreshed /me payload."""
        async with await self._client() as c:
            response = await c.post(
                "/api/auth/me/summary-preferences",
                headers=self._headers(),
                json={"summary_enrichment_enabled": enabled},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def set_regional_preferences(self, *, locale: str, timezone: str) -> dict[str, Any]:
        """POST /api/auth/me/regional-settings → refreshed /me payload."""
        async with await self._client() as c:
            response = await c.post(
                "/api/auth/me/regional-settings",
                headers=self._headers(),
                json={"locale": locale, "timezone": timezone},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def set_ui_preferences(
        self,
        *,
        sidebar_pinned: bool | None = None,
        sidebar_width: int | None = None,
        sidebar_groups_collapsed: dict[str, bool] | None = None,
        quick_access_filter: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/auth/me/ui-preferences → refreshed /me payload.

        Sends only the fields that aren't ``None`` so the server merges
        the one preference that changed (home-redesign §9) without
        clobbering the rest. Passing no fields raises a 422 server-side
        (an empty write is a caller bug), so always supply at least one.
        """
        payload: dict[str, Any] = {}
        if sidebar_pinned is not None:
            payload["sidebar_pinned"] = sidebar_pinned
        if sidebar_width is not None:
            payload["sidebar_width"] = sidebar_width
        if sidebar_groups_collapsed is not None:
            payload["sidebar_groups_collapsed"] = sidebar_groups_collapsed
        if quick_access_filter is not None:
            payload["quick_access_filter"] = quick_access_filter
        async with await self._client() as c:
            response = await c.post(
                "/api/auth/me/ui-preferences",
                headers=self._headers(),
                json=payload,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def change_my_password(self, *, current_password: str, new_password: str) -> None:
        """POST /api/auth/me/change-password — verifies current, sets new."""
        async with await self._client() as c:
            response = await c.post(
                "/api/auth/me/change-password",
                headers=self._headers(),
                json={
                    "current_password": current_password,
                    "new_password": new_password,
                },
            )
            _raise_for_status(response)

    # ----- tools ---------------------------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        """GET /api/tools → list of installed tools the user can see."""
        async with await self._client() as c:
            response = await c.get("/api/tools", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def get_tool(self, alias: str) -> dict[str, Any]:
        """GET /api/tools/{alias} → full descriptor for form rendering."""
        async with await self._client() as c:
            response = await c.get(f"/api/tools/{alias}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def list_tool_version_options(self, alias: str) -> list[dict[str, Any]]:
        """GET /api/tools/{alias}/versions → [{tool_version_id, version, is_current}]."""
        async with await self._client() as c:
            response = await c.get(f"/api/tools/{alias}/versions", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def list_tool_presets(self, alias: str) -> list[dict[str, Any]]:
        """GET /api/tools/{alias}/presets → preset bundle for the workspace popover."""
        async with await self._client() as c:
            response = await c.get(f"/api/tools/{alias}/presets", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    # ----- KPI extraction (ADR-0012) ----------------------------------

    async def list_tool_kpis(self, alias: str, *, version_id: int | None = None) -> dict[str, Any]:
        """GET /api/tools/{alias}/kpis → catalog wrapper + ``entries``.

        Backs the warehouse-page header. ``version_id`` narrows to one
        tool_version's KPI set; absent → union of every released
        version (the same column-set the page renders by default).
        """
        params = {"version_id": version_id} if version_id is not None else None
        async with await self._client() as c:
            response = await c.get(
                f"/api/tools/{alias}/kpis",
                headers=self._headers(),
                params=params,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def list_tool_results(
        self,
        alias: str,
        *,
        version_id: int | None = None,
        completed_from: str | None = None,
        completed_to: str | None = None,
        job_ids: list[int] | None = None,
        kpi_ranges: list[tuple[str, float | None, float | None]] | None = None,
        vv_status: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """GET /api/tools/{alias}/results → paginated warehouse rows + counts.

        The warehouse-page table consumer. Filters are sent as query
        params; absent means "no constraint on that dimension"
        (mirrors the API's ``WarehouseFilters`` semantics). ``kpi_ranges``
        are ``(kpi_name, min, max)`` value filters; ``vv_status`` narrows to
        verified / verified-&-validated tool versions.

        ``job_ids`` is the unbounded-selection allow-list — pass the
        ticked set to restrict the page to just those rows + their
        ACL-allowed intersection.
        """
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if version_id is not None:
            params["version_id"] = version_id
        if completed_from is not None:
            params["completed_from"] = completed_from
        if completed_to is not None:
            params["completed_to"] = completed_to
        if job_ids is not None:
            params["job_ids"] = ",".join(str(i) for i in job_ids)
        if kpi_ranges:
            params["kpi_range"] = [_encode_kpi_range(e) for e in kpi_ranges]
        if vv_status and vv_status != "any":
            params["vv_status"] = vv_status
        async with await self._client() as c:
            response = await c.get(
                f"/api/tools/{alias}/results",
                headers=self._headers(),
                params=params,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def tool_results_timeseries(
        self,
        alias: str,
        *,
        kpi_name: str,
        version_id: int | None = None,
        completed_from: str | None = None,
        completed_to: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/tools/{alias}/results/timeseries → trend-chart points (slice 7)."""
        params: dict[str, Any] = {"kpi_name": kpi_name}
        if version_id is not None:
            params["version_id"] = version_id
        if completed_from is not None:
            params["completed_from"] = completed_from
        if completed_to is not None:
            params["completed_to"] = completed_to
        async with await self._client() as c:
            response = await c.get(
                f"/api/tools/{alias}/results/timeseries",
                headers=self._headers(),
                params=params,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def download_tool_results_csv(
        self,
        alias: str,
        *,
        version_id: int | None = None,
        completed_from: str | None = None,
        completed_to: str | None = None,
        job_ids: list[int] | None = None,
        kpi_ranges: list[tuple[str, float | None, float | None]] | None = None,
        vv_status: str | None = None,
    ) -> bytes:
        """Fetch the Results-warehouse CSV export **with the bearer header**.

        The browser can't GET ``/api/tools/.../results/export.csv`` directly —
        the endpoint is bearer-only and a browser-initiated ``<a href>`` can't
        carry the header (it gets a 401). The page fetches the bytes here and
        hands them to ``ui.download.content`` instead. Filters (incl.
        ``kpi_ranges`` / ``vv_status``) match the table so the export reflects
        the filtered view; empty ones are omitted.
        """
        url = _build_results_export_url(
            alias,
            "csv",
            version_id=version_id,
            completed_from=completed_from,
            completed_to=completed_to,
            job_ids=job_ids,
            kpi_ranges=kpi_ranges,
            vv_status=vv_status,
        )
        async with await self._client() as c:
            response = await c.get(url, headers=self._headers())
            _raise_for_status(response)
            return response.content

    async def download_tool_results_pdf(
        self,
        alias: str,
        *,
        version_id: int | None = None,
        completed_from: str | None = None,
        completed_to: str | None = None,
        job_ids: list[int] | None = None,
        kpi_ranges: list[tuple[str, float | None, float | None]] | None = None,
        vv_status: str | None = None,
    ) -> bytes:
        """Fetch the Results-warehouse PDF export **with the bearer header**.

        The endpoint returns a self-contained **printable HTML** document (a
        print-on-load script; the operator picks "Save as PDF" in the browser
        print dialog when they open it). Bearer-only, so — like the CSV twin —
        the page fetches the bytes here rather than linking the endpoint. Same
        filters as the table.
        """
        url = _build_results_export_url(
            alias,
            "pdf",
            version_id=version_id,
            completed_from=completed_from,
            completed_to=completed_to,
            job_ids=job_ids,
            kpi_ranges=kpi_ranges,
            vv_status=vv_status,
        )
        async with await self._client() as c:
            response = await c.get(url, headers=self._headers())
            _raise_for_status(response)
            return response.content

    async def search_jobs_by_inputs(
        self,
        alias: str,
        *,
        filters: dict[str, Any] | None = None,
        numeric_tolerance_pct: float = 25.0,
    ) -> list[dict[str, Any]]:
        """POST /api/tools/{alias}/job-search → completed jobs matching ``filters``.

        Backs the ``/job-search`` page. ``filters`` is a flat
        ``{descriptor_field: value}`` map; the server splits it into
        equality vs numeric-range buckets using the tool's descriptor.
        Empty / ``None`` / empty-string filter values are treated as
        "no constraint on this field".
        """
        async with await self._client() as c:
            response = await c.post(
                f"/api/tools/{alias}/job-search",
                headers=self._headers(),
                json={
                    "filters": filters or {},
                    "numeric_tolerance_pct": numeric_tolerance_pct,
                },
            )
            _raise_for_status(response)
            return list(response.json())

    async def find_similar_jobs(
        self,
        alias: str,
        *,
        reference_inputs: dict[str, Any] | None = None,
        reference_job_id: int | None = None,
        top_n: int = 25,
        exclude_job_id: int | None = None,
    ) -> dict[str, Any]:
        """POST /api/tools/{alias}/job-search/similar → ranked similar runs.

        Backs the *Similar* mode on ``/job-search``. The reference point
        is either an existing run (``reference_job_id``), a typed
        ``{field: value}`` map (``reference_inputs``), or both (typed
        values overlay the seed run). Returns the full response dict:
        ``{"weighting", "weights", "weighting_source", "results"}`` where
        each result row carries ``distance`` + ``closeness_pct`` on top of
        the usual visible-job fields.
        """
        async with await self._client() as c:
            response = await c.post(
                f"/api/tools/{alias}/job-search/similar",
                headers=self._headers(),
                json={
                    "reference_inputs": reference_inputs or {},
                    "reference_job_id": reference_job_id,
                    "top_n": top_n,
                    "exclude_job_id": exclude_job_id,
                },
            )
            _raise_for_status(response)
            return dict(response.json())

    async def list_distinct_input_values(self, alias: str, *, field: str) -> list[Any]:
        """GET /api/tools/{alias}/distinct-input-values?field=… → unique value set.

        Powers the file-input dropdown on ``/job-search``: the page
        asks the server for every distinct value previously submitted
        for one descriptor field across the user's visible runs of
        the tool, so the user picks from history rather than re-typing.
        """
        async with await self._client() as c:
            response = await c.get(
                f"/api/tools/{alias}/distinct-input-values",
                headers=self._headers(),
                params={"field": field},
            )
            _raise_for_status(response)
            return list(response.json().get("values", []))

    async def list_file_input_history(self, alias: str, *, field: str) -> list[dict[str, Any]]:
        """GET /api/tools/{alias}/file-input-history?field=… → enriched history.

        Returns one entry per distinct previously-used path with
        ``value`` + ``job_id`` + ``project_name`` + ``scenario_name``
        keys — used by the workspace's file-input picker to render
        ``"<filename>  ·  Job 0001234 (Project, Scenario)"`` rather
        than a bare path. The Job Search page keeps using the cheaper
        :meth:`list_distinct_input_values` since it only needs the
        value list.
        """
        async with await self._client() as c:
            response = await c.get(
                f"/api/tools/{alias}/file-input-history",
                headers=self._headers(),
                params={"field": field},
            )
            _raise_for_status(response)
            return list(response.json().get("entries", []))

    async def list_file_output_candidates(
        self,
        *,
        accepted_file_types: list[str] | None = None,
        expected_field_array: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /api/tools/file-output-candidates → cross-tool file outputs.

        Returns one entry per file *output* of every visible completed job
        (any tool), each with ``value`` (absolute path) + ``filename`` +
        ``file_type`` + ``field_array`` + ``output_name`` / ``output_label`` +
        ``tool_alias`` / ``tool_name`` + ``job_id`` + ``project_name`` +
        ``scenario_name``. ``accepted_file_types`` (the consuming input's
        allowed extensions, e.g. ``[".vtu"]``) narrows by file type;
        ``expected_field_array`` (the nodal array the input needs, e.g.
        ``"temperature"``) narrows field outputs to that exact array so an
        incompatible field is never offered. Used by the file-input widget's
        "use a file from another job" mode (cross-tool), alongside the
        same-tool :meth:`list_file_input_history`.
        """
        params: dict[str, Any] = {}
        if accepted_file_types:
            params["accepted_file_types"] = list(accepted_file_types)
        if expected_field_array:
            params["expected_field_array"] = expected_field_array
        async with await self._client() as c:
            response = await c.get(
                "/api/tools/file-output-candidates",
                headers=self._headers(),
                params=params,
            )
            _raise_for_status(response)
            return list(response.json().get("entries", []))

    async def create_or_get_draft_job(
        self,
        scenario_id: int,
        *,
        tool_alias: str,
        name: str = "Untitled run",
        inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST /api/scenarios/{sid}/draft-jobs → creates or returns existing draft.

        Idempotent — repeated calls for the same (scenario, tool)
        combination return the existing job without producing
        duplicates.

        ``inputs`` (optional) seeds the new draft's ``Job.inputs``
        dict — used by the ``/job-search`` "Use this run as a starting
        point" flow. Idempotency is preserved: when a draft already
        exists for the pair the seed inputs are silently dropped and
        the existing draft is returned unchanged.
        """
        payload: dict[str, Any] = {"tool_alias": tool_alias, "name": name}
        if inputs:
            payload["inputs"] = inputs
        async with await self._client() as c:
            response = await c.post(
                f"/api/scenarios/{scenario_id}/draft-jobs",
                headers=self._headers(),
                json=payload,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def fetch_output_cad_preview(self, job_id: int, *, filename: str) -> bytes:
        """GET /api/jobs/{id}/outputs/{filename}/cad-preview → STL bytes.

        Output-side sibling of :meth:`fetch_draft_cad_preview` —
        clients viewing job results don't have absolute filesystem
        paths, so this route accepts an outputs-relative filename
        and resolves it server-side. Same conversion semantics:
        STL passes through, STP/STEP/IGES/OBJ tessellate via
        cadquery / OCP / trimesh.
        """
        from urllib.parse import quote  # noqa: PLC0415

        async with await self._client() as c:
            response = await c.get(
                f"/api/jobs/{job_id}/outputs/{quote(filename, safe='')}/cad-preview",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return bytes(response.content)

    async def fetch_draft_cad_preview(self, job_id: int, *, path: str) -> bytes:
        """GET /api/jobs/{id}/cad-preview?path=… → STL bytes.

        Server-side conversion of any supported CAD source (STP / STEP
        / IGES / OBJ / STL) into binary STL. The bytes are consumed
        directly by NiceGUI's ``ui.scene.stl`` element via the same
        base64 data-URL plumbing used for native STL inputs, so all
        five CAD formats render through one client path.
        """
        async with await self._client() as c:
            response = await c.get(
                f"/api/jobs/{job_id}/cad-preview",
                headers=self._headers(),
                params={"path": path},
            )
            _raise_for_status(response)
            return bytes(response.content)

    async def fetch_draft_file_preview(self, job_id: int, *, path: str) -> bytes:
        """GET /api/jobs/{id}/file-preview?path=… → raw file bytes.

        Used by the rich file-input widget to drive its preview pane —
        the path comes either from a fresh upload's response or from
        the "use file from a previous job" picker. The server checks
        that the path resolves under the active storage mount and
        below a ``drafts/`` or ``jobs/`` segment, so this isn't a
        general-purpose file-read endpoint.
        """
        async with await self._client() as c:
            response = await c.get(
                f"/api/jobs/{job_id}/file-preview",
                headers=self._headers(),
                params={"path": path},
            )
            _raise_for_status(response)
            return bytes(response.content)

    async def upload_draft_file_input(
        self,
        job_id: int,
        *,
        field_name: str,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """POST /api/jobs/{id}/uploads/{field_name} → stage a file upload.

        Multipart upload that lands the bytes on the shared mount under
        the draft job's staging area and rewrites
        ``Job.inputs[field_name]`` to the resulting absolute path. The
        returned :class:`JobResponse` carries the updated ``inputs``
        dict so the form can re-render against it.
        """
        async with await self._client() as c:
            response = await c.post(
                f"/api/jobs/{job_id}/uploads/{field_name}",
                headers=self._headers(),
                files={"file": (filename, content, content_type)},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def clear_draft_file_input(self, job_id: int, *, field_name: str) -> dict[str, Any]:
        """DELETE /api/jobs/{id}/uploads/{field_name} → remove the staged file.

        Wipes the bytes from the draft staging area AND clears the
        matching ``Job.inputs`` entry so the workspace form re-renders
        in its no-file state (re-enabling the previous-job picker).
        """
        async with await self._client() as c:
            response = await c.delete(
                f"/api/jobs/{job_id}/uploads/{field_name}",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    async def save_draft_inputs(
        self,
        job_id: int,
        *,
        inputs: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/jobs/{id}/draft-update → persist WIP form values.

        Backs the workspace's "Save Inputs" button — the row's
        ``inputs`` (and optionally ``name``) are updated in place; the
        job stays in ``created`` state. The user can re-open the draft
        later and the form repopulates from the saved values.
        """
        payload: dict[str, Any] = {}
        if inputs is not None:
            payload["inputs"] = inputs
        if name is not None:
            payload["name"] = name
        async with await self._client() as c:
            response = await c.post(
                f"/api/jobs/{job_id}/draft-update",
                headers=self._headers(),
                json=payload,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def queue_draft_job(
        self,
        job_id: int,
        *,
        inputs: dict[str, Any],
        name: str | None = None,
        priority: bool = False,
        justification: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/jobs/{id}/queue → promote a draft to queued.

        ``priority=True`` requests skip-queue treatment for admins /
        super-users; the API enforces the ``jobs.skip_queue`` permission
        and returns 403 if the caller doesn't hold it. ``justification``
        is the optional note shown to the approver when the tool's
        policy holds the job (§16.15); ignored for non-approval tools.
        """
        payload: dict[str, Any] = {"inputs": inputs, "priority": priority}
        if name is not None:
            payload["name"] = name
        if justification is not None:
            payload["justification"] = justification
        async with await self._client() as c:
            response = await c.post(
                f"/api/jobs/{job_id}/queue",
                headers=self._headers(),
                json=payload,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def list_my_visible_jobs(
        self,
        *,
        q: str | None = None,
        tool_alias: str | None = None,
        state: str | None = None,
        mine: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """GET /api/users/me/visible-jobs → cross-project ACL-filtered job list.

        Powers the My Jobs workspace's global Job-ID search picker
        (call with ``q``), the empty-state recent-activity panel (no
        filters), and the Compare Select tab (``mine`` + ``tool_alias``).

        ``mine=True`` restricts the result to jobs the caller submitted.
        ``mine=False`` (default) returns every job in projects the
        caller can see, regardless of who submitted it.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if q is not None and q.strip():
            params["q"] = q.strip()
        if tool_alias is not None:
            params["tool_alias"] = tool_alias
        if state is not None:
            params["state"] = state
        if mine:
            params["mine"] = True
        async with await self._client() as c:
            response = await c.get(
                "/api/users/me/visible-jobs",
                headers=self._headers(),
                params=params,
            )
            _raise_for_status(response)
            return list(response.json())

    # ----- projects + scenarios -----------------------------------------

    async def list_projects(self) -> list[dict[str, Any]]:
        """GET /api/projects → projects visible to the actor."""
        async with await self._client() as c:
            response = await c.get("/api/projects", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def create_project(self, *, name: str, description: str = "") -> dict[str, Any]:
        """POST /api/projects → newly created project."""
        async with await self._client() as c:
            response = await c.post(
                "/api/projects",
                headers=self._headers(),
                json={"name": name, "description": description},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def get_project(self, project_id: int) -> dict[str, Any]:
        """GET /api/projects/{id} → one project."""
        async with await self._client() as c:
            response = await c.get(f"/api/projects/{project_id}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def list_scenarios(self, project_id: int) -> list[dict[str, Any]]:
        """GET /api/projects/{pid}/scenarios → scenarios in the project."""
        async with await self._client() as c:
            response = await c.get(f"/api/projects/{project_id}/scenarios", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def create_scenario(
        self, project_id: int, *, name: str, description: str = ""
    ) -> dict[str, Any]:
        """POST /api/projects/{pid}/scenarios → newly created scenario."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/projects/{project_id}/scenarios",
                headers=self._headers(),
                json={"name": name, "description": description},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def get_scenario(self, scenario_id: int) -> dict[str, Any]:
        """GET /api/scenarios/{sid} → one scenario."""
        async with await self._client() as c:
            response = await c.get(f"/api/scenarios/{scenario_id}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def list_projects_ex(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        """GET /api/projects with the archived filter."""
        async with await self._client() as c:
            response = await c.get(
                "/api/projects",
                headers=self._headers(),
                params={"include_archived": str(include_archived).lower()},
            )
            _raise_for_status(response)
            return list(response.json())

    async def update_project(
        self,
        project_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """PATCH /api/projects/{id} → edited project (Creator/Owner)."""
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if expected_version is not None:
            body["expected_version"] = expected_version
        async with await self._client() as c:
            response = await c.patch(
                f"/api/projects/{project_id}", headers=self._headers(), json=body
            )
            _raise_for_status(response)
            return dict(response.json())

    async def archive_project(self, project_id: int, *, state: str = "archived") -> dict[str, Any]:
        """POST /api/projects/{id}/archive → archived/cancelled project."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/projects/{project_id}/archive",
                headers=self._headers(),
                json={"state": state},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def transfer_project(self, project_id: int, *, new_owner_user_id: int) -> dict[str, Any]:
        """POST /api/projects/{id}/transfer → project after the hand-off."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/projects/{project_id}/transfer",
                headers=self._headers(),
                json={"new_owner_user_id": new_owner_user_id},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def list_user_directory(self) -> list[dict[str, Any]]:
        """GET /api/users/directory → [{user_id, username, full_name}]."""
        async with await self._client() as c:
            response = await c.get("/api/users/directory", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def get_project_acl(self, project_id: int) -> dict[str, Any]:
        """GET /api/projects/{id}/acl → {creator_user_id, entries}."""
        async with await self._client() as c:
            response = await c.get(f"/api/projects/{project_id}/acl", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def upsert_project_acl(self, project_id: int, *, user_id: int, role: str) -> None:
        """POST /api/projects/{id}/acl → add or change one grant."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/projects/{project_id}/acl",
                headers=self._headers(),
                json={"user_id": user_id, "role": role},
            )
            _raise_for_status(response)

    async def replace_project_acl(self, project_id: int, *, grants: list[dict[str, Any]]) -> None:
        """PUT /api/projects/{id}/acl → batch-replace the whole ACL."""
        async with await self._client() as c:
            response = await c.put(
                f"/api/projects/{project_id}/acl",
                headers=self._headers(),
                json={"grants": grants},
            )
            _raise_for_status(response)

    async def remove_project_acl(self, project_id: int, user_id: int) -> None:
        """DELETE /api/projects/{id}/acl/{uid} → revoke a grant."""
        async with await self._client() as c:
            response = await c.delete(
                f"/api/projects/{project_id}/acl/{user_id}", headers=self._headers()
            )
            _raise_for_status(response)

    async def get_project_metadata(self, project_id: int) -> list[dict[str, Any]]:
        """GET /api/projects/{id}/metadata → current selections."""
        async with await self._client() as c:
            response = await c.get(f"/api/projects/{project_id}/metadata", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def set_project_metadata(
        self, project_id: int, *, selections: list[dict[str, Any]]
    ) -> None:
        """PUT /api/projects/{id}/metadata → replace selections (OWNER)."""
        async with await self._client() as c:
            response = await c.put(
                f"/api/projects/{project_id}/metadata",
                headers=self._headers(),
                json={"selections": selections},
            )
            _raise_for_status(response)

    # ----- project metadata taxonomy -----------------------------------

    async def list_metadata_fields(self) -> list[dict[str, Any]]:
        """GET /api/metadata/fields → active fields + active values."""
        async with await self._client() as c:
            response = await c.get("/api/metadata/fields", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def admin_list_metadata_fields(self) -> list[dict[str, Any]]:
        """GET /api/admin/metadata/fields → every field + value (admin)."""
        async with await self._client() as c:
            response = await c.get("/api/admin/metadata/fields", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def create_metadata_field(
        self,
        *,
        key: str,
        label: str,
        field_type: str,
        required: bool = False,
        display_order: int = 0,
        show_in_table: bool = False,
        number_format: str | None = None,
        format_mask: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/admin/metadata/fields → created field."""
        async with await self._client() as c:
            response = await c.post(
                "/api/admin/metadata/fields",
                headers=self._headers(),
                json={
                    "key": key,
                    "label": label,
                    "field_type": field_type,
                    "required": required,
                    "display_order": display_order,
                    "show_in_table": show_in_table,
                    "number_format": number_format,
                    "format_mask": format_mask,
                },
            )
            _raise_for_status(response)
            return dict(response.json())

    async def update_metadata_field(self, field_id: int, **changes: Any) -> dict[str, Any]:
        """PATCH /api/admin/metadata/fields/{fid} → updated field."""
        async with await self._client() as c:
            response = await c.patch(
                f"/api/admin/metadata/fields/{field_id}",
                headers=self._headers(),
                json=changes,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def create_metadata_value(
        self, field_id: int, *, value: str, display_order: int = 0
    ) -> dict[str, Any]:
        """POST /api/admin/metadata/fields/{fid}/values → created value."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/metadata/fields/{field_id}/values",
                headers=self._headers(),
                json={"value": value, "display_order": display_order},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def update_metadata_value(self, value_id: int, **changes: Any) -> dict[str, Any]:
        """PATCH /api/admin/metadata/values/{vid} → updated value."""
        async with await self._client() as c:
            response = await c.patch(
                f"/api/admin/metadata/values/{value_id}",
                headers=self._headers(),
                json=changes,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def reorder_metadata_values(self, field_id: int, *, value_ids: list[int]) -> None:
        """PATCH /api/admin/metadata/fields/{fid}/values/order → set value order."""
        async with await self._client() as c:
            response = await c.patch(
                f"/api/admin/metadata/fields/{field_id}/values/order",
                headers=self._headers(),
                json={"value_ids": value_ids},
            )
            _raise_for_status(response)

    # ----- jobs ----------------------------------------------------------

    async def list_jobs(self, scenario_id: int) -> list[dict[str, Any]]:
        """GET /api/scenarios/{sid}/jobs → jobs in the scenario."""
        async with await self._client() as c:
            response = await c.get(f"/api/scenarios/{scenario_id}/jobs", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def submit_job(
        self,
        scenario_id: int,
        *,
        name: str,
        tool_alias: str,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """POST /api/scenarios/{sid}/jobs → newly created + queued job."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/scenarios/{scenario_id}/jobs",
                headers=self._headers(),
                json={"name": name, "tool_alias": tool_alias, "inputs": inputs},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def get_job(self, job_id: int) -> dict[str, Any]:
        """GET /api/jobs/{id} → current job status."""
        async with await self._client() as c:
            response = await c.get(f"/api/jobs/{job_id}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def get_summary(self, kind: str, surface_id: int) -> dict[str, Any]:
        """GET /api/summaries/{kind}/{id} → auto-generated results summary."""
        async with await self._client() as c:
            response = await c.get(f"/api/summaries/{kind}/{surface_id}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def get_job_logs(
        self,
        job_id: int,
        *,
        since: dict[str, int] | None = None,
        tail: bool = False,
    ) -> dict[str, Any]:
        """GET /api/jobs/{id}/logs → captured stdout / stderr.

        With no ``since`` this reads each log whole — what a finished
        job's page wants. Pass the ``*_offset`` values from the previous
        response (keyed ``stdout`` / ``stderr`` / ``launcher``) and only
        the bytes written since come back, which is what a live job's
        page polls for. ``tail=True`` additionally ends each chunk on a
        line boundary; see the endpoint for why that is not the default.
        """
        params: dict[str, Any] = {}
        for stream, offset in (since or {}).items():
            params[f"since_{stream}"] = int(offset)
        if tail:
            params["tail"] = "true"
        async with await self._client() as c:
            response = await c.get(
                f"/api/jobs/{job_id}/logs", headers=self._headers(), params=params or None
            )
            _raise_for_status(response)
            return dict(response.json())

    async def list_job_outputs(self, job_id: int) -> list[str]:
        """GET /api/jobs/{id}/outputs → filenames in the job's outputs directory."""
        async with await self._client() as c:
            response = await c.get(f"/api/jobs/{job_id}/outputs", headers=self._headers())
            _raise_for_status(response)
            return list(response.json().get("filenames", []))

    async def download_job_report(self, job_id: int, *, include_provenance: bool = False) -> bytes:
        """GET /api/jobs/{id}/report → bytes of the self-contained HTML report (ADR-0004).

        Server returns 404 for jobs not in the ``complete`` state, so
        the caller should gate the button on the row's ``state``
        rather than relying on the error path.

        ``include_provenance=True`` adds the lineage / provenance
        graph section to the report (ADR-0010 slice 6). Off by
        default to keep file sizes predictable.
        """
        params: dict[str, str] = {}
        if include_provenance:
            params["include_provenance"] = "true"
        async with await self._client() as c:
            response = await c.get(
                f"/api/jobs/{job_id}/report",
                headers=self._headers(),
                params=params,
            )
            _raise_for_status(response)
            return bytes(response.content)

    async def download_outputs_zip(self, job_id: int) -> bytes:
        """GET /api/jobs/{id}/outputs.zip → bytes of a ZIP of every output file."""
        async with await self._client() as c:
            response = await c.get(f"/api/jobs/{job_id}/outputs.zip", headers=self._headers())
            _raise_for_status(response)
            return bytes(response.content)

    async def download_all_files_zip(self, job_id: int) -> bytes:
        """GET /api/jobs/{id}/all-files.zip → bytes of a ZIP of the whole working dir."""
        async with await self._client() as c:
            response = await c.get(f"/api/jobs/{job_id}/all-files.zip", headers=self._headers())
            _raise_for_status(response)
            return bytes(response.content)

    async def get_job_output_document(self, job_id: int) -> dict[str, Any]:
        """GET /api/jobs/{id}/output-document → parsed outputs.json.

        Returns the full :class:`~matofx.tools.exec.contracts.OutputsDocument`
        shape (``schema_version``, ``tool_alias``, ``outputs``). 404 means
        the job hasn't reached a terminal state yet — the Compare workspace
        treats that as "skip this row, the user picked something not ready
        to compare". A 200 with an empty ``outputs`` list means the job
        finished but the runner didn't write a parseable document.
        """
        async with await self._client() as c:
            response = await c.get(f"/api/jobs/{job_id}/output-document", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def download_job_output(self, job_id: int, filename: str) -> bytes:
        """GET /api/jobs/{id}/outputs/{filename} → raw file bytes.

        Used by the job page to embed HTML and image outputs inline,
        because browser-initiated iframe / img requests can't carry the
        bearer header that the API requires. The page handler fetches
        bytes here (with bearer auth) and inlines them into the page.
        """
        from urllib.parse import quote  # noqa: PLC0415

        async with await self._client() as c:
            response = await c.get(
                f"/api/jobs/{job_id}/outputs/{quote(filename, safe='')}",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return bytes(response.content)

    async def get_job_field_frames(self, job_id: int, output: str) -> dict[str, Any]:
        """GET /api/jobs/{id}/field-frames?output= → JobFieldFramesResponse.

        One run's field output parsed server-side: the shared mesh + per-frame
        node values the Job page renders as a coloured ``Mesh3d`` (with a time
        slider when there's more than one frame).
        """
        from urllib.parse import quote  # noqa: PLC0415

        async with await self._client() as c:
            response = await c.get(
                f"/api/jobs/{job_id}/field-frames?output={quote(output, safe='')}",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    async def cancel_job(self, job_id: int) -> dict[str, Any]:
        """POST /api/jobs/{id}/cancel → request cancellation; runner picks it up."""
        async with await self._client() as c:
            response = await c.post(f"/api/jobs/{job_id}/cancel", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    # ----- admin: live jobs ----------------------------------------------

    async def live_jobs_list(
        self,
        *,
        kinds: list[str] | None = None,
        submitter_ids: list[int] | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/admin/live-jobs → the in-flight rows + filter options."""
        params: dict[str, Any] = {}
        if kinds:
            params["kind"] = list(kinds)
        if submitter_ids:
            params["submitter_id"] = [str(s) for s in submitter_ids]
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        async with await self._client() as c:
            response = await c.get("/api/admin/live-jobs", headers=self._headers(), params=params)
            _raise_for_status(response)
            return dict(response.json())

    async def live_jobs_cancel(
        self,
        *,
        job_ids: list[int] | None = None,
        run_ids: list[int] | None = None,
        study_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """POST /api/admin/live-jobs/cancel → bulk cancel; per-item outcomes back."""
        body = {
            "job_ids": list(job_ids or []),
            "run_ids": list(run_ids or []),
            "study_ids": list(study_ids or []),
        }
        async with await self._client() as c:
            response = await c.post(
                "/api/admin/live-jobs/cancel", headers=self._headers(), json=body
            )
            _raise_for_status(response)
            return dict(response.json())

    # ----- lineage (ADR-0010) --------------------------------------------

    async def lineage_graph(
        self,
        *,
        kind: str,
        obj_id: int,
        upstream: int = 6,
        downstream: int = 3,
    ) -> dict[str, Any]:
        """GET /api/lineage/{kind}/{obj_id} → the assembled graph.

        ``upstream`` / ``downstream`` clamp to ``0..20`` server-side
        (an out-of-range value surfaces as :class:`ApiError` with
        status 422). The response shape matches
        :class:`matofx.api.schemas.LineageGraphResponse`.
        """
        async with await self._client() as c:
            response = await c.get(
                f"/api/lineage/{kind}/{obj_id}",
                headers=self._headers(),
                params={"upstream": upstream, "downstream": downstream},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def lineage_picker(
        self,
        *,
        kind: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """GET /api/lineage/picker/{kind} → list of ``{id, label, state}``.

        Drives the admin lineage page's centre selector — each entry
        becomes one option in the ID dropdown. Reserved-for-later
        kinds return ``[]`` (the admin page surfaces a "no options
        available" note in that case).
        """
        async with await self._client() as c:
            response = await c.get(
                f"/api/lineage/picker/{kind}",
                headers=self._headers(),
                params={"limit": limit},
            )
            _raise_for_status(response)
            return list(response.json())

    async def workflow_graph_preview(
        self,
        *,
        tool_aliases: list[str],
    ) -> dict[str, Any]:
        """POST /api/workflows/graph/preview → ephemeral structure graph.

        Returns the same shape as :meth:`workflow_graph` but with
        ``workflow_id=None`` and placeholder header values — the
        workflow doesn't exist yet, this is a projection over the
        user's current tool picks in the editor. Raises
        :class:`ApiError` 400 on unknown / hidden / duplicate aliases.
        """
        async with await self._client() as c:
            response = await c.post(
                "/api/workflows/graph/preview",
                headers=self._headers(),
                json={"tool_aliases": list(tool_aliases)},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def workflow_graph(
        self,
        *,
        workflow_id: int,
        run_id: int | None = None,
    ) -> dict[str, Any]:
        """GET /api/workflows/{id}/graph → the descriptor-inferred graph.

        Structure-only when ``run_id`` is ``None``; per-node runtime
        state overlay when set. Response shape matches
        :class:`matofx.api.schemas.WorkflowGraphResponse`. Raises
        :class:`ApiError` on 404 (workflow not visible to the caller,
        or supplied ``run_id`` mismatches the workflow).
        """
        params: dict[str, Any] = {}
        if run_id is not None:
            params["run_id"] = int(run_id)
        async with await self._client() as c:
            response = await c.get(
                f"/api/workflows/{int(workflow_id)}/graph",
                headers=self._headers(),
                params=params,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def lineage_export(
        self,
        *,
        kind: str,
        obj_id: int,
        export_format: str,
        upstream: int = 6,
        downstream: int = 3,
    ) -> tuple[bytes, str]:
        """GET /api/lineage/{kind}/{obj_id}/export → (bytes, filename).

        ``export_format`` is ``"svg"`` (image), ``"json"``, or
        ``"graphml"``. Returns the
        bytes + the filename the server's ``Content-Disposition``
        header advertised — the caller passes both to
        :func:`nicegui.ui.download.content` to push the file to the
        browser. The browser-tab approach (``ui.navigate.to(...,
        new_tab=True)``) doesn't carry the bearer token, so this
        authenticated-fetch + push-to-browser path is the supported
        way to download authenticated bytes.
        """
        async with await self._client() as c:
            response = await c.get(
                f"/api/lineage/{kind}/{obj_id}/export",
                headers=self._headers(),
                params={
                    "format": export_format,
                    "upstream": upstream,
                    "downstream": downstream,
                },
            )
            _raise_for_status(response)
            filename = (
                _extract_filename(response.headers.get("content-disposition") or "")
                or f"lineage-{kind}-{obj_id}.{export_format}"
            )
            return response.content, filename

    # ----- admin: users + roles ------------------------------------------

    async def list_users(self) -> list[dict[str, Any]]:
        """GET /api/admin/users → every user row."""
        async with await self._client() as c:
            response = await c.get("/api/admin/users", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def create_user(
        self,
        *,
        username: str,
        email: str,
        password: str,
        first_name: str = "",
        last_name: str = "",
        full_name: str = "",
        role: str,
        is_approver: bool = False,
        team_ids: list[int] | None = None,
        capability_roles: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /api/admin/users → newly created user.

        ``full_name`` is auto-derived from first + last when blank.
        ``team_ids`` (when non-empty) requires the caller to also hold
        ``teams.manage``. ``capability_roles`` (when non-empty) requires
        ``roles.manage`` and stacks each capability role on the new
        account in the same request (ADR-0005).
        """
        async with await self._client() as c:
            response = await c.post(
                "/api/admin/users",
                headers=self._headers(),
                json={
                    "username": username,
                    "email": email,
                    "first_name": first_name,
                    "last_name": last_name,
                    "full_name": full_name,
                    "password": password,
                    "role": role,
                    "is_approver": is_approver,
                    "team_ids": list(team_ids or []),
                    "capability_roles": list(capability_roles or []),
                },
            )
            _raise_for_status(response)
            return dict(response.json())

    async def get_user(self, user_id: int) -> dict[str, Any]:
        """GET /api/admin/users/{id} → one user."""
        async with await self._client() as c:
            response = await c.get(f"/api/admin/users/{user_id}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def deactivate_user(self, user_id: int) -> dict[str, Any]:
        """POST /api/admin/users/{id}/deactivate."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/users/{user_id}/deactivate", headers=self._headers()
            )
            _raise_for_status(response)
            return dict(response.json())

    async def reactivate_user(self, user_id: int) -> dict[str, Any]:
        """POST /api/admin/users/{id}/reactivate."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/users/{user_id}/reactivate", headers=self._headers()
            )
            _raise_for_status(response)
            return dict(response.json())

    async def set_user_approver(self, user_id: int, *, is_approver: bool) -> dict[str, Any]:
        """POST /api/admin/users/{id}/set-approver."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/users/{user_id}/set-approver",
                headers=self._headers(),
                json={"is_approver": is_approver},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def set_user_role(self, user_id: int, *, role: str) -> dict[str, Any]:
        """POST /api/admin/users/{id}/set-role."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/users/{user_id}/set-role",
                headers=self._headers(),
                json={"role": role},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def update_user_identity(
        self,
        user_id: int,
        *,
        first_name: str,
        last_name: str,
        email: str,
        full_name: str = "",
    ) -> dict[str, Any]:
        """POST /api/admin/users/{id}/update-identity."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/users/{user_id}/update-identity",
                headers=self._headers(),
                json={
                    "first_name": first_name,
                    "last_name": last_name,
                    "full_name": full_name,
                    "email": email,
                },
            )
            _raise_for_status(response)
            return dict(response.json())

    async def set_user_password(self, user_id: int, *, password: str) -> dict[str, Any]:
        """POST /api/admin/users/{id}/set-password — admin-initiated reset."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/users/{user_id}/set-password",
                headers=self._headers(),
                json={"password": password},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def set_user_teams(self, user_id: int, *, team_ids: list[int]) -> dict[str, Any]:
        """POST /api/admin/users/{id}/set-teams — bulk-replace team membership."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/users/{user_id}/set-teams",
                headers=self._headers(),
                json={"team_ids": list(team_ids)},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def set_user_capability_roles(
        self, user_id: int, *, role_names: list[str]
    ) -> dict[str, Any]:
        """POST /api/admin/users/{id}/set-capability-roles — bulk-replace capability roles.

        Requires ``roles.manage``. Only ``capability``-kind role names
        are accepted (ADR-0005).
        """
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/users/{user_id}/set-capability-roles",
                headers=self._headers(),
                json={"role_names": list(role_names)},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def list_roles(self) -> list[dict[str, Any]]:
        """GET /api/admin/roles → every role + its permissions + member count."""
        async with await self._client() as c:
            response = await c.get("/api/admin/roles", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def create_role(
        self,
        *,
        name: str,
        description: str = "",
        permissions: list[str] | None = None,
        kind: str = "base",
    ) -> dict[str, Any]:
        """POST /api/admin/roles → newly created custom role (base or capability)."""
        async with await self._client() as c:
            response = await c.post(
                "/api/admin/roles",
                headers=self._headers(),
                json={
                    "name": name,
                    "description": description,
                    "permissions": list(permissions or []),
                    "kind": kind,
                },
            )
            _raise_for_status(response)
            return dict(response.json())

    async def set_role_permissions(self, role_id: int, *, permissions: list[str]) -> dict[str, Any]:
        """POST /api/admin/roles/{id}/set-permissions — bulk-replace the permission set."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/roles/{role_id}/set-permissions",
                headers=self._headers(),
                json={"permissions": list(permissions)},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def update_role_description(self, role_id: int, *, description: str) -> dict[str, Any]:
        """POST /api/admin/roles/{id}/update-description."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/roles/{role_id}/update-description",
                headers=self._headers(),
                json={"description": description},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def rename_role(self, role_id: int, *, name: str) -> dict[str, Any]:
        """POST /api/admin/roles/{id}/rename — rename a custom role."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/roles/{role_id}/rename",
                headers=self._headers(),
                json={"name": name},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def add_role_members(self, role_id: int, *, user_ids: list[int]) -> dict[str, Any]:
        """POST /api/admin/roles/{id}/members/bulk — grant a capability to many users."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/roles/{role_id}/members/bulk",
                headers=self._headers(),
                json={"user_ids": list(user_ids)},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def remove_role_member(self, role_id: int, *, user_id: int) -> None:
        """DELETE /api/admin/roles/{id}/members/{user_id} — revoke one capability grant."""
        async with await self._client() as c:
            response = await c.delete(
                f"/api/admin/roles/{role_id}/members/{user_id}",
                headers=self._headers(),
            )
            _raise_for_status(response)

    async def delete_role(self, role_id: int) -> None:
        """DELETE /api/admin/roles/{id} — refuses built-ins and in-use roles."""
        async with await self._client() as c:
            response = await c.delete(
                f"/api/admin/roles/{role_id}",
                headers=self._headers(),
            )
            _raise_for_status(response)

    async def list_permissions(self) -> list[dict[str, Any]]:
        """GET /api/admin/permissions → catalog of every permission slug + label + description."""
        async with await self._client() as c:
            response = await c.get("/api/admin/permissions", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    # ----- admin: teams --------------------------------------------------

    async def list_teams(self) -> list[dict[str, Any]]:
        """GET /api/admin/teams → every team + member count."""
        async with await self._client() as c:
            response = await c.get("/api/admin/teams", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def create_team(self, *, name: str, description: str = "") -> dict[str, Any]:
        """POST /api/admin/teams."""
        async with await self._client() as c:
            response = await c.post(
                "/api/admin/teams",
                headers=self._headers(),
                json={"name": name, "description": description},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def update_team(
        self, team_id: int, *, name: str, description: str = ""
    ) -> dict[str, Any]:
        """PUT /api/admin/teams/{id} — rename and/or re-describe a team."""
        async with await self._client() as c:
            response = await c.put(
                f"/api/admin/teams/{team_id}",
                headers=self._headers(),
                json={"name": name, "description": description},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def list_team_members(self, team_id: int) -> list[dict[str, Any]]:
        """GET /api/admin/teams/{id}/members."""
        async with await self._client() as c:
            response = await c.get(f"/api/admin/teams/{team_id}/members", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def add_team_member(self, team_id: int, *, user_id: int) -> None:
        """POST /api/admin/teams/{id}/members."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/teams/{team_id}/members",
                headers=self._headers(),
                json={"user_id": user_id},
            )
            _raise_for_status(response)

    async def add_team_members(self, team_id: int, *, user_ids: list[int]) -> None:
        """POST /api/admin/teams/{id}/members/bulk — add several users at once."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/teams/{team_id}/members/bulk",
                headers=self._headers(),
                json={"user_ids": list(user_ids)},
            )
            _raise_for_status(response)

    async def remove_team_member(self, team_id: int, *, user_id: int) -> None:
        """DELETE /api/admin/teams/{id}/members/{user_id}."""
        async with await self._client() as c:
            response = await c.delete(
                f"/api/admin/teams/{team_id}/members/{user_id}",
                headers=self._headers(),
            )
            _raise_for_status(response)

    # ----- admin: tools --------------------------------------------------

    async def list_admin_tools(self) -> list[dict[str, Any]]:
        """GET /api/admin/tools → every tool incl. archived + state."""
        async with await self._client() as c:
            response = await c.get("/api/admin/tools", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def get_admin_tool(self, alias: str) -> dict[str, Any]:
        """GET /api/admin/tools/{alias} → tool wrapper + full descriptor."""
        async with await self._client() as c:
            response = await c.get(f"/api/admin/tools/{alias}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def list_tool_versions(self, alias: str) -> list[dict[str, Any]]:
        """GET /api/admin/tools/{alias}/versions → change-log entries, newest first."""
        async with await self._client() as c:
            response = await c.get(f"/api/admin/tools/{alias}/versions", headers=self._headers())
            _raise_for_status(response)
            body = response.json()
            return list(body.get("versions") or [])

    async def list_tool_drafts(self) -> list[dict[str, Any]]:
        """GET /api/admin/tool-drafts → the caller's New Tool drafts, newest first."""
        async with await self._client() as c:
            response = await c.get("/api/admin/tool-drafts", headers=self._headers())
            _raise_for_status(response)
            body = response.json()
            return list(body.get("drafts") or [])

    async def get_tool_draft(self, draft_id: int) -> dict[str, Any]:
        """GET /api/admin/tool-drafts/{id} → full draft for resuming the form."""
        async with await self._client() as c:
            response = await c.get(f"/api/admin/tool-drafts/{draft_id}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def save_tool_draft(
        self,
        *,
        title: str,
        alias: str,
        payload: dict[str, Any],
        draft_id: int | None = None,
    ) -> dict[str, Any]:
        """POST /api/admin/tool-drafts → create, or update when ``draft_id`` set."""
        async with await self._client() as c:
            response = await c.post(
                "/api/admin/tool-drafts",
                headers=self._headers(),
                json={
                    "draft_id": draft_id,
                    "title": title,
                    "alias": alias,
                    "payload": payload,
                },
            )
            _raise_for_status(response)
            return dict(response.json())

    async def delete_tool_draft(self, draft_id: int) -> None:
        """DELETE /api/admin/tool-drafts/{id} — discard one of the caller's drafts."""
        async with await self._client() as c:
            response = await c.delete(f"/api/admin/tool-drafts/{draft_id}", headers=self._headers())
            _raise_for_status(response)

    async def set_tool_run_cap(self, alias: str, run_cap: int | None) -> dict[str, Any]:
        """POST /api/admin/tools/{alias}/run-cap → AdminToolResponse.

        ``run_cap = None`` clears the per-tool override (study creation
        falls back to the install-wide default — currently 500). See
        ADR-0001 2026-05-20 revision / designs/parametric-studies.md §16.
        """
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/tools/{alias}/run-cap",
                headers=self._headers(),
                json={"run_cap": run_cap},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def set_tool_python_env(self, alias: str, python_env_id: int | None) -> dict[str, Any]:
        """POST /api/admin/tools/{alias}/python-env → AdminToolResponse.

        ``python_env_id = None`` clears the pin (jobs fall back to the
        MATOFX server's own interpreter — the "main MATOFX environment").
        Otherwise the id must reference an active ``python_env`` row.
        """
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/tools/{alias}/python-env",
                headers=self._headers(),
                json={"python_env_id": python_env_id},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def list_tool_files(self, alias: str) -> dict[str, Any]:
        """GET /api/admin/tools/{alias}/files → bundle file tree for the editor."""
        async with await self._client() as c:
            response = await c.get(f"/api/admin/tools/{alias}/files", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def read_tool_file(self, alias: str, file_path: str) -> dict[str, Any]:
        """GET /api/admin/tools/{alias}/files/{path} → UTF-8 contents."""
        async with await self._client() as c:
            response = await c.get(
                f"/api/admin/tools/{alias}/files/{file_path}",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    async def save_tool_file(
        self,
        alias: str,
        file_path: str,
        *,
        contents: str,
        expected_row_version: int | None = None,
    ) -> dict[str, Any]:
        """POST /api/admin/tools/{alias}/files/{path} → minor-bumped tool row."""
        body: dict[str, Any] = {"contents": contents}
        if expected_row_version is not None:
            body["expected_row_version"] = expected_row_version
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/tools/{alias}/files/{file_path}",
                headers=self._headers(),
                json=body,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def promote_tool(self, alias: str, *, new_state: str) -> dict[str, Any]:
        """POST /api/admin/tools/{alias}/promote."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/tools/{alias}/promote",
                headers=self._headers(),
                json={"new_state": new_state},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def archive_tool(self, alias: str, *, reason: str = "") -> dict[str, Any]:
        """POST /api/admin/tools/{alias}/archive."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/tools/{alias}/archive",
                headers=self._headers(),
                json={"reason": reason},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def unarchive_tool(self, alias: str) -> dict[str, Any]:
        """POST /api/admin/tools/{alias}/unarchive — re-activate a previously archived tool."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/tools/{alias}/unarchive",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    async def create_tool(
        self,
        *,
        metadata: dict[str, Any],
        descriptor_json_bytes: bytes,
        descriptor_filename: str,
        static_zip_bytes: bytes,
        static_zip_filename: str,
        icon_bytes: bytes | None = None,
        icon_filename: str | None = None,
        presets_bytes: bytes | None = None,
        presets_filename: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/admin/tools — multipart upload + JSON metadata.

        ``metadata`` is the form fields the operator filled in
        (alias, name, version, short_description, long_description,
        main_script, teams,
        downstream_tools, resources, licences, approvals_policy,
        approver_usernames, email_notifications). The four file
        arguments carry the bundle's descriptor JSON + static-files
        ZIP + optional icon + optional presets.
        """
        import json  # noqa: PLC0415

        files: list[tuple[str, tuple[str, bytes, str]]] = [
            (
                "descriptor_json",
                (descriptor_filename, descriptor_json_bytes, "application/json"),
            ),
            (
                "static_zip",
                (static_zip_filename, static_zip_bytes, "application/zip"),
            ),
        ]
        if icon_bytes is not None and icon_filename:
            files.append(("icon", (icon_filename, icon_bytes, "image/png")))
        if presets_bytes is not None and presets_filename:
            files.append(("presets", (presets_filename, presets_bytes, "application/json")))
        async with await self._client() as c:
            response = await c.post(
                "/api/admin/tools",
                headers=self._headers(),
                data={"metadata": json.dumps(metadata)},
                files=files,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def update_tool_descriptor(
        self,
        alias: str,
        *,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """POST /api/admin/tools/{alias}/update-descriptor — metadata-only edit."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/tools/{alias}/update-descriptor",
                headers=self._headers(),
                json=body,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def register_downstream_tool(self, *, source_alias: str, downstream_alias: str) -> bool:
        """Ensure ``source_alias`` lists ``downstream_alias`` in its downstream tools.

        Used by the tool editor's upstream-wiring flow so an author can
        make another tool a *source* for an auto-populated input without
        leaving the page. Fetches ``source_alias``'s current descriptor,
        adds ``downstream_alias`` to its ``downstream_tools`` only if
        absent, and re-saves the full metadata unchanged otherwise (so no
        other field is clobbered) via update-descriptor — which
        patch-bumps the source tool.

        Returns ``True`` when a patch was applied, ``False`` when the
        source already listed the downstream (a no-op, no version bump).
        """
        detail = await self.get_admin_tool(source_alias)
        descriptor = dict(detail.get("descriptor") or {})
        current = [str(a) for a in (descriptor.get("downstream_tools") or [])]
        if downstream_alias in current:
            return False
        resources = dict(descriptor.get("resources") or {})
        body: dict[str, Any] = {
            "name": str(detail.get("name") or ""),
            "version": str(detail.get("version") or "0.1.0"),
            "short_description": str(detail.get("short_description") or ""),
            "long_description": detail.get("long_description"),
            "teams": list(descriptor.get("teams") or []),
            "downstream_tools": [*current, downstream_alias],
            "cpu_cores": int(resources.get("cpu_cores", 1)),
            "ram_mb": int(resources.get("ram_mb", 512)),
            "gpu_memory_mb": int(resources.get("gpu_memory_mb", 0)),
            "licences_required": list(descriptor.get("licences_required") or []),
            "duration_class": str(descriptor.get("duration_class") or "medium"),
            "approvals_policy": str(descriptor.get("approvals_policy") or "none"),
            "approver_usernames": list(descriptor.get("approver_usernames") or []),
            # Carry the tool's own setting through unchanged. Absent
            # reads as off, the same as the runtime gate gives it — this
            # request must not switch mail *on* as a side effect of
            # wiring up a downstream tool.
            "email_notifications": bool(descriptor.get("email_notifications", False)),
        }
        await self.update_tool_descriptor(source_alias, body=body)
        return True

    async def set_tool_approvers(
        self, alias: str, *, approver_usernames: list[str]
    ) -> dict[str, Any]:
        """POST /api/admin/tools/{alias}/set-approvers — bulk-replace the approver list."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/tools/{alias}/set-approvers",
                headers=self._headers(),
                json={"approver_usernames": list(approver_usernames)},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def replace_tool_bundle(
        self,
        alias: str,
        *,
        metadata: dict[str, Any],
        static_zip_bytes: bytes,
        static_zip_filename: str,
        descriptor_json_bytes: bytes | None = None,
        descriptor_filename: str | None = None,
        icon_bytes: bytes | None = None,
        icon_filename: str | None = None,
        presets_bytes: bytes | None = None,
        presets_filename: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/admin/tools/{alias}/replace-bundle.

        The descriptor JSON is optional — omit it to keep the tool's
        current inputs / outputs / layout and only swap the bundle files.
        """
        import json  # noqa: PLC0415

        files: list[tuple[str, tuple[str, bytes, str]]] = [
            (
                "static_zip",
                (static_zip_filename, static_zip_bytes, "application/zip"),
            ),
        ]
        if descriptor_json_bytes is not None and descriptor_filename:
            files.append(
                (
                    "descriptor_json",
                    (descriptor_filename, descriptor_json_bytes, "application/json"),
                )
            )
        if icon_bytes is not None and icon_filename:
            files.append(("icon", (icon_filename, icon_bytes, "image/png")))
        if presets_bytes is not None and presets_filename:
            files.append(("presets", (presets_filename, presets_bytes, "application/json")))
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/tools/{alias}/replace-bundle",
                headers=self._headers(),
                data={"metadata": json.dumps(metadata)},
                files=files,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def update_tool_icon(
        self,
        alias: str,
        *,
        icon_bytes: bytes,
        icon_filename: str,
    ) -> dict[str, Any]:
        """POST /api/admin/tools/{alias}/icon — drop in a new icon PNG only."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/tools/{alias}/icon",
                headers=self._headers(),
                files={"icon": (icon_filename, icon_bytes, "image/png")},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def get_tool_icon_bytes(self, alias: str) -> tuple[bytes, str] | None:
        """GET /api/tools/{alias}/icon — return ``(bytes, mime)`` or ``None`` on 404."""
        async with await self._client() as c:
            response = await c.get(
                f"/api/tools/{alias}/icon",
                headers=self._headers(),
            )
            if response.status_code == httpx.codes.NOT_FOUND:
                return None
            _raise_for_status(response)
            return response.content, response.headers.get("content-type", "image/png")

    async def update_tool_guides(
        self,
        alias: str,
        *,
        manifest: list[dict[str, Any]],
        new_images: list[tuple[str, bytes]],
    ) -> dict[str, Any]:
        """POST /api/admin/tools/{alias}/guide-images — replace the guide-image set.

        ``manifest`` is the desired ordered list of ``{caption,
        existing_filename?}``; ``new_images`` are the ``(filename, bytes)``
        for the manifest's new entries, in order. Sent as multipart only
        when there are new images (else a plain form post carrying the
        manifest — e.g. a caption-only edit, a reorder, or clearing all).
        """
        data = {"manifest": json.dumps(manifest)}
        async with await self._client() as c:
            if new_images:
                files = [("images", (fn, by, _guide_image_mime(fn))) for fn, by in new_images]
                response = await c.post(
                    f"/api/admin/tools/{alias}/guide-images",
                    headers=self._headers(),
                    data=data,
                    files=files,
                )
            else:
                response = await c.post(
                    f"/api/admin/tools/{alias}/guide-images",
                    headers=self._headers(),
                    data=data,
                )
            _raise_for_status(response)
            return dict(response.json())

    async def get_tool_guide_image_bytes(self, alias: str, index: int) -> tuple[bytes, str] | None:
        """GET /api/tools/{alias}/guide-images/{index} — ``(bytes, mime)`` or ``None`` on 404."""
        async with await self._client() as c:
            response = await c.get(
                f"/api/tools/{alias}/guide-images/{index}",
                headers=self._headers(),
            )
            if response.status_code == httpx.codes.NOT_FOUND:
                return None
            _raise_for_status(response)
            return response.content, response.headers.get("content-type", "image/png")

    async def update_workflow_icon(
        self,
        alias: str,
        *,
        icon_bytes: bytes,
        icon_filename: str,
    ) -> dict[str, Any]:
        """POST /api/admin/workflows/{alias}/icon — drop in a new icon PNG."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/workflows/{alias}/icon",
                headers=self._headers(),
                files={"icon": (icon_filename, icon_bytes, "image/png")},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def get_workflow_icon_bytes(self, alias: str) -> tuple[bytes, str] | None:
        """GET /api/workflows/{alias}/icon — return ``(bytes, mime)`` or ``None`` on 404."""
        async with await self._client() as c:
            response = await c.get(
                f"/api/workflows/{alias}/icon",
                headers=self._headers(),
            )
            if response.status_code == httpx.codes.NOT_FOUND:
                return None
            _raise_for_status(response)
            return response.content, response.headers.get("content-type", "image/png")

    # ----- admin: storage locations --------------------------------------

    async def list_storage_locations(self) -> list[dict[str, Any]]:
        """GET /api/admin/storage-locations."""
        async with await self._client() as c:
            response = await c.get("/api/admin/storage-locations", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def create_storage_location(self, *, name: str, mount_path: str) -> dict[str, Any]:
        """POST /api/admin/storage-locations."""
        async with await self._client() as c:
            response = await c.post(
                "/api/admin/storage-locations",
                headers=self._headers(),
                json={"name": name, "mount_path": mount_path},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def retire_storage_location(self, row_id: int) -> dict[str, Any]:
        """POST /api/admin/storage-locations/{id}/retire."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/storage-locations/{row_id}/retire",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    # ----- admin: python envs --------------------------------------------

    async def list_python_envs(self) -> list[dict[str, Any]]:
        """GET /api/admin/python-envs."""
        async with await self._client() as c:
            response = await c.get("/api/admin/python-envs", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def create_python_env(
        self,
        *,
        name: str,
        interpreter_path: str,
        storage_location_id: int | None = None,
    ) -> dict[str, Any]:
        """POST /api/admin/python-envs."""
        async with await self._client() as c:
            response = await c.post(
                "/api/admin/python-envs",
                headers=self._headers(),
                json={
                    "name": name,
                    "interpreter_path": interpreter_path,
                    "storage_location_id": storage_location_id,
                },
            )
            _raise_for_status(response)
            return dict(response.json())

    async def deactivate_python_env(self, row_id: int) -> dict[str, Any]:
        """POST /api/admin/python-envs/{id}/deactivate."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/python-envs/{row_id}/deactivate",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    # ----- admin: AOS hosts ----------------------------------------------

    async def list_aos_hosts(self) -> list[dict[str, Any]]:
        """GET /api/admin/aos-hosts."""
        async with await self._client() as c:
            response = await c.get("/api/admin/aos-hosts", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def create_aos_host(self, *, body: dict[str, Any]) -> dict[str, Any]:
        """POST /api/admin/aos-hosts."""
        async with await self._client() as c:
            response = await c.post("/api/admin/aos-hosts", headers=self._headers(), json=body)
            _raise_for_status(response)
            return dict(response.json())

    async def update_aos_host(self, row_id: int, *, body: dict[str, Any]) -> dict[str, Any]:
        """PUT /api/admin/aos-hosts/{id}."""
        async with await self._client() as c:
            response = await c.put(
                f"/api/admin/aos-hosts/{row_id}", headers=self._headers(), json=body
            )
            _raise_for_status(response)
            return dict(response.json())

    async def set_aos_host_active(self, row_id: int, *, active: bool) -> dict[str, Any]:
        """POST /api/admin/aos-hosts/{id}/{activate|deactivate}."""
        verb = "activate" if active else "deactivate"
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/aos-hosts/{row_id}/{verb}", headers=self._headers()
            )
            _raise_for_status(response)
            return dict(response.json())

    # ----- admin: HPC clusters -------------------------------------------

    async def list_hpc_clusters(self) -> list[dict[str, Any]]:
        """GET /api/admin/hpc-clusters."""
        async with await self._client() as c:
            response = await c.get("/api/admin/hpc-clusters", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def create_hpc_cluster(self, *, body: dict[str, Any]) -> dict[str, Any]:
        """POST /api/admin/hpc-clusters."""
        async with await self._client() as c:
            response = await c.post("/api/admin/hpc-clusters", headers=self._headers(), json=body)
            _raise_for_status(response)
            return dict(response.json())

    async def update_hpc_cluster(self, row_id: int, *, body: dict[str, Any]) -> dict[str, Any]:
        """PUT /api/admin/hpc-clusters/{id}."""
        async with await self._client() as c:
            response = await c.put(
                f"/api/admin/hpc-clusters/{row_id}", headers=self._headers(), json=body
            )
            _raise_for_status(response)
            return dict(response.json())

    async def set_hpc_cluster_active(self, row_id: int, *, active: bool) -> dict[str, Any]:
        """POST /api/admin/hpc-clusters/{id}/{activate|deactivate}."""
        verb = "activate" if active else "deactivate"
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/hpc-clusters/{row_id}/{verb}", headers=self._headers()
            )
            _raise_for_status(response)
            return dict(response.json())

    # ----- admin: runtime settings ---------------------------------------

    async def get_runtime_settings(self) -> dict[str, Any]:
        """GET /api/admin/runtime-settings → ``{settings: {key: value}}``."""
        async with await self._client() as c:
            response = await c.get("/api/admin/runtime-settings", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def set_runtime_setting(self, key: str, *, value: Any) -> dict[str, Any]:
        """POST /api/admin/runtime-settings/{key}."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/runtime-settings/{key}",
                headers=self._headers(),
                json={"value": value},
            )
            _raise_for_status(response)
            return dict(response.json())

    # ----- admin: db explorer --------------------------------------------

    async def list_db_tables(self) -> list[dict[str, Any]]:
        """GET /api/admin/db/tables → list of allowed tables with metadata."""
        async with await self._client() as c:
            response = await c.get("/api/admin/db/tables", headers=self._headers())
            _raise_for_status(response)
            return list(response.json().get("tables", []))

    async def get_db_diagnostics(self) -> dict[str, Any]:
        """GET /api/admin/db/diagnostics → bundled diagnostics-tab snapshot."""
        async with await self._client() as c:
            response = await c.get("/api/admin/db/diagnostics", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def get_user_activity(self, user_id: int, *, limit: int = 50) -> dict[str, Any]:
        """GET /api/admin/users/{id}/activity → bundled per-user dashboard data."""
        async with await self._client() as c:
            response = await c.get(
                f"/api/admin/users/{user_id}/activity",
                headers=self._headers(),
                params={"limit": str(limit)},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def read_db_table(
        self,
        name: str,
        *,
        page: int = 1,
        page_size: int = 100,
        sort_by: str | None = None,
        sort_order: str = "asc",
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET /api/admin/db/tables/{name} → one page of rows + metadata.

        ``filters`` is a recursive boolean filter tree (root group);
        it's JSON-encoded into the ``filters`` query parameter.
        """
        import json  # noqa: PLC0415

        params: dict[str, str] = {
            "page": str(page),
            "page_size": str(page_size),
            "sort_order": sort_order,
        }
        if sort_by:
            params["sort_by"] = sort_by
        if filters:
            params["filters"] = json.dumps(filters)
        async with await self._client() as c:
            response = await c.get(
                f"/api/admin/db/tables/{name}",
                headers=self._headers(),
                params=params,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def get_column_distinct_values(self, table: str, column: str) -> dict[str, Any]:
        """GET …/tables/{table}/columns/{column}/distinct → value-picker data."""
        async with await self._client() as c:
            response = await c.get(
                f"/api/admin/db/tables/{table}/columns/{column}/distinct",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    # ----- audit log ----------------------------------------------------

    @staticmethod
    def _audit_params(
        *,
        user: str | None = None,
        actor_type: str | None = None,
        action: list[str] | None = None,
        resource_type: list[str] | None = None,
        resource_id: str | None = None,
        outcome: str | None = None,
        correlation_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        """Shared query params for the audit log/export endpoints.

        ``action`` / ``resource_type`` are repeatable (multi-select) and pass
        through as list values so httpx emits one query key per entry.
        """
        params: dict[str, Any] = {}
        for name, value in (
            ("user", user),
            ("actor_type", actor_type),
            ("resource_id", resource_id),
            ("outcome", outcome),
            ("correlation_id", correlation_id),
            ("since", since),
            ("until", until),
        ):
            if value:
                params[name] = value
        if action:
            params["action"] = list(action)
        if resource_type:
            params["resource_type"] = list(resource_type)
        return params

    async def list_audit_log(
        self,
        *,
        page: int = 1,
        page_size: int = 100,
        user: str | None = None,
        actor_type: str | None = None,
        action: list[str] | None = None,
        resource_type: list[str] | None = None,
        resource_id: str | None = None,
        outcome: str | None = None,
        correlation_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        """GET /api/admin/audit/log → one page of audit rows + total count."""
        params = self._audit_params(
            user=user,
            actor_type=actor_type,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            correlation_id=correlation_id,
            since=since,
            until=until,
        )
        params["page"] = str(page)
        params["page_size"] = str(page_size)
        async with await self._client() as c:
            response = await c.get(
                "/api/admin/audit/log",
                headers=self._headers(),
                params=params,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def audit_log_filters(self) -> dict[str, Any]:
        """GET /api/admin/audit/filters → dropdown option lists."""
        async with await self._client() as c:
            response = await c.get("/api/admin/audit/filters", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def export_audit_log(
        self,
        *,
        user: str | None = None,
        actor_type: str | None = None,
        action: list[str] | None = None,
        resource_type: list[str] | None = None,
        resource_id: str | None = None,
        outcome: str | None = None,
        correlation_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> bytes:
        """GET /api/admin/audit/export → CSV bytes for the current filter."""
        params = self._audit_params(
            user=user,
            actor_type=actor_type,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            correlation_id=correlation_id,
            since=since,
            until=until,
        )
        async with await self._client() as c:
            response = await c.get(
                "/api/admin/audit/export",
                headers=self._headers(),
                params=params,
            )
            _raise_for_status(response)
            return response.content

    async def verify_audit_chain(
        self,
        *,
        start_id: int | None = None,
        end_id: int | None = None,
    ) -> dict[str, Any]:
        """POST /api/admin/audit/verify → ``{ok, records_checked, failures}``."""
        params: dict[str, str] = {}
        if start_id is not None:
            params["start_id"] = str(start_id)
        if end_id is not None:
            params["end_id"] = str(end_id)
        async with await self._client() as c:
            response = await c.post(
                "/api/admin/audit/verify",
                headers=self._headers(),
                params=params,
            )
            _raise_for_status(response)
            return dict(response.json())

    # ----- usage statistics ---------------------------------------------

    async def usage_filters(self) -> dict[str, Any]:
        """GET /api/admin/usage/filters → filter-bar dropdown options."""
        async with await self._client() as c:
            response = await c.get("/api/admin/usage/filters", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    @staticmethod
    def _usage_params(
        *,
        since: str | None,
        until: str | None,
        tool_aliases: list[str] | None,
        team_ids: list[int] | None,
        roles: list[str] | None,
        execution_targets: list[str] | None,
    ) -> dict[str, Any]:
        """Build the shared usage query params (repeated keys for multi-select)."""
        params: dict[str, Any] = {}
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        if tool_aliases:
            params["tool_alias"] = list(tool_aliases)
        if team_ids:
            params["team_id"] = [str(t) for t in team_ids]
        if roles:
            params["role"] = list(roles)
        if execution_targets:
            params["execution_target"] = list(execution_targets)
        return params

    async def usage_overview(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        tool_aliases: list[str] | None = None,
        team_ids: list[int] | None = None,
        roles: list[str] | None = None,
        execution_targets: list[str] | None = None,
    ) -> dict[str, Any]:
        """GET /api/admin/usage/overview → the Overview tab payload."""
        params = self._usage_params(
            since=since,
            until=until,
            tool_aliases=tool_aliases,
            team_ids=team_ids,
            roles=roles,
            execution_targets=execution_targets,
        )
        async with await self._client() as c:
            response = await c.get(
                "/api/admin/usage/overview",
                headers=self._headers(),
                params=params,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def usage_compute(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        tool_aliases: list[str] | None = None,
        team_ids: list[int] | None = None,
        roles: list[str] | None = None,
        execution_targets: list[str] | None = None,
    ) -> dict[str, Any]:
        """GET /api/admin/usage/compute → the Compute & Throughput tab payload."""
        params = self._usage_params(
            since=since,
            until=until,
            tool_aliases=tool_aliases,
            team_ids=team_ids,
            roles=roles,
            execution_targets=execution_targets,
        )
        async with await self._client() as c:
            response = await c.get(
                "/api/admin/usage/compute",
                headers=self._headers(),
                params=params,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def usage_reliability(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        tool_aliases: list[str] | None = None,
        team_ids: list[int] | None = None,
        roles: list[str] | None = None,
        execution_targets: list[str] | None = None,
    ) -> dict[str, Any]:
        """GET /api/admin/usage/reliability → the Reliability tab payload."""
        params = self._usage_params(
            since=since,
            until=until,
            tool_aliases=tool_aliases,
            team_ids=team_ids,
            roles=roles,
            execution_targets=execution_targets,
        )
        async with await self._client() as c:
            response = await c.get(
                "/api/admin/usage/reliability",
                headers=self._headers(),
                params=params,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def _usage_tab(self, tab: str, **filters: Any) -> dict[str, Any]:
        """GET /api/admin/usage/{tab} with the shared filter params."""
        params = self._usage_params(
            since=filters.get("since"),
            until=filters.get("until"),
            tool_aliases=filters.get("tool_aliases"),
            team_ids=filters.get("team_ids"),
            roles=filters.get("roles"),
            execution_targets=filters.get("execution_targets"),
        )
        async with await self._client() as c:
            response = await c.get(
                f"/api/admin/usage/{tab}",
                headers=self._headers(),
                params=params,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def usage_people(self, **filters: Any) -> dict[str, Any]:
        """GET /api/admin/usage/people → the People & Adoption tab payload."""
        return await self._usage_tab("people", **filters)

    async def usage_seats(self, **filters: Any) -> dict[str, Any]:
        """GET /api/admin/usage/seats → the Seats & Licence tab payload."""
        return await self._usage_tab("seats", **filters)

    async def usage_infra(self, **filters: Any) -> dict[str, Any]:
        """GET /api/admin/usage/infra → the Infrastructure tab payload."""
        return await self._usage_tab("infra", **filters)

    async def usage_export(
        self,
        *,
        fmt: str,
        tabs: list[str] | None = None,
        **filters: Any,
    ) -> bytes:
        """GET /api/admin/usage/export → raw export bytes (CSV ZIP or HTML)."""
        params = self._usage_params(
            since=filters.get("since"),
            until=filters.get("until"),
            tool_aliases=filters.get("tool_aliases"),
            team_ids=filters.get("team_ids"),
            roles=filters.get("roles"),
            execution_targets=filters.get("execution_targets"),
        )
        params["format"] = fmt
        if tabs:
            params["tab"] = list(tabs)
        async with await self._client() as c:
            response = await c.get(
                "/api/admin/usage/export",
                headers=self._headers(),
                params=params,
            )
            _raise_for_status(response)
            return response.content

    async def usage_digest_send(
        self,
        *,
        recipients: list[str],
        tabs: list[str] | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        """POST /api/admin/usage/digest/send → ``{disabled, attempted, sent, failed}``."""
        params = self._usage_params(
            since=filters.get("since"),
            until=filters.get("until"),
            tool_aliases=filters.get("tool_aliases"),
            team_ids=filters.get("team_ids"),
            roles=filters.get("roles"),
            execution_targets=filters.get("execution_targets"),
        )
        async with await self._client() as c:
            response = await c.post(
                "/api/admin/usage/digest/send",
                headers=self._headers(),
                params=params,
                json={"recipients": recipients, "tabs": tabs or []},
            )
            _raise_for_status(response)
            return dict(response.json())

    # ----- workflows ----------------------------------------------------

    async def list_admin_workflows(self) -> list[dict[str, Any]]:
        """GET /api/admin/workflows → every workflow (active + archived)."""
        async with await self._client() as c:
            response = await c.get("/api/admin/workflows", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def get_admin_workflow(self, alias: str) -> dict[str, Any]:
        """GET /api/admin/workflows/{alias} → single workflow + ordered members."""
        async with await self._client() as c:
            response = await c.get(f"/api/admin/workflows/{alias}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def create_workflow(
        self,
        *,
        alias: str,
        name: str,
        description: str = "",
        teams: list[str] | None = None,
        tool_aliases: list[str],
    ) -> dict[str, Any]:
        """POST /api/admin/workflows → newly created workflow."""
        async with await self._client() as c:
            response = await c.post(
                "/api/admin/workflows",
                headers=self._headers(),
                json={
                    "alias": alias,
                    "name": name,
                    "description": description,
                    "teams": list(teams or []),
                    "tool_aliases": list(tool_aliases),
                },
            )
            _raise_for_status(response)
            return dict(response.json())

    async def update_workflow(
        self,
        alias: str,
        *,
        name: str,
        description: str = "",
        teams: list[str] | None = None,
        tool_aliases: list[str],
    ) -> dict[str, Any]:
        """POST /api/admin/workflows/{alias}/update — bumps version + snapshots."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/workflows/{alias}/update",
                headers=self._headers(),
                json={
                    "name": name,
                    "description": description,
                    "teams": list(teams or []),
                    "tool_aliases": list(tool_aliases),
                },
            )
            _raise_for_status(response)
            return dict(response.json())

    async def promote_workflow(self, alias: str, *, new_state: str) -> dict[str, Any]:
        """POST /api/admin/workflows/{alias}/promote."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/workflows/{alias}/promote",
                headers=self._headers(),
                json={"new_state": new_state},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def archive_workflow(self, alias: str, *, reason: str = "") -> dict[str, Any]:
        """POST /api/admin/workflows/{alias}/archive."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/workflows/{alias}/archive",
                headers=self._headers(),
                json={"reason": reason},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def unarchive_workflow(self, alias: str) -> dict[str, Any]:
        """POST /api/admin/workflows/{alias}/unarchive."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/workflows/{alias}/unarchive",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    async def list_workflow_versions(self, alias: str) -> list[dict[str, Any]]:
        """GET /api/admin/workflows/{alias}/versions → change-log entries."""
        async with await self._client() as c:
            response = await c.get(
                f"/api/admin/workflows/{alias}/versions",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return list(response.json().get("versions") or [])

    async def list_visible_workflows(self) -> list[dict[str, Any]]:
        """GET /api/workflows → workflows visible to the caller."""
        async with await self._client() as c:
            response = await c.get("/api/workflows", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    # ----- favourites --------------------------------------------------

    async def get_my_favourites(self) -> dict[str, Any]:
        """GET /api/me/favourites → ``{enabled, tools, workflows}``.

        Always returns a dict even when the feature is disabled —
        the ``enabled`` flag is how the UI decides whether to draw
        the star icons. Tool / workflow lists are alphabetical by
        alias.
        """
        async with await self._client() as c:
            response = await c.get("/api/me/favourites", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def add_favourite(self, *, kind: str, alias: str) -> dict[str, Any]:
        """PUT /api/me/favourites/{kind}/{alias} → updated snapshot.

        Idempotent server-side; the UI may call this redundantly
        without worrying about pre-checking the current state.
        """
        async with await self._client() as c:
            response = await c.put(f"/api/me/favourites/{kind}/{alias}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def remove_favourite(self, *, kind: str, alias: str) -> dict[str, Any]:
        """DELETE /api/me/favourites/{kind}/{alias} → updated snapshot.

        Idempotent server-side.
        """
        async with await self._client() as c:
            response = await c.delete(f"/api/me/favourites/{kind}/{alias}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    # ----- custom workflows --------------------------------------------

    async def list_my_custom_workflows(
        self, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        """GET /api/workflows/custom → owner-owned + shared-with-me rows."""
        params: dict[str, Any] = {}
        if include_archived:
            params["include_archived"] = "true"
        async with await self._client() as c:
            response = await c.get(
                "/api/workflows/custom",
                headers=self._headers(),
                params=params,
            )
            _raise_for_status(response)
            return list(response.json())

    async def get_custom_workflow(self, workflow_id: int) -> dict[str, Any]:
        """GET /api/workflows/custom/{id} → full row + members + shares."""
        async with await self._client() as c:
            response = await c.get(f"/api/workflows/custom/{workflow_id}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def create_custom_workflow(
        self,
        *,
        name: str,
        description: str,
        tool_aliases: list[str],
        share_with_user_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """POST /api/workflows/custom → newly-created custom workflow."""
        async with await self._client() as c:
            response = await c.post(
                "/api/workflows/custom",
                headers=self._headers(),
                json={
                    "name": name,
                    "description": description,
                    "tool_aliases": list(tool_aliases),
                    "share_with_user_ids": list(share_with_user_ids or []),
                },
            )
            _raise_for_status(response)
            return dict(response.json())

    async def update_custom_workflow(
        self,
        workflow_id: int,
        *,
        name: str,
        description: str,
        tool_aliases: list[str],
        share_with_user_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """POST /api/workflows/custom/{id}/update — owner edit."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/workflows/custom/{workflow_id}/update",
                headers=self._headers(),
                json={
                    "name": name,
                    "description": description,
                    "tool_aliases": list(tool_aliases),
                    "share_with_user_ids": list(share_with_user_ids or []),
                },
            )
            _raise_for_status(response)
            return dict(response.json())

    async def archive_custom_workflow(
        self, workflow_id: int, *, reason: str = ""
    ) -> dict[str, Any]:
        """POST /api/workflows/custom/{id}/archive."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/workflows/custom/{workflow_id}/archive",
                headers=self._headers(),
                json={"reason": reason},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def copy_custom_workflow(self, workflow_id: int) -> dict[str, Any]:
        """POST /api/workflows/custom/{id}/copy → owner-owned fork."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/workflows/custom/{workflow_id}/copy",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    async def unarchive_custom_workflow(self, workflow_id: int) -> dict[str, Any]:
        """POST /api/workflows/custom/{id}/unarchive."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/workflows/custom/{workflow_id}/unarchive",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    async def list_custom_workflow_share_candidates(
        self,
    ) -> list[dict[str, Any]]:
        """GET /api/workflows/custom/_meta/share-candidates."""
        async with await self._client() as c:
            response = await c.get(
                "/api/workflows/custom/_meta/share-candidates",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return list(response.json())

    async def admin_list_custom_workflows(
        self,
        *,
        owner_id: int | None = None,
        contains_tool_alias: str | None = None,
        include_archived: bool = True,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /api/admin/custom-workflows — inspector listing (any owner)."""
        params: dict[str, Any] = {"include_archived": str(include_archived).lower()}
        if owner_id is not None:
            params["owner_id"] = owner_id
        if contains_tool_alias is not None:
            params["contains_tool_alias"] = contains_tool_alias
        if search:
            params["search"] = search
        async with await self._client() as c:
            response = await c.get(
                "/api/admin/custom-workflows",
                headers=self._headers(),
                params=params,
            )
            _raise_for_status(response)
            return list(response.json())

    async def start_workflow_run(
        self,
        scenario_id: int,
        *,
        workflow_alias: str,
        name: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/scenarios/{sid}/workflow-runs → newly-created run."""
        payload: dict[str, Any] = {"workflow_alias": workflow_alias}
        if name is not None:
            payload["name"] = name
        async with await self._client() as c:
            response = await c.post(
                f"/api/scenarios/{scenario_id}/workflow-runs",
                headers=self._headers(),
                json=payload,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def get_workflow_run(self, run_id: int) -> dict[str, Any]:
        """GET /api/workflow-runs/{id} → full run + children."""
        async with await self._client() as c:
            response = await c.get(f"/api/workflow-runs/{run_id}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def cancel_workflow_run(self, run_id: int) -> dict[str, Any]:
        """POST /api/workflow-runs/{id}/cancel."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/workflow-runs/{run_id}/cancel",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    async def list_workflow_runs_for_scenario(self, scenario_id: int) -> list[dict[str, Any]]:
        """GET /api/scenarios/{sid}/workflow-runs → newest first."""
        async with await self._client() as c:
            response = await c.get(
                f"/api/scenarios/{scenario_id}/workflow-runs",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return list(response.json())

    async def list_my_visible_workflow_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """GET /api/users/me/visible-workflow-runs → cross-project list."""
        async with await self._client() as c:
            response = await c.get(
                "/api/users/me/visible-workflow-runs",
                headers=self._headers(),
                params={"limit": limit},
            )
            _raise_for_status(response)
            return list(response.json())

    # ----- tutorials ----------------------------------------------------

    async def list_active_tutorials(self) -> list[dict[str, Any]]:
        """GET /api/tutorials → all *active* tutorials (home-page view)."""
        async with await self._client() as c:
            response = await c.get("/api/tutorials", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def list_admin_tutorials(self) -> list[dict[str, Any]]:
        """GET /api/admin/tutorials → every tutorial (active + inactive)."""
        async with await self._client() as c:
            response = await c.get("/api/admin/tutorials", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def upload_tutorial(
        self,
        *,
        media_type: str,
        title: str,
        filename: str,
        file_bytes: bytes,
        mime_type: str,
        tool_alias: str | None = None,
        workflow_alias: str | None = None,
        version: str = "1",
        description: str = "",
        activate: bool = True,
    ) -> dict[str, Any]:
        """POST /api/admin/tutorials — multipart upload + form metadata.

        At most one of ``tool_alias`` / ``workflow_alias`` may be set;
        both blank ⇒ a general tutorial. Server enforces the
        mutual-exclusion rule (see :class:`ScopeConflictError`).
        """
        data: dict[str, str] = {
            "media_type": media_type,
            "title": title,
            "version": version,
            "description": description,
            "activate": "true" if activate else "false",
        }
        if tool_alias:
            data["tool_alias"] = tool_alias
        if workflow_alias:
            data["workflow_alias"] = workflow_alias
        files = {"file": (filename, file_bytes, mime_type)}
        async with await self._client() as c:
            response = await c.post(
                "/api/admin/tutorials",
                headers=self._headers(),
                data=data,
                files=files,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def activate_tutorial(self, tutorial_id: int) -> dict[str, Any]:
        """POST /api/admin/tutorials/{id}/activate."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/tutorials/{tutorial_id}/activate",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    async def deactivate_tutorial(self, tutorial_id: int) -> dict[str, Any]:
        """POST /api/admin/tutorials/{id}/deactivate."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/tutorials/{tutorial_id}/deactivate",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    async def update_tutorial(
        self,
        tutorial_id: int,
        *,
        filename: str,
        file_bytes: bytes,
        mime_type: str,
        version: str = "",
        title: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        """POST /api/admin/tutorials/{id}/update — publish a new version.

        Uploads a replacement file; the server creates a new version in
        the same scope + media type and archives the old one (kept as
        history). Scope and media type are inherited from the source, so
        they aren't sent here.
        """
        data: dict[str, str] = {
            "version": version,
            "title": title,
            "description": description,
        }
        files = {"file": (filename, file_bytes, mime_type)}
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/tutorials/{tutorial_id}/update",
                headers=self._headers(),
                data=data,
                files=files,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def reset_runtime_setting(self, key: str) -> dict[str, Any]:
        """POST /api/admin/runtime-settings/{key}/reset."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/runtime-settings/{key}/reset",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    # ----- admin: general settings ---------------------------------------

    async def get_general_settings(self) -> dict[str, Any]:
        """GET /api/admin/general-settings → ``{catalog: [...], values: {...}}``."""
        async with await self._client() as c:
            response = await c.get("/api/admin/general-settings", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def set_general_setting(self, key: str, *, value: Any) -> dict[str, Any]:
        """POST /api/admin/general-settings/{key}."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/general-settings/{key}",
                headers=self._headers(),
                json={"value": value},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def reset_general_setting(self, key: str) -> dict[str, Any]:
        """POST /api/admin/general-settings/{key}/reset."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/general-settings/{key}/reset",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    async def upload_general_setting_image(
        self,
        key: str,
        *,
        filename: str,
        data: bytes,
        mime_type: str,
    ) -> dict[str, Any]:
        """PUT /api/admin/general-settings/{key}/image — multipart image upload."""
        async with await self._client() as c:
            response = await c.put(
                f"/api/admin/general-settings/{key}/image",
                headers=self._headers(),
                files={"file": (filename, data, mime_type)},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def delegate_general_setting(
        self,
        key: str,
        *,
        delegated_to_role: str | None,
    ) -> dict[str, Any]:
        """POST /api/admin/general-settings/{key}/delegate — super-user only.

        Pass ``delegated_to_role=None`` to revoke; otherwise the role
        string must match the catalog spec's ``delegatable_to`` —
        the server rejects mismatches.
        """
        async with await self._client() as c:
            response = await c.post(
                f"/api/admin/general-settings/{key}/delegate",
                headers=self._headers(),
                json={"delegated_to_role": delegated_to_role},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def preview_inactive_user_sweep(
        self,
        *,
        threshold_days: int,
        limit: int = 100,
    ) -> dict[str, Any]:
        """GET /api/admin/inactive-users/preview?threshold_days=N — dry-run.

        Returns ``{threshold_days, count, users: [...]}`` describing the
        users the next sweep would auto-disable at this threshold.
        Doesn't disable anyone; safe to call freely.
        """
        async with await self._client() as c:
            response = await c.get(
                "/api/admin/inactive-users/preview",
                headers=self._headers(),
                params={"threshold_days": threshold_days, "limit": limit},
            )
            _raise_for_status(response)
            return dict(response.json())

    # ----- admin: send notifications (broadcast) -------------------------

    async def get_notification_template(self) -> dict[str, Any]:
        """GET /api/admin/notifications/template."""
        async with await self._client() as c:
            response = await c.get("/api/admin/notifications/template", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def set_notification_template(
        self, *, header_text: str, footer_text: str
    ) -> dict[str, Any]:
        """POST /api/admin/notifications/template — save header/footer text."""
        async with await self._client() as c:
            response = await c.post(
                "/api/admin/notifications/template",
                headers=self._headers(),
                json={"header_text": header_text, "footer_text": footer_text},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def upload_notification_template_image(
        self,
        slot: str,
        *,
        filename: str,
        data: bytes,
        mime_type: str,
    ) -> dict[str, Any]:
        """PUT /api/admin/notifications/template/{slot}/image — multipart upload."""
        async with await self._client() as c:
            response = await c.put(
                f"/api/admin/notifications/template/{slot}/image",
                headers=self._headers(),
                files={"file": (filename, data, mime_type)},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def clear_notification_template_image(self, slot: str) -> dict[str, Any]:
        """DELETE /api/admin/notifications/template/{slot}/image."""
        async with await self._client() as c:
            response = await c.delete(
                f"/api/admin/notifications/template/{slot}/image",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    async def get_notification_template_image(self, slot: str) -> bytes | None:
        """GET the saved template image bytes, or ``None`` when unset.

        Used by the compose page to render a live preview; the image
        route is admin-only so an ``<img src>`` can't fetch it
        directly (no bearer header) — we pull the bytes here and the
        page renders them as a data URI.
        """
        async with await self._client() as c:
            response = await c.get(
                f"/api/admin/notifications/template/{slot}/image",
                headers=self._headers(),
            )
            if response.status_code == _HTTP_NOT_FOUND:
                return None
            _raise_for_status(response)
            return response.content

    async def get_broadcast_audience_options(self) -> dict[str, Any]:
        """GET /api/admin/notifications/audience-options — teams/roles/curators."""
        async with await self._client() as c:
            response = await c.get(
                "/api/admin/notifications/audience-options",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    async def send_broadcast(
        self,
        *,
        audience: str,
        subject: str,
        body: str,
        attachments: list[tuple[str, bytes, str]],
        group_kind: str = "",
        selection: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /api/admin/notifications/broadcast — multipart fan-out.

        ``attachments`` is a list of ``(filename, data, content_type)``
        tuples, all sent under the repeated ``files`` form field.
        ``audience`` is the base scope; ``group_kind`` + ``selection``
        optionally narrow it to chosen team / role ids or curator
        category keys (``selection`` sent as a repeated form field).
        """
        files = [("files", (name, data, ctype)) for name, data, ctype in attachments]
        # httpx form-encodes a list value as repeated fields
        # (selection=a&selection=b), which is what the endpoint reads.
        form: dict[str, Any] = {"audience": audience, "subject": subject, "body": body}
        if group_kind:
            form["group_kind"] = group_kind
        if selection:
            form["selection"] = [str(s) for s in selection]
        async with await self._client() as c:
            response = await c.post(
                "/api/admin/notifications/broadcast",
                headers=self._headers(),
                data=form,
                files=files or None,
            )
            _raise_for_status(response)
            return dict(response.json())

    # ----- admin: cross-install migrate ----------------------------------

    async def get_install_fingerprint(self) -> dict[str, Any]:
        """GET /api/admin/migrate/this-install-fingerprint."""
        async with await self._client() as c:
            response = await c.get(
                "/api/admin/migrate/this-install-fingerprint",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    async def list_trusted_peers(self) -> list[dict[str, Any]]:
        """GET /api/admin/migrate/trusted-peers."""
        async with await self._client() as c:
            response = await c.get(
                "/api/admin/migrate/trusted-peers",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return list(response.json())

    async def add_trusted_peer(
        self, *, fingerprint: str, label: str, notes: str = ""
    ) -> dict[str, Any]:
        """POST /api/admin/migrate/trusted-peers."""
        async with await self._client() as c:
            response = await c.post(
                "/api/admin/migrate/trusted-peers",
                headers=self._headers(),
                json={"fingerprint": fingerprint, "label": label, "notes": notes},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def remove_trusted_peer(self, *, fingerprint: str, reason: str) -> None:
        """DELETE /api/admin/migrate/trusted-peers/{fingerprint}."""
        async with await self._client() as c:
            response = await c.request(
                "DELETE",
                f"/api/admin/migrate/trusted-peers/{fingerprint}",
                headers=self._headers(),
                json={"reason": reason},
            )
            _raise_for_status(response)

    async def export_migration_bundle(
        self,
        *,
        tool_specs: list[dict[str, str]],
        workflow_aliases: list[str],
        notes: str = "",
    ) -> tuple[bytes, str]:
        """POST /api/admin/migrate/export — returns ``(bytes, suggested_filename)``.

        The server's ``Content-Disposition`` header carries a sensible
        default filename (``matofx-migrate-<timestamp>-<short-fp>.tar.gz``)
        that the UI surfaces to the browser's save dialog.
        """
        async with await self._client() as c:
            response = await c.post(
                "/api/admin/migrate/export",
                headers=self._headers(),
                json={
                    "tool_specs": tool_specs,
                    "workflow_aliases": workflow_aliases,
                    "notes": notes,
                },
            )
            _raise_for_status(response)
            filename = _extract_filename(response.headers.get("content-disposition", ""))
            return response.content, filename

    async def import_migration_bundle(
        self,
        *,
        filename: str,
        file_bytes: bytes,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """POST /api/admin/migrate/import — multipart upload + dry_run flag.

        Returns the JSON envelope ``{"result": {...}, "events": [...]}``.
        """
        params = {"dry_run": "true"} if dry_run else {}
        files = {"file": (filename, file_bytes, "application/gzip")}
        async with await self._client() as c:
            response = await c.post(
                "/api/admin/migrate/import",
                headers=self._headers(),
                params=params,
                files=files,
            )
            _raise_for_status(response)
            return dict(response.json())

    # --- home dashboard -----------------------------------------------

    async def home_summary(self) -> dict[str, Any]:
        """GET /api/home/summary → the header greeting's live counts.

        Keys: ``active_jobs``, ``running_optimisations``,
        ``running_parametric_studies``, ``pending_approvals``,
        ``can_approve`` — each ACL-filtered to the caller (home §8.6).
        """
        async with await self._client() as c:
            response = await c.get("/api/home/summary", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def search(self, *, query: str, scope: str = "all") -> list[dict[str, Any]]:
        """GET /api/search → global quick-find hits (home §6).

        Each hit is ``{type, type_label, label, sublabel, status,
        route}`` — already ACL / feature / role gated and display-ready
        (labels never aliases). ``scope`` is ``all`` or one entity type.
        """
        async with await self._client() as c:
            response = await c.get(
                "/api/search",
                headers=self._headers(),
                params={"q": query, "scope": scope},
            )
            _raise_for_status(response)
            return [dict(row) for row in response.json()]

    # --- job approvals (§16.15) ---------------------------------------

    async def approvals_summary(self) -> dict[str, Any]:
        """GET /api/users/me/approvals/summary → {can_approve, pending}."""
        async with await self._client() as c:
            response = await c.get("/api/users/me/approvals/summary", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def list_my_approvals(self) -> list[dict[str, Any]]:
        """GET /api/users/me/approvals → pending requests for the caller."""
        async with await self._client() as c:
            response = await c.get("/api/users/me/approvals", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def get_approval_detail(self, request_id: int) -> dict[str, Any]:
        """GET /api/approvals/{id} → the rich review payload."""
        async with await self._client() as c:
            response = await c.get(f"/api/approvals/{request_id}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def submit_approval_decisions(self, decisions: list[dict[str, Any]]) -> dict[str, Any]:
        """POST /api/approvals/decisions → per-item batch results."""
        async with await self._client() as c:
            response = await c.post(
                "/api/approvals/decisions",
                headers=self._headers(),
                json={"decisions": decisions},
            )
            _raise_for_status(response)
            return dict(response.json())

    # ----- parametric studies (ADR-0001 / §G39a) ------------------------

    async def create_study(
        self,
        project_id: int,
        *,
        name: str,
        tool_alias: str,
        mode: str,
        description: str = "",
        factor_spec: dict[str, Any] | None = None,
        sampling_params: dict[str, Any] | None = None,
        seed: int | None = None,
        run_cap: int | None = None,
    ) -> dict[str, Any]:
        """POST /api/projects/{id}/studies → StudyResponse (draft)."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/projects/{project_id}/studies",
                headers=self._headers(),
                json={
                    "name": name,
                    "tool_alias": tool_alias,
                    "mode": mode,
                    "description": description,
                    "factor_spec": factor_spec or {},
                    "sampling_params": sampling_params or {},
                    "seed": seed,
                    "run_cap": run_cap,
                },
            )
            _raise_for_status(response)
            return dict(response.json())

    async def list_studies(self, project_id: int) -> list[dict[str, Any]]:
        """GET /api/projects/{id}/studies → [StudyResponse], newest first."""
        async with await self._client() as c:
            response = await c.get(f"/api/projects/{project_id}/studies", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def get_study(self, study_id: int) -> dict[str, Any]:
        """GET /api/studies/{id} → StudyDetailResponse {study, children}."""
        async with await self._client() as c:
            response = await c.get(f"/api/studies/{study_id}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def submit_study(self, study_id: int) -> dict[str, Any]:
        """POST /api/studies/{id}/submit → StudyResponse."""
        async with await self._client() as c:
            response = await c.post(f"/api/studies/{study_id}/submit", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def cancel_study(self, study_id: int) -> dict[str, Any]:
        """POST /api/studies/{id}/cancel → StudyResponse."""
        async with await self._client() as c:
            response = await c.post(f"/api/studies/{study_id}/cancel", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def retry_study_child(self, study_id: int, job_id: int) -> dict[str, Any]:
        """POST /api/studies/{id}/children/{job_id}/retry → StudyDetailResponse."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/studies/{study_id}/children/{job_id}/retry",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    async def get_study_results(self, study_id: int) -> dict[str, Any]:
        """GET /api/studies/{id}/results → StudyResultsResponse.

        The (children x inputs x scalars) matrix plus response-source
        metadata (named scalars + ``csv_plot`` files) for the 2-PS-b0
        Results tab + response picker.
        """
        async with await self._client() as c:
            response = await c.get(f"/api/studies/{study_id}/results", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def download_study_results_csv(self, study_id: int) -> bytes:
        """GET /api/studies/{id}/results.csv → CSV bytes for download."""
        async with await self._client() as c:
            response = await c.get(f"/api/studies/{study_id}/results.csv", headers=self._headers())
            _raise_for_status(response)
            return bytes(response.content)

    # --- job cohorts (ADR-0008 §3) ---------------------------------------

    async def list_cohorts(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        """GET /api/cohorts → [CohortResponse] (workspace-wide)."""
        async with await self._client() as c:
            response = await c.get(
                "/api/cohorts",
                headers=self._headers(),
                params={"include_archived": str(include_archived).lower()},
            )
            _raise_for_status(response)
            return list(response.json())

    async def preview_cohort_candidates(
        self,
        *,
        tool_alias: str,
        tolerated_tool_version_ids: list[int] | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        """POST /api/cohorts/preview → CohortPreviewResponse.

        ``filters`` values must be JSON-serialisable (dates as ISO
        strings). Returns ``{rows: [...], acl_hidden: int}``.
        """
        payload: dict[str, Any] = {
            "tool_alias": tool_alias,
            "tolerated_tool_version_ids": tolerated_tool_version_ids or [],
        }
        payload.update({k: v for k, v in filters.items() if v is not None})
        async with await self._client() as c:
            response = await c.post("/api/cohorts/preview", headers=self._headers(), json=payload)
            _raise_for_status(response)
            return dict(response.json())

    async def create_cohort(
        self,
        *,
        name: str,
        tool_alias: str,
        tolerated_tool_version_ids: list[int] | None = None,
        description: str = "",
        auto_grow: bool = True,
        member_job_ids: list[int] | None = None,
        excluded_job_ids: list[int] | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        """POST /api/cohorts → CohortResponse (active, workspace-level).

        An empty / omitted ``tolerated_tool_version_ids`` anchors the
        cohort on the tool's latest version server-side (§3.2).
        ``auto_grow=False`` freezes the ``member_job_ids`` set; otherwise
        membership stays live. Extra ``filters`` must be JSON-serialisable.
        """
        payload: dict[str, Any] = {
            "name": name,
            "tool_alias": tool_alias,
            "tolerated_tool_version_ids": tolerated_tool_version_ids or [],
            "description": description,
            "auto_grow": auto_grow,
        }
        if member_job_ids is not None:
            payload["member_job_ids"] = member_job_ids
        if excluded_job_ids is not None:
            payload["excluded_job_ids"] = excluded_job_ids
        payload.update({k: v for k, v in filters.items() if v is not None})
        async with await self._client() as c:
            response = await c.post("/api/cohorts", headers=self._headers(), json=payload)
            _raise_for_status(response)
            return dict(response.json())

    async def get_cohort_build_meta(self, tool_alias: str) -> dict[str, Any]:
        """GET /api/cohorts/tools/{alias}/build-meta → version bands + numeric inputs."""
        async with await self._client() as c:
            response = await c.get(
                f"/api/cohorts/tools/{tool_alias}/build-meta", headers=self._headers()
            )
            _raise_for_status(response)
            return dict(response.json())

    async def update_cohort(self, cohort_id: int, **fields: Any) -> dict[str, Any]:
        """PATCH /api/cohorts/{id} -> CohortResponse. Sends exactly the given fields.

        **``None`` is sent, not dropped.** A PATCH distinguishes "leave this
        alone" (key absent) from "clear this" (key present, ``null``), and
        dropping nulls here collapsed the two: a filter, once set, could never
        be removed -- the edit form emptied its boxes, this dropped the keys,
        and the server left the old values in place. The caller decides which
        keys to send; this only transports them.
        """
        payload = dict(fields)
        async with await self._client() as c:
            response = await c.patch(
                f"/api/cohorts/{cohort_id}", headers=self._headers(), json=payload
            )
            _raise_for_status(response)
            return dict(response.json())

    async def cohort_coverage(
        self, cohort_id: int, *, envelope: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """POST /api/cohorts/{id}/coverage → CoverageReportResponse (ADR-0020).

        ``envelope`` carries the user's edits to the declared range; omitted,
        the engine starts from the training set's observed spread.
        """
        async with await self._client() as c:
            response = await c.post(
                f"/api/cohorts/{cohort_id}/coverage",
                headers=self._headers(),
                json={"envelope": envelope or []},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def cohort_coverage_plan(
        self,
        cohort_id: int,
        *,
        envelope: list[dict[str, Any]] | None = None,
        measure_envelope: list[dict[str, Any]] | None = None,
        sizes: list[int] | None = None,
    ) -> dict[str, Any]:
        """POST /api/cohorts/{id}/coverage/plan -> CoveragePlanResponse (§11.5).

        What each candidate batch size would buy, so the user can pick a size
        off the curve instead of guessing one.

        ``envelope`` is the space the batch may move in; ``measure_envelope``,
        when given, is the space the figures describe.
        """
        body: dict[str, Any] = {"envelope": envelope or []}
        if measure_envelope is not None:
            body["measure_envelope"] = measure_envelope
        if sizes:
            body["sizes"] = sizes
        async with await self._client() as c:
            response = await c.post(
                f"/api/cohorts/{cohort_id}/coverage/plan",
                headers=self._headers(),
                json=body,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def cohort_coverage_suggest(
        self, cohort_id: int, *, n: int, envelope: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """POST /api/cohorts/{id}/coverage/suggest → CoverageSuggestionResponse."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/cohorts/{cohort_id}/coverage/suggest",
                headers=self._headers(),
                json={"n": n, "envelope": envelope or []},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def cohort_coverage_create_study(
        self,
        cohort_id: int,
        *,
        n: int,
        project_id: int,
        name: str,
        envelope: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """POST /api/cohorts/{id}/coverage/study → the draft StudyResponse."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/cohorts/{cohort_id}/coverage/study",
                headers=self._headers(),
                json={
                    "n": n,
                    "project_id": project_id,
                    "name": name,
                    "envelope": envelope or [],
                },
            )
            _raise_for_status(response)
            return dict(response.json())

    async def get_cohort(self, cohort_id: int) -> dict[str, Any]:
        """GET /api/cohorts/{id} → CohortResponse."""
        async with await self._client() as c:
            response = await c.get(f"/api/cohorts/{cohort_id}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def archive_cohort(self, cohort_id: int) -> dict[str, Any]:
        """POST /api/cohorts/{id}/archive → CohortResponse."""
        async with await self._client() as c:
            response = await c.post(f"/api/cohorts/{cohort_id}/archive", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def resolve_cohort(self, cohort_id: int) -> dict[str, Any]:
        """POST /api/cohorts/{id}/resolve → CohortMembersResponse."""
        async with await self._client() as c:
            response = await c.post(f"/api/cohorts/{cohort_id}/resolve", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def get_cohort_results(self, cohort_id: int) -> dict[str, Any]:
        """GET /api/cohorts/{id}/results → CohortResultsResponse (lazy populate)."""
        async with await self._client() as c:
            response = await c.get(f"/api/cohorts/{cohort_id}/results", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    # --- optimisation studies (ADR-0009 §15) ----------------------------

    async def list_optimisation_studies(
        self, project_id: int, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        """GET /api/projects/{id}/optimisation-studies → [OptimisationStudyResponse]."""
        async with await self._client() as c:
            response = await c.get(
                f"/api/projects/{project_id}/optimisation-studies",
                headers=self._headers(),
                params={"include_archived": str(include_archived).lower()},
            )
            _raise_for_status(response)
            return list(response.json())

    async def create_optimisation_study(
        self,
        project_id: int,
        *,
        name: str,
        tool_alias: str,
        tool_version_id: int,
        description: str = "",
    ) -> dict[str, Any]:
        """POST /api/projects/{id}/optimisation-studies → OptimisationStudyResponse (draft)."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/projects/{project_id}/optimisation-studies",
                headers=self._headers(),
                json={
                    "name": name,
                    "tool_alias": tool_alias,
                    "tool_version_id": tool_version_id,
                    "description": description,
                },
            )
            _raise_for_status(response)
            return dict(response.json())

    async def list_workflows(self) -> list[dict[str, Any]]:
        """GET /api/workflows → workflows visible to the caller (alias/name/tool_aliases)."""
        async with await self._client() as c:
            response = await c.get("/api/workflows", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def create_optimisation_workflow_study(
        self,
        project_id: int,
        *,
        name: str,
        workflow_alias: str,
        workflow_version_id: int | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        """POST /api/projects/{id}/optimisation-studies/workflow → draft (workflow target)."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/projects/{project_id}/optimisation-studies/workflow",
                headers=self._headers(),
                json={
                    "name": name,
                    "workflow_alias": workflow_alias,
                    "workflow_version_id": workflow_version_id,
                    "description": description,
                },
            )
            _raise_for_status(response)
            return dict(response.json())

    async def get_optimisation_study(self, study_id: int) -> dict[str, Any]:
        """GET /api/optimisation-studies/{id} → OptimisationStudyResponse."""
        async with await self._client() as c:
            response = await c.get(f"/api/optimisation-studies/{study_id}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def update_optimisation_study(
        self, study_id: int, config_patch: dict[str, Any]
    ) -> dict[str, Any]:
        """PATCH /api/optimisation-studies/{id} → OptimisationStudyResponse (draft merge)."""
        async with await self._client() as c:
            response = await c.patch(
                f"/api/optimisation-studies/{study_id}",
                headers=self._headers(),
                json={"config_patch": config_patch},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def configure_optimisation_study(
        self, study_id: int, *, start_approver_user_id: int | None = None
    ) -> dict[str, Any]:
        """POST /api/optimisation-studies/{id}/configure → OptimisationStudyResponse."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/optimisation-studies/{study_id}/configure",
                headers=self._headers(),
                json={"start_approver_user_id": start_approver_user_id},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def list_optimisation_approvers(self) -> list[dict[str, Any]]:
        """GET /api/optimisation-approvers → eligible start-gate approvers (§14.1)."""
        async with await self._client() as c:
            response = await c.get("/api/optimisation-approvers", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def get_optimisation_cost_estimate(self, study_id: int) -> dict[str, Any]:
        """GET /api/optimisation-studies/{id}/cost-estimate → campaign footprint (§14.1)."""
        async with await self._client() as c:
            response = await c.get(
                f"/api/optimisation-studies/{study_id}/cost-estimate", headers=self._headers()
            )
            _raise_for_status(response)
            return dict(response.json())

    async def approve_optimisation_start(
        self, study_id: int, *, reason: str | None = None
    ) -> dict[str, Any]:
        """POST /api/optimisation-studies/{id}/start-approve → OptimisationStudyResponse."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/optimisation-studies/{study_id}/start-approve",
                headers=self._headers(),
                json={"reason": reason},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def reject_optimisation_start(
        self, study_id: int, *, reason: str | None = None
    ) -> dict[str, Any]:
        """POST /api/optimisation-studies/{id}/start-reject → OptimisationStudyResponse."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/optimisation-studies/{study_id}/start-reject",
                headers=self._headers(),
                json={"reason": reason},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def halt_optimisation_study(self, study_id: int) -> dict[str, Any]:
        """POST /api/optimisation-studies/{id}/halt → OptimisationStudyResponse."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/optimisation-studies/{study_id}/halt", headers=self._headers()
            )
            _raise_for_status(response)
            return dict(response.json())

    async def continue_optimisation_study(
        self, study_id: int, *, additional_runs: int
    ) -> dict[str, Any]:
        """POST /api/optimisation-studies/{id}/continue → OptimisationStudyResponse."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/optimisation-studies/{study_id}/continue",
                json={"additional_runs": additional_runs},
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    async def archive_optimisation_study(self, study_id: int) -> dict[str, Any]:
        """POST /api/optimisation-studies/{id}/archive → OptimisationStudyResponse."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/optimisation-studies/{study_id}/archive", headers=self._headers()
            )
            _raise_for_status(response)
            return dict(response.json())

    async def get_optimisation_convergence(self, study_id: int) -> dict[str, Any]:
        """GET /api/optimisation-studies/{id}/convergence → OptimisationConvergenceResponse."""
        async with await self._client() as c:
            response = await c.get(
                f"/api/optimisation-studies/{study_id}/convergence", headers=self._headers()
            )
            _raise_for_status(response)
            return dict(response.json())

    async def get_optimisation_run_results(self, study_id: int) -> dict[str, Any]:
        """GET /api/optimisation-studies/{id}/run-results → per-run results matrix."""
        async with await self._client() as c:
            response = await c.get(
                f"/api/optimisation-studies/{study_id}/run-results", headers=self._headers()
            )
            _raise_for_status(response)
            return dict(response.json())

    async def get_optimisation_recommendation(self, study_id: int) -> dict[str, Any]:
        """GET /api/optimisation-studies/{id}/recommendation → the Navigator's pick."""
        async with await self._client() as c:
            response = await c.get(
                f"/api/optimisation-studies/{study_id}/recommendation", headers=self._headers()
            )
            _raise_for_status(response)
            return dict(response.json())

    async def compute_optimisation_mop(
        self, study_id: int, *, cutoff: float | None = None
    ) -> dict[str, Any]:
        """POST /api/optimisation-studies/{id}/mop → the CoP analysis + frozen-factor set."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/optimisation-studies/{study_id}/mop",
                headers=self._headers(),
                json={"cutoff": cutoff},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def set_optimisation_mop_frozen_factors(
        self, study_id: int, frozen_factors: list[str]
    ) -> dict[str, Any]:
        """POST /api/optimisation-studies/{id}/mop/frozen-factors → updated MOP state."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/optimisation-studies/{study_id}/mop/frozen-factors",
                headers=self._headers(),
                json={"frozen_factors": list(frozen_factors)},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def publish_optimisation_surrogate(
        self,
        study_id: int,
        *,
        name: str | None = None,
        scope: str = "personal",
        surrogate_kind: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/optimisation-studies/{id}/publish-surrogate → version summary."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/optimisation-studies/{study_id}/publish-surrogate",
                headers=self._headers(),
                json={"name": name, "scope": scope, "surrogate_kind": surrogate_kind},
            )
            _raise_for_status(response)
            return dict(response.json())

    # ----- surrogate models (ADR-0008 §11.2) ----------------------------

    async def list_models(
        self,
        *,
        tool: str | None = None,
        scope: str | None = None,
        include_retired: bool = False,
    ) -> list[dict[str, Any]]:
        """GET /api/models → [ModelResponse] (catalogue; in-progress included)."""
        params: dict[str, str] = {"include_retired": str(include_retired).lower()}
        if tool:
            params["tool"] = tool
        if scope:
            params["scope"] = scope
        async with await self._client() as c:
            response = await c.get("/api/models", headers=self._headers(), params=params)
            _raise_for_status(response)
            return list(response.json())

    async def model_approvers(self) -> list[dict[str, Any]]:
        """GET /api/models/approvers → nominatable Model Curators (excludes self)."""
        async with await self._client() as c:
            response = await c.get("/api/models/approvers", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def compare_model_fits(
        self,
        *,
        tool_alias: str,
        source_kind: str,
        source_id: int,
        response_names: list[str],
        methods: list[str],
        field_response_names: list[str] | None = None,
        field_methods: list[str] | None = None,
        output_filter: dict[str, dict[str, float | None]] | None = None,
        project_id: int | None = None,
    ) -> dict[str, Any]:
        """POST /api/models/compare-fits → ModelCompareResponse (no persistence)."""
        body: dict[str, Any] = {
            "tool_alias": tool_alias,
            "source_kind": source_kind,
            "source_id": source_id,
            "response_names": response_names,
            "methods": methods,
            "field_response_names": field_response_names or [],
            "field_methods": field_methods or [],
            "output_filter": output_filter or {},
        }
        if project_id is not None:
            body["project_id"] = project_id
        async with await self._client() as c:
            response = await c.post("/api/models/compare-fits", headers=self._headers(), json=body)
            _raise_for_status(response)
            return dict(response.json())

    async def model_version_coverage(self, version_id: int) -> dict[str, Any]:
        """GET /api/models/versions/{id}/coverage → ModelCoverageResponse (gauge data)."""
        async with await self._client() as c:
            response = await c.get(
                f"/api/models/versions/{version_id}/coverage", headers=self._headers()
            )
            _raise_for_status(response)
            return dict(response.json())

    async def get_model(self, model_id: int) -> dict[str, Any]:
        """GET /api/models/{id} → ModelDetailResponse {model, versions}."""
        async with await self._client() as c:
            response = await c.get(f"/api/models/{model_id}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def create_model(
        self,
        *,
        name: str,
        tool_alias: str,
        scope: str,
        source_kind: str,
        source_id: int,
        response_names: list[str],
        project_id: int | None = None,
        surrogate_kind: str | None = None,
        description: str = "",
        tags: list[str] | None = None,
        keep_training_data: bool = False,
        field_auto_select: bool = False,
        field_surrogate_kind: str | None = None,
        output_filter: dict[str, dict[str, float | None]] | None = None,
    ) -> dict[str, Any]:
        """POST /api/models → ModelResponse (fits + persists a new model)."""
        body: dict[str, Any] = {
            "name": name,
            "tool_alias": tool_alias,
            "scope": scope,
            "source_kind": source_kind,
            "source_id": source_id,
            "response_names": response_names,
            "description": description,
            "tags": tags or [],
            "keep_training_data": keep_training_data,
            "field_auto_select": field_auto_select,
            "output_filter": output_filter or {},
        }
        if project_id is not None:
            body["project_id"] = project_id
        if surrogate_kind is not None:
            body["surrogate_kind"] = surrogate_kind
        if field_surrogate_kind is not None:
            body["field_surrogate_kind"] = field_surrogate_kind
        async with await self._client() as c:
            response = await c.post("/api/models", headers=self._headers(), json=body)
            _raise_for_status(response)
            return dict(response.json())

    async def update_model(
        self,
        model_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """PATCH /api/models/{id} → ModelResponse (mutable metadata)."""
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if tags is not None:
            body["tags"] = tags
        async with await self._client() as c:
            response = await c.patch(f"/api/models/{model_id}", headers=self._headers(), json=body)
            _raise_for_status(response)
            return dict(response.json())

    async def field_predict(
        self,
        model_id: int,
        version_id: int,
        point: dict[str, float | str],
        response_name: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/models/{id}/versions/{vid}/field-predict → FieldPredictResponse.

        Drives the predicted-field viewer: ``point`` is the ``{factor: value}``
        (design factors plus ``time`` for a time-resolved surrogate); the
        response carries the reconstructed nodal field + per-node 1-sigma.
        ``response_name`` picks which field of a combined model (ADR-0018).
        A value may be a **string** — a varied categorical is named by the
        level it takes, not by a number.
        """
        body: dict[str, Any] = {"point": point}
        if response_name is not None:
            body["response_name"] = response_name
        async with await self._client() as c:
            response = await c.post(
                f"/api/models/{model_id}/versions/{version_id}/field-predict",
                headers=self._headers(),
                json=body,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def field_geometry(
        self, model_id: int, version_id: int, response_name: str | None = None
    ) -> dict[str, Any]:
        """GET /api/models/{id}/versions/{vid}/field-geometry → FieldGeometryResponse.

        The reference-mesh surface (points + triangles) the predicted-field
        viewer colours; ``has_geometry=False`` for a ``.npy`` field.
        ``response_name`` picks which field of a combined model (ADR-0018).
        """
        params = {"response_name": response_name} if response_name is not None else None
        async with await self._client() as c:
            response = await c.get(
                f"/api/models/{model_id}/versions/{version_id}/field-geometry",
                headers=self._headers(),
                params=params,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def refit_model(self, model_id: int) -> dict[str, Any]:
        """POST /api/models/{id}/refit → ModelVersionResponse (new ordinal)."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/models/{model_id}/refit", headers=self._headers(), json={}
            )
            _raise_for_status(response)
            return dict(response.json())

    async def set_model_scope(
        self, model_id: int, *, scope: str, project_id: int | None = None
    ) -> dict[str, Any]:
        """POST /api/models/{id}/scope → ModelResponse (tier move; global gated)."""
        body: dict[str, Any] = {"new_scope": scope}
        if project_id is not None:
            body["project_id"] = project_id
        async with await self._client() as c:
            response = await c.post(
                f"/api/models/{model_id}/scope", headers=self._headers(), json=body
            )
            _raise_for_status(response)
            return dict(response.json())

    async def retire_model(self, model_id: int) -> dict[str, Any]:
        """POST /api/models/{id}/retire → ModelResponse (no-delete withdrawal)."""
        async with await self._client() as c:
            response = await c.post(f"/api/models/{model_id}/retire", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def submit_model_review(
        self, model_id: int, version_id: int, *, checker_user_id: int | None = None
    ) -> dict[str, Any]:
        """POST .../versions/{vid}/submit-review → ModelVersionResponse (gate 1)."""
        body: dict[str, Any] = {}
        if checker_user_id is not None:
            body["checker_user_id"] = checker_user_id
        async with await self._client() as c:
            response = await c.post(
                f"/api/models/{model_id}/versions/{version_id}/submit-review",
                headers=self._headers(),
                json=body,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def release_model_version(self, model_id: int, version_id: int) -> dict[str, Any]:
        """POST .../versions/{vid}/release → ModelVersionResponse (gate 2)."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/models/{model_id}/versions/{version_id}/release",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    async def export_model_as_tool(self, model_id: int) -> dict[str, Any]:
        """POST /api/models/{id}/export-as-tool → ModelExportResponse."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/models/{model_id}/export-as-tool", headers=self._headers(), json={}
            )
            _raise_for_status(response)
            return dict(response.json())

    async def get_study_csv_series(
        self,
        study_id: int,
        *,
        output: str,
        x_column: int = 0,
        y_column: int = 1,
        max_points: int = 500,
    ) -> dict[str, Any]:
        """GET /api/studies/{id}/csv-series → per-child (x,y) series (2-PS-b)."""
        async with await self._client() as c:
            response = await c.get(
                f"/api/studies/{study_id}/csv-series",
                headers=self._headers(),
                params={
                    "output": output,
                    "x_column": x_column,
                    "y_column": y_column,
                    "max_points": max_points,
                },
            )
            _raise_for_status(response)
            return dict(response.json())

    async def get_study_csv_aggregate(
        self,
        study_id: int,
        *,
        output: str,
        column: int,
        aggregate: str = "last",
    ) -> dict[str, Any]:
        """GET /api/studies/{id}/csv-aggregate → per-child computed scalar (2-PS-b)."""
        async with await self._client() as c:
            response = await c.get(
                f"/api/studies/{study_id}/csv-aggregate",
                headers=self._headers(),
                params={"output": output, "column": column, "aggregate": aggregate},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def get_study_sensitivity(
        self,
        study_id: int,
        *,
        response: str,
        source: str = "scalar",
        column: int = 0,
        aggregate: str = "last",
    ) -> dict[str, Any]:
        """GET /api/studies/{id}/sensitivity → factor ranking for a response (2-PS-b5)."""
        async with await self._client() as c:
            resp = await c.get(
                f"/api/studies/{study_id}/sensitivity",
                headers=self._headers(),
                params={
                    "response": response,
                    "source": source,
                    "column": column,
                    "aggregate": aggregate,
                },
            )
            _raise_for_status(resp)
            return dict(resp.json())

    async def get_study_sensitivity_matrix(
        self, study_id: int, *, responses: list[str]
    ) -> dict[str, Any]:
        """GET /api/studies/{id}/sensitivity-matrix -> per-output SRC grid (2-PS-b')."""
        async with await self._client() as c:
            resp = await c.get(
                f"/api/studies/{study_id}/sensitivity-matrix",
                headers=self._headers(),
                params={"responses": responses},
            )
            _raise_for_status(resp)
            return dict(resp.json())

    async def get_study_anova(self, study_id: int, *, response: str) -> dict[str, Any]:
        """GET /api/studies/{id}/anova -> ANOVA % contribution + main effects (2-PS-c-stats)."""
        async with await self._client() as c:
            resp = await c.get(
                f"/api/studies/{study_id}/anova",
                headers=self._headers(),
                params={"response": response},
            )
            _raise_for_status(resp)
            return dict(resp.json())

    async def get_study_rsm(
        self, study_id: int, *, response: str, x: str = "", y: str = ""
    ) -> dict[str, Any]:
        """GET /api/studies/{id}/rsm -> quadratic response surface + contour (2-PS-c-stats)."""
        async with await self._client() as c:
            resp = await c.get(
                f"/api/studies/{study_id}/rsm",
                headers=self._headers(),
                params={"response": response, "x": x, "y": y},
            )
            _raise_for_status(resp)
            return dict(resp.json())

    async def study_available_modes(self, tool_alias: str) -> dict[str, str | None]:
        """GET /api/studies/available-modes/{alias} → {mode: None|reason}."""
        from urllib.parse import quote  # noqa: PLC0415

        async with await self._client() as c:
            response = await c.get(
                f"/api/studies/available-modes/{quote(tool_alias, safe='')}",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json().get("modes") or {})

    async def preview_factor_spec(
        self,
        *,
        tool_alias: str,
        mode: str,
        factor_spec: dict[str, Any] | None = None,
        sampling_params: dict[str, Any] | None = None,
        seed: int | None = None,
        run_cap: int | None = None,
    ) -> dict[str, Any]:
        """POST /api/studies/preview → FactorSpecPreviewResponse."""
        async with await self._client() as c:
            response = await c.post(
                "/api/studies/preview",
                headers=self._headers(),
                json={
                    "tool_alias": tool_alias,
                    "mode": mode,
                    "factor_spec": factor_spec or {},
                    "sampling_params": sampling_params or {},
                    "seed": seed,
                    "run_cap": run_cap,
                },
            )
            _raise_for_status(response)
            return dict(response.json())

    # ----- file library (ADR-0006 §7) -----------------------------------

    async def list_library_files(
        self,
        *,
        scope: str | None = None,
        tool: str | None = None,
        include_retired: bool = False,
    ) -> list[dict[str, Any]]:
        """GET /api/library/files → [LibraryFileResponse] the caller can see."""
        params: dict[str, Any] = {"include_retired": include_retired}
        if scope:
            params["scope"] = scope
        if tool:
            params["tool"] = tool
        async with await self._client() as c:
            response = await c.get("/api/library/files", headers=self._headers(), params=params)
            _raise_for_status(response)
            return list(response.json())

    async def get_library_file(self, file_id: int) -> dict[str, Any]:
        """GET /api/library/files/{id} → {file, versions, tools}."""
        async with await self._client() as c:
            response = await c.get(f"/api/library/files/{file_id}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def create_library_file(
        self,
        *,
        metadata: dict[str, Any],
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """POST /api/library/files → multipart create (JSON metadata + file)."""
        async with await self._client() as c:
            response = await c.post(
                "/api/library/files",
                headers=self._headers(),
                data={"metadata": json.dumps(metadata)},
                files={"file": (filename, content, content_type)},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def add_library_version(
        self,
        file_id: int,
        *,
        metadata: dict[str, Any],
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """POST /api/library/files/{id}/versions → multipart add-version."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/library/files/{file_id}/versions",
                headers=self._headers(),
                data={"metadata": json.dumps(metadata)},
                files={"file": (filename, content, content_type)},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def update_library_metadata(
        self, file_id: int, *, body: dict[str, Any]
    ) -> dict[str, Any]:
        """PATCH /api/library/files/{id} → entry-metadata edit (optimistic)."""
        async with await self._client() as c:
            response = await c.patch(
                f"/api/library/files/{file_id}", headers=self._headers(), json=body
            )
            _raise_for_status(response)
            return dict(response.json())

    async def change_library_scope(
        self,
        file_id: int,
        *,
        new_scope: str,
        project_id: int | None = None,
        expected_version: int | None = None,
        approver_user_id: int | None = None,
    ) -> dict[str, Any]:
        """POST /api/library/files/{id}/scope → governed scope change."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/library/files/{file_id}/scope",
                headers=self._headers(),
                json={
                    "new_scope": new_scope,
                    "project_id": project_id,
                    "expected_version": expected_version,
                    "approver_user_id": approver_user_id,
                },
            )
            _raise_for_status(response)
            return dict(response.json())

    async def assign_library_tool(
        self, file_id: int, *, tool_alias: str, field_name: str | None = None
    ) -> dict[str, Any]:
        """POST /api/library/files/{id}/tools → add a tool assignment."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/library/files/{file_id}/tools",
                headers=self._headers(),
                json={"tool_alias": tool_alias, "field_name": field_name},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def unassign_library_tool(self, file_id: int, map_id: int) -> dict[str, Any]:
        """DELETE /api/library/files/{id}/tools/{mapId} → remove an assignment."""
        async with await self._client() as c:
            response = await c.delete(
                f"/api/library/files/{file_id}/tools/{map_id}", headers=self._headers()
            )
            _raise_for_status(response)
            return dict(response.json())

    async def retire_library_file(self, file_id: int) -> dict[str, Any]:
        """POST /api/library/files/{id}/retire → soft-delete (state only)."""
        async with await self._client() as c:
            response = await c.post(f"/api/library/files/{file_id}/retire", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def restore_library_file(self, file_id: int) -> dict[str, Any]:
        """POST /api/library/files/{id}/restore → reactivate a retired file."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/library/files/{file_id}/restore", headers=self._headers()
            )
            _raise_for_status(response)
            return dict(response.json())

    async def library_picker(
        self, *, tool: str, field: str, version: str | None = None
    ) -> list[dict[str, Any]]:
        """GET /api/library/picker → selectable versions for a tool's file field."""
        params: dict[str, Any] = {"tool": tool, "field": field}
        if version:
            params["version"] = version
        async with await self._client() as c:
            response = await c.get("/api/library/picker", headers=self._headers(), params=params)
            _raise_for_status(response)
            return list(response.json())

    async def library_approvers(self, *, scope: str = "global") -> list[dict[str, Any]]:
        """GET /api/library/approvers → nominatable curators (excludes self)."""
        async with await self._client() as c:
            response = await c.get(
                "/api/library/approvers", headers=self._headers(), params={"scope": scope}
            )
            _raise_for_status(response)
            return list(response.json())

    async def download_library_version(self, file_id: int, version_id: int) -> tuple[bytes, str]:
        """GET /api/library/files/{id}/versions/{vid}/download → (bytes, filename)."""
        async with await self._client() as c:
            response = await c.get(
                f"/api/library/files/{file_id}/versions/{version_id}/download",
                headers=self._headers(),
            )
            _raise_for_status(response)
            filename = _extract_filename(response.headers.get("content-disposition", ""))
            return bytes(response.content), filename or "download"

    # ----- materials database (ADR-0007 §6) -----------------------------

    async def list_material_classes(self, *, include_retired: bool = False) -> list[dict[str, Any]]:
        """GET /api/materials/classes → [MaterialClassResponse]."""
        async with await self._client() as c:
            response = await c.get(
                "/api/materials/classes",
                headers=self._headers(),
                params={"include_retired": include_retired},
            )
            _raise_for_status(response)
            return list(response.json())

    async def create_material_class(self, *, name: str, description: str = "") -> dict[str, Any]:
        """POST /api/materials/classes → MaterialClassResponse."""
        async with await self._client() as c:
            response = await c.post(
                "/api/materials/classes",
                headers=self._headers(),
                json={"name": name, "description": description},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def add_material_class_property(
        self,
        class_id: int,
        *,
        prop_name: str,
        unit: str,
        required: bool = True,
        slug: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/materials/classes/{id}/properties → MaterialClassResponse.

        ``slug`` is the immutable snake_case identifier; when ``None``
        the server derives it from ``prop_name``.
        """
        body: dict[str, Any] = {"prop_name": prop_name, "unit": unit, "required": required}
        if slug is not None:
            body["slug"] = slug
        async with await self._client() as c:
            response = await c.post(
                f"/api/materials/classes/{class_id}/properties",
                headers=self._headers(),
                json=body,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def retire_material_class(self, class_id: int) -> dict[str, Any]:
        """POST /api/materials/classes/{id}/retire → MaterialClassResponse."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/materials/classes/{class_id}/retire", headers=self._headers()
            )
            _raise_for_status(response)
            return dict(response.json())

    async def update_material_class_property(
        self, class_id: int, property_id: int, *, body: dict[str, Any]
    ) -> dict[str, Any]:
        """PATCH /api/materials/classes/{id}/properties/{pid} → MaterialClassResponse."""
        async with await self._client() as c:
            response = await c.patch(
                f"/api/materials/classes/{class_id}/properties/{property_id}",
                headers=self._headers(),
                json=body,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def remove_material_class_property(
        self, class_id: int, property_id: int
    ) -> dict[str, Any]:
        """DELETE /api/materials/classes/{id}/properties/{pid} → MaterialClassResponse."""
        async with await self._client() as c:
            response = await c.delete(
                f"/api/materials/classes/{class_id}/properties/{property_id}",
                headers=self._headers(),
            )
            _raise_for_status(response)
            return dict(response.json())

    async def list_materials(
        self,
        *,
        name: str | None = None,
        class_id: int | None = None,
        tag: str | None = None,
        include_retired: bool = False,
    ) -> list[dict[str, Any]]:
        """GET /api/materials → [MaterialResponse] (name / class / tag filters)."""
        params: dict[str, Any] = {"include_retired": include_retired}
        if name:
            params["name"] = name
        if class_id is not None:
            params["class_id"] = class_id
        if tag:
            params["tag"] = tag
        async with await self._client() as c:
            response = await c.get("/api/materials", headers=self._headers(), params=params)
            _raise_for_status(response)
            return list(response.json())

    async def create_material(self, *, body: dict[str, Any]) -> dict[str, Any]:
        """POST /api/materials → MaterialDetailResponse {material, versions}."""
        async with await self._client() as c:
            response = await c.post("/api/materials", headers=self._headers(), json=body)
            _raise_for_status(response)
            return dict(response.json())

    async def get_material(self, material_id: int) -> dict[str, Any]:
        """GET /api/materials/{id} → MaterialDetailResponse."""
        async with await self._client() as c:
            response = await c.get(f"/api/materials/{material_id}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def update_material(self, material_id: int, *, body: dict[str, Any]) -> dict[str, Any]:
        """PATCH /api/materials/{id} → MaterialDetailResponse (optimistic)."""
        async with await self._client() as c:
            response = await c.patch(
                f"/api/materials/{material_id}", headers=self._headers(), json=body
            )
            _raise_for_status(response)
            return dict(response.json())

    async def add_material_version(
        self, material_id: int, *, body: dict[str, Any]
    ) -> dict[str, Any]:
        """POST /api/materials/{id}/versions → MaterialDetailResponse."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/materials/{material_id}/versions", headers=self._headers(), json=body
            )
            _raise_for_status(response)
            return dict(response.json())

    async def retire_material(self, material_id: int) -> dict[str, Any]:
        """POST /api/materials/{id}/retire → MaterialResponse."""
        async with await self._client() as c:
            response = await c.post(f"/api/materials/{material_id}/retire", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def materials_picker(
        self, *, material_class: str, required_props: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """GET /api/materials/picker → [MaterialPickerCandidate] for a class."""
        params: dict[str, Any] = {"class": material_class}
        if required_props:
            params["required_props"] = ",".join(required_props)
        async with await self._client() as c:
            response = await c.get("/api/materials/picker", headers=self._headers(), params=params)
            _raise_for_status(response)
            return list(response.json())

    async def materials_approvers(self) -> list[dict[str, Any]]:
        """GET /api/materials/approvers → nominatable curators (excludes self)."""
        async with await self._client() as c:
            response = await c.get("/api/materials/approvers", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    # ----- V&V (ADR-0011) ----------------------------------------------

    async def vv_tool_status(self, tool_version_id: int) -> dict[str, Any]:
        """GET /api/vv/status/tool/{id} → inheritance-aware status badge."""
        async with await self._client() as c:
            response = await c.get(
                f"/api/vv/status/tool/{tool_version_id}", headers=self._headers()
            )
            _raise_for_status(response)
            return dict(response.json())

    async def vv_envelope_check(
        self, *, tool_version_id: int, form_values: dict[str, float]
    ) -> dict[str, Any] | None:
        """POST /api/vv/envelope-check → soft warning or ``None`` if inside envelope."""
        async with await self._client() as c:
            response = await c.post(
                "/api/vv/envelope-check",
                headers=self._headers(),
                json={"tool_version_id": tool_version_id, "form_values": form_values},
            )
            _raise_for_status(response)
            body = response.json()
            return dict(body) if body is not None else None

    async def vv_list_tool_records(
        self, *, tool_version_id: int, include_retired: bool = False
    ) -> list[dict[str, Any]]:
        """GET /api/vv/tool-records?tool_version_id=… → records for one tool version."""
        params: dict[str, Any] = {
            "tool_version_id": tool_version_id,
            "include_retired": include_retired,
        }
        async with await self._client() as c:
            response = await c.get("/api/vv/tool-records", headers=self._headers(), params=params)
            _raise_for_status(response)
            return list(response.json())

    async def vv_get_tool_record(self, record_id: int) -> dict[str, Any]:
        """GET /api/vv/tool-records/{id} → one tool V&V record."""
        async with await self._client() as c:
            response = await c.get(f"/api/vv/tool-records/{record_id}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def vv_list_job_records(
        self, *, job_id: int, include_retired: bool = False
    ) -> list[dict[str, Any]]:
        """GET /api/vv/job-records?job_id=… → records for one job."""
        params: dict[str, Any] = {"job_id": job_id, "include_retired": include_retired}
        async with await self._client() as c:
            response = await c.get("/api/vv/job-records", headers=self._headers(), params=params)
            _raise_for_status(response)
            return list(response.json())

    async def vv_get_job_record(self, record_id: int) -> dict[str, Any]:
        """GET /api/vv/job-records/{id} → one job V&V record."""
        async with await self._client() as c:
            response = await c.get(f"/api/vv/job-records/{record_id}", headers=self._headers())
            _raise_for_status(response)
            return dict(response.json())

    async def vv_list_records(
        self,
        *,
        record_kind: str = "all",
        state: list[str] | None = None,
        kind: list[str] | None = None,
        author_user_id: int | None = None,
        search: str | None = None,
        since: str | None = None,
        until: str | None = None,
        include_retired: bool = False,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """GET /api/vv/records → one page of the cross-install V&V register.

        Returns ``{rows, total, page, page_size}`` where each row carries
        resolved labels (target / author / evidence), never raw ids alone.
        """
        params: dict[str, Any] = {
            "record_kind": record_kind,
            "include_retired": include_retired,
            "page": page,
            "page_size": page_size,
        }
        if state:
            params["state"] = list(state)
        if kind:
            params["kind"] = list(kind)
        if author_user_id is not None:
            params["author_user_id"] = author_user_id
        if search:
            params["search"] = search
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        async with await self._client() as c:
            response = await c.get("/api/vv/records", headers=self._headers(), params=params)
            _raise_for_status(response)
            return dict(response.json())

    async def vv_list_pending(self) -> list[dict[str, Any]]:
        """GET /api/vv/pending → curator inbox (oldest first)."""
        async with await self._client() as c:
            response = await c.get("/api/vv/pending", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def vv_list_curators(self) -> list[dict[str, Any]]:
        """GET /api/vv/curators → eligible-approver picker."""
        async with await self._client() as c:
            response = await c.get("/api/vv/curators", headers=self._headers())
            _raise_for_status(response)
            return list(response.json())

    async def vv_create_tool_record(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /api/vv/tool-records — author draft (curate-only)."""
        async with await self._client() as c:
            response = await c.post("/api/vv/tool-records", headers=self._headers(), json=body)
            _raise_for_status(response)
            return dict(response.json())

    async def vv_patch_tool_record(self, record_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """PATCH /api/vv/tool-records/{id} — edit draft."""
        async with await self._client() as c:
            response = await c.patch(
                f"/api/vv/tool-records/{record_id}",
                headers=self._headers(),
                json=body,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def vv_submit_tool_record(
        self,
        record_id: int,
        *,
        designated_approver_user_id: int | None = None,
        designated_approver_username: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/vv/tool-records/{id}/submit → flip draft to pending."""
        body: dict[str, Any] = {
            "designated_approver_user_id": designated_approver_user_id,
            "designated_approver_username": designated_approver_username,
        }
        async with await self._client() as c:
            response = await c.post(
                f"/api/vv/tool-records/{record_id}/submit",
                headers=self._headers(),
                json=body,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def vv_approve_tool_record(
        self, record_id: int, *, reason: str | None = None
    ) -> dict[str, Any]:
        """POST /api/vv/tool-records/{id}/approve → record approved."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/vv/tool-records/{record_id}/approve",
                headers=self._headers(),
                json={"reason": reason},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def vv_reject_tool_record(self, record_id: int, *, reason: str) -> dict[str, Any]:
        """POST /api/vv/tool-records/{id}/reject (reason required)."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/vv/tool-records/{record_id}/reject",
                headers=self._headers(),
                json={"reason": reason},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def vv_retire_tool_record(self, record_id: int) -> dict[str, Any]:
        """POST /api/vv/tool-records/{id}/retire → retire from any live state."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/vv/tool-records/{record_id}/retire", headers=self._headers()
            )
            _raise_for_status(response)
            return dict(response.json())

    async def vv_numeric_factors(self, tool_version_id: int) -> list[dict[str, Any]]:
        """GET /api/vv/numeric-factors/{id} → numeric inputs for the range editor."""
        async with await self._client() as c:
            response = await c.get(
                f"/api/vv/numeric-factors/{tool_version_id}", headers=self._headers()
            )
            _raise_for_status(response)
            return list(response.json())

    async def vv_migrate_tool_records(
        self,
        *,
        source_tool_version_id: int,
        target_tool_version_id: int,
        record_ids: list[int],
    ) -> dict[str, Any]:
        """POST /api/vv/tool-records/migrate → carry approved records to a new version."""
        body = {
            "source_tool_version_id": source_tool_version_id,
            "target_tool_version_id": target_tool_version_id,
            "record_ids": record_ids,
        }
        async with await self._client() as c:
            response = await c.post(
                "/api/vv/tool-records/migrate", headers=self._headers(), json=body
            )
            _raise_for_status(response)
            return dict(response.json())

    async def vv_create_job_record(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /api/vv/job-records — author draft (curate-only)."""
        async with await self._client() as c:
            response = await c.post("/api/vv/job-records", headers=self._headers(), json=body)
            _raise_for_status(response)
            return dict(response.json())

    async def vv_patch_job_record(self, record_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """PATCH /api/vv/job-records/{id} — edit draft."""
        async with await self._client() as c:
            response = await c.patch(
                f"/api/vv/job-records/{record_id}",
                headers=self._headers(),
                json=body,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def vv_submit_job_record(
        self,
        record_id: int,
        *,
        designated_approver_user_id: int | None = None,
        designated_approver_username: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/vv/job-records/{id}/submit → flip draft to pending."""
        body: dict[str, Any] = {
            "designated_approver_user_id": designated_approver_user_id,
            "designated_approver_username": designated_approver_username,
        }
        async with await self._client() as c:
            response = await c.post(
                f"/api/vv/job-records/{record_id}/submit",
                headers=self._headers(),
                json=body,
            )
            _raise_for_status(response)
            return dict(response.json())

    async def vv_approve_job_record(
        self, record_id: int, *, reason: str | None = None
    ) -> dict[str, Any]:
        """POST /api/vv/job-records/{id}/approve → record approved."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/vv/job-records/{record_id}/approve",
                headers=self._headers(),
                json={"reason": reason},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def vv_reject_job_record(self, record_id: int, *, reason: str) -> dict[str, Any]:
        """POST /api/vv/job-records/{id}/reject (reason required)."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/vv/job-records/{record_id}/reject",
                headers=self._headers(),
                json={"reason": reason},
            )
            _raise_for_status(response)
            return dict(response.json())

    async def vv_retire_job_record(self, record_id: int) -> dict[str, Any]:
        """POST /api/vv/job-records/{id}/retire → record retired."""
        async with await self._client() as c:
            response = await c.post(
                f"/api/vv/job-records/{record_id}/retire", headers=self._headers()
            )
            _raise_for_status(response)
            return dict(response.json())


def _extract_filename(content_disposition: str) -> str:
    """Pull the ``filename="..."`` value out of a ``Content-Disposition`` header.

    Returns an empty string when the header doesn't carry one. Falls
    back to a generic ``bundle.tar.gz`` at the caller's discretion.
    """
    if not content_disposition:
        return ""
    # The header looks like: attachment; filename="matofx-migrate-...tar.gz"
    for part in content_disposition.split(";"):
        kv = part.strip()
        if kv.lower().startswith("filename="):
            return kv.split("=", 1)[1].strip().strip('"').strip("'")
    return ""
