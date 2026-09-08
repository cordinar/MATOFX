"""Exceptions for the LLM enrichment client.

Every failure here is *recoverable* from the summary engine's point of view —
the orchestration layer (Slice 7) catches :class:`LlmError` and falls back to
the deterministic templated baseline. Nothing in this package ever propagates
an exception that could fail a job or a study.
"""

from __future__ import annotations


class LlmError(Exception):
    """Base class for every LLM-client failure (all → templated fallback)."""


class LlmConfigError(LlmError):
    """The configured provider could not be constructed (bad/absent wiring)."""


class LlmTimeoutError(LlmError):
    """The model did not respond within ``timeout_seconds``."""


class LlmTransportError(LlmError):
    """A network / connection error reaching the endpoint."""


class LlmResponseError(LlmError):
    """A non-2xx status, malformed body, or empty completion."""
