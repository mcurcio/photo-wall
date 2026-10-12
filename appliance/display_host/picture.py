"""The Pi's picture controller: carries out each picture document (the display's own settings and
Photo Wall's, each on its own) and reports back (decision 0022; the picture split, slice S1; from
roadmap 1c's controller).

Pure core, effects at the edges. `operations_for`, `kelvin_multipliers`, `judged`, `write_order`
and `brightness_line` are pure. `PictureController` applies them through five ports: `PictureDdc`
(one code's read and write over the port's DDC/CI channel, picture's turn), `PictureAsk` (which
settings on the picture list the display offers; picture_list.PictureListAsker), `PictureShell`
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
  worker asks the list (`PictureAsk.ask`), again every ASK_AGAIN_SECONDS while it is unknown; the
  list is held per identity for the controller's life (a blip with the same identity does not
  re-ask).
- **Write only on a new picture change or another display:** the hardware codes are written, in
  `write_order`, when the document's picture_change rose or the display appeared with another
  identity, once it is connected and its list is known. Each code is read first (a display that
  already has the value is not written), a level past the display's maximum is not written
  (`not-taken`), and the read-back judges a write (`judged`). A picture change already carried out
  on the same display is never written again, even by a restarted controller (a Weston restart
  restarts it): `AppliedPictures` remembers it.
- **At most two writes a second per display** (WRITE_GAP_SECONDS): some displays save each write
  to memory that wears, so the worker waits out the gap before a write.
- **Newest wins:** each Output has one picture worker; a document that arrives while it writes
  replaces the one waiting, so a dragged slider's writes never queue.
- **Operations from the software group** (`operations_for`): white balance from colour
  temperature (`kelvin_multipliers`, in linear light, normalised so KELVIN_NEUTRAL is exactly
  (1, 1, 1) and the largest is 1); light factor = brightness / SOFTWARE_PERCENT_NEUTRAL; contrast
  slope = contrast / 100; gamma exponent = gamma_tenths / GAMMA_TENTHS_NEUTRAL. Sent to the shell
  at once on each new change, before any hardware write.
- **Re-sent at start:** the controller restarts with Weston, and sends every Output's operations
  to the shell when it starts and whenever the shell reports an Output (re)created.
- **Presented:** `presented_change` is the newest picture change the shell's `presented` events
  echoed for the Output (frame protocol v4 `tag_picture`), or the new change when its operations
  equal the ones last presented.
- **The base page's line** (`brightness_line`) rides the shell's `picture` op beside the
  operations; the shell forwards it to the overlay client (frame protocol v4 `brightness`).
- **The report** is put whenever it changes (identity, offered, for_change, results,
  presented_change).

The periodic re-read of the display's settings (1c's REREAD_SECONDS, "Changed on the display's
buttons") is the picture split's slice S3, on this controller's worker.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol

from contracts.node_frame import PictureOperations, srgb_to_linear
from contracts.node_output import DisplayIdentity
from contracts.node_picture import (
    CHOICE_VALUE_MAX,
    GAMMA_TENTHS_NEUTRAL,
    KELVIN_NEUTRAL,
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

from .ddc import DdcChannel, Urgency, VcpAnswer, VcpReading, parse_terse
from .picture_list import ASK_AGAIN_SECONDS, PictureAsk

BRIGHTNESS_CODE: Final = "10"       # VESA MCCS luminance
READ_SECONDS: Final = 5.0           # one VCP read (the port's wait included)
WRITE_SECONDS: Final = 10.0         # one VCP write with its read-back
WRITE_GAP_SECONDS: Final = 0.5      # the least time between two writes to one display
SHELL_SECONDS: Final = 2.0          # one `picture` control op
TICK_SECONDS: Final = 1.0           # a worker's longest sleep: an ask again falls due within it
CONTRAST_NEUTRAL_PERCENT: Final = 100   # contrast slope = contrast / 100
_HISTORY: Final = 16                # picture changes remembered per Output for `presented`

log = logging.getLogger(__name__)

# Mitchell Charity's blackbody colours (http://www.vendian.org/mncharity/dir3/blackbody/,
# bbr_color.txt version 2001-Jun-22): the CIE 1931 2-degree rows, R G B normalised, sRGB primaries
# with a D65 white point, KELVIN_MIN..KELVIN_MAX in KELVIN_STEP.
_CHARITY: Final[Mapping[int, tuple[float, float, float]]] = MappingProxyType({
    3000: (1.0000, 0.4870, 0.1411), 3100: (1.0000, 0.5067, 0.1602), 3200: (1.0000, 0.5259, 0.1800),
    3300: (1.0000, 0.5447, 0.2005), 3400: (1.0000, 0.5630, 0.2216), 3500: (1.0000, 0.5809, 0.2433),
    3600: (1.0000, 0.5983, 0.2655), 3700: (1.0000, 0.6153, 0.2881), 3800: (1.0000, 0.6318, 0.3112),
    3900: (1.0000, 0.6480, 0.3346), 4000: (1.0000, 0.6636, 0.3583), 4100: (1.0000, 0.6789, 0.3823),
    4200: (1.0000, 0.6938, 0.4066), 4300: (1.0000, 0.7083, 0.4310), 4400: (1.0000, 0.7223, 0.4556),
    4500: (1.0000, 0.7360, 0.4803), 4600: (1.0000, 0.7494, 0.5051), 4700: (1.0000, 0.7623, 0.5299),
    4800: (1.0000, 0.7750, 0.5548), 4900: (1.0000, 0.7872, 0.5796), 5000: (1.0000, 0.7992, 0.6045),
    5100: (1.0000, 0.8108, 0.6293), 5200: (1.0000, 0.8221, 0.6541), 5300: (1.0000, 0.8330, 0.6787),
    5400: (1.0000, 0.8437, 0.7032), 5500: (1.0000, 0.8541, 0.7277), 5600: (1.0000, 0.8642, 0.7519),
    5700: (1.0000, 0.8740, 0.7760), 5800: (1.0000, 0.8836, 0.8000), 5900: (1.0000, 0.8929, 0.8238),
    6000: (1.0000, 0.9019, 0.8473), 6100: (1.0000, 0.9107, 0.8707), 6200: (1.0000, 0.9193, 0.8939),
    6300: (1.0000, 0.9276, 0.9168), 6400: (1.0000, 0.9357, 0.9396), 6500: (1.0000, 0.9436, 0.9621),
    6600: (1.0000, 0.9513, 0.9844), 6700: (0.9937, 0.9526, 1.0000), 6800: (0.9726, 0.9395, 1.0000),
    6900: (0.9526, 0.9270, 1.0000), 7000: (0.9337, 0.9150, 1.0000), 7100: (0.9157, 0.9035, 1.0000),
    7200: (0.8986, 0.8925, 1.0000), 7300: (0.8823, 0.8819, 1.0000), 7400: (0.8668, 0.8718, 1.0000),
    7500: (0.8520, 0.8621, 1.0000), 7600: (0.8379, 0.8527, 1.0000), 7700: (0.8244, 0.8437, 1.0000),
    7800: (0.8115, 0.8351, 1.0000), 7900: (0.7992, 0.8268, 1.0000), 8000: (0.7874, 0.8187, 1.0000),
    8100: (0.7761, 0.8110, 1.0000), 8200: (0.7652, 0.8035, 1.0000), 8300: (0.7548, 0.7963, 1.0000),
    8400: (0.7449, 0.7894, 1.0000), 8500: (0.7353, 0.7827, 1.0000), 8600: (0.7260, 0.7762, 1.0000),
    8700: (0.7172, 0.7699, 1.0000), 8800: (0.7086, 0.7638, 1.0000), 8900: (0.7004, 0.7579, 1.0000),
    9000: (0.6925, 0.7522, 1.0000), 9100: (0.6848, 0.7467, 1.0000), 9200: (0.6774, 0.7414, 1.0000),
    9300: (0.6703, 0.7362, 1.0000),
})


def kelvin_multipliers(kelvin: int) -> tuple[float, float, float]:
    """White balance for `kelvin` (KELVIN_MIN..KELVIN_MAX in KELVIN_STEP), in linear light as the
    Player applies it: Mitchell Charity's blackbody table (vendian.org, CIE 1931 2-degree, D65
    white; its R G B are gamma-encoded sRGB) taken to linear light (`srgb_to_linear`), divided by
    its linear KELVIN_NEUTRAL entry and scaled so the largest is 1, so white shows as the table's
    colour. Pure. ValueError("kelvin") outside the range or step."""
    if type(kelvin) is not int or kelvin not in _CHARITY:
        raise ValueError("kelvin")
    red, green, blue = (srgb_to_linear(value) / srgb_to_linear(neutral)
                        for value, neutral in zip(_CHARITY[kelvin], _CHARITY[KELVIN_NEUTRAL], strict=True))
    largest = max(red, green, blue)
    return red / largest, green / largest, blue / largest


def operations_for(output_id: str, picture_change: int, software: SoftwarePicture) -> PictureOperations:
    """The Player's operations for one Output's software group (the module's rule). Pure."""
    return PictureOperations(output_id=output_id, picture_change=picture_change,
                             rgb_multipliers=kelvin_multipliers(software.colour_temperature_kelvin),
                             light_factor=software.brightness_percent / SOFTWARE_PERCENT_NEUTRAL,
                             contrast_slope=software.contrast_percent / CONTRAST_NEUTRAL_PERCENT,
                             gamma_exponent=software.gamma_tenths / GAMMA_TENTHS_NEUTRAL)


def _value(answer: VcpAnswer | None) -> int | None:
    """An answer's value: a level's current or a list setting's byte; None for no answer."""
    if answer is None:
        return None
    return answer.current if isinstance(answer, VcpReading) else answer.value


def judged(written: int, after: VcpAnswer | None) -> WriteResult:
    """A write's result by its read-back: TAKEN when the value read equals the value written,
    NOT_TAKEN for another, NO_ANSWER for none. Pure."""
    value = _value(after)
    if value is None:
        return WriteResult.NO_ANSWER
    return WriteResult.TAKEN if value == written else WriteResult.NOT_TAKEN


def write_order(hardware: Mapping[VcpCode, int], offered: tuple[HardwareSetting, ...]) -> tuple[VcpCode, ...]:
    """The codes of `hardware` the display offers, in the order they are written: PICTURE_LIST's
    (S3: list settings before levels). A code the display does not offer is never written. Pure."""
    codes = {setting.code for setting in offered}
    return tuple(code for code in PICTURE_LIST if code in hardware and code in codes)


@dataclass(frozen=True)
class DisplayBrightness:
    """What the base page says of the display's own brightness (VCP 10)."""
    percent: int | None      # the display's last read of 10 as a whole percent of its range; None: unknown
    set_here: bool           # 10 is in the document's hardware
    took: bool               # the latest write for 10 was taken, or nothing needed writing


def brightness_line(software_percent: int, display: DisplayBrightness) -> str:
    """The base page's brightness line: the display's own brightness and Photo Wall's, each named,
    so a software value is never shown as a panel value (U9). Pure. (S3 owns the final words.)"""
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


class AppliedPictures(Protocol):
    """Which picture change each Output's display last had written, and on which display, kept
    across controller restarts."""

    def last(self, output_id: str) -> tuple[int, DisplayIdentity | None] | None: ...   # None: none known

    def record(self, output_id: str, picture_change: int, identity: DisplayIdentity | None) -> None: ...


class FileAppliedPictures:
    """`AppliedPictures` as one small JSON file per Output in `directory` (the controller's
    preserved runtime directory: it outlives the controller's restarts with Weston, not a reboot).
    A file that is missing or does not read is "none known"; a failed write is logged."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def _path(self, output_id: str) -> Path:
        return self._directory / f"picture-{output_id}.json"

    def last(self, output_id: str) -> tuple[int, DisplayIdentity | None] | None:
        try:
            value = json.loads(self._path(output_id).read_text())
            change, identity = value["picture_change"], value["identity"]
            if type(change) is not int:
                return None
            return change, None if identity is None else DisplayIdentity(**identity)
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def record(self, output_id: str, picture_change: int, identity: DisplayIdentity | None) -> None:
        path = self._path(output_id)
        partial = path.with_suffix(".partial")
        try:
            partial.write_text(json.dumps({"picture_change": picture_change,
                                           "identity": None if identity is None else asdict(identity)}))
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
    """What the Player does, without which change it came from."""
    return (operations.rgb_multipliers, operations.light_factor, operations.contrast_slope,
            operations.gamma_exponent)


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
    for_change: int | None = None
    results: dict[VcpCode, HardwareResult] = field(default_factory=dict)
    brightness_read: int | None = None              # the display's last read of 10, in its own count
    brightness_took: bool = True                    # the latest write of 10 was taken, or none was needed
    sent: tuple[PictureOperations, str] | None = None   # what the shell last took
    history: dict[int, tuple] = field(default_factory=dict)   # picture change -> its operations' values
    presented_values: tuple | None = None
    presented_change: int | None = None
    put: PictureReport | None = None                # the report last put
    want_apply: bool = False
    want_send: bool = False
    last_write: float | None = None                 # time.monotonic() at the end of the last write
    worker: threading.Thread | None = None

    def offered(self) -> tuple[HardwareSetting, ...] | None:
        """The connected display's list; None while it is not known (or nothing is connected)."""
        if not self.connected or self.identity is None:
            return None
        return self.lists.get(self.identity)


class PictureController:
    """Every Output's picture, from its picture document, through DDC/CI and the shell, to its
    picture report."""

    def __init__(self, ddc: PictureDdc, ask: PictureAsk, shell: PictureShell, reports: PictureReports,
                 applied: AppliedPictures, clock: Callable[[], float]) -> None:
        """`clock` is the Pi's monotonic seconds for asking again, never compared elsewhere."""
        self._ddc, self._ask, self._shell, self._reports = ddc, ask, shell, reports
        self._applied, self._clock = applied, clock
        self._ports: dict[str, _Port] = {}
        self._wake = threading.Condition()
        self._started = False
        self._stopping = False

    def start(self) -> None:
        """Start one worker per known Output and send every known Output's operations to the shell
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
            workers = [port.worker for port in self._ports.values() if port.worker is not None]
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
            port.pending = port.want_apply = True
            self._worker(output_id, port)
            self._wake.notify_all()

    def output(self, output_id: str, *, connected: bool, identity: DisplayIdentity | None) -> None:
        """What the compositor and the EDID say about `output_id` now: an identity whose list is
        not held is asked, and another identity carries out the document again. Thread-safe, never
        blocks."""
        with self._wake:
            port = self._ports.setdefault(output_id, _Port())
            port.connected, port.identity = connected, identity
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
        """Under the lock: the Output's worker, once the controller started."""
        if self._started and not self._stopping and port.worker is None:
            port.worker = threading.Thread(target=self._run, args=(output_id, port),
                                           name=f"picture-{output_id}", daemon=True)
            port.worker.start()

    def _ask_due(self, port: _Port) -> DisplayIdentity | None:
        """Under the lock: the connected display whose list is due an ask; None when none is."""
        identity = port.identity
        if not port.connected or identity is None or identity in port.lists:
            return None
        after = port.ask_after.get(identity)
        return identity if after is None or self._clock() >= after else None

    def _run(self, output_id: str, port: _Port) -> None:
        while True:
            with self._wake:
                while not self._stopping and not (port.want_apply or port.want_send or self._ask_due(port)):
                    self._wake.wait(TICK_SECONDS)
                if self._stopping:
                    return
                apply, send, asking = port.want_apply, port.want_send, self._ask_due(port)
                port.want_apply = port.want_send = False
            try:
                if apply or send:                    # the operations first, before any port wait
                    self._send(output_id, port, force=send)
                if asking is not None and self._asked(output_id, port, asking):
                    apply = True
                if apply:
                    self._carry_out(output_id, port)
                elif asking is not None:
                    self._send(output_id, port, force=False)   # the line follows the list's read of 10
            except Exception:
                log.exception("picture: %s failed", output_id)

    def _asked(self, output_id: str, port: _Port, identity: DisplayIdentity) -> bool:
        """Ask `identity`'s list; whether the latest document now waits only on it."""
        offered = self._ask.ask(output_id)
        with self._wake:
            if offered is None:
                port.ask_after[identity] = self._clock() + ASK_AGAIN_SECONDS
                return False
            port.lists[identity] = offered
            brightness = next((setting for setting in offered if setting.code == BRIGHTNESS_CODE), None)
            if brightness is not None and port.identity == identity:
                port.brightness_read = brightness.current
            self._report(output_id, port)
            return port.pending

    def _carry_out(self, output_id: str, port: _Port) -> None:
        """The latest document's hardware on the connected display: each offered code in
        `write_order`, read first, written and judged by its read-back. Waits (stays pending) while
        hardware is set and the display is not connected or its list is not known."""
        with self._wake:
            document, identity, connected, offered = port.document, port.identity, port.connected, port.offered()
            if document is None or not port.pending:
                return
        if not document.hardware:
            self._finish(output_id, port, document, identity, {})
            return
        if not connected:
            return
        if self._applied.last(output_id) == (document.picture_change, identity):
            self._finish(output_id, port, document, identity, {})   # this display already had it
            return
        if offered is None:
            return
        settings = {setting.code: setting for setting in offered}
        results: dict[VcpCode, HardwareResult] = {}
        for code in write_order(document.hardware, offered):
            result = self._write(output_id, port, settings[code], document.hardware[code])
            if result is not None:
                results[code] = result
        self._applied.record(output_id, document.picture_change, identity)
        self._finish(output_id, port, document, identity, results)

    def _write(self, output_id: str, port: _Port, setting: HardwareSetting, value: int) -> HardwareResult | None:
        """One code: None when the display already has `value`; else the write's result. A write
        waits out WRITE_GAP_SECONDS after the port's last."""
        code = setting.code
        before = _value(self._ddc.read(output_id, code, timeout=READ_SECONDS))
        self._brightness(port, code, before)
        if before == value:
            return None
        limit = setting.range.maximum if setting.range.kind is RangeKind.LEVEL else CHOICE_VALUE_MAX
        if value > (limit or 0):                    # past the display's own range: never written
            if before is None:
                return HardwareResult(WriteResult.NO_ANSWER, None, None)
            return HardwareResult(WriteResult.NOT_TAKEN, before, before)
        if port.last_write is not None:
            time.sleep(max(0.0, port.last_write + WRITE_GAP_SECONDS - time.monotonic()))
        try:
            after = self._ddc.write(output_id, code, value, timeout=WRITE_SECONDS)
        finally:
            port.last_write = time.monotonic()
        self._brightness(port, code, _value(after))
        return HardwareResult(judged(value, after), before, _value(after))

    def _brightness(self, port: _Port, code: VcpCode, value: int | None) -> None:
        if code == BRIGHTNESS_CODE and value is not None:
            with self._wake:
                port.brightness_read = value

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
        self._send(output_id, port, force=False)     # the line after the result

    def _line(self, port: _Port, document: PictureDocument) -> str:
        """Under the lock: the base page's brightness line for `document` on this display."""
        offered = port.offered() or ()
        setting = next((item for item in offered if item.code == BRIGHTNESS_CODE), None)
        percent = None
        if (setting is not None and setting.range.kind is RangeKind.LEVEL and setting.range.maximum
                and port.brightness_read is not None):
            percent = round(port.brightness_read * 100 / setting.range.maximum)
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


__all__ = ["BRIGHTNESS_CODE", "CONTRAST_NEUTRAL_PERCENT", "READ_SECONDS", "SHELL_SECONDS", "TICK_SECONDS",
           "WRITE_GAP_SECONDS", "WRITE_SECONDS", "AppliedPictures", "DdcPictureAdapter", "DisplayBrightness",
           "FileAppliedPictures", "PictureController", "PictureDdc", "PictureReports", "PictureShell",
           "brightness_line", "judged", "kelvin_multipliers", "operations_for", "write_order"]
