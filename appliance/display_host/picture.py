"""The Pi's picture controller: carries out each Output document's picture part and reports back
(roadmap 1c; run ledger .claude/runs/display-1c.md, slice P1; design rule 1).

Pure core, effects at the edges. `operations_for`, `kelvin_multipliers`, `vcp_for` and
`percent_of` are pure. `PictureController` applies them through three ports: `PictureDdc`
(VCP 0x10 and 0x12 over the port's DDC/CI channel), `PictureShell` (the shell's `picture` control
op; weston.WestonBackend) and `PictureReports` (output_report.OutputReports).

Rules this module owns (one home each):
- **Where it acts is the document's:** a setting acting on the display is written over DDC/CI
  and never also adjusted in software; one acting as picture adjustment becomes an operation
  and is never written to the display. The Pi never moves a setting on its own: a refused write
  is reported (`not-taken`, `no-answer`), the setting stays where it is.
- **Unset writes nothing:** AS_THE_DISPLAY_HAS_IT is no write and the neutral operation.
- **Write only on a new picture change or another display:** a setting on the display is
  written when the document's picture_change rose, or the display (re)appeared with another
  identity; then read back, and the read-back judges it (`taken` when it equals the value
  written). Never between those: a person on the display's buttons is not fought; a re-read
  that differs (at each appearance, and every REREAD_SECONDS while a value is set) is reported
  as `read`, left alone until the next change.
- **Newest wins:** each Output has one picture worker; a document that arrives while it writes
  replaces the one waiting, so a dragged slider's writes never queue.
- **Operations from settings:** white balance from colour temperature (`kelvin_multipliers`,
  normalised so KELVIN_NEUTRAL is exactly (1, 1, 1) and the largest is 1); light factor =
  brightness / 50 and contrast slope = contrast / 50 when they act as picture adjustment and are
  set, else 1; gamma exponent = gamma_tenths / GAMMA_TENTHS_NEUTRAL.
- **Re-sent at start:** the controller restarts with Weston, and sends every Output's operations
  to the shell when it starts and whenever the shell reports an Output (re)created.
- **Presented:** `presented_change` is the newest picture change the shell's `presented` events
  echoed for the Output (frame protocol v4 `tag_picture`), or the new change when its operations
  equal the ones last presented.
- A percent maps to the display's own range read at probe: `vcp_for(percent, maximum)`.
- **The base page's line** (`brightness_line`) rides the shell's `picture` op beside the
  operations; the shell forwards it to the overlay client (frame protocol v4 `brightness`).
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Protocol

from contracts.node_frame import PictureOperations
from contracts.node_output import (
    AS_THE_DISPLAY_HAS_IT,
    GAMMA_TENTHS_NEUTRAL,
    KELVIN_NEUTRAL,
    ActsOn,
    DisplayIdentity,
    OutputDocument,
    OutputPicture,
    PictureReport,
    PictureSetting,
    SettingReport,
    WriteResult,
)

from .ddc import DdcChannel, VcpReading

BRIGHTNESS_CODE: Final = "10"       # VESA MCCS luminance
CONTRAST_CODE: Final = "12"         # VESA MCCS contrast
READ_SECONDS: Final = 5.0           # one VCP read
WRITE_SECONDS: Final = 10.0         # one VCP write with its read-back
REREAD_SECONDS: Final = 60.0        # how often a set value is read again (buttons on the display)
NEUTRAL_PERCENT: Final = 50         # brightness or contrast as picture adjustment: 50 % changes nothing
SHELL_SECONDS: Final = 2.0          # one `picture` control op
TICK_SECONDS: Final = 1.0           # a worker's longest sleep: a re-read falls due within it
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
    """White balance for `kelvin` (KELVIN_MIN..KELVIN_MAX in KELVIN_STEP): Mitchell Charity's
    blackbody table (vendian.org, CIE 1931 2-degree, D65 white) divided by its KELVIN_NEUTRAL
    entry and scaled so the largest is 1. Pure. ValueError("kelvin") outside the range or step."""
    if type(kelvin) is not int or kelvin not in _CHARITY:
        raise ValueError("kelvin")
    red, green, blue = (value / neutral for value, neutral in zip(_CHARITY[kelvin], _CHARITY[KELVIN_NEUTRAL],
                                                                    strict=True))
    largest = max(red, green, blue)
    return red / largest, green / largest, blue / largest


def _factor(setting: PictureSetting) -> float:
    """A setting's software factor: value / NEUTRAL_PERCENT as picture adjustment and set, else 1."""
    if setting.acts_on is ActsOn.PICTURE_ADJUSTMENT and setting.value != AS_THE_DISPLAY_HAS_IT:
        return setting.value / NEUTRAL_PERCENT
    return 1.0


def operations_for(output_id: str, picture_change: int, picture: OutputPicture) -> PictureOperations:
    """The Player's operations for one Output's picture part (the module's rule). Pure."""
    return PictureOperations(output_id=output_id, picture_change=picture_change,
                             rgb_multipliers=kelvin_multipliers(picture.colour_temperature_kelvin),
                             light_factor=_factor(picture.brightness), contrast_slope=_factor(picture.contrast),
                             gamma_exponent=picture.gamma_tenths / GAMMA_TENTHS_NEUTRAL)


def vcp_for(percent: int, maximum: int) -> int:
    """The VCP value for `percent` of the display's own range: round(percent * maximum / 100). Pure."""
    return round(percent * maximum / 100)


def percent_of(reading: VcpReading) -> int:
    """The percent a reading is of the display's range: round(current * 100 / maximum). Pure."""
    return round(reading.current * 100 / reading.maximum)


def brightness_line(setting: PictureSetting, read: int | None) -> str:
    """The base page's brightness line (design, "What the tab and the base page say"). `read` is
    the percent the display last read back for a setting on the display; None when its write was
    not taken or not answered. A software value is never shown as a panel value. Pure."""
    if setting.value == AS_THE_DISPLAY_HAS_IT:
        return "Brightness: as the display has it"
    if setting.acts_on is ActsOn.PICTURE_ADJUSTMENT:
        return f"Brightness: Photo Wall picture adjustment · {setting.value} %"
    if read is None:
        return "Brightness: on the display · the display did not take it"
    return f"Brightness: on the display · {read} %"


class PictureDdc(Protocol):
    """VCP 0x10 and 0x12 on one Output. Every call returns within its `timeout` and never raises for
    a display that does not answer."""

    def read(self, output_id: str, code: str, *, timeout: float) -> VcpReading | None: ...

    def write(self, output_id: str, code: str, percent: int, *, maximum: int, timeout: float) -> WriteResult:
        """Write `vcp_for(percent, maximum)` and read back: TAKEN when the read-back equals it,
        NOT_TAKEN for another value, NO_ANSWER for none."""
        ...


class PictureShell(Protocol):
    """The shell's `picture` control op (weston.WestonBackend.picture). `brightness` is the base
    page's line (`brightness_line`), forwarded by the shell to the overlay client."""

    def picture(self, operations: PictureOperations, *, timeout: float, brightness: str) -> bool: ...   # the shell took it


class PictureReports(Protocol):
    """Where the picture part of each Output's report goes (output_report.OutputReports)."""

    def put_picture(self, output_id: str, picture: PictureReport | None) -> None: ...   # never blocks


class DdcPictureAdapter:
    """`PictureDdc` over the port's DDC/CI channel (ddc.DdcChannel)."""

    def __init__(self, channel: DdcChannel) -> None:
        self._channel = channel

    def read(self, output_id: str, code: str, *, timeout: float) -> VcpReading | None:
        return self._channel.get_continuous(output_id, code, timeout=timeout)

    def write(self, output_id: str, code: str, percent: int, *, maximum: int, timeout: float) -> WriteResult:
        deadline = time.monotonic() + timeout
        value = vcp_for(percent, maximum)
        # The read-back judges the write, never the tool's exit status.
        self._channel.set(output_id, code, value, timeout=timeout)
        reading = self._channel.get_continuous(output_id, code, timeout=deadline - time.monotonic())
        if reading is None:
            return WriteResult.NO_ANSWER
        return WriteResult.TAKEN if reading.current == value else WriteResult.NOT_TAKEN


_CODES: Final = (BRIGHTNESS_CODE, CONTRAST_CODE)
_NEVER: Final = object()            # no identity applied yet (an identity may itself be None)


def _values(operations: PictureOperations) -> tuple:
    """What the Player does, without which change it came from."""
    return (operations.rgb_multipliers, operations.light_factor, operations.contrast_slope,
            operations.gamma_exponent)


def _settings(picture: OutputPicture) -> dict[str, PictureSetting]:
    return {BRIGHTNESS_CODE: picture.brightness, CONTRAST_CODE: picture.contrast}


def _on_the_display(setting: PictureSetting) -> bool:
    return setting.acts_on is ActsOn.DISPLAY and setting.value != AS_THE_DISPLAY_HAS_IT


@dataclass
class _Port:
    """One Output as the picture controller knows it; guarded by the controller's lock."""
    connected: bool = False
    identity: DisplayIdentity | None = None
    document: OutputDocument | None = None          # the latest document with a picture part
    applied: OutputDocument | None = None           # the document the results belong to
    applied_identity: object = _NEVER
    answered: dict[str, bool] = field(default_factory=lambda: dict.fromkeys(_CODES, False))
    reads: dict[str, int | None] = field(default_factory=lambda: dict.fromkeys(_CODES))
    results: dict[str, WriteResult | None] = field(default_factory=lambda: dict.fromkeys(_CODES))
    sent: tuple[PictureOperations, str] | None = None   # what the shell last took
    history: dict[int, tuple] = field(default_factory=dict)   # picture change -> its operations' values
    presented_values: tuple | None = None
    presented_change: int | None = None
    want_read: bool = False
    want_apply: bool = False
    want_send: bool = False
    next_reread: float | None = None
    worker: threading.Thread | None = None


class PictureController:
    """Every Output's picture, from its document, through DDC/CI and the shell, to the report."""

    def __init__(self, ddc: PictureDdc, shell: PictureShell, reports: PictureReports,
                 clock: Callable[[], float]) -> None:
        """`clock` is the Pi's monotonic seconds, never compared elsewhere."""
        self._ddc, self._shell, self._reports, self._clock = ddc, shell, reports, clock
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

    def document(self, output_id: str, document: OutputDocument) -> None:
        """A document arrived; acts when its picture_change rose (newest wins). Thread-safe, never
        blocks."""
        if document.output_id != output_id:
            raise ValueError("output_document_output")
        with self._wake:
            port = self._ports.setdefault(output_id, _Port())
            seen = port.document
            if seen is not None and document.picture_change <= seen.picture_change:
                return
            port.document = document         # replaces the one waiting: newest wins
            port.want_apply = True
            self._worker(output_id, port)
            self._wake.notify_all()

    def output(self, output_id: str, *, connected: bool, identity: DisplayIdentity | None) -> None:
        """What the compositor and the EDID say about `output_id` now: a (re)appearance reads 0x10
        and 0x12 (the answered flags), and another identity re-applies the document. Thread-safe,
        never blocks."""
        with self._wake:
            port = self._ports.setdefault(output_id, _Port())
            appeared = connected and not port.connected
            port.connected, port.identity = connected, identity
            if appeared:
                port.want_read = True
            if connected and port.document is not None and identity != port.applied_identity:
                port.want_apply = True
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
                current = operations_for(output_id, port.document.picture_change, port.document.picture)
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

    def _rereading(self, port: _Port) -> bool:
        """Under the lock: whether a set value on the display is due a re-read."""
        return (port.connected and port.applied is not None and port.next_reread is not None
                and self._clock() >= port.next_reread
                and any(_on_the_display(setting) for setting in _settings(port.applied.picture).values()))

    def _run(self, output_id: str, port: _Port) -> None:
        while True:
            with self._wake:
                while not self._stopping and not (port.want_read or port.want_apply or port.want_send
                                                  or self._rereading(port)):
                    self._wake.wait(TICK_SECONDS)
                if self._stopping:
                    return
                appearance, reread = port.want_read, not port.want_read and self._rereading(port)
                apply, send = port.want_apply, port.want_send
                port.want_read = port.want_apply = port.want_send = False
            try:
                if appearance or reread:
                    self._read(output_id, port, appearance=appearance)
                if apply:
                    self._apply(output_id, port)
                elif send:
                    self._send(output_id, port, force=True)
            except Exception:
                log.exception("picture: %s failed", output_id)

    def _read(self, output_id: str, port: _Port, *, appearance: bool) -> None:
        """Read 0x10 and 0x12 (reads only); at an appearance they set the answered flags."""
        readings = {code: self._ddc.read(output_id, code, timeout=READ_SECONDS) for code in _CODES}
        with self._wake:
            for code, reading in readings.items():
                if appearance:
                    port.answered[code] = reading is not None
                port.reads[code] = None if reading is None else percent_of(reading)
            port.next_reread = self._clock() + REREAD_SECONDS
            self._report(output_id, port)
        self._send(output_id, port, force=False)     # the base page's line follows the read

    def _apply(self, output_id: str, port: _Port) -> None:
        """Carry out the latest document: operations to the shell, then each setting on the display
        written and read back (read first: a display that already has the value is not written)."""
        with self._wake:
            document, identity, connected = port.document, port.identity, port.connected
        if document is None:
            return
        self._send(output_id, port, force=False, document=document)
        results: dict[str, WriteResult | None] = dict.fromkeys(_CODES)
        reads: dict[str, int | None] = {}
        if connected:
            for code, setting in _settings(document.picture).items():
                if _on_the_display(setting):
                    results[code], reads[code] = self._write(output_id, code, setting.value)
        with self._wake:
            port.applied, port.applied_identity = document, identity
            port.results = results
            port.reads.update(reads)
            if connected:
                port.next_reread = self._clock() + REREAD_SECONDS
            self._report(output_id, port)
        self._send(output_id, port, force=False, document=document)   # the line after the result

    def _write(self, output_id: str, code: str, percent: int) -> tuple[WriteResult | None, int | None]:
        """One setting on the display: (the write's result, None when nothing was written; the
        percent the display reads)."""
        reading = self._ddc.read(output_id, code, timeout=READ_SECONDS)
        if reading is None:
            return WriteResult.NO_ANSWER, None
        wanted = vcp_for(percent, reading.maximum)
        if reading.current == wanted:
            return None, percent_of(reading)
        result = self._ddc.write(output_id, code, percent, maximum=reading.maximum, timeout=WRITE_SECONDS)
        if result is WriteResult.TAKEN:
            return result, percent_of(VcpReading(wanted, reading.maximum))
        after = self._ddc.read(output_id, code, timeout=READ_SECONDS) if result is WriteResult.NOT_TAKEN else None
        return result, None if after is None else percent_of(after)

    def _send(self, output_id: str, port: _Port, *, force: bool,
              document: OutputDocument | None = None) -> None:
        """The Output's operations and base page line to the shell, when either differs from what
        the shell last took (or `force`: a re-send)."""
        with self._wake:
            document = document or port.document
            if document is None:
                return
            operations = operations_for(output_id, document.picture_change, document.picture)
            brightness = document.picture.brightness
            failed = port.applied is document and port.results[BRIGHTNESS_CODE] in (
                WriteResult.NOT_TAKEN, WriteResult.NO_ANSWER)
            line = brightness_line(brightness, None if failed else port.reads[BRIGHTNESS_CODE])
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
        """Under the lock: the Output's picture part (None before its first applied document)."""
        applied = port.applied
        if applied is None:
            self._reports.put_picture(output_id, None)
            return
        settings = _settings(applied.picture)

        def setting(code: str) -> SettingReport:
            acts_on = settings[code].acts_on
            if acts_on is ActsOn.PICTURE_ADJUSTMENT:
                return SettingReport(acts_on, None, None)
            return SettingReport(acts_on, port.reads[code], port.results[code])

        self._reports.put_picture(output_id, PictureReport(
            port.answered[BRIGHTNESS_CODE], port.answered[CONTRAST_CODE], applied.picture_change,
            setting(BRIGHTNESS_CODE), setting(CONTRAST_CODE), port.presented_change))


__all__ = ["BRIGHTNESS_CODE", "CONTRAST_CODE", "NEUTRAL_PERCENT", "READ_SECONDS", "REREAD_SECONDS",
           "SHELL_SECONDS", "TICK_SECONDS", "WRITE_SECONDS", "DdcPictureAdapter", "PictureController",
           "PictureDdc", "PictureReports", "PictureShell", "brightness_line", "kelvin_multipliers",
           "operations_for", "percent_of", "vcp_for"]
