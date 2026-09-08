"""Asset-embedding helpers for the per-job report.

Tiny pure-Python helpers that turn raw bytes (logo, images, videos)
into ``data:`` URIs the Jinja2 template can drop straight into
``<img src=...>`` / ``<video src=...>`` tags. Keeps all the file-IO
+ MIME-sniffing + safe-path resolution in one place so the template
stays declarative.

Compression rule (ADR-0004): raster images above 256 KB are re-encoded
as JPEG quality 85 when they have no alpha channel (alpha → PNG with
``optimize=True`` instead). SVGs and small files embed verbatim.
"""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
from typing import Any, Final

from PIL import Image, UnidentifiedImageError

_log = logging.getLogger(__name__)

_IMAGE_REENCODE_THRESHOLD_BYTES: Final[int] = 256 * 1024
"""Raw-byte cutoff above which raster images get re-compressed."""

VIDEO_SIZE_CAP_BYTES: Final[int] = 25 * 1024 * 1024
"""ADR-0004: videos at or below 25 MB embed as ``<video controls>``;
anything bigger falls through to filename-only listing with a size
note. Never re-encoded (no ffmpeg dep)."""

_VIDEO_MIME_BY_EXTENSION: Final[dict[str, str]] = {
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "webm": "video/webm",
    "ogg": "video/ogg",
    "ogv": "video/ogg",
    "mov": "video/quicktime",
}

_3D_EXTENSIONS: Final[frozenset[str]] = frozenset({"gltf", "glb"})
"""GLTF / GLB outputs — listed filename-only; no three.js bundled."""

_PREVIEWABLE_IMAGE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"}
)
"""Extensions the input-preview pipeline can embed inline. Everything
else falls through to filename-only rendering."""

_RASTER_MIME_BY_EXTENSION: Final[dict[str, str]] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}

# Recognised header-icon extensions. The Admin → Branding upload pane
# enforces this set on the way in, so anything reaching the report
# pipeline has to be in here; unknown extensions fall through to
# ``None`` and the template skips the logo.
_LOGO_MIME_BY_EXTENSION: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
}


def _extension(filename: str) -> str:
    """Lowercase extension (without the dot), or empty string when none."""
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


def is_previewable_image(filename: str) -> bool:
    """Return ``True`` when the extension is one the report knows how to embed."""
    return _extension(filename) in _PREVIEWABLE_IMAGE_EXTENSIONS


def is_video_file(filename: str) -> bool:
    """``True`` when the extension is in :data:`_VIDEO_MIME_BY_EXTENSION`."""
    return _extension(filename) in _VIDEO_MIME_BY_EXTENSION


def is_pdf_file(filename: str) -> bool:
    """``True`` when the extension is ``pdf``."""
    return _extension(filename) == "pdf"


def is_html_file(filename: str) -> bool:
    """``True`` when the extension is ``html`` or ``htm``."""
    return _extension(filename) in {"html", "htm"}


def is_3d_animation_file(filename: str) -> bool:
    """``True`` for GLTF / GLB outputs (listed filename-only — no viewer)."""
    return _extension(filename) in _3D_EXTENSIONS


_BYTES_PER_KIB: Final[int] = 1024


def human_size(bytes_value: int) -> str:
    """Format ``bytes_value`` as a short human-readable string.

    1024-based units (KiB / MiB style) feel right for tool outputs;
    the suffix drops the ``i`` so the label reads as ``42 MB`` —
    matches what users see in OS file managers.
    """
    if bytes_value < _BYTES_PER_KIB:
        return f"{bytes_value} B"
    units = ("KB", "MB", "GB", "TB")
    value = float(bytes_value) / _BYTES_PER_KIB
    for unit in units:
        if value < _BYTES_PER_KIB or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= _BYTES_PER_KIB
    # Unreachable — the loop always returns; here to satisfy the type
    # checker.
    return f"{bytes_value} B"


def inline_input_image(
    *,
    value: str,
    mount_root: Path,
) -> str | None:
    r"""Resolve an input file's path, read it, return a base64 ``data:`` URI.

    Job-input file values are stored as **absolute paths** under the
    storage mount (see ``matofx.api.routers.jobs`` upload handler). We
    re-resolve here and refuse anything that doesn't sit under
    ``mount_root`` — defence in depth, so a job row carrying a
    poisoned path (``/etc/passwd``, ``..\windows\...``) can never
    leak host files into a downloadable report.

    Returns ``None`` (and the template falls through to filename-only
    rendering) on any failure: bad path, file missing, unrecognised
    extension, or Pillow refusing the bytes.
    """
    path = safe_resolve_under_mount(value, mount_root)
    if path is None:
        return None
    extension = path.suffix.lstrip(".").lower()
    if extension not in _PREVIEWABLE_IMAGE_EXTENSIONS:
        return None

    raw = path.read_bytes()
    if extension == "svg":
        return _data_uri("image/svg+xml", raw)
    raster_mime = _RASTER_MIME_BY_EXTENSION.get(extension, "application/octet-stream")
    if len(raw) <= _IMAGE_REENCODE_THRESHOLD_BYTES:
        return _data_uri(raster_mime, raw)
    try:
        return _reencode_compressed(raw)
    except (UnidentifiedImageError, OSError) as exc:
        _log.debug("Falling back to raw embed for %s: %s", path.name, exc)
        return _data_uri(raster_mime, raw)


def safe_resolve_under_mount(value: str, mount_root: Path) -> Path | None:
    """Resolve ``value`` and confirm it sits under ``mount_root`` on disk."""
    try:
        path = Path(value).resolve(strict=False)
        root = mount_root.resolve(strict=False)
    except (OSError, ValueError):
        return None
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if not path.is_file():
        return None
    return path


def _data_uri(mime: str, raw: bytes) -> str:
    """Encode ``raw`` as ``data:<mime>;base64,...``."""
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _reencode_compressed(raw: bytes) -> str:
    """Re-encode an oversized raster as JPEG q85 (or PNG-optimized for alpha).

    Pillow's ``optimize=True`` plus q85 hits a good fidelity-vs-size
    point for screenshot / render output without visible banding;
    alpha-channel images stay PNG so transparency survives.
    """
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        has_alpha = image.mode in ("RGBA", "LA") or (
            image.mode == "P" and "transparency" in image.info
        )
        buffer = io.BytesIO()
        if has_alpha:
            image.save(buffer, format="PNG", optimize=True)
            mime = "image/png"
        else:
            converted = image.convert("RGB") if image.mode != "RGB" else image
            converted.save(buffer, format="JPEG", quality=85, optimize=True)
            mime = "image/jpeg"
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def inline_video(*, value: str, mount_root: Path) -> dict[str, Any] | None:
    """Resolve a video output and embed it as a base64 ``data:`` URI.

    Returns ``{"data_uri": str, "size_bytes": int, "mime": str}``
    when the file is safe to embed (≤ :data:`VIDEO_SIZE_CAP_BYTES`),
    ``{"oversized": True, "size_bytes": int}`` when it sits on disk
    but exceeds the cap, and ``None`` for missing-file / wrong-mount
    / unrecognised-extension. The router renders the oversized case
    as filename + size note (still useful in the operational record).
    """
    path = safe_resolve_under_mount(value, mount_root)
    if path is None:
        return None
    extension = path.suffix.lstrip(".").lower()
    mime = _VIDEO_MIME_BY_EXTENSION.get(extension)
    if mime is None:
        return None
    size = path.stat().st_size
    if size > VIDEO_SIZE_CAP_BYTES:
        return {"oversized": True, "size_bytes": size}
    return {
        "data_uri": _data_uri(mime, path.read_bytes()),
        "size_bytes": size,
        "mime": mime,
    }


def inline_pdf(*, value: str, mount_root: Path) -> str | None:
    """Return a ``data:application/pdf;base64,...`` URI for a PDF output.

    No size cap — most simulation-report PDFs are well under a few
    megabytes, and the report's overall envelope is already governed
    by the per-asset image / video rules. Returns ``None`` for
    missing file / outside the mount / wrong extension.
    """
    path = safe_resolve_under_mount(value, mount_root)
    if path is None or path.suffix.lstrip(".").lower() != "pdf":
        return None
    return _data_uri("application/pdf", path.read_bytes())


def inline_html_embed(*, value: str, mount_root: Path) -> str | None:
    """Return a ``data:text/html;base64,...`` URI for an HTML output.

    The router drops the URI into a ``<iframe sandbox src="...">``.
    Sandboxing keeps the embedded document scriptless and cookieless
    in the recipient's browser — important because the HTML may be a
    tool-author-produced report with its own JS the report author
    didn't audit.
    """
    path = safe_resolve_under_mount(value, mount_root)
    if path is None:
        return None
    if path.suffix.lstrip(".").lower() not in {"html", "htm"}:
        return None
    return _data_uri("text/html", path.read_bytes())


def inline_logo(*, icons_dir: Path | None, basename: str | None) -> str | None:
    """Read the branded logo file and return a base64 ``data:`` URI.

    Returns ``None`` when:

    - ``icons_dir`` or ``basename`` is missing (no logo configured),
    - the file doesn't exist on disk (mid-upload race / missing
      mount),
    - the extension isn't a recognised image type.

    The template gates rendering on the return value, so ``None``
    just hides the logo slot — the rest of the header still renders
    fine.
    """
    if icons_dir is None or not basename:
        return None
    path = icons_dir / basename
    if not path.is_file():
        return None
    extension = path.suffix.lstrip(".").lower()
    mime = _LOGO_MIME_BY_EXTENSION.get(extension)
    if mime is None:
        return None
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"
