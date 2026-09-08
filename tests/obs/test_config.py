"""Tests for the obs.configure entry point."""

from __future__ import annotations

import io
import logging

from matofx.config import LogSettings
from matofx.obs import configure, get_logger


def _settings(level: str = "INFO", *, ensure_ascii: bool = False) -> LogSettings:
    return LogSettings(level=level, ensure_ascii=ensure_ascii)  # type: ignore[arg-type]


def test_configure_is_idempotent() -> None:
    buf1 = io.StringIO()
    configure(_settings(), stream=buf1)
    buf2 = io.StringIO()
    configure(_settings(), stream=buf2)

    root = logging.getLogger()
    obs_handlers = [h for h in root.handlers if h.get_name() == "matofx.obs"]
    assert len(obs_handlers) == 1


def test_level_string_accepted() -> None:
    buf = io.StringIO()
    configure(_settings("DEBUG"), stream=buf)
    log = get_logger("matofx.test")
    log.debug("quiet but visible")
    assert "quiet but visible" in buf.getvalue()


def test_level_filters_below_threshold() -> None:
    buf = io.StringIO()
    configure(_settings("WARNING"), stream=buf)
    log = get_logger("matofx.test")
    log.info("should be suppressed")
    log.warning("should appear")
    text = buf.getvalue()
    assert "should be suppressed" not in text
    assert "should appear" in text


def test_third_party_loggers_use_same_handler() -> None:
    buf = io.StringIO()
    configure(_settings(), stream=buf)
    logging.getLogger("some.third_party").info("thirdparty hello")
    assert "thirdparty hello" in buf.getvalue()
