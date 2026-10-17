"""A scripted display behind a real fake `ddcutil` executable, shared by the DDC/CI port tests and
the picture controller tests (the picture split, slice S1).

The tool records every call with its start and end time and answers from the scripted display:
continuous codes (`vcp`: code -> [current, maximum]), simple non-continuous ones (`snc`: code ->
byte), the `capabilities` text it prints, power D6, whether it answers at all, whether a write takes, how long a read or a write
lasts (`read_seconds`, for every read but power's), and which codes hang a read (`hang`: a getvcp naming one sleeps `hang_seconds`). It runs
through the production `run_tool` and the real `DdcChannel`.

A read or a write lasts its scripted time from the moment the channel ran the tool (`Display.run`
passes that instant), not from when the tool's own interpreter got going: a loaded test runner can
take a large part of a second to start a Python process, and a 1 s read that started late would
run past PICTURE_HOLD_SECONDS and be cut, although the display took 1 s. `Display.runs` records
every command the channel ran, with that instant, in order: the moment it took its port. Codes are the terse format's
upper-case hex; `getvcp` takes several codes at once and answers each on its own line, an unknown
code with `ERR`, as ddcutil does.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from node.display.fake_display_state import SOURCE as STATE_SOURCE
from node.display.fake_display_state import changing_state

from appliance.display_host.ddc import DdcChannel, PortLock, Urgency
from appliance.display_host.power_methods import DDCUTIL, run_tool

OUTPUT = "HDMI-A-1"
OTHER = "HDMI-A-2"

# A recorded `ddcutil capabilities` (a Wacom Cintiq Pro 32, MCCS 2.2; the linuxwacom wiki,
# "Adjusting Display Settings"): colour preset 14 names six values; DC is not listed; maker codes
# list values without names.
RECORDED_CAPABILITIES = """\
MCCS version: 2.2
Commands:
   Command: 01 (VCP Request)
   Command: 02 (VCP Response)
   Command: 03 (VCP Set)
   Command: 07 (Timing Request)
   Command: 0c (Save Settings)
   Command: e3 (Capabilities Reply)
   Command: f3 (Capabilities Request)
VCP Features:
   Feature: 02 (New control value)
   Feature: 04 (Restore factory defaults)
   Feature: 05 (Restore factory brightness/contrast defaults)
   Feature: 08 (Restore color defaults)
   Feature: 10 (Luminosity)
   Feature: 12 (Contrast)
   Feature: 14 (Select color preset)
      Values:
         04: 5000 K
         05: 6500 K
         08: 9300 K
         09: 10000 K
         0b: User 1
         0c: User 2
   Feature: 16 (Video gain: Red)
   Feature: 18 (Video gain: Green)
   Feature: 1A (Video gain: Blue)
   Feature: 59 (6 axis saturation: Red)
   Feature: 5A (6 axis saturation: Yellow)
   Feature: 5B (6 axis saturation: Green)
   Feature: 5C (6 axis saturation: Cyan)
   Feature: 5D (6 axis saturation: Blue)
   Feature: 5E (6 axis saturation: Magenta)
   Feature: 72 (Gamma)
      Values:
         Invalid gamma descriptor: fd 05 28
   Feature: 87 (Sharpness)
   Feature: 8A (Color Saturation)
   Feature: 90 (Hue)
   Feature: 9B (6 axis hue control: Red)
   Feature: 9C (6 axis hue control: Yellow)
   Feature: 9D (6 axis hue control: Green)
   Feature: 9E (6 axis hue control: Cyan)
   Feature: 9F (6 axis hue control: Blue)
   Feature: A0 (6 axis hue control: Magenta)
   Feature: AC (Horizontal frequency)
   Feature: AE (Vertical frequency)
   Feature: B6 (Display technology type)
   Feature: C0 (Display usage time)
   Feature: C6 (Application enable key)
   Feature: C8 (Display controller type)
   Feature: C9 (Display firmware level)
   Feature: CC (OSD Language)
      Values:
         01: Chinese (traditional, Hantai)
         02: English
         03: French
         04: German
         05: Italian
         06: Japanese
         07: Korean
         08: Portuguese (Portugal)
         09: Russian
         0a: Spanish
         0d: Chinese (simplified / Kantai)
         14: Dutch
         1e: Polish
         26: Unrecognized value
   Feature: D6 (Power mode)
      Values:
         01: DPM: On,  DPMS: Off
         04: DPM: Off, DPMS: Off
         05: Write only value to turn off display
   Feature: DF (VCP Version)
   Feature: E0 (manufacturer specific feature)
      Values: 00 01 02 03 (interpretation unavailable)
   Feature: E1 (manufacturer specific feature)
      Values: 00 01 (interpretation unavailable)
   Feature: E2 (manufacturer specific feature)
      Values: 01 02 03 (interpretation unavailable)
   Feature: E4 (manufacturer specific feature)
   Feature: E5 (manufacturer specific feature)
   Feature: E6 (manufacturer specific feature)
   Feature: E7 (manufacturer specific feature)
      Values: 00 01 (interpretation unavailable)
   Feature: E8 (manufacturer specific feature)
      Values: 00 01 (interpretation unavailable)
   Feature: E9 (manufacturer specific feature)
      Values: 00 01 (interpretation unavailable)
   Feature: EA (manufacturer specific feature)
      Values: 00 01 (interpretation unavailable)
   Feature: EB (manufacturer specific feature)
   Feature: EC (manufacturer specific feature)
      Values: 60 (interpretation unavailable)
   Feature: ED (manufacturer specific feature)
      Values: 00 01 02 (interpretation unavailable)
   Feature: F0 (manufacturer specific feature)
      Values: 00 01 02 03 04 05 06 07 08 09 0a 0b 0c 0d (interpretation unavailable)
   Feature: F2 (manufacturer specific feature)
   Feature: F3 (manufacturer specific feature)
      Values: 00 01 (interpretation unavailable)
"""

# Its state goes through `fake_display_state`, whose source the tool embeds ahead of this.
FAKE_DDCUTIL = '''
import json, sys, time
state_path, log_path = {state!r}, {log!r}
started = time.monotonic()
ran_at = float(sys.argv[sys.argv.index("--ran-at") + 1])
def last(seconds):
    time.sleep(max(0.0, seconds - (time.monotonic() - ran_at)))
args = sys.argv[sys.argv.index("--noconfig") + 1:]
bus = sys.argv[sys.argv.index("--bus") + 1]
state = read_state(state_path)
def record(**entry):
    with open(log_path, "a") as log:
        log.write(json.dumps({{"bus": bus, "args": args, **entry}}) + "\\n")
record(at="start", time=started)
try:
    if args[0] == "getvcp":
        codes = [code.upper() for code in args[1:] if not code.startswith("--")]
        if any(code in state["hang"] for code in codes):
            time.sleep(state["hang_seconds"])
        if codes != ["D6"]:
            last(state["read_seconds"])                # a picture read's time; power's own read is quick
        if not state["answer"]:
            print("No monitor detected on bus", file=sys.stderr); sys.exit(1)
        for code in codes:
            if code == "D6":
                print("VCP D6 SNC x%02x" % state["power"])
            elif code in state["vcp"]:
                current, maximum = state["vcp"][code]
                print("VCP %s C %d %d" % (code, current, maximum))
            elif code in state["snc"]:
                print("VCP %s SNC x%02x" % (code, state["snc"][code]))
            else:
                print("VCP %s ERR" % code)
    elif args[0] == "capabilities":
        if not state["answer"]:
            print("No monitor detected on bus", file=sys.stderr); sys.exit(1)
        last(state["read_seconds"])
        print(state["capabilities"], end="")
    elif args[0] == "setvcp":
        if not state["answer"]:
            print("No monitor detected on bus", file=sys.stderr); sys.exit(1)
        last(state["write_seconds"])
        with changing_state(state_path) as state:
            code, value = args[1].upper(), int(args[2])
            if code == "D6":
                state["power"] = {{1: 1, 4: 2}}[value]
            elif state["take"] and code in state["vcp"]:
                state["vcp"][code][0] = value
            elif state["take"] and code in state["snc"]:
                state["snc"][code] = value
            if state["mute_after_write"]:
                state["answer"] = False
finally:
    record(at="end", time=time.monotonic())
'''


class RecordingPortLock(PortLock):
    """The real PortLock, recording in `turns` each waiter's ("queued", urgency, instant) as it
    joins its queue and each holder's ("released", urgency, instant) as it lets the port go. Each
    record is taken inside the lock's own condition (a re-entrant lock: its wait releases it
    whole), so `turns` is the order the lock saw, not the test runner's timing."""

    def __init__(self, turns: list[tuple[str, Urgency, float]]) -> None:
        super().__init__()
        self.turns = turns

    def acquire(self, urgency: Urgency, timeout: float) -> bool:
        with self._turn:
            self.turns.append(("queued", urgency, time.monotonic()))
            return super().acquire(urgency, timeout)

    def release(self) -> None:
        with self._turn:
            if self._held is not None:
                self.turns.append(("released", self._held, time.monotonic()))
            super().release()


class Display:
    """One scripted display on HDMI-A-1 (bus 13) and HDMI-A-2 (bus 14), and its ddcutil. It reads
    brightness (10) as 60 of 100 and contrast (12) as 50 of 100, and answers nothing else on the
    picture list until scripted to."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.state_path = root / "display.json"
        self.log_path = root / "calls.log"
        self.log_path.touch()
        self.script(vcp={"10": [60, 100], "12": [50, 100]}, snc={}, power=1, answer=True, take=True,
                    write_seconds=0, read_seconds=0, hang=[], hang_seconds=0, mute_after_write=False,
                    capabilities="")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        tool = bin_dir / Path(DDCUTIL).name
        tool.write_text(f"#!{sys.executable} -I\n" + STATE_SOURCE
                        + FAKE_DDCUTIL.format(state=str(self.state_path), log=str(self.log_path)))
        tool.chmod(0o755)
        self.tool = tool
        self.drm = root / "drm"
        for port, bus in ((OUTPUT, 13), (OTHER, 14)):
            directory = self.drm / f"card1-{port}"
            directory.mkdir(parents=True)
            (root / f"i2c-{bus}").mkdir()
            (directory / "ddc").symlink_to(root / f"i2c-{bus}")
        self.runs: list[tuple[float, list[str]]] = []      # (when it took the port, its command)
        self.channel = DdcChannel(self.run, drm_root=self.drm, cache_dir=root / "cache")
        self.turns: list[tuple[str, Urgency, float]] = []   # HDMI-A-1's lock, as it saw them
        self.channel._ports[OUTPUT] = RecordingPortLock(self.turns)

    def script(self, **values: object) -> None:
        with changing_state(self.state_path) as state:
            state.update(values)

    def vcp(self, code: str, current: int, maximum: int) -> None:
        with changing_state(self.state_path) as state:
            state["vcp"][code] = [current, maximum]

    def run(self, argv, timeout):
        """The production runner, with the tool's path pointed at the fake and the instant it ran
        (the channel calls this holding the port), recorded in `runs` and passed to the tool."""
        ran_at = time.monotonic()
        command = argv[list(argv).index("--noconfig") + 1:]
        self.runs.append((ran_at, [arg for arg in command if arg != "--terse"]))
        return run_tool([str(self.tool), "--ran-at", repr(ran_at), *argv[1:]], timeout)

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def started(self) -> list[list[str]]:
        """Every command's arguments (without `--terse`), in the order they started."""
        return [[arg for arg in call["args"] if arg != "--terse"] for call in self.calls() if call["at"] == "start"]

    def writes(self) -> list[list[str]]:
        """Every setvcp's code and value, in the order they started."""
        return [args[1:] for args in self.started() if args[0] == "setvcp"]

    def intervals(self, bus: str) -> list[tuple[float, float]]:
        """(start, end) of every command on `bus` (each must have finished: none killed)."""
        calls = [call for call in self.calls() if call["bus"] == bus]
        starts = [call["time"] for call in calls if call["at"] == "start"]
        ends = [call["time"] for call in calls if call["at"] == "end"]
        return list(zip(sorted(starts), sorted(ends), strict=True))
