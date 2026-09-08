"""Slice 8 — the install-level enrichment gate (``enrichment_context_if_enabled``).

The safety-critical defaults: a ``disabled`` provider and an off
``llm_summaries_enabled`` setting both yield no enrichment context (templated
only). The on-branch builds a context once both are satisfied.
"""

from __future__ import annotations

import datetime as _dt
import json
from types import SimpleNamespace

import pytest

from matofx.audit import AuditWriter
from matofx.config import LlmSettings
from matofx.db import session_scope
from matofx.db.session import SessionFactory
from matofx.summaries import EnrichmentContext, enrichment_context_if_enabled
from matofx.tools.exec_orm import RuntimeSetting

pytestmark = pytest.mark.asyncio


def _settings(provider: str = "disabled", **kw: object) -> SimpleNamespace:
    return SimpleNamespace(llm=LlmSettings(provider=provider, **kw))  # type: ignore[arg-type]


def _wired() -> SimpleNamespace:
    return _settings("openai_compatible", base_url="https://x/v1", model="m")


async def _set_flag(factory: SessionFactory, *, value: bool) -> None:
    async with session_scope(factory) as session:
        session.add(
            RuntimeSetting(
                domain="general",
                key="llm_summaries_enabled",
                value_json=json.dumps(value),
                updated_at=_dt.datetime.now(tz=_dt.UTC),
            )
        )
        await session.flush()


async def test_disabled_provider_yields_no_context(
    api_factory: SessionFactory, api_audit: AuditWriter
) -> None:
    async with session_scope(api_factory) as session:
        ctx = await enrichment_context_if_enabled(
            session, settings=_settings(), audit=api_audit, actor="user:1"
        )
    assert ctx is None


async def test_setting_off_yields_no_context(
    api_factory: SessionFactory, api_audit: AuditWriter
) -> None:
    async with session_scope(api_factory) as session:
        ctx = await enrichment_context_if_enabled(
            session, settings=_wired(), audit=api_audit, actor="user:1"
        )
    assert ctx is None  # llm_summaries_enabled defaults off


async def test_provider_wired_and_setting_on_builds_context(
    api_factory: SessionFactory, api_audit: AuditWriter
) -> None:
    await _set_flag(api_factory, value=True)
    async with session_scope(api_factory) as session:
        ctx = await enrichment_context_if_enabled(
            session, settings=_wired(), audit=api_audit, actor="user:7"
        )
    assert isinstance(ctx, EnrichmentContext)
    assert ctx.client.provider == "openai_compatible"
    assert ctx.actor == "user:7"
