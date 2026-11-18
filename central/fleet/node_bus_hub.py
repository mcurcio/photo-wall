"""Fleet's keeper of the hub: one loop in the worker matches the hub to Central's enrolled set (E3b
design §7.2 rows "Hub runner and Fleet generator", "NodeLink supervisors", §9.6, §9.7, §11; errata
E-E3D-CUT-6, -7).

A look runs at a wake (`wake`: a Player state commit, which every enrolment and retirement is, or a
release, deployment or Install write, heard on the worker's LISTEN, central/node_bus_wiring.py), when
the hub moved (every HUB_PING_SECONDS Fleet asks the hub, not the database, for its server id and the
leaves it holds: a hub that restarted, stopped answering, or links or drops a leaf), and at most
HUB_LOOK_SECONDS after the last one, which bounds a missed wake. A look reads the enrolled serials, writes the hub's configuration file for
them when its bytes differ (atomically, on the volume the hub starts from), and points both NodeLink
supervisors at them. Whenever the hub has not loaded what Fleet last wrote (the file changed, or the
hub's server id is one Fleet has not reloaded: worker start, the hub came back), Fleet reloads it
twice (X6) and lets WallWriter re-create WALL past every mirror with Central's wall documents. A
leaf the hub still holds for an account no longer enrolled is closed (a reload keeps it). So
enrolment and retirement need no hook of their own at their write sites: the look their commit wakes
sees them. A hub that does not answer is retried at the next look; the file and the links are kept meanwhile. A database error
is Central's own failure and ends the loop.

Each look also runs the worker's per-look work it was handed (`each_look`: a running install moves
on, central/infra/player_versions.py `advance_installs`), in a thread after the hub was kept. A
download finishing sends no wake of its own (its ready write is the asset cache's): the look
HUB_LOOK_SECONDS bounds moves that install on.

Presence (S4): every look whose `HubAdmin.linked()` answered records, in one transaction, which
enrolled Nodes' leaves the hub holds and that the hub was reachable; a look that did not reach the
hub records it unreachable (`central.fleet.node_bus_presence`). Each is written only when it changes
(decision 0034): the console judges how fresh they are from the worker's queue heartbeat.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from central.db import Database, DatabaseTransactionClock, TransactionClock
from central.fleet.node_bus_accounts import FLEET_SYSTEM_USER, HubListeners, hub_configuration
from central.fleet.node_bus_presence import record_hub_reachable_in, record_presence_in
from central.infra.node_links import NodeLinks
from contracts.node_link import account_id
from contracts.time import Clock, SystemClock
from nodeapi.buffers import KeyTable
from nodeapi.hub import HubAdmin, HubUnavailable, WallMarks, WallWriter

log = logging.getLogger(__name__)

HUB_LISTENERS: Final = HubListeners(            # in-container invariants of the hub service (S5, Kubernetes note)
    server_name="photo-wall-hub", client_host="0.0.0.0", client_port=4222,
    websocket_host="0.0.0.0", websocket_port=8080, leaf_host="127.0.0.1", leaf_port=7422,
    monitor_port=8222, max_memory_store_bytes=4 * 1024 * 1024)
HUB_LOOK_SECONDS: Final = 60.0   # the longest between looks with no wake and no move of the hub
HUB_PING_SECONDS: Final = 2.0    # how often Fleet asks the hub (never the database) whether it moved


def enrolled_serials_in(conn) -> frozenset[str]:
    """Serials of devices with retired_at IS NULL and serial IS NOT NULL. A serial `account_id`
    refuses has no hub account: it is left out and logged."""
    return frozenset(_enrolled_accounts_in(conn))


def enrolled_devices_in(conn) -> dict[str, str]:
    """`{serial: device_id}` of the devices `enrolled_serials_in` names: every enrolled Pi."""
    return {serial: device_id for serial, (device_id, _) in _enrolled_accounts_in(conn).items()}


def _enrolled_accounts_in(conn) -> dict[str, tuple[str, str]]:
    """`{serial: (device_id, hub account)}` of the devices `enrolled_serials_in` names."""
    rows = conn.execute("SELECT device_id,serial FROM devices "
                        "WHERE retired_at IS NULL AND serial IS NOT NULL").fetchall()
    accounts = {}
    for row in rows:
        try:
            accounts[row["serial"]] = (row["device_id"], account_id(row["serial"]))
        except ValueError:
            log.warning("hub: serial %r has no hub account; left out", row["serial"])
    return accounts


class FleetHub:
    """Fleet's keeper of the hub (one per worker process)."""

    def __init__(self, db: Database, hub_url: str, config_path: Path, listeners: HubListeners,
                 wall_table: KeyTable, marks: WallMarks, links: Sequence[NodeLinks],
                 clock: Clock | None = None, each_look: Sequence[Callable[[], None]] = (),
                 times: TransactionClock | None = None) -> None:
        """`times` (default DatabaseTransactionClock()) stamps the hub's reachability."""
        self._db = db
        self._times = times or DatabaseTransactionClock()
        self._clock = clock or SystemClock()
        self._hub_url = hub_url
        self._config_path = Path(config_path)
        self._listeners = listeners
        self._wall_table = wall_table
        self._marks = marks
        self._links = tuple(links)
        self._each_look = tuple(each_look)
        self._admin: HubAdmin | None = None
        self._writer: WallWriter | None = None
        self._loaded: str | None = None   # the hub's server id Fleet last reloaded and ensured WALL in
        self._wall_ensured = False
        self._seen: tuple[str, frozenset[str]] | None = None   # the hub as the last look left it
        self._loop: asyncio.AbstractEventLoop | None = None
        self._woken: asyncio.Event | None = None

    @property
    def wall(self) -> WallWriter | None:
        """For E5/E8's wall writes: None until connected and WALL ensured once."""
        return self._writer if self._wall_ensured else None

    def wake(self) -> None:
        """Something a look reads may have changed: look now. Safe from any thread."""
        loop, woken = self._loop, self._woken
        if loop is not None and woken is not None:
            with contextlib.suppress(RuntimeError):   # that loop is closed: nobody waits there any more
                loop.call_soon_threadsafe(woken.set)

    async def run(self, stop: asyncio.Event) -> None:
        """Look at once, then at each wake, each move of the hub and at most HUB_LOOK_SECONDS
        apart, until `stop`; then close both hub clients. A database error propagates."""
        loop = asyncio.get_running_loop()
        self._loop, self._woken = loop, asyncio.Event()
        try:
            while not stop.is_set():
                self._woken.clear()
                await self._look()
                ceiling = loop.time() + HUB_LOOK_SECONDS
                while not stop.is_set() and not self._woken.is_set():
                    left = ceiling - loop.time()
                    if left <= 0:
                        break
                    waking = asyncio.ensure_future(self._woken.wait())
                    stopping = asyncio.ensure_future(stop.wait())
                    try:
                        await asyncio.wait((waking, stopping), timeout=min(HUB_PING_SECONDS, left),
                                           return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        waking.cancel()
                        stopping.cancel()
                    if not stop.is_set() and not self._woken.is_set() and await self._moved():
                        break
        finally:
            self._loop = self._woken = None
            for client in (self._admin, self._writer):
                if client is not None:
                    await client.close()
            self._admin = self._writer = None

    async def _moved(self) -> bool:
        """Whether the hub differs from how the last look left it (another server id, other leaves,
        or it answers again), asked of the hub alone. A hub that stops answering is recorded
        unreachable here, at once (a look would only ask it again)."""
        try:
            if self._admin is None:
                self._admin = await HubAdmin.connect(self._hub_url, FLEET_SYSTEM_USER)
            seen = (await self._admin.identity(), await self._admin.linked())
        except HubUnavailable as error:
            if self._seen is not None:
                log.info("hub: %s", error)
                self._seen = None
                await asyncio.to_thread(self._record_unreachable)
            return False
        return seen != self._seen

    async def _look(self) -> None:
        serials = await asyncio.to_thread(self._enrolled)
        if await asyncio.to_thread(self._write, hub_configuration(sorted(serials), self._listeners)):
            self._loaded = None   # the running hub has not loaded this file: reload at the next answer
        try:
            await self._keep(serials)
        except HubUnavailable as error:
            log.info("hub: %s", error)
            self._seen = None
            await asyncio.to_thread(self._record_unreachable)
        for links in self._links:
            await links.track(serials)
        for work in self._each_look:
            await asyncio.to_thread(work)

    async def _keep(self, serials: frozenset[str]) -> None:
        """Connect what is not connected; reload and ensure WALL unless the running hub is the one
        Fleet last did both in (`_loaded` moves only after both succeeded, so a look that fails
        midway is repeated whole); then record presence from the leaves the hub holds, and close
        any leaf of an account not enrolled."""
        if self._admin is None:
            self._admin = await HubAdmin.connect(self._hub_url, FLEET_SYSTEM_USER)
        if self._writer is None:
            self._writer = await WallWriter.connect(self._hub_url, self._wall_table, self._marks)
        identity = await self._admin.identity()
        if identity != self._loaded:
            await self._admin.reload()
            await self._writer.ensure()
            self._wall_ensured = True
            self._loaded = identity
        linked = await self._admin.linked()
        await asyncio.to_thread(self._record_presence, linked)
        self._seen = (identity, linked)
        stray = linked - {account_id(serial) for serial in serials}
        if stray:   # retired: a reload keeps a removed account's leaf (erratum E-E3D-S2-2)
            await self._admin.unlink(stray)

    def _enrolled(self) -> frozenset[str]:
        with self._db.transaction() as conn:
            return enrolled_serials_in(conn)

    def _record_presence(self, linked: frozenset[str]) -> None:
        """One transaction: whether the hub holds each enrolled device's leaf, and that the hub
        was reachable; each written only when it changed."""
        with self._db.transaction() as conn:
            record_presence_in(conn, {device_id: account in linked
                                      for device_id, account in _enrolled_accounts_in(conn).values()},
                               self._clock.utc())
            record_hub_reachable_in(conn, True, times=self._times)

    def _record_unreachable(self) -> None:
        with self._db.transaction() as conn:
            record_hub_reachable_in(conn, False, times=self._times)

    def _write(self, text: str) -> bool:
        """Write `text` to the configuration file unless it already holds exactly those bytes: a
        temporary file in the same directory, then os.replace, mode 0644. True when it wrote."""
        data = text.encode()
        with contextlib.suppress(FileNotFoundError):
            if self._config_path.read_bytes() == data:
                return False
        handle, temporary = tempfile.mkstemp(dir=self._config_path.parent, prefix=f".{self._config_path.name}.")
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, 0o644)
            os.replace(temporary, self._config_path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise
        return True
