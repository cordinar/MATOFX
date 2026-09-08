"""Bounded reading of multipart uploads.

``await UploadFile.read()`` buffers the *entire* request body into a
single ``bytes`` object before the handler gets a chance to check its
size, so a single large upload pins arbitrary memory on the API host —
a cheap denial-of-service for any caller allowed to hit an upload
endpoint. :func:`read_upload_capped` instead reads in bounded chunks
and aborts the moment the running total crosses the cap, so the host
never holds more than ``max_bytes + chunk_size`` for a rejected upload.

It also rejects early on the request's ``Content-Length`` header when a
:class:`~starlette.requests.Request` is supplied: an honest client that
declares an oversized body is refused before a single byte is read.
The header is advisory (a hostile client can lie or omit it), so the
chunked-read ceiling is always enforced regardless.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, UploadFile, status

_DEFAULT_CHUNK_SIZE = 1024 * 1024  # 1 MiB


def _too_large(max_bytes: int, what: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail=(
            f"That {what} is larger than the {max_bytes:,}-byte limit. "
            "Ask an administrator to raise the cap if you need more headroom."
        ),
    )


async def read_upload_capped(
    file: UploadFile,
    *,
    max_bytes: int,
    request: Request | None = None,
    what: str = "file",
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
) -> bytes:
    """Read ``file`` fully, raising 413 as soon as it exceeds ``max_bytes``.

    Args:
        file: The multipart ``UploadFile`` to drain.
        max_bytes: Hard ceiling. The first byte past this aborts the read.
        request: When given, the ``Content-Length`` header is checked up
            front so an honestly-declared oversized body is refused before
            any bytes are buffered. Optional because the chunked ceiling is
            the real guarantee.
        what: Noun used in the error message (e.g. ``"file"``, ``"bundle"``,
            ``"image"``).
        chunk_size: Read granularity; also the worst-case overshoot held in
            memory before a rejection.

    Returns:
        The full upload bytes (guaranteed ``<= max_bytes``).

    Raises:
        HTTPException: ``413`` when the upload exceeds ``max_bytes``.
    """
    if request is not None:
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > max_bytes:
                    raise _too_large(max_bytes, what)
            except ValueError:
                # Malformed header — ignore and fall back to the read ceiling.
                pass

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise _too_large(max_bytes, what)
        chunks.append(chunk)
    return b"".join(chunks)
