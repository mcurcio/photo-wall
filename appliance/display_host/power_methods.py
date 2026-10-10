"""The three power methods as adapters over their tools (roadmap 1b; run ledger
.claude/runs/display-1b.md, slice D2; the spike's runbook, .claude/skills/display-control/).

Each class implements `output_power.PowerMethodAdapter`. They are the only code that runs
`ddcutil` or `cec-ctl` or asks the compositor for output power; every call is bounded by its
`timeout` through the injected `Runner` (tests pass a fake that plays recorded tool output).

- DDC/CI: `ddcutil --bus <n> --noconfig` with XDG_CACHE_HOME on the unit's private /tmp; read
  `getvcp D6 --terse`, write `setvcp D6 4` (standby) / `setvcp D6 1` (on), always read back;
  never D6 = 5. The bus of an Output is the i2c number of `/sys/class/drm/<card>-<output>/ddc`
  (HDMI-A-1 -> 13, HDMI-A-2 -> 14 on a Pi 5). Input (#117, #118): VCP 60, which many monitors
  ignore (the test monitor does), so `showing_other_input` is None when the read is unstable.
- HDMI-CEC: `cec-ctl -d /dev/cec<n> --playback` to claim a logical address, `--to 0
  --give-device-power-status` to read, `--standby` / `--image-view-on` to write,
  `--active-source` to claim the input; `--clear` on exit. "Not Acknowledged" = no answer. The
  device of an Output is the cecN whose DRM connector info names it.
- Signal off: the compositor's own output power through the base-only control socket (op
  `output_power`, appliance/display_host/native/shell.c, `weston_output_power_off`/`_on`): never a
  request in photo-wall-frame-v1.xml, which is the apps' protocol. It confirms the Pi stopped its
  picture and never what the panel did, so its success is SIGNAL_STOPPED.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final, Protocol

from contracts.node_output import Power, PowerMethod, PowerResult

DDCUTIL: Final = "/usr/bin/ddcutil"
CEC_CTL: Final = "/usr/bin/cec-ctl"
DDC_POWER_CODE: Final = "D6"
DDC_INPUT_CODE: Final = "60"
DDC_ON: Final = 1
DDC_STANDBY: Final = 4          # never 5 ("hard off"): a display may never come back from it

# Runs one tool with argv, an environment and a deadline; a tool past its deadline is killed and
# raises subprocess.TimeoutExpired. Production: subprocess.run(..., capture_output=True, timeout=...).
Runner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


class OutputPowerControl(Protocol):
    """The compositor's output power, through the base-only control socket (weston.WestonBackend)."""

    def output_power(self, output_id: str, power: Power, *, timeout: float) -> bool: ...   # done by the compositor

    def output_powered(self, output_id: str) -> Power | None: ...   # the compositor's own state; None: no such Output


class DdcCiAdapter:
    method: Final = PowerMethod.DDC_CI

    def __init__(self, run: Runner, *, drm_root: Path = Path("/sys/class/drm"),
                 cache_dir: Path = Path("/tmp/ddcutil")) -> None:
        raise NotImplementedError

    def probe(self, output_id: str, *, timeout: float) -> bool:
        raise NotImplementedError

    def read(self, output_id: str, *, timeout: float) -> Power | None:
        raise NotImplementedError

    def set(self, output_id: str, power: Power, *, timeout: float) -> PowerResult:
        raise NotImplementedError

    def showing_other_input(self, output_id: str, *, timeout: float) -> bool | None:
        raise NotImplementedError

    def claim_input(self, output_id: str, *, timeout: float) -> bool:
        raise NotImplementedError


class HdmiCecAdapter:
    method: Final = PowerMethod.HDMI_CEC

    def __init__(self, run: Runner, *, dev_root: Path = Path("/dev")) -> None:
        raise NotImplementedError

    def probe(self, output_id: str, *, timeout: float) -> bool:
        raise NotImplementedError

    def read(self, output_id: str, *, timeout: float) -> Power | None:
        raise NotImplementedError

    def set(self, output_id: str, power: Power, *, timeout: float) -> PowerResult:
        raise NotImplementedError

    def showing_other_input(self, output_id: str, *, timeout: float) -> bool | None:
        raise NotImplementedError

    def claim_input(self, output_id: str, *, timeout: float) -> bool:
        raise NotImplementedError


class SignalOffAdapter:
    method: Final = PowerMethod.SIGNAL_OFF

    def __init__(self, compositor: OutputPowerControl) -> None:
        raise NotImplementedError

    def probe(self, output_id: str, *, timeout: float) -> bool:
        raise NotImplementedError

    def read(self, output_id: str, *, timeout: float) -> Power | None:
        raise NotImplementedError

    def set(self, output_id: str, power: Power, *, timeout: float) -> PowerResult:
        raise NotImplementedError

    def showing_other_input(self, output_id: str, *, timeout: float) -> bool | None:
        raise NotImplementedError

    def claim_input(self, output_id: str, *, timeout: float) -> bool:
        raise NotImplementedError
