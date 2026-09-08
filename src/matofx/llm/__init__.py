"""LLM enrichment client for auto-summaries.

A thin, provider-pluggable client behind one protocol
(:class:`LlmClient`). Constructed only when an install has wired ``[llm]`` and
opted in; the templated-summary path never imports it. Design:
``designs/auto-summaries.md`` §5.

Grounding + the deterministic guard stack that make enrichment safe to ship live
in :mod:`matofx.llm.grounding`.
"""

from __future__ import annotations

from matofx.llm.client import LlmClient, LlmRequest, LlmResponse, make_llm_client
from matofx.llm.errors import (
    LlmConfigError,
    LlmError,
    LlmResponseError,
    LlmTimeoutError,
    LlmTransportError,
)

__all__ = [
    "LlmClient",
    "LlmConfigError",
    "LlmError",
    "LlmRequest",
    "LlmResponse",
    "LlmResponseError",
    "LlmTimeoutError",
    "LlmTransportError",
    "make_llm_client",
]
