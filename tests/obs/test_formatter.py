"""Tests for the JSON-Lines formatter."""

from __future__ import annotations

import io
import re

import pytest

from matofx.obs import get_logger
from tests.obs.conftest import read_lines


def test_emits_single_json_object_per_line(log_stream: io.StringIO) -> None:
    log = get_logger("matofx.test")
    log.info("hello")
    records = read_lines(log_stream)
    assert len(records) == 1
    assert records[0]["message"] == "hello"


def test_mandatory_fields_present(log_stream: io.StringIO) -> None:
    log = get_logger("matofx.test")
    log.warning("careful")
    record = read_lines(log_stream)[0]
    assert record["level"] == "warning"
    assert record["logger"] == "matofx.test"
    assert record["message"] == "careful"
    assert "timestamp" in record


def test_timestamp_is_iso8601_utc(log_stream: io.StringIO) -> None:
    log = get_logger("matofx.test")
    log.info("ping")
    record = read_lines(log_stream)[0]
    # 2026-05-03T12:34:56.123456Z
    timestamp = record["timestamp"]
    assert isinstance(timestamp, str)
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$", timestamp)


@pytest.mark.parametrize(
    ("method", "level"),
    [
        ("info", "info"),
        ("warning", "warning"),
        ("error", "error"),
        ("critical", "critical"),
    ],
)
def test_levels_lowercase(log_stream: io.StringIO, method: str, level: str) -> None:
    log = get_logger("matofx.test")
    getattr(log, method)("x")
    record = read_lines(log_stream)[0]
    assert record["level"] == level


def test_extras_promoted_to_top_level(log_stream: io.StringIO) -> None:
    log = get_logger("matofx.test")
    log.info("with extras", extra={"job_id": 42, "tool": "demo"})
    record = read_lines(log_stream)[0]
    assert record["job_id"] == 42
    assert record["tool"] == "demo"


def test_extras_colliding_with_reserved_fields_nested(log_stream: io.StringIO) -> None:
    log = get_logger("matofx.test")
    log.info("collision", extra={"level": "shenanigans", "ok": True})
    record = read_lines(log_stream)[0]
    assert record["level"] == "info"  # reserved field intact
    assert record["extra"] == {"level": "shenanigans"}
    assert record["ok"] is True


def test_exception_serialised(log_stream: io.StringIO) -> None:
    log = get_logger("matofx.test")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("blew up")
    record = read_lines(log_stream)[0]
    assert "exception" in record
    assert "ValueError: boom" in str(record["exception"])


def test_non_ascii_preserved_by_default(log_stream: io.StringIO) -> None:
    log = get_logger("matofx.test")
    log.info("こんにちは")
    text = log_stream.getvalue()
    assert "こんにちは" in text


def test_non_serialisable_extras_fall_back_to_str(log_stream: io.StringIO) -> None:
    class Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    log = get_logger("matofx.test")
    log.info("opaque extra", extra={"obj": Opaque()})
    record = read_lines(log_stream)[0]
    assert record["obj"] == "<opaque>"
