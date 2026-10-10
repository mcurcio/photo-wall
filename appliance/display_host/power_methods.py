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

D2 (stated in E-1B-D2-2): neither cable channel can tell which input is this Pi's, so both
`showing_other_input` answer None (the off goes ahead) and DDC/CI claims no input; CEC claims it
with Active Source.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import time
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
DDC_CACHE: Final = Path("/tmp/ddcutil")   # XDG_CACHE_HOME for ddcutil: the unit's private /tmp
TOOL_PATH: Final = "/usr/bin"

# Runs one tool with argv, an environment and a deadline; a tool past its deadline is killed and
# raises subprocess.TimeoutExpired. Production: subprocess.run(..., capture_output=True, timeout=...).
Runner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


class OutputPowerControl(Protocol):
    """The compositor's output power, through the base-only control socket (weston.WestonBackend)."""

    def output_power(self, output_id: str, power: Power, *, timeout: float) -> bool: ...   # done by the compositor

    def output_powered(self, output_id: str) -> Power | None: ...   # the compositor's own state; None: no such Output


def run_tool(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """The production `Runner`: one tool, its output captured, killed past `timeout`."""
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False,
                          env={"XDG_CACHE_HOME": str(DDC_CACHE), "PATH": TOOL_PATH})


def _ask(run: Runner, argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
    """`run` bounded by `timeout`; None when no time is left, or the tool hung or could not start."""
    if timeout <= 0:
        return None
    try:
        return run(argv, timeout)
    except (subprocess.SubprocessError, OSError):
        return None


def _left(deadline: float) -> float:
    return deadline - time.monotonic()


def _connectors(drm_root: Path, output_id: str) -> list[Path]:
    """The DRM connector directories of `output_id` (`<root>/card*-<output_id>`)."""
    try:
        return sorted(drm_root.glob(f"card*-{glob.escape(output_id)}"))
    except (OSError, ValueError):
        return []


_DDC_POWER: Final = re.compile(rf"^VCP {DDC_POWER_CODE} SNC x([0-9A-Fa-f]{{2}})\s*$", re.MULTILINE)
_I2C_BUS: Final = re.compile(r"i2c-(\d+)")


class DdcCiAdapter:
    method: Final = PowerMethod.DDC_CI

    def __init__(self, run: Runner, *, drm_root: Path = Path("/sys/class/drm"),
                 cache_dir: Path = DDC_CACHE) -> None:
        self._run = run
        self._drm_root = drm_root
        self._cache_dir = cache_dir

    def _bus(self, output_id: str) -> int | None:
        """The i2c bus the connector's `ddc` link names (HDMI-A-1 -> 13 on a Pi 5)."""
        for connector in _connectors(self._drm_root, output_id):
            try:
                target = os.readlink(connector / "ddc")
            except OSError:
                continue
            if (bus := _I2C_BUS.fullmatch(Path(target).name)) is not None:
                return int(bus.group(1))
        return None

    def _ddcutil(self, output_id: str, arguments: Sequence[str], timeout: float) -> str | None:
        """ddcutil's output for one command on the Output's bus; None when it did not answer."""
        bus = self._bus(output_id)
        if bus is None:
            return None
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass                                    # ddcutil works without its cache
        done = _ask(self._run, [DDCUTIL, "--bus", str(bus), "--noconfig", *arguments], timeout)
        return done.stdout if done is not None and done.returncode == 0 else None

    def probe(self, output_id: str, *, timeout: float) -> bool:
        return self.read(output_id, timeout=timeout) is not None

    def read(self, output_id: str, *, timeout: float) -> Power | None:
        output = self._ddcutil(output_id, ["getvcp", DDC_POWER_CODE, "--terse"], timeout)
        value = _DDC_POWER.search(output) if output is not None else None
        if value is None:
            return None
        return Power.ON if int(value.group(1), 16) == DDC_ON else Power.OFF   # 2..5: standby to off

    def set(self, output_id: str, power: Power, *, timeout: float) -> PowerResult:
        deadline = time.monotonic() + timeout
        value = DDC_ON if power is Power.ON else DDC_STANDBY
        self._ddcutil(output_id, ["setvcp", DDC_POWER_CODE, str(value)], timeout)
        # The read-back decides, never the write's exit status: a write that bounced hot-plug may
        # still have taken.
        if self.read(output_id, timeout=_left(deadline)) is power:
            return PowerResult.CONFIRMED
        return PowerResult.DID_NOT_ANSWER

    def showing_other_input(self, output_id: str, *, timeout: float) -> bool | None:
        """Cannot tell: VCP 60 reads an input code, and nothing tells the Pi which code is its own."""
        return None

    def claim_input(self, output_id: str, *, timeout: float) -> bool:
        """Not done, for the same reason; on the test monitor an input write only bounces hot-plug."""
        return False


_CONNECTOR_INFO: Final = re.compile(r"DRM Connector Info\s*:\s*card (\d+), connector (\d+)")
_PHYSICAL_ADDRESS: Final = re.compile(r"Physical Address\s*:\s*([0-9a-fA-F](?:\.[0-9a-fA-F]){3})")
_POWER_STATUS: Final = re.compile(
    r"pwr-state:\s*(on|standby|in transition standby to on|in transition on to standby)")
_CEC_POWER: Final = {"on": Power.ON, "in transition standby to on": Power.ON,
                     "standby": Power.OFF, "in transition on to standby": Power.OFF}
_CEC_NO_ANSWER: Final = "Not Acknowledged"
_CEC_DEVICE: Final = re.compile(r"cec\d+")
_CARD: Final = re.compile(r"card(\d+)-")


class HdmiCecAdapter:
    method: Final = PowerMethod.HDMI_CEC

    def __init__(self, run: Runner, *, dev_root: Path = Path("/dev"),
                 drm_root: Path = Path("/sys/class/drm")) -> None:
        self._run = run
        self._dev_root = dev_root
        self._drm_root = drm_root
        self._devices: dict[str, Path] = {}         # an Output's cec device, once found

    def _device(self, output_id: str, deadline: float) -> Path | None:
        """The cecN whose DRM connector info names the Output's connector (card, connector id)."""
        if output_id in self._devices:
            return self._devices[output_id]
        wanted = set()
        for connector in _connectors(self._drm_root, output_id):
            card = _CARD.match(connector.name)
            try:
                connector_id = int((connector / "connector_id").read_text().strip())
            except (OSError, ValueError):
                continue
            if card is not None:
                wanted.add((int(card.group(1)), connector_id))
        if not wanted:
            return None
        try:
            devices = sorted(path for path in self._dev_root.iterdir() if _CEC_DEVICE.fullmatch(path.name))
        except OSError:
            return None
        for device in devices:
            done = _ask(self._run, [CEC_CTL, "-d", str(device)], _left(deadline))
            info = _CONNECTOR_INFO.search(done.stdout) if done is not None else None
            if info is not None and (int(info.group(1)), int(info.group(2))) in wanted:
                self._devices[output_id] = device
                return device
        return None

    def _cec(self, output_id: str, arguments: Sequence[str], deadline: float) -> str | None:
        """cec-ctl's output for one message sent as a Playback device; None when nothing answered."""
        device = self._device(output_id, deadline)
        if device is None:
            return None
        done = _ask(self._run, [CEC_CTL, "-d", str(device), "--playback", *arguments], _left(deadline))
        if done is None or done.returncode != 0 or _CEC_NO_ANSWER in done.stdout + done.stderr:
            return None
        return done.stdout

    def probe(self, output_id: str, *, timeout: float) -> bool:
        return self.read(output_id, timeout=timeout) is not None

    def read(self, output_id: str, *, timeout: float) -> Power | None:
        output = self._cec(output_id, ["--to", "0", "--give-device-power-status"], time.monotonic() + timeout)
        status = _POWER_STATUS.search(output) if output is not None else None
        return None if status is None else _CEC_POWER[status.group(1)]

    def set(self, output_id: str, power: Power, *, timeout: float) -> PowerResult:
        deadline = time.monotonic() + timeout
        message = "--image-view-on" if power is Power.ON else "--standby"
        if self._cec(output_id, ["--to", "0", message], deadline) is None:
            return PowerResult.DID_NOT_ANSWER
        if self.read(output_id, timeout=_left(deadline)) is power:
            return PowerResult.CONFIRMED
        return PowerResult.DID_NOT_ANSWER

    def showing_other_input(self, output_id: str, *, timeout: float) -> bool | None:
        """Cannot tell: the active source is a broadcast reply the one-shot tool does not wait for."""
        return None

    def claim_input(self, output_id: str, *, timeout: float) -> bool:
        """Broadcast Active Source with this Pi's physical address (#117)."""
        deadline = time.monotonic() + timeout
        output = self._cec(output_id, [], deadline)
        address = _PHYSICAL_ADDRESS.search(output) if output is not None else None
        if address is None or address.group(1).lower() == "f.f.f.f":
            return False
        return self._cec(output_id, ["--active-source", f"phys-addr={address.group(1)}"], deadline) is not None

    def close(self, timeout: float = 2.0) -> None:
        """Give back the logical addresses this adapter claimed (`--clear`)."""
        for device in sorted(set(self._devices.values())):
            _ask(self._run, [CEC_CTL, "-d", str(device), "--clear"], timeout)


class SignalOffAdapter:
    method: Final = PowerMethod.SIGNAL_OFF

    def __init__(self, compositor: OutputPowerControl) -> None:
        self._compositor = compositor

    def probe(self, output_id: str, *, timeout: float) -> bool:
        return self._compositor.output_powered(output_id) is not None

    def read(self, output_id: str, *, timeout: float) -> Power | None:
        return self._compositor.output_powered(output_id)

    def set(self, output_id: str, power: Power, *, timeout: float) -> PowerResult:
        if self._compositor.output_power(output_id, power, timeout=timeout):
            return PowerResult.SIGNAL_STOPPED
        return PowerResult.DID_NOT_ANSWER

    def showing_other_input(self, output_id: str, *, timeout: float) -> bool | None:
        return None                                 # the compositor cannot see the panel's input

    def claim_input(self, output_id: str, *, timeout: float) -> bool:
        return False
