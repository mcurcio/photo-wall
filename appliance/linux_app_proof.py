"""Linux readers for a future root-owned local app-proof socket service.

These readers only supply facts to LocalAppProofVerifier. They do not create a
socket, command authority, Central evidence, or an acceptance record. A caller
must pass the same accepted AF_UNIX stream socket to begin and verify.
"""

from __future__ import annotations

import re
import socket
import struct
import subprocess
from collections.abc import Callable
from pathlib import Path

from appliance.app_launcher import UNIT
from appliance.app_process_proof import PeerCredentials
from appliance.process_identity import read_proc_start_ticks
from contracts.app_process_proof import ProcessIdentity

_INVOCATION = re.compile(r"[0-9a-f]{32}")
_SHOW_PROPERTIES = "MainPID,InvocationID,ActiveState,ControlGroup"
_SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)  # Linux SOL_SOCKET option.
_UCRED_BYTES = struct.calcsize("=iII")


def _read_unit_show() -> str | None:
    try:
        result = subprocess.run(
            ["systemctl", "show", "--no-pager", f"--property={_SHOW_PROPERTIES}", UNIT],
            capture_output=True, text=True, check=False, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 and len(result.stdout) <= 4096 else None


class LinuxAppProofSamplers:
    """Bracket PID1 and /proc reads; reject every ambiguous or changed sample."""

    def __init__(self, *, proc_root: Path = Path("/proc"),
                 show_reader: Callable[[], str | None] = _read_unit_show,
                 peercred_reader: Callable[[socket.socket], bytes] | None = None) -> None:
        self.proc_root = proc_root
        self.show_reader = show_reader
        self.peercred_reader = peercred_reader or (
            lambda connection: connection.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED,
                                                     _UCRED_BYTES))

    def peer_credentials(self, peer_handle: object) -> PeerCredentials:
        """Read kernel credentials from the exact connected socket supplied."""
        if (not isinstance(peer_handle, socket.socket)
                or peer_handle.family != socket.AF_UNIX
                or peer_handle.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM):
            raise TypeError("app_proof_unix_stream_required")
        peer_handle.getpeername()  # An unconnected listener is not a peer.
        raw = self.peercred_reader(peer_handle)
        if type(raw) is not bytes or len(raw) != _UCRED_BYTES:
            raise ValueError("app_proof_peercred_invalid")
        pid, uid, gid = struct.unpack("=iII", raw)
        if not 0 < pid < 2**31:
            raise ValueError("app_proof_peercred_invalid")
        return PeerCredentials(pid, uid, gid)

    def main_process(self) -> ProcessIdentity | None:
        return self._stable_process()

    def peer_process(self, pid: int) -> ProcessIdentity | None:
        return self._stable_process(expected_pid=pid)

    def _unit(self) -> tuple[int, str, str] | None:
        try:
            raw = self.show_reader()
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        if type(raw) is not str or len(raw) > 4096:
            return None
        rows = [line.split("=", 1) for line in raw.splitlines()]
        if (len(rows) != 4 or any(len(row) != 2 for row in rows)
                or {row[0] for row in rows} != set(_SHOW_PROPERTIES.split(","))):
            return None
        values = dict(rows)
        pid_text, invocation = values["MainPID"], values["InvocationID"]
        cgroup = values["ControlGroup"]
        if (values["ActiveState"] != "active" or not pid_text.isascii()
                or not pid_text.isdecimal() or not 0 < int(pid_text) < 2**31
                or _INVOCATION.fullmatch(invocation) is None
                or not cgroup.startswith("/") or cgroup.rsplit("/", 1)[-1] != UNIT
                or "//" in cgroup or any(char.isspace() for char in cgroup)):
            return None
        return int(pid_text), invocation, cgroup

    def _cgroup(self, pid: int) -> str | None:
        try:
            with (self.proc_root / str(pid) / "cgroup").open("rb") as stream:
                raw = stream.read(4097)
        except OSError:
            return None
        if len(raw) > 4096:
            return None
        try:
            lines = raw.decode("ascii").splitlines()
        except UnicodeDecodeError:
            return None
        unified = [line[3:] for line in lines if line.startswith("0::")]
        return unified[0] if len(unified) == 1 else None

    def _stable_process(self, *, expected_pid: int | None = None) -> ProcessIdentity | None:
        if expected_pid is not None and (type(expected_pid) is not int
                                         or not 0 < expected_pid < 2**31):
            return None
        first = self._unit()
        if first is None or (expected_pid is not None and first[0] != expected_pid):
            return None
        pid, invocation, cgroup = first
        ticks = read_proc_start_ticks(self.proc_root, pid)
        if ticks is None or self._cgroup(pid) != cgroup:
            return None
        if self._unit() != first:
            return None
        if (read_proc_start_ticks(self.proc_root, pid) != ticks
                or self._cgroup(pid) != cgroup):
            return None
        return ProcessIdentity(pid=pid, start_ticks=ticks, invocation_id=invocation,
                               cgroup_unit=UNIT)
