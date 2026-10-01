"""Single-writer stop journal and cooperative Linux observation adapter."""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from uuid import UUID

from appliance.node.clock import boottime_ms
from appliance.node.lifecycle_storage import primitive
from appliance.node.recovery import STOP_TIMEOUT_SECONDS  # noqa: F401
from appliance.node.stop_operation import StopGuaranteeUnavailable, StopView

LOG = logging.getLogger(__name__)
UNIT = "photo-wall-node-player.service"
GROUP = "/photowallapp.slice/" + UNIT
PROPERTIES = "LoadState,ActiveState,MainPID,InvocationID,ControlGroup,RootDirectory,Job,Restart,Delegate,KillMode,TimeoutStopUSec"


def proc_birth(proc, pid):
    """Only ENOENT means absence; permissions and malformed records stay unknown."""
    try:
        with (proc / str(pid) / "stat").open("rb") as stream:
            raw = stream.read(4097)
    except FileNotFoundError:
        return None
    close = raw.rfind(b") ")
    fields = raw[close + 2:].split()
    if (len(raw) > 4096 or not raw.startswith(f"{pid} (".encode()) or close < 0
            or len(fields) < 20 or not fields[19].isdigit() or int(fields[19]) <= 0):
        raise ValueError("stop_proc_malformed")
    return int(fields[19])


class StopObserver:
    def __init__(self, driver):
        self.driver, self.request, self.name = driver, None, None
        self.group_fd = -1
        self.child = self.output = None
        self.child_started = 0
        self.submitting = False

    def close(self, *, keep_identity=False):
        if self.child is not None:
            if self.child.poll() is None:
                self.child.kill()
            self.child.wait(timeout=1)
            self.child = None
        if self.output is not None:
            self.output.close()
            self.output = None
        if self.group_fd >= 0 and not keep_identity:
            os.close(self.group_fd)
            self.group_fd = -1

    def _read(self, request=None, name=None):
        request = request or self.request
        row = self.driver.store.read(name or self.name)
        if (row is None or row.get("schema") != 1 or row.get("request") != primitive(request)
                or set(row) != {"schema", "request", "identity", "dispatch", "completed_ms", "fault", "diagnostic"}
                or row["dispatch"] not in ("not_dispatched", "dispatch_unknown", "acknowledged")):
            raise StopGuaranteeUnavailable("stop_journal_invalid")
        return row

    def _save(self, row):
        self.driver.store.write(self.name, row)

    def stop(self, request, *, reattach_only):
        if str(request.boot_id) != self.driver.store.binding["boot_id"]:
            raise StopGuaranteeUnavailable("stop_boot_changed")
        if self.request is not None and self.request != request:
            if not self.view.done():
                raise StopGuaranteeUnavailable("stop_owner_busy")
            self.close()
        self.request = request
        self.name = "stop-" + str(request.operation_id)
        self.view = StopView(request, lambda request=request, name=self.name: self._read(request, name))
        prior = self.driver.store.read(self.name)
        if prior is not None:
            self._read()  # Exact request/schema check before returning a recovered view.
            return self.view
        if reattach_only:
            raise StopGuaranteeUnavailable("stop_capture_unavailable")
        if self.driver.current() != request.old:
            raise StopGuaranteeUnavailable("stop_identity_changed")
        group = self.driver.cgroups / GROUP.lstrip("/")
        self.group_fd = os.open(group, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(self.group_fd)
        root = self.driver.roots / request.old.environment.environment_sha256 / "rootfs"
        root_info = root.stat()
        row = {"schema": 1, "request": primitive(request),
               "identity": {"group": [info.st_dev, info.st_ino],
                            "root": [root_info.st_dev, root_info.st_ino]},
               "dispatch": "not_dispatched", "completed_ms": None, "fault": None, "diagnostic": None}
        # Lifecycle intent is already durable. This record precedes any syscall.
        self._save(row)
        if self.driver.current() != request.old:
            self._save({**row, "fault": "stop_identity_changed"})
            return self.view
        policy = subprocess.run(["/usr/bin/systemctl", "show", UNIT,
            "--property=Restart,Delegate,KillMode,TimeoutStopUSec"], capture_output=True,
            text=True, check=True, timeout=0.25).stdout
        if dict(line.split("=", 1) for line in policy.splitlines()) != {
                "Restart": "no", "Delegate": "no", "KillMode": "control-group", "TimeoutStopUSec": "30s"}:
            self._save({**row, "fault": "stop_unit_policy_changed"})
            return self.view
        self._save({**row, "dispatch": "dispatch_unknown"})
        if boottime_ms() >= request.dispatch_not_after_boottime_ms:
            self._save({**row, "fault": "stop_dispatch_expired"})
            return self.view
        try:
            self.child = subprocess.Popen(["/usr/bin/systemctl", "--no-block", "stop", UNIT],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/bin", "LANG": "C"})
            self.submitting = True
            self.child_started = boottime_ms()
        except OSError:
            self._save({**self._read(), "diagnostic": "stop_submission_unavailable"})
        return self.view

    def _observe(self, rows):
        expected = self.request.old
        root = self.driver.roots / expected.environment.environment_sha256 / "rootfs"
        if (rows["InvocationID"] and UUID(rows["InvocationID"]) != expected.process.invocation_id
                or rows["ControlGroup"] and rows["ControlGroup"] != GROUP
                or rows["RootDirectory"] and rows["RootDirectory"] != str(root)
                or rows["MainPID"] not in ("0", str(expected.process.pid))):
            raise StopGuaranteeUnavailable("stop_identity_changed")
        if rows["LoadState"] != "not-found" and (
                rows["Restart"] != "no" or rows["Delegate"] != "no" or rows["KillMode"] != "control-group"):
            raise StopGuaranteeUnavailable("stop_unit_policy_changed")
        row = self._read()
        group = self.driver.cgroups / GROUP.lstrip("/")
        try:
            info = group.lstat()
        except FileNotFoundError:
            empty = True
        else:
            if [info.st_dev, info.st_ino] != row["identity"]["group"]:
                raise StopGuaranteeUnavailable("stop_cgroup_replaced")
            if self.group_fd < 0:
                self.group_fd = os.open(group, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            opened = os.fstat(self.group_fd)
            if [opened.st_dev, opened.st_ino] != row["identity"]["group"]:
                raise StopGuaranteeUnavailable("stop_cgroup_replaced")
            # Open relative to the captured object; never follow a replaced path.
            fd = os.open("cgroup.events", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.group_fd)
            with os.fdopen(fd) as stream:
                events = dict(line.split() for line in stream.read(4096).splitlines())
            if events.get("populated") not in ("0", "1"):
                raise ValueError("stop_cgroup_malformed")
            empty = events["populated"] == "0"
        birth = proc_birth(self.driver.proc, expected.process.pid)
        if birth == expected.process.start_ticks:
            # Missing teardown metadata is transient; a surviving mismatch is not.
            actual_root = (self.driver.proc / str(expected.process.pid) / "root").stat()
            if [actual_root.st_dev, actual_root.st_ino] != row["identity"]["root"]:
                raise StopGuaranteeUnavailable("stop_root_changed")
            return False
        return (empty and rows["MainPID"] == "0" and rows["Job"] in ("", "0")
                and (rows["LoadState"] == "not-found" or rows["ActiveState"] in ("inactive", "failed")))

    def service(self, *, now_ms, budget_ms):
        if self.request is None or budget_ms <= 0 or self.view.done():
            return
        try:
            if self.child is None:
                self.output = tempfile.TemporaryFile()
                self.child = subprocess.Popen(["/usr/bin/systemctl", "show", UNIT, "--property=" + PROPERTIES],
                    stdin=subprocess.DEVNULL, stdout=self.output, stderr=subprocess.DEVNULL,
                    env={"PATH": "/usr/bin", "LANG": "C"})
                self.child_started = now_ms
                self.submitting = False
                return
            status = self.child.poll()
            if status is None:
                if now_ms - self.child_started >= 1000:
                    self.child.kill()  # Only our transport child; never signals the Player.
                return
            self.child = None
            if self.submitting:
                if status == 0:
                    self._save({**self._read(), "dispatch": "acknowledged"})
                return
            self.output.seek(0)
            raw = self.output.read(16385)
            self.output.close()
            self.output = None
            if status or len(raw) > 16384:
                raise ValueError("stop_observation_unavailable")
            rows = dict(line.split("=", 1) for line in raw.decode().splitlines())
            if set(rows) != set(PROPERTIES.split(",")):
                raise ValueError("stop_observation_malformed")
            if self._observe(rows):
                self._save({**self._read(), "completed_ms": now_ms, "diagnostic": None})
                self.close()
        except StopGuaranteeUnavailable as error:
            self._save({**self._read(), "fault": error.code})
            self.close()
        except (OSError, ValueError, subprocess.SubprocessError):
            if self.driver.store.failed:
                raise
            self.close(keep_identity=True)
            self._save({**self._read(), "diagnostic": "stop_observation_unavailable"})
        except Exception:
            LOG.exception("stop adapter programming fault")
            if self.driver.store.failed:
                raise
            self._save({**self._read(), "fault": "stop_adapter_fault"})
            self.close()
