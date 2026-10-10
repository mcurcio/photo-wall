"""The display-controller launcher: runs appliance.display_host.runner as __main__ (decision 0019).

Run as `python3 -I -B /usr/lib/photo-wall/node/display-controller`. PATH is the package directories
appliance.display_host.runner's imports reach, sorted; the build refuses any other (scripts/import_check.py).
"""
import runpy
import sys

PATH: tuple[str, ...] = (
    "/usr/lib/photo-wall/common",
    "/usr/lib/photo-wall/node-central-session",
    "/usr/lib/photo-wall/node-display",
    "/usr/lib/photo-wall/node-kernel",
)
ENTRY: str = "appliance.display_host.runner"

sys.path[:0] = PATH
runpy.run_module(ENTRY, run_name="__main__", alter_sys=True)
