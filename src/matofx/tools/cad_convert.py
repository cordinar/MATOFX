"""Server-side CAD → STL conversion for inline previews.

Five supported source formats, all converted to binary STL so the
browser-side viewers (``ui.scene.stl`` + base64 data URLs) can render
them through one code path:

- ``.stl``  — pass-through (no conversion).
- ``.stp`` / ``.step`` — cadquery's :func:`importers.importStep`
  followed by :func:`exporters.export` with ``exportType="STL"``.
  cadquery wraps OpenCascade under the hood; the wheel is heavy
  (~200 MB) but eliminates the FreeCAD subprocess the legacy app used.
- ``.iges`` / ``.igs`` — raw OCP (OpenCascade Python bindings) via
  :class:`IGESControl_Reader`. cadquery's high-level importer doesn't
  cover IGES, but the underlying tessellator is the same as for STEP.
- ``.obj``  — :mod:`trimesh` reads OBJ natively and exports STL bytes
  in one call. Much lighter than OCP — added because users routinely
  ship OBJ alongside STEP and we don't want to spin up OpenCascade
  for a format that doesn't need it.

Cache: results are memoised on ``(sha256(content), extension)`` so a
file viewed in My Jobs and then re-shown in Compare doesn't get
re-tessellated. Bounded to the most-recent 64 entries — STL output
is typically 1-10× the source size, so the cap caps memory growth.
First-eviction is approximately FIFO via :class:`OrderedDict`.

Errors are wrapped in :class:`CadConversionError` with a plain-English
message; the API layer surfaces these as 400s. ``ImportError`` from a
missing optional dependency (cadquery / trimesh / OCP) surfaces as a
503 with an install hint — the rest of the app keeps working, only
CAD previews degrade.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import tempfile
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger(__name__)


SUPPORTED_CAD_EXTENSIONS: frozenset[str] = frozenset({"stl", "stp", "step", "iges", "igs", "obj"})
"""Formats :func:`convert_to_stl` accepts. Other extensions raise."""

MAX_SOURCE_BYTES: int = 50 * 1024 * 1024
"""Hard ceiling on source-file size. STEP geometries above this take
many seconds to tessellate and tie up the API worker; the user is
better served by a "preview unavailable, file too large" message than
a hung request."""

_CACHE_SIZE: int = 64
_cache: OrderedDict[tuple[str, str], bytes] = OrderedDict()


class CadConversionError(RuntimeError):
    """Plain-English failure surfaced to the user via the API.

    The API layer renders this as a 400 with the message verbatim —
    the message must be readable to a non-developer.
    """


def convert_to_stl(content: bytes, source_ext: str) -> bytes:
    """Return STL bytes for ``content`` of the given source format.

    Args:
        content: Raw bytes of the source file.
        source_ext: Source extension (with or without leading dot).
            Case-insensitive; ``"stl"`` / ``".stl"`` / ``"STL"`` all
            work.

    Returns:
        Binary STL bytes ready for ``ui.scene.stl`` consumption.
        For ``.stl`` input, the original bytes are returned
        unchanged.

    Raises:
        CadConversionError: when the source format is unsupported,
            the file is too large, or the underlying tessellator
            fails to produce a valid mesh. Always plain-English.
        ImportError: when the required optional dependency
            (cadquery / OCP / trimesh) isn't installed. The API
            layer translates this into a 503 with an install hint.
    """
    ext = source_ext.lower().lstrip(".")
    if ext not in SUPPORTED_CAD_EXTENSIONS:
        raise CadConversionError(
            f"Inline 3-D preview isn't supported for .{ext} files. "
            f"Supported formats: {', '.join(sorted(SUPPORTED_CAD_EXTENSIONS))}."
        )
    if len(content) > MAX_SOURCE_BYTES:
        raise CadConversionError(
            f"That file is {len(content) / (1024 * 1024):.1f} MB — too big to "
            "tessellate inline. Download the file and view it locally if "
            "you need a preview at this size."
        )
    if ext == "stl":
        return content

    content_hash = hashlib.sha256(content).hexdigest()
    cache_key = (content_hash, ext)
    cached = _cache.get(cache_key)
    if cached is not None:
        # Bump LRU recency: move-to-end so this entry survives the
        # next eviction tick.
        _cache.move_to_end(cache_key)
        return cached

    if ext in {"stp", "step"}:
        result = _convert_step(content)
    elif ext in {"iges", "igs"}:
        result = _convert_iges(content)
    elif ext == "obj":
        result = _convert_obj(content)
    else:  # pragma: no cover — guarded by the SUPPORTED set above
        raise CadConversionError(f"Unsupported CAD format: .{ext}")

    _cache[cache_key] = result
    if len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return result


def _convert_step(content: bytes) -> bytes:
    """STP / STEP → STL via raw OCP (skipping cadquery's wrapper).

    Earlier rev used :func:`cadquery.importers.importStep`, which
    works for clean STEP files but bails on the OCP-stderr-driven
    "Line N: Incorrect syntax" message without surfacing a useful
    Python-side exception (see 2026-05-14 feedback). Going through
    OCP directly lets us check ``IFSelect_RetDone`` and report
    failure modes precisely — bad header, schema mismatch, file
    truncation, etc.

    Same shape as :func:`_convert_iges`: read with ``STEPControl_Reader``,
    tessellate with ``BRepMesh_IncrementalMesh``, write with
    ``StlAPI_Writer``. Temp files for both ends because the
    OCP-binding readers / writers consume paths, not buffers.
    """
    from OCP.BRepMesh import BRepMesh_IncrementalMesh  # noqa: PLC0415
    from OCP.IFSelect import IFSelect_RetDone  # noqa: PLC0415
    from OCP.STEPControl import STEPControl_Reader  # noqa: PLC0415
    from OCP.StlAPI import StlAPI_Writer  # noqa: PLC0415

    # Sanity-check the source bytes before paying for OCP. STEP
    # files start with ``ISO-10303-21;`` per the ISO 10303-21 spec;
    # if ours doesn't, the upload was truncated / the file is a
    # different format renamed to .stp / .step, and we'd rather
    # surface that than let OCP emit its cryptic
    # ``ERR StepFile : Undefined Parsing: Line 2`` message.
    head = content.lstrip(b"\xef\xbb\xbf").lstrip()[:32].upper()
    if not head.startswith(b"ISO-10303-21"):
        raise CadConversionError(
            "That file doesn't look like a STEP file — it doesn't start "
            "with the ``ISO-10303-21;`` header the format requires. "
            "Open it in your CAD package and re-export, or check that "
            "the upload wasn't truncated."
        )

    in_path = _write_temp(content, ".step")
    out_path = _new_temp_path(".stl")
    try:
        reader = STEPControl_Reader()
        try:
            status = reader.ReadFile(str(in_path))
        except Exception as exc:  # noqa: BLE001 — OCP raises native errors
            raise CadConversionError(f"OCP couldn't open the STEP file: {exc}") from exc
        if status != IFSelect_RetDone:
            raise CadConversionError(
                "OCP couldn't parse the STEP file — the header may be "
                "malformed or the file may have been truncated during "
                "upload. Open it in your CAD package and re-export, or "
                "convert to STL ahead of time."
            )
        try:
            reader.TransferRoots()
            shape = reader.OneShape()
        except Exception as exc:  # noqa: BLE001
            raise CadConversionError(f"OCP couldn't transfer the STEP geometry: {exc}") from exc
        if shape is None or shape.IsNull():
            raise CadConversionError(
                "The STEP file parsed but didn't yield any geometry to tessellate."
            )

        # Tessellation tolerance — same value as IGES; fine-enough for
        # typical inputs, coarse enough that a 50 MB part doesn't take
        # minutes. Tool authors who need finer meshes can convert to
        # STL ahead of time.
        BRepMesh_IncrementalMesh(shape, 0.5)

        writer = StlAPI_Writer()
        if not writer.Write(shape, str(out_path)):
            raise CadConversionError(
                "Couldn't write STL output from the STEP geometry — the "
                "model may contain unsupported surface types."
            )
        return out_path.read_bytes()
    finally:
        _unlink_quietly(in_path)
        _unlink_quietly(out_path)


def _convert_iges(content: bytes) -> bytes:
    """IGES → STL via the raw OCP bindings.

    cadquery's high-level importer doesn't cover IGES. We open the
    file with ``IGESControl_Reader``, tessellate via ``BRepMesh``,
    and write the result with ``StlAPI_Writer`` — the same flow
    cadquery uses for STEP under the hood.
    """
    from OCP.BRepMesh import BRepMesh_IncrementalMesh  # noqa: PLC0415
    from OCP.IFSelect import IFSelect_RetDone  # noqa: PLC0415
    from OCP.IGESControl import IGESControl_Reader  # noqa: PLC0415
    from OCP.StlAPI import StlAPI_Writer  # noqa: PLC0415

    in_path = _write_temp(content, ".iges")
    out_path = _new_temp_path(".stl")
    try:
        reader = IGESControl_Reader()
        if reader.ReadFile(str(in_path)) != IFSelect_RetDone:
            raise CadConversionError("Couldn't parse the IGES file.")
        reader.TransferRoots()
        shape = reader.OneShape()
        # 0.5 is a reasonable linear tolerance — fine-enough for most
        # inputs, coarse enough that a 50 MB ship-hull doesn't grind
        # to a halt. Tool authors who need finer meshes can convert
        # to STL ahead of time.
        BRepMesh_IncrementalMesh(shape, 0.5)
        writer = StlAPI_Writer()
        if not writer.Write(shape, str(out_path)):
            raise CadConversionError(
                "Couldn't write STL output — the IGES file may not contain "
                "any tessellatable geometry."
            )
        return out_path.read_bytes()
    finally:
        _unlink_quietly(in_path)
        _unlink_quietly(out_path)


def _convert_obj(content: bytes) -> bytes:
    """OBJ → STL via trimesh.

    OBJ is a mesh format (vertices + faces) rather than a BREP
    description, so it doesn't need OpenCascade — trimesh reads the
    bytes directly from memory and emits STL in one call.
    """
    import io  # noqa: PLC0415

    import trimesh  # noqa: PLC0415

    try:
        mesh = trimesh.load(io.BytesIO(content), file_type="obj", force="mesh")
    except Exception as exc:  # noqa: BLE001 — trimesh raises a broad family
        raise CadConversionError(f"Couldn't read OBJ file: {exc}") from exc
    try:
        # trimesh ships no stubs; ``export`` returns bytes for file_type=stl.
        return bytes(mesh.export(file_type="stl"))  # type: ignore[no-untyped-call,call-arg]
    except Exception as exc:  # noqa: BLE001
        raise CadConversionError(f"Couldn't export STL from OBJ: {exc}") from exc


def _write_temp(content: bytes, suffix: str) -> Path:
    """Write ``content`` to a unique temp file; return the path."""
    fd, name = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
    except Exception:
        # Clean up the file descriptor on write failure so we don't
        # leak it back to the OS.
        _unlink_quietly(Path(name))
        raise
    return Path(name)


def _new_temp_path(suffix: str) -> Path:
    """Reserve a unique temp path; caller writes to it."""
    fd, name = tempfile.mkstemp(suffix=suffix)
    import os  # noqa: PLC0415

    os.close(fd)
    return Path(name)


def _unlink_quietly(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def clear_cache() -> None:
    """Drop every cached conversion result.

    Used by tests; production callers shouldn't need to call this —
    the LRU eviction handles bounded memory growth.
    """
    _cache.clear()
