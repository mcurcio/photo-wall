"""Independent standard-library Linux host observation and reboot adapters."""
from __future__ import annotations

import ipaddress
import os
import socket
import struct
import subprocess
import time
from pathlib import Path

from appliance.node.base_status import STATUS, read_supervisor_status
from appliance.node.clock import boot_id, boottime_ms  # noqa: F401
from contracts.node_host_facts import valid_fact

# The firmware's throttle flags (console DDD §63): bits 0-3 hold now, bits 16-19 are sticky,
# set since boot or since another firmware reader last cleared them, so "occurred" not "since boot".
_THROTTLE_FLAGS = (("under_voltage", 0), ("frequency_capped", 1), ("throttled", 2),
                   ("soft_temperature_limit", 3))


class LinuxHostSampler:
    """`sample` and `throttling` read only /proc and /sys (no subprocess or netlink; the unit
    refuses netlink); a value that cannot be read is omitted, never sent as zero; each name
    is emitted once. `supervision` alone runs systemctl."""

    def __init__(self, proc: Path = Path("/proc"), filesystem: Path = Path("/run"),
                 sys: Path = Path("/sys")):
        self.proc, self.filesystem, self.sys = proc, filesystem, sys
        self._cpu: tuple[int, int] | None = None  # (idle, total) jiffies at the previous sample

    def _soc_temperature(self) -> tuple:
        """Thermal zone 0 in celsius, one decimal; no row when unreadable, never zero."""
        try:
            millidegrees = int((self.sys / "class/thermal/thermal_zone0/temp").read_text().strip())
        except (OSError, ValueError):
            return ()
        return (("soc_temperature", round(millidegrees / 1000, 1), "celsius"),)

    def _cpu_busy(self) -> tuple:
        """100 x (1 - delta idle / delta total) of /proc/stat's aggregate line since this
        process's previous sample, idle including iowait; none on the first sample."""
        try:
            fields = (self.proc / "stat").read_text().splitlines()[0].split()
            if fields[0] != "cpu":
                raise ValueError("cpu_line")
            # user nice system idle iowait irq softirq steal; guest time is already in user/nice.
            jiffies = [int(value) for value in fields[1:9]]
        except (OSError, ValueError, IndexError):
            self._cpu = None
            return ()
        current, previous = (jiffies[3] + jiffies[4], sum(jiffies)), self._cpu
        self._cpu = current
        if previous is None or current[1] <= previous[1]:
            return ()
        busy = 100 * (1 - (current[0] - previous[0]) / (current[1] - previous[1]))
        return (("cpu_busy", round(min(100.0, max(0.0, busy)), 1), "percent"),)

    def default_route_interface(self) -> str | None:
        """The interface carrying the lowest-metric up default route in /proc/net/route."""
        best = None
        try:
            rows = (self.proc / "net/route").read_text().splitlines()[1:]
        except OSError:
            return None
        for row in rows:
            fields = row.split()
            try:
                if (len(fields) < 8 or fields[1] != "00000000" or fields[7] != "00000000"
                        or not int(fields[3], 16) & 1):  # RTF_UP
                    continue
                metric = int(fields[6])
            except ValueError:
                continue
            if fields[0] not in (".", "..") and "/" not in fields[0] and (best is None or metric < best[0]):
                best = (metric, fields[0])
        return None if best is None else best[1]

    def _routes(self, interface: str) -> list:
        """`interface`'s own non-default routes in /proc/net/route as IPv4 networks. The kernel
        prints destination and mask in host byte order, so they unpack natively."""
        networks = []
        try:
            rows = (self.proc / "net/route").read_text().splitlines()[1:]
        except OSError:
            return networks
        for row in rows:
            fields = row.split()
            try:
                if len(fields) < 8 or fields[0] != interface or int(fields[7], 16) == 0:
                    continue
                destination, mask = (socket.inet_ntoa(struct.pack("=I", int(fields[index], 16)))
                                     for index in (1, 7))
                networks.append(ipaddress.IPv4Network(f"{destination}/{mask}", strict=False))
            except (ValueError, struct.error):
                continue
        return networks

    def _local_addresses(self) -> set:
        """Every `/32 host LOCAL` leaf of /proc/net/fib_trie (its Main and Local tables repeat
        them, so a set)."""
        found, leaf = set(), None
        try:
            lines = (self.proc / "net/fib_trie").read_text().splitlines()
        except OSError:
            return found
        for line in lines:
            text = line.strip()
            if text.startswith("|-- "):
                leaf = text[4:].strip()
            elif text.split() == ["/32", "host", "LOCAL"] and leaf is not None:
                try:
                    found.add(ipaddress.IPv4Address(leaf))
                except ValueError:
                    continue
        return found

    def _address(self, interface: str) -> str | None:
        """The interface's one local address inside one of its own routes, or None for zero
        or several."""
        networks = self._routes(interface)
        candidates = [address for address in self._local_addresses()
                      if any(address in network for network in networks)]
        return str(candidates[0]) if len(candidates) == 1 else None

    def facts(self) -> dict:
        """The host facts record's values (console DDD §63, G13): the kernel release, the
        default-route interface, its operstate and its address. None for each field that
        cannot be read or is not well formed by the contract's rule."""
        def read(path: Path) -> str | None:
            try:
                return path.read_text().strip()
            except (OSError, UnicodeDecodeError):
                return None
        interface = self.default_route_interface()
        values = {"kernel_release": read(self.proc / "sys/kernel/osrelease"), "interface": interface,
                  "link_state": None, "address": None}
        if valid_fact("interface", interface) and interface is not None:
            values["link_state"] = read(self.sys / "class/net" / interface / "operstate")
            values["address"] = self._address(interface)
        return {name: value if valid_fact(name, value) else None for name, value in values.items()}

    def _link_speed(self) -> tuple:
        """The default-route interface's `speed`; none when unreadable or negative (no link)."""
        interface = self.default_route_interface()
        if interface is None:
            return ()
        try:
            speed = int((self.sys / "class/net" / interface / "speed").read_text().strip())
        except (OSError, ValueError):
            return ()
        return () if speed < 0 else (("link_speed", speed, "megabits_per_second"),)

    def throttling(self) -> tuple:
        """The eight firmware flags from the Raspberry Pi firmware driver's `get_throttled`
        attribute (hex), source `firmware`; () when it is not readable."""
        for path in sorted((self.sys / "devices/platform").glob("*/*:firmware/get_throttled")):
            try:
                flags = int(path.read_text().strip(), 16)
            except (OSError, ValueError):
                return ()
            if flags < 0:
                return ()
            return tuple((f"{name}_{when}", (flags >> (bit + shift)) & 1, "boolean", "firmware")
                         for when, shift in (("now", 0), ("occurred", 16))
                         for name, bit in _THROTTLE_FLAGS)
        return ()

    def sample(self) -> tuple:
        memory = {}
        for line in (self.proc / "meminfo").read_text().splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[2] == "kB":
                memory[fields[0].rstrip(":")] = int(fields[1]) * 1024
        load = float((self.proc / "loadavg").read_text().split()[0])
        disk = os.statvfs(self.filesystem)
        metrics = (("uptime", boottime_ms() / 1000, "seconds"),
                ("load_1m", load, "tasks"),
                ("memory_total", memory["MemTotal"], "bytes"),
                ("memory_available", memory["MemAvailable"], "bytes"),
                ("runtime_available", disk.f_bavail * disk.f_frsize, "bytes"))
        return metrics + self._soc_temperature() + self._cpu_busy() + self._link_speed()

    def supervision(self) -> tuple:
        rows = []
        units = {"manager_supervisor": "photo-wall-manager-supervisor.service",
                 "manager": "photo-wall-node-manager.service", "broker": "photo-wall-app-broker.service",
                 "display": "photo-wall-display.service", "display_controller": "photo-wall-display-controller.service"}
        for name, unit in units.items():
            state = None
            try:
                result = subprocess.run(["/usr/bin/systemctl", "show", unit,
                    "--property=LoadState,ActiveState"], capture_output=True, text=True,
                    check=True, timeout=0.25, env={"PATH": "/usr/bin", "LANG": "C"})
                if len(result.stdout) > 1024:
                    raise ValueError("unit_observation_bound")
                value = dict(line.split("=", 1) for line in result.stdout.splitlines())
                if (set(value) != {"LoadState", "ActiveState"}
                        or value["ActiveState"] not in {"active", "inactive", "failed", "activating", "deactivating", "reloading", "maintenance", "refreshing"}):
                    raise ValueError("unit_observation_invalid")
                state = value["ActiveState"] if value["LoadState"] == "loaded" else "absent"
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            rows.extend(((name + "_known", int(state is not None), "boolean", "pid1"),
                         (name + "_active", int(state == "active"), "boolean", "pid1"),
                         (name + "_failed", int(state == "failed"), "boolean", "pid1")))
        try:
            value = read_supervisor_status(STATUS, kernel_boot_id=boot_id(), now_ms=boottime_ms())
        except (OSError, ValueError):
            rows.append(("manager_summary_known", 0, "boolean", "base_supervisor"))
        else:
            rows.extend((("manager_summary_known", 1, "boolean", "base_supervisor"),
                         ("manager_running", int(value["running"]), "boolean", "base_supervisor"),
                         ("manager_attempts", value["attempts"], "count", "base_supervisor"),
                         ("manager_recovery_required", int(value["fault"] == "manager_recovery_required"), "boolean", "base_supervisor"),
                         ("manager_start_unknown", int(value["fault"] == "manager_start_unknown"), "boolean", "base_supervisor"),
                         ("manager_summary_age", boottime_ms() - value["sampled_boottime_ms"], "milliseconds", "base_supervisor")))
        return tuple(rows)



class SystemdRebootDriver:
    """Request PID1 reboot, then observe stopping state; never completed-boot evidence."""

    def initiate(self) -> bool:
        result = subprocess.run(["/usr/bin/systemctl", "--no-block", "reboot"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=5, check=False,
                                env={"PATH": "/usr/sbin:/usr/bin", "LANG": "C"})
        if result.returncode != 0:
            return False
        # Job submission alone is admission. Only the separate PID1 state read
        # supports an initiation fact; service death/timeout remains unknown.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = subprocess.run(["/usr/bin/systemctl", "show", "--property=SystemState", "--value"],
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   timeout=1, check=False,
                                   env={"PATH": "/usr/bin", "LANG": "C"})
            if state.returncode == 0 and state.stdout.strip() == "stopping":
                return True
            time.sleep(0.05)
        return False
