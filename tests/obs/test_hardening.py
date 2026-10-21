"""Security-hardening tests for matofx.obs.

Covers redaction (built-in + customer-extensible, recursive),
per-line size cap (truncation marker), and process / host metadata
on every record.
"""

from __future__ import annotations

import io
import json
import logging

from matofx.config import LogSettings
from matofx.obs import configure, get_logger
from tests.obs.conftest import read_lines


def _emit(level: str = "INFO", **field_overrides: object) -> tuple[io.StringIO, dict[str, object]]:
    buf = io.StringIO()
    settings = LogSettings(level=level, **field_overrides)  # type: ignore[arg-type]
    configure(settings, stream=buf)
    return buf, {}


# --- Redaction: built-in patterns ----------------------------------------


def test_password_field_redacted() -> None:
    buf, _ = _emit()
    log = get_logger("matofx.test")
    log.info("login", extra={"password": "hunter2"})
    rec = read_lines(buf)[0]
    assert rec["password"] == "***REDACTED***"


def test_api_key_redacted() -> None:
    buf, _ = _emit()
    log = get_logger("matofx.test")
    log.info("op", extra={"api_key": "sk-abc"})
    assert read_lines(buf)[0]["api_key"] == "***REDACTED***"


def test_substring_match_user_password_redacted() -> None:
    buf, _ = _emit()
    log = get_logger("matofx.test")
    log.info("op", extra={"user_password": "hunter2"})
    assert read_lines(buf)[0]["user_password"] == "***REDACTED***"


def test_case_insensitive_match() -> None:
    buf, _ = _emit()
    log = get_logger("matofx.test")
    log.info("op", extra={"PassWord": "hunter2"})
    assert read_lines(buf)[0]["PassWord"] == "***REDACTED***"


def test_session_id_not_redacted_by_default() -> None:
    """``session_id`` is meaningful in audit context and isn't a credential."""
    buf, _ = _emit()
    log = get_logger("matofx.test")
    log.info("op", extra={"session_id": "audit-1"})
    assert read_lines(buf)[0]["session_id"] == "audit-1"


def test_safe_field_unchanged() -> None:
    buf, _ = _emit()
    log = get_logger("matofx.test")
    log.info("op", extra={"username": "alice", "count": 42})
    rec = read_lines(buf)[0]
    assert rec["username"] == "alice"
    assert rec["count"] == 42


def test_authorization_redacted() -> None:
    buf, _ = _emit()
    log = get_logger("matofx.test")
    log.info("req", extra={"authorization": "Bearer xyz"})
    assert read_lines(buf)[0]["authorization"] == "***REDACTED***"


def test_cookie_redacted() -> None:
    buf, _ = _emit()
    log = get_logger("matofx.test")
    log.info("req", extra={"cookie": "session=abc"})
    assert read_lines(buf)[0]["cookie"] == "***REDACTED***"


# --- Redaction: customer-extensible --------------------------------------


def test_customer_redact_field_applied() -> None:
    buf, _ = _emit(redact_fields=["clearance_level"])
    log = get_logger("matofx.test")
    log.info("op", extra={"clearance_level": "TS"})
    assert read_lines(buf)[0]["clearance_level"] == "***REDACTED***"


# --- Redaction: recursive into nested structures ------------------------


def test_redaction_recurses_into_nested_dict() -> None:
    buf, _ = _emit()
    log = get_logger("matofx.test")
    log.info("op", extra={"payload": {"password": "x", "user": "alice"}})
    rec = read_lines(buf)[0]
    payload = rec["payload"]
    assert isinstance(payload, dict)
    assert payload["password"] == "***REDACTED***"
    assert payload["user"] == "alice"


def test_redaction_recurses_into_list_of_dicts() -> None:
    buf, _ = _emit()
    log = get_logger("matofx.test")
    log.info("op", extra={"items": [{"password": "x"}, {"name": "y"}]})
    rec = read_lines(buf)[0]
    items = rec["items"]
    assert isinstance(items, list)
    assert items[0]["password"] == "***REDACTED***"
    assert items[1]["name"] == "y"


def test_redaction_handles_deeply_nested() -> None:
    buf, _ = _emit()
    log = get_logger("matofx.test")
    log.info(
        "op",
        extra={"a": {"b": {"c": {"secret_key": "kaboom"}}}},
    )
    rec = read_lines(buf)[0]
    a = rec["a"]
    assert isinstance(a, dict)
    assert a["b"]["c"]["secret_key"] == "***REDACTED***"


# --- Per-line size cap ---------------------------------------------------


def test_oversize_line_replaced_with_marker() -> None:
    buf, _ = _emit(max_line_bytes=1024)  # tight cap for testing
    log = get_logger("matofx.test")
    huge_payload = "x" * 5_000
    log.info("big", extra={"data": huge_payload})
    rec = read_lines(buf)[0]
    # Marker carries identity + the original size.
    assert "truncated_bytes" in rec
    assert isinstance(rec["truncated_bytes"], int)
    assert rec["truncated_bytes"] > 1024
    # The message survives — it is what says *what* failed, and it is
    # never the field that blew the cap.
    assert rec["message"] == "big"
    # A separate field explains the truncation and names what was dropped.
    assert "dropped" in str(rec["truncated"]).lower()
    assert "data" in str(rec["truncated"])
    # Marker preserves identity fields.
    assert rec["level"] == "info"
    assert rec["logger"] == "matofx.test"


def test_marker_keeps_message_of_an_oversize_exception_record() -> None:
    """The real shape: a short message with a huge traceback attached.

    This is what a runner logs when a job's heartbeat fails. Losing the
    message here left an operator with a run of identical "original was N
    bytes" lines and no way to tell which job or which failure.
    """
    buf, _ = _emit(max_line_bytes=1024)
    log = get_logger("matofx.test")
    try:
        raise RuntimeError("x" * 5_000)
    except RuntimeError:
        log.exception("Heartbeat poll for job 1483 raised; skipping it this cycle")
    rec = read_lines(buf)[0]
    assert rec["message"] == "Heartbeat poll for job 1483 raised; skipping it this cycle"
    assert "exception" in str(rec["truncated"])


def test_marker_clips_a_pathologically_long_message() -> None:
    """A message big enough on its own must not let the marker breach the cap."""
    buf, _ = _emit(max_line_bytes=1024)
    log = get_logger("matofx.test")
    log.info("m" * 20_000)
    text = buf.getvalue()
    assert text.count("\n") == 1
    json.loads(text.strip())  # must still parse
    rec = read_lines(buf)[0]
    assert len(str(rec["message"]).encode("utf-8")) <= 2048 + 4  # budget + ellipsis


def test_marker_clips_on_a_character_boundary() -> None:
    """Clipping must not split a multi-byte character into invalid UTF-8."""
    buf, _ = _emit(max_line_bytes=1024)
    log = get_logger("matofx.test")
    # 3 bytes each, so the 2048-byte budget lands mid-character.
    log.info("あ" * 20_000)
    rec = read_lines(buf)[0]
    assert isinstance(rec["message"], str)  # round-tripped through JSON intact


def test_under_cap_line_unchanged() -> None:
    buf, _ = _emit(max_line_bytes=16384)
    log = get_logger("matofx.test")
    log.info("small", extra={"data": "x" * 10})
    rec = read_lines(buf)[0]
    assert "truncated_bytes" not in rec
    assert rec["message"] == "small"


def test_truncation_marker_is_valid_json_line() -> None:
    """Whatever the input, output is one parseable JSON object per line."""
    buf, _ = _emit(max_line_bytes=1024)
    log = get_logger("matofx.test")
    log.info("big", extra={"data": "y" * 10_000})
    text = buf.getvalue()
    assert text.count("\n") == 1
    json.loads(text.strip())  # must parse


# --- Process / host metadata --------------------------------------------


def test_process_id_and_name_present() -> None:
    buf, _ = _emit()
    log = get_logger("matofx.test")
    log.info("hi")
    rec = read_lines(buf)[0]
    assert "process_id" in rec
    assert isinstance(rec["process_id"], int)
    assert "process_name" in rec
    assert isinstance(rec["process_name"], str)


def test_hostname_present_by_default() -> None:
    buf, _ = _emit()
    log = get_logger("matofx.test")
    log.info("hi")
    rec = read_lines(buf)[0]
    assert "hostname" in rec
    assert rec["hostname"]


def test_hostname_omitted_when_disabled() -> None:
    buf, _ = _emit(include_hostname=False)
    log = get_logger("matofx.test")
    log.info("hi")
    rec = read_lines(buf)[0]
    assert "hostname" not in rec


def test_process_metadata_in_truncation_marker() -> None:
    buf, _ = _emit(max_line_bytes=1024)
    log = get_logger("matofx.test")
    log.info("big", extra={"data": "z" * 10_000})
    rec = read_lines(buf)[0]
    assert "process_id" in rec
    assert "process_name" in rec
    assert "hostname" in rec  # default include_hostname=True


# --- Settings round-trip -------------------------------------------------


def test_log_settings_default_max_line_is_16k() -> None:
    settings = LogSettings()
    assert settings.max_line_bytes == 16_384


def test_log_settings_redact_fields_default_empty() -> None:
    """Customer extensions default empty; built-in deny list always applies."""
    settings = LogSettings()
    assert settings.redact_fields == []


def test_log_settings_max_line_bounds() -> None:
    """Max line is bounded — too small or too large is rejected."""
    import pytest  # noqa: PLC0415
    from pydantic import ValidationError  # noqa: PLC0415

    with pytest.raises(ValidationError):
        LogSettings(max_line_bytes=10)  # below minimum
    with pytest.raises(ValidationError):
        LogSettings(max_line_bytes=1024 * 1024 * 1024)  # 1 GB, far above cap


# Cleanup: re-run conftest's reset is automatic, but we should put back
# the default INFO-level handler so other test modules see the fixture.
def teardown_module(_module: object) -> None:
    """After this module's stand-alone configure() calls, restore default."""
    # Remove any matofx.obs handler.
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler.get_name() == "matofx.obs":
            root.removeHandler(handler)
