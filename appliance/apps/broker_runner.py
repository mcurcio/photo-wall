"""Base-owned cold-start composition root from a protected exact boot handoff."""
from __future__ import annotations

import argparse
import http.client
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

from appliance.apps.broker import AppEffectBroker, ColdStart
from appliance.apps.descriptors import DESCRIPTOR_SAMPLE_MS, descriptor_use
from appliance.apps.import_worker import RootImportWorker
from appliance.apps.lifecycle_storage import (
    FileEffectJournal,
    FileRelaunchPacing,
    primitive,
    running_from,
)
from appliance.apps.online_runner import OnlineRunner
from appliance.apps.probe import (
    RECOVERY_ACKNOWLEDGED,
    RECOVERY_ARMED,
    AppRunKey,
    recovery_may_be_armed,
)
from appliance.apps.probe_channel import ProbeThread
from appliance.apps.process_linux import SystemdAppProcessDriver
from appliance.central_session.http import NodeHTTP
from appliance.central_session.session import NodeSession
from appliance.feed import Feed, answer_feed_read
from appliance.feed_socket import FEED_READERS, FEEDS_GROUP, FeedListener
from appliance.kernel.app_facts import APP_DESCRIPTORS, APP_EXITED, APP_STARTED
from appliance.kernel.boot_store import BootStore
from appliance.kernel.clock import boot_id, boottime_ms
from appliance.node.app_link import BrokerLinkService, deliver_app_link, owed_relink
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
MAX_FEED_REPLY = 65536  # a reader's receive buffer (the health judge's MAX_FEED_REPLY)

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


def feed_listener(feed: Feed, path: Path = FEED_SOCKET, *, owner_uid: int = 0,
                  group: int = FEEDS_GROUP, **seams) -> FeedListener:
    """The broker's node feed socket: root-owned, group pw-node-feeds, read by root and
    pw-health only (`seams`: the kernel listener's `peer` and `kind`, for tests)."""
    return FeedListener(path, owner_uid=owner_uid, group=group, readers=FEED_READERS,
                        answer=lambda request: answer_feed_read(feed, request),
                        max_reply=MAX_FEED_REPLY, **seams)


class BrokerLoop:
    """One main-loop turn. Every blocking call (systemctl, HTTP) lives here, never in the
    probe thread; the loop publishes the current app run to the probe thread each turn and
    alone carries out a kill the thread found due. Every known turn also reports the run's
    start and exit and samples its open files (`appliance.kernel.app_facts`), Central session
    or not."""

    def __init__(self, *, broker, online, store, session, driver, links, probes: ProbeThread,
                 feeds: FeedListener, clock: Callable[[], int] = boottime_ms,
                 proc: Path = Path("/proc")):
        self.broker, self.online, self.store, self.session = broker, online, store, session
        self.driver, self.links, self.probes, self.feeds = driver, links, probes, feeds
        self.clock, self.proc = clock, proc
        self.last_online_poll = 0.0
        self.withheld: tuple[AppRunKey, str] | None = None  # last kill_withheld (run, reason)
        self.killed: AppRunKey | None = None  # the run this broker killed: never signalled twice
        self.last_run: AppRunKey | None = None  # the run the last known turn saw
        self.sampled_at: int | None = None  # when the current run's descriptors were sampled

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
        known, current, granted = self._observe()
        run = None if current is None else AppRunKey.of(current)
        if known:
            self._report_run(run)
            # After online.broker.service(), which may have acknowledged a recovery this turn.
            armed = self._recovery_may_be_armed()
            self.probes.publish_run(run, recovery_may_be_armed=armed)
            # The owed relink is restated every known turn, grant or not (delivery needs one);
            # the boot store keeps it across a broker restart.
            try:
                self.probes.owe_relink(owed_relink(self.store, run))
            except ValueError:
                if self.store.failed:
                    raise
            # Only on a known turn (an unknown one leaves the latch), against this turn's run.
            self._consume_kill_due(run, current, armed)
        if known and granted:
            # Proofs are accepted locally; Central learns of them here, never inside a proof.
            try:
                deliver_app_link(self.store, self.session, self.probes, current=run)
            except (OSError, ValueError, http.client.HTTPException):
                if self.store.failed:
                    raise
        self.feeds.serve()
        # A switch converges even when enrollment or Central is unavailable.
        if time.monotonic() - self.last_online_poll >= 2:
            self.last_online_poll = time.monotonic()
            try:
                self.online.tick()
            except (OSError, ValueError, http.client.HTTPException):
                if self.store.failed:
                    raise

    def _report_run(self, run: AppRunKey | None) -> None:
        """The run's start and exit, once each, and its open files every DESCRIPTOR_SAMPLE_MS:
        node-feed facts for the health judge (`appliance.kernel.app_facts`)."""
        feed = self.probes.feed
        if run != self.last_run:
            if self.last_run is not None:
                feed.append(APP_EXITED, {"run": self.last_run.document()})
            if run is not None:
                feed.append(APP_STARTED, {"run": run.document()})
            self.last_run, self.sampled_at = run, None
        if run is None:
            return
        now = self.clock()
        if self.sampled_at is not None and now - self.sampled_at < DESCRIPTOR_SAMPLE_MS:
            return
        self.sampled_at = now
        try:
            use = descriptor_use(self.proc, run.pid, run.start_ticks)
        except OSError:
            return
        if use is not None:
            feed.append(APP_DESCRIPTORS, {"run": run.document(), "open": use.open,
                                          "soft_limit": use.soft_limit})

    def _recovery_may_be_armed(self) -> bool:
        try:
            return recovery_may_be_armed(self.online.broker.record, self.store.read(RECOVERY_ARMED),
                                         self.store.read(RECOVERY_ACKNOWLEDGED))
        except ValueError:
            if self.store.failed:
                raise
            return True  # unreadable: a recovery may be armed, so no kill this turn

    def _consume_kill_due(self, run: AppRunKey | None, current, armed: bool) -> None:
        """Kill the unresponsive app iff the due run is this turn's run and no switch recovery
        may be armed (Q1); the driver re-checks the process identity at the signal. No grant
        is needed. A withheld kill is offered again by the thread while still overdue."""
        due = self.probes.take_kill_due()
        if due is None or due.run == self.killed:
            return
        if due.run != run:
            reason = "run_changed"
        elif armed:
            reason = "recovery_armed"
        else:
            try:
                killed = self.driver.kill(current)
            except (OSError, ValueError, subprocess.SubprocessError):
                return  # identity unobservable this turn: nothing sent, the latch comes back
            if killed:
                self.killed, self.withheld = run, None
                self.probes.feed.append("app_killed", {
                    "run": run.document(), "reason": "unresponsive",
                    "unanswered_ms": due.unanswered_ms})
                return
            reason = "run_changed"  # the process is no longer this run's: no signal
        if self.withheld != (due.run, reason):
            self.withheld = (due.run, reason)
            self.probes.feed.append("kill_withheld", {"run": due.run.document(), "reason": reason})

    def _observe(self):
        """(known, current, granted): one `driver.current()` per turn, Central session or not."""
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
        return known, current, asked


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
    measured = {key: value[key] for key in ("base_abi", "graphics_abi", "plugin_abi")}
    driver = SystemdAppProcessDriver(Path("/run/photo-wall-node-storage/app-roots"), store, **measured)
    # One pacing for the app unit, shared by the cold-start and the online owner.
    pacing = FileRelaunchPacing(store)
    broker = AppEffectBroker(boot_id=kernel_boot_id, offer_id=UUID(value["offer_id"]),
                            authorized_environment=environment,
                            journal=FileEffectJournal(store), driver=driver,
                            pacing=pacing, now_ms=boottime_ms)
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
    online = OnlineRunner(store, driver, session, RecoveryClient(),
                          worker=RootImportWorker(store, **measured), pacing=pacing,
                          now_ms=boottime_ms)
    feed = Feed(FEED_CAPACITY)
    probes = ProbeThread(feed)
    links = BrokerLinkService(driver, session, Path("/run/photo-wall-app-proof/app-link.sock"),
                              probes=probes, feed=feed)
    feeds = feed_listener(feed)
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
