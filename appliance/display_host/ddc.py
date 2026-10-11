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

from dataclasses import dataclass
from pathlib import Path

from .power_methods import DDC_CACHE, Runner


@dataclass(frozen=True)
class VcpReading:
    """One `getvcp <code> --terse` answer for a continuous code: its current and maximum values."""
    current: int
    maximum: int

    def __post_init__(self) -> None:
        """ValueError("vcp_reading") unless 0 <= current <= maximum and maximum >= 1."""
        raise NotImplementedError


class DdcChannel:
    """Every Output's DDC/CI, one command at a time per port. Thread-safe."""

    def __init__(self, run: Runner, *, drm_root: Path = Path("/sys/class/drm"),
                 cache_dir: Path = DDC_CACHE) -> None:
        raise NotImplementedError

    def bus(self, output_id: str) -> int | None:
        """The i2c bus the connector's `ddc` link names; None without one."""
        raise NotImplementedError

    def get(self, output_id: str, code: str, *, timeout: float) -> str | None:
        """`getvcp <code> --terse`'s stdout; None when it did not answer (no bus, a non-zero exit, a
        hang past `timeout`)."""
        raise NotImplementedError

    def get_continuous(self, output_id: str, code: str, *, timeout: float) -> VcpReading | None:
        """A continuous code's reading ("VCP 10 C 60 100"); None when it did not answer or the
        answer is not a continuous reading."""
        raise NotImplementedError

    def set(self, output_id: str, code: str, value: int, *, timeout: float) -> bool:
        """`setvcp <code> <value>`; whether the tool exited 0. Never the judge of a write: callers
        read back."""
        raise NotImplementedError


__all__ = ["DdcChannel", "VcpReading"]
