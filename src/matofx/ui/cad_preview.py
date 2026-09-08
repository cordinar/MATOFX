"""Inline 3D previews for CAD-style file outputs.

NiceGUI's :class:`ui.scene` element wraps three.js and ships with a
ready-made ``scene.stl(url)`` loader. The legacy MATOFX viewer relied
on the Streamlit-specific ``stpyvista`` integration which doesn't
port; instead we feed the STL bytes directly into the browser via a
``data:`` URL — three.js's STLLoader uses XMLHttpRequest internally,
which transparently handles ``data:`` URLs, so we don't need a
server-side static file route or a one-shot signed URL just to show a
preview.

What's here vs. deferred:

- **STL** — first-class: :func:`render_stl_preview` emits a sized
  ``ui.scene`` with the bytes embedded as a data URL. Used by the
  upload widget's preview pane, the My Jobs file-output renderer,
  and the Compare workspace's per-cell view.
- **STP / STEP / OBJ / IGES / X_T / X_B** — second pass.
  :func:`render_cad_placeholder` surfaces a "preview pending"
  message so the user knows the upload worked even though the right
  pane stays empty. Wiring them up means porting the legacy
  ``stp_to_stl.py`` (FreeCAD subprocess) which is its own follow-up.

Sizing rationale: 320 px default height matches the upload widget's
image-preview box and the My Jobs CSV-plot height, so a row of
mixed-type previews lines up vertically without one type towering
over the others. Callers can override per-surface.
"""

from __future__ import annotations

import base64

from nicegui import ui

from matofx.ui.static_assets import ASSETS_URL_PREFIX

_STL_MESH_COLOR: str = "#40A1A4"
"""Funis accent teal — same on both palettes, so the mesh material
reads with good contrast against either the light off-white page bg
(``#f5f5f5``) or the dark page bg (``#1e1e1e``). The legacy app used
pyvista's default chalky grey, which got lost on dark backgrounds —
this is the v2 fix locked 2026-05-14."""


def render_stl_preview(content: bytes, *, height: int = 320) -> None:
    """Render STL bytes as an interactive 3D scene.

    The bytes are encoded as a base64 ``data:`` URL and passed to
    ``ui.scene.stl(...)``. The browser fetches the URL via three.js's
    STLLoader, which converts the binary STL to a mesh and renders it
    in WebGL.

    Display rules:

    - Background tracks ``current_palette().page_bg`` so the viewer
      reads as part of the surrounding page (locked 2026-05-14 — the
      hard-coded ``#1a1a1a`` looked wrong on the light theme).
    - The default ``ui.scene`` grid_helper is **disabled** —
      it drew a checkerboard plane through the middle of every
      mesh, which the user reported as "an unnecessary grid through
      the structure".
    - Mesh material is the Funis brand teal so it stands out on both
      light and dark palettes without us having to switch colours
      per theme.

    Args:
        content: Raw STL bytes (binary or ASCII; STLLoader auto-
            detects). Empty / corrupt bytes degrade to an empty
            scene; we don't try to validate up front because the
            legacy app didn't either and we'd rather show the
            failure shape than mask it.
        height: Pixel height of the scene. Width fills the container.
            Default 320 matches the upload-widget image preview so
            mixed-type rows line up.
    """
    # Local import: ``matofx.ui.theme`` reads NiceGUI's per-client
    # storage to resolve the palette, which isn't available outside a
    # page context. Importing lazily keeps this module test-friendly.
    from matofx.ui.theme import current_palette  # noqa: PLC0415

    palette = current_palette()
    data_url = "data:application/octet-stream;base64," + base64.b64encode(content).decode("ascii")
    scene = (
        ui.scene(
            background_color=palette.page_bg,
            grid=False,
        )
        .classes("w-full")
        .style(f"height: {height}px; border-radius: 4px;")
    )
    mesh = scene.stl(data_url)
    # Funis teal — high enough chroma to read on both light and dark
    # page backgrounds without us having to flip per theme.
    mesh.material(_STL_MESH_COLOR)
    # Move the camera back so the mesh isn't crammed against the
    # near clip plane. The legacy viewer used a similar offset.
    scene.move_camera(x=2.5, y=2.5, z=2.5)


_GLTF_MIME_BY_EXT: dict[str, str] = {
    "gltf": "model/gltf+json",
    "glb": "model/gltf-binary",
}

_VENDOR_URL_PREFIX: str = f"{ASSETS_URL_PREFIX}/vendor"

_MODEL_VIEWER_JS: str = f"{_VENDOR_URL_PREFIX}/model-viewer.min.js"
"""Google's ``<model-viewer>`` web component — three.js-based GLTF
viewer that *plays* embedded animations via ``AnimationMixer``,
handles relative-path resolution for ``.gltf``+``.bin`` pairs when
loaded from an HTTP URL, and offers camera controls + AR mode out of
the box. NiceGUI's own ``ui.scene.gltf(...)`` loads geometry but
doesn't tick the animation mixer per frame, which is why the v1 of
this helper showed Cesium Man's first frame and then froze.

Served from ``ui/assets/vendor/`` rather than a public CDN so the
viewer works on an air-gapped install."""

_DRACO_DECODER_LOCATION: str = f"{_VENDOR_URL_PREFIX}/draco/"
_KTX2_TRANSCODER_LOCATION: str = f"{_VENDOR_URL_PREFIX}/basis/"
"""Where model-viewer should find its two optional decoders.

These matter more than they look. model-viewer reads
``self.ModelViewerElement.dracoDecoderLocation`` /
``.ktx2TranscoderLocation`` **once, at module-evaluation time**, and
defaults both to ``https://www.gstatic.com/…``. It then loads them
lazily — only when a model actually uses Draco mesh compression or
KTX2 textures. Vendoring the viewer alone therefore *looks* fully
offline in testing and fails on the first Draco-compressed ``.glb``
somebody uploads, which is what most glTF exporters emit by default.
Both locations are set before the module script is added, below."""

_MODEL_VIEWER_CONFIG_SCRIPT: str = (
    "<script>window.ModelViewerElement = window.ModelViewerElement || {};"
    f"window.ModelViewerElement.dracoDecoderLocation = '{_DRACO_DECODER_LOCATION}';"
    f"window.ModelViewerElement.ktx2TranscoderLocation = '{_KTX2_TRANSCODER_LOCATION}';"
    "</script>"
)
"""Point model-viewer at the vendored decoders. Must precede the module
script — see :data:`_DRACO_DECODER_LOCATION`. Merged into any existing
``ModelViewerElement`` object rather than replacing it, because the
module assigns the real class to that same global once it loads."""


def render_gltf_preview(
    content: bytes | None = None,
    *,
    src_url: str | None = None,
    extension: str = "glb",
    height: int = 320,
) -> None:
    """Render a GLTF / GLB scene with auto-playing animations.

    Two ways to pass the model:

    - ``content`` (bytes): the file's bytes. Encoded as a base64
      ``data:`` URL keyed off ``extension``. Best for ``.glb`` —
      single binary, self-contained. ``.gltf`` (JSON) bytes work for
      the geometry inside the JSON itself, but references to
      external ``.bin`` / texture files won't resolve because a
      ``data:`` URL has no base path; the model loads "empty".
    - ``src_url`` (str): a regular HTTP URL the browser can fetch.
      Preferred for ``.gltf`` files because the GLTFLoader resolves
      any relative buffer / texture references against the URL's
      directory.

    Pass exactly one of the two.

    Display rules mirror :func:`render_stl_preview`:

    - Background tracks ``current_palette().page_bg``.
    - Camera controls let the user orbit + zoom.
    - ``autoplay`` makes embedded keyframe animations start
      immediately — that's the whole reason this helper exists
      separately from the static STL viewer.
    """
    if (content is None) == (src_url is None):
        raise ValueError("render_gltf_preview takes exactly one of content / src_url.")

    from matofx.ui.theme import current_palette  # noqa: PLC0415

    palette = current_palette()
    if src_url is None:
        assert (  # noqa: S101 - narrowing for the type checker, not a runtime check
            content is not None
        )  # narrow for the type checker  # noqa: S101 - narrowing for the type checker
        ext = (extension or "").lower().lstrip(".")
        mime = _GLTF_MIME_BY_EXT.get(ext, _GLTF_MIME_BY_EXT["glb"])
        src_url = f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"

    # Order matters, and is the whole reason the config is a separate
    # block: model-viewer reads the two decoder locations off ``window``
    # while its module is evaluating, so they must be set by then.
    #
    # ``add_head_html`` does not deduplicate (an earlier comment here
    # claimed it did — it appends unconditionally), so N previews on one
    # page append both blocks N times. That is harmless rather than
    # merely tolerable: the browser's module map keys on URL so the
    # viewer evaluates exactly once however many tags point at it, and
    # the config block is idempotent by construction.
    ui.add_head_html(_MODEL_VIEWER_CONFIG_SCRIPT)
    ui.add_head_html(f'<script type="module" src="{_MODEL_VIEWER_JS}"></script>')
    # ``camera-controls`` enables orbit / zoom. ``autoplay`` ticks the
    # AnimationMixer so keyframe animations actually move — the
    # missing piece that made the v1 helper show a frozen first
    # frame. ``shadow-intensity=1`` gives the model a contact shadow
    # so it doesn't float against the background.
    # ``sanitize=False`` is mandatory in NiceGUI 3.0+ — without it the
    # call raises ``TypeError`` which gets swallowed by the page
    # handler and the embed silently disappears. Trusted content
    # (we control every byte of the HTML below), so sanitisation is
    # not what we want here.
    #
    # The child <p> is model-viewer's own fallback slot: a browser that
    # never upgrades the custom element (script blocked, or an engine
    # too old for web components) renders the children instead. Without
    # it the element collapses to a blank rectangle and the user is left
    # unable to tell a broken viewer from an empty model.
    ui.html(
        f"<model-viewer "
        f'src="{_html_attr_escape(src_url)}" '
        f"camera-controls autoplay "
        f'shadow-intensity="1" '
        f'style="width:100%; height:{height}px; border-radius:4px; '
        f'background-color: {palette.page_bg};">'
        f'<p style="padding:1rem; text-align:center; color:var(--matofx-muted-text);">'
        f"The 3-D viewer couldn&#8217;t start. The file itself is fine and is "
        f"still available to download &mdash; worth reporting to your administrator."
        f"</p>"
        f"</model-viewer>",
        sanitize=False,
    ).classes("w-full")


def _html_attr_escape(text: str) -> str:
    """Escape the five XML-significant characters for an HTML attribute.

    The ``src`` we put into the ``<model-viewer>`` attribute can be
    either a same-origin path (``/_demo-asset/...``) or a base64
    ``data:`` URL — neither needs URL-encoding, but a stray quote
    character could break out of the attribute. Belt-and-braces
    escaping covers both cases without needing to know which one we
    got.
    """
    return (
        text.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def render_pdf_preview(
    content: bytes | None = None,
    *,
    src_url: str | None = None,
    height: int = 480,
) -> None:
    """Render a PDF inline.

    Two ways to pass the file:

    - ``content`` (bytes): rendered via ``<embed type="application/pdf">``
      over a ``data:application/pdf`` URL. ``<embed>`` goes through
      the browser's plugin path rather than the ``<iframe>`` PDF
      viewer, which is the more reliable rendering route for data
      URLs in Chrome — the older ``<iframe>`` approach was the
      source of the "iframe is empty" complaint locked 2026-05-14.
    - ``src_url`` (str): a same-origin URL. Rendered via ``<iframe>``,
      which uses the browser's full built-in PDF viewer (toolbar,
      paging, text-selection). Preferred when you have an HTTP URL
      to point at — used by the demo page.

    Pass exactly one of the two.
    """
    if (content is None) == (src_url is None):
        raise ValueError("render_pdf_preview takes exactly one of content / src_url.")

    if src_url is not None:
        ui.html(
            f'<iframe src="{_html_attr_escape(src_url)}" '
            f'style="width:100%; height:{height}px; border:0; '
            f'background:white; border-radius:4px;"></iframe>',
            sanitize=False,
        ).classes("w-full")
        return

    assert content is not None  # noqa: S101 - narrowing for the type checker
    data_url = "data:application/pdf;base64," + base64.b64encode(content).decode("ascii")
    # ``<embed>`` instead of ``<iframe>``: Chrome's PDFium viewer
    # honours ``<embed type="application/pdf">`` with data URLs more
    # consistently than it honours iframes pointing at the same data
    # URL. Both Firefox and Safari accept both element types.
    ui.html(
        f'<embed src="{data_url}" '
        f'type="application/pdf" '
        f'style="width:100%; height:{height}px; border:0; '
        f'background:white; border-radius:4px;" />',
        sanitize=False,
    ).classes("w-full")


_VIDEO_MIME_BY_EXT: dict[str, str] = {
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "webm": "video/webm",
    "ogg": "video/ogg",
    "ogv": "video/ogg",
    "mov": "video/quicktime",
}


def render_video_preview(
    content: bytes | None = None,
    *,
    src_url: str | None = None,
    extension: str,
    height: int = 360,
) -> None:
    """Render a video inline via HTML5 ``<video controls>``.

    Two ways to pass the file:

    - ``content`` (bytes): rendered via a ``data:`` URL keyed off the
      ``extension`` MIME. Fine for short clips; a 50 MB clip is a
      ~67 MB data URL which is wasteful when an HTTP URL is available.
    - ``src_url`` (str): same-origin URL. The browser streams + seeks
      properly (``Range`` requests), which a data URL can't.

    Pass exactly one of the two. ``extension`` is required either way
    so the ``<source type=...>`` attribute is correct — browsers
    sometimes reject a video element whose source has no type hint.
    """
    if (content is None) == (src_url is None):
        raise ValueError("render_video_preview takes exactly one of content / src_url.")
    ext = (extension or "").lower().lstrip(".")
    mime = _VIDEO_MIME_BY_EXT.get(ext, "application/octet-stream")
    if src_url is None:
        assert content is not None  # noqa: S101 - narrowing for the type checker
        src_url = f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
    ui.html(
        f'<video controls preload="metadata" '
        f'style="width:100%; max-height:{height}px; background:black; '
        f'border-radius:4px;">'
        f'<source src="{_html_attr_escape(src_url)}" type="{mime}">'
        f"Your browser can't play this video format."
        f"</video>",
        sanitize=False,
    ).classes("w-full")


def render_cad_placeholder(filename: str, extension: str) -> None:
    """Friendly placeholder for CAD formats that need conversion.

    STP / STEP / OBJ / IGES etc. can't go straight into three.js — the
    legacy app shelled out to FreeCAD via ``stp_to_stl.py`` to convert
    them to STL first. That path lives as a port-still-pending in the
    v2 todo list; until then this placeholder makes it clear the file
    is saved and will be passed to the tool, even though there's no
    inline preview yet.
    """
    ui.icon("view_in_ar").style("font-size: 2.5rem; color: var(--matofx-muted-text);")
    ui.label(f".{extension.upper()} file").style("font-weight: 500; font-size: 0.9rem;")
    if filename:
        ui.label(_basename(filename)).style("color: var(--matofx-muted-text); font-size: 0.8rem;")
    ui.label(
        "Inline 3D preview for this CAD format is being ported from the "
        "legacy app (FreeCAD-driven conversion to STL). The file is saved "
        "and will be passed to the tool as-is."
    ).style("color: var(--matofx-muted-text); font-size: 0.75rem; text-align: center;")


def _basename(path: str) -> str:
    if not path:
        return ""
    return path.replace("\\", "/").rsplit("/", 1)[-1]
