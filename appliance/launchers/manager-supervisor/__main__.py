"""The manager-supervisor launcher: runs appliance.node.manager_launcher as __main__ (decision 0019).

Run as `python3 -I -B /usr/lib/photo-wall/node/manager-supervisor`. PATH is the package directories
appliance.node.manager_launcher's imports reach, sorted; the build refuses any other (scripts/import_check.py).
"""
import runpy
import sys

PATH: tuple[str, ...] = (
    "/usr/lib/photo-wall/common",
    "/usr/lib/photo-wall/node-apps",
    "/usr/lib/photo-wall/node-host",
    "/usr/lib/photo-wall/node-kernel",
    "/usr/lib/photo-wall/node-manager",
)
ENTRY: str = "appliance.node.manager_launcher"

sys.path[:0] = PATH
runpy.run_module(ENTRY, run_name="__main__", alter_sys=True)
