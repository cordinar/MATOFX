"""LLM client contract + factory for auto-summary enrichment.

One small protocol — :class:`LlmClient` — with a single ``generate`` call that
takes a system+user prompt and returns the completion text plus optional token
counts. Adapters per provider live in :mod:`matofx.llm.adapters`;
:func:`make_llm_client` selects one from :class:`~matofx.config.LlmSettings`, or
returns ``None`` when the provider is ``disabled`` (the default), so the rest of
the LLM path is never constructed on an install that hasn't opted in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from matofx.config import LlmSettings


@dataclass(frozen=True)
class LlmRequest:
    """A grounded prompt: a system instruction + the user content."""

    system: str
    user: str


@dataclass(frozen=True)
class LlmResponse:
    """A completion plus the metadata the audit record carries."""

    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


@runtime_checkable
class LlmClient(Protocol):
    """The minimal surface the enrichment orchestrator depends on."""

    provider: str
    model: str

    async def generate(self, request: LlmRequest) -> LlmResponse:
        """Return a completion, or raise a :class:`~matofx.llm.errors.LlmError`."""
        ...


def make_llm_client(settings: LlmSettings) -> LlmClient | None:
    """Construct the client for ``settings.provider``, or ``None`` if disabled.

    Raises :class:`~matofx.llm.errors.LlmConfigError` for a provider that is
    recognised but not yet wired, so a misconfiguration surfaces clearly rather
    than silently disabling enrichment.
    """
    if settings.provider == "disabled":
        return None
    # Lazy import so the adapter module (and any optional deps it touches) is
    # only loaded once an install has actually configured a provider.
    from matofx.llm.adapters import build_adapter  # noqa: PLC0415

    return build_adapter(settings)
