"""The ``SummaryFacts`` grounding contract.

One small, closed, JSON-serialisable fact set per result surface (job /
workflow run / parametric study / optimisation study). It is the *single*
source of truth that both the deterministic templated narrative and the
optional LLM enrichment draw from — so the two paths can never disagree on a
number, and the templated text becomes the ground truth the guard stack
(``matofx.llm.grounding``) checks the model output against.

Everything here is plain scalars / lists / dicts so a facts object can be
canonicalised to JSON, hashed (the cache-invalidation key), and handed to a
language model verbatim. Per-surface ``collect_*`` functions are added by the
slices that own each surface; this module owns the data shape and the hash.

Display strings carry descriptor **labels**, never internal aliases
(``feedback_use_labels_not_names``): collectors map keys → labels before
populating facts.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

SurfaceKind = Literal[
    "job",
    "workflow_run",
    "parametric_study",
    "optimisation_study",
]
"""Closed vocabulary of result surfaces a summary can describe."""

SURFACE_KINDS: tuple[SurfaceKind, ...] = (
    "job",
    "workflow_run",
    "parametric_study",
    "optimisation_study",
)

TERMINAL_STATES: frozenset[str] = frozenset({"complete", "failed", "partial", "cancelled"})
"""States for which a summary is produced. A collector handed anything else
raises :class:`~matofx.summaries.errors.NonTerminalResultError`."""


@dataclass(frozen=True)
class HeadlineNumber:
    """One headline figure — a KPI, an objective best, a response extreme.

    ``value`` may be ``None`` when the figure is known-absent (rendered as
    "not available" rather than guessed). ``unit`` is the descriptor unit and
    is matched verbatim by the enrichment unit-guard.
    """

    label: str
    value: float | int | str | None
    unit: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict form for JSON serialisation and hashing."""
        return {"label": self.label, "value": self.value, "unit": self.unit}


@dataclass(frozen=True)
class SummaryFacts:
    """The grounded fact set for one terminal result.

    Frozen so it can be safely shared between the templated and enrichment
    paths. ``details`` is a free-form (but JSON-serialisable) mapping carrying
    surface-specific facts — e.g. a parametric study's method-aware
    ``analysis`` block (design §3.1). New fields are added with defaults so
    collectors can grow without breaking the hash contract for callers that
    do not set them.
    """

    surface_kind: SurfaceKind
    surface_id: int
    title: str
    state: str
    headline: tuple[HeadlineNumber, ...] = ()
    submitted_by: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Canonical plain-dict form — the JSON the hash and the LLM see."""
        return {
            "surface_kind": self.surface_kind,
            "surface_id": self.surface_id,
            "title": self.title,
            "state": self.state,
            "headline": [h.to_dict() for h in self.headline],
            "submitted_by": self.submitted_by,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "details": dict(self.details),
        }

    def facts_hash(self) -> str:
        """SHA-256 of the canonical JSON — the cache-invalidation key.

        Deterministic across processes: keys sorted, no insignificant
        whitespace, non-JSON values coerced via ``str``. A change to *any*
        fact (a KPI re-extraction, a new child job) changes the hash, which is
        how a stale cached summary is invalidated.
        """
        canonical = json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_terminal(state: str) -> bool:
    """True when a result in ``state`` is eligible for a summary."""
    return state in TERMINAL_STATES
