"""Colour-check tool — invert image + list dominant colours.

Reads an image file path from ``inputs/inputs.json``, inverts every
pixel's RGB channel, and writes the inverted image plus a CSV of the
colours present in the original image (with percentage coverage,
filtered to colours that occupy at least 0.1 % of the image).

Outputs:

- ``outputs/transformed_image.bmp`` — the inverted image.
- ``outputs/colours_list.csv`` — ``R,G,B,%`` rows sorted by coverage.
- ``outputs/outputs.json`` — the v2 :class:`OutputsDocument`.

Depends on Pillow (HPND, permissive).
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from PIL import Image

_COVERAGE_THRESHOLD_PCT: float = 0.1


def _read_inputs() -> dict[str, Any]:
    payload = json.loads(Path("inputs/inputs.json").read_text(encoding="utf-8"))
    return {entry["name"]: entry["value"] for entry in payload["inputs"]}


def _write_outputs_doc(tool_alias: str) -> None:
    document = {
        "schema_version": 1,
        "tool_alias": tool_alias,
        "outputs": [
            {
                "name": "inverted_image",
                "label": "Inverted image",
                "type": "file",
                "filename": "transformed_image.bmp",
            },
            {
                "name": "colours_list",
                "label": "Dominant colours",
                "type": "file",
                "filename": "colours_list.csv",
            },
        ],
    }
    Path("outputs/outputs.json").write_text(json.dumps(document, indent=2), encoding="utf-8")


def main() -> int:
    """Invert the input image and write the colour-coverage CSV + outputs document."""
    inputs = _read_inputs()
    image_path = Path(inputs["input_image"])

    img = Image.open(image_path).convert("RGB")
    pixels = list(img.getdata())
    total = len(pixels)

    counter: Counter[tuple[int, int, int]] = Counter(pixels)

    # Invert each unique colour once, then map every pixel through the
    # lookup. Cheaper than per-pixel arithmetic on large images.
    inverted_lookup = {rgb: (255 - rgb[0], 255 - rgb[1], 255 - rgb[2]) for rgb in counter}
    inverted = Image.new("RGB", img.size)
    inverted.putdata([inverted_lookup[p] for p in pixels])

    out_dir = Path("outputs")
    out_dir.mkdir(exist_ok=True)
    inverted.save(out_dir / "transformed_image.bmp")

    rows = sorted(
        (
            (r, g, b, round(count / total * 100.0, 2))
            for (r, g, b), count in counter.items()
            if (count / total * 100.0) >= _COVERAGE_THRESHOLD_PCT
        ),
        key=lambda row: row[3],
        reverse=True,
    )
    with (out_dir / "colours_list.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["R", "G", "B", "%"])
        writer.writerows(rows)

    _write_outputs_doc(tool_alias="colour_check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
