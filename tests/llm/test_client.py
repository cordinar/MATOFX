"""Slice 6 — LlmSettings validation + the OpenAI-compatible adapter.

The adapter is exercised over an ``httpx.MockTransport`` so no network is
touched; every transport / decode failure must surface as an ``LlmError``
subclass (the orchestrator's silent-fallback contract depends on it).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from matofx.config import LlmSettings
from matofx.llm import (
    LlmRequest,
    LlmResponseError,
    LlmTimeoutError,
    LlmTransportError,
    make_llm_client,
)
from matofx.llm.adapters import OpenAICompatibleClient


def _settings(**overrides: Any) -> LlmSettings:
    base: dict[str, Any] = {
        "provider": "openai_compatible",
        "base_url": "https://gateway.internal/v1",
        "model": "gpt-4o-mini",
    }
    base.update(overrides)
    return LlmSettings(**base)


def _client(handler: Any) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(_settings(), transport=httpx.MockTransport(handler))


def _ok_body(text: str = "Enriched prose.") -> dict[str, Any]:
    return {
        "model": "gpt-4o-mini",
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 412, "completion_tokens": 138},
    }


# --- settings validation -----------------------------------------------------


def test_disabled_is_the_default() -> None:
    assert LlmSettings().provider == "disabled"


def test_openai_compatible_requires_base_url_and_model() -> None:
    with pytest.raises(ValidationError):
        LlmSettings(provider="openai_compatible", model="m")  # no base_url
    with pytest.raises(ValidationError):
        LlmSettings(provider="openai_compatible", base_url="https://x/v1")  # no model


def test_azure_requires_base_url_and_api_version() -> None:
    with pytest.raises(ValidationError):
        LlmSettings(provider="azure_openai", base_url="https://x", model="dep")


def test_bedrock_requires_region() -> None:
    with pytest.raises(ValidationError):
        LlmSettings(provider="bedrock", model="anthropic.claude")


def test_temperature_defaults_to_zero() -> None:
    assert _settings().temperature == 0.0


# --- factory -----------------------------------------------------------------


def test_factory_returns_none_when_disabled() -> None:
    assert make_llm_client(LlmSettings()) is None


def test_factory_builds_openai_compatible() -> None:
    client = make_llm_client(_settings())
    assert isinstance(client, OpenAICompatibleClient)
    assert client.provider == "openai_compatible"
    assert client.model == "gpt-4o-mini"


# --- adapter happy path + failures ------------------------------------------


@pytest.mark.asyncio
async def test_generate_returns_text_and_tokens() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_ok_body())

    client = _client(handler)
    result = await client.generate(LlmRequest(system="sys", user="usr"))
    assert result.text == "Enriched prose."
    assert result.input_tokens == 412
    assert result.output_tokens == 138
    assert captured["url"].endswith("/chat/completions")


@pytest.mark.asyncio
async def test_non_2xx_maps_to_response_error() -> None:
    client = _client(lambda _request: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(LlmResponseError):
        await client.generate(LlmRequest(system="s", user="u"))


@pytest.mark.asyncio
async def test_empty_completion_maps_to_response_error() -> None:
    body = {"choices": [{"message": {"content": "   "}}]}
    client = _client(lambda _request: httpx.Response(200, json=body))
    with pytest.raises(LlmResponseError):
        await client.generate(LlmRequest(system="s", user="u"))


@pytest.mark.asyncio
async def test_no_choices_maps_to_response_error() -> None:
    client = _client(lambda _request: httpx.Response(200, json={"choices": []}))
    with pytest.raises(LlmResponseError):
        await client.generate(LlmRequest(system="s", user="u"))


@pytest.mark.asyncio
async def test_timeout_maps_to_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LlmTimeoutError):
        await _client(handler).generate(LlmRequest(system="s", user="u"))


@pytest.mark.asyncio
async def test_connect_error_maps_to_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LlmTransportError):
        await _client(handler).generate(LlmRequest(system="s", user="u"))
