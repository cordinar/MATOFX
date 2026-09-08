"""Seeders — input-source loaders for the My Jobs workspace's "load inputs" popovers.

Three sources match the legacy app's three buttons that appear above
a `New`-state job's input form:

1. **Preset** — a tool-author-supplied named bundle of input values
   (legacy ``<alias>_presets.json``; v2 prefers ``presets.toml`` next
   to ``descriptor.toml`` — both are read here for forward and
   backward compatibility).
2. **Previous job** — copy the inputs from any prior job of the same
   tool the user is allowed to view. The ACL check happens **upstream**
   in the API layer (Task #6); this module only handles data shaping.
3. **Defaults** — every input's descriptor-declared default. Used by
   the "Reset to defaults" button to wipe whatever the user has typed
   back to the descriptor's pristine state.

Each seeder returns a plain ``dict[str, Any]`` ready to hand straight
to :meth:`matofx.tools.forms.render.FormHandle.seed`.

## Why the formats differ

`presets.toml` (new):

```toml
[[preset]]
name = "Glass at 200°C"
description = "Default glass starting parameters"
[preset.values]
object_temperature = 200
density_left = 2500
```

Legacy `<alias>_presets.json`:

```json
{
    "Glass at 200°C": { "object_temperature": 200, "density_left": 2500 }
}
```

Both shapes load through :func:`load_presets`, which returns a list
of :class:`Preset` records. The descriptor itself declares which
filename it expects via ``ToolDescriptor.presets_filename`` (legacy)
or — when a v2 ``presets.toml`` is present alongside the descriptor —
that one wins.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from matofx.tools.descriptor import ToolDescriptor
from matofx.tools.exec.contracts import InputsDocument

_TOML_PRESETS_FILENAME = "presets.toml"


# --- Preset record -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Preset:
    """One preset entry, ready to surface as a "Use this preset" button."""

    name: str
    description: str
    values: dict[str, Any]


# --- Preset loader -------------------------------------------------------


def load_presets(bundle_path: str | Path, descriptor: ToolDescriptor) -> list[Preset]:
    """Load presets from a tool bundle.

    Resolution order:

    1. ``descriptor.presets`` — the wizard-authored, in-descriptor list.
       When non-empty this wins; the bundle files aren't consulted.
    2. ``<bundle>/presets.toml`` (v2 file shape).
    3. ``<bundle>/<descriptor.presets_filename>`` (legacy JSON file
       shape; only consulted when neither of the above produced
       entries and the descriptor declared the legacy filename).

    A bundle / descriptor with no presets at all returns an empty
    list. Files that exist but are empty / malformed also return an
    empty list rather than raising — a missing or broken preset file
    is a tool-author concern surfaced through `matofx tool validate`,
    not a user-facing crash on the workspace.
    """
    # Descriptor-authored presets take precedence — once a tool has
    # been re-saved through the wizard, the bundle files are stale
    # and we'd rather show the operator's edits than the older file.
    if descriptor.presets:
        return [
            Preset(name=p.name, description=p.description, values=dict(p.values))
            for p in descriptor.presets
        ]

    bundle = Path(bundle_path)

    toml_path = bundle / _TOML_PRESETS_FILENAME
    if toml_path.is_file():
        return _safe_load(toml_path, _parse_toml_presets)

    if descriptor.presets_filename:
        json_path = bundle / descriptor.presets_filename
        if json_path.is_file():
            return _safe_load(json_path, _parse_json_presets)

    return []


def _safe_load(path: Path, parser: Callable[[bytes], list[Preset]]) -> list[Preset]:
    """Read + parse; swallow file/parse errors as "no presets"."""
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    try:
        return parser(raw)
    except (tomllib.TOMLDecodeError, json.JSONDecodeError, ValueError, TypeError):
        return []


def _parse_toml_presets(raw: bytes) -> list[Preset]:
    """Parse the v2 ``presets.toml`` shape."""
    data = tomllib.loads(raw.decode("utf-8"))
    presets_array = data.get("preset")
    if not isinstance(presets_array, list):
        return []

    out: list[Preset] = []
    for entry in presets_array:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        description = str(entry.get("description") or "").strip()
        values = entry.get("values")
        if not isinstance(values, dict):
            continue
        out.append(Preset(name=name, description=description, values=dict(values)))
    return out


def _parse_json_presets(raw: bytes) -> list[Preset]:
    """Parse the legacy ``<alias>_presets.json`` flat-dict shape."""
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        return []

    out: list[Preset] = []
    for name, values in data.items():
        # Skip malformed entries silently — we don't crash a workspace
        # on a half-edited preset file.
        name_str = str(name).strip()
        if not name_str or not isinstance(values, dict):
            continue
        out.append(Preset(name=name_str, description="", values=dict(values)))
    return out


# --- Previous-job inputs -------------------------------------------------


def from_previous_job_inputs(inputs_doc: InputsDocument | dict[str, Any]) -> dict[str, Any]:
    """Extract a ``{name: value}`` map from a previous job's ``inputs.json``.

    Accepts either a parsed :class:`InputsDocument` or the raw dict
    form (the latter is what the API layer typically hands us). The
    ACL check ("can this user see that job?") happens in the API
    layer; this function is pure data-shape conversion.

    Only the ``name`` and ``value`` fields are propagated — units,
    types, and labels live on the descriptor and are re-applied by
    the renderer when the seed lands in a fresh form.
    """
    inputs_iter = _iter_input_values(inputs_doc)
    return {entry["name"]: entry["value"] for entry in inputs_iter if "name" in entry}


def _iter_input_values(doc: InputsDocument | dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise both the typed and raw-dict shapes to a list of ``{name, value}``."""
    if isinstance(doc, InputsDocument):
        return [{"name": iv.name, "value": iv.value} for iv in doc.inputs]
    if isinstance(doc, dict):
        raw_inputs = doc.get("inputs")
        if isinstance(raw_inputs, list):
            out: list[dict[str, Any]] = []
            for entry in raw_inputs:
                if isinstance(entry, dict) and "name" in entry:
                    out.append({"name": entry["name"], "value": entry.get("value")})
            return out
    return []


# --- Descriptor defaults -------------------------------------------------


def from_descriptor_defaults(descriptor: ToolDescriptor) -> dict[str, Any]:
    """Return ``{name: default}`` for every input that declares a default.

    Used by the "Reset to defaults" popover. Inputs without a
    declared default (genuinely required + no pre-fill) are excluded
    so the seed doesn't blank them out — the renderer's
    ``fill_missing_with_defaults`` flag handles the rest.
    """
    return {f.name: f.default for f in descriptor.inputs if f.default is not None}
