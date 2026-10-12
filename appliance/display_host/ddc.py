"""The Pi's one DDC/CI channel per HDMI port (roadmap 1c; run ledger .claude/runs/display-1c.md, slice
P1; the picture split, decision 0022). The only code that runs `ddcutil`: power (VCP D6,
power_methods.DdcCiAdapter) and picture (the picture list and the display's capabilities string,
picture_list.PictureListAsker, and picture.DdcPictureAdapter) all go through it.

Rules this module owns (one home each):
- **One command at a time per port, power first:** a port's commands are serialised by that port's
  `PortLock`, so a power write and a picture write never meet on one i2c bus; a waiting POWER
  command always takes the port before a waiting PICTURE command; two ports never wait on each
  other. Every call names its `urgency` (a required keyword).
- **Power first for a whole power operation:** a power operation is several commands (a read, the
  write, its read-back), each its own hold; after a POWER command releases the port, no PICTURE
  command takes it for POWER_GRACE_SECONDS, so the operation's next command never queues behind a
  picture hold.
- **A picture command's hold is bounded:** its tool run is cut at PICTURE_HOLD_SECONDS (here, in
  `_command`, the one home of the cap; a `setvcp` and `capabilities` at picture's turn are cut too),
  so a power operation waits at most one picture command, however many commands it has.
- **An answer's value** (`answer_value`): a level's current, a list setting's byte.
- **The terse format** (`parse_terse`).
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
_TERSE_CONTINUOUS: Final = re.compile(r"VCP ([0-9A-Fa-f]{2}) C (\d+) (\d+)", re.IGNORECASE)
_TERSE_SIMPLE: Final = re.compile(r"VCP ([0-9A-Fa-f]{2}) SNC x([0-9A-Fa-f]{2})", re.IGNORECASE)

PICTURE_HOLD_SECONDS: Final = 1.5   # a picture command's longest hold of a port: power waits at most this
POWER_GRACE_SECONDS: Final = 0.2    # after a POWER command, the port waits this long for the operation's next one


class Urgency(IntEnum):
    """Whose turn on a port: a POWER command always goes before a waiting PICTURE command, so a
    picture ask or write never costs a power look its time (decision 0022, "Picture never costs
    power")."""
    POWER = 0
    PICTURE = 1


class PortLock:
    """One port's lock with two queues: when it is released, a waiting POWER command takes it before
    any waiting PICTURE command; PICTURE waiters take it in arrival order only while no POWER
    command waits and POWER_GRACE_SECONDS have passed since a POWER holder released it (so a power
    operation's commands follow each other). Thread-safe; every acquire returns within its
    timeout."""

    def __init__(self, grace: float = POWER_GRACE_SECONDS) -> None:
        self._turn = threading.Condition()
        self._held: Urgency | None = None
        self._grace = grace
        self._power_until = 0.0                          # time.monotonic() until which PICTURE waits
        self._waiting: dict[Urgency, list[object]] = {urgency: [] for urgency in Urgency}

    def acquire(self, urgency: Urgency, timeout: float) -> bool:
        """Whether the port was taken within `timeout` seconds (False: not taken; nothing to release)."""
        deadline = time.monotonic() + timeout
        ticket = object()
        with self._turn:
            queue = self._waiting[urgency]
            queue.append(ticket)
            try:
                while self._held is not None or self._next() is not ticket:
                    now = time.monotonic()
                    left = deadline - now
                    if left <= 0:
                        return False
                    grace = self._power_until - now
                    self._turn.wait(min(left, grace) if grace > 0 else left)
                self._held = urgency
                return True
            finally:
                queue.remove(ticket)
                self._turn.notify_all()        # a waiter that gave up may have been the head

    def release(self) -> None:
        """Hand the port to the next waiter by the rule above; RuntimeError when not held."""
        with self._turn:
            if self._held is None:
                raise RuntimeError("port_not_held")
            if self._held is Urgency.POWER:
                self._power_until = time.monotonic() + self._grace
            self._held = None
            self._turn.notify_all()

    def _next(self) -> object | None:
        """The waiter whose turn it is: the first POWER waiter; else the first PICTURE waiter once
        the grace after a POWER release has passed."""
        if self._waiting[Urgency.POWER]:
            return self._waiting[Urgency.POWER][0]
        if self._waiting[Urgency.PICTURE] and time.monotonic() >= self._power_until:
            return self._waiting[Urgency.PICTURE][0]
        return None


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
    value byte."""
    value: int

    def __post_init__(self) -> None:
        """ValueError("vcp_byte") unless 0 <= value <= 255."""
        if not (type(self.value) is int and 0 <= self.value <= 255):
            raise ValueError("vcp_byte")


VcpAnswer = VcpReading | VcpByte


def answer_value(answer: VcpAnswer | None) -> int | None:
    """An answer's value: a level's current, a list setting's byte; None for no answer. Pure."""
    if answer is None:
        return None
    return answer.current if isinstance(answer, VcpReading) else answer.value


def parse_terse(stdout: str) -> dict[str, VcpAnswer]:
    """Each code's answer in `getvcp <codes> --terse` output: "VCP 10 C 60 100" a VcpReading,
    "VCP 14 SNC x05" a VcpByte; codes upper-case; any other line (ERR, CNC, T, noise) is no answer.
    Pure; the one home of the terse format."""
    answers: dict[str, VcpAnswer] = {}
    for line in stdout.splitlines():
        if (continuous := _TERSE_CONTINUOUS.fullmatch(line.strip())) is not None:
            code, current, maximum = continuous.groups()
            try:
                answers[code.upper()] = VcpReading(int(current), int(maximum))
            except ValueError:
                continue
        elif (simple := _TERSE_SIMPLE.fullmatch(line.strip())) is not None:
            answers[simple.group(1).upper()] = VcpByte(int(simple.group(2), 16))
    return answers


class DdcChannel:
    """Every Output's DDC/CI, one command at a time per port. Thread-safe."""

    def __init__(self, run: Runner, *, drm_root: Path = Path("/sys/class/drm"),
                 cache_dir: Path = DDC_CACHE) -> None:
        self._run = run
        self._drm_root = drm_root
        self._cache_dir = cache_dir
        self._guard = threading.Lock()
        self._ports: dict[str, PortLock] = {}           # one lock per port, made on first use

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

    def get(self, output_id: str, code: str, *, urgency: Urgency, timeout: float) -> str | None:
        """`getvcp <code> --terse`'s stdout; None when it did not answer (no bus, a non-zero exit, a
        hang past `timeout`)."""
        done = self._command(output_id, ["getvcp", code, "--terse"], urgency, timeout)
        return done.stdout if done is not None and done.returncode == 0 else None

    def get_continuous(self, output_id: str, code: str, *, urgency: Urgency, timeout: float) -> VcpReading | None:
        """A continuous code's reading ("VCP 10 C 60 100"); None when it did not answer or the
        answer is not a continuous reading."""
        output = self.get(output_id, code, urgency=urgency, timeout=timeout)
        answer = parse_terse(output).get(code.upper()) if output is not None else None
        return answer if isinstance(answer, VcpReading) else None

    def set(self, output_id: str, code: str, value: int, *, urgency: Urgency, timeout: float) -> bool:
        """`setvcp <code> <value>`; whether the tool exited 0. Never the judge of a write: callers
        read back."""
        done = self._command(output_id, ["setvcp", code, str(value)], urgency, timeout)
        return done is not None and done.returncode == 0

    def read_many(self, output_id: str, codes: Sequence[str], *, urgency: Urgency,
                  timeout: float) -> dict[str, VcpAnswer] | None:
        """One `getvcp <codes...> --terse` as one hold of the port at `urgency` (a PICTURE hold runs
        at most PICTURE_HOLD_SECONDS): the codes it read (`parse_terse`), whatever the tool's exit
        status once it finished; None when it did not finish in time or the Output has no bus."""
        done = self._command(output_id, ["getvcp", *codes, "--terse"], urgency, timeout)
        return parse_terse(done.stdout) if done is not None else None

    def capabilities(self, output_id: str, *, urgency: Urgency, timeout: float) -> str | None:
        """`capabilities`'s stdout as one hold of the port at `urgency`; None when it did not answer
        (no bus, a non-zero exit, a run past `timeout` or, for PICTURE, past PICTURE_HOLD_SECONDS)."""
        done = self._command(output_id, ["capabilities"], urgency, timeout)
        return done.stdout if done is not None and done.returncode == 0 else None

    def _port(self, output_id: str) -> PortLock:
        with self._guard:
            return self._ports.setdefault(output_id, PortLock())

    def _command(self, output_id: str, arguments: Sequence[str], urgency: Urgency,
                 timeout: float) -> CompletedProcess[str] | None:
        """One ddcutil command on the Output's bus, alone on its port at `urgency` and within
        `timeout` (the wait for the port included); a PICTURE command's run is cut at
        PICTURE_HOLD_SECONDS. None when it could not run in time."""
        deadline = time.monotonic() + timeout
        bus = self.bus(output_id)
        if bus is None or timeout <= 0:
            return None
        port = self._port(output_id)
        if not port.acquire(urgency, timeout):
            return None
        try:
            try:
                self._cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass                                    # ddcutil works without its cache
            run_for = deadline - time.monotonic()
            if urgency is Urgency.PICTURE:
                run_for = min(run_for, PICTURE_HOLD_SECONDS)
            return _ask(self._run, [DDCUTIL, "--bus", str(bus), "--noconfig", *arguments], run_for)
        finally:
            port.release()


__all__ = ["PICTURE_HOLD_SECONDS", "POWER_GRACE_SECONDS", "DdcChannel", "PortLock", "Urgency", "VcpAnswer", "VcpByte",
           "VcpReading", "answer_value", "parse_terse"]
