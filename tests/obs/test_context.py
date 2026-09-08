"""Tests for the contextvars-backed binding helpers."""

from __future__ import annotations

import asyncio
import io

from matofx.obs import bind, current_context, get_logger
from tests.obs.conftest import read_lines


def test_unbound_context_is_empty() -> None:
    assert dict(current_context()) == {}


def test_bind_attaches_fields(log_stream: io.StringIO) -> None:
    log = get_logger("matofx.test")
    with bind(request_id="r-1", user_id=7):
        log.info("inside")
    log.info("outside")
    inside, outside = read_lines(log_stream)
    assert inside["request_id"] == "r-1"
    assert inside["user_id"] == 7
    assert "request_id" not in outside
    assert "user_id" not in outside


def test_nested_bind_merges_and_inner_wins(log_stream: io.StringIO) -> None:
    log = get_logger("matofx.test")
    with bind(request_id="outer", scope="a"):
        with bind(request_id="inner"):
            log.info("nested")
        log.info("after inner")
    rec_inner, rec_after = read_lines(log_stream)
    assert rec_inner["request_id"] == "inner"
    assert rec_inner["scope"] == "a"
    assert rec_after["request_id"] == "outer"


def test_explicit_extra_overrides_context(log_stream: io.StringIO) -> None:
    log = get_logger("matofx.test")
    with bind(user_id=1):
        log.info("override", extra={"user_id": 999})
    record = read_lines(log_stream)[0]
    assert record["user_id"] == 999


def test_context_propagates_through_await(log_stream: io.StringIO) -> None:
    log = get_logger("matofx.test")

    async def child() -> None:
        log.info("from child")

    async def parent() -> None:
        with bind(request_id="async-r"):
            await child()

    asyncio.run(parent())
    record = read_lines(log_stream)[0]
    assert record["request_id"] == "async-r"


def test_context_isolated_between_concurrent_tasks(log_stream: io.StringIO) -> None:
    log = get_logger("matofx.test")

    async def worker(rid: str) -> None:
        with bind(request_id=rid):
            await asyncio.sleep(0)
            log.info("hi")

    async def driver() -> None:
        await asyncio.gather(worker("a"), worker("b"), worker("c"))

    asyncio.run(driver())
    seen = {r["request_id"] for r in read_lines(log_stream)}
    assert seen == {"a", "b", "c"}
