"""The Player's launcher: runs player.service as __main__ (decision 0019).

Run as `python3 -I -B /usr/lib/photo-wall/player --config /etc/photo-wall/public.json`, the app
root's one program (photo-wall-player). PATH is the package directories player.service's imports
reach, sorted; the build refuses any other, and any Node context directory (scripts/import_check.py).
"""
import runpy
import sys

PATH: tuple[str, ...] = (
    "/usr/lib/photo-wall/common",
    "/usr/lib/photo-wall/player",
    "/usr/lib/photo-wall/uplink",
)
ENTRY: str = "player.service"

sys.path[:0] = PATH
runpy.run_module(ENTRY, run_name="__main__", alter_sys=True)
