"""The Pi's picture controller: carries out each picture document (the display's own settings and
Photo Wall's, each on its own) and reports back (decision 0022; the picture split, slices S1 and
S3; from roadmap 1c's controller).

Pure core, effects at the edges. `operations_for`, `judged`, `write_order`
and `brightness_line` are pure. `PictureController` applies them through six ports: `PictureDdc`
(one code's read and write over the port's DDC/CI channel, picture's turn), `PictureAsk` (which
settings on the picture list the display offers, and their values read again;
picture_list.PictureListAsker), `OfferedCache` (each display's last list, kept across controller
restarts; picture_list.FileOfferedCache), `PictureShell`
(the shell's `picture` control op; weston.WestonBackend), `PictureReports` (the picture report's
state key; bus.BusPictureSink) and `AppliedPictures` (which picture change each display last had,
kept across controller restarts; `FileAppliedPictures`).

Rules this module owns (one home each):
- **Two groups, never coupled:** the document's `hardware` is written over DDC/CI and only there;
  its `software` becomes the Player's operations and only that. No rule reads one to decide the
  other.
- **Unset writes nothing:** a code absent from `hardware` is never written, nor is a code the
  display did not offer. The Pi never moves a setting on its own: a refused write is reported
  (`not-taken`, `no-answer`).
- **Ask once per display:** at an appearance with an identity whose list it does not hold, the
  worker takes the list kept for it (`OfferedCache`) or else asks it (`PictureAsk.ask`), again
  every ASK_AGAIN_SECONDS while it is unknown; the list is held per identity for the controller's
  life (a blip with the same identity does not re-ask, but its values are read again at once), and
  kept whenever it changes.
- **Read again:** every REREAD_SECONDS the worker reads the held list's values again
  (`PictureAsk.read_again`, picture's turn, batched) and reports them as each setting's current,
  so a value changed on the display's own buttons shows (Central says so). Every read the worker
  makes (before a write, its read-back) is a current too. Reading never writes: the Pi never puts
  a value back on its own.
- **Write only what changed:** when the document's picture_change rose, only the hardware codes
  whose value differs from the change last carried out on the same display are written; on another
  display (or with no record of the last), every code. A change that moves only Photo Wall's group
  touches no DDC/CI at all. Codes are written in `write_order` (list settings first, so a preset is
  chosen before the gains it moves), once the display is connected and its list is known. Each
  code is read first (a display that already has the value is not written), a value its range does
  not hold (`HardwareRange.holds`) is not written (`not-taken`), a list setting's value is its byte
  (`setvcp 14 5`), and the read-back judges a write (`judged`). The report's results keep the
  latest result of each code the change did not touch. A picture change already carried out on
  the same display is never written again, even by a restarted controller (a Weston restart
  restarts it): `AppliedPictures` remembers it and its hardware.
- **A change is answered while its hardware waits:** a change whose codes cannot be written yet
  (the display is not connected, or its list is not known) is still answered (`for_change`, with
  the kept results of the codes it did not touch), so Photo Wall's group is never held by the
  display's; the codes are written once the display and its list are there.
- **At most two writes a second per display** (WRITE_GAP_SECONDS): some displays save each write
  to memory that wears, so the worker waits out the gap before a write.
- **Newest wins:** each Output has one hardware worker; a document that arrives while it writes
  replaces the one waiting, so a dragged slider's writes never queue.
- **The shell never waits on the display:** each Output's operations and line go to the shell from
  their own worker, at once on each new change, never queued behind an ask, a re-read or a write.
- **Operations from the software group** (`operations_for`): white balance from colour
  temperature (contracts/kelvin.py `kelvin_white`, in linear light, normalised so KELVIN_NEUTRAL is
  exactly (1, 1, 1) and the largest is 1); light factor = brightness / SOFTWARE_PERCENT_NEUTRAL; contrast
  slope = contrast / SOFTWARE_PERCENT_NEUTRAL; gamma exponent = gamma_tenths / GAMMA_TENTHS_NEUTRAL. Sent to the shell
  at once on each new change, by the shell worker.
- **Re-sent at start:** the controller restarts with Weston, and sends every Output's operations
  to the shell when it starts and whenever the shell reports an Output (re)created.
- **Presented:** `presented_change` is the newest picture change the shell's `presented` events
  echoed for the Output (frame protocol v4 `tag_picture`), or the new change when its operations
  equal the ones last presented.
- **The base page's line** (`brightness_line`): the display's own brightness and Photo Wall's,
  each named; it rides the shell's `picture` op beside the operations, and the shell forwards it to
  the overlay client (frame protocol v4 `brightness`).
- **The report** is put whenever it changes (identity, offered, for_change, results,
  presented_change).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Final, Protocol

from contracts.kelvin import kelvin_white
from contracts.node_frame import PictureOperations
from contracts.node_output import DisplayIdentity
from contracts.node_picture import (
    GAMMA_TENTHS_NEUTRAL,
    PICTURE_LIST,
    SOFTWARE_PERCENT_NEUTRAL,
    HardwareResult,
    HardwareSetting,
    PictureDocument,
    PictureReport,
    RangeKind,
    SoftwarePicture,
    VcpCode,
    WriteResult,
)

from .ddc import DdcChannel, Urgency, VcpAnswer, answer_value, parse_terse
from .picture_list import ASK_AGAIN_SECONDS, OfferedCache, PictureAsk, with_current

BRIGHTNESS_CODE: Final = "10"       # VESA MCCS luminance
READ_SECONDS: Final = 5.0           # one VCP read's wait for the port and its run (the run itself is cut at the hold)
WRITE_SECONDS: Final = 10.0         # one VCP write with its read-back, the port waits included (each run is cut at the hold)
WRITE_GAP_SECONDS: Final = 0.5      # the least time between two writes to one display
SHELL_SECONDS: Final = 2.0          # one `picture` control op
TICK_SECONDS: Final = 1.0           # a worker's longest sleep: an ask or a read again falls due within it
REREAD_SECONDS: Final = 60.0        # the held list's values are read again this often (1c's re-read)
_HISTORY: Final = 16                # picture changes remembered per Output for `presented`

log = logging.getLogger(__name__)

def operations_for(output_id: str, picture_change: int, software: SoftwarePicture) -> PictureOperations:
    """The Player's operations for one Output's software group (the module's rule). Pure."""
    return PictureOperations(output_id=output_id, picture_change=picture_change,
                             rgb_multipliers=kelvin_white(software.colour_temperature_kelvin),
                             light_factor=software.brightness_percent / SOFTWARE_PERCENT_NEUTRAL,
                             contrast_slope=software.contrast_percent / SOFTWARE_PERCENT_NEUTRAL,
                             gamma_exponent=software.gamma_tenths / GAMMA_TENTHS_NEUTRAL)


def judged(written: int, after: VcpAnswer | None) -> WriteResult:
    """A write's result by its read-back: TAKEN when the value read equals the value written,
    NOT_TAKEN for another, NO_ANSWER for none. Pure."""
    value = answer_value(after)
    if value is None:
        return WriteResult.NO_ANSWER
    return WriteResult.TAKEN if value == written else WriteResult.NOT_TAKEN


def write_order(hardware: Mapping[VcpCode, int], offered: tuple[HardwareSetting, ...]) -> tuple[VcpCode, ...]:
    """The codes of `hardware` the display offers, in the order they are written: list settings
    first, then levels, each in PICTURE_LIST order (a preset is chosen before the gains it moves).
    A code the display does not offer is never written. Pure."""
    levels = {setting.code: setting.range.kind is RangeKind.LEVEL for setting in offered}
    codes = [code for code in PICTURE_LIST if code in hardware and code in levels]
    return tuple(sorted(codes, key=lambda code: levels[code]))          # stable: list order within each


@dataclass(frozen=True)
class DisplayBrightness:
    """What the base page says of the display's own brightness (VCP 10)."""
    percent: int | None      # the display's last read of 10 as a whole percent of its range; None: unknown
    set_here: bool           # 10 is in the document's hardware
    took: bool               # the latest write for 10 was taken, or nothing needed writing


def brightness_line(software_percent: int, display: DisplayBrightness) -> str:
    """The base page's brightness line: the display's own brightness and Photo Wall's, each named,
    so a software value is never shown as a panel value (U9): "Brightness: display 46 % · Photo Wall
    114 %"; when the display did not take the brightness set in Photo Wall, "the display did not take
    it" in the display's place; when its brightness is not known, "as the display has it". Pure."""
    software = f"Photo Wall {software_percent} %"
    if display.set_here and not display.took:
        return f"Brightness: the display did not take it · {software}"
    if display.percent is not None:
        return f"Brightness: display {display.percent} % · {software}"
    return f"Brightness: as the display has it · {software}"


class PictureDdc(Protocol):
    """One code on one Output, picture's turn. Every call returns within its `timeout` and never
    raises for a display that does not answer."""

    def read(self, output_id: str, code: VcpCode, *, timeout: float) -> VcpAnswer | None: ...

    def write(self, output_id: str, code: VcpCode, value: int, *, timeout: float) -> VcpAnswer | None:
        """`setvcp <code> <value>`, then the read-back (None: no answer)."""
        ...


class PictureShell(Protocol):
    """The shell's `picture` control op (weston.WestonBackend.picture). `brightness` is the base
    page's line (`brightness_line`), forwarded by the shell to the overlay client."""

    def picture(self, operations: PictureOperations, *, timeout: float, brightness: str) -> bool: ...   # the shell took it


class PictureReports(Protocol):
    """Where each Output's picture report goes (bus.BusPictureSink)."""

    def put_picture(self, report: PictureReport) -> None: ...   # never blocks


@dataclass(frozen=True)
class Applied:
    """The picture change last carried out on an Output, the display it was carried out on, and
    its hardware values."""
    picture_change: int
    identity: DisplayIdentity | None
    hardware: Mapping[VcpCode, int]


class AppliedPictures(Protocol):
    """Which picture change each Output's display last had carried out, on which display and with
    which hardware values, kept across controller restarts."""

    def last(self, output_id: str) -> Applied | None: ...   # None: none known

    def record(self, output_id: str, applied: Applied) -> None: ...


class FileAppliedPictures:
    """`AppliedPictures` as one small JSON file per Output in `directory` (the controller's
    preserved runtime directory: it outlives the controller's restarts with Weston, not a reboot).
    A file that is missing or does not read is "none known"; a failed write is logged."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def _path(self, output_id: str) -> Path:
        return self._directory / f"picture-{output_id}.json"

    def last(self, output_id: str) -> Applied | None:
        try:
            value = json.loads(self._path(output_id).read_text())
            change, identity, hardware = value["picture_change"], value["identity"], value["hardware"]
            if type(change) is not int or not all(type(item) is int for item in hardware.values()):
                return None
            return Applied(change, None if identity is None else DisplayIdentity(**identity), dict(hardware))
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    def record(self, output_id: str, applied: Applied) -> None:
        path = self._path(output_id)
        partial = path.with_suffix(".partial")
        identity = applied.identity
        try:
            partial.write_text(json.dumps({"picture_change": applied.picture_change,
                                           "identity": None if identity is None else asdict(identity),
                                           "hardware": dict(applied.hardware)}))
            os.replace(partial, path)
        except OSError:
            log.warning("picture: could not record %s's applied change", output_id, exc_info=True)


class DdcPictureAdapter:
    """`PictureDdc` over the port's DDC/CI channel (ddc.DdcChannel), every call at Urgency.PICTURE."""

    def __init__(self, channel: DdcChannel) -> None:
        self._channel = channel

    def read(self, output_id: str, code: VcpCode, *, timeout: float) -> VcpAnswer | None:
        output = self._channel.get(output_id, code, urgency=Urgency.PICTURE, timeout=timeout)
        return None if output is None else parse_terse(output).get(code.upper())

    def write(self, output_id: str, code: VcpCode, value: int, *, timeout: float) -> VcpAnswer | None:
        deadline = time.monotonic() + timeout
        # The read-back judges the write, never the tool's exit status.
        self._channel.set(output_id, code, value, urgency=Urgency.PICTURE, timeout=timeout)
        return self.read(output_id, code, timeout=deadline - time.monotonic())


_NEVER: Final = object()            # no display carried out yet (an identity may itself be None)


def _values(operations: PictureOperations) -> tuple:
    """What the Player does, without which change it came from: every field but output_id and
    picture_change, so a new operation is compared the moment the contract grows it."""
    return tuple(getattr(operations, item.name) for item in fields(PictureOperations)
                 if item.name not in ("output_id", "picture_change"))


@dataclass
class _Port:
    """One Output as the picture controller knows it; guarded by the controller's lock."""
    connected: bool = False
    identity: DisplayIdentity | None = None
    document: PictureDocument | None = None         # the latest document
    pending: bool = False                           # its hardware is not carried out on this display yet
    carried_out_on: object = _NEVER                 # the display the latest finished change was carried out on
    lists: dict[DisplayIdentity, tuple[HardwareSetting, ...]] = field(default_factory=dict)   # held per identity
    ask_after: dict[DisplayIdentity, float] = field(default_factory=dict)   # an unknown list's next ask
    read_after: dict[DisplayIdentity, float] = field(default_factory=dict)  # a held list's next read again
    for_change: int | None = None
    results: dict[VcpCode, HardwareResult] = field(default_factory=dict)
    brightness_took: bool = True                    # the latest write of 10 was taken, or none was needed
    sent: tuple[PictureOperations, str] | None = None   # what the shell last took
    history: dict[int, tuple] = field(default_factory=dict)   # picture change -> its operations' values
    presented_values: tuple | None = None
    presented_change: int | None = None
    put: PictureReport | None = None                # the report last put
    want_apply: bool = False
    want_send: bool = False                         # re-send the operations and line, even unchanged
    want_line: bool = False                         # send them if either changed
    last_write: float | None = None                 # time.monotonic() at the end of the last write
    worker: threading.Thread | None = None          # the hardware worker: asks, re-reads, writes
    shell_worker: threading.Thread | None = None    # the shell worker: operations and the line

    def offered(self) -> tuple[HardwareSetting, ...] | None:
        """The connected display's list; None while it is not known (or nothing is connected)."""
        if not self.connected or self.identity is None:
            return None
        return self.lists.get(self.identity)


class PictureController:
    """Every Output's picture, from its picture document, through DDC/CI and the shell, to its
    picture report."""

    def __init__(self, ddc: PictureDdc, ask: PictureAsk, offered: OfferedCache, shell: PictureShell,
                 reports: PictureReports, applied: AppliedPictures, clock: Callable[[], float]) -> None:
        """`clock` is the Pi's monotonic seconds for asking and reading again, never compared
        elsewhere."""
        self._ddc, self._ask, self._offered, self._shell, self._reports = ddc, ask, offered, shell, reports
        self._applied, self._clock = applied, clock
        self._ports: dict[str, _Port] = {}
        self._wake = threading.Condition()
        self._started = False
        self._stopping = False

    def start(self) -> None:
        """Start the workers of every known Output and send every known Output's operations to the shell
        (the re-send at start); returns at once."""
        with self._wake:
            self._started = True
            for output_id, port in self._ports.items():
                port.want_send = port.document is not None
                self._worker(output_id, port)
            self._wake.notify_all()

    def stop(self, timeout: float = 5.0) -> None:
        with self._wake:
            self._stopping = True
            workers = [worker for port in self._ports.values() for worker in (port.worker, port.shell_worker)
                       if worker is not None]
            self._wake.notify_all()
        for worker in workers:
            worker.join(timeout)

    def document(self, output_id: str, document: PictureDocument) -> None:
        """A picture document arrived; acts when its picture_change rose (newest wins). Thread-safe,
        never blocks."""
        if document.output_id != output_id:
            raise ValueError("picture_document_output")
        with self._wake:
            port = self._ports.setdefault(output_id, _Port())
            seen = port.document
            if seen is not None and document.picture_change <= seen.picture_change:
                return
            port.document = document         # replaces the one waiting: newest wins
            port.pending = port.want_apply = port.want_line = True
            self._worker(output_id, port)
            self._wake.notify_all()

    def output(self, output_id: str, *, connected: bool, identity: DisplayIdentity | None) -> None:
        """What the compositor and the EDID say about `output_id` now: an identity whose list is
        not held is asked, and another identity carries out the document again. Thread-safe, never
        blocks."""
        with self._wake:
            port = self._ports.setdefault(output_id, _Port())
            appeared = connected and (not port.connected or identity != port.identity)
            port.connected, port.identity = connected, identity
            if appeared and identity is not None and port.lists.get(identity):
                port.read_after[identity] = self._clock()   # it may have changed while away: read it again now
            if connected and port.document is not None and (port.pending or identity != port.carried_out_on):
                port.pending = port.want_apply = True
            self._report(output_id, port)
            self._worker(output_id, port)
            self._wake.notify_all()

    def shell_output(self, output_id: str) -> None:
        """The shell (re)created `output_id` (a Weston restart or hotplug): re-send its operations."""
        with self._wake:
            port = self._ports.setdefault(output_id, _Port())
            port.sent = None
            port.want_send = port.document is not None
            self._worker(output_id, port)
            self._wake.notify_all()

    def presented(self, output_id: str, picture_change: int | None) -> None:
        """The shell's `presented` event echoed this picture change for `output_id`."""
        if picture_change is None:
            return
        with self._wake:
            port = self._ports.get(output_id)
            values = None if port is None else port.history.get(picture_change)
            if port is None or values is None:
                return
            port.presented_values = values
            self._presented(port, picture_change)
            if port.document is not None:
                current = operations_for(output_id, port.document.picture_change, port.document.software)
                if _values(current) == values:
                    self._presented(port, port.document.picture_change)
            self._report(output_id, port)

    # -- the workers: one per Output, never under the lock while they talk to a device --------

    def _worker(self, output_id: str, port: _Port) -> None:
        """Under the lock: the Output's two workers, once the controller started."""
        if not self._started or self._stopping:
            return
        if port.worker is None:
            port.worker = threading.Thread(target=self._run, args=(output_id, port),
                                           name=f"picture-{output_id}", daemon=True)
            port.worker.start()
        if port.shell_worker is None:
            port.shell_worker = threading.Thread(target=self._run_shell, args=(output_id, port),
                                                 name=f"picture-shell-{output_id}", daemon=True)
            port.shell_worker.start()

    def _read_due(self, port: _Port) -> DisplayIdentity | None:
        """Under the lock: the connected display whose held list is due a read again; None when none is."""
        identity = port.identity
        if not port.connected or identity is None or not port.lists.get(identity):
            return None
        after = port.read_after.get(identity)
        return identity if after is not None and self._clock() >= after else None

    def _ask_due(self, port: _Port) -> DisplayIdentity | None:
        """Under the lock: the connected display whose list is due an ask; None when none is."""
        identity = port.identity
        if not port.connected or identity is None or identity in port.lists:
            return None
        after = port.ask_after.get(identity)
        return identity if after is None or self._clock() >= after else None

    def _run_shell(self, output_id: str, port: _Port) -> None:
        """The shell worker: the operations and the line, never behind the display's port."""
        while True:
            with self._wake:
                while not self._stopping and not (port.want_send or port.want_line):
                    self._wake.wait(TICK_SECONDS)
                if self._stopping:
                    return
                force = port.want_send
                port.want_send = port.want_line = False
            try:
                self._send(output_id, port, force=force)
            except Exception:
                log.exception("picture: %s's operations failed", output_id)

    def _refresh(self, port: _Port) -> None:
        """Ask the shell worker to send the operations and line if either changed."""
        with self._wake:
            port.want_line = True
            self._wake.notify_all()

    def _run(self, output_id: str, port: _Port) -> None:
        """The hardware worker: asks, writes and re-reads, one at a time on the Output's port."""
        while True:
            with self._wake:
                while not self._stopping and not (port.want_apply or self._ask_due(port) or self._read_due(port)):
                    self._wake.wait(TICK_SECONDS)
                if self._stopping:
                    return
                apply, asking = port.want_apply, self._ask_due(port)
                reading = self._read_due(port)
                port.want_apply = False
            try:
                if asking is not None and self._asked(output_id, port, asking):
                    apply = True
                if apply:
                    self._carry_out(output_id, port)
                elif asking is not None:
                    self._refresh(port)              # the line follows the list's read of 10
                if reading is not None:
                    self._read_again(output_id, port, reading)
            except Exception:
                log.exception("picture: %s failed", output_id)

    def _asked(self, output_id: str, port: _Port, identity: DisplayIdentity) -> bool:
        """`identity`'s list, the one kept for it or else asked; whether the latest document now
        waits only on it."""
        kept = self._offered.get(identity)
        offered = kept if kept is not None else self._ask.ask(output_id)
        with self._wake:
            if offered is None:
                port.ask_after[identity] = self._clock() + ASK_AGAIN_SECONDS
                return False
            port.lists[identity] = offered
            port.read_after[identity] = self._clock() + REREAD_SECONDS
            self._report(output_id, port)
            pending = port.pending
        if kept is None:
            self._offered.put(identity, offered)
        return pending

    def _read_again(self, output_id: str, port: _Port, identity: DisplayIdentity) -> None:
        """The held list's values read again (the periodic re-read); a read that is not known
        changes nothing."""
        with self._wake:
            held = port.lists.get(identity)
            port.read_after[identity] = self._clock() + REREAD_SECONDS
        if not held:
            return
        fresh = self._ask.read_again(output_id, held)
        with self._wake:
            if fresh is None or port.lists.get(identity) != held or fresh == held:
                return
            port.lists[identity] = fresh
            self._report(output_id, port)
        self._offered.put(identity, fresh)
        self._refresh(port)                          # the line follows the display's brightness

    def _current(self, output_id: str, port: _Port, identity: DisplayIdentity | None, code: VcpCode,
                 answer: VcpAnswer | None) -> None:
        """A read the worker made of `code` on `identity`'s display is that setting's current."""
        with self._wake:
            held = None if identity is None else port.lists.get(identity)
            if identity is None or held is None:
                return
            port.lists[identity] = tuple(with_current(setting, answer) if setting.code == code else setting
                                         for setting in held)
            self._report(output_id, port)

    def _carry_out(self, output_id: str, port: _Port) -> None:
        """The latest document's hardware on the connected display: each offered code whose value
        changed since the change last carried out on this display (every code on another display),
        in `write_order`, read first, written and judged by its read-back. While such codes wait for
        the display or its list, the change is answered (`_answer`) and stays pending."""
        with self._wake:
            document, identity, connected, offered = port.document, port.identity, port.connected, port.offered()
            if document is None or not port.pending:
                return
            earlier = dict(port.results) if port.carried_out_on == identity else {}
        last = self._applied.last(output_id)
        same = last is not None and last.identity == identity
        if same and last.picture_change == document.picture_change:
            self._finish(output_id, port, document, identity, {})   # this display already had it
            return
        carried = last.hardware if same else {}
        to_write = {code: value for code, value in document.hardware.items() if carried.get(code, _NEVER) != value}
        kept = {code: result for code, result in earlier.items()
                if code in document.hardware and code not in to_write}
        if document.hardware and (not connected or (to_write and offered is None)):
            self._answer(output_id, port, document, kept)           # Photo Wall's group does not wait
            return
        settings = {setting.code: setting for setting in offered or ()}
        results = dict(kept)
        for code in write_order(to_write, offered or ()):
            result = self._write(output_id, port, identity, settings[code], to_write[code])
            if result is not None:
                results[code] = result
            else:
                results.pop(code, None)
        self._applied.record(output_id, Applied(document.picture_change, identity, dict(document.hardware)))
        self._finish(output_id, port, document, identity, results)
        if not to_write:
            return
        with self._wake:
            held = None if identity is None else port.lists.get(identity)
        if identity is not None and held is not None:
            self._offered.put(identity, held)        # the currents the writes read

    def _answer(self, output_id: str, port: _Port, document: PictureDocument,
                results: dict[VcpCode, HardwareResult]) -> None:
        """`document`'s change is answered while its hardware waits for the display or its list: the
        report names it with `results` (the codes it did not touch); it stays pending."""
        with self._wake:
            if port.document is not document:
                return
            port.for_change, port.results = document.picture_change, results
            self._report(output_id, port)

    def _write(self, output_id: str, port: _Port, identity: DisplayIdentity | None, setting: HardwareSetting,
               value: int) -> HardwareResult | None:
        """One code: None when the display already has `value`; else the write's result. A write
        waits out WRITE_GAP_SECONDS after the port's last."""
        code = setting.code
        read = self._ddc.read(output_id, code, timeout=READ_SECONDS)
        self._current(output_id, port, identity, code, read)
        before = answer_value(read)
        if before == value:
            return None
        if not setting.range.holds(value):          # past the display's range, or a choice it does not list
            if before is None:
                return HardwareResult(WriteResult.NO_ANSWER, None, None)
            return HardwareResult(WriteResult.NOT_TAKEN, before, before)
        if port.last_write is not None:
            time.sleep(max(0.0, port.last_write + WRITE_GAP_SECONDS - time.monotonic()))
        try:
            after = self._ddc.write(output_id, code, value, timeout=WRITE_SECONDS)
        finally:
            port.last_write = time.monotonic()
        self._current(output_id, port, identity, code, after)
        return HardwareResult(judged(value, after), before, answer_value(after))

    def _finish(self, output_id: str, port: _Port, document: PictureDocument, identity: DisplayIdentity | None,
                results: dict[VcpCode, HardwareResult]) -> None:
        """`document`'s hardware is carried out on `identity`: the report names its change."""
        with self._wake:
            port.for_change, port.results = document.picture_change, results
            port.carried_out_on = identity
            if BRIGHTNESS_CODE in document.hardware:
                written = results.get(BRIGHTNESS_CODE)
                port.brightness_took = written is None or written.result is WriteResult.TAKEN
            else:
                port.brightness_took = True
            if port.document is document:
                port.pending = False
            self._report(output_id, port)
        self._refresh(port)                          # the line after the result

    def _line(self, port: _Port, document: PictureDocument) -> str:
        """Under the lock: the base page's brightness line for `document` on this display."""
        offered = port.offered() or ()
        setting = next((item for item in offered if item.code == BRIGHTNESS_CODE), None)
        percent = None
        if setting is not None and setting.range.kind is RangeKind.LEVEL and setting.current is not None:
            percent = setting.range.percent(setting.current)
        display = DisplayBrightness(percent, BRIGHTNESS_CODE in document.hardware, port.brightness_took)
        return brightness_line(document.software.brightness_percent, display)

    def _send(self, output_id: str, port: _Port, *, force: bool) -> None:
        """The Output's operations and base page line to the shell, when either differs from what
        the shell last took (or `force`: a re-send)."""
        with self._wake:
            document = port.document
            if document is None:
                return
            operations = operations_for(output_id, document.picture_change, document.software)
            line = self._line(port, document)
            if not force and port.sent == (operations, line):
                return
            port.history[document.picture_change] = _values(operations)
            while len(port.history) > _HISTORY:
                del port.history[next(iter(port.history))]
            if port.presented_values == _values(operations):
                self._presented(port, document.picture_change)
                self._report(output_id, port)
        if self._shell.picture(operations, timeout=SHELL_SECONDS, brightness=line):
            with self._wake:
                port.sent = (operations, line)
        else:
            log.warning("picture: the shell did not take %s's operations", output_id)

    @staticmethod
    def _presented(port: _Port, change: int) -> None:
        port.presented_change = max(change, port.presented_change or 0)

    def _report(self, output_id: str, port: _Port) -> None:
        """Under the lock: the Output's picture report, put when it differs from the last put."""
        report = PictureReport(output_id, port.identity if port.connected else None, port.offered(),
                               port.for_change, dict(port.results), port.presented_change)
        if report != port.put:
            port.put = report
            self._reports.put_picture(report)


__all__ = ["BRIGHTNESS_CODE", "READ_SECONDS", "REREAD_SECONDS", "SHELL_SECONDS",
           "TICK_SECONDS", "WRITE_GAP_SECONDS", "WRITE_SECONDS", "Applied", "AppliedPictures", "DdcPictureAdapter", "DisplayBrightness",
           "FileAppliedPictures", "PictureController", "PictureDdc", "PictureReports", "PictureShell",
           "brightness_line", "judged", "operations_for", "write_order"]
