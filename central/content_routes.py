"""The Player-facing content routes and the pod probes (a composition-root module; imports fastapi).

Every byte route resolves through the catalog and reads through `AssetReader` (design §6(b)-(c),
§10.2): Unknown is a 404, a miss waits up to 30s on the fetch job's handle, then serves or 503s
with `Retry-After`. The routes are unauthenticated (trusted LAN, 0009): a Pi has no credential
before it boots.

Starlette never cancels an endpoint when its client goes away, so `until_disconnect` watches
`http.disconnect` (the `media_gateway.py` pattern) and cancels the read. A Pi gone at ~10s frees
its waiter slot at once; the fetch job itself is never cancelled.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from collections.abc import Awaitable, Iterator
from contextlib import suppress
from typing import BinaryIO, Final, TypeVar

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from central.assets.reader import Opened, Unavailable
from central.content_catalog.catalog import DevicePackage, ManifestRefusal, sanitize_serial
from central.content_wiring import ContentServices
from central.kernel.ports import Candidates, NetbootBaseRequest, PackageRequest, Unknown
from central.netboot_base import SERIAL_HEADER

LOG = logging.getLogger("central.app")  # the netboot serial log line keeps its logger

CHUNK_BYTES: Final = 1024 * 1024
DEB_MEDIA_TYPE: Final = "application/vnd.debian.binary-package"
_CLIENT_GONE: Final = 499  # nginx's "client closed request"; nobody is left to read it

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


def _stream(opened: Opened, media_type: str) -> StreamingResponse:
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


def _error(code: str, status: int, *, retry_after: int | None = None) -> JSONResponse:
    headers = None if retry_after is None else {"Retry-After": str(retry_after)}
    return JSONResponse({"error": code}, status_code=status, headers=headers)


def _unavailable(prefix: str, served: Unavailable) -> JSONResponse:
    return _error(f"{prefix}_{served.reason}", 503, retry_after=served.retry_after_seconds)


def _package(package: DevicePackage) -> dict[str, object]:
    return {"version": package.version, "sha256": package.sha256, "size": package.size}


def mount_content_routes(app: FastAPI, content: ContentServices) -> None:
    """Bind the netboot, package and manifest routes plus `/livez` and `/readyz`."""
    catalog, reader, probe = content.catalog, content.reader, content.probe

    async def read(request: Request, candidates: Candidates) -> Opened | Unavailable | None:
        """`reader.read` bounded by the client's connection; None when the client left."""
        try:
            return await until_disconnect(request, reader.read(candidates))
        except ClientDisconnected:
            return None

    @app.get("/v1/netboot/base")
    async def netboot_base(request: Request) -> Response:
        # The Pi self-identifies by serial. The catalog sanitizes it before any use, and the log
        # line records only the sanitized value.
        header = request.headers.get(SERIAL_HEADER)
        LOG.info("netboot base fetch: serial=%s",
                 sanitize_serial(header) or "<absent-or-invalid>")
        base = NetbootBaseRequest(header)
        resolution = await catalog.resolve(base)
        if isinstance(resolution, Unknown):
            return _error("base_unknown", 404)  # decision 4, also an empty catalog
        served = await read(request, resolution)
        if served is None:
            return Response(status_code=_CLIENT_GONE)
        if isinstance(served, Unavailable):
            return _unavailable("base", served)  # a miss records nothing about the device
        try:
            await catalog.record_served(base, served.job)  # only after a 200
        except BaseException:
            os.close(served.fd)
            raise
        return _stream(served, "application/octet-stream")

    @app.get("/v1/app/package/{sha256}.deb")
    async def app_package(request: Request, sha256: str) -> Response:
        try:
            package = PackageRequest(sha256)
        except ValueError:
            return _error("app_package_not_found", 404)
        resolution = await catalog.resolve(package)
        if isinstance(resolution, Unknown):
            return _error("app_package_not_found", 404)
        served = await read(request, resolution)
        if served is None:
            return Response(status_code=_CLIENT_GONE)
        if isinstance(served, Unavailable):
            return _unavailable("app", served)
        return _stream(served, DEB_MEDIA_TYPE)

    @app.get("/v1/netboot/manifest")
    async def netboot_manifest(request: Request) -> Response:
        # The `.deb` of the tag whose base this device was actually served this boot (F4), plus
        # that tag, which the enrolled player reports back as base-health `running_tag`.
        package = await catalog.device_package(request.headers.get(SERIAL_HEADER))
        if isinstance(package, ManifestRefusal):
            return _error(package.code, 503)
        return JSONResponse({**_package(package), "tag": package.tag})

    @app.get("/v1/app/manifest")
    async def app_manifest() -> Response:
        package = await catalog.promoted_package()
        if isinstance(package, ManifestRefusal):
            return _error(package.code, 503)  # app_unconfigured keeps the appliance retrying
        return JSONResponse(_package(package))

    @app.get("/livez")
    def livez() -> Response:
        return JSONResponse({"status": "ok"}, status_code=200 if probe.live() else 503)

    @app.get("/readyz")
    def readyz() -> Response:
        # Sync: FastAPI runs it on a worker thread, so a slow DB check never blocks the loop.
        ready = probe.ready()
        return JSONResponse({"status": "ok" if ready else "unavailable"},
                            status_code=200 if ready else 503)

