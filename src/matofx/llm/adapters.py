"""Provider adapters behind the :class:`~matofx.llm.client.LlmClient` protocol.

Four providers, one protocol:

* **OpenAI-compatible** — a plain ``POST /chat/completions`` over ``httpx``;
  covers most internal corporate gateways + self-hosted vLLM.
* **Ollama** — the same OpenAI-compatible shape against a local Ollama's
  ``/v1`` endpoint (no key); a thin subclass for honest provider labelling.
* **Azure OpenAI** — same body, Azure's deployment URL + ``api-key`` header.
* **AWS Bedrock** — the model-agnostic ``converse`` API via a lazily-imported
  ``boto3`` (an optional extra), run off the event loop.

All HTTP providers use the already-present ``httpx`` dependency. Every transport
/ decode failure maps to an :class:`~matofx.llm.errors.LlmError` subclass so the
orchestrator falls back to the templated baseline on any of them.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from matofx.config import LlmSettings
from matofx.llm.client import LlmClient, LlmRequest, LlmResponse
from matofx.llm.errors import (
    LlmConfigError,
    LlmError,
    LlmResponseError,
    LlmTimeoutError,
    LlmTransportError,
)


def _chat_body(
    model: str, request: LlmRequest, max_tokens: int, temperature: float
) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": request.system},
            {"role": "user", "content": request.user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }


async def _post_chat_completion(
    *,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
    transport: httpx.AsyncBaseTransport | None,
    fallback_model: str,
) -> LlmResponse:
    """POST an OpenAI-shaped chat request and decode it (errors → LlmError)."""
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.post(url, json=body, headers=headers)
    except httpx.TimeoutException as exc:
        raise LlmTimeoutError(f"LLM request timed out after {timeout}s") from exc
    except httpx.HTTPError as exc:
        raise LlmTransportError(f"LLM transport error: {exc}") from exc

    if response.status_code >= httpx.codes.BAD_REQUEST:
        raise LlmResponseError(f"LLM endpoint returned HTTP {response.status_code}")
    return _parse_chat_completion(_safe_json(response), fallback_model)


class OpenAICompatibleClient:
    """An OpenAI ``/chat/completions``-shaped endpoint over ``httpx``."""

    provider = "openai_compatible"

    def __init__(
        self, settings: LlmSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """Capture the endpoint wiring; ``transport`` is for test injection."""
        if not settings.base_url:  # pragma: no cover - guarded by LlmSettings
            raise LlmConfigError(f"{self.provider} requires base_url")
        self.model = settings.model
        self._url = settings.base_url.rstrip("/") + "/chat/completions"
        self._api_key = settings.api_key.get_secret_value() if settings.api_key else None
        self._timeout = settings.timeout_seconds
        self._max_tokens = settings.max_output_tokens
        self._temperature = settings.temperature
        self._transport = transport

    async def generate(self, request: LlmRequest) -> LlmResponse:
        """POST the prompt and decode the completion, mapping failures to LlmError."""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return await _post_chat_completion(
            url=self._url,
            headers=headers,
            body=_chat_body(self.model, request, self._max_tokens, self._temperature),
            timeout=self._timeout,
            transport=self._transport,
            fallback_model=self.model,
        )


class OllamaClient(OpenAICompatibleClient):
    """A local Ollama via its OpenAI-compatible ``/v1`` endpoint (no key)."""

    provider = "ollama"


class AzureOpenAIClient:
    """Azure OpenAI: the deployment URL + ``api-key`` header, OpenAI body."""

    provider = "azure_openai"

    def __init__(
        self, settings: LlmSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """Capture the Azure deployment wiring; ``transport`` is for tests."""
        if not settings.base_url or not settings.api_version:  # pragma: no cover
            raise LlmConfigError("azure_openai requires base_url + api_version")
        self.model = settings.model  # the deployment name
        self._url = (
            f"{settings.base_url.rstrip('/')}/openai/deployments/{settings.model}"
            f"/chat/completions?api-version={settings.api_version}"
        )
        self._api_key = settings.api_key.get_secret_value() if settings.api_key else None
        self._timeout = settings.timeout_seconds
        self._max_tokens = settings.max_output_tokens
        self._temperature = settings.temperature
        self._transport = transport

    async def generate(self, request: LlmRequest) -> LlmResponse:
        """POST to the Azure deployment, mapping failures to LlmError."""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["api-key"] = self._api_key
        return await _post_chat_completion(
            url=self._url,
            headers=headers,
            body=_chat_body(self.model, request, self._max_tokens, self._temperature),
            timeout=self._timeout,
            transport=self._transport,
            fallback_model=self.model,
        )


class BedrockClient:
    """AWS Bedrock via the model-agnostic ``converse`` API (lazy ``boto3``).

    ``boto3`` is an optional extra (``matofx[bedrock]``) imported only here, so
    the base install stays lean and air-gap-clean. The sync SDK call runs off
    the event loop via :func:`asyncio.to_thread`. Tests inject a fake
    ``runtime_client`` to avoid the dependency.
    """

    provider = "bedrock"

    def __init__(self, settings: LlmSettings, *, runtime_client: Any | None = None) -> None:
        """Capture the Bedrock wiring; ``runtime_client`` is for test injection."""
        if not settings.region:  # pragma: no cover - guarded by LlmSettings
            raise LlmConfigError("bedrock requires region")
        self.model = settings.model
        self._region = settings.region
        self._max_tokens = settings.max_output_tokens
        self._temperature = settings.temperature
        self._timeout = settings.timeout_seconds
        self._runtime = runtime_client

    def _client(self) -> Any:
        if self._runtime is not None:
            return self._runtime
        try:
            import boto3  # noqa: PLC0415 - optional extra, lazy on purpose.
            from botocore.config import Config  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - exercised via a clear error
            raise LlmConfigError(
                "llm.provider='bedrock' needs the optional 'boto3' dependency "
                "(install matofx[bedrock])."
            ) from exc
        return boto3.client(
            "bedrock-runtime",
            region_name=self._region,
            config=Config(read_timeout=self._timeout),
        )

    async def generate(self, request: LlmRequest) -> LlmResponse:
        """Call Bedrock ``converse`` off the event loop, mapping failures."""
        try:
            data = await asyncio.to_thread(self._invoke, request)
        except LlmError:
            raise  # a config error (e.g. missing boto3) surfaces as itself.
        except Exception as exc:  # any SDK error → recoverable transport error.
            raise LlmTransportError(f"Bedrock error: {exc}") from exc
        return _parse_converse(data, self.model)

    def _invoke(self, request: LlmRequest) -> dict[str, Any]:
        result = self._client().converse(
            modelId=self.model,
            system=[{"text": request.system}],
            messages=[{"role": "user", "content": [{"text": request.user}]}],
            inferenceConfig={
                "maxTokens": self._max_tokens,
                "temperature": self._temperature,
            },
        )
        return dict(result)


def build_adapter(settings: LlmSettings) -> LlmClient:
    """Select the adapter for ``settings.provider``."""
    if settings.provider == "openai_compatible":
        return OpenAICompatibleClient(settings)
    if settings.provider == "ollama":
        return OllamaClient(settings)
    if settings.provider == "azure_openai":
        return AzureOpenAIClient(settings)
    if settings.provider == "bedrock":
        return BedrockClient(settings)
    raise LlmConfigError(  # pragma: no cover - guarded by LlmSettings
        f"llm.provider={settings.provider!r} has no adapter"
    )


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise LlmResponseError("LLM endpoint returned a non-JSON body") from exc
    if not isinstance(data, dict):
        raise LlmResponseError("LLM endpoint returned an unexpected JSON shape")
    return data


def _parse_chat_completion(data: dict[str, Any], fallback_model: str) -> LlmResponse:
    """Extract message content + token usage from a chat-completion body."""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmResponseError("LLM response carried no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise LlmResponseError("LLM response carried no completion text")
    usage_raw = data.get("usage")
    usage = usage_raw if isinstance(usage_raw, dict) else {}
    return LlmResponse(
        text=content.strip(),
        model=str(data.get("model") or fallback_model),
        input_tokens=_int_or_none(usage.get("prompt_tokens")),
        output_tokens=_int_or_none(usage.get("completion_tokens")),
    )


def _parse_converse(data: dict[str, Any], fallback_model: str) -> LlmResponse:
    """Extract text + token usage from a Bedrock ``converse`` response."""
    output = data.get("output") if isinstance(data, dict) else None
    message = output.get("message") if isinstance(output, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    text = ""
    if isinstance(content, list) and content and isinstance(content[0], dict):
        text = str(content[0].get("text") or "")
    if not text.strip():
        raise LlmResponseError("Bedrock response carried no completion text")
    usage_raw = data.get("usage")
    usage = usage_raw if isinstance(usage_raw, dict) else {}
    return LlmResponse(
        text=text.strip(),
        model=fallback_model,
        input_tokens=_int_or_none(usage.get("inputTokens")),
        output_tokens=_int_or_none(usage.get("outputTokens")),
    )


def _int_or_none(value: Any) -> int | None:
    return int(value) if isinstance(value, int) else None
