"""Slice 9 — Azure OpenAI, Ollama, and AWS Bedrock adapters.

HTTP providers run over an ``httpx.MockTransport``; Bedrock runs over an
injected fake ``boto3`` runtime client. Also pins that the base install imports
the adapter module without ``boto3`` and that a Bedrock call without the extra
fails with a clear ``LlmConfigError``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from matofx.config import LlmSettings
from matofx.llm.adapters import (
    AzureOpenAIClient,
    BedrockClient,
    OllamaClient,
    OpenAICompatibleClient,
    build_adapter,
)
from matofx.llm.client import LlmRequest
from matofx.llm.errors import LlmConfigError, LlmResponseError, LlmTransportError


def _chat_ok(text: str = "Prose.") -> dict[str, Any]:
    return {
        "model": "m",
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7},
    }


# --- factory dispatch --------------------------------------------------------


def test_factory_dispatches_each_provider() -> None:
    assert isinstance(
        build_adapter(LlmSettings(provider="ollama", base_url="http://x/v1", model="m")),
        OllamaClient,
    )
    assert isinstance(
        build_adapter(
            LlmSettings(
                provider="azure_openai",
                base_url="https://r.openai.azure.com",
                model="dep",
                api_version="2024-06-01",
            )
        ),
        AzureOpenAIClient,
    )
    assert isinstance(
        build_adapter(LlmSettings(provider="bedrock", region="eu-west-1", model="claude")),
        BedrockClient,
    )


# --- Ollama (OpenAI-compatible, no key) -------------------------------------


@pytest.mark.asyncio
async def test_ollama_returns_text() -> None:
    settings = LlmSettings(provider="ollama", base_url="http://localhost:11434/v1", model="m")
    client = OllamaClient(
        settings,
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=_chat_ok())),
    )
    assert client.provider == "ollama"
    result = await client.generate(LlmRequest(system="s", user="u"))
    assert result.text == "Prose."


# --- Azure OpenAI ------------------------------------------------------------


@pytest.mark.asyncio
async def test_azure_uses_deployment_url_and_api_key_header() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["api_key"] = request.headers.get("api-key")
        return httpx.Response(200, json=_chat_ok())

    settings = LlmSettings(
        provider="azure_openai",
        base_url="https://res.openai.azure.com",
        model="gpt4dep",
        api_version="2024-06-01",
        api_key="secret",  # type: ignore[arg-type]
    )
    client = AzureOpenAIClient(settings, transport=httpx.MockTransport(handler))
    result = await client.generate(LlmRequest(system="s", user="u"))
    assert result.text == "Prose."
    assert "/openai/deployments/gpt4dep/chat/completions" in captured["url"]
    assert "api-version=2024-06-01" in captured["url"]
    assert captured["api_key"] == "secret"


@pytest.mark.asyncio
async def test_azure_non_2xx_maps_to_response_error() -> None:
    settings = LlmSettings(
        provider="azure_openai",
        base_url="https://res.openai.azure.com",
        model="dep",
        api_version="2024-06-01",
    )
    client = AzureOpenAIClient(
        settings, transport=httpx.MockTransport(lambda _r: httpx.Response(429))
    )
    with pytest.raises(LlmResponseError):
        await client.generate(LlmRequest(system="s", user="u"))


# --- Bedrock (injected fake runtime) ----------------------------------------


class FakeBedrockRuntime:
    def __init__(
        self,
        *,
        response: dict[str, Any] | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.response = response
        self.exc = exc
        self.kwargs: dict[str, Any] = {}

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs = kwargs
        if self.exc is not None:
            raise self.exc
        assert self.response is not None
        return self.response


def _bedrock_settings() -> LlmSettings:
    return LlmSettings(provider="bedrock", region="eu-west-1", model="anthropic.claude")


@pytest.mark.asyncio
async def test_bedrock_parses_converse_response() -> None:
    runtime = FakeBedrockRuntime(
        response={
            "output": {"message": {"content": [{"text": "Bedrock prose."}]}},
            "usage": {"inputTokens": 11, "outputTokens": 22},
        }
    )
    client = BedrockClient(_bedrock_settings(), runtime_client=runtime)
    result = await client.generate(LlmRequest(system="sys", user="usr"))
    assert result.text == "Bedrock prose."
    assert result.input_tokens == 11
    assert result.output_tokens == 22
    assert runtime.kwargs["modelId"] == "anthropic.claude"


@pytest.mark.asyncio
async def test_bedrock_sdk_error_maps_to_transport_error() -> None:
    runtime = FakeBedrockRuntime(exc=RuntimeError("throttled"))
    client = BedrockClient(_bedrock_settings(), runtime_client=runtime)
    with pytest.raises(LlmTransportError):
        await client.generate(LlmRequest(system="s", user="u"))


@pytest.mark.asyncio
async def test_bedrock_without_boto3_raises_config_error() -> None:
    # No runtime injected → it tries to import boto3, which the base dev env
    # doesn't install → a clear LlmConfigError (not an unhandled ImportError).
    try:
        import boto3  # noqa: F401, PLC0415
    except ImportError:
        client = BedrockClient(_bedrock_settings())
        with pytest.raises(LlmConfigError):
            await client.generate(LlmRequest(system="s", user="u"))
    else:  # pragma: no cover - only when boto3 happens to be installed
        pytest.skip("boto3 is installed; the missing-extra path can't be exercised")


def test_openai_compatible_still_the_default() -> None:
    client = build_adapter(
        LlmSettings(provider="openai_compatible", base_url="http://x/v1", model="m")
    )
    assert isinstance(client, OpenAICompatibleClient)
    assert client.provider == "openai_compatible"
