"""CAD upload — smoke-test tool for the v2 CAD preview pipeline.

Reads a CAD geometry file (STL / STP / STEP / IGES / OBJ) supplied as
input, tessellates it to STL via the same converter the API server
uses (:mod:`matofx.tools.cad_convert`), and writes the STL back under
``outputs/`` so the post-run viewer / Compare-Jobs cell renders it
inline. Also emits a small ``stats.csv`` so the ``csv_table`` output
viewer has something to chew on and a couple of scalar outputs so the
"Inputs / Outputs" panel reads as a complete tool run.

What this tool exists for:

- **Input widget** — drag-drop / browse / pick-from-previous-job
  flows through the rich :func:`matofx.tools.forms.render._render_rich_file_field`
  widget; the inline preview pane on the right runs the same
  cad-preview round-trip a real tool's input would.
- **Output viewer** — the My Jobs page's CAD-file branch fetches via
  ``/api/jobs/{id}/outputs/{filename}/cad-preview`` and renders STL.
- **Compare Jobs** — two runs side-by-side exercise the per-cell
  output_preview_blobs / input_preview_blobs pre-loaders for both
  the input file (any source format) AND the output STL.

No real geometry maths happens here — the tool is a deliberate
test fixture. Real CAD tools (FEA / CFD / etc.) live outside this
example tree.
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
from pathlib import Path
from typing import Any


def _read_inputs() -> dict[str, Any]:
    payload = json.loads(Path("inputs/inputs.json").read_text(encoding="utf-8"))
    return {entry["name"]: entry["value"] for entry in payload["inputs"]}


def main() -> int:
    """Tessellate the input CAD to STL, save under ``outputs/``, emit outputs.json."""
    inputs = _read_inputs()
    source_path_value = inputs.get("geometry_file")
    if not isinstance(source_path_value, str) or not source_path_value:
        print("No geometry_file path supplied — cannot continue.", file=sys.stderr)
        return 2

    source_path = Path(source_path_value)
    if not source_path.is_file():
        print(
            f"Source CAD file not found at {source_path}.",
            file=sys.stderr,
        )
        return 3

    out_dir = Path("outputs")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_stl = out_dir / "uploaded_geometry.stl"

    source_bytes = source_path.read_bytes()
    source_ext = source_path.suffix.lower().lstrip(".")
    source_size_kb = round(len(source_bytes) / 1024.0, 1)

    # The framework's CAD converter is the same machinery the
    # cad-preview endpoint uses, so doing the conversion here mirrors
    # what the runtime preview would produce. Imports lazily so the
    # tool fails clean if cadquery / trimesh are missing.
    converted_size_kb: float | None = None
    conversion_error: str | None = None
    try:
        from matofx.tools.cad_convert import (  # noqa: PLC0415
            CadConversionError,
            convert_to_stl,
        )

        stl_bytes = convert_to_stl(source_bytes, source_ext)
        out_stl.write_bytes(stl_bytes)
        converted_size_kb = round(len(stl_bytes) / 1024.0, 1)
    except CadConversionError as exc:
        conversion_error = str(exc)
        # Fall back to a raw copy when the source is already STL or
        # the converter declined — the run shouldn't fail just
        # because preview tessellation didn't work.
        if source_ext == "stl":
            shutil.copyfile(source_path, out_stl)
            converted_size_kb = source_size_kb
        else:
            # Touch an empty placeholder so the output filename the
            # descriptor declares still exists; the run is marked
            # successful but the preview will be empty.
            out_stl.write_bytes(b"")
    except ImportError as exc:
        conversion_error = (
            f"CAD conversion libraries not installed: {exc}. "
            "Install with: pip install cadquery trimesh"
        )
        # Same fallback as above for STL sources.
        if source_ext == "stl":
            shutil.copyfile(source_path, out_stl)
            converted_size_kb = source_size_kb
        else:
            out_stl.write_bytes(b"")

    # Write the stats CSV — exercises the ``csv_table`` output viewer.
    stats_csv = out_dir / "stats.csv"
    with stats_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["field", "value"])
        writer.writerow(["source_filename", source_path.name])
        writer.writerow(["source_extension", source_ext])
        writer.writerow(["source_size_kb", f"{source_size_kb:.1f}"])
        writer.writerow(["converted_to_stl", "yes" if converted_size_kb is not None else "no"])
        if converted_size_kb is not None:
            writer.writerow(["stl_size_kb", f"{converted_size_kb:.1f}"])
        if conversion_error:
            writer.writerow(["conversion_error", conversion_error])
        run_label = inputs.get("run_label")
        if isinstance(run_label, str) and run_label.strip():
            writer.writerow(["run_label", run_label.strip()])
        notes = inputs.get("notes")
        if isinstance(notes, str) and notes.strip():
            writer.writerow(["notes", notes.strip()])

    # Outputs document — descriptor + tool agree on names + filenames.
    outputs_doc: dict[str, Any] = {
        "schema_version": 1,
        "tool_alias": "cad_upload",
        "outputs": [
            {
                "name": "uploaded_geometry",
                "label": "Uploaded geometry",
                "type": "file",
                "filename": "uploaded_geometry.stl",
            },
            {
                "name": "file_size_kb",
                "label": "File size",
                "type": "number",
                "unit": "kB",
                "value": source_size_kb,
            },
            {
                "name": "source_extension",
                "label": "Source extension",
                "type": "string",
                "value": source_ext,
            },
            {
                "name": "stats_csv",
                "label": "Run details",
                "type": "file",
                "filename": "stats.csv",
            },
        ],
    }
    (out_dir / "outputs.json").write_text(
        json.dumps(outputs_doc, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
