"""The display-signal-range launcher: runs appliance.display_host.signal_range as __main__ (decision
0019; display tuning, decision 0024).

Run as root by photo-wall-display.service's `ExecStartPre=+-/usr/bin/python3 -I -B
/usr/lib/photo-wall/node/display-signal-range`, before Weston takes the screen. PATH is the package
directories appliance.display_host.signal_range's imports reach, sorted; the build refuses any other
(scripts/import_check.py).
"""
import runpy
import sys

PATH: tuple[str, ...] = (
    "/usr/lib/photo-wall/common",
    "/usr/lib/photo-wall/node-display",
    "/usr/lib/photo-wall/node-kernel",
)
ENTRY: str = "appliance.display_host.signal_range"

sys.path[:0] = PATH
runpy.run_module(ENTRY, run_name="__main__", alter_sys=True)
