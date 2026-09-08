"""Help Documentation pages.

Two routes render the single-source Markdown tree (:mod:`matofx.docs`)
natively with :func:`nicegui.ui.markdown` — no extra runtime dependency:

* ``/help`` — the signed-in **hub**. The left pane is a role-filtered
  table of contents (the **User guide** for everyone; the
  **Administrator guide** only when :func:`matofx.ui.state.is_admin`).
* ``/getting-started`` — the **public** set, reachable *before* sign-in
  (linked from the login page). It renders only the ``public`` set and
  resolves ``?topic=`` within that set, so the user / admin guides can
  never be read unauthenticated.

A chapter is chosen via ``?topic=<slug>``. On the hub, an ``admin`` slug
requested by a non-admin falls back to the hub root (gate in
:meth:`matofx.docs.models.DocTree.visible_topic`).

See ``decisions/0014-documentation-system.md`` and
``designs/documentation.md``.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote

import markdown2
from nicegui import ui

from matofx.docs import DOC_RELEASE, get_doc_tree, search_topics
from matofx.docs.models import DocSet, DocTopic
from matofx.ui.pages._chrome import (
    card_header,
    render_public_docs_header,
    render_shell,
    require_logged_in,
)
from matofx.ui.state import is_admin

_HELP_PATH = "/help"
_PUBLIC_PATH = "/getting-started"


@ui.page("/help")
async def help_page(topic: str = "") -> None:
    """Render the signed-in documentation hub, optionally opened on ``?topic=``."""
    if not require_logged_in():
        return
    render_shell()

    tree = get_doc_tree()
    include_admin = is_admin()
    sets = tree.in_app_sets(include_admin=include_admin)

    selected = tree.visible_topic(topic, include_admin=include_admin) if topic else None
    if selected is None:
        selected = _default_topic(sets)

    _render_doc_body(
        "Help Documentation",
        "Browse the User guide — and, for administrators, the Administrator guide.",
        sets,
        selected,
        base_path=_HELP_PATH,
        searchable=True,
    )


@ui.page("/getting-started")
async def public_help_page(topic: str = "") -> None:
    """Render the public 'Getting started' set — reachable before sign-in.

    Only the ``public`` set is rendered, and ``?topic=`` is resolved
    *within* it, so a slug from the user / admin guides can't be read
    here without authenticating.
    """
    render_public_docs_header()

    public = get_doc_tree().public_set()
    sets: tuple[DocSet, ...] = (public,) if public is not None else ()
    selected: DocTopic | None = None
    if public is not None:
        selected = (public.topic(topic) if topic else None) or public.first()

    _render_doc_body(
        "Getting started with MATOFX SPDM™",
        "The essentials to get you signed in and find your way around.",
        sets,
        selected,
        base_path=_PUBLIC_PATH,
        download=_download_getting_started,
    )


# Standalone-download styling for the Getting Started guide: the brand
# typeface + palette baked in so the saved file reads like the app even
# when opened offline (navy body text, teal headings, a centred column).
_DOWNLOAD_CSS = (
    "*{box-sizing:border-box;}"
    "body{font-family:'Segoe UI',-apple-system,system-ui,'Helvetica Neue',sans-serif;"
    "color:#07203F;background:#ffffff;margin:0;padding:2.5rem 1rem 4rem;line-height:1.55;}"
    "main,.doc-head,.doc-foot{max-width:760px;margin:0 auto;}"
    ".doc-head{border-bottom:2px solid #40A1A4;padding-bottom:1rem;margin-bottom:1.5rem;}"
    ".doc-title{font-size:1.9rem;font-weight:700;margin:0 0 .35rem;}"
    ".doc-sub{color:#5a6b7b;font-size:1rem;margin:0;}"
    ".chapter{padding:1.2rem 0;border-bottom:1px solid #e3e8ec;}"
    ".chapter:last-child{border-bottom:none;}"
    "h1{font-size:1.5rem;font-weight:650;margin:.2rem 0 .8rem;}"
    "h2{font-size:1.2rem;font-weight:650;color:#2D7682;margin:1.3rem 0 .5rem;}"
    "h3{font-size:1.05rem;font-weight:650;margin:1rem 0 .4rem;}"
    "p,li{font-size:.97rem;}"
    "a{color:#2D7682;}"
    "blockquote{border-left:3px solid #6DCDC4;margin:.8rem 0;padding:.2rem 0 .2rem 1rem;"
    "color:#5a6b7b;background:#f4fbfb;}"
    "code{background:#f0f3f5;border-radius:4px;padding:.1rem .3rem;font-size:.9em;}"
    ".doc-foot{margin-top:2rem;color:#8a98a5;font-size:.8rem;text-align:center;}"
)


def _download_getting_started() -> None:
    """Ship the Getting Started guide as a self-contained HTML file.

    A single, dependency-free document the reader can keep offline or
    print to PDF from their browser — built from the same ``public`` set
    Markdown the page renders, so the two never drift.
    """
    html = _build_getting_started_html()
    ui.download.content(html.encode("utf-8"), "MATOFX-SPDM-Getting-Started.html")


def _build_getting_started_html() -> str:
    """Render the public 'Getting started' set into one standalone HTML page."""
    public = get_doc_tree().public_set()
    topics = public.topics if public is not None else ()
    chapters = "\n".join(
        '<section class="chapter">'
        + markdown2.markdown(topic.body, extras=["tables", "fenced-code-blocks", "cuddled-lists"])
        + "</section>"
        for topic in topics
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Getting started with MATOFX SPDM&trade;</title>"
        "<style>" + _DOWNLOAD_CSS + "</style></head><body>"
        '<header class="doc-head">'
        '<h1 class="doc-title">Getting started with MATOFX SPDM&trade;</h1>'
        '<p class="doc-sub">The essentials to get you signed in and find your way around.</p>'
        "</header><main>" + chapters + "</main>"
        '<footer class="doc-foot">Documentation for MATOFX ' + DOC_RELEASE + "</footer>"
        "</body></html>"
    )


def _default_topic(sets: tuple[DocSet, ...]) -> DocTopic | None:
    """First topic of the first non-empty set, or ``None``."""
    for doc_set in sets:
        first = doc_set.first()
        if first is not None:
            return first
    return None


def _render_doc_body(
    heading: str,
    subtitle: str,
    sets: tuple[DocSet, ...],
    selected: DocTopic | None,
    *,
    base_path: str,
    searchable: bool = False,
    download: Callable[[], None] | None = None,
) -> None:
    """Shared two-pane layout: header, TOC on the left, chapter on the right.

    Styled to the home-redesign idiom (ADR-0016): a bold title + muted
    subtitle header above two themed cards with icon-chip headers, all
    colours pulled from the theme CSS variables so the page tracks the
    active light/dark theme.

    When ``download`` is given, a right-aligned **Download guide** button
    sits beside the title — used only for the Getting Started guide so a
    reader can keep an offline copy.
    """
    with ui.column().classes("w-full gap-4").style("padding: 0.85rem 1.5rem 1.5rem;"):
        with ui.row().classes("w-full items-start justify-between no-wrap").style("gap: 1rem;"):
            with ui.column().classes("gap-0").style("min-width: 0;"):
                ui.label(heading).style(
                    "font-size: 1.6rem; font-weight: 650; line-height: 1.1; margin-top: -2px;"
                )
                ui.label(subtitle).style(
                    "color: var(--matofx-muted-text); font-size: 0.9rem; margin-top: 0.15rem;"
                )
            if download is not None:
                ui.button("Download guide", icon="download", on_click=download).props(
                    "outline dense color=primary"
                ).style("flex: none; margin-top: 2px;")
        with ui.row().classes("w-full items-start gap-4 no-wrap"):
            with ui.column().classes("gap-2").style("flex: 0 0 300px; min-width: 0;"):
                _render_left_pane(sets, selected, base_path=base_path, searchable=searchable)
            with ui.column().classes("gap-2").style("flex: 1 1 0; min-width: 0;"):
                _render_content(selected)


def _card_header(icon: str, title: str) -> None:
    """Icon-chip card header matching the home-page cards (title truncates)."""
    card_header(icon, title, ellipsis=True)


def _render_left_pane(
    sets: tuple[DocSet, ...],
    selected: DocTopic | None,
    *,
    base_path: str,
    searchable: bool,
) -> None:
    """The left card: an optional search box above the table of contents."""
    with ui.card().classes("w-full").style("padding: 0; gap: 0;"):
        _card_header("list_alt", "Contents")
        with ui.column().classes("w-full").style("padding: 0.8rem; gap: 0;"):
            if not sets:
                ui.label("No documentation is available yet.").style(
                    "color: var(--matofx-muted-text);"
                )
                return
            if searchable:
                _render_search(sets, selected, base_path=base_path)
            else:
                _render_toc(sets, selected, base_path=base_path)


def _render_search(sets: tuple[DocSet, ...], selected: DocTopic | None, *, base_path: str) -> None:
    """Search box that swaps the table of contents for a results list.

    Filtering is server-side over the in-memory tree (dozens of chapters,
    so it's cheap) and scoped to ``sets`` — the same role-filtered sets the
    hub renders — so a search can never surface a chapter the user can't
    otherwise see.
    """
    search = (
        ui.input(placeholder="Search the documentation…").props("dense clearable").classes("w-full")
    )
    results = ui.column().classes("w-full gap-0")
    results.set_visibility(False)
    toc = ui.column().classes("w-full gap-0")
    with toc:
        _render_toc(sets, selected, base_path=base_path)

    all_topics = [topic for doc_set in sets for topic in doc_set.topics]

    def _on_change() -> None:
        needle = (search.value or "").strip()
        results.clear()
        if not needle:
            results.set_visibility(False)
            toc.set_visibility(True)
            return
        toc.set_visibility(False)
        results.set_visibility(True)
        matches = search_topics(all_topics, needle)
        with results:
            if not matches:
                ui.label("No matching chapters.").style(
                    "color: var(--matofx-muted-text); font-size: 0.85rem; padding: 0.3rem 0;"
                )
            for topic in matches:
                _render_search_result(topic, base_path=base_path)

    search.on_value_change(lambda _e: _on_change())


def _render_search_result(topic: DocTopic, *, base_path: str) -> None:
    """One result row: title over a muted summary, click to open."""
    row = (
        ui.column()
        .classes("w-full cursor-pointer rounded")
        .style("padding: 0.3rem 0.5rem; gap: 0.1rem;")
        .on("click", lambda _e=None, _s=topic.slug: _open_topic(base_path, _s))
    )
    with row:
        ui.label(topic.title).style("font-size: 0.9rem; font-weight: 600;")
        if topic.summary:
            ui.label(topic.summary).style("color: var(--matofx-muted-text); font-size: 0.78rem;")


def _render_toc(sets: tuple[DocSet, ...], selected: DocTopic | None, *, base_path: str) -> None:
    """Table of contents: one collapsible group per set (no surrounding card)."""
    selected_slug = selected.slug if selected is not None else ""
    for doc_set in sets:
        expansion = ui.expansion(doc_set.title, value=True).classes("w-full")
        expansion.style("font-weight: 600;")
        with expansion, ui.column().classes("w-full gap-0"):
            if not doc_set.topics:
                ui.label("Coming soon.").style(
                    "color: var(--matofx-muted-text); font-size: 0.85rem;"
                )
            for chapter in doc_set.topics:
                _render_toc_entry(
                    chapter,
                    is_current=(chapter.slug == selected_slug),
                    base_path=base_path,
                )


def _render_toc_entry(chapter: DocTopic, *, is_current: bool, base_path: str) -> None:
    """One clickable chapter row in the table of contents."""
    weight = "600" if is_current else "400"
    row = (
        ui.row()
        .classes("items-center w-full cursor-pointer rounded")
        .style(
            "padding: 0.3rem 0.5rem; gap: 0.4rem;"
            + ("background-color: rgba(64,161,164,0.15);" if is_current else "")
        )
        .on("click", lambda _e=None, _s=chapter.slug: _open_topic(base_path, _s))
    )
    with row:
        ui.label(chapter.title).style(f"font-size: 0.9rem; font-weight: {weight};")
    if chapter.summary:
        row.tooltip(chapter.summary)


def _open_topic(base_path: str, slug: str) -> None:
    """Navigate to a chapter by slug (full page load keeps the URL shareable)."""
    ui.navigate.to(f"{base_path}?topic={quote(slug, safe='')}")


#: Toned-down heading sizes for rendered chapters: `ui.markdown` emits
#: real h1-h4, whose browser defaults are far too large for a docs pane.
_DOC_CONTENT_CSS = (
    "<style>"
    ".matofx-doc{color:var(--matofx-body-text);}"
    ".matofx-doc h1{font-size:1.45rem;font-weight:600;margin:0.2rem 0 0.7rem;}"
    ".matofx-doc h2{font-size:1.18rem;font-weight:600;margin:1.1rem 0 0.4rem;}"
    ".matofx-doc h3{font-size:1.02rem;font-weight:600;margin:0.8rem 0 0.3rem;}"
    ".matofx-doc h4{font-size:0.92rem;font-weight:700;margin:0.6rem 0 0.2rem;}"
    ".matofx-doc p,.matofx-doc li{font-size:0.92rem;line-height:1.55;}"
    "</style>"
)


def _render_content(selected: DocTopic | None) -> None:
    """Right-pane rendered chapter, plus the version footer."""
    ui.add_head_html(_DOC_CONTENT_CSS)
    title = selected.title if selected is not None else "Documentation"
    with ui.card().classes("w-full").style("padding: 0; gap: 0;"):
        _card_header("menu_book", title)
        with ui.column().classes("w-full").style("padding: 1rem; gap: 0.6rem;"):
            if selected is None:
                ui.label("Select a chapter on the left to start reading.").style(
                    "color: var(--matofx-muted-text);"
                )
            else:
                ui.markdown(selected.body).classes("matofx-doc")
            ui.separator().style("background: var(--matofx-card-border);")
            ui.label(f"Documentation for MATOFX {DOC_RELEASE}").style(
                "color: var(--matofx-muted-text); font-size: 0.8rem;"
            )
