"""Which framebuffers are Weston's own composites (fix round 2): read from its drm-backend debug
stream (`weston-debug drm-backend`, Weston started with --debug), the only witness of which
framebuffer the renderer drew into. The kernel's state dump names every framebuffer as allocated
by Weston, a client's buffer and the renderer's alike, so it cannot tell them apart. Pure."""
from __future__ import annotations

import re
from typing import Final

_BLOCK: Final = re.compile(r"^Weston scene graph at ", re.MULTILINE)
_USING: Final = re.compile(r"using renderer FB ID (\d+)")
_FB_ID: Final = re.compile(r"\[PLANE:(\d+)\] \d+ \(FB_ID\) -> (\d+)")
_MODE: Final = re.compile(r"\[repaint\] Using (\S+) state composition")
_APPLYING: Final = "[atomic] applying output"


def renderer_framebuffers(log: str) -> frozenset[int]:
    """Every framebuffer id Weston's renderer drew into, from the drm-backend stream `log`: those
    a mixed state borrows ("using renderer FB ID"), and those a repaint that composited (mixed or
    render-only) applied to the plane the renderer's framebuffer goes on (in a render-only state,
    every plane it applies). A stream cut off mid-block reads up to the cut."""
    found: set[int] = set()
    for block in _BLOCK.split(log):
        found.update(int(number) for number in _USING.findall(block))
        mode = _MODE.search(block)
        if mode is None or mode.group(1) == "plane-only":
            continue
        tested, _, applied = block.partition(_APPLYING)
        if mode.group(1) == "render-only":
            found.update(int(fb) for _plane, fb in _FB_ID.findall(applied))
            continue
        borrowed = {int(number) for number in _USING.findall(tested)}
        scanout = {plane for plane, fb in _FB_ID.findall(tested) if int(fb) in borrowed}
        found.update(int(fb) for plane, fb in _FB_ID.findall(applied) if plane in scanout)
    return frozenset(found)


__all__ = ["renderer_framebuffers"]
