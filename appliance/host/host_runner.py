"""Independent HostCore composition root; opt-in base service, never imports Player.

The base supplies a root-owned boot configuration (the base tag alone). HostCore speaks to Central
only over the Node's bus (decision 0034, S4c): it puts its metrics and facts on the host line and
serves the bus's `reboot`. It makes no HTTP request.
"""
from __future__ import annotations

import argparse
import logging
import queue
import time
from dataclasses import dataclass
from pathlib import Path

from appliance.host.bus import HostLine, host_session
from appliance.host.host_linux import (
    WATCHDOG_BOOTSTATUS,
    LinuxHostSampler,
    SystemdRebootDriver,
    reset_cause,
)
from appliance.host.light_sensor import IIO_ROOT, LightMeter, start_light_meter
from appliance.kernel.boot_stage import DIRECTORY as BOOT_STAGE_DIRECTORY
from appliance.kernel.boot_stage import read_boot_report
from contracts.node_host_facts import FACT_RULES, HostFactsState, valid_fact
from contracts.node_link import NODE_BUS_URL
from contracts.node_observation import HostMetricsState, valid_metrics
from contracts.node_state import HOST_METRICS_EVERY
from contracts.strict_json import loads_object
from nodeapi.node import NodeSession as BusSession

log = logging.getLogger(__name__)
# Seconds between two samples of the host's metrics.
TICK_SECONDS = 2


class HostRunner:
    """Every tick samples the metrics and puts them on the host line (HostLine puts only what
    changed), and reads the facts once per HOST_METRICS_EVERY seconds on this process's monotonic
    clock (`_line_facts_at`)."""

    # The base this boot runs, as the node records it: the tag of the boot offer whose base the
    # initramfs verified and mounted, carried into host.json by node_bootstrap.py from the boot
    # handoff, and stated in the facts (`base_tag`).
    base_tag: str | None = None
    # The base boot stages' records (boot_stage.py), folded into the facts' `boot`.
    boot_stage_directory: Path = BOOT_STAGE_DIRECTORY
    # The hardware watchdog's bootstatus, read into the facts' `reset_cause` (issue #125).
    watchdog_bootstatus: Path = WATCHDOG_BOOTSTATUS
    # PID1's failed units as last read by this process: a failed read reuses
    # the last successful one; None until one succeeds, so "not read" never reads "none failed".
    _failed_units: tuple | None = None
    _line_facts_at: float | None = None

    def __init__(self, line: HostLine, *, base_tag: str | None = None, monotonic=time.monotonic):
        self.line, self.base_tag, self.monotonic = line, base_tag, monotonic
        self.sampler = LinuxHostSampler()

    def tick(self) -> None:
        """This tick's metrics onto the host line, and the facts once per HOST_METRICS_EVERY."""
        metrics = self.sampler.sample() + self.sampler.throttling() + self.sampler.memory_rows()
        self.line.metrics(HostMetricsState(valid_metrics(metrics)))
        if self._line_facts_at is None or self.monotonic() - self._line_facts_at >= HOST_METRICS_EVERY:
            self._line_facts_at = self.monotonic()
            values, boot = self._fact_values()
            self.line.facts(HostFactsState(**{name: values.get(name) for name in FACT_RULES}, boot=boot))

    def _fact_values(self) -> tuple[dict, object]:
        """The facts read now (the sampler's, the boot's base tag, the reset cause) and the boot report."""
        values = {**self.sampler.facts(),
                  "base_tag": self.base_tag if valid_fact("base_tag", self.base_tag) else None,
                  "reset_cause": reset_cause(self.watchdog_bootstatus)}
        return values, read_boot_report(directory=self.boot_stage_directory, units=self._read_failed_units)

    def _read_failed_units(self) -> tuple | None:
        try:
            self._failed_units = self.sampler.failed_units()
        except (OSError, ValueError):
            pass  # unreadable now: the last successful reading, or None (not read)
        return self._failed_units


class BusReboots:
    """The bus's `reboot` asks, run on HostCore's own thread: the session's thread only queues the
    reason (`ask`), and the host's loop runs each queued ask (`run_asked`) with the reboot effect
    (SystemdRebootDriver). Once a reboot started, a later ask runs nothing."""

    def __init__(self, driver=None) -> None:
        self._asked: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._driver = driver if driver is not None else SystemdRebootDriver()
        self._started = False

    def ask(self, reason: str) -> None:
        """From any thread: reboot for `reason` at the host loop's next turn."""
        self._asked.put(reason)

    def run_asked(self) -> None:
        """On HostCore's thread: run every queued ask, at most one reboot started."""
        while True:
            try:
                reason = self._asked.get_nowait()
            except queue.Empty:
                return
            if self._started:
                continue
            log.info("reboot asked over the bus: %s", reason)
            try:
                self._started = self._driver.initiate() is True
            except Exception:   # an unknown effect: never blindly retried (RebootDriver)
                log.exception("reboot asked over the bus: not started")


@dataclass(frozen=True)
class HostBus:
    """HostCore on the Node's bus: its host-line session and the wired light meter feeding it."""
    session: BusSession
    meter: LightMeter

    def stop(self) -> None:
        self.meter.stop()
        self.session.stop()


def start_host_bus(base_tag: str, *, reboot, url: str = NODE_BUS_URL, iio_root: Path = IIO_ROOT) -> HostBus:
    """The host component on the Node's bus (appliance/host/bus.py), on its own thread and never waited
    on, serving `reboot` through `reboot(reason)`, and a wired light meter, if any, on its own thread
    onto the host line's state; both started (it returns at once)."""
    session = host_session(base_tag, url=url, reboot=reboot)
    session.start()
    return HostBus(session, start_light_meter(session.state.put, root=iio_root))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/run/photo-wall-node/host.json"))
    args = parser.parse_args()
    info = args.config.lstat()
    if args.config.is_symlink() or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("host_configuration_ownership")
    value = loads_object(args.config.read_bytes(), max_bytes=8192)
    if value is None or set(value) != {"base_tag"}:
        raise ValueError("host_configuration_invalid")
    reboots = BusReboots()
    bus = start_host_bus(value["base_tag"], reboot=reboots.ask)
    runner = HostRunner(HostLine(bus.session.state.put), base_tag=value["base_tag"])
    last_tick = 0.0
    try:
        while True:
            reboots.run_asked()
            if time.monotonic() - last_tick >= TICK_SECONDS:
                last_tick = time.monotonic()
                try:
                    runner.tick()
                except (OSError, ValueError):
                    log.warning("host: a tick failed; sampling again at the next", exc_info=True)
            time.sleep(0.05)
    finally:
        bus.stop()


if __name__ == "__main__":
    main()
