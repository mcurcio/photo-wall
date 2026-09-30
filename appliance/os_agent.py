"""Base-owned observational facts, independent of the replaceable Player app.

This module does not issue or accept OS commands. A serial and boot offer only
correlate claims on the current trusted LAN; they are not device authentication.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import re
import secrets
import subprocess
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from appliance.boot_offer import BOOT_HANDOFF, read_handoff
from appliance.bootstrap import read_pi_serial
from appliance.central_post import UnsupportedRoute, post_json
from contracts.strict_json import loads_object
from uplink.causes import UplinkError
from uplink.files import write_atomically
from uplink.finder import Found, find_central
from uplink.resolver import (
    KERNEL_COMMAND_LINE,
    Unconfigured,
    read_kernel_command_line,
    resolve_central,
)
from uplink.transport import HttpTransport, Transport
from uplink.trust import Trust

MAX_STATE_BYTES = 512
MAX_RECEIPT_BYTES = 256
MAX_SEQUENCE = 2**31 - 1
OBSERVATION_STATE = Path("/run/photo-wall/os-observation.json")
PHASE_STATE = Path("/run/photo-wall/provision-phase.json")
KERNEL_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
CHECK_IN_PATH = "/v1/appliance/check-ins"
CHECK_IN_SECONDS = 15.0
CHECK_IN_PERIOD = 10.0
LOG = logging.getLogger("photo_wall.appliance.os_agent")
PHASES = frozenset({"base_ready", "fetching_app", "verifying_app", "installing_app",
                    "starting_app", "player_unit_started", "retry_wait"})
_BOOT_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_SERIAL = re.compile(r"[0-9a-fA-F]{1,128}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FAULT = re.compile(r"[a-z0-9_]{1,64}")


class OsObservationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CheckInReceipt:
    accepted: bool
    next_sequence: int | None = None

    @classmethod
    def parse(cls, body: bytes) -> CheckInReceipt:
        """Accept only the receipt vocabulary; a T0 response cannot carry a command."""
        value = loads_object(body, max_bytes=MAX_RECEIPT_BYTES)
        if value == {"accepted": True}:
            return cls(True)
        if (value is None or set(value) != {"accepted", "reason", "next_sequence"}
                or value.get("accepted") is not False
                or value.get("reason") != "stale_or_duplicate"):
            raise OsObservationError("check_in_receipt_invalid")
        next_sequence = value["next_sequence"]
        if (next_sequence is not None
                and (type(next_sequence) is not int or not 1 <= next_sequence <= MAX_SEQUENCE)):
            raise OsObservationError("check_in_receipt_invalid")
        return cls(False, next_sequence)


class ObservationSequence:
    """Single local writer sequence, retained across agent restarts within one boot."""

    def __init__(self, path: Path = OBSERVATION_STATE) -> None:
        self.path = path

    def next(self, kernel_boot_id: str) -> int:
        if _BOOT_ID.fullmatch(kernel_boot_id) is None:
            raise OsObservationError("boot_id_invalid")
        self.path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        with (self.path.parent / f".{self.path.name}.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if self.path.exists():
                    try:
                        raw = self.path.read_bytes()
                        if len(raw) > MAX_STATE_BYTES:
                            raise ValueError("state too large")
                        value = json.loads(raw)
                    except (OSError, ValueError) as exc:
                        raise OsObservationError("observation_state_invalid") from exc
                    if (not isinstance(value, dict) or type(value.get("sequence")) is not int
                            or not 0 <= value["sequence"] <= MAX_SEQUENCE):
                        raise OsObservationError("observation_state_invalid")
                    if value.get("kernel_boot_id") == kernel_boot_id:
                        sequence = value["sequence"] + 1
                    else:
                        sequence = 1
                else:
                    sequence = 1
                if sequence > MAX_SEQUENCE:
                    raise OsObservationError("observation_sequence_exhausted")
                write_atomically(self.path, json.dumps({"kernel_boot_id": kernel_boot_id,
                                                        "sequence": sequence}).encode(), mode=0o600)
                return sequence
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def advance_to(self, kernel_boot_id: str, next_sequence: int) -> None:
        """Persist a stale-receipt high-water hint before retrying the observation.

        This only repairs T0 telemetry. The unauthenticated hint is never an authority or
        command fence. A corrupt journal can be replaced after an explicit sequence-zero probe.
        """
        if (_BOOT_ID.fullmatch(kernel_boot_id) is None or type(next_sequence) is not int
                or not 1 <= next_sequence <= MAX_SEQUENCE):
            raise OsObservationError("observation_recovery_invalid")
        self.path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        with (self.path.parent / f".{self.path.name}.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                current = 0
                if self.path.exists():
                    try:
                        raw = self.path.read_bytes()
                        value = loads_object(raw, max_bytes=MAX_STATE_BYTES)
                    except OSError as exc:
                        raise OsObservationError("observation_state_invalid") from exc
                    if value is not None and value.get("kernel_boot_id") == kernel_boot_id:
                        old = value.get("sequence")
                        if type(old) is int and 0 <= old <= MAX_SEQUENCE:
                            current = old
                sequence = max(current, next_sequence - 1)
                write_atomically(self.path, json.dumps({"kernel_boot_id": kernel_boot_id,
                                                        "sequence": sequence}).encode(), mode=0o600)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def observation(*, serial: str, kernel_boot_id: str, agent_incarnation: str,
                sequence: int, phase: str, sampled_boottime_ms: int,
                handoff_path: Path = BOOT_HANDOFF, fault_code: str | None = None,
                attempted_app_sha256: str | None = None,
                recovery_probe: bool = False) -> dict[str, object]:
    """Build a bounded T0 claim for Central's receipt-only check-in endpoint."""
    if (_SERIAL.fullmatch(serial) is None or _BOOT_ID.fullmatch(kernel_boot_id) is None
            or not re.fullmatch(r"[0-9a-f]{32}", agent_incarnation)
            or type(sequence) is not int
            or not (sequence == 0 if recovery_probe else 1 <= sequence <= MAX_SEQUENCE)
            or phase not in PHASES or type(sampled_boottime_ms) is not int
            or sampled_boottime_ms < 0):
        raise OsObservationError("observation_invalid")
    if fault_code is not None and _FAULT.fullmatch(fault_code) is None:
        raise OsObservationError("observation_fault_invalid")
    if attempted_app_sha256 is not None and _SHA256.fullmatch(attempted_app_sha256) is None:
        raise OsObservationError("observation_digest_invalid")
    handoff = read_handoff(handoff_path)
    if handoff is not None and handoff["kernel_boot_id"] != kernel_boot_id:
        handoff = None  # prior boot's file must not join this current OS claim
    return {"schema": 1, "kind": "pi", "serial": serial,
            "kernel_boot_id": kernel_boot_id,
            "boot_nonce": handoff["boot_nonce"] if handoff else None,
            "offer_id": handoff["offer_id"] if handoff else None,
            "base_digest": handoff["base_digest"] if handoff else None,
            "agent_incarnation": agent_incarnation,
            "observation_sequence": sequence,
            "phase": phase, "fault_code": fault_code,
            "attempted_app_sha256": attempted_app_sha256,
            "sampled_boottime_ms": sampled_boottime_ms}


def new_incarnation() -> str:
    return secrets.token_hex(16)


def write_phase(phase: str, digest: str | None, fault: str | None, *,
                path: Path = PHASE_STATE, boot_id_path: Path = KERNEL_BOOT_ID) -> None:
    """Provisioner's single-writer volatile status; a report is not proof of install or output."""
    if phase not in PHASES or (digest is not None and _SHA256.fullmatch(digest) is None) or (
            fault is not None and _FAULT.fullmatch(fault) is None):
        raise OsObservationError("phase_invalid")
    try:
        boot_id = boot_id_path.read_text().strip()
        if _BOOT_ID.fullmatch(boot_id) is None:
            raise OsObservationError("boot_id_invalid")
        value = {"kernel_boot_id": boot_id, "phase": phase, "digest": digest,
                 "fault": fault}
        write_atomically(path, json.dumps(value, sort_keys=True).encode(), mode=0o600)
    except OSError as exc:
        LOG.warning("os_agent: phase journal unavailable: %s", exc)


def read_phase(kernel_boot_id: str, *, path: Path = PHASE_STATE) -> tuple[str, str | None, str | None]:
    try:
        raw = path.read_bytes()
        value = loads_object(raw, max_bytes=MAX_STATE_BYTES)
    except OSError:
        return "base_ready", None, None
    if (value is None or value.get("kernel_boot_id") != kernel_boot_id
            or value.get("phase") not in PHASES):
        return "retry_wait", None, "phase_state_invalid"
    digest, fault = value.get("digest"), value.get("fault")
    if (digest is not None and (not isinstance(digest, str)
                                or _SHA256.fullmatch(digest) is None)
            or fault is not None and (not isinstance(fault, str)
                                      or _FAULT.fullmatch(fault) is None)):
        return "retry_wait", None, "phase_state_invalid"
    return value["phase"], digest, fault


def player_unit_active() -> bool:
    """Direct systemd observation. A prior unit start is not current process evidence."""
    try:
        result = subprocess.run(["systemctl", "is-active", "--quiet",
                                 "photo-wall-player.service"], check=False,
                                timeout=3.0)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


class OsAgent:
    """T0 periodic observation only. The app never owns this transport or process."""

    def __init__(self, *, serial: str, kernel_boot_id: str,
                 find: Callable[[], Awaitable[Found]], transport: Transport,
                 sequence: ObservationSequence = ObservationSequence(),
                 handoff_path: Path = BOOT_HANDOFF,
                 phase_path: Path = PHASE_STATE,
                 boottime: Callable[[], float] = time.monotonic,
                 unit_active: Callable[[], bool] = player_unit_active) -> None:
        if _SERIAL.fullmatch(serial) is None or _BOOT_ID.fullmatch(kernel_boot_id) is None:
            raise OsObservationError("os_agent_identity_invalid")
        self.serial, self.kernel_boot_id = serial, kernel_boot_id
        self.find, self.transport, self.sequence = find, transport, sequence
        self.handoff_path, self.phase_path = handoff_path, phase_path
        self.boottime = boottime
        self.unit_active = unit_active
        self.incarnation = new_incarnation()

    async def report_once(self) -> CheckInReceipt:
        phase, digest, fault = read_phase(self.kernel_boot_id, path=self.phase_path)
        if phase == "player_unit_started" and not self.unit_active():
            phase, fault = "retry_wait", "player_unit_inactive"
        recovery_probe = False
        try:
            sequence = self.sequence.next(self.kernel_boot_id)
        except OsObservationError as exc:
            if str(exc) != "observation_state_invalid":
                raise
            sequence, recovery_probe = 0, True
            phase, digest, fault = "retry_wait", None, "observation_state_invalid"
        value = observation(serial=self.serial, kernel_boot_id=self.kernel_boot_id,
                            agent_incarnation=self.incarnation, sequence=sequence,
                            phase=phase, sampled_boottime_ms=int(self.boottime() * 1000),
                            handoff_path=self.handoff_path, fault_code=fault,
                            attempted_app_sha256=digest, recovery_probe=recovery_probe)
        found = await self.find()
        body = post_json(found.central, CHECK_IN_PATH, value,
                         transport=self.transport, seconds=CHECK_IN_SECONDS,
                         max_reply=MAX_RECEIPT_BYTES)
        receipt = CheckInReceipt.parse(body)
        if not receipt.accepted and receipt.next_sequence is not None:
            self.sequence.advance_to(self.kernel_boot_id, receipt.next_sequence)
        return receipt

    async def run_forever(self, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        while True:
            try:
                receipt = await self.report_once()
                if not receipt.accepted and receipt.next_sequence is None:
                    LOG.error("os_agent: observation sequence exhausted; waiting for reboot or repair")
            except (UplinkError, UnsupportedRoute, OsObservationError, OSError) as exc:
                LOG.warning("os_agent: observation unavailable: %s", exc)
            await sleep(CHECK_IN_PERIOD)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Photo Wall base OS observation agent")
    parser.add_argument("--cmdline", type=Path, default=KERNEL_COMMAND_LINE)
    parser.add_argument("--boot-id", type=Path, default=KERNEL_BOOT_ID)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    serial = read_pi_serial()
    if serial is None:
        raise SystemExit("os_agent: Pi serial unavailable")
    kernel_boot_id = args.boot_id.read_text().strip()
    resolution = resolve_central(read_kernel_command_line(args.cmdline))
    transport = HttpTransport(trust=Trust.public())
    discovery = None
    if isinstance(resolution, Unconfigured):
        from player.mdns_discovery import MdnsCentralDiscovery

        discovery = MdnsCentralDiscovery(timeout=3.0)
    async def find() -> Found:
        return await find_central(resolution, transport=transport, discovery=discovery)
    asyncio.run(OsAgent(serial=serial, kernel_boot_id=kernel_boot_id,
                        find=find, transport=transport).run_forever())


if __name__ == "__main__":
    main()
