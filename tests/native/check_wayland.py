"""Verify synthetic app IDs precede buffers and enter distinct Weston Outputs."""
from __future__ import annotations

import re
import sys
from pathlib import Path

lines = Path(sys.argv[1]).read_text().splitlines()
xdg_surfaces, top_levels, assignments, enters, first_buffers = {}, {}, {}, {}, {}
for index, line in enumerate(lines):
    match = re.search(r"get_xdg_surface\(new id xdg_surface@(\d+), wl_surface@(\d+)\)", line)
    if match:
        xdg_surfaces[match[1]] = match[2]
    match = re.search(r"xdg_surface@(\d+)\.get_toplevel\(new id xdg_toplevel@(\d+)\)", line)
    if match:
        top_levels[match[2]] = xdg_surfaces[match[1]]
    match = re.search(r'xdg_toplevel@(\d+)\.set_app_id\("(org.photowall.[^"]+)"\)', line)
    if match:
        assignments[match[2]] = top_levels[match[1]], index
    match = re.search(r"wl_surface@(\d+)\.attach\(wl_buffer@", line)
    if match:
        first_buffers.setdefault(match[1], index)
    match = re.search(r"wl_surface@(\d+)\.enter\(wl_output@(\d+)\)", line)
    if match:
        enters.setdefault(match[1], set()).add(match[2])
outputs = []
for name in ("org.photowall.hdmi1", "org.photowall.hdmi2"):
    surface, app_index = assignments[name]
    assert app_index < first_buffers[surface], f"{name} app ID followed first content buffer"
    outputs.append(enters.get(surface, set()))
if "--two-outputs" in sys.argv:
    assert all(len(output) == 1 for output in outputs), outputs
    assert outputs[0].isdisjoint(outputs[1]), outputs
print("Wayland verified app IDs before first content buffers" +
      (" and separate kiosk Outputs." if "--two-outputs" in sys.argv else "."))
