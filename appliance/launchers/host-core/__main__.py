"""The host-core launcher: runs appliance.host.host_runner as __main__ (decision 0019).

Run as `python3 -I -B /usr/lib/photo-wall/node/host-core`. PATH is the package directories
appliance.host.host_runner's imports reach, sorted; the build refuses any other (scripts/import_check.py).
"""
import runpy
import sys

PATH: tuple[str, ...] = (
    "/usr/lib/photo-wall/common",
    "/usr/lib/photo-wall/node-apps",
    "/usr/lib/photo-wall/node-central-session",
    "/usr/lib/photo-wall/node-host",
    "/usr/lib/photo-wall/node-kernel",
    "/usr/lib/photo-wall/node-manager",
    "/usr/lib/photo-wall/uplink",
)
ENTRY: str = "appliance.host.host_runner"

sys.path[:0] = PATH
runpy.run_module(ENTRY, run_name="__main__", alter_sys=True)
