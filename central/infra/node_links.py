"""One pipe's NodeLink supervisor: every tracked Node's link, recorded in PostgreSQL (E3b design §7.2
"NodeLink supervisors", §9.1, §11).

The fleet pipe and the show pipe each run one `NodeLinks` in the worker, on its own hub connection per
Node (`nodeapi.hub.run_link`), so the pipes stay apart: a link reads, cursors and writes only its own
pipe's streams. Each link is a long-lived task, never a job and never a process per Node; it
reconnects by itself. A Node that leaves the tracked set has its link stopped and its records kept. A
store error is Central's own failure: it ends the supervisor, and the worker with it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Collection

from central.infra.node_link_store import PgLinkStores
from contracts.node_link import Pipe
from nodeapi.hub import DocumentSource, run_link

log = logging.getLogger(__name__)


class NodeLinks:
    """Every tracked Node's link on one pipe, each a long-lived task (never a job, never a process per
    Node): a serial that joins gets a link, one that leaves has its link stopped; records are kept."""

    def __init__(self, pipe: Pipe, hub_url: str, stores: PgLinkStores, documents: DocumentSource) -> None:
        self._pipe = Pipe(pipe)
        self._hub_url = hub_url
        self._stores = stores
        self._documents = documents
        self._links: dict[str, tuple[asyncio.Event, asyncio.Task[None]]] = {}
        self._failed = asyncio.Event()
        self._error: BaseException | None = None

    async def track(self, serials: Collection[str]) -> None:
        """Start a link for each serial not linked; stop and await each linked serial not listed."""
        wanted = set(serials)
        for serial in sorted(wanted - self._links.keys()):
            stop = asyncio.Event()
            task = asyncio.create_task(
                run_link(self._hub_url, serial, self._pipe, self._stores.store(serial, self._pipe),
                         self._documents, stop), name=f"node-link {self._pipe} {serial}")
            task.add_done_callback(self._ended)
            self._links[serial] = (stop, task)
        await self._stop([serial for serial in self._links if serial not in wanted])

    async def run(self, stop: asyncio.Event) -> None:
        """Until `stop` or a link fails, then stop every link; raises the first exception a link
        raised (a store error)."""
        stopping = asyncio.create_task(stop.wait())
        failing = asyncio.create_task(self._failed.wait())
        try:
            await asyncio.wait((stopping, failing), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in (stopping, failing):
                waiter.cancel()
            await self._stop(list(self._links))
        if self._error is not None:
            raise self._error

    async def _stop(self, serials: list[str]) -> None:
        links = [self._links.pop(serial) for serial in serials]
        for stop, _ in links:
            stop.set()
        for _, task in links:
            with contextlib.suppress(Exception):   # a link's own error is `run`'s to raise
                await task

    def _ended(self, task: asyncio.Task[None]) -> None:
        if task.cancelled() or task.exception() is None:
            return
        log.error("%s ended: %r", task.get_name(), task.exception())
        if self._error is None:
            self._error = task.exception()
        self._failed.set()
