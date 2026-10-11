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
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Final, Protocol

from contracts.node_frame import PictureOperations
from contracts.node_output import (
    DisplayIdentity,
    OutputDocument,
    OutputPicture,
    PictureReport,
    WriteResult,
)

from .ddc import DdcChannel, VcpReading

BRIGHTNESS_CODE: Final = "10"       # VESA MCCS luminance
CONTRAST_CODE: Final = "12"         # VESA MCCS contrast
READ_SECONDS: Final = 5.0           # one VCP read
WRITE_SECONDS: Final = 10.0         # one VCP write with its read-back
REREAD_SECONDS: Final = 60.0        # how often a set value is read again (buttons on the display)
NEUTRAL_PERCENT: Final = 50         # brightness or contrast as picture adjustment: 50 % changes nothing


def kelvin_multipliers(kelvin: int) -> tuple[float, float, float]:
    """White balance for `kelvin` (KELVIN_MIN..KELVIN_MAX in KELVIN_STEP): Mitchell Charity's
    blackbody table (vendian.org, CIE 1931 2-degree, D65 white) divided by its KELVIN_NEUTRAL
    entry and scaled so the largest is 1. Pure. ValueError("kelvin") outside the range or step."""
    raise NotImplementedError


def operations_for(output_id: str, picture_change: int, picture: OutputPicture) -> PictureOperations:
    """The Player's operations for one Output's picture part (the module's rule). Pure."""
    raise NotImplementedError


def vcp_for(percent: int, maximum: int) -> int:
    """The VCP value for `percent` of the display's own range: round(percent * maximum / 100). Pure."""
    raise NotImplementedError


def percent_of(reading: VcpReading) -> int:
    """The percent a reading is of the display's range: round(current * 100 / maximum). Pure."""
    raise NotImplementedError


class PictureDdc(Protocol):
    """VCP 0x10 and 0x12 on one Output. Every call returns within its `timeout` and never raises for
    a display that does not answer."""

    def read(self, output_id: str, code: str, *, timeout: float) -> VcpReading | None: ...

    def write(self, output_id: str, code: str, percent: int, *, maximum: int, timeout: float) -> WriteResult:
        """Write `vcp_for(percent, maximum)` and read back: TAKEN when the read-back equals it,
        NOT_TAKEN for another value, NO_ANSWER for none."""
        ...


class PictureShell(Protocol):
    """The shell's `picture` control op (weston.WestonBackend.picture)."""

    def picture(self, operations: PictureOperations, *, timeout: float) -> bool: ...   # the shell took it


class PictureReports(Protocol):
    """Where the picture part of each Output's report goes (output_report.OutputReports)."""

    def put_picture(self, output_id: str, picture: PictureReport | None) -> None: ...   # never blocks


class DdcPictureAdapter:
    """`PictureDdc` over the port's DDC/CI channel (ddc.DdcChannel)."""

    def __init__(self, channel: DdcChannel) -> None:
        raise NotImplementedError

    def read(self, output_id: str, code: str, *, timeout: float) -> VcpReading | None:
        raise NotImplementedError

    def write(self, output_id: str, code: str, percent: int, *, maximum: int, timeout: float) -> WriteResult:
        raise NotImplementedError


class PictureController:
    """Every Output's picture, from its document, through DDC/CI and the shell, to the report."""

    def __init__(self, ddc: PictureDdc, shell: PictureShell, reports: PictureReports,
                 clock: Callable[[], float]) -> None:
        """`clock` is the Pi's monotonic seconds, never compared elsewhere."""
        raise NotImplementedError

    def start(self) -> None:
        """Start one worker per known Output and send every known Output's operations to the shell
        (the re-send at start); returns at once."""
        raise NotImplementedError

    def stop(self, timeout: float = 5.0) -> None:
        raise NotImplementedError

    def document(self, output_id: str, document: OutputDocument) -> None:
        """A document arrived; acts when its picture_change rose (newest wins). A document with no
        picture part (picture None, before 1c's INTEGRATE) does nothing. Thread-safe, never blocks."""
        raise NotImplementedError

    def output(self, output_id: str, *, connected: bool, identity: DisplayIdentity | None) -> None:
        """What the compositor and the EDID say about `output_id` now: a (re)appearance reads 0x10
        and 0x12 (the answered flags), and another identity re-applies the document. Thread-safe,
        never blocks."""
        raise NotImplementedError

    def shell_output(self, output_id: str) -> None:
        """The shell (re)created `output_id` (a Weston restart or hotplug): re-send its operations."""
        raise NotImplementedError

    def presented(self, output_id: str, picture_change: int | None) -> None:
        """The shell's `presented` event echoed this picture change for `output_id`."""
        raise NotImplementedError


__all__ = ["BRIGHTNESS_CODE", "CONTRAST_CODE", "NEUTRAL_PERCENT", "READ_SECONDS", "REREAD_SECONDS",
           "WRITE_SECONDS", "DdcPictureAdapter", "PictureController", "PictureDdc", "PictureReports",
           "PictureShell", "kelvin_multipliers", "operations_for", "percent_of", "vcp_for"]
