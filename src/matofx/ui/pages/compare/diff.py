"""Pure diff logic for the Compare workspace.

A :class:`JobBundle` is the minimal projection of one selected job:
its inputs and its flattened outputs (variable name → value, or file
output name → filename). :func:`compute_differing_keys` walks every
bundle and returns the set of keys whose serialised values aren't all
equal. The Compare page renders those keys bold + Funis-red so the
user can spot what changed at a glance.

Intentionally has zero NiceGUI / SQLAlchemy / network dependency —
makes it cheap to unit-test and keeps the UI module thin.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class JobBundle:
    """One job's inputs + outputs, flattened for diffing.

    Keep this slim: the diff doesn't need state, timestamps, or descriptor
    metadata. The Compare page builds a richer view-model for rendering;
    this projection is the part the diff cares about.
    """

    job_id: int
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)


_MISSING = object()
"""Sentinel for "this key didn't exist in this bundle" — distinct from
a stored ``None``, so a key present-with-None in one bundle and absent
in another is correctly flagged as differing."""


def compute_differing_keys(bundles: Iterable[JobBundle]) -> set[str]:
    """Return the set of input + output keys whose values aren't all equal.

    Two or more bundles are required for any key to be flagged — with
    zero or one bundle there's nothing to compare against, so the
    result is always the empty set.

    Comparison is by JSON-canonical serialisation (``sort_keys=True``)
    so equal dicts in different key order count as equal, and lists
    are compared element-wise. Values that aren't JSON-serialisable
    fall back to ``repr`` — exotic types still compare consistently
    with themselves, which is the only invariant the diff needs.
    """
    materialised = list(bundles)
    if len(materialised) < 2:
        return set()

    differing: set[str] = set()
    for key in _all_keys(materialised):
        if not _values_equal(key, materialised):
            differing.add(key)
    return differing


def _all_keys(bundles: list[JobBundle]) -> set[str]:
    """Union of every input key and every output key across all bundles."""
    keys: set[str] = set()
    for bundle in bundles:
        keys.update(bundle.inputs.keys())
        keys.update(bundle.outputs.keys())
    return keys


def _values_equal(key: str, bundles: list[JobBundle]) -> bool:
    """``True`` if every bundle reports the same value for ``key``."""
    encoded = {_encode(_lookup(key, bundle)) for bundle in bundles}
    return len(encoded) == 1


def _lookup(key: str, bundle: JobBundle) -> Any:
    """Look the key up in inputs first, then outputs, returning ``_MISSING`` if neither."""
    if key in bundle.inputs:
        return bundle.inputs[key]
    if key in bundle.outputs:
        return bundle.outputs[key]
    return _MISSING


def _encode(value: Any) -> str:
    """Serialise a value to a deterministic string for set-membership comparison.

    JSON with ``sort_keys=True`` covers the common cases (numbers,
    strings, lists, dicts of the same). The ``repr`` fallback handles
    odd values that the runner might surface (Pydantic models, custom
    classes, the ``_MISSING`` sentinel) — equal-by-repr is enough for
    our use, since we only need a stable identity per value.
    """
    if value is _MISSING:
        return "\x00MISSING"
    try:
        return json.dumps(value, sort_keys=True, default=repr)
    except (TypeError, ValueError):
        return repr(value)
