"""A scripted display behind a real fake `ddcutil` executable, shared by the DDC/CI port tests and
the picture controller tests (the picture split, slice S1).

The tool records every call with its start and end time and answers from the scripted display:
continuous codes (`vcp`: code -> [current, maximum]), simple non-continuous ones (`snc`: code ->
byte), power D6, whether it answers at all, whether a write takes, how long a read or a write
lasts (`read_seconds`, for every read but power's), and which codes hang a read (`hang`: a getvcp naming one sleeps `hang_seconds`). It runs
through the production `run_tool` and the real `DdcChannel`. Codes are the terse format's
upper-case hex; `getvcp` takes several codes at once and answers each on its own line, an unknown
code with `ERR`, as ddcutil does.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from node.display.fake_display_state import SOURCE as STATE_SOURCE
from node.display.fake_display_state import changing_state

from appliance.display_host.ddc import DdcChannel
from appliance.display_host.power_methods import DDCUTIL, run_tool

OUTPUT = "HDMI-A-1"
OTHER = "HDMI-A-2"

# Its state goes through `fake_display_state`, whose source the tool embeds ahead of this.
FAKE_DDCUTIL = '''
import json, sys, time
state_path, log_path = {state!r}, {log!r}
started = time.monotonic()
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
            time.sleep(state["read_seconds"])          # a picture read's time; power's own read is quick
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
    elif args[0] == "setvcp":
        if not state["answer"]:
            print("No monitor detected on bus", file=sys.stderr); sys.exit(1)
        time.sleep(state["write_seconds"])
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
                    write_seconds=0, read_seconds=0, hang=[], hang_seconds=0, mute_after_write=False)
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
        self.channel = DdcChannel(self.run, drm_root=self.drm, cache_dir=root / "cache")

    def script(self, **values: object) -> None:
        with changing_state(self.state_path) as state:
            state.update(values)

    def vcp(self, code: str, current: int, maximum: int) -> None:
        with changing_state(self.state_path) as state:
            state["vcp"][code] = [current, maximum]

    def run(self, argv, timeout):
        """The production runner, with the tool's path pointed at the fake."""
        return run_tool([str(self.tool), *argv[1:]], timeout)

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
