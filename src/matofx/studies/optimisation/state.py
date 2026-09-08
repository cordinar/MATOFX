"""Durable optimiser-state persistence (ADR-0009 §10).

The optimiser's serialised state is written after every batch so a
runner restart can rehydrate and continue:

- **inline JSON** into ``optimisation_study.optimiser_state_json`` when
  the serialised state is ≤ 1 MB (the common case — the baselines, small
  BO runs);
- a **disk blob** (``joblib``) under
  ``<blob_root>/opt_<id>/state.pkl`` when it would exceed 1 MB, with a
  sha256 the load step verifies.

``joblib`` is used for the blob (never raw ``pickle.load`` in the web
process — non-negotiable #4); the runner-hosted runtime owns
deserialisation. A blob whose digest doesn't match — or that won't load
— raises :class:`OptimiserStateCorruptError`, which the runtime turns
into a parked study rather than continuing on bad state (#3).

The convergence trace, best-so-far, and batch number are *not* handled
here — they are cheap JSON columns the runtime writes directly and are
always the readable source for the Convergence tab even when this heavy
state is blob-backed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from matofx.studies.optimisation.errors import OptimiserStateCorruptError

if TYPE_CHECKING:
    from matofx.studies.optimisation.orm import OptimisationStudy

_MAX_INLINE_BYTES = 1_000_000
"""Serialised-state size at/above which the state spills to a disk blob (§10)."""


def persist_optimiser_state(
    study: OptimisationStudy, payload: dict[str, object], *, blob_root: Path | None
) -> None:
    """Serialise the optimiser-state ``payload`` onto the study row (§10).

    ``payload`` is the runtime's JSON-serialisable state envelope (the
    strategy dump plus the budget counters). Small payloads go inline as
    JSON; large ones spill to a ``joblib`` blob under ``blob_root`` with a
    sha256. When the payload is large but no ``blob_root`` is available
    (no storage location resolved), it is kept inline regardless — the
    1 MB rule is a performance guideline, not a correctness constraint,
    and a running study must never fail to persist.
    """
    size = len(json.dumps(payload, default=str).encode("utf-8"))
    if size <= _MAX_INLINE_BYTES or blob_root is None:
        study.optimiser_state_json = payload
        study.optimiser_state_blob_path = None
        study.optimiser_state_sha256 = None
        return

    import joblib  # noqa: PLC0415

    target_dir = Path(blob_root) / f"opt_{study.id}"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "state.pkl"
    joblib.dump(payload, path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    study.optimiser_state_json = None
    study.optimiser_state_blob_path = str(path)
    study.optimiser_state_sha256 = digest


def load_optimiser_state(study: OptimisationStudy) -> dict[str, object] | None:
    """Read the persisted optimiser state back, or ``None`` if never written.

    Verifies the disk blob's sha256 before deserialising; a mismatch or a
    load failure raises :class:`OptimiserStateCorruptError`.
    """
    if study.optimiser_state_json is not None:
        return dict(study.optimiser_state_json)
    blob_path = study.optimiser_state_blob_path
    if not blob_path:
        return None

    path = Path(blob_path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise OptimiserStateCorruptError(
            f"The optimiser state file for study {study.id} couldn't be read ({path})."
        ) from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != study.optimiser_state_sha256:
        raise OptimiserStateCorruptError(
            f"The optimiser state for study {study.id} failed its integrity check "
            "(the file changed since it was written)."
        )
    import joblib  # noqa: PLC0415

    try:
        loaded = joblib.load(path)
    except Exception as exc:
        raise OptimiserStateCorruptError(
            f"The optimiser state for study {study.id} couldn't be deserialised."
        ) from exc
    return dict(loaded)
