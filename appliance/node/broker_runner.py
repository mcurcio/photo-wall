"""Base-owned cold-start composition root from a protected exact boot handoff."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import stat
import struct
import time
from pathlib import Path
from uuid import UUID, uuid4

from appliance.boot_store import BootStore
from appliance.central_session.http import NodeHTTP
from appliance.central_session.session import NodeSession
from appliance.clock import boot_id, boottime_ms
from appliance.feed import Feed, answer_feed_read
from appliance.node.app_link import BrokerLinkService
from appliance.node.broker import AppEffectBroker, ColdStart
from appliance.node.lifecycle_storage import FileEffectJournal, primitive, running_from
from appliance.node.online_runner import OnlineRunner
from appliance.node.probe import AppRunKey
from appliance.node.probe_channel import ProbeThread
from appliance.node.process_linux import SystemdAppProcessDriver
from appliance.node.recovery_linux import RecoveryClient
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_protocol import (
    AppProcessFact,
    NodeEventV2,
    encode_node_message,
    parse_node_message,
)
from contracts.strict_json import loads_object

# The broker's node feed (audience node): probe facts for the health judge. No Central-bound
# code reads it; Central receives process evidence and app-effect events only.
FEED_CAPACITY = 512
FEED_SOCKET = Path("/run/photo-wall-app-feed/feed.sock")
FEED_READERS = frozenset({0, 10006})  # root, pw-health
FEEDS_GROUP = 10007  # pw-node-feeds
MAX_FEED_REQUEST = 4096
FEED_READ_WAIT_SECONDS = 0.05
FEED_READS_PER_TURN = 8

def emit_process_evidence(store, session, running, state: str) -> None:
    document = store.read("process-evidence") or {"sequence": 0, "last": None, "pending": []}
    identity = [str(running.process.invocation_id), running.app_epoch, state]
    if document["last"] != identity:
        if len(document["pending"]) >= 128:
            raise ValueError("process_evidence_capacity")
        sequence = document["sequence"] + 1
        event = NodeEventV2(session.grant.producer, uuid4(), sequence, boottime_ms(),
                            (AppProcessFact(running.process, running.app_epoch,
                                            running.environment.environment_sha256, state),))
        document = {"sequence": sequence, "last": identity,
                    "pending": [*document["pending"], encode_node_message(event).decode()]}
        store.write("process-evidence", document)
    for _ in range(min(4, len(document["pending"]))):
        event = parse_node_message(document["pending"][0].encode())
        code, _ = session.request("POST", "/v2/node/evidence", encode_node_message(event))
        if code != 200:
            break
        document = {**document, "pending": document["pending"][1:]}
        store.write("process-evidence", document)


def peer_uid(connection: socket.socket) -> int:
    size = struct.calcsize("=iII")
    _pid, uid, _gid = struct.unpack("=iII", connection.getsockopt(
        socket.SOL_SOCKET, getattr(socket, "SO_PEERCRED", 17), size))
    return uid


class FeedListener:
    """Serves the feed's `events` op: one request per accept, readers by SO_PEERCRED uid.

    Never blocks the turn beyond a short bounded wait per accepted reader.
    """

    def __init__(self, feed: Feed, path: Path, *, owner_uid: int = 0, group: int = FEEDS_GROUP,
                 readers: frozenset[int] = FEED_READERS, peer=peer_uid,
                 kind: int = getattr(socket, "SOCK_SEQPACKET", socket.SOCK_STREAM)):
        self.feed, self.path, self.readers, self.peer = feed, path, readers, peer
        self.owner_uid = owner_uid
        directory = path.parent.lstat()
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != owner_uid
                or directory.st_mode & 0o022):
            raise ValueError("feed_directory")
        self._remove_stale()
        self.listener = socket.socket(socket.AF_UNIX, kind)
        try:
            self.listener.bind(str(path))
            os.chown(path, owner_uid, group)
            os.chmod(path, 0o660)
            info = path.lstat()
            self.identity = info.st_dev, info.st_ino
            self.listener.listen(8)
            self.listener.setblocking(False)
        except BaseException:
            self.listener.close()
            raise

    def _remove_stale(self) -> None:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.owner_uid:
            raise ValueError("feed_socket_ownership")
        self.path.unlink()

    def serve(self, budget: int = FEED_READS_PER_TURN) -> None:
        for _ in range(budget):
            try:
                connection, _ = self.listener.accept()
            except (BlockingIOError, InterruptedError):
                return
            with connection:
                try:
                    if self.peer(connection) not in self.readers:
                        continue  # not a reader: closed without a reply
                    connection.settimeout(FEED_READ_WAIT_SECONDS)
                    raw = connection.recv(MAX_FEED_REQUEST + 1)
                except OSError:
                    continue
                try:
                    value = loads_object(raw, max_bytes=MAX_FEED_REQUEST)
                    if value is None:
                        raise ValueError("feed_read_request")
                    reply = {"accepted": True, **answer_feed_read(self.feed, value)}
                except ValueError as error:
                    reply = {"accepted": False, "reason": str(error)}
                try:
                    connection.sendall(json.dumps(reply, separators=(",", ":")).encode())
                except OSError:
                    pass

    def close(self) -> None:
        self.listener.close()
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if (info.st_dev, info.st_ino) == self.identity:
            self.path.unlink()


class BrokerLoop:
    """One main-loop turn. Every blocking call (systemctl, HTTP) lives here, never in the
    probe thread; the loop publishes the current app run to the probe thread each turn."""

    def __init__(self, *, broker, online, store, session, driver, links, probes: ProbeThread,
                 feeds: FeedListener):
        self.broker, self.online, self.store, self.session = broker, online, store, session
        self.driver, self.links, self.probes, self.feeds = driver, links, probes, feeds
        self.last_online_poll = 0.0

    def turn(self) -> None:
        self.probes.check()
        if self.online.broker.record is None:
            self.broker.reconcile()
        try:
            self.online.broker.service()
        except (OSError, ValueError, http.client.HTTPException):
            if self.store.failed:
                raise
        # Local proofs and stop progress must run even without a Central session.
        self.links.serve_one()
        known, current = self._observe()
        if known:
            # B8 supplies the recovery predicate; until then nothing consumes a kill.
            self.probes.publish_run(None if current is None else AppRunKey.of(current),
                                    recovery_may_be_armed=False)
        self.feeds.serve()
        # A switch converges even when enrollment or Central is unavailable.
        if time.monotonic() - self.last_online_poll >= 2:
            self.last_online_poll = time.monotonic()
            try:
                self.online.tick()
            except (OSError, ValueError, http.client.HTTPException):
                if self.store.failed:
                    raise

    def _observe(self):
        """(known, current): one `driver.current()` per turn, Central session or not."""
        store, session, driver, links = self.store, self.session, self.driver, self.links
        asked = known = False
        current = None
        try:
            links.remember_grant()
            grant = session.ensure()
            links.remember_grant()
            if grant is not None:
                asked = True
                current = driver.current()
                known = True
                prior = store.read("observed-app")
                previous = running_from(prior["running"]) if prior and prior.get("running") else None
                if previous is not None and (current is None or current.process != previous.process):
                    emit_process_evidence(store, session, previous, "exited")
                if current is not None:
                    emit_process_evidence(store, session, current, "running")
                store.write("observed-app", {"running": primitive(current) if current else None})
        except (OSError, ValueError, http.client.HTTPException):
            if store.failed:
                raise
        if not asked:
            try:
                current, known = driver.current(), True
            except (OSError, ValueError):
                if store.failed:
                    raise
        return known, current


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/run/photo-wall-node/cold.json"))
    args = parser.parse_args()
    info = args.config.lstat()
    if args.config.is_symlink() or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("cold_configuration_ownership")
    value = loads_object(args.config.read_bytes(), max_bytes=16384)
    if value is None or set(value) != {"boot_id", "offer_id", "operation_id", "environment", "base_abi", "graphics_abi", "plugin_abi"}:
        raise ValueError("cold_configuration_invalid")
    kernel_boot_id = boot_id()
    if UUID(value["boot_id"]) != kernel_boot_id:
        raise ValueError("cold_boot_mismatch")
    environment = AppEnvironmentRefV2(**value["environment"])
    store = BootStore(Path("/run/photo-wall-app-broker"), boot_id=kernel_boot_id,
                      policy={"offer_id": value["offer_id"], "environment": value["environment"]})
    driver = SystemdAppProcessDriver(Path("/run/photo-wall-node-storage/app-roots"), store,
                                     **{key: value[key] for key in ("base_abi", "graphics_abi", "plugin_abi")})
    broker = AppEffectBroker(boot_id=kernel_boot_id, offer_id=UUID(value["offer_id"]),
                            authorized_environment=environment,
                            journal=FileEffectJournal(store), driver=driver)
    # Independently scoped broker credential; no HostCore credential or reboot port.
    configuration = Path("/run/photo-wall-node/broker.json")
    status = configuration.lstat()
    if configuration.is_symlink() or status.st_uid != 0 or status.st_mode & 0o077:
        raise ValueError("broker_session_configuration_ownership")
    endpoint = loads_object(configuration.read_bytes(), max_bytes=8192)
    if endpoint is None or set(endpoint) != {"central", "serial"}:
        raise ValueError("broker_session_configuration")
    session = NodeSession(store, NodeHTTP(endpoint["central"], timeout=0.5), owner="app_effect_broker",
                          serial=endpoint["serial"], offer_id=UUID(value["offer_id"]),
                          kernel_boot_id=kernel_boot_id)
    online = OnlineRunner(store, driver, session, RecoveryClient())
    feed = Feed(FEED_CAPACITY)
    probes = ProbeThread(feed)
    links = BrokerLinkService(driver, session, Path("/run/photo-wall-app-proof/app-link.sock"),
                              probes=probes)
    feeds = FeedListener(feed, FEED_SOCKET)
    loop = BrokerLoop(broker=broker, online=online, store=store, session=session, driver=driver,
                      links=links, probes=probes, feeds=feeds)
    try:
        probes.start()
        if broker.journal.current() is None:
            broker.cold_start(ColdStart(UUID(value["operation_id"]), kernel_boot_id,
                                        UUID(value["offer_id"]), environment))
        while True:
            loop.turn()
            time.sleep(0.1)
    finally:
        probes.close()
        feeds.close()
        driver.stops.close()
        links.close()
        store.close()


if __name__ == "__main__":
    main()
