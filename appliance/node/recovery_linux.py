"""Root broker/HostCore packet port and independent fixed-unit observations."""
from __future__ import annotations

import os
import socket
import stat
import subprocess
from pathlib import Path
from uuid import UUID

from appliance.kernel.clock import boottime_ms
from appliance.kernel.unix_credentials import receive_credential_packet
from appliance.node.recovery import RecoveryObligation, canonical
from appliance.process_identity import read_proc_start_ticks
from contracts.strict_json import loads_object

SOCKET = Path("/run/photo-wall-recovery/control.sock")
UNIT = "photo-wall-node-player.service"
GROUP = "/photowallapp.slice/" + UNIT
ROOTS = "/run/photo-wall-node-storage/app-roots/"


def unit_rows(unit):
    output = subprocess.run(["/usr/bin/systemctl", "show", unit,
        "--property=LoadState,ActiveState,MainPID,InvocationID,ControlGroup,RootDirectory,Job"],
        check=True, capture_output=True, text=True, timeout=0.25,
        env={"PATH": "/usr/bin", "LANG": "C"}).stdout
    if len(output) > 8192:
        raise ValueError("recovery_observation_bound")
    rows = dict(line.split("=", 1) for line in output.splitlines())
    if set(rows) != {"LoadState", "ActiveState", "MainPID", "InvocationID", "ControlGroup", "RootDirectory", "Job"}:
        raise ValueError("recovery_observation")
    return rows


class RecoveryObserver:
    def stopped(self, obligation):
        # Host observation only advances the deadline phase; it never authorizes spawn.
        rows = unit_rows(UNIT)
        if (rows["MainPID"] != "0" or rows["ActiveState"] not in ("inactive", "failed")
                or rows["Job"] not in ("", "0")):
            return False
        proc = Path("/proc") / str(obligation.old_process.pid)
        try:
            proc.stat()
        except FileNotFoundError:
            pass
        else:
            return False  # Includes unknown or reused PIDs; safe deadline escalation.
        group = Path("/sys/fs/cgroup") / GROUP.lstrip("/")
        try:
            group.stat()
        except FileNotFoundError:
            return True
        events = dict(line.split() for line in (group / "cgroup.events").read_text().splitlines())
        return events.get("populated") == "0"

    def controlled(self, obligation, progress, *, now_ms):
        if (set(progress) != {"kind", "process", "app_epoch", "environment", "sampled_ms", "challenge_sha256"}
                or progress["kind"] != "controlled" or progress["environment"] not in (obligation.target, obligation.fallback)
                or type(progress["app_epoch"]) is not int or progress["app_epoch"] <= obligation.old_app_epoch
                or type(progress["sampled_ms"]) is not int or not obligation.armed_ms <= progress["sampled_ms"] <= now_ms
                or now_ms - progress["sampled_ms"] > 5000):
            return False
        from contracts.node_protocol import NodeProcessIdentity, digest
        digest(progress["challenge_sha256"])
        process = progress["process"]
        identity = NodeProcessIdentity(**{**process, "invocation_id": UUID(process["invocation_id"])})
        rows = unit_rows(UNIT)
        return (rows["ActiveState"] == "active" and rows["Job"] in ("", "0")
                and rows["MainPID"] == str(process["pid"])
                and UUID(rows["InvocationID"]) == UUID(process["invocation_id"])
                and rows["ControlGroup"] == GROUP
                and rows["RootDirectory"] == ROOTS + progress["environment"] + "/rootfs"
                and read_proc_start_ticks(Path("/proc"), process["pid"]) == process["start_ticks"]
                and identity.invocation_id != obligation.old_process.invocation_id
                and "0::" + GROUP in (Path("/proc") / str(identity.pid) / "cgroup").read_text().splitlines()
                and unit_rows(UNIT) == rows)


def broker_peer(pid, uid):
    return (uid == 0 and pid > 0 and unit_rows("photo-wall-app-broker.service")["MainPID"] == str(pid)
            and "0::/photowallbase.slice/photo-wall-app-broker.service" in
            (Path("/proc") / str(pid) / "cgroup").read_text().splitlines())


class RecoveryServer:
    def __init__(self, supervisor, path=SOCKET):
        self.supervisor, self.path = supervisor, path
        info = path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("recovery_directory")
        try:
            info = path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
                raise ValueError("recovery_socket")
            path.unlink()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        self.listener.bind(str(path))
        os.chmod(path, 0o600)
        self.listener.listen(4)
        self.listener.settimeout(0.01)

    def close(self):
        self.listener.close()

    def serve_one(self):
        try:
            connection, _ = self.listener.accept()
        except TimeoutError:
            return
        with connection:
            connection.settimeout(0.1)
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            try:
                (pid, uid, _), raw = receive_credential_packet(connection, maximum=8192)
                if not broker_peer(pid, uid):
                    raise ValueError("recovery_peer")
                value = loads_object(raw, max_bytes=8192)
                if value is None or value.get("schema") != 1:
                    raise ValueError("recovery_packet")
                if set(value) == {"schema", "arm"}:
                    result = self.supervisor.arm(RecoveryObligation.parse(value["arm"]), now_ms=boottime_ms())
                elif set(value) == {"schema", "operation_id", "receipt", "progress"}:
                    self.supervisor.advance(UUID(value["operation_id"]), value["receipt"], value["progress"], now_ms=boottime_ms())
                    result = value["receipt"]
                else:
                    raise ValueError("recovery_packet")
                connection.send(canonical({"receipt": result}))
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                if self.supervisor.store.failed:
                    raise
                try:
                    connection.send(b'{"error":"recovery_refused"}')
                except OSError:
                    pass


class RecoveryClient:
    def __init__(self, path=SOCKET):
        self.path = path

    def _request(self, packet, receipt):
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as connection:
            connection.settimeout(1)
            connection.connect(str(self.path))
            connection.send(canonical({"schema": 1, **packet}))
            result = loads_object(connection.recv(8193), max_bytes=8192)
            if result != {"receipt": receipt}:
                raise ValueError("recovery_receipt")
        return receipt

    def arm(self, obligation):
        return self._request({"arm": obligation.document()}, obligation.receipt)

    def advance(self, obligation, progress):
        return self._request({"operation_id": str(obligation.operation_id), "receipt": obligation.receipt,
                              "progress": progress}, obligation.receipt)
