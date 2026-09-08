"""Demo group pages — sample-file viewer for the rich output renderers.

The four Demo pages in the sidebar share the same shape: scan a
category folder under :func:`_demo_assets.resolve_demo_root`, present
a picker of the files inside, and render the selection inline using
the exact same helpers the production output panels use
(:mod:`matofx.ui.cad_preview`). Dropping a file into the matching
subfolder is the entire authoring loop — no descriptor, no job, no
upload widget.

Why share one module across four pages: the pages all need the same
"folder is empty / pick a file / render inline" plumbing, and the
empty-state copy needs to be consistent so a user clicking around
sees one message style across the group. Keeping them together also
makes it cheap to add a new demo category later — add a
:class:`_demo_assets.DemoCategory` constant and a few lines here.
"""

from __future__ import annotations

import base64
from typing import Any

from nicegui import ui

from matofx.ui.cad_preview import (
    render_cad_placeholder,
    render_gltf_preview,
    render_pdf_preview,
    render_stl_preview,
    render_video_preview,
)
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.pages._demo_assets import (
    CATEGORY_ANIM3D,
    CATEGORY_CAD,
    CATEGORY_IMAGES,
    CATEGORY_PDF,
    CATEGORY_VIDEO,
    DemoCategory,
    demo_file_url,
    list_files,
    read_file,
    resolve_demo_root,
)

_KB_PER_MB = 1024
"""Above this many KB a file size reads in MB."""

# Note: the ``/_demo-asset/...`` static mount is registered eagerly
# from ``matofx.ui.app.mount_ui`` — we tried wiring it here via
# ``nicegui.app.on_startup`` first, but the hook didn't fire under
# the matofx app's lifespan, so the URLs were 404'ing silently.

_IMAGE_MIMES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "webp": "image/webp",
    "svg": "image/svg+xml",
}


# ---------- /demo/cad — CAD + Image ----------------------------------------


@ui.page("/demo/cad")
def demo_cad_page() -> None:
    """CAD + Image preview demo.

    Two pickers stacked vertically — CAD files render via the
    legacy-equivalent 3-D viewer (STL passes through, STP / STEP /
    IGES / OBJ tessellate via :mod:`matofx.tools.cad_convert`),
    images embed as base64 data URLs the same way the upload widget
    and the job page do.
    """
    if not require_logged_in():
        return
    render_shell()
    with _page_container("CAD + Image preview"):
        _render_category_section(
            CATEGORY_CAD,
            empty_hint="Drop .stl / .stp / .step / .iges / .obj files here.",
            render_fn=_render_cad_file,
        )
        _render_category_section(
            CATEGORY_IMAGES,
            empty_hint="Drop .png / .jpg / .gif / .bmp / .webp / .svg files here.",
            render_fn=_render_image_file,
        )


# ---------- /demo/gltf — 3D animation + Video -------------------------------


@ui.page("/demo/gltf")
def demo_gltf_page() -> None:
    """3-D animation + Video demo.

    Top of page renders a "test pattern" — a known-good public GLB
    (Khronos's animated *DamagedHelmet* sample, served from the
    KhronosGroup GitHub releases) — so the model-viewer mechanism is
    visible *separately* from the local file-picker path. If the test
    pattern doesn't render, the issue is upstream (CDN blocked, CSP
    refused the web component, etc.), not in your local file.
    """
    if not require_logged_in():
        return
    render_shell()
    with _page_container("3-D animation + Video"):
        _render_anim3d_test_pattern()
        ui.label(
            "Note for .gltf: the JSON file references its geometry buffer "
            "(.bin) and textures (.jpg / .png) by relative filename. Drop "
            "every referenced file into the animation_3d/ folder alongside "
            "the .gltf, or the model will load as a plain untextured form. "
            "Prefer .glb when you can — it bundles geometry, animation, and "
            "textures into one self-contained binary."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem; max-width: 56rem;")
        _render_category_section(
            CATEGORY_ANIM3D,
            empty_hint=(
                "Drop .gltf / .glb files here. For .gltf, also drop its "
                ".bin and texture files (.jpg / .png) in the same folder."
            ),
            render_fn=_render_anim3d_file,
        )
        _render_category_section(
            CATEGORY_VIDEO,
            empty_hint="Drop .mp4 / .webm / .ogg / .mov files here.",
            render_fn=_render_video_file,
        )


# ---------- /demo/pdf — PDF viewer -----------------------------------------


@ui.page("/demo/pdf")
def demo_pdf_page() -> None:
    """PDF viewer demo — picker + preview."""
    if not require_logged_in():
        return
    render_shell()
    with _page_container("PDF viewer"):
        _render_category_section(
            CATEGORY_PDF,
            empty_hint="Drop .pdf files here.",
            render_fn=_render_pdf_file,
        )


# ---------- /demo/web — embedded web tool ----------------------------------


@ui.page("/demo/web")
def demo_web_page() -> None:
    """Embed a hardcoded URL inside an iframe.

    Goes full-width (bypasses the standard 1024 px page max-width)
    because most websites' responsive CSS picks the mobile / tablet
    layout below ~1280 px — leaving the page narrow makes the
    embedded site look "squashed" compared to opening it directly in
    a browser tab.
    """
    if not require_logged_in():
        return
    render_shell()
    with _page_container("Web tool", full_width=True):
        ui.label(
            "Embed an external URL inline. Useful for stitching a "
            "third-party dashboard or visualiser into a workflow without "
            "leaving the Matofx shell."
        ).style("color: var(--matofx-muted-text);")
        ui.label(
            "Heads-up: the embedded site must permit framing. Sites that "
            "send X-Frame-Options: DENY or a strict Content-Security-Policy "
            "frame-ancestors directive (Google, Facebook, GitHub, and most "
            "auth-gated dashboards) will load but render blank."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem; max-width: 48rem;")

        # 600 px tall (doubled from the original 300 px) gives the
        # embedded page enough vertical room to look natural instead
        # of cramped. The iframe stretches to the full page width
        # because the container above has ``full_width=True``.
        # Placeholder embed target. Uses the RFC-2606 reserved `example.com`
        # domain, which is guaranteed never to be registered by a third party
        # — a hardcoded arbitrary domain here would be an authenticated-page
        # embed of whoever happens to own it (typosquat / takeover risk).
        # Swap this for the real dashboard URL when wiring a specific demo.
        ui.html(
            '<iframe src="https://example.com" '
            'style="width:100%; height:600px; border:1px solid #555; '
            'border-radius:4px; background:white;"></iframe>',
            sanitize=False,
        ).classes("w-full")


# ---------- shared shell ---------------------------------------------------


def _page_container(title: str, *, full_width: bool = False) -> Any:
    """Outer page chrome shared by all four demo pages.

    Surfaces the resolved demo-asset root so an operator wondering
    "where do I drop files" can see the path without grepping.

    Args:
        title: Heading shown at the top of the page.
        full_width: When ``True``, drop the 1024 px max-width that
            the picker pages use so the body fills the full browser
            viewport. Wired by ``/demo/web`` because a narrow column
            triggers most websites' mobile / tablet responsive
            layout — the embedded site looks squashed compared to
            opening it in a normal tab.
    """
    root = resolve_demo_root()
    width_classes = "w-full p-6 gap-5"
    if not full_width:
        # ``max-w-5xl mx-auto`` = ~1024 px column centred horizontally.
        # Comfortable reading width for the picker pages where the
        # body is mostly text + a moderately-sized preview.
        width_classes += " max-w-5xl mx-auto"
    column = ui.column().classes(width_classes)
    with column:
        ui.label(title).style("font-size: 1.6rem; font-weight: 600;")
        ui.label(f"Sample files are read from: {root}").style(
            "color: var(--matofx-muted-text); font-size: 0.85rem;"
        )
    return column


def _render_category_section(
    category: DemoCategory,
    *,
    empty_hint: str,
    render_fn: Any,
) -> None:
    """One picker + viewer block for a single category.

    Layout: card containing the category label, a select with the
    files in that folder, and a viewer area that re-renders when the
    selection changes. Empty folder shows the drop-files hint instead
    of an empty select.
    """
    files = list_files(category)
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-3"):
        ui.label(category.label).style("font-weight: 600; font-size: 1.1rem;")
        if not files:
            ui.label(empty_hint).style("color: var(--matofx-muted-text); font-size: 0.9rem;")
            return

        viewer = ui.element("div").classes("w-full")

        def _redraw(selected: str | None) -> None:
            viewer.clear()
            if not selected:
                with viewer:
                    ui.label("Pick a file above to preview it.").style(
                        "color: var(--matofx-muted-text);"
                    )
                return
            try:
                content = read_file(category, selected)
            except (FileNotFoundError, ValueError, OSError) as exc:
                with viewer:
                    ui.label(f"Couldn't load {selected}: {exc}").style(
                        "color: var(--matofx-error-text);"
                    )
                return
            with viewer:
                render_fn(content=content, filename=selected)

        initial = files[0]
        ui.select(
            options=files,
            value=initial,
            label="File",
            on_change=lambda e: _redraw(str(e.value) if e.value else None),
        ).classes("w-full max-w-md")
        _redraw(initial)


# ---------- per-category renderers -----------------------------------------


def _render_cad_file(*, content: bytes, filename: str) -> None:
    """Render any of the five supported CAD formats as a 3-D scene.

    STL bytes pass straight to :func:`render_stl_preview`; STP / STEP /
    IGES / OBJ go through :func:`matofx.tools.cad_convert.convert_to_stl`
    first (same code path the live ``/cad-preview`` endpoint uses).
    Unrecognised extensions fall to :func:`render_cad_placeholder`.
    """
    ext = _ext_of(filename)
    if ext == "stl":
        render_stl_preview(content, height=520)
        _filename_label(filename, len(content))
        return
    if ext in {"stp", "step", "iges", "igs", "obj"}:
        # Local import: cad_convert pulls in cadquery / OCP / trimesh
        # which are heavy and not needed for the other demo pages.
        from matofx.tools.cad_convert import convert_to_stl  # noqa: PLC0415

        try:
            stl_bytes = convert_to_stl(content, ext)
        except Exception as exc:  # noqa: BLE001
            ui.label(f"Couldn't tessellate {filename}: {exc}").style(
                "color: var(--matofx-error-text);"
            )
            return
        render_stl_preview(stl_bytes, height=520)
        _filename_label(filename, len(content), extra=f"tessellated to {len(stl_bytes):,} B STL")
        return
    render_cad_placeholder(filename, ext)


def _render_image_file(*, content: bytes, filename: str) -> None:
    """Inline image preview via a base64 data URL.

    Mirrors the upload widget's ``fit=contain`` so non-square images
    aren't cropped. Caps the visible height at 520 px so a tall image
    doesn't push everything else offscreen.
    """
    ext = _ext_of(filename) or "bin"
    mime = _IMAGE_MIMES.get(ext, "application/octet-stream")
    data_url = f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
    ui.image(data_url).props("fit=contain").classes("w-full").style(
        "max-height: 520px; height: 520px; background: transparent;"
    )
    _filename_label(filename, len(content))


def _render_anim3d_file(*, content: bytes, filename: str) -> None:
    """Inline GLTF / GLB scene via the shared helper.

    Demo path uses the static URL so the GLTF loader can resolve
    relative ``.bin`` / texture references against the file's
    directory — what makes the Cesium Man ``.gltf`` + ``.bin`` pair
    work. ``content`` is still read off disk for the size label.
    """
    src = demo_file_url(CATEGORY_ANIM3D, filename)
    render_gltf_preview(src_url=src, extension=_ext_of(filename), height=520)
    _filename_label(filename, len(content))


def _render_video_file(*, content: bytes, filename: str) -> None:
    """Inline HTML5 video player via the shared helper.

    Demo path uses the static URL so the browser can ``Range``-stream
    and seek properly. Embedding a 50 MB clip via base64 would push
    the whole video into memory just to play it.
    """
    src = demo_file_url(CATEGORY_VIDEO, filename)
    render_video_preview(src_url=src, extension=_ext_of(filename) or "mp4", height=520)
    _filename_label(filename, len(content))


def _render_anim3d_test_pattern() -> None:
    """Top-of-page hardcoded GLB embed for diagnosis.

    Loads Khronos's public ``DamagedHelmet.glb`` sample (animated
    PBR helmet from the KhronosGroup glTF-Sample-Models repo). If
    this renders and animates but the picker below doesn't, the bug
    is in the ``/_demo-asset/`` static mount or the local file
    itself; if this *doesn't* render, the model-viewer CDN script
    or the web component isn't loading.
    """
    test_url = (
        "https://raw.githubusercontent.com/KhronosGroup/glTF-Sample-Models/"
        "master/2.0/DamagedHelmet/glTF-Binary/DamagedHelmet.glb"
    )
    with ui.card().classes("w-full"), ui.column().classes("w-full gap-2"):
        ui.label("Test pattern (hardcoded public GLB — DamagedHelmet)").style("font-weight: 600;")
        ui.label(test_url).style(
            "color: var(--matofx-muted-text); font-size: 0.8rem; word-break: break-all;"
        )
        render_gltf_preview(src_url=test_url, extension="glb", height=420)


def _render_pdf_file(*, content: bytes, filename: str) -> None:
    """Inline PDF preview via the shared helper.

    Demo path uses the static URL so the browser's built-in PDF
    viewer takes the regular ``<iframe>`` rendering path — more
    reliable than the data-URL ``<embed>`` fallback the production
    output renderer falls back to when bytes are all it has.
    """
    src = demo_file_url(CATEGORY_PDF, filename)
    render_pdf_preview(src_url=src, height=720)
    _filename_label(filename, len(content))


# ---------- tiny helpers ---------------------------------------------------


def _filename_label(filename: str, size_bytes: int, *, extra: str = "") -> None:
    """Caption beneath the viewer — filename + size, optional extra note."""
    size_kb = size_bytes / 1024
    size_text = f"{size_kb / 1024:.1f} MB" if size_kb >= _KB_PER_MB else f"{size_kb:.1f} KB"
    text = f"{filename} ({size_text})"
    if extra:
        text = f"{text} — {extra}"
    ui.label(text).style("color: var(--matofx-muted-text); font-size: 0.8rem;")


def _ext_of(filename: str) -> str:
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()
