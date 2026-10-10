"""The root-import launcher: runs appliance.apps.root_import as __main__ (decision 0019).

Run as `python3 -I -B /usr/lib/photo-wall/node/root-import`. PATH is the package directories
appliance.apps.root_import's imports reach, sorted; the build refuses any other (scripts/import_check.py).
"""
import runpy
import sys

PATH: tuple[str, ...] = (
    "/usr/lib/photo-wall/common",
    "/usr/lib/photo-wall/node-apps",
    "/usr/lib/photo-wall/node-kernel",
    "/usr/lib/photo-wall/uplink",
)
ENTRY: str = "appliance.apps.root_import"

sys.path[:0] = PATH
runpy.run_module(ENTRY, run_name="__main__", alter_sys=True)
