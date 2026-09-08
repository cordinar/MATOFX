"""Unit tests for the bounded upload reader (memory-exhaustion guard)."""

from __future__ import annotations

import io
from typing import cast

import pytest
from fastapi import HTTPException, Request, UploadFile

from matofx.api._uploads import read_upload_capped


class _StubUpload:
    """Minimal async-readable stand-in for ``UploadFile``."""

    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)

    async def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)


def _request_with_content_length(value: str) -> Request:
    return Request({"type": "http", "headers": [(b"content-length", value.encode())]})


@pytest.mark.asyncio
async def test_returns_full_bytes_under_cap() -> None:
    upload = cast(UploadFile, _StubUpload(b"x" * 100))
    out = await read_upload_capped(upload, max_bytes=1000, chunk_size=16)
    assert out == b"x" * 100


@pytest.mark.asyncio
async def test_aborts_once_chunked_read_exceeds_cap() -> None:
    # 5 KiB body, 1 KiB cap: the read must stop and raise rather than
    # buffering the whole thing (memory-exhaustion DoS).
    upload = cast(UploadFile, _StubUpload(b"x" * 5000))
    with pytest.raises(HTTPException) as exc_info:
        await read_upload_capped(upload, max_bytes=1000, chunk_size=256)
    assert exc_info.value.status_code == 413


@pytest.mark.asyncio
async def test_rejects_oversized_declared_content_length() -> None:
    # An honest client declaring an oversized body is refused before any
    # bytes are read.
    upload = cast(UploadFile, _StubUpload(b"x" * 10))
    request = _request_with_content_length("999999")
    with pytest.raises(HTTPException) as exc_info:
        await read_upload_capped(upload, max_bytes=1000, request=request, what="bundle")
    assert exc_info.value.status_code == 413


@pytest.mark.asyncio
async def test_malformed_content_length_falls_back_to_read_ceiling() -> None:
    # A garbage header doesn't crash; the chunked ceiling still applies.
    upload = cast(UploadFile, _StubUpload(b"x" * 50))
    request = _request_with_content_length("not-a-number")
    out = await read_upload_capped(upload, max_bytes=1000, request=request)
    assert out == b"x" * 50
