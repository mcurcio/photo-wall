"""The app-broker launcher: runs appliance.apps.broker_runner as __main__ (decision 0019).

Run as `python3 -I -B /usr/lib/photo-wall/node/app-broker`. PATH is the package directories
appliance.apps.broker_runner's imports reach, sorted; the build refuses any other (scripts/import_check.py).
"""
import runpy
import sys

PATH: tuple[str, ...] = (
    "/usr/lib/photo-wall/common",
    "/usr/lib/photo-wall/node-apps",
    "/usr/lib/photo-wall/node-central-session",
    "/usr/lib/photo-wall/node-kernel",
    "/usr/lib/photo-wall/node-manager",
)
ENTRY: str = "appliance.apps.broker_runner"

sys.path[:0] = PATH
runpy.run_module(ENTRY, run_name="__main__", alter_sys=True)
