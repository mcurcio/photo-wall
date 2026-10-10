"""Single-writer stop journal and cooperative Linux observation adapter."""
from __future__ import annotations

import logging
import os
import stat
import subprocess
import tempfile
from uuid import UUID

from appliance.apps.stop_operation import (
    StopGuaranteeUnavailable,
    StopView,
    stop_request_document,
)
from appliance.kernel.clock import boottime_ms
from appliance.node.recovery import STOP_TIMEOUT_SECONDS  # noqa: F401

LOG = logging.getLogger(__name__)
UNIT = "photo-wall-node-player.service"
GROUP = "/photowallapp.slice/" + UNIT
PROPERTIES = "LoadState,ActiveState,MainPID,InvocationID,ControlGroup,RootDirectory,Job,Restart,Delegate,KillMode,TimeoutStopUSec"
TRANSPORT_TIMEOUT_MS = 1000


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
        self.retiring = []
        self.capture_allowed = False

    def close(self, *, keep_identity=False):
        """Release local resources without waiting for a transport subprocess.

        poll() reaps with WNOHANG; the serial service loop finishes reaping a killed
        child. This never signals the Player or cancels its durable operation.
        """
        if self.child is not None:
            if self.child.poll() is None:
                try:
                    self.child.kill()
                except ProcessLookupError:
                    pass
                self.retiring.append(self.child)
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
        if (row is None or type(row.get("schema")) is not int or row.get("schema") != 1 or row.get("request") != stop_request_document(request)
                or set(row) != {"schema", "request", "identity", "dispatch", "completed_ms", "fault", "diagnostic"}
                or row["dispatch"] not in ("not_dispatched", "dispatch_unknown", "acknowledged")):
            raise StopGuaranteeUnavailable("stop_journal_invalid")
        identity = row["identity"]
        if identity is not None and (type(identity) is not dict or set(identity) != {"group", "root"}
                or any(type(identity[key]) is not list or len(identity[key]) != 2
                       or any(type(v) is not int or v < 0 for v in identity[key])
                       for key in ("group", "root"))):
            raise StopGuaranteeUnavailable("stop_journal_invalid")
        if (row["completed_ms"] is not None and (type(row["completed_ms"]) is not int
                or row["completed_ms"] < 1 or identity is None or row["fault"] is not None)
                or row["fault"] is not None and (type(row["fault"]) is not str or len(row["fault"]) > 128)
                or identity is None and row["dispatch"] != "not_dispatched"):
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
            self.capture_allowed = False
        self.request = request
        self.name = "stop-" + str(request.operation_id)
        self.view = StopView(request, lambda request=request, name=self.name: self._read(request, name))
        prior = self.driver.store.read(self.name)
        if prior is not None:
            row = self._read()
            # Repeated admission in this owner preserves capture progress; a new
            # owner never recreates missing evidence or dispatches a recovered intent.
            if row["identity"] is None and not self.capture_allowed and row["fault"] is None:
                self._save({**row, "fault": "stop_capture_unavailable"})
            return self.view
        self.capture_allowed = False
        if reattach_only:
            raise StopGuaranteeUnavailable("stop_capture_unavailable")
        self._save({"schema": 1, "request": stop_request_document(request), "identity": None,
                    "dispatch": "not_dispatched", "completed_ms": None, "fault": None, "diagnostic": None})
        self.capture_allowed = True
        # No PID1 wait or live-process observation happens on the caller's stack.
        return self.view

    def _identity(self, rows):
        expected = self.request.old
        root = self.driver.roots / expected.environment.environment_sha256 / "rootfs"
        if (rows["InvocationID"] and UUID(rows["InvocationID"]) != expected.process.invocation_id
                or rows["ControlGroup"] and rows["ControlGroup"] != GROUP
                or rows["RootDirectory"] and rows["RootDirectory"] != str(root)
                or rows["MainPID"] not in ("0", str(expected.process.pid))):
            raise StopGuaranteeUnavailable("stop_identity_changed")
        if rows["LoadState"] != "not-found" and (
                rows["Restart"] != "no" or rows["Delegate"] != "no" or rows["KillMode"] != "control-group"
                or rows["TimeoutStopUSec"] != f"{STOP_TIMEOUT_SECONDS}s"):
            raise StopGuaranteeUnavailable("stop_unit_policy_changed")
        return root

    def _capture_and_dispatch(self, rows):
        root = self._identity(rows)
        expected = self.request.old
        if (rows["ActiveState"] != "active" or rows["MainPID"] != str(expected.process.pid)
                or not rows["InvocationID"] or rows["ControlGroup"] != GROUP
                or rows["RootDirectory"] != str(root) or rows["Job"] not in ("", "0")):
            raise StopGuaranteeUnavailable("stop_capture_unavailable")
        if proc_birth(self.driver.proc, expected.process.pid) != expected.process.start_ticks:
            raise StopGuaranteeUnavailable("stop_capture_unavailable")
        group = self.driver.cgroups / GROUP.lstrip("/")
        if self.group_fd < 0:
            self.group_fd = os.open(group, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(self.group_fd)
        current = group.lstat()
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise StopGuaranteeUnavailable("stop_cgroup_replaced")
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            root_info = os.fstat(root_fd)
            actual_root = (self.driver.proc / str(expected.process.pid) / "root").stat()
            current_root = root.lstat()
            if ((actual_root.st_dev, actual_root.st_ino) != (root_info.st_dev, root_info.st_ino)
                    or (current_root.st_dev, current_root.st_ino) != (root_info.st_dev, root_info.st_ino)):
                raise StopGuaranteeUnavailable("stop_root_changed")
        finally:
            os.close(root_fd)
        with (self.driver.proc / str(expected.process.pid) / "cgroup").open("rb") as stream:
            cgroup = stream.read(4097)
        if len(cgroup) > 4096 or f"0::{GROUP}".encode() not in cgroup.splitlines():
            raise StopGuaranteeUnavailable("stop_identity_changed")
        if proc_birth(self.driver.proc, expected.process.pid) != expected.process.start_ticks:
            raise ValueError("stop_capture_changed_during_observation")
        row = {**self._read(), "identity": {"group": [info.st_dev, info.st_ino],
                                           "root": [root_info.st_dev, root_info.st_ino]},
               "diagnostic": None}
        self._save(row)  # Captured evidence is durable before the dispatch barrier.
        self.capture_allowed = False
        self._save({**row, "dispatch": "dispatch_unknown"})
        if boottime_ms() >= self.request.dispatch_not_after_boottime_ms:
            self._save({**self._read(), "fault": "stop_dispatch_expired"})
            self.close()
            return
        try:
            self.child = subprocess.Popen(["/usr/bin/systemctl", "--no-block", "stop", UNIT],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={"PATH": "/usr/bin", "LANG": "C"})
            self.submitting = True
            self.child_started = boottime_ms()
        except OSError:
            self._save({**self._read(), "diagnostic": "stop_submission_unavailable"})

    def _observe(self, rows):
        self._identity(rows)
        expected = self.request.old
        row = self._read()
        if row["identity"] is None:
            raise StopGuaranteeUnavailable("stop_capture_unavailable")
        group = self.driver.cgroups / GROUP.lstrip("/")
        try:
            info = group.lstat()
        except FileNotFoundError:
            empty = True
        else:
            if not stat.S_ISDIR(info.st_mode) or [info.st_dev, info.st_ino] != row["identity"]["group"]:
                raise StopGuaranteeUnavailable("stop_cgroup_replaced")
            if self.group_fd < 0:
                self.group_fd = os.open(group, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            opened = os.fstat(self.group_fd)
            if [opened.st_dev, opened.st_ino] != row["identity"]["group"]:
                raise StopGuaranteeUnavailable("stop_cgroup_replaced")
            fd = os.open("cgroup.events", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.group_fd)
            with os.fdopen(fd, "rb") as stream:
                raw = stream.read(4097)
            events = dict(line.split() for line in raw.decode().splitlines())
            if len(raw) > 4096 or events.get("populated") not in ("0", "1"):
                raise ValueError("stop_cgroup_malformed")
            empty = events["populated"] == "0"
        if proc_birth(self.driver.proc, expected.process.pid) == expected.process.start_ticks:
            # A live old process is pending. Its teardown root metadata is not
            # needed: the exact root and subtree were captured before dispatch.
            return False
        return (empty and rows["MainPID"] == "0" and rows["Job"] in ("", "0")
                and (rows["LoadState"] == "not-found" or rows["ActiveState"] in ("inactive", "failed")))

    def service(self, *, now_ms, budget_ms):
        self.retiring = [child for child in self.retiring if child.poll() is None]
        if (self.request is None or budget_ms <= 0 or self.view.done() or self.retiring):
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
                if now_ms - self.child_started >= TRANSPORT_TIMEOUT_MS:
                    self.close(keep_identity=True)  # Only the bounded transport child.
                    self._save({**self._read(), "diagnostic": "stop_transport_timeout"})
                return
            self.child = None
            if self.submitting:
                self._save({**self._read(), **({"dispatch": "acknowledged", "diagnostic": None} if status == 0
                    else {"diagnostic": "stop_submission_unavailable"})})
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
            if self._read()["identity"] is None:
                if not self.capture_allowed:
                    raise StopGuaranteeUnavailable("stop_capture_unavailable")
                self._capture_and_dispatch(rows)
            elif self._observe(rows):
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
