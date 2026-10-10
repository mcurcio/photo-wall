"""The pod probes and the streaming helpers the byte routes share (a composition-root module;
imports fastapi).

Starlette never cancels an endpoint when its client goes away, so `until_disconnect` watches
`http.disconnect` (the `media_gateway.py` pattern) and cancels the read. A Pi gone at ~10s frees
its waiter slot at once; the fetch job itself is never cancelled.
"""

from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import Awaitable, Iterator
from contextlib import suppress
from typing import BinaryIO, Final, TypeVar

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from central.assets.reader import Opened
from central.content_wiring import ContentServices

CHUNK_BYTES: Final = 1024 * 1024

T = TypeVar("T")


class ClientDisconnected(asyncio.CancelledError):
    """The client sent `http.disconnect` while `until_disconnect` was waiting."""


async def until_disconnect(request: Request, operation: Awaitable[T]) -> T:
    """Await `operation`, cancelling it when the client disconnects.

    On a disconnect the operation is cancelled and awaited, then `ClientDisconnected` (a
    `CancelledError`) reaches the caller. Cancelling the caller cancels the operation too.
    """
    task = asyncio.ensure_future(operation)

    async def disconnected() -> None:
        while (await request.receive())["type"] != "http.disconnect":
            pass

    watcher = asyncio.ensure_future(disconnected())
    try:
        await asyncio.wait((task, watcher), return_when=asyncio.FIRST_COMPLETED)
    finally:
        watcher.cancel()
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        with suppress(asyncio.CancelledError):
            await watcher
    if task.cancelled():
        raise ClientDisconnected
    return task.result()


def _chunks(source: BinaryIO) -> Iterator[bytes]:
    with source:  # the generator's close closes the fd
        while chunk := source.read(CHUNK_BYTES):
            yield chunk


def stream_opened(opened: Opened, media_type: str) -> StreamingResponse:
    source = os.fdopen(opened.fd, "rb")  # owns the fd from here on, even if never iterated
    return StreamingResponse(
        _chunks(source),
        media_type=media_type,
        headers={
            "Cache-Control": "public, immutable",
            "Content-Length": str(opened.size),
            "Digest": "sha-256=" + base64.b64encode(bytes.fromhex(opened.sha256)).decode(),
        },
        background=BackgroundTask(source.close),  # also after a disconnect mid-stream
    )


def error_response(code: str, status: int, *, retry_after: int | None = None) -> JSONResponse:
    headers = None if retry_after is None else {"Retry-After": str(retry_after)}
    return JSONResponse({"error": code}, status_code=status, headers=headers)


def mount_content_routes(app: FastAPI, content: ContentServices) -> None:
    """Bind the pod probes `/livez` and `/readyz`."""
    probe = content.probe

    @app.get("/livez")
    def livez() -> Response:
        return JSONResponse({"status": "ok"}, status_code=200 if probe.live() else 503)

    @app.get("/readyz")
    def readyz() -> Response:
        # Sync: FastAPI runs it on a worker thread, so a slow DB check never blocks the loop.
        ready = probe.ready()
        return JSONResponse({"status": "ok" if ready else "unavailable"},
                            status_code=200 if ready else 503)

