"""The Player's renderer, composed (render redesign S3, run ledger .claude/runs/render-planes/s3-page.md,
bead B6): the one place the new path's parts are built and wired, so player/service.py `main` only
asks for a renderer. It composes and wires; it computes no rule.

What it builds, on the main loop's thread (the GPU's context and every screen call stay there):
player/plane_screen.py `PlaneScreens` on Display Host's Weston (`$WAYLAND_DISPLAY`), player/gpu.py
`Gpu`, player/fit_pass.py `FitPass` and player/pattern_pass.py `PatternPass` on its GL,
player/plane_renderer.py `PlaneRenderer` with player/decode.py `StillDecode` and `VideoFrames` (each
video's new-frame callback handed from its streaming thread to the main loop through `dispatcher`,
then a frame turn woken), and player/mainloop.py `FrameLoop` on the screens' connection, whose turn
reads the connection, presents (`PlaneRenderer.frame`) and then finishes (`PlaneRenderer.finish`).
It also measures the connected Outputs' modes (player/output_discovery.py `measured`), waiting up to
`mode_wait` seconds for Weston to announce them, so the Player enrolls with their sizes.

Any part that cannot be made (no Weston, no render node, a GL program that does not link) leaves
the Player registrable without a picture: player/rendering.py `UnavailableRenderer`, the fault
"native_initialization", and the Outputs as discovered.
"""
from __future__ import annotations

from typing import Final

from contracts.enrollment import OutputReport
from player.luts import LutSource
from player.mainloop import MainLoopDispatcher
from player.rendering import Renderer

MODE_WAIT: Final = 2.0
"""Seconds `start_renderer` waits for Weston to announce every connected Output's mode."""


class RendererRoot:
    """What `start_renderer` made: the `renderer` the service drives, the `outputs` it enrolls
    with (their modes measured), and the `fault` to report (None: the new path is up)."""

    renderer: Renderer
    outputs: tuple[OutputReport, ...]
    fault: str | None

    def close(self) -> None:
        """Stop the frame loop, then close the renderer, the fit and pattern passes, the GPU and the
        screens, in that order (each once; safe after a partial start)."""
        raise NotImplementedError("render-planes S3 bead B6")


def start_renderer(glib, dispatcher: MainLoopDispatcher, luts: LutSource,
                   outputs: tuple[OutputReport, ...], *, socket: str | None = None,
                   mode_wait: float = MODE_WAIT) -> RendererRoot:
    """Build and wire the new path (module docstring) for the connected Outputs among `outputs`;
    `socket` is Weston's (None: $WAYLAND_DISPLAY). Never raises for a part that cannot be made: the
    root then holds `UnavailableRenderer` and the fault "native_initialization" (no connected
    Output: `UnavailableRenderer` and no fault, as discovery already reported "outputs_missing")."""
    raise NotImplementedError("render-planes S3 bead B6")


__all__ = ["MODE_WAIT", "RendererRoot", "start_renderer"]
