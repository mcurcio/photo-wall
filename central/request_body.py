"""A request body read under a size cap: the one home of the rule for every route that streams one
(the Node's control routes, a paper profile's upload).

A declared Content-Length past the cap is refused before anything is read; the streamed bytes are
counted before each chunk is kept, so an absent or dishonest Content-Length never lets more than
`limit` bytes in.
"""
from __future__ import annotations

from collections.abc import Callable

from starlette.requests import Request


async def capped_body(request: Request, limit: int, refusal: Callable[[], Exception]) -> bytes:
    """The body's bytes, at most `limit`; raises `refusal()` past it."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise refusal()
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise refusal()
        data.extend(chunk)
    return bytes(data)


__all__ = ["capped_body"]
