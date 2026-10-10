"""Fleet's keeper of the hub: one loop in the worker matches the hub to Central's enrolled set (E3b
design §7.2 rows "Hub runner and Fleet generator", "NodeLink supervisors", §9.6, §9.7, §11; errata
E-E3D-CUT-6, -7).

Every look (HUB_LOOK_SECONDS) reads the enrolled serials, writes the hub's configuration file for
them when its bytes differ (atomically, on the volume the hub starts from), and points both NodeLink
supervisors at them. Whenever the hub has not loaded what Fleet last wrote (the file changed, or the
hub's server id is one Fleet has not reloaded: worker start, the hub came back), Fleet reloads it
twice (X6) and lets WallWriter re-create WALL past every mirror with Central's wall documents. A
leaf the hub still holds for an account no longer enrolled is closed (a reload keeps it). So
enrolment and retirement need no hook at their write sites: the next look sees them. A hub that does
not answer is retried at the next look; the file and the links are kept meanwhile. A database error
is Central's own failure and ends the loop.

Presence (S4): every look whose `HubAdmin.linked()` answered records, in one transaction, which
enrolled Nodes' leaves the hub holds and when it looked (`central.fleet.node_bus_presence`); a look
that did not reach the hub writes nothing, so the look's age tells the console how stale it is.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from central.db import Database
from central.fleet.node_bus_accounts import FLEET_SYSTEM_USER, HubListeners, hub_configuration
from central.fleet.node_bus_presence import record_look_in
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
HUB_LOOK_SECONDS: Final = 2.0
PRESENCE_STALE_SECONDS: Final = 30.0   # a look older than this at a read: presence reads unknown (facts.js)


def enrolled_serials_in(conn) -> frozenset[str]:
    """Serials of devices with retired_at IS NULL whose fleet_device_lifecycle.revoked_at IS NULL and
    serial IS NOT NULL. A serial `account_id` refuses has
    no hub account: it is left out and logged."""
    return frozenset(_enrolled_accounts_in(conn))


def _enrolled_accounts_in(conn) -> dict[str, tuple[str, str]]:
    """`{serial: (device_id, hub account)}` of the devices `enrolled_serials_in` names."""
    rows = conn.execute("SELECT device.device_id,device.serial FROM devices AS device "
                        "JOIN fleet_device_lifecycle AS lifecycle ON lifecycle.device_id=device.device_id "
                        "WHERE device.retired_at IS NULL AND lifecycle.revoked_at IS NULL "
                        "AND device.serial IS NOT NULL").fetchall()
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
                 clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or SystemClock()
        self._hub_url = hub_url
        self._config_path = Path(config_path)
        self._listeners = listeners
        self._wall_table = wall_table
        self._marks = marks
        self._links = tuple(links)
        self._admin: HubAdmin | None = None
        self._writer: WallWriter | None = None
        self._loaded: str | None = None   # the hub's server id Fleet last reloaded and ensured WALL in
        self._wall_ensured = False

    @property
    def wall(self) -> WallWriter | None:
        """For E5/E8's wall writes: None until connected and WALL ensured once."""
        return self._writer if self._wall_ensured else None

    async def run(self, stop: asyncio.Event) -> None:
        """Look every HUB_LOOK_SECONDS until `stop`, then close both hub clients. A database error
        propagates."""
        try:
            while not stop.is_set():
                await self._look()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), HUB_LOOK_SECONDS)
        finally:
            for client in (self._admin, self._writer):
                if client is not None:
                    await client.close()
            self._admin = self._writer = None

    async def _look(self) -> None:
        serials = await asyncio.to_thread(self._enrolled)
        if await asyncio.to_thread(self._write, hub_configuration(sorted(serials), self._listeners)):
            self._loaded = None   # the running hub has not loaded this file: reload at the next answer
        try:
            await self._keep(serials)
        except HubUnavailable as error:
            log.info("hub: %s", error)
        for links in self._links:
            await links.track(serials)

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
        stray = linked - {account_id(serial) for serial in serials}
        if stray:   # retired: a reload keeps a removed account's leaf (erratum E-E3D-S2-2)
            await self._admin.unlink(stray)

    def _enrolled(self) -> frozenset[str]:
        with self._db.transaction() as conn:
            return enrolled_serials_in(conn)

    def _record_presence(self, linked: frozenset[str]) -> None:
        """One transaction: whether the hub holds each enrolled device's leaf, and the look's time."""
        with self._db.transaction() as conn:
            record_look_in(conn, {device_id: account in linked
                                  for device_id, account in _enrolled_accounts_in(conn).values()},
                           self._clock.utc())

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
