"""Base-owned cold-start composition root from a protected exact boot handoff."""
from __future__ import annotations

import argparse
import logging
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final
from uuid import UUID

from appliance.apps.broker import AppEffectBroker, ColdStart, ForeignAppUnit
from appliance.apps.descriptors import DESCRIPTOR_SAMPLE_MS, descriptor_use
from appliance.apps.display_runs import DisplayRuns
from appliance.apps.lifecycle_storage import FileEffectJournal
from appliance.apps.probe import AppRunKey
from appliance.apps.probe_channel import ProbeThread
from appliance.apps.probe_listener import ProbeListener
from appliance.apps.process_linux import SystemdAppProcessDriver
from appliance.feed import Feed, answer_feed_read
from appliance.feed_socket import FEED_READERS, FEEDS_GROUP, FeedListener
from appliance.kernel.app_facts import (
    APP_DESCRIPTORS,
    APP_EXITED,
    APP_OBSERVATION_FAILED,
    APP_STARTED,
    RUN_RESTATE_MS,
)
from appliance.kernel.boot_store import BootStore
from appliance.kernel.clock import boot_id, boottime_ms
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_app_link import PROBE_SOCKET
from contracts.strict_json import loads_object

# The broker's node feed (audience node): probe and run facts for the health judge. The broker
# says nothing to Central (decision 0034, S4c).
FEED_CAPACITY = 512
FEED_SOCKET = Path("/run/photo-wall-app-feed/feed.sock")
MAX_FEED_REPLY = 65536  # a reader's receive buffer (the health judge's MAX_FEED_REPLY)
# After an observation fault (the driver could not tell the app's state), the boot's owner
# observes again after these waits, consecutive faults stepping through them; a clean observation
# starts over. Short: such a fault is a moment between states, and the broker never exits over it.
OBSERVATION_BACKOFF_MS: Final[tuple[int, ...]] = (1_000, 2_000, 5_000, 10_000)
LOG = logging.getLogger(__name__)


def feed_listener(feed: Feed, path: Path = FEED_SOCKET, *, owner_uid: int = 0,
                  group: int = FEEDS_GROUP, **seams) -> FeedListener:
    """The broker's node feed socket: root-owned, group pw-node-feeds, read by root and
    pw-health only (`seams`: the kernel listener's `peer` and `kind`, for tests)."""
    return FeedListener(path, owner_uid=owner_uid, group=group, readers=FEED_READERS,
                        answer=lambda request: answer_feed_read(feed, request),
                        max_reply=MAX_FEED_REPLY, **seams)


class BrokerLoop:
    """One main-loop turn. Every blocking call (systemctl) lives here, never in the probe thread;
    the loop publishes the current app run to the probe thread each turn. Every known turn also
    states the launch's run started or exited, judged from the driver's launch record and the
    turn's observation, and samples its open files (`appliance.kernel.app_facts`), and tells
    Display the current run (`display`, a `DisplayRuns`: sent only when it changed or Display's
    controller is new). The broker speaks to no Central (decision 0034, S4c): its one listener is
    the probe socket (`listener`). Nothing here kills or restarts the app (issue #125)."""

    def __init__(self, *, broker, store, driver, listener, probes: ProbeThread,
                 feeds: FeedListener, display: DisplayRuns, clock: Callable[[], int] = boottime_ms,
                 proc: Path = Path("/proc")):
        self.broker, self.store, self.driver = broker, store, driver
        self.listener, self.probes, self.feeds = listener, probes, feeds
        self.display = display
        self.clock, self.proc = clock, proc
        self.last_run: AppRunKey | None = None  # the launch's run last stated started
        self.run_exited = False  # whether `last_run`'s exit has been stated
        self.stated_at = 0  # when `last_run`'s state was last stated
        self.sampled_at: int | None = None  # when the current run's descriptors were sampled
        self.observation_fault: str | None = None  # the boot owner's last fault, until it clears
        self.faults = 0  # consecutive observation faults
        self.observe_after = 0  # boot ms before which the boot owner does not observe again

    def turn(self) -> None:
        self.probes.check()
        self._reconcile()
        self.listener.serve_one()
        known, current = self._observe()
        if known:
            run = None if current is None else AppRunKey.of(current)
            self._report_run(run, self._launch_run(run))
            self.display.tell(run)
            self.probes.publish_run(run)
        self.feeds.serve()

    def _reconcile(self) -> None:
        """The boot owner's turn. No observed state is fatal: a fault is reported once per reason
        on the node feed (`app_observation_failed`, for the health judge) and in the journal, and
        observed again after OBSERVATION_BACKOFF_MS; a unit that is not the launch's is stopped
        and nothing starts in its place. Only a failed boot store exits."""
        now = self.clock()
        if now < self.observe_after:
            return
        try:
            self.broker.reconcile()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            if self.store.failed:
                raise
            if isinstance(error, ForeignAppUnit):
                self._stop_foreign()
            reason = str(error) if isinstance(error, ValueError) else type(error).__name__
            if reason != self.observation_fault:
                LOG.warning("broker: the app could not be observed (%s); observing again on a backoff",
                            reason)
                self.probes.feed.append(APP_OBSERVATION_FAILED, {"reason": reason})
            self.observation_fault = reason
            self.observe_after = now + OBSERVATION_BACKOFF_MS[min(self.faults, len(OBSERVATION_BACKOFF_MS) - 1)]
            self.faults += 1
        else:
            self.observation_fault, self.faults = None, 0

    def _stop_foreign(self) -> None:
        """A unit under the app's name that is not the launch's: stop it (logged), never adopt it."""
        LOG.warning("broker: stopping an app unit that is not its launch's")
        try:
            self.driver.stop_foreign()
        except (OSError, subprocess.SubprocessError):
            LOG.warning("broker: the stop was not accepted; asking again on a later turn", exc_info=True)

    def _launch_run(self, run: AppRunKey | None) -> AppRunKey | None:
        """The run the current launch started: `run` (the turn's current run) when there is one,
        else the driver's launch record's (`AppRunKey.of_launch`); None with no launch, or when
        the record cannot be read this turn."""
        if run is not None:
            return run
        try:
            launch = self.driver.launched()
        except (OSError, ValueError):
            if self.store.failed:
                raise
            return None
        return None if launch is None else AppRunKey.of_launch(launch)

    def _report_run(self, run: AppRunKey | None, launched: AppRunKey | None) -> None:
        """The launch's run state and the current run's open files every DESCRIPTOR_SAMPLE_MS:
        node-feed facts for the health judge (`appliance.kernel.app_facts`). `launched` has
        started; it has exited once `run` (the turn's current run) is None. Each is stated when it
        first holds, so a run no turn saw current still starts and exits, and restated every
        RUN_RESTATE_MS while it holds."""
        feed = self.probes.feed
        now = self.clock()
        if launched != self.last_run:
            if self.last_run is not None and not self.run_exited:
                feed.append(APP_EXITED, {"run": self.last_run.document()})
            if launched is not None:
                feed.append(APP_STARTED, {"run": launched.document()})
            self.last_run, self.run_exited, self.stated_at, self.sampled_at = launched, False, now, None
        if self.last_run is not None:
            if run is None and not self.run_exited:
                feed.append(APP_EXITED, {"run": self.last_run.document()})
                self.run_exited, self.stated_at = True, now
            elif now - self.stated_at >= RUN_RESTATE_MS:
                feed.append(APP_EXITED if self.run_exited else APP_STARTED,
                            {"run": self.last_run.document()})
                self.stated_at = now
        if run is None:
            return
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

    def _observe(self):
        """(known, current): one `driver.current()` per turn; unknown when it cannot be read."""
        try:
            return True, self.driver.current()
        except (OSError, ValueError):
            if self.store.failed:
                raise
            return False, None


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
    broker = AppEffectBroker(boot_id=kernel_boot_id, offer_id=UUID(value["offer_id"]),
                            authorized_environment=environment,
                            journal=FileEffectJournal(store), driver=driver)
    feed = Feed(FEED_CAPACITY)
    probes = ProbeThread(feed)
    listener = ProbeListener(driver, Path(PROBE_SOCKET), probes=probes)
    feeds = feed_listener(feed)
    loop = BrokerLoop(broker=broker, store=store, driver=driver, listener=listener, probes=probes,
                      feeds=feeds, display=DisplayRuns())
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
        listener.close()
        store.close()


if __name__ == "__main__":
    main()
