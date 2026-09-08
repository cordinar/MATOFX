"""Job result page (``/ui/jobs/{job_id}``).

Faithful port of the legacy three-column results layout. The shape
of the page is driven by the tool's descriptor — the same descriptor
the submit form uses — so every output renders in the right group
with the right viewer (number with units, embedded HTML plot,
CSV-driven line chart, image preview, table).

State branching:

- **complete** → 3-column layout: project + scenario meta on the far
  left, descriptor-grouped Inputs (read-only, with units) in the
  middle, descriptor-grouped Outputs on the right with typed viewers;
  download buttons for the I/O summary, the outputs ZIP, and the full
  job ZIP (the ZIPs land in a follow-up).
- **running / queued** → status badge + cancel button + collapsible
  logs, refreshing every 2 s until terminal.
- **failed** → status badge + logs (no Outputs section because there
  may be nothing usable to render).

Auth caveat: browser-initiated ``<iframe src=...>`` and ``<img src=...>``
requests cannot carry the bearer header, so ``/api/jobs/.../outputs/...``
returns 401 to those requests. The page handler — which DOES have the
bearer — fetches the bytes server-side via the api_client and embeds
the content directly (``<iframe srcdoc=...>``, base64 ``data:`` URLs,
plotly figure JSON).
"""

from __future__ import annotations

import base64
import contextlib
import datetime as _dt
import io
import json
import math
from html import escape as html_escape
from typing import Any, Final

import pandas as pd
import plotly.graph_objects as go
from nicegui import ui

from matofx.ui._coverage_panel import support_view
from matofx.ui._features import kpi_extraction_enabled, vv_tracking_enabled
from matofx.ui._field_surrogate_viewer import (
    _field_animation_figure,
    _field_animation_filename,
    _field_frame_figure,
)
from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.format import (
    format_job_id,
    format_job_state,
    format_number,
    format_user_datetime,
)
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.plotly_extract import extract_plotly_figure as _extract_plotly_figure
from matofx.ui.state import current_token
from matofx.ui.theme import export_plotly_html, themed_plotly

_REFRESH_INTERVAL_S = 2.0
"""How often the page re-polls the API while the job is in-flight."""

_HTML_EXTS = frozenset({"html", "htm"})
_IMAGE_EXTS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "svg", "bmp"})
_PDF_EXTS = frozenset({"pdf"})
_ANIM3D_EXTS = frozenset({"gltf", "glb"})
"""Animated / scene-graph 3-D formats the browser can render natively
via three.js's GLTFLoader (NiceGUI's ``ui.scene.gltf(...)``). Other
animated formats (``.fbx`` / ``.dae`` / ``.usdz``) fall to a download
link — they need server-side conversion which is its own roadmap item,
mirroring the STP/STEP path."""
_VIDEO_EXTS = frozenset({"mp4", "webm", "ogg", "ogv", "mov", "m4v"})
"""Video formats the HTML5 ``<video>`` element handles. ``.mp4`` /
``.webm`` / ``.ogg`` are universal; ``.mov`` (QuickTime) and ``.m4v``
work in Safari and recent Chromium builds — when the browser refuses
to play, the user still has the download link beneath the player."""

_INLINE_HTML_CAP_BYTES = 5 * 1024 * 1024
"""HTML files larger than 5 MB get a friendly fallback message instead
of being inlined — keeps the page weight bounded."""

_INLINE_PDF_CAP_BYTES = 25 * 1024 * 1024
"""PDFs larger than 25 MB skip the inline ``<iframe>`` and surface a
download link instead. Base64-embedded data URLs grow ~33% over raw
bytes, so 25 MB on disk → ~33 MB on the wire; above that point most
browsers stop rendering the embedded viewer reliably."""

_INLINE_ANIM3D_CAP_BYTES = 50 * 1024 * 1024
"""GLB/GLTF files above 50 MB skip the inline scene and fall back to a
download link. Three.js can handle larger meshes but the embedding
data-URL grows ~33% over raw bytes and the WebGL initial parse time
becomes the bottleneck on lower-spec laptops."""

_INLINE_VIDEO_CAP_BYTES = 50 * 1024 * 1024
"""Videos above 50 MB skip the inline player — a base64 data URL of
that size keeps the whole file in browser memory which is wasteful for
a 1080p clip the user only wants to skim. Download link still
surfaces."""

_IMAGE_MIMES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "bmp": "image/bmp",
}

_VIDEO_MIMES: dict[str, str] = {
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "webm": "video/webm",
    "ogg": "video/ogg",
    "ogv": "video/ogg",
    "mov": "video/quicktime",
}

_ANIM3D_MIMES: dict[str, str] = {
    "gltf": "model/gltf+json",
    "glb": "model/gltf-binary",
}

_STATE_BADGE: dict[str, str] = {
    "created": "color=grey",
    "queued": "color=info",
    "running": "color=warning",
    "complete": "color=positive",
    "failed": "color=negative",
}


def detect_output_kind(filename: str) -> str:
    """Classify ``filename`` by its extension to pick an inline viewer.

    Returns one of ``"html"``, ``"image"``, ``"pdf"``, ``"animation_3d"``,
    ``"video"``, or ``"download"``. Used by the fallback path that
    renders files NOT declared in the tool's descriptor —
    descriptor-declared outputs go through the type-aware renderer
    instead. The classifier picks the *primary* inline viewer for an
    extension; callers still have the file's bytes and can fall back to
    a download link if the viewer refuses the content.
    """
    if "." not in filename:
        return "download"
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext in _HTML_EXTS:
        return "html"
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _PDF_EXTS:
        return "pdf"
    if ext in _ANIM3D_EXTS:
        return "animation_3d"
    if ext in _VIDEO_EXTS:
        return "video"
    return "download"


def _normalise_output_path(filename: str) -> tuple[str, str]:
    """``(server-relative path, download basename)`` for an output filename.

    Tool authors may write the descriptor ``filename`` bare (``"clip.mp4"``)
    or with the redundant working-dir ``outputs/`` prefix (the field tools do
    this). We strip that prefix and any ``.`` / ``..`` traversal segments
    (the endpoint independently guards too), and return the cleaned relative
    path plus its basename (used as the saved-as filename).
    """
    norm = filename.replace("\\", "/").lstrip("/")
    if norm.startswith("outputs/"):
        norm = norm[len("outputs/") :]
    norm = "/".join(p for p in norm.split("/") if p not in ("", ".", ".."))
    leaf = norm.rsplit("/", 1)[-1] or "output"
    return norm, leaf


def _output_download_link(
    client: ApiClient, job_id: int, filename: str, *, text: str | None = None
) -> ui.button:
    """A download control for one job output file — the ONLY correct way.

    Fetches the bytes **server-side with the bearer header** (via the
    api_client, which reaches the API directly, not through the browser's
    reverse proxy) and hands them to the browser with ``ui.download.content``.
    A bare ``<a href>`` to ``/api/jobs/.../outputs/...`` can't carry the
    bearer the API requires, so a browser-initiated request gets a 401/404
    and the download silently fails. **Do NOT build direct href links to the
    output endpoint** — that class of bug hit field outputs first, then
    videos (``.mp4``), PDFs, CAD, and images in turn. Routing every output
    download through this one helper is what keeps the fix permanent.
    """
    norm, leaf = _normalise_output_path(filename)

    async def _download() -> None:
        try:
            data = await client.download_job_output(job_id, norm)
        except ApiError as exc:
            ui.notify(f"Couldn't download {leaf}: {exc.detail}", type="negative")
            return
        ui.download.content(data, leaf)

    return ui.button(text or f"Download {leaf}", icon="download", on_click=_download).props(
        "flat dense no-caps color=primary size=sm"
    )


# ---------- page entry point -------------------------------------------------


@ui.page("/jobs/{job_id}")
async def job_page(job_id: int) -> None:
    """Deep-link for a single job.

    Scenario-homed jobs defer to the unified ``/my-jobs`` workspace (the single
    interactive surface — ``feedback_my_jobs_unified_workspace``). Study-homed
    children (parametric / optimisation) have **no scenario**, so that
    scenario/tool-cascade workspace can't show them — they render standalone
    here instead. The rendering helpers (``_render_body`` and friends) are the
    same ones ``my_jobs.py`` uses for its right pane.
    """
    if not require_logged_in():
        return
    client = ApiClient(get_app(), token=current_token())
    try:
        job = await client.get_job(int(job_id))
    except ApiError:
        # Fall back to the workspace, which renders its own not-found message.
        ui.navigate.to(f"/my-jobs?job={int(job_id)}")
        return
    if job.get("scenario_id") is not None:
        ui.navigate.to(f"/my-jobs?job={int(job_id)}")
        return
    render_shell()
    with ui.column().classes("w-full mx-auto p-6 gap-4").style("max-width: 1700px;"):
        with ui.row().classes("items-center gap-2"):
            ui.button(icon="arrow_back", on_click=ui.navigate.back).props(
                "flat round dense"
            ).tooltip("Back")
            ui.label("Run result").style("font-size: 1.5rem; font-weight: 600;")
        container = ui.column().classes("w-full gap-4")
        await _render_body(client, job, int(job_id), container)


async def _render_body(
    client: ApiClient,
    job: dict[str, Any],
    job_id: int,
    container: ui.column,
) -> None:
    """Render the state-specific body inside ``container``.

    Fetches the tool's name + version once up-front so every state
    branch can render ``Tool: <Friendly Name> v1.2.3`` in the status
    row without repeating the lookup. Falls back to the alias when the
    fetch fails (a tool the user no longer has visibility on, e.g.
    after archival) so the status row never goes blank.
    """
    state = str(job.get("state") or "")
    tool_meta = await _resolve_tool_meta(client, str(job.get("tool_alias") or ""))
    container.clear()
    with container:
        if state == "complete":
            await _render_complete(client, job, job_id, container, tool_meta)
        elif state == "failed":
            await _render_failed(client, job, job_id, container, tool_meta)
        else:
            await _render_inflight(client, job, job_id, container, tool_meta)


async def _resolve_tool_meta(client: ApiClient, alias: str) -> dict[str, str]:
    """Fetch the tool's friendly ``name`` + ``version`` for the status row."""
    if not alias:
        return {"name": "", "version": ""}
    try:
        detail = await client.get_tool(alias)
    except ApiError:
        return {"name": alias, "version": ""}
    return {
        "name": str(detail.get("name") or alias),
        "version": str(detail.get("version") or ""),
    }


# ---------- complete state — legacy-style 2-col layout -----------------------


async def _render_complete(
    client: ApiClient,
    job: dict[str, Any],
    job_id: int,
    body_container: ui.column,
    tool_meta: dict[str, str],
) -> None:
    """Legacy-style results: meta + Inputs (left) + Outputs (right) + downloads."""
    _render_status_row(job, job_id, tool_meta)

    # Auto-generated results summary (designs/auto-summaries.md).
    # Best-effort executive summary up top; any failure collapses silently.
    from matofx.ui._summary_card import render_summary_card  # noqa: PLC0415

    await render_summary_card(client, "job", job_id)

    descriptor: dict[str, Any] = {}
    try:
        tool_detail = await client.get_tool(job["tool_alias"])
        descriptor = dict(tool_detail.get("descriptor") or {})
    except ApiError as exc:
        ui.label(f"Couldn't load the tool descriptor: {exc.detail}").style("color: #c0392b;")

    outputs_doc = await _load_outputs_doc(client, job_id)

    # Project + scenario for the meta panel. Best-effort: a permission
    # error here shouldn't block the rest of the page.
    scenario: dict[str, Any] | None = None
    project: dict[str, Any] | None = None
    opt_study: dict[str, Any] | None = None
    # Study-homed children (parametric / optimisation) have no scenario — skip
    # the scenario/project meta lookup rather than ``int(None)``-crashing. An
    # optimisation child resolves its project + study facts instead.
    if job.get("scenario_id") is not None:
        with contextlib.suppress(ApiError):
            scenario = await client.get_scenario(int(job["scenario_id"]))
        if scenario is not None:
            with contextlib.suppress(ApiError):
                project = await client.get_project(int(scenario["project_id"]))
    elif job.get("optimisation_study_id") is not None:
        with contextlib.suppress(ApiError):
            opt_study = await client.get_optimisation_study(int(job["optimisation_study_id"]))
        if opt_study is not None and opt_study.get("project_id") is not None:
            with contextlib.suppress(ApiError):
                project = await client.get_project(int(opt_study["project_id"]))
    elif job.get("parametric_study_id") is not None:
        # A parametric-study child is homed on the study, not a scenario —
        # resolve its project via the study so the Project card still shows
        # (it was silently missing: no branch here before). ``get_study``
        # returns ``{study, children}``, so the project_id is nested under
        # ``study`` — not the top-level dict.
        study_detail: dict[str, Any] | None = None
        with contextlib.suppress(ApiError):
            study_detail = await client.get_study(int(job["parametric_study_id"]))
        study_obj = (study_detail or {}).get("study") or {}
        if study_obj.get("project_id") is not None:
            with contextlib.suppress(ApiError):
                project = await client.get_project(int(study_obj["project_id"]))

    # Three-section row matching the legacy "col1 / col2 / col3" layout:
    # project + scenario meta on the far left, the descriptor's read-only
    # Inputs in the middle, Outputs filling the rest. Outputs still gets
    # the bulk of the width so plots have room to breathe; Inputs gets
    # enough width to fit a ``material`` input's compact Property /
    # Value / Unit table without wrapping (a too-narrow column squashed
    # the table when a job carried a material — feedback 2026-05-26).
    with ui.row().classes("w-full items-start gap-4 no-wrap"):
        with ui.column().classes("gap-3").style("flex: 0 0 18%; min-width: 14rem;"):
            if opt_study is not None:
                _render_optimisation_meta(opt_study, project, descriptor)
            else:
                _render_meta_panel(project, scenario)
            # The run's own identity, under the cards describing where it
            # lives. Last because it's the most specific of the three.
            _render_job_card(job, job_id)
        with ui.column().classes("gap-3").style("flex: 0 0 26%; min-width: 22rem;"):
            _render_inputs_panel(client, job_id, descriptor, dict(job.get("inputs") or {}))
        with ui.column().classes("gap-3").style("flex: 1 1 auto; min-width: 0;"):
            await _render_outputs_panel(client, job_id, descriptor, outputs_doc)

    _render_download_row(job_id, descriptor, dict(job.get("inputs") or {}), outputs_doc)
    # Lineage / provenance section (ADR-0010 slice 5). Lazy-loaded
    # collapsible — the API call only fires when the user opens the
    # expansion, so the complete-state render stays light by default.
    from matofx.ui.lineage_view import render_provenance_section  # noqa: PLC0415

    render_provenance_section(kind="job", obj_id=job_id)
    # V&V card (ADR-0011 §8 — per-job surface). Same gate-and-degrade
    # pattern as lineage above: the card only renders when the feature
    # is on, and any API failure inside collapses silently rather than
    # breaking the rest of the page.
    if vv_tracking_enabled():
        await _render_job_vv_card(client, job_id, str(job.get("name") or ""))
    # Headline KPIs card (ADR-0012 §8.3 — per-job surface). Same
    # gate-and-degrade pattern as V&V above.
    if kpi_extraction_enabled():
        await _render_job_kpi_card(client, job, job_id)
    _ = body_container  # body_container handed in for symmetry; nothing more to do here


def _render_job_card(job: dict[str, Any], job_id: int) -> None:
    """Info card for the job itself — name, id, when it was created.

    The job's *name* had no home on this page: the status row carries
    the tool, the state and the timestamps, and the meta column
    described the project and scenario the run belongs to but never the
    run. Shaped like its Project / Scenario siblings and shown in every
    state — a running or failed job is exactly as worth identifying by
    name as a finished one.
    """
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-1"):
        ui.label("Job").style("font-size: 1rem; font-weight: 600;")
        _meta_row("Name", job.get("name"))
        _meta_row("Job ID", format_job_id(job_id))
        _meta_row("Created", _format_timestamp(job.get("created_at")))


def _render_standalone_job_card(job: dict[str, Any], job_id: int) -> None:
    """The Job card for the states that have no meta column (in-flight / failed).

    Those views are a status row plus logs — no Project / Scenario
    column to sit under — so the card is width-capped rather than
    stretched across the page.
    """
    with ui.row().classes("w-full").style("max-width: 26rem;"):
        _render_job_card(job, job_id)


def _render_meta_panel(project: dict[str, Any] | None, scenario: dict[str, Any] | None) -> None:
    """Far-left column: stacked Project + Scenario info cards (legacy ``col1``).

    May render nothing at all (a study-homed run has neither) — the
    caller adds the Job card underneath either way.
    """
    if project is None and scenario is None:
        return  # nothing to show — caller handles the empty column
    if project is not None:
        with ui.card().classes("w-full"), ui.column().classes("w-full gap-1"):
            ui.label("Project").style("font-size: 1rem; font-weight: 600;")
            _meta_row("Name", project.get("name"))
            _meta_row("Description", project.get("description"))
            _meta_row("Created", _format_timestamp(project.get("created_at")))
    if scenario is not None:
        with ui.card().classes("w-full"), ui.column().classes("w-full gap-1"):
            ui.label("Scenario").style("font-size: 1rem; font-weight: 600;")
            _meta_row("Name", scenario.get("name"))
            _meta_row("Description", scenario.get("description"))
            _meta_row("Created", _format_timestamp(scenario.get("created_at")))


def _render_optimisation_meta(
    study: dict[str, Any], project: dict[str, Any] | None, descriptor: dict[str, Any]
) -> None:
    """Far-left column for an optimisation run: Project card + Optimisation card.

    Mirrors the Project/Scenario meta panel of a standalone tool run. Objectives
    are shown by descriptor label, not output name (labels-not-names).
    """
    from matofx.ui.pages.optimisation import _STRATEGY_OPTIONS  # noqa: PLC0415

    if project is not None:
        with ui.card().classes("w-full"), ui.column().classes("w-full gap-1"):
            ui.label("Project").style("font-size: 1rem; font-weight: 600;")
            _meta_row("Name", project.get("name"))
            _meta_row("Description", project.get("description"))
            _meta_row("Created", _format_timestamp(project.get("created_at")))

    cfg = study.get("config_json") or {}
    strat = str(cfg.get("strategy") or "")
    out_labels = {
        str(o["name"]): str(o.get("label") or o["name"])
        for o in (descriptor.get("outputs") or [])
        if isinstance(o, dict) and o.get("name")
    }
    objectives = []
    for r in cfg.get("responses") or []:
        if not isinstance(r, dict) or r.get("goal") not in ("minimise", "maximise"):
            continue
        spec = r.get("spec") if isinstance(r.get("spec"), dict) else {}
        key = str(spec.get("name") or r.get("name")) if spec.get("kind") == "scalar" else str(
            r.get("name")
        )
        objectives.append(f"{out_labels.get(key, key)} ({r.get('goal')})")

    with ui.card().classes("w-full"), ui.column().classes("w-full gap-1"):
        ui.label("Optimisation study").style("font-size: 1rem; font-weight: 600;")
        _meta_row("Name", study.get("name"))
        _meta_row("State", str(study.get("state") or "").replace("_", " ").title())
        _meta_row("Strategy", _STRATEGY_OPTIONS.get(strat, strat))
        _meta_row("Objectives", ", ".join(objectives) or None)
        _meta_row("Batches run", study.get("last_batch_number"))
        _meta_row("Created", _format_timestamp(study.get("created_at")))
        sid = int(study.get("id") or 0)
        ui.button(
            "View optimisation study",
            icon="open_in_new",
            on_click=lambda _e=None, _sid=sid: ui.navigate.to(f"/optimisation/{_sid}"),
        ).props("flat dense color=primary")


def _meta_row(label: str, value: Any) -> None:
    """One ``**Label:** value`` line in a meta card; skipped when the value is empty."""
    if value is None or value == "":
        return
    ui.markdown(f"**{label}:** {value}").classes("text-sm")


def _format_timestamp(value: Any) -> str:
    """Render an API datetime string (or anything stringifiable) as ``YYYY-MM-DD HH:MM``."""
    if value is None:
        return ""
    text = str(value)
    # API uses ISO 8601 like ``2026-05-06T08:30:00.123456+00:00``. Trim to
    # minutes for readability and replace the T separator with a space.
    if "T" in text and len(text) >= 16:
        return text[:16].replace("T", " ")
    return text


_INPUT_PREVIEW_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {"png", "jpg", "jpeg", "gif", "bmp", "webp", "svg"}
)
"""Extensions the Inputs panel renders inline beneath the row.

Mirrors the upload widget's preview list — anything in here gets a
thumbnail under the ``label: filename`` line so the user can see at a
glance what file the job ran against. Bigger CAD formats need their
own viewer and aren't in this set; their row stays text-only."""

_INPUT_PREVIEW_IMAGE_MIME: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "webp": "image/webp",
    "svg": "image/svg+xml",
}


def _render_inputs_panel(
    client: ApiClient,
    job_id: int,
    descriptor: dict[str, Any],
    job_inputs: dict[str, Any],
) -> None:
    """Bordered "Inputs" panel — every submitted parameter, grouped + with units.

    File inputs whose value ends in a recognised image extension also
    get an inline thumbnail beneath the ``label: filename`` line so
    the user can see what file the job ran against without having to
    download it. Bytes are fetched via the same authenticated
    file-preview endpoint the submit form's preview pane uses; a
    fetch failure (e.g. the file was cleaned up post-run) silently
    degrades to text-only.
    """
    inputs_by_name: dict[str, dict[str, Any]] = {
        i["name"]: i for i in (descriptor.get("inputs") or []) if i.get("name")
    }
    groups = list(descriptor.get("input_groups") or [])

    with ui.card().classes("w-full"), ui.column().classes("w-full gap-3"):
        ui.label("Inputs").style("font-size: 1.25rem; font-weight: 600;")

        if not groups:
            # No groups declared — render a flat list.
            for name, value in job_inputs.items():
                _render_input_row(
                    client,
                    job_id,
                    inputs_by_name.get(name, {"label": name}),
                    value,
                )
            return

        for group in groups:
            group_name = group.get("name")
            field_names = [
                i["name"] for i in (descriptor.get("inputs") or []) if i.get("group") == group_name
            ]
            with ui.card().classes("w-full"), ui.column().classes("w-full gap-1"):
                ui.label(group.get("label") or group_name or "").style(
                    "font-size: 1rem; font-weight: 600;"
                )
                for name in field_names:
                    spec = inputs_by_name.get(name, {"label": name})
                    _render_input_row(client, job_id, spec, job_inputs.get(name))


def _render_input_row(
    client: ApiClient,
    job_id: int,
    field_spec: dict[str, Any],
    value: Any,
) -> None:
    """One input row: ``label: value unit`` + inline image preview for file inputs.

    For non-file inputs, renders a single ``**Label:** value`` line
    via ``ui.markdown``. For file inputs whose path ends in a
    recognised image extension, the row also kicks off an async
    fetch to the preview endpoint and embeds the bytes as a data URL
    beneath the row — same look-and-feel as the upload widget's
    preview pane.

    For ``material`` inputs the frozen block is rendered as a small
    card with a ``Name vN — class: Class`` header and a compact
    Property / Value / Unit table (default-expanded; one click on the
    header collapses the table for a busy page).
    """
    label = str(field_spec.get("label") or field_spec.get("name") or "?")
    unit = str(field_spec.get("unit") or "")
    field_type = str(field_spec.get("type") or "")
    if field_type == "material" and isinstance(value, dict):
        _render_material_input_row(label, value)
        return
    formatted = _format_input_value(value, field_type)
    line = f"**{label}:** {formatted}"
    if unit and field_type != "file":
        line = f"{line} {unit}"
    ui.markdown(line).classes("text-sm")

    if field_type != "file" or not isinstance(value, str) or not value:
        return
    ext = value.rsplit(".", 1)[-1].lower() if "." in value else ""
    if ext not in _INPUT_PREVIEW_IMAGE_EXTENSIONS:
        return

    placeholder = ui.label("Loading preview…").style(
        "color: var(--matofx-muted-text); font-size: 0.75rem;"
    )
    # A flex column, not a bare div: the download button under the preview
    # right-aligns with ``self-end``, which needs a flex parent. Styled rather
    # than switched to ``ui.column`` so its default ``gap-4`` doesn't fight the
    # tight spacing this row wants.
    image_holder = (
        ui.element("div")
        .classes("w-full")
        .style("display: flex; flex-direction: column; gap: 0.25rem;")
    )

    async def _load_preview() -> None:
        try:
            blob = await client.fetch_draft_file_preview(job_id, path=value)
        except ApiError:
            placeholder.text = ""
            placeholder.visible = False
            return
        except Exception:
            placeholder.text = ""
            placeholder.visible = False
            return
        placeholder.delete()
        mime = _INPUT_PREVIEW_IMAGE_MIME.get(ext, "application/octet-stream")
        data_url = f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"
        with image_holder:
            # ``fit=contain`` so the whole image renders inside the
            # 200 px height bound (Quasar's default ``cover`` crops
            # to fill — see the 2026-05-14 file-widget fix for the
            # same lesson). Width is full of the (narrow) Inputs
            # column; height capped so a tall portrait doesn't push
            # the rest of the inputs offscreen.
            ui.image(data_url).props("fit=contain").classes("w-full").style(
                "max-height: 200px; height: 200px; background: transparent;"
            )
            # Same rule as the outputs panel: anything rendered can be taken
            # away. The bytes are already in hand from the preview fetch, so
            # this costs no second round-trip.
            leaf = value.replace("\\", "/").rsplit("/", 1)[-1] or "input"
            ui.button(
                f"Download {leaf}",
                icon="download",
                on_click=lambda _e=None, _b=blob, _n=leaf: ui.download.content(_b, _n),
            ).props("flat dense no-caps color=primary size=sm").classes("self-end")

    ui.timer(0.0, _load_preview, once=True)


def _render_material_input_row(label: str, block: dict[str, Any]) -> None:
    """Render a material input's frozen block (ADR-0007 §6) as a card + table.

    The expansion's *header* is the compact "Iron v2 — class: Metals"
    summary; the body is a Property / Value / Unit table over the
    frozen properties dict (slug-keyed; each value carries the display
    ``prop_name``). Default-expanded; clicking the header collapses
    it so a job page with several material inputs stays scannable.
    """
    name = str(block.get("name") or "(unnamed)")
    class_name = str(block.get("class") or "")
    ordinal = block.get("version_ordinal")
    head_bits = [f"{name} v{ordinal}"] if ordinal else [name]
    if class_name:
        head_bits.append(f"class: {class_name}")
    header = "  —  ".join(head_bits)

    props_map = block.get("properties") or {}
    rows = []
    for slug in sorted(props_map):
        p = props_map[slug] or {}
        rows.append(
            {
                "prop_name": str(p.get("prop_name") or slug),
                "value": p.get("value"),
                "unit": str(p.get("unit") or ""),
            }
        )

    # Bordered card so the material reads as one block within the
    # Inputs panel; matches the picker dialog's card look.
    ui.label(label).style("font-weight: 500; font-size: 0.85rem; margin-top: 0.25rem;")
    with (
        ui.card()
        .classes("w-full q-pa-none")
        .style("border: 1px solid var(--matofx-card-border); border-radius: 4px;")
    ):
        with ui.expansion(header, value=True).classes("w-full").props("dense") as exp:
            exp.props("expand-icon-toggle")
            with exp:
                if not rows:
                    ui.label("(no properties on this version)").style(
                        "color: #888; font-size: 0.78rem;"
                    )
                    return
                ui.table(
                    columns=[
                        {
                            "name": "prop_name",
                            "label": "Property",
                            "field": "prop_name",
                            "align": "left",
                        },
                        {"name": "value", "label": "Value", "field": "value", "align": "right"},
                        {"name": "unit", "label": "Unit", "field": "unit", "align": "left"},
                    ],
                    rows=rows,
                ).classes("w-full").props("dense flat")


def _format_input_value(value: Any, field_type: str) -> str:
    """Turn a submitted value into a compact human-readable string."""
    if value is None or value == "":
        return "—"
    if field_type == "file":
        # Submitted file values are typically full paths; show only the leaf.
        text = str(value)
        return text.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list | tuple):
        if not value:
            return "(none)"
        return ", ".join(_format_input_value(v, "") for v in value)
    if isinstance(value, int | float):
        # Round numeric inputs to a sensible length — a parametric-study /
        # optimisation child varies inputs to many decimals, which rendered
        # raw here (str(float)). Significant-figures via the shared helper.
        return format_number(value)
    return str(value)


# ---------- outputs panel ----------------------------------------------------


async def _render_outputs_panel(
    client: ApiClient,
    job_id: int,
    descriptor: dict[str, Any],
    outputs_doc: dict[str, Any] | None,
) -> None:
    """Bordered "Outputs" panel — descriptor-driven typed renderers per group."""
    output_groups = list(descriptor.get("output_groups") or [])
    output_fields = list(descriptor.get("outputs") or [])
    column_layout = dict(descriptor.get("output_column_layout") or {})
    value_lookup = _outputs_value_lookup(outputs_doc)

    # Surrogate ± uncertainty folding (ADR-0008 §7.1): an output whose
    # ``uncertainty_for`` names a value sibling is that value's 1-sigma. Fold it
    # into the sibling's display as ``value ± std`` and suppress its own
    # row; a footnote explains the ± once. ``std_for`` maps a value name to
    # its std runtime value; ``visible_fields`` drops the std outputs.
    std_for: dict[str, Any] = {}
    std_names: set[str] = set()
    for f in output_fields:
        target = f.get("uncertainty_for")
        if target:
            std_names.add(str(f.get("name")))
            std_for[str(target)] = value_lookup.get(str(f.get("name")), {}).get("value")
    visible_fields = [f for f in output_fields if str(f.get("name")) not in std_names]

    with ui.card().classes("w-full"), ui.column().classes("w-full gap-3"):
        ui.label("Outputs").style("font-size: 1.25rem; font-weight: 600;")

        if not output_fields:
            ui.label(
                "This tool's descriptor doesn't declare any outputs to display. "
                "Use the download buttons below to fetch the raw files."
            ).style("color: #888;")
            return

        if not output_groups:
            # Flat list.
            for output in visible_fields:
                await _render_typed_output(client, job_id, output, value_lookup, std_for=std_for)
        else:
            num_cols = max((int(g.get("column") or 1) for g in output_groups), default=1)
            flex_weights = _flex_weights_from_layout(column_layout, num_cols)

            with ui.row().classes("w-full items-start gap-3 no-wrap"):
                for col_idx in range(num_cols):
                    with (
                        ui.column()
                        .classes("gap-3")
                        .style(f"flex: {flex_weights[col_idx]} 1 0; min-width: 0;")
                    ):
                        col_groups = sorted(
                            (g for g in output_groups if int(g.get("column") or 1) == col_idx + 1),
                            key=lambda g: int(g.get("row") or 1),
                        )
                        for group in col_groups:
                            await _render_output_group(
                                client, job_id, group, visible_fields, value_lookup, std_for=std_for
                            )

        # One ± footnote when any uncertainty was folded in.
        if any(v is not None for v in std_for.values()):
            ui.label(
                "± values are one standard deviation (1-sigma) of the surrogate's prediction."
            ).style("color: #888; font-size: 0.8rem; font-style: italic;")


def _flex_weights_from_layout(layout: dict[str, float], num_cols: int) -> list[float]:
    """Translate ``column_layout`` into flex-grow weights, one per column.

    Normalised to sum to 1 — which is what the wizard's layout editor
    already promises authors ("they're normalised on render"). It has to
    be: ``flex-grow`` values that sum to *less* than 1 distribute only
    that fraction of the free space, so the row renders short and the
    remainder sits blank. That bit tools whose groups collapsed back to
    a single column while a stale ``column_2`` weight stayed in the
    descriptor: the one column read ``0.5`` and rendered at half width.
    Relative widths are unchanged by this (2:1 was already 67/33).
    """
    weights: list[float] = []
    even = 1.0 / num_cols if num_cols else 1.0
    for col in range(1, num_cols + 1):
        try:
            w = float(layout.get(f"column_{col}", even))
        except (TypeError, ValueError):
            w = even
        weights.append(max(w, 0.0))
    total = sum(weights)
    if total <= 0:
        weights = [even for _ in weights]
        total = sum(weights) or 1.0
    # Normalise first, *then* clamp: the floor keeps a nearly-zero
    # column visible, and applying it afterwards means it can only ever
    # push the total over 1 (which still fills the row), never under.
    return [max(w / total, 0.05) for w in weights]


async def _render_output_group(
    client: ApiClient,
    job_id: int,
    group: dict[str, Any],
    all_outputs: list[dict[str, Any]],
    value_lookup: dict[str, dict[str, Any]],
    std_for: dict[str, Any] | None = None,
) -> None:
    """One group card with its declared outputs."""
    group_name = group.get("name")
    group_outputs = [o for o in all_outputs if o.get("group") == group_name]
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        ui.label(group.get("label") or group_name or "").style("font-size: 1rem; font-weight: 600;")
        for output in group_outputs:
            await _render_typed_output(client, job_id, output, value_lookup, std_for=std_for)


def _outputs_value_lookup(outputs_doc: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Build a ``{output_name: entry}`` map from the v2 outputs document.

    The v2 schema stores outputs as a flat list of dicts under the
    top-level ``outputs`` key; the legacy schema used a separate
    ``variables`` / ``files`` split. We index the v2 list by ``name``
    so each descriptor-declared output can find its runtime value
    cheaply.
    """
    if not outputs_doc:
        return {}
    raw = outputs_doc.get("outputs")
    if not isinstance(raw, list):
        return {}
    return {entry["name"]: entry for entry in raw if isinstance(entry, dict) and entry.get("name")}


async def _render_typed_output(
    client: ApiClient,
    job_id: int,
    output: dict[str, Any],
    value_lookup: dict[str, dict[str, Any]],
    std_for: dict[str, Any] | None = None,
) -> None:
    """Dispatch to the right viewer based on the descriptor's type / file_type."""
    type_ = str(output.get("type") or "")
    name = str(output.get("name") or "")
    label = str(output.get("label") or name or "?")
    value_entry = value_lookup.get(name, {})

    if type_ == "number":
        std_value = (std_for or {}).get(name)
        _render_number_output(label, output, value_entry, std_value=std_value)
        return

    if type_ == "table":
        await _render_table_output(client, job_id, label, output, value_entry)
        return

    if type_ == "file":
        await _render_file_output(client, job_id, output, label)
        return

    if type_ == "string":
        _render_string_output(label, name, value_entry)
        return

    # Unknown type — just show the label so the operator knows something
    # is missing rather than silently dropping it.
    ui.label(f"{label}: (unsupported output type {type_!r})").style("color: #888;")


def support_badge(value_entry: dict[str, Any]) -> tuple[str, str, str] | None:
    """``(label, colour, note)`` for a ``support_status`` output, or ``None``.

    The decision, separated from the drawing: this module's tests exercise
    pure helpers because the NiceGUI page context is not drivable from the
    test transport, so a badge whose logic lived inside the renderer would be
    a badge nothing could check.

    ``None`` means **draw nothing** — a version fitted before coverage existed
    carries no verdict, and a blank badge invites the reader to assume the
    blank means fine.
    """
    # The job-output shape (``value`` / ``note``) adapted onto the standard
    # one; the decision itself belongs to ``support_view`` and is shared with
    # the predicted-field viewer.
    return support_view(
        {"status": str(value_entry.get("value") or ""), "reason": value_entry.get("note")}
    )


def _render_string_output(
    label: str,
    name: str,
    value_entry: dict[str, Any],
) -> None:
    """A text output — as a badge for the predict-time support verdict (§8).

    ``string`` has been a legal output type since ADR-0008 but had no branch
    here, so it fell through to "(unsupported output type)". That is what an
    exported surrogate's **Prediction support** has been showing: the guard's
    verdict rendered as an error message, in the one place a prediction
    actually reaches a person.

    ``support_status`` gets the badge treatment because the three verdicts are
    not interchangeable text — "outside the trained range" needs to look
    different from "well supported" at a glance, and the reason underneath
    names the input that caused it, which is the part anybody can act on.
    """
    if name == "support_status":
        badge = support_badge(value_entry)
        if badge is None:
            # A version fitted before coverage existed has no verdict, and an
            # empty badge is worse than no badge: it invites the reader to
            # assume the blank means fine.
            return
        chip_label, colour, note = badge
        with ui.column().classes("gap-1"):
            with ui.row().classes("items-center gap-2"):
                ui.label(f"{label}:").classes("text-sm").style("font-weight: 600;")
                ui.badge(chip_label).props(f"color={colour}").style(
                    "font-size: 0.8rem; padding: 0.3rem 0.6rem"
                )
            if note:
                ui.label(note).style("color: #888; font-size: 0.8rem;")
        return

    value = value_entry.get("value")
    if value is None or str(value) == "":
        ui.markdown(f"**{label}:** —").classes("text-sm")
        return
    ui.markdown(f"**{label}:** {value}").classes("text-sm")


def _render_number_output(
    label: str,
    output: dict[str, Any],
    value_entry: dict[str, Any],
    std_value: Any = None,
) -> None:
    """``label: value unit`` — value rounded to descriptor's precision.

    When ``std_value`` is given (a surrogate response's paired 1-sigma
    uncertainty, ADR-0008 §7.1) the value renders as ``value ± std`` so
    the prediction and its confidence read as one quantity.
    """
    value = value_entry.get("value")
    unit = str(output.get("unit") or "")
    precision = output.get("precision")
    formatted = _format_number(value, precision)
    if std_value is not None and value is not None:
        formatted = f"{formatted} ± {_format_number(std_value, precision)}"
    line = f"**{label}:** {formatted}"
    if unit:
        line = f"{line} {unit}"
    ui.markdown(line).classes("text-sm")


def _format_number(value: Any, precision: float | None) -> str:
    """Round ``value`` to the number of decimals implied by ``precision``."""
    if value is None:
        return "—"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if precision is None or precision <= 0:
        return f"{f:.4g}"  # sensible default — 4 significant figures
    decimals = max(0, -math.floor(math.log10(precision)))
    return f"{f:.{decimals}f}"


async def _render_table_output(
    client: ApiClient,
    job_id: int,
    label: str,
    output: dict[str, Any],
    value_entry: dict[str, Any],
) -> None:
    """Render a ``type=table`` output as an inline DataFrame-style table.

    Three accepted shapes — the tool author picks whichever matches
    how they emit the data:

    1. **Inline dict-of-lists** in ``outputs.json`` — row-oriented:
       ``{"col_a": [1, 2, 3], "col_b": [4, 5, 6]}``. Each value is a
       column.
    2. **Inline dict-of-dicts** in ``outputs.json`` — column-oriented:
       ``{"row_label_1": {"col_a": 1, "col_b": 2}, ...}``. Outer keys
       are row labels, inner keys are columns.
    3. **CSV file on disk** — the descriptor carries
       ``filename = "..."`` and the tool wrote the table as a CSV
       under the job's working dir. We download + parse it here so a
       user who declared ``type = "table"`` without using the
       ``file_type = "csv_table"`` indirection still gets a real
       rendered table.

    Empty / unparseable data surfaces a friendly placeholder rather
    than crashing the panel.
    """
    ui.label(label).style("font-weight: 600;")
    raw = value_entry.get("value")
    if isinstance(raw, dict) and raw:
        _render_inline_table(raw)
        return
    filename = str(output.get("filename") or "")
    if filename and filename.lower().endswith(".csv"):
        try:
            content = await client.download_job_output(job_id, filename)
        except ApiError as exc:
            ui.label(f"Couldn't load {filename}: {exc.detail}").style("color: #c0392b;")
            _output_download_link(client, job_id, filename).classes("self-end")
            return
        try:
            df = pd.read_csv(io.BytesIO(content))
        except Exception as exc:
            ui.label(f"Couldn't parse {filename}: {exc}").style("color: #c0392b;")
            _output_download_link(client, job_id, filename).classes("self-end")
            return
        if df.empty:
            ui.label("(no rows)").style("color: #888;")
            _output_download_link(client, job_id, filename).classes("self-end")
            return
        _render_dataframe_table(df, declared_columns=output.get("columns"))
        _output_download_link(client, job_id, filename).classes("self-end")
        return
    ui.label("(no data)").style("color: #888;")


def _render_inline_table(raw: dict[str, Any]) -> None:
    """Render an inline dict-shaped table value as a ``ui.table``.

    Helper extracted from :func:`_render_table_output` so the
    inline path and the CSV-file path share the same emit logic.
    """
    first_value = next(iter(raw.values()))
    try:
        if isinstance(first_value, list):
            df = pd.DataFrame(raw)
        elif isinstance(first_value, dict):
            df = pd.DataFrame.from_dict(raw)
        else:
            ui.label(f"(unrecognised table shape: {type(first_value).__name__})").style(
                "color: #888;"
            )
            return
    except Exception as exc:  # pragma: no cover — defensive
        ui.label(f"(couldn't parse table: {exc})").style("color: #c0392b;")
        return
    _render_dataframe_table(df)


def _render_dataframe_table(
    df: pd.DataFrame,
    *,
    declared_columns: Any | None = None,
) -> None:
    """Emit a ``ui.table`` from a pandas DataFrame.

    ``declared_columns`` (when supplied) is the descriptor's
    ``columns`` array for table-shaped outputs — used to pin the
    visible column order and override header labels. Columns the CSV
    doesn't actually carry are dropped from the visible set rather
    than rendering as empty.
    """
    if isinstance(declared_columns, list) and declared_columns:
        visible: list[tuple[str, str]] = []
        for col in declared_columns:
            if not isinstance(col, dict):
                continue
            field_name = str(col.get("name") or "")
            if not field_name or field_name not in df.columns:
                continue
            display_label = str(col.get("label") or field_name)
            visible.append((field_name, display_label))
        if not visible:
            visible = [(str(c), str(c)) for c in df.columns]
    else:
        visible = [(str(c), str(c)) for c in df.columns]

    rows_source = df.reset_index().to_dict("records") if df.index.name else df.to_dict("records")
    if df.index.name and not isinstance(declared_columns, list):
        visible.insert(0, ("index", str(df.index.name)))
    columns = [
        {"name": field_name, "label": display_label, "field": field_name}
        for field_name, display_label in visible
    ]
    # Synthesise a per-row key so ``ui.table`` keeps sort / selection
    # state stable. The visible columns don't need to include it.
    rows = [
        {**{f: row.get(f) for f, _ in visible}, "_row": idx} for idx, row in enumerate(rows_source)
    ]
    ui.table(columns=columns, rows=rows, row_key="_row").classes("w-full").props("dense flat")


async def _render_file_output(
    client: ApiClient,
    job_id: int,
    output: dict[str, Any],
    label: str,
) -> None:
    """Dispatch a ``type=file`` output by its ``file_type``.

    Heavy outputs (the 3-D field viewer, animations, videos, large images) fetch
    their bytes server-side and build client-side, which can take several seconds
    — long enough that the panel looked blank/frozen. We show a loading spinner
    for the duration and remove it once the content is in the DOM, so the user
    always sees that something is happening.
    """
    file_type = str(output.get("file_type") or "")
    filename = str(output.get("filename") or "")
    if not filename:
        ui.label(f"{label}: (descriptor has no filename)").style("color: #888;")
        return

    loading = ui.row().classes("items-center gap-2 q-my-xs")
    with loading:
        ui.spinner(size="sm", color="primary")
        ui.label(f"Loading {label}…").style("color: #888; font-size: 0.85rem;")
    try:
        if file_type == "html_plot":
            await _render_html_plot(client, job_id, filename, label)
        elif file_type == "csv_plot":
            await _render_csv_plot(client, job_id, filename, output, label)
        elif file_type == "csv_table":
            await _render_csv_table(client, job_id, filename, output, label)
        elif file_type == "image":
            await _render_image_file(client, job_id, filename, label)
        elif file_type == "cad_file":
            await _render_cad_file_output(client, job_id, filename, label)
        elif file_type == "pdf":
            await _render_pdf_output(client, job_id, filename, label)
        elif file_type == "animation_3d":
            await _render_animation_3d_output(client, job_id, filename, label)
        elif file_type == "video":
            await _render_video_output(client, job_id, filename, label)
        elif file_type == "field":
            await _render_field_output(client, job_id, filename, output, label)
        else:
            # Unknown file_type — fall back to a download link.
            ui.label(f"{label}:").style("font-weight: 600;")
            _output_download_link(client, job_id, filename)
    finally:
        # The render can take a while (heavy field/animation); if the panel was
        # torn down meanwhile (poll re-render / navigation) the spinner's parent
        # is already gone, so deleting it would raise. Guard it.
        try:
            loading.delete()
        except (ValueError, RuntimeError):
            pass


def _field_download_link(client: ApiClient, job_id: int, filename: str) -> None:
    """Download button for a raw field file (e.g. a ``.vtu``).

    Thin wrapper over :func:`_output_download_link` (the single, bearer-aware
    download path) — right-aligned to sit under the field viewer.
    """
    _output_download_link(client, job_id, filename).classes("self-end")


async def _render_field_output(
    client: ApiClient,
    job_id: int,
    filename: str,
    output: dict[str, Any],
    label: str,
) -> None:
    """A ``field`` output — render the nodal field this run produced (ADR-0008 §4.4).

    Fetches the parsed field via the ``field-frames`` endpoint and draws it
    inline: a coloured 3-D ``Mesh3d`` surface for geometry-bearing formats
    (VTK / CGNS / Ensight), or a value-vs-node line for ``.npy``. A
    **transient** output (``field_timesteps``) gets a time slider + play/pause
    that scrubs the series — the per-run counterpart to the model page's
    predicted-field viewer (which is the *surrogate* over many runs).
    """
    fmt = str(output.get("field_format") or "").lower().lstrip(".") or "field"
    transient = bool(output.get("field_timesteps"))
    ui.label(f"{label}:").style("font-weight: 600;")
    detail = f"nodal field ({fmt})"
    if transient:
        detail += " · time-resolved series"
    ui.label(detail).style("color: #888; font-size: 0.85rem;")

    try:
        data = await client.get_job_field_frames(job_id, str(output.get("name") or ""))
    except ApiError as exc:
        ui.label(f"Couldn't render the field: {exc.detail}").style("color: #c0392b;")
        _field_download_link(client, job_id, filename)
        return

    frames: list[list[float]] = [list(f) for f in (data.get("frames") or [])]
    if not frames:
        ui.label("No field data to display.").style("color: #888;")
        _field_download_link(client, job_id, filename)
        return

    # Fixed colour / value range across the whole series, so the animation is
    # comparable frame-to-frame rather than re-normalising each step.
    all_values = [v for fr in frames for v in fr]
    c_range = (min(all_values), max(all_values)) if all_values else None

    plot = ui.plotly(themed_plotly(_field_frame_figure(data, frames[0], label, c_range)))
    plot.classes("w-full")

    if len(frames) > 1:
        _field_time_controls(plot, data, frames, label, c_range)
        # The whole series as one self-contained animation (plays in any
        # browser). This supersedes a single-frame ``.vtp`` link, which only
        # ever pointed at step 0 of a transient field.
        anim = _field_animation_figure(data, frames, label, c_range)
        anim_name = _field_animation_filename(filename)
        # Two takeaways again: the animation built from the whole series, and
        # the raw field file the tool wrote. A transient run used to offer only
        # the former, which left no way to get the data itself off the page.
        with ui.row().classes("w-full justify-end items-center gap-2"):
            _output_download_link(client, job_id, filename)
            ui.button(
                "Download animation (HTML)",
                icon="movie",
                on_click=lambda _e=None, _fig=anim, _name=anim_name: ui.download.content(
                    export_plotly_html(_fig), _name
                ),
            ).props("flat dense color=primary size=sm")
    else:
        _field_download_link(client, job_id, filename)


def _field_time_controls(
    plot: Any,
    data: dict[str, Any],
    frames: list[list[float]],
    label: str,
    c_range: tuple[float, float] | None,
) -> None:
    """Time slider + play/pause that scrubs a transient field's frames."""
    n = len(frames)
    state = {"playing": False}

    with ui.row().classes("w-full items-center gap-3"):
        ui.icon("schedule").style("color: #888;")
        ui.label("Time").style("font-size: 0.82rem; color: #888;")
        step_lbl = ui.label("step 0").style("font-size: 0.78rem; font-weight: 600;")
        slider = ui.slider(min=0, max=n - 1, value=0, step=1).classes("flex-1")

        def _show(idx: int) -> None:
            idx = max(0, min(n - 1, idx))
            step_lbl.text = f"step {idx}"
            plot.update_figure(
                themed_plotly(_field_frame_figure(data, frames[idx], label, c_range))
            )

        def _on_slide(_e: Any = None) -> None:
            _show(int(slider.value))

        slider.on("update:model-value", _on_slide, throttle=0.1)

        def _tick() -> None:
            nxt = int(slider.value) + 1
            nxt = 0 if nxt >= n else nxt
            slider.value = nxt  # moves the handle; a programmatic set won't fire the event
            _show(nxt)  # ...so redraw the frame directly here

        timer = ui.timer(0.6, _tick, active=False)

        def _toggle() -> None:
            state["playing"] = not state["playing"]
            timer.active = state["playing"]
            play_btn.props(f"icon={'pause' if state['playing'] else 'play_arrow'}")

        play_btn = ui.button(icon="play_arrow", on_click=_toggle).props("flat dense round")


def _render_inline_plot_download(fig: go.Figure, *, default_filename: str) -> None:
    """Small "Download as HTML" button under each plot on the job page.

    Renders the figure via plotly's CDN-embed mode (small file, full
    interactivity in any modern browser). The filename echoes the
    source filename so a user collecting outputs from multiple jobs
    can tell them apart at a glance.
    """
    leaf = default_filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in leaf:
        leaf = leaf.rsplit(".", 1)[0]
    out_name = f"{leaf or 'chart'}.html"
    ui.button(
        "Download as HTML",
        icon="download",
        on_click=lambda _e=None, _fig=fig, _name=out_name: ui.download.content(
            export_plotly_html(_fig), _name
        ),
    ).props("flat dense color=primary size=sm").classes("self-end")


async def _render_html_plot(client: ApiClient, job_id: int, filename: str, label: str) -> None:
    """Render a ``write_html``-style plotly file as a native ``ui.plotly``.

    The plotly HTML pulls plotly.js from a CDN, which the browser blocks
    inside our iframe (CSP / COEP); the iframe ends up empty. Instead we
    extract the figure's data + layout JSON from the embedded
    ``Plotly.newPlot(...)`` call and pass them to NiceGUI's bundled
    plotly element — same path the ``csv_plot`` renderer uses, so plots
    behave consistently across the dark theme. Falls back to a download
    link if extraction fails (non-plotly HTML, or a future plotly that
    changes its emit format).
    """
    ui.label(label).style("font-weight: 600;")
    try:
        content = await client.download_job_output(job_id, filename)
    except ApiError as exc:
        ui.label(f"Couldn't load {filename}: {exc.detail}").style("color: #c0392b;")
        _output_download_link(client, job_id, filename).classes("self-end")
        return
    if len(content) > _INLINE_HTML_CAP_BYTES:
        ui.label(
            f"This plot is {len(content) / (1024 * 1024):.1f} MB — too large to embed; "
            f"open it via the download button below."
        ).style("color: #888;")
        _output_download_link(client, job_id, filename).classes("self-end")
        return
    html_text = content.decode("utf-8", errors="replace")
    figure_dict = _extract_plotly_figure(html_text)
    if figure_dict is not None:
        fig = go.Figure(data=figure_dict["data"], layout=figure_dict["layout"])
        # Theme-aware tint + Matofx Plotly defaults (no Plotly logo, responsive).
        ui.plotly(themed_plotly(fig)).classes("w-full")
        # The file the tool actually wrote, and the chart re-exported from what
        # is on screen (themed, self-contained). They are not the same bytes,
        # so both are offered.
        with ui.row().classes("w-full justify-end items-center gap-2"):
            _output_download_link(client, job_id, filename)
            _render_inline_plot_download(fig, default_filename=filename)
        return
    # Last-resort fallback: srcdoc iframe with a hint so the operator
    # knows what's wrong rather than seeing a silent blank rectangle.
    ui.label(
        "Couldn't extract the plotly figure from this HTML — falling back to "
        "an embedded view, which may not render if the browser blocks the "
        "plotly CDN script."
    ).style("color: #888; font-size: 0.85rem;")
    # ``sandbox="allow-scripts"`` (deliberately NO ``allow-same-origin``)
    # runs the tool-produced HTML in an opaque origin so any embedded
    # script can't reach the MATOFX session cookie / DOM / storage —
    # contains stored-XSS in tool output while still letting plot scripts
    # render.
    ui.html(
        f'<iframe srcdoc="{html_escape(html_text, quote=True)}" '
        'sandbox="allow-scripts" '
        'style="width:100%; height:500px; border:0; '
        'background:white; border-radius:4px;"></iframe>',
        sanitize=False,
    ).classes("w-full")
    _output_download_link(client, job_id, filename).classes("self-end")


async def _render_csv_plot(
    client: ApiClient,
    job_id: int,
    filename: str,
    output: dict[str, Any],
    label: str,
) -> None:
    """Read a CSV output and render a plotly line chart per descriptor's plot config."""
    ui.label(label).style("font-weight: 600;")
    try:
        content = await client.download_job_output(job_id, filename)
    except ApiError as exc:
        ui.label(f"Couldn't load {filename}: {exc.detail}").style("color: #c0392b;")
        _output_download_link(client, job_id, filename).classes("self-end")
        return
    try:
        df = pd.read_csv(io.BytesIO(content))
    except Exception as exc:
        ui.label(f"Couldn't parse {filename}: {exc}").style("color: #c0392b;")
        _output_download_link(client, job_id, filename).classes("self-end")
        return
    x_col = int(output.get("xaxis_column") or 0)
    y_col = int(output.get("yaxis_column") or 1)
    if x_col >= len(df.columns) or y_col >= len(df.columns):
        ui.label(f"{filename}: column index out of range (have {len(df.columns)} columns).").style(
            "color: #c0392b;"
        )
        _output_download_link(client, job_id, filename).classes("self-end")
        return
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df.iloc[:, x_col], y=df.iloc[:, y_col], mode="lines"))
    fig.update_layout(
        title=str(output.get("plot_title") or label),
        xaxis_title=str(output.get("xaxis_title") or df.columns[x_col]),
        yaxis_title=str(output.get("yaxis_title") or df.columns[y_col]),
        height=380,
        margin={"l": 50, "r": 20, "t": 50, "b": 40},
    )
    ui.plotly(themed_plotly(fig)).classes("w-full")
    # Two different things to take away: the *data* the plot was drawn from
    # (the CSV as the tool wrote it) and the *chart* as a standalone file.
    # The picture alone is downloadable straight from Plotly's own camera
    # button on the modebar, so it needs no control of ours.
    with ui.row().classes("w-full justify-end items-center gap-2"):
        _output_download_link(client, job_id, filename)
        _render_inline_plot_download(fig, default_filename=filename)


async def _render_csv_table(
    client: ApiClient,
    job_id: int,
    filename: str,
    output: dict[str, Any],
    label: str,
) -> None:
    """Read a CSV output and render it as an inline DataFrame-style table.

    Companion of :func:`_render_csv_plot` — same fetch / pandas-parse
    path, but emits ``ui.table`` instead of a plotly figure. The
    descriptor's ``columns`` array, when present, pins the visible
    column order and header labels; otherwise the CSV's own header
    row is used verbatim.
    """
    ui.label(label).style("font-weight: 600;")
    try:
        content = await client.download_job_output(job_id, filename)
    except ApiError as exc:
        ui.label(f"Couldn't load {filename}: {exc.detail}").style("color: #c0392b;")
        _output_download_link(client, job_id, filename).classes("self-end")
        return
    try:
        df = pd.read_csv(io.BytesIO(content))
    except Exception as exc:
        ui.label(f"Couldn't parse {filename}: {exc}").style("color: #c0392b;")
        _output_download_link(client, job_id, filename).classes("self-end")
        return
    if df.empty:
        ui.label("(no rows)").style("color: #888;")
        _output_download_link(client, job_id, filename).classes("self-end")
        return
    _render_dataframe_table(df, declared_columns=output.get("columns"))
    _output_download_link(client, job_id, filename).classes("self-end")


_CAD_PREVIEW_EXTENSIONS_OUTPUTS: frozenset[str] = frozenset(
    {"stl", "stp", "step", "iges", "igs", "obj"}
)


async def _render_cad_file_output(
    client: ApiClient, job_id: int, filename: str, label: str
) -> None:
    """Inline 3-D preview for ``file_type == "cad_file"`` outputs.

    Goes through the server-side ``cad-preview`` endpoint for every
    supported source (STL/STP/STEP/IGES/OBJ) — the endpoint returns
    binary STL bytes regardless of source format, and the client
    feeds them into the one viewer
    (:func:`matofx.ui.cad_preview.render_stl_preview`). Unsupported
    formats land on the placeholder. Download link is always present
    below so the user can pull the file in its original format.
    """
    from matofx.ui.cad_preview import (  # noqa: PLC0415
        render_cad_placeholder,
        render_stl_preview,
    )

    ui.label(label).style("font-weight: 600;")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext in _CAD_PREVIEW_EXTENSIONS_OUTPUTS:
        # Server-side tessellation: the cad-preview endpoint resolves
        # the output filename against the job's working dir, converts
        # STP/STEP/IGES/OBJ to STL on the fly (or passes STL through),
        # and returns STL bytes the browser viewer consumes directly.
        try:
            stl_bytes = await client.fetch_output_cad_preview(job_id, filename=filename)
        except ApiError as exc:
            ui.label(f"Couldn't tessellate {filename}: {exc.detail}").style("color: #c0392b;")
        else:
            render_stl_preview(stl_bytes, height=420)
        _output_download_link(client, job_id, filename)
        return

    # Parasolid (``.x_t`` / ``.x_b``) and any unknown CAD extension —
    # preview pending; download link is what the user has for now.
    render_cad_placeholder(filename, ext)
    _output_download_link(client, job_id, filename)


async def _render_image_file(client: ApiClient, job_id: int, filename: str, label: str) -> None:
    """Inline preview for ``file_type == "image"`` outputs, plus a download link.

    What is on the page is a *preview*: capped in height and re-encoded
    into a data URL. Anyone collecting a run's evidence needs the original
    bytes, so every image ends on the same download control the other file
    outputs use.
    """
    ui.label(label).style("font-weight: 600;")
    try:
        content = await client.download_job_output(job_id, filename)
    except ApiError as exc:
        ui.label(f"Couldn't load {filename}: {exc.detail}").style("color: #c0392b;")
        _output_download_link(client, job_id, filename).classes("self-end")
        return
    import html as _html  # noqa: PLC0415

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    mime = _IMAGE_MIMES.get(ext, "application/octet-stream")
    data_url = f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
    # `label` is the tool descriptor's output label (author-controlled, no
    # charset restriction) rendered here with sanitize=False — escape it so it
    # can't break out of the alt attribute into an <img onerror=…> XSS.
    alt = _html.escape(label, quote=True)
    # Plain <img> with object-fit:contain so the WHOLE image is shown (a q-img
    # defaults to object-fit:cover, which crops the top/bottom of tall images
    # such as the blend animation). Aspect ratio is preserved; nothing clipped.
    ui.html(
        f'<img src="{data_url}" alt="{alt}" '
        'style="max-width:100%; max-height:600px; height:auto; width:auto; '
        'object-fit:contain; display:block; border-radius:4px;" />',
        sanitize=False,
    ).classes("w-full")
    _output_download_link(client, job_id, filename).classes("self-end")


async def _render_pdf_output(client: ApiClient, job_id: int, filename: str, label: str) -> None:
    """Inline PDF viewer for ``file_type == "pdf"`` outputs.

    Every modern browser ships a PDF viewer wired into ``<iframe>`` /
    ``<embed>``. We fetch the bytes server-side (authorisation comes
    via the api_client's bearer header, which browser-initiated
    iframe ``src`` requests can't carry) and embed them via a base64
    ``data:application/pdf`` URL so the viewer reads the bytes
    directly without a second auth round-trip.

    Above :data:`_INLINE_PDF_CAP_BYTES` the inline path is skipped —
    the user gets a friendly message + the download link below so
    very large reports don't blow the page weight up.
    """
    ui.label(label).style("font-weight: 600;")
    try:
        content = await client.download_job_output(job_id, filename)
    except ApiError as exc:
        ui.label(f"Couldn't load {filename}: {exc.detail}").style("color: #c0392b;")
        _output_download_link(client, job_id, filename).classes("self-end")
        return
    if len(content) > _INLINE_PDF_CAP_BYTES:
        ui.label(
            f"This PDF is {len(content) / (1024 * 1024):.1f} MB — too large to embed; "
            f"open it via the download link below."
        ).style("color: #888;")
    else:
        data_url = "data:application/pdf;base64," + base64.b64encode(content).decode("ascii")
        ui.html(
            f'<iframe src="{data_url}" '
            'style="width:100%; height:640px; border:0; '
            'background:white; border-radius:4px;"></iframe>',
            sanitize=False,
        ).classes("w-full")
    _output_download_link(client, job_id, filename)


async def _render_animation_3d_output(
    client: ApiClient, job_id: int, filename: str, label: str
) -> None:
    """Inline 3-D scene for ``file_type == "animation_3d"`` outputs.

    First-class formats are GLTF and GLB — three.js's GLTFLoader
    handles both natively, including any embedded ``AnimationMixer``
    keyframe tracks. NiceGUI exposes the loader via
    ``ui.scene.gltf(url)``; we feed it a base64 data URL the same way
    :func:`matofx.ui.cad_preview.render_stl_preview` does for STL.

    GLTF (the JSON flavour) may reference external textures /
    buffers that the data-URL embed can't resolve — for those, ship
    the asset as ``.glb`` (the binary single-file flavour). Other
    animated formats (``.fbx`` / ``.dae`` / ``.usdz``) fall through
    to the placeholder + download link; converting them server-side
    is a separate roadmap item.
    """
    from matofx.ui.cad_preview import render_cad_placeholder  # noqa: PLC0415
    from matofx.ui.theme import current_palette  # noqa: PLC0415

    ui.label(label).style("font-weight: 600;")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext not in _ANIM3D_EXTS:
        render_cad_placeholder(filename, ext)
        _output_download_link(client, job_id, filename)
        return

    try:
        content = await client.download_job_output(job_id, filename)
    except ApiError as exc:
        ui.label(f"Couldn't load {filename}: {exc.detail}").style("color: #c0392b;")
        _output_download_link(client, job_id, filename).classes("self-end")
        return
    if len(content) > _INLINE_ANIM3D_CAP_BYTES:
        ui.label(
            f"This animation is {len(content) / (1024 * 1024):.1f} MB — too "
            f"large to embed; open it via the download link below."
        ).style("color: #888;")
    else:
        mime = _ANIM3D_MIMES.get(ext, "application/octet-stream")
        data_url = f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
        palette = current_palette()
        scene = (
            ui.scene(background_color=palette.page_bg, grid=False)
            .classes("w-full")
            .style("height: 480px; border-radius: 4px;")
        )
        scene.gltf(data_url)
        scene.move_camera(x=2.5, y=2.5, z=2.5)
    _output_download_link(client, job_id, filename)


async def _render_video_output(client: ApiClient, job_id: int, filename: str, label: str) -> None:
    """Inline HTML5 ``<video controls>`` player for ``file_type == "video"``.

    Bytes go in via a base64 ``data:`` URL keyed off the file extension
    (``.mp4`` / ``.webm`` / ``.ogg`` / ``.mov`` / ``.m4v``). The browser
    decides whether it can decode the codec; when it can't, ``<video>``
    surfaces a generic "no playable source" hint and the download link
    below is the user's escape hatch.

    Above :data:`_INLINE_VIDEO_CAP_BYTES` the inline path is skipped so
    a multi-hundred-MB clip doesn't get base64-encoded into the page
    weight just to be scrubbed once.
    """
    ui.label(label).style("font-weight: 600;")
    try:
        content = await client.download_job_output(job_id, filename)
    except ApiError as exc:
        ui.label(f"Couldn't load {filename}: {exc.detail}").style("color: #c0392b;")
        _output_download_link(client, job_id, filename).classes("self-end")
        return
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _VIDEO_EXTS:
        ui.label(f"{filename}: unknown video format — use the download link below.").style(
            "color: #888;"
        )
    elif len(content) > _INLINE_VIDEO_CAP_BYTES:
        ui.label(
            f"This video is {len(content) / (1024 * 1024):.1f} MB — too large "
            f"to embed; open it via the download link below."
        ).style("color: #888;")
    else:
        mime = _VIDEO_MIMES.get(ext, "application/octet-stream")
        data_url = f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
        ui.html(
            f'<video controls preload="metadata" '
            f'style="width:100%; max-height:520px; background:black; '
            f'border-radius:4px;">'
            f'<source src="{data_url}" type="{mime}">'
            f"Your browser can't play this video format."
            f"</video>",
            sanitize=False,
        ).classes("w-full")
    _output_download_link(client, job_id, filename)


async def _load_outputs_doc(client: ApiClient, job_id: int) -> dict[str, Any] | None:
    """Fetch + parse ``outputs/outputs.json``; ``None`` if missing / malformed."""
    try:
        content = await client.download_job_output(job_id, "outputs.json")
    except ApiError:
        return None
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None


# ---------- download buttons (placeholder) -----------------------------------


def _render_download_row(
    job_id: int,
    descriptor: dict[str, Any],
    job_inputs: dict[str, Any],
    outputs_doc: dict[str, Any] | None,
) -> None:
    """Three legacy-style download buttons: I/O summary, output ZIP, full ZIP.

    The I/O summary is built locally from descriptor + inputs + outputs.
    The two ZIPs need new API endpoints (``/api/jobs/{id}/outputs.zip``
    and ``/api/jobs/{id}/all-files.zip``); placeholder buttons stay
    disabled until those endpoints land.
    """
    summary = _build_io_summary(descriptor, job_inputs, outputs_doc)
    summary_bytes = json.dumps(summary, indent=2).encode("utf-8")
    summary_filename = f"job_{job_id:07d}_input_output.json"

    include_provenance_state: dict[str, bool] = {"value": False}

    with ui.row().classes("w-full gap-3 q-mt-md items-start"):
        with ui.column().classes("gap-1 items-start"):
            ui.button(
                "Download Report",
                icon="description",
                on_click=lambda _e=None, _jid=job_id: _download_job_report(
                    _jid, include_provenance=include_provenance_state["value"]
                ),
            ).props("color=primary").classes("q-px-md")
            # ADR-0010 slice 6: opt-in toggle for the inlined lineage SVG.
            # Default off because the SVG is bounded but non-zero in size;
            # operators opt in when they want a self-contained provenance
            # record alongside the report. Sits below the button as a small
            # caption-sized toggle.
            ui.checkbox("Include provenance graph").bind_value(
                include_provenance_state, "value"
            ).props("dense").classes("text-caption").tooltip(
                "Add an inlined SVG of the job's lineage / provenance graph to the downloaded report."
            )
        ui.button(
            "Download Input and Output Summary",
            on_click=lambda: ui.download.content(summary_bytes, summary_filename),
            icon="download",
        ).props("color=primary").classes("q-px-md")
        ui.button(
            "Download Output Files",
            icon="folder_zip",
            on_click=lambda _e=None, _jid=job_id: _download_outputs_zip(_jid),
        ).props("color=primary").classes("q-px-md")
        ui.button(
            "Download All Tool Files",
            icon="folder_zip",
            on_click=lambda _e=None, _jid=job_id: _download_all_files_zip(_jid),
        ).props("color=primary").classes("q-px-md")


async def _download_job_report(job_id: int, *, include_provenance: bool = False) -> None:
    """Fetch the self-contained HTML report (ADR-0004) and ship to the browser.

    ``include_provenance`` adds the lineage SVG section (ADR-0010
    slice 6); driven by the inline checkbox next to the button.
    """
    client = ApiClient(get_app(), token=current_token())
    try:
        data = await client.download_job_report(job_id, include_provenance=include_provenance)
    except ApiError as exc:
        ui.notify(f"Couldn't build the report: {exc.detail}", type="negative")
        return
    stamp = _dt.datetime.now(tz=_dt.UTC).strftime("%Y%m%d")
    ui.download.content(data, f"report_job{job_id:07d}_{stamp}.html")


async def _download_outputs_zip(job_id: int) -> None:
    """Fetch the outputs ZIP via the bearer-authed api_client and ship to the browser."""
    client = ApiClient(get_app(), token=current_token())
    try:
        data = await client.download_outputs_zip(job_id)
    except ApiError as exc:
        ui.notify(f"Couldn't build the outputs ZIP: {exc.detail}", type="negative")
        return
    ui.download.content(data, f"job_{job_id:07d}_outputs.zip")


async def _download_all_files_zip(job_id: int) -> None:
    """Fetch the full-working-dir ZIP via the api_client and ship to the browser."""
    client = ApiClient(get_app(), token=current_token())
    try:
        data = await client.download_all_files_zip(job_id)
    except ApiError as exc:
        ui.notify(f"Couldn't build the full ZIP: {exc.detail}", type="negative")
        return
    ui.download.content(data, f"job_{job_id:07d}_all_files.zip")


def _build_io_summary(
    descriptor: dict[str, Any],
    job_inputs: dict[str, Any],
    outputs_doc: dict[str, Any] | None,
) -> dict[str, Any]:
    """Flatten descriptor + values into the legacy I/O summary shape."""
    inputs_by_name: dict[str, dict[str, Any]] = {
        i["name"]: i for i in (descriptor.get("inputs") or []) if i.get("name")
    }
    inputs_section: dict[str, str] = {}
    for name, value in job_inputs.items():
        spec = inputs_by_name.get(name, {})
        unit = str(spec.get("unit") or "")
        rendered = _format_input_value(value, str(spec.get("type") or ""))
        inputs_section[name] = f"{rendered} {unit}".strip()

    outputs_by_name: dict[str, dict[str, Any]] = {
        o["name"]: o for o in (descriptor.get("outputs") or []) if o.get("name")
    }
    value_lookup = _outputs_value_lookup(outputs_doc)
    outputs_section: dict[str, str] = {}
    for name, spec in outputs_by_name.items():
        entry = value_lookup.get(name, {})
        if str(spec.get("type") or "") == "number":
            unit = str(spec.get("unit") or "")
            outputs_section[name] = (
                f"{_format_number(entry.get('value'), spec.get('precision'))} {unit}".strip()
            )
        elif spec.get("filename"):
            outputs_section[name] = str(spec["filename"])
        else:
            outputs_section[name] = str(entry.get("value") or "")

    return {"inputs": inputs_section, "outputs": outputs_section}


# ---------- inflight + failed states -----------------------------------------


async def _render_inflight(
    client: ApiClient,
    job: dict[str, Any],
    job_id: int,
    body_container: ui.column,
    tool_meta: dict[str, str],
) -> None:
    """Queued / running view: status badge, cancel, streaming logs.

    **Nothing here is rebuilt on a tick.** Two bugs came from doing that.
    The status row used to be drawn once from the job as it was at page
    load, so a job that went ``queued`` → ``running`` under the user kept
    saying *Queued* until they refreshed the page by hand. And the log
    section used to be cleared and re-drawn every two seconds, which
    destroyed the ``ui.expansion`` the user had just opened and built a
    fresh, closed one in its place — so the log could not be read while
    it was being written, which is the only time anyone wants to read it.

    The fix in both cases is to build the widgets once and *update* them:
    the status row is repainted only when the state actually changes, and
    the logs append into a :class:`_LiveLogs` view that never rebuilds.
    """
    status_box = ui.column().classes("w-full gap-1")
    with status_box:
        _render_status_row(job, job_id, tool_meta)
    # Built once, outside ``status_box``: a job's name and id don't
    # change under the poller, and anything inside that box is thrown
    # away and rebuilt whenever the state does.
    _render_standalone_job_card(job, job_id)
    ui.label(
        "The job has not finished yet — this page will refresh automatically when it completes."
    ).style("color: #888;")

    logs = _LiveLogs()
    # ``shown`` is what the status row currently claims, not what the job
    # is: repainting on every tick would fight the Cancel button's own
    # click handling for no benefit.
    shown: dict[str, Any] = {
        "state": str(job.get("state") or ""),
        "cancel_requested_at": job.get("cancel_requested_at"),
    }

    async def _tick() -> None:
        try:
            fresh = await client.get_job(job_id)
        except ApiError as exc:
            ui.notify(f"Refresh failed: {exc.detail}", type="negative")
            return
        state = str(fresh.get("state") or "")
        if state in _TERMINAL_STATES:
            timer.deactivate()
            await _render_body(client, fresh, job_id, body_container)
            return
        if state != shown["state"] or _cancel_pending_changed(fresh, shown):
            shown["state"] = state
            shown["cancel_requested_at"] = fresh.get("cancel_requested_at")
            status_box.clear()
            with status_box:
                _render_status_row(fresh, job_id, tool_meta)
        if state == "running":
            await logs.poll(client, job_id)

    timer = ui.timer(_REFRESH_INTERVAL_S, _tick)
    if job["state"] == "running":
        await logs.poll(client, job_id)


#: Job states from which nothing more will happen. Held in one place
#: because the in-flight tick and My Jobs' outer safety-net poller both
#: need it, and they had already drifted — the tick treated a cancelled
#: job as still in flight, so it kept polling one that was never coming
#: back and only stopped when the slower outer timer noticed.
_TERMINAL_STATES: Final[frozenset[str]] = frozenset({"complete", "failed", "cancelled"})


def _cancel_pending_changed(fresh: dict[str, Any], shown: dict[str, Any]) -> bool:
    """Has the job picked up a cancel request since the row was drawn?

    The status row swaps the Cancel button for "waiting for the runner"
    on this field alone, so it is a repaint trigger in its own right —
    a cancel issued from another tab must not leave a live button here.
    """
    return bool(fresh.get("cancel_requested_at")) != bool(shown.get("cancel_requested_at"))


async def _render_failed(
    client: ApiClient,
    job: dict[str, Any],
    job_id: int,
    body_container: ui.column,
    tool_meta: dict[str, str],
) -> None:
    """Failed view: status row + logs (Outputs / Inputs panel skipped)."""
    _ = body_container
    _render_status_row(job, job_id, tool_meta)
    _render_standalone_job_card(job, job_id)
    ui.label("This job didn't finish successfully — see the logs below.").style("color: #ff6b6b;")
    try:
        logs = await client.get_job_logs(job_id)
    except ApiError as exc:
        logs = {"stdout": f"(could not load logs: {exc.detail})", "stderr": ""}
    _render_logs(logs)
    # Provenance is meaningful for failed jobs too — operators trace
    # back through inputs / library files / approvals to triage.
    from matofx.ui.lineage_view import render_provenance_section  # noqa: PLC0415

    render_provenance_section(kind="job", obj_id=job_id)
    if vv_tracking_enabled():
        await _render_job_vv_card(client, job_id, str(job.get("name") or ""))


# ---------- V&V card (ADR-0011 §8) ------------------------------------


_JOB_VV_KIND_LABEL: dict[str, str] = {
    "validated_against": "Validated against",
    "calibrated_from": "Calibrated from",
    "sanity_checked": "Sanity checked",
}
_VV_STATE_COLOR_JOB: dict[str, str] = {
    "draft": "grey",
    "pending": "warning",
    "approved": "positive",
    "rejected": "negative",
    "retired": "grey",
}
_VV_STATE_LABEL_JOB: dict[str, str] = {
    "draft": "Draft",
    "pending": "Pending review",
    "approved": "Approved",
    "rejected": "Rejected",
    "retired": "Retired",
}


async def _render_job_vv_card(client: ApiClient, job_id: int, job_name: str = "") -> None:
    """Per-job V&V card: every record about this run + a curator action button.

    Sits underneath the lineage section on every state (complete /
    failed / inflight). The "Record V&V" button opens the shared V&V
    authoring dialog pre-filled with this job, so a curator never has to
    re-find the run by id. Best-effort: any API failure shows nothing,
    never blocking the rest of the job page.
    """
    from matofx.ui.pages.vv_records import open_vv_create_dialog  # noqa: PLC0415

    try:
        records = await client.vv_list_job_records(job_id=job_id, include_retired=False)
    except ApiError:
        return
    try:
        me = await client.me()
    except ApiError:
        me = {}
    perms = me.get("permissions") or []
    is_author = "vv.curate" in perms or "vv.create" in perms
    job_label = f"{format_job_id(job_id)} — {job_name}" if job_name else format_job_id(job_id)

    with ui.expansion("V&V", icon="verified", value=False).classes("w-full"):
        if not records:
            ui.label("No V&V records have been recorded for this run yet.").style("color: #888;")
        else:
            for r in records:
                _render_job_vv_record_row(r)
        if is_author:
            ui.button(
                "Record V&V",
                icon="add",
                on_click=lambda: open_vv_create_dialog(
                    client, prefill_job_id=job_id, prefill_job_label=job_label
                ),
            ).props("flat dense color=primary")


def _render_job_vv_record_row(r: dict[str, Any]) -> None:
    rid = int(r.get("id") or 0)
    kind_label = _JOB_VV_KIND_LABEL.get(str(r.get("kind")), str(r.get("kind")))
    state = str(r.get("state") or "draft")
    state_label = _VV_STATE_LABEL_JOB.get(state, state.title())
    state_color = _VV_STATE_COLOR_JOB.get(state, "grey")
    with (
        ui.row()
        .classes("w-full items-center gap-3")
        .style("padding: 0.35rem 0.5rem; border-bottom: 1px solid var(--matofx-card-border);")
    ):
        ui.label(kind_label).style("font-weight: 500;")
        ui.chip(state_label).props(f"color={state_color} text-color=white dense")
        notes = str(r.get("notes") or "").strip()
        if notes:
            ui.label(notes[:140] + ("…" if len(notes) > 140 else "")).style(
                "color: #888; font-size: 0.82rem;"
            )
        ui.space()
        ui.button(
            "Open",
            icon="open_in_new",
            on_click=lambda _e=None, _rid=rid: ui.navigate.to(f"/vv-records/job/{_rid}"),
        ).props("flat dense")


# ---------- shared bits ------------------------------------------------------


def _render_status_row(job: dict[str, Any], job_id: int, tool_meta: dict[str, str]) -> None:
    """Status row: tool name + version, state badge, timestamps, optional cancel.

    The tool ``name`` (not alias) and ``version`` are taken from
    ``tool_meta`` resolved by :func:`_resolve_tool_meta` up the call
    stack. Timestamps go through ``format_user_datetime`` so the
    user-locale + UTC-bracket shape is consistent with everywhere
    else in the workspace.
    """
    state = str(job.get("state") or "")
    tool_name = tool_meta.get("name") or str(job.get("tool_alias") or "-")
    tool_version = tool_meta.get("version") or ""
    tool_label = f"Tool: {tool_name}"
    if tool_version:
        tool_label = f"{tool_label} v{tool_version}"

    with ui.row().classes("items-center gap-3 w-full"):
        ui.label(tool_label)
        ui.badge(format_job_state(state) or "?").props(_STATE_BADGE.get(state, "color=grey"))
    if job.get("started_at"):
        ui.label(f"Started: {format_user_datetime(job.get('started_at'))}").style(
            "color: #888; font-size: 0.85rem;"
        )
    if job.get("completed_at"):
        ui.label(f"Finished: {format_user_datetime(job.get('completed_at'))}").style(
            "color: #888; font-size: 0.85rem;"
        )
    if state in ("queued", "running") and not job.get("cancel_requested_at"):
        ui.button(
            "Cancel job",
            on_click=lambda _e=None, _jid=job_id: _cancel(_jid),
        ).props("color=warning")
    elif job.get("cancel_requested_at"):
        ui.label("Cancel requested — waiting for the runner to act.").style("color: #c0392b;")


def _render_logs(logs: dict[str, str]) -> None:
    """Static log view for a job that has finished.

    Whole-file and rendered once, so ``ui.code`` is right here: it gives
    the copy button, and there is nothing to append. The live view is a
    different shape entirely — see :class:`_LiveLogs`.

    ``language=None``: this is solver output, and syntax-highlighting it
    as Python coloured random words in a CFD residual table.
    """
    ui.label("Logs").style("font-size: 1.1rem; font-weight: 600;")
    with ui.expansion("stdout", icon="article").classes("w-full"):
        ui.code(logs.get("stdout") or "(no stdout output)", language=None).classes("w-full")
    with ui.expansion("stderr", icon="warning").classes("w-full"):
        ui.code(logs.get("stderr") or "(no stderr output)", language=None).classes("w-full")
    # The launcher log carries the auto-generated launcher script's
    # own output (typically empty — but it's the only place a launcher
    # startup failure shows up when stdout/stderr never get opened).
    # Default-collapsed so it doesn't clutter the normal "my job ran
    # fine" view; the icon flags it as diagnostic.
    launcher = logs.get("launcher") or ""
    if launcher.strip():
        with ui.expansion("launcher (diagnostic)", icon="bug_report").classes("w-full"):
            ui.code(launcher, language=None).classes("w-full")


#: Lines held in a live log view before the oldest are dropped. A chatty
#: solver can emit hundreds of thousands of lines in a long run and the
#: browser will not hold them all. The full log is on the shared mount
#: and the finished-job view reads it whole, so nothing is lost — but
#: the view has to *say* it is showing a window, or a truncated log is
#: indistinguishable from a complete one.
_LIVE_LOG_MAX_LINES: Final[int] = 5000

#: Height of a live log pane. Tall enough to read a solver's residual
#: block without scrolling, short enough that three panes still fit.
_LIVE_LOG_STYLE: Final[str] = (
    "height: 22rem; width: 100%; font-family: ui-monospace, SFMono-Regular, "
    "Menlo, Consolas, monospace; font-size: 0.8rem; white-space: pre;"
)


class _LiveLogs:
    """A log view for a running job that appends instead of rebuilding.

    The whole point is that **nothing is ever destroyed**. The previous
    implementation cleared its container and re-ran the render on every
    poll, which meant the ``ui.expansion`` the user had opened to read
    stdout was thrown away and replaced by a new, closed one every two
    seconds. You could not read a log while it was being written.

    So the sections are built at most once each and then only pushed to.
    ``ui.log`` is the right element for that: it appends lines without
    re-transmitting the history, and its client-side half already follows
    the tail *only while the viewer is at the bottom* — scroll up to read
    something and it stops moving under you; scroll back down and it
    resumes. That behaviour is the feature, and it is why this is not
    just a ``ui.code`` whose content gets reassigned.

    Only the bytes written since the last poll are fetched, so a solver
    log that grows to hundreds of megabytes costs a few hundred bytes a
    tick rather than being re-read and re-sent in full every two seconds.
    """

    def __init__(self) -> None:
        self._container = ui.column().classes("w-full gap-2")
        self._heading: ui.label | None = None
        self._views: dict[str, ui.log] = {}
        self._offsets: dict[str, int] = {"stdout": 0, "stderr": 0, "launcher": 0}
        self._failed = False

    async def poll(self, client: ApiClient, job_id: int) -> None:
        """Fetch what is new and append it. Never raises."""
        try:
            payload = await client.get_job_logs(job_id, since=self._offsets, tail=True)
        except ApiError as exc:
            self._report(f"(could not load logs: {exc.detail})")
            return
        self._failed = False
        if payload.get("truncated"):
            # The log was rotated or the job re-run beneath us. Appending
            # would splice two different runs into one scrollback, so
            # start the view again rather than show a plausible lie.
            for view in self._views.values():
                view.clear()
        for stream in ("stdout", "stderr", "launcher"):
            self._offsets[stream] = int(payload.get(f"{stream}_offset") or 0)
            chunk = str(payload.get(stream) or "")
            if chunk:
                # Pushed verbatim: ``push`` splits on ``splitlines()``,
                # which already drops the terminating newline and keeps a
                # genuine blank line ("A\nB\n\n" → A, B, ""). Trimming the
                # tail first — ``rstrip("\n")`` especially — deletes the
                # blank lines a solver uses to separate its blocks.
                self._view(stream).push(chunk)

    def _report(self, message: str) -> None:
        """Surface a fetch failure once, not once per tick."""
        if self._failed:
            return
        self._failed = True
        self._view("stdout").push(message)

    def _view(self, stream: str) -> ui.log:
        """The pane for ``stream``, built on first use and then reused.

        Built lazily so a job with an empty stderr — the normal case —
        does not carry an empty section, and the launcher pane appears
        only if the launcher ever said anything.
        """
        existing = self._views.get(stream)
        if existing is not None:
            return existing
        with self._container:
            if self._heading is None:
                self._heading = ui.label("Logs").style("font-size: 1.1rem; font-weight: 600;")
                ui.label(
                    f"Updating live. Showing the most recent {_LIVE_LOG_MAX_LINES:,} lines — "
                    "the full log is available once the job finishes. Scroll up to read; "
                    "the view follows new output only while you are at the bottom."
                ).style("color: #888; font-size: 0.8rem;")
            title, icon = _LOG_SECTIONS[stream]
            # stdout is what the user came to read while the job runs, so
            # it opens by default; the other two stay out of the way.
            with ui.expansion(title, icon=icon, value=stream == "stdout").classes("w-full"):
                view = ui.log(max_lines=_LIVE_LOG_MAX_LINES).style(_LIVE_LOG_STYLE)
        self._views[stream] = view
        return view


_LOG_SECTIONS: Final[dict[str, tuple[str, str]]] = {
    "stdout": ("stdout", "article"),
    "stderr": ("stderr", "warning"),
    "launcher": ("launcher (diagnostic)", "bug_report"),
}


async def _cancel(job_id: int) -> None:
    """Send the cancel request; show a toast either way."""
    client = ApiClient(get_app(), token=current_token())
    try:
        await client.cancel_job(job_id)
    except ApiError as exc:
        ui.notify(f"Cancel failed: {exc.detail}", type="negative")
        return
    ui.notify("Cancel requested.", type="positive")


# ---- KPI extraction (per-job Headline KPIs) -------------------------

#: Whether to draw the 30-day history sparkline beneath each KPI tile.
#: Hidden for now — auto-scaling to each KPI's own range made tiny
#: run-to-run variation read as a confusing squiggle. The renderer and
#: timeseries wiring stay intact; flip this back to ``True`` to restore
#: the thumbnails if a customer asks for the trend.
_SHOW_KPI_SPARKLINES = False


async def _render_job_kpi_card(client: ApiClient, job: dict[str, Any], job_id: int) -> None:
    """Per-job Headline KPIs card — large-numeric tiles + 30-day sparkline.

    Sits below the V&V card on completed jobs (skipped on inflight /
    failed — see :func:`_render_complete` only). Reads the warehouse
    via the ``job_ids=[job_id]`` allow-list to get the captured
    values; per-KPI sparklines pull the last 30 days of points from
    the timeseries endpoint and render an inline mini-chart.

    Best-effort: any API failure shows nothing rather than blocking
    the rest of the page.
    """
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    tool_alias = job.get("tool_alias")
    if not tool_alias:
        return
    try:
        catalog = await client.list_tool_kpis(tool_alias)
        page = await client.list_tool_results(tool_alias, job_ids=[job_id], page_size=1)
    except ApiError:
        return
    entries = catalog.get("entries") or []
    rows = page.get("rows") or []
    if not entries or not rows:
        return  # tool has no KPIs, or this job has no row yet
    captured = rows[0].get("kpi_values") or {}
    captured_units = rows[0].get("kpi_units") or {}

    with ui.expansion("Headline KPIs", icon="leaderboard", value=True).classes("w-full"):
        with ui.row().classes("w-full flex-wrap gap-3"):
            for e in entries:
                name = e["kpi_name"]
                label = e.get("kpi_label") or name
                unit = captured_units.get(name) or e.get("kpi_unit")
                value = captured.get(name)
                with ui.card().classes("min-w-44"):
                    with ui.column().classes("gap-0"):
                        ui.label(label).style(
                            "color:#888; font-size:0.75rem; "
                            "font-weight:600; text-transform:uppercase;"
                        )
                        if value is None:
                            ui.label("—").style("font-size:1.5rem; font-weight:600; color:#888;")
                        else:
                            ui.label(_format_kpi_value(value)).style(
                                "font-size:1.5rem; font-weight:600;"
                            )
                        if unit:
                            ui.label(unit).style("color:#888; font-size:0.7rem;")

        # 30-day sparkline per KPI — call timeseries endpoint.
        # Hidden behind a flag for now; the numeric tiles above are the
        # whole card while this is off.
        if not _SHOW_KPI_SPARKLINES:
            return
        completed_from = (datetime.now(tz=UTC) - timedelta(days=30)).isoformat()
        with ui.row().classes("w-full flex-wrap gap-3 mt-2"):
            for e in entries:
                name = e["kpi_name"]
                try:
                    body = await client.tool_results_timeseries(
                        tool_alias,
                        kpi_name=name,
                        completed_from=completed_from,
                    )
                except ApiError:
                    continue
                points = body.get("points") or []
                if len(points) < 2:
                    continue
                ui.html(
                    _build_sparkline_svg(
                        [
                            float(p["value_numeric"])
                            for p in points
                            if p.get("value_numeric") is not None
                        ],
                        label=e.get("kpi_label") or name,
                    ),
                    sanitize=False,
                ).classes("inline-block")


def _format_kpi_value(value: Any) -> str:
    """Format a captured KPI value for the per-job card."""
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    # Compact: show up to 4 significant digits, no trailing zeros.
    return f"{v:g}"


def _build_sparkline_svg(values: list[float], *, label: str) -> str:
    """Render a tiny inline SVG sparkline for the last-30-days trend.

    Pure SVG, no external libraries — avoids paying the Plotly bundle
    weight for an indicator chart. Returns the SVG string the page
    embeds via ``ui.html(..., sanitize=False)``.
    """
    if not values:
        return ""
    width, height, pad = 160, 32, 2
    lo = min(values)
    hi = max(values)
    span = (hi - lo) or 1.0
    step = (width - 2 * pad) / max(1, len(values) - 1)
    pts: list[str] = []
    for i, v in enumerate(values):
        x = pad + i * step
        y = pad + (height - 2 * pad) * (1 - (v - lo) / span)
        pts.append(f"{x:.1f},{y:.1f}")
    import html as _html  # noqa: PLC0415

    polyline = (
        f'<polyline fill="none" stroke="#0f766e" stroke-width="1.5" points="{" ".join(pts)}" />'
    )
    title = _html.escape(label)
    return (
        f'<svg width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" aria-label="{title} sparkline">'
        f"{polyline}</svg>"
    )
