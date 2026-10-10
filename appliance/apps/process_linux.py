"""Base-owned exact-root systemd effect driver. No shell or host app interpreter."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from appliance.apps.broker import RunningApp
from appliance.apps.device_grants import DeviceGrant, player_device_grants
from appliance.apps.environment import mounted_root
from appliance.apps.lifecycle_storage import primitive, running_from
from appliance.apps.stop_linux import STOP_TIMEOUT_SECONDS, StopObserver
from appliance.kernel.boot_store import BootStore
from appliance.kernel.capacity import ROOT_IMAGES, line
from appliance.kernel.display_paths import DISPLAY_UNIT, WAYLAND_DIRECTORY, WAYLAND_SOCKET
from appliance.kernel.image_mount import ImageMounter, SystemdImageMounter
from appliance.process_identity import read_proc_start_ticks
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_protocol import NodeProcessIdentity

UNIT = "photo-wall-node-player.service"
# pw-display's fixed id (the base's sysusers.d, debian/photo-wall-node.sysusers): the Player joins
# it to traverse WAYLAND_DIRECTORY (0750) and connect to the socket.
PW_DISPLAY_GID = 10005
APP_PROPERTIES = ("LoadState", "ActiveState", "SubState", "MainPID", "InvocationID", "ControlGroup",
                  "RootDirectory")
DISPLAY_PROPERTIES = ("ActiveState", "InvocationID")


def systemctl_show(unit: str, properties: tuple[str, ...] = APP_PROPERTIES) -> dict[str, str]:
    result = subprocess.run(["/usr/bin/systemctl", "show", unit, "--property=" + ",".join(properties)],
                            capture_output=True, text=True, timeout=5, check=True,
                            env={"PATH": "/usr/bin", "LANG": "C"})
    if len(result.stdout) > 16384:
        raise ValueError("process_observation_bound")
    rows = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if set(rows) != set(properties):
        raise ValueError("process_observation_invalid")
    if rows["InvocationID"]:
        rows["InvocationID"] = str(UUID(rows["InvocationID"]))
    elif rows.get("MainPID", "0") != "0":
        raise ValueError("process_invocation_missing")
    return rows


def process_root_matches(proc: Path, pid: int, root: Path) -> bool:
    """Compare kernel filesystem identity across pivot_root mount namespaces.

    /proc/PID/root's textual target can be '/' even for an isolated root. stat
    follows that process's root capability and identifies the actual directory.
    """
    descriptor = -1
    try:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        expected = os.fstat(descriptor)
        actual = (proc / str(pid) / "root").stat()
        current = root.lstat()
        identity = expected.st_dev, expected.st_ino
        return (expected.st_nlink > 0 and actual.st_nlink > 0
                and (actual.st_dev, actual.st_ino) == identity
                and (current.st_dev, current.st_ino) == identity)
    except OSError:
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def app_unit_properties(root: Path, devices: Sequence[DeviceGrant]) -> tuple[str, ...]:
    """Single production sandbox definition, also exercised by real PID1 probes.

    The Player runs bound to one Weston incarnation (`BindsTo=` + `After=` the display unit):
    its start waits for Weston's READY=1, and it stops when that Weston does; the broker starts
    it again for the next incarnation (`AppEffectBroker.reconcile`). It reaches Weston through
    WAYLAND_DIRECTORY, bound in whole at the same path (the directory outlives every Weston, so
    the bind never holds a stale socket), with WAYLAND_DISPLAY the socket's absolute path; its
    XDG_RUNTIME_DIR stays its own private tmpfs.

    Deny-by-default devices (`PrivateDevices=yes`): the only device nodes in the Player's `/dev`
    are `devices` (`player_device_grants`), each bound in read-only (a read-only mount never
    stops a device node opening read-write), allowed by the cgroup device policy and opened
    through its group. Raw DRM/KMS (`card*`) can never be among them (`DeviceGrant`).
    """
    groups = " ".join(str(gid) for gid in sorted({PW_DISPLAY_GID, *(d.gid for d in devices)}))
    grants = tuple(f"DeviceAllow={device.path} rw" for device in devices)
    if devices:
        grants += ("BindReadOnlyPaths=" + " ".join(device.path for device in devices),)
    return (
        f"BindsTo={DISPLAY_UNIT}", f"After={DISPLAY_UNIT}",
        f"RootDirectory={root}", "User=10004", "Group=10004", f"SupplementaryGroups={groups}", "Slice=photowallapp.slice",
        "ProtectSystem=strict", "ProtectHome=yes", "PrivateDevices=yes", "PrivateTmp=yes",
        "NoNewPrivileges=yes", "CapabilityBoundingSet=", "RestrictSUIDSGID=yes",
        "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectControlGroups=yes",
        "RestrictNamespaces=yes", "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        f"MemoryMax={line('app').cap_bytes}", "MemorySwapMax=0", "OOMScoreAdjust=500", "TasksMax=128", "CPUQuota=200%",
        "TemporaryFileSystem=/run:rw,nosuid,nodev,size=64M /run/photo-wall/player:rw,nosuid,nodev,noexec,size=1M,uid=10004,gid=10004,mode=0700 /run/photo-wall-wayland:rw,nosuid,nodev,noexec,size=1M,uid=10004,gid=10004,mode=0700 /tmp:rw,nosuid,nodev,size=128M",
        f"BindReadOnlyPaths=/run/photo-wall-app-proof:/run/photo-wall-client {WAYLAND_DIRECTORY} /etc/photo-wall/public.json:/etc/photo-wall/public.json /etc/resolv.conf:/etc/resolv.conf",
        *grants,
        "RuntimeMaxSec=infinity", "Restart=no", "KillMode=control-group",
        f"TimeoutStopSec={STOP_TIMEOUT_SECONDS}", "Delegate=no",
        f"Environment=HOME=/tmp XDG_RUNTIME_DIR=/run/photo-wall-wayland WAYLAND_DISPLAY={WAYLAND_SOCKET} GDK_BACKEND=wayland PHOTO_WALL_DISPLAY_HOST=1 PYTHONNOUSERSITE=1 GST_REGISTRY=/tmp/gst-registry.bin",
        "UnsetEnvironment=PYTHONPATH PYTHONHOME LD_LIBRARY_PATH LD_PRELOAD GI_TYPELIB_PATH GST_PLUGIN_PATH GST_PLUGIN_PATH_1_0 GST_PLUGIN_SYSTEM_PATH GST_PLUGIN_SYSTEM_PATH_1_0",
    )


class SystemdAppProcessDriver:
    def __init__(self, roots: Path, store: BootStore, *, base_abi: str,
                 graphics_abi: str, plugin_abi: str, proc: Path = Path("/proc"),
                 cgroups: Path = Path("/sys/fs/cgroup"), images: Path = ROOT_IMAGES,
                 mounter: ImageMounter | None = None, sysfs: Path = Path("/sys")):
        self.roots, self.store, self.proc, self.sysfs = roots, store, proc, sysfs
        self.cgroups, self.images = cgroups, images
        self.mounter = mounter if mounter is not None else SystemdImageMounter()
        self.stops = StopObserver(self)
        self.abi = dict(base_abi=base_abi, graphics_abi=graphics_abi, plugin_abi=plugin_abi)

    def verify(self, environment: AppEnvironmentRefV2) -> bool:
        if environment.deb_name != "photo-wall-player":
            raise ValueError("app_package_kind_mismatch")
        # C3 as amended (errata E-E2C-DR-2): the root is staged for this Node's measured ABI; the
        # image digest proved its tree at staging, so no walk here.
        mounted_root(self.roots, environment, images=self.images, mounter=self.mounter, **self.abi)
        return True

    def display_incarnation(self) -> str | None:
        """The running Weston's InvocationID; None unless the display unit is active."""
        rows = systemctl_show(DISPLAY_UNIT, DISPLAY_PROPERTIES)
        return (rows["InvocationID"] or None) if rows["ActiveState"] == "active" else None

    def launched_display(self) -> str | None:
        """The Weston incarnation recorded when the current launch was spawned."""
        launch = self.store.read("launch")
        return None if launch is None else launch.get("display")

    def unit_collected(self) -> bool:
        """PID1 unloaded the fixed unit name (after --collect) and no app or job remains."""
        return systemctl_show(UNIT)["LoadState"] == "not-found" and self.absent_and_quiescent()

    def select(self, environment: AppEnvironmentRefV2) -> None:
        self.verify(environment)
        self.store.write("selected", {"environment": primitive(environment)})

    def _observe(self, environment: AppEnvironmentRefV2, operation_id: UUID,
                 epoch: int) -> RunningApp | None:
        rows = systemctl_show(UNIT)
        if rows["LoadState"] == "not-found" or (rows["ActiveState"] in ("inactive", "failed") and rows["MainPID"] == "0"):
            return None
        if rows["ActiveState"] != "active":
            raise ValueError("app_process_ambiguous")
        return self._process_observation(rows, environment, operation_id, epoch)

    def _process_observation(self, rows: dict[str, str], environment: AppEnvironmentRefV2,
                             operation_id: UUID, epoch: int) -> RunningApp:
        root = self.roots / environment.environment_sha256 / "rootfs"
        if rows["RootDirectory"] != str(root) or not rows["ControlGroup"].startswith("/photowallapp.slice/"):
            raise ValueError("app_process_ambiguous")
        pid = int(rows["MainPID"])
        ticks = read_proc_start_ticks(self.proc, pid)
        if ticks is None:
            raise ValueError("app_process_birth_unavailable")
        # Check the kernel cgroup, not just PID1's claimed MainPID; sample birth twice.
        cgroups = (self.proc / str(pid) / "cgroup").read_text().splitlines()
        if f"0::{rows['ControlGroup']}" not in cgroups or read_proc_start_ticks(self.proc, pid) != ticks:
            raise ValueError("app_process_cgroup_mismatch")
        if not process_root_matches(self.proc, pid, root):
            raise ValueError("app_process_root_mismatch")
        if systemctl_show(UNIT) != rows or read_proc_start_ticks(self.proc, pid) != ticks:
            raise ValueError("app_process_changed_during_observation")
        return RunningApp(environment, NodeProcessIdentity(pid, ticks, UUID(rows["InvocationID"])), epoch, operation_id)

    def current(self) -> RunningApp | None:
        launch = self.store.read("launch")
        if launch is None:
            rows = systemctl_show(UNIT)
            if rows["LoadState"] == "not-found" or (rows["MainPID"] == "0" and rows["ActiveState"] in ("inactive", "failed")):
                return None
            raise ValueError("untracked_app_process")
        running = running_from(launch["running"]) if launch.get("running") else None
        result = self._observe(AppEnvironmentRefV2(**launch["environment"]), UUID(launch["operation_id"]), launch["epoch"])
        if running is not None and result is not None and running.process != result.process:
            raise ValueError("app_process_incarnation_mismatch")
        return result

    def kill(self, expected: RunningApp) -> bool:
        """SIGKILL exactly `expected`'s main process; False (no signal) on any mismatch.

        The pidfd pins one process before its identity is checked, so a pid reused after the
        check cannot receive the signal: kernel birth (start ticks) and systemd's MainPID and
        InvocationID must all match. KillMode=control-group takes the rest of the unit;
        Restart=no keeps it down. An unavailable observation raises (nothing was sent).
        """
        pid = expected.process.pid
        try:
            descriptor = os.pidfd_open(pid)
        except ProcessLookupError:
            return False
        try:
            if read_proc_start_ticks(self.proc, pid) != expected.process.start_ticks:
                return False
            rows = systemctl_show(UNIT)
            if (rows["MainPID"] != str(pid)
                    or rows["InvocationID"] != str(expected.process.invocation_id)):
                return False
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                return False
            return True
        finally:
            os.close(descriptor)

    def absent_and_quiescent(self) -> bool:
        if self.current() is not None:
            return False
        job = subprocess.run(["/usr/bin/systemctl", "show", UNIT, "--property=Job", "--value"],
                             check=True, capture_output=True, text=True, timeout=5).stdout.strip()
        return job in ("", "0") and self.current() is None

    def quiescent(self, expected: RunningApp) -> bool:
        if self.current() != expected:
            return False
        job = subprocess.run(["/usr/bin/systemctl", "show", UNIT, "--property=Job", "--value"],
                             check=True, capture_output=True, text=True, timeout=5).stdout.strip()
        return job in ("", "0") and self.current() == expected

    def _cgroup_empty(self, control_group: str) -> bool:
        group = self.cgroups / control_group.lstrip("/")
        try:
            events = dict(line.split() for line in (group / "cgroup.events").read_text().splitlines())
            return events.get("populated") == "0"
        except FileNotFoundError:
            if group.exists():
                raise ValueError("app_stop_cgroup_evidence_missing")
            return True  # A removed cgroup subtree cannot retain children.

    def _stop_sample(self, expected: RunningApp, control_group: str) -> bool:
        """Stopping is pending; only stable unit, process and cgroup absence completes."""
        try:
            rows = systemctl_show(UNIT)
        except ValueError as error:
            if str(error) == "process_invocation_missing":
                return False  # An unavailable identity never proves terminal absence.
            raise
        root = str(self.roots / expected.environment.environment_sha256 / "rootfs")
        if (rows["InvocationID"] and rows["InvocationID"] != str(expected.process.invocation_id)
                or rows["RootDirectory"] and rows["RootDirectory"] != root
                or rows["ControlGroup"] and rows["ControlGroup"] != control_group
                or rows["MainPID"] not in ("0", str(expected.process.pid))):
            raise ValueError("app_stop_identity_changed")
        terminal = rows["MainPID"] == "0" and (rows["LoadState"] == "not-found" or
            rows["ActiveState"] in ("inactive", "failed"))
        if terminal:
            if read_proc_start_ticks(self.proc, expected.process.pid) == expected.process.start_ticks:
                return False
            if not self._cgroup_empty(control_group):
                return False
            return self.absent_and_quiescent() and systemctl_show(UNIT) == rows
        if rows["ActiveState"] not in ("active", "deactivating"):
            raise ValueError("app_stop_identity_changed")
        # PID1 may clear fields during teardown; surviving identities were checked.
        if rows["ActiveState"] == "deactivating" and any(
                not rows[key] for key in ("InvocationID", "RootDirectory", "ControlGroup")):
            return False
        if rows["MainPID"] == "0" and rows["ActiveState"] == "deactivating":
            return False
        if rows["MainPID"] != str(expected.process.pid):
            raise ValueError("app_stop_identity_changed")
        try:
            observed = self._process_observation(rows, expected.environment,
                                                 expected.operation_id, expected.app_epoch)
        except FileNotFoundError:
            return False
        except ValueError as error:
            if str(error) in ("app_process_birth_unavailable", "app_process_changed_during_observation"):
                return False
            if (str(error) in ("app_process_root_mismatch", "app_process_cgroup_mismatch")
                    and read_proc_start_ticks(self.proc, expected.process.pid) is None):
                return False
            raise
        if observed != expected:
            raise ValueError("app_stop_identity_changed")
        return False

    def stop(self, request, *, reattach_only=False):
        return self.stops.stop(request, reattach_only=reattach_only)

    def service(self, *, now_ms, budget_ms):
        self.stops.service(now_ms=now_ms, budget_ms=budget_ms)

    def _await_unit_unloaded(self, previous: dict | None) -> None:
        """Wait for PID1's asynchronous collection before reusing the fixed name."""
        expected = running_from(previous["running"]) if previous and previous.get("running") else None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.current() is not None:
                raise ValueError("app_already_running")
            rows = systemctl_show(UNIT)
            if rows["MainPID"] != "0" or rows["ActiveState"] not in ("inactive", "failed"):
                raise ValueError("app_stop_identity_changed")
            if previous is not None and expected is None:
                root = str(self.roots / previous["environment"]["environment_sha256"] / "rootfs")
                if (rows["RootDirectory"] and rows["RootDirectory"] != root
                        or rows["ControlGroup"] and rows["ControlGroup"] != "/photowallapp.slice/" + UNIT):
                    raise ValueError("app_stop_identity_changed")
            if expected is not None:
                absent = self._stop_sample(expected, "/photowallapp.slice/" + UNIT)
            elif rows["LoadState"] == "not-found":
                absent = self._cgroup_empty("/photowallapp.slice/" + UNIT) and self.absent_and_quiescent()
            elif previous is not None:
                # An exec failure may leave only our durable launch intent before
                # --collect unloads the unit. Wait; never manufacture an identity.
                absent = False  # Loaded intent-only units can never authorize spawn.
            else:
                raise ValueError("untracked_app_process")
            if absent and rows["LoadState"] == "not-found" and systemctl_show(UNIT) == rows:
                return
            time.sleep(0.05)
        raise ValueError("app_previous_unit_not_unloaded")

    def start(self, environment: AppEnvironmentRefV2, operation_id: UUID) -> RunningApp:
        if self.current() is not None:
            raise ValueError("app_already_running")
        if self.store.read("selected") != {"environment": primitive(environment)}:
            raise ValueError("app_selection_mismatch")
        self.verify(environment)
        previous = self.store.read("launch")
        self._await_unit_unloaded(previous)
        epoch = previous["epoch"] + 1 if previous else 1
        # Read before the spawn: a Weston that restarts in between costs one spare relaunch,
        # never a missed one.
        launch = {"environment": primitive(environment), "operation_id": str(operation_id),
                  "epoch": epoch, "running": None, "display": self.display_incarnation()}
        self.store.write("launch", launch)
        root = self.roots / environment.environment_sha256 / "rootfs"
        # All paths visible to the process are inside RootDirectory except these exact
        # base-selected read-only IPC/config binds and this Node's granted device nodes.
        # No host /usr, libraries or plugins.
        properties = app_unit_properties(root, player_device_grants(self.sysfs))
        command = ["/usr/bin/systemd-run", "--quiet", "--collect", "--unit=" + UNIT, "--service-type=exec"]
        for prop in properties:
            command.extend(("--property", prop))
        command.extend(("--", environment.entry_point))
        subprocess.run(command, check=True, timeout=15, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env={"PATH": "/usr/bin", "LANG": "C"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                observed = self._observe(environment, operation_id, epoch)
            except (ValueError, FileNotFoundError):
                observed = None
            if observed is not None:
                self.store.write("launch", {**launch, "running": primitive(observed)})
                return observed
            time.sleep(0.05)
        raise ValueError("app_start_outcome_unknown")
