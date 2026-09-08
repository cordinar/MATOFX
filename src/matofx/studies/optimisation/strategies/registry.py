"""The strategy registry — a closed, pluggable catalogue (ADR-0009 §5).

Adding a strategy = a new module + a :func:`register` call at import. The
submit form's strategy dropdown and the configure-time validator read the
catalogue; the objective-shape map is the one in
:mod:`matofx.studies.optimisation.config` (``STRATEGY_OBJECTIVE_SHAPES``)
so there is a single source of truth — :func:`register` cross-checks
against it and refuses a name/shape the validator doesn't know about.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from matofx.studies.optimisation.config import STRATEGY_OBJECTIVE_SHAPES

if TYPE_CHECKING:
    from matofx.studies.optimisation.strategies.base import Strategy

_REGISTRY: dict[str, type[Strategy]] = {}


class StrategyRegistryError(Exception):
    """A strategy registration or lookup failed (programmer error, not user-facing)."""


def register(name: str, strategy_cls: type[Strategy]) -> None:
    """Register ``strategy_cls`` under ``name`` (idempotent on re-import).

    Refuses a name absent from the config's
    ``STRATEGY_OBJECTIVE_SHAPES`` catalogue, and a class whose
    ``objective_shapes`` disagree with it — keeping the runtime registry
    and the configure-time validator from drifting apart.
    """
    if name not in STRATEGY_OBJECTIVE_SHAPES:
        raise StrategyRegistryError(
            f"Strategy {name!r} isn't in the config objective-shape catalogue."
        )
    declared = STRATEGY_OBJECTIVE_SHAPES[name]
    if strategy_cls.objective_shapes != declared:
        raise StrategyRegistryError(
            f"Strategy {name!r} declares shapes {set(strategy_cls.objective_shapes)} "
            f"but the catalogue says {set(declared)}."
        )
    _REGISTRY[name] = strategy_cls


def get(name: str) -> Strategy:
    """Instantiate the registered strategy ``name`` (unbound)."""
    try:
        strategy_cls = _REGISTRY[name]
    except KeyError as exc:
        raise StrategyRegistryError(f"No strategy registered under {name!r}.") from exc
    return strategy_cls()


def is_registered(name: str) -> bool:
    """Whether ``name`` has a runtime implementation (vs only a catalogue entry)."""
    return name in _REGISTRY


def registered_names() -> frozenset[str]:
    """The strategies with a runtime implementation available."""
    return frozenset(_REGISTRY)
