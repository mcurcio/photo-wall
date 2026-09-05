"""Source-neutral HTTP delivery from an authorized local descriptor."""

from __future__ import annotations

import anyio
from starlette.responses import JSONResponse, Response

from central.media_store import MediaStore
from central.registry import RegistryError


class MediaGateway:
    """Bound concurrent descriptor/hash/stream work across this server process."""

    def __init__(self, store: MediaStore, max_transfers: int = 8):
        self.store = store
        self.limiter = anyio.CapacityLimiter(max_transfers)

    def response(self, token: str, digest: str):
        return MediaResponse(self.store, token, digest, limiter=self.limiter)


class MediaResponse(Response):
    def __init__(self, store: MediaStore, token: str, digest: str, *, seconds: float = 120,
                 limiter=None):
        super().__init__()
        self.store, self.token, self.digest, self.seconds = store, token, digest, seconds
        self.limiter = limiter

    async def __call__(self, scope, receive, send):
        lease = None
        started = False
        borrowed = False
        try:
            if self.limiter is not None:
                try:
                    self.limiter.acquire_nowait()
                    borrowed = True
                except anyio.WouldBlock:
                    with anyio.fail_after(self.seconds):
                        await JSONResponse({"error": "media_transfer_capacity"}, status_code=503)(
                            scope, receive, send)
                    return
            with anyio.fail_after(self.seconds):
                async with anyio.create_task_group() as group:
                    async def disconnected():
                        while True:
                            if (await receive())["type"] == "http.disconnect":
                                group.cancel_scope.cancel()
                                return

                    group.start_soon(disconnected)
                    try:
                        # run_sync shields the descriptor-producing operation from
                        # abandonment, so finally can always close its result.
                        lease = await anyio.to_thread.run_sync(
                            self.store.open_read, self.token, self.digest)
                        variant = lease.variant
                        headers = {
                            "Content-Length": str(variant.size),
                            "Content-Type": variant.media_type,
                            "ETag": f'"{variant.sha256}"',
                            "Cache-Control": "private, no-store",
                            "Accept-Ranges": "none",
                            "X-Content-Type-Options": "nosniff",
                        }
                        started = True
                        await send({"type": "http.response.start", "status": 200,
                                    "headers": [(k.lower().encode(), v.encode())
                                                for k, v in headers.items()]})
                        remaining = variant.size
                        while remaining:
                            data = await anyio.to_thread.run_sync(lease.read, 65536)
                            remaining -= len(data)
                            await send({"type": "http.response.body", "body": data,
                                        "more_body": remaining > 0})
                    except RegistryError as error:
                        if started:
                            raise RuntimeError("media_transfer_failed") from None
                        await JSONResponse({"error": error.code}, status_code=error.status)(
                            scope, receive, send)
                    except Exception:
                        if started:
                            raise RuntimeError("media_transfer_failed") from None
                        await JSONResponse({"error": "media_unavailable"}, status_code=503)(
                            scope, receive, send)
                    finally:
                        group.cancel_scope.cancel()
        finally:
            if lease is not None:
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(lease.close)
            if borrowed:
                self.limiter.release()
