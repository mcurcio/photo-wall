"""The Pi's one DDC/CI channel per HDMI port (roadmap 1c; run ledger .claude/runs/display-1c.md, slice
P1). The only code that runs `ddcutil`: power (VCP D6, power_methods.DdcCiAdapter) and picture
(VCP 0x10 brightness, 0x12 contrast, picture.DdcPictureAdapter) both go through it.

Rules this module owns (one home each):
- **One command at a time per port:** a port's commands are serialised by that port's lock, so
  a power write and a brightness write never meet on one i2c bus; two ports never wait on each
  other.
- **The bus of an Output:** the i2c number of `/sys/class/drm/<card>-<output>/ddc` (HDMI-A-1 -> 13,
  HDMI-A-2 -> 14 on a Pi 5). No bus: every call answers None/False at once.
- **Bounded:** every call returns within its `timeout`, the lock wait included (a call whose
  deadline passes while it waits runs nothing and answers None/False).
- `ddcutil --bus <n> --noconfig` with XDG_CACHE_HOME on the unit's private /tmp, through the
  injected `Runner` (tests pass a fake that plays recorded tool output).

Newest-wins (a slider's writes never queue) is the picture controller's rule, above this.
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from subprocess import CompletedProcess
from typing import Final

from .power_methods import DDC_CACHE, DDCUTIL, Runner, _ask, _connectors

_I2C_BUS: Final = re.compile(r"i2c-(\d+)")

# The picture split (decision 0022; slice S1 implements, S1 also gives `get`, `get_continuous` and
# `set` a required keyword `urgency: Urgency`, so every caller states whose turn it is).
PICTURE_HOLD_SECONDS: Final = 1.5   # a picture command's longest hold of a port: power waits at most this


class Urgency(IntEnum):
    """Whose turn on a port: a POWER command always goes before a waiting PICTURE command, so a
    picture ask or write never costs a power look its time (decision 0022, "Picture never costs
    power")."""
    POWER = 0
    PICTURE = 1


class PortLock:
    """One port's lock with two queues: when it is released, a waiting POWER command takes it before
    any waiting PICTURE command; PICTURE waiters take it in arrival order only while no POWER
    command waits. Thread-safe; every acquire returns within its timeout (S1)."""

    def __init__(self) -> None:
        raise NotImplementedError("picture split S1")

    def acquire(self, urgency: Urgency, timeout: float) -> bool:
        """Whether the port was taken within `timeout` seconds (False: not taken; nothing to release)."""
        raise NotImplementedError("picture split S1")

    def release(self) -> None:
        """Hand the port to the next waiter by the rule above; RuntimeError when not held."""
        raise NotImplementedError("picture split S1")


@dataclass(frozen=True)
class VcpReading:
    """One `getvcp <code> --terse` answer for a continuous code: its current and maximum values."""
    current: int
    maximum: int

    def __post_init__(self) -> None:
        """ValueError("vcp_reading") unless 0 <= current <= maximum and maximum >= 1."""
        if not (type(self.current) is int and type(self.maximum) is int and 0 <= self.current <= self.maximum
                and self.maximum >= 1):
            raise ValueError("vcp_reading")


@dataclass(frozen=True)
class VcpByte:
    """One `getvcp <code> --terse` answer for a simple non-continuous code ("VCP 14 SNC x05"): its
    value byte (S1)."""
    value: int

    def __post_init__(self) -> None:
        """ValueError("vcp_byte") unless 0 <= value <= 255."""
        raise NotImplementedError("picture split S1")


VcpAnswer = VcpReading | VcpByte


def parse_terse(stdout: str) -> dict[str, VcpAnswer]:
    """Each code's answer in `getvcp <codes> --terse` output: "VCP 10 C 60 100" a VcpReading,
    "VCP 14 SNC x05" a VcpByte; codes upper-case; any other line (ERR, CNC, T, noise) is no answer.
    Pure; the one home of the terse format (S1)."""
    raise NotImplementedError("picture split S1")


class DdcChannel:
    """Every Output's DDC/CI, one command at a time per port. Thread-safe."""

    def __init__(self, run: Runner, *, drm_root: Path = Path("/sys/class/drm"),
                 cache_dir: Path = DDC_CACHE) -> None:
        self._run = run
        self._drm_root = drm_root
        self._cache_dir = cache_dir
        self._guard = threading.Lock()
        self._ports: dict[str, threading.Lock] = {}     # one lock per port, made on first use

    def bus(self, output_id: str) -> int | None:
        """The i2c bus the connector's `ddc` link names; None without one."""
        for connector in _connectors(self._drm_root, output_id):
            try:
                target = os.readlink(connector / "ddc")
            except OSError:
                continue
            if (bus := _I2C_BUS.fullmatch(Path(target).name)) is not None:
                return int(bus.group(1))
        return None

    def get(self, output_id: str, code: str, *, timeout: float) -> str | None:
        """`getvcp <code> --terse`'s stdout; None when it did not answer (no bus, a non-zero exit, a
        hang past `timeout`)."""
        done = self._command(output_id, ["getvcp", code, "--terse"], timeout)
        return done.stdout if done is not None and done.returncode == 0 else None

    def get_continuous(self, output_id: str, code: str, *, timeout: float) -> VcpReading | None:
        """A continuous code's reading ("VCP 10 C 60 100"); None when it did not answer or the
        answer is not a continuous reading."""
        output = self.get(output_id, code, timeout=timeout)
        pattern = rf"^VCP {re.escape(code)} C (\d+) (\d+)\s*$"
        found = re.search(pattern, output, re.MULTILINE | re.IGNORECASE) if output is not None else None
        if found is None:
            return None
        try:
            return VcpReading(int(found.group(1)), int(found.group(2)))
        except ValueError:
            return None

    def set(self, output_id: str, code: str, value: int, *, timeout: float) -> bool:
        """`setvcp <code> <value>`; whether the tool exited 0. Never the judge of a write: callers
        read back."""
        done = self._command(output_id, ["setvcp", code, str(value)], timeout)
        return done is not None and done.returncode == 0

    def read_many(self, output_id: str, codes: Sequence[str], *, urgency: Urgency,
                  timeout: float) -> dict[str, VcpAnswer] | None:
        """One `getvcp <codes...> --terse` as one hold of the port at `urgency` (a PICTURE hold runs
        at most PICTURE_HOLD_SECONDS): the codes it read (`parse_terse`), whatever the tool's exit
        status once it finished; None when it did not finish in time or the Output has no bus (S1)."""
        raise NotImplementedError("picture split S1")

    def capabilities(self, output_id: str, *, urgency: Urgency, timeout: float) -> str | None:
        """`capabilities`'s stdout as one hold of the port; None when it did not answer (S3)."""
        raise NotImplementedError("picture split S3")

    def _port(self, output_id: str) -> threading.Lock:
        with self._guard:
            return self._ports.setdefault(output_id, threading.Lock())

    def _command(self, output_id: str, arguments: Sequence[str], timeout: float) -> CompletedProcess[str] | None:
        """One ddcutil command on the Output's bus, alone on its port and within `timeout` (the
        wait for the port included); None when it could not run in time."""
        deadline = time.monotonic() + timeout
        bus = self.bus(output_id)
        if bus is None or timeout <= 0:
            return None
        port = self._port(output_id)
        if not port.acquire(timeout=timeout):
            return None
        try:
            try:
                self._cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass                                    # ddcutil works without its cache
            return _ask(self._run, [DDCUTIL, "--bus", str(bus), "--noconfig", *arguments],
                        deadline - time.monotonic())
        finally:
            port.release()


__all__ = ["PICTURE_HOLD_SECONDS", "DdcChannel", "PortLock", "Urgency", "VcpAnswer", "VcpByte", "VcpReading",
           "parse_terse"]
