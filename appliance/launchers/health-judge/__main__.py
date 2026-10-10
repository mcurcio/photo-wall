"""The health-judge launcher: runs appliance.health.runner as __main__ (decision 0019).

Run as `python3 -I -B /usr/lib/photo-wall/node/health-judge`. PATH is the package directories
appliance.health.runner's imports reach, sorted; the build refuses any other (scripts/import_check.py).
"""
import runpy
import sys

PATH: tuple[str, ...] = (
    "/usr/lib/photo-wall/common",
    "/usr/lib/photo-wall/node-display",
    "/usr/lib/photo-wall/node-health",
    "/usr/lib/photo-wall/node-kernel",
)
ENTRY: str = "appliance.health.runner"

sys.path[:0] = PATH
runpy.run_module(ENTRY, run_name="__main__", alter_sys=True)
