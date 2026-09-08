"""Error hierarchy for the surrogate Model registry (ADR-0008 §5/§6).

All messages are plain-English (F30) — they surface verbatim in the API
and the registry UI.
"""

from __future__ import annotations


class ModelError(Exception):
    """Base for every Model-registry failure."""


class ModelConfigError(ModelError):
    """A model create / refit request is malformed or unsatisfiable.

    Raised when the chosen source has too few complete runs to fit, a
    response name doesn't resolve, or a scope/argument combination is
    invalid.
    """


class ModelStateError(ModelError):
    """A registry action was attempted from the wrong state / scope.

    Plain-English (F30) — e.g. retiring an already-retired model, or
    (in Phase 5a) creating a ``global`` model before the governance gate
    is available.
    """


class ModelBundleCorruptError(ModelError):
    """A model bundle failed its sha256 integrity check on load (§5.3).

    Non-negotiable #2: the runner never returns predictions from a
    corrupt bundle — it raises this instead.
    """
