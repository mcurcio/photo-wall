"""The node-bootstrap launcher: runs appliance.boot.node_bootstrap as __main__ (decision 0019).

Run as `python3 -I -B /usr/lib/photo-wall/node/node-bootstrap`. PATH is the package directories
appliance.boot.node_bootstrap's imports reach, sorted; the build refuses any other (scripts/import_check.py).
"""
import runpy
import sys

PATH: tuple[str, ...] = (
    "/usr/lib/photo-wall/common",
    "/usr/lib/photo-wall/node-apps",
    "/usr/lib/photo-wall/node-boot",
    "/usr/lib/photo-wall/node-kernel",
    "/usr/lib/photo-wall/node-manager",
    "/usr/lib/photo-wall/uplink",
)
ENTRY: str = "appliance.boot.node_bootstrap"

sys.path[:0] = PATH
runpy.run_module(ENTRY, run_name="__main__", alter_sys=True)
