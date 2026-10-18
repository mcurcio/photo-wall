"""The Player's renderer, composed (render redesign S3, run ledger .claude/runs/render-planes/s3-page.md,
bead B6): the one place the new path's parts are built and wired, so player/service.py `main` only
asks for a renderer. It composes and wires; it computes no rule.

What it builds, on the main loop's thread (the GPU's context and every screen call stay there):
player/plane_screen.py `PlaneScreens` on Display Host's Weston (`$WAYLAND_DISPLAY`), player/gpu.py
`Gpu`, player/fit_pass.py `FitPass` and player/pattern_pass.py `PatternPass` on its GL,
player/plane_renderer.py `PlaneRenderer` with player/decode.py `StillDecode` and `VideoFrames` (each
video's new-frame callback handed from its streaming thread to the main loop through its own lane of
`dispatcher`, then a frame turn woken; a full lane drops the post, as the next frame comes), and
player/mainloop.py `FrameLoop` on the screens' connection, whose turn reads the connection, presents
(`PlaneRenderer.frame`) and then finishes (`PlaneRenderer.finish`), answering `PlaneRenderer.due`.
It also measures the connected Outputs' modes (player/output_discovery.py `measured`), dispatching
the connection until Weston announced every one or `mode_wait` seconds passed, so the Player enrolls
with their sizes.

Any part that cannot be made (no Weston, no render node, a GL program that does not link) leaves
the Player registrable without a picture: player/rendering.py `UnavailableRenderer`, the fault
"native_initialization", and the Outputs as discovered.
"""
from __future__ import annotations

import logging
import select
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from contracts.enrollment import OutputReport
from player.decode import StillDecode, VideoFrames
from player.fit_pass import FitPass
from player.gpu import Gpu
from player.luts import LutSource
from player.mainloop import FrameLoop, MainLoopDispatcher
from player.output_discovery import measured
from player.pattern_pass import PatternPass
from player.plane_renderer import PlaneRenderer
from player.plane_screen import PlaneScreens
from player.rendering import Renderer, UnavailableRenderer

MODE_WAIT: Final = 2.0
"""Seconds `start_renderer` waits for Weston to announce every connected Output's mode."""
VIDEO_LANE: Final = 2
"""A video's new-frame posts held at once: one queued while one runs; more are dropped."""
LOGGER = logging.getLogger(__name__)
# The order `RendererRoot.close` closes the parts in (its docstring).
_CLOSING: Final = ("loop", "renderer", "fit", "pattern", "gpu", "screens")


def _readable(fd: int, timeout: float) -> None:
    """Wait up to `timeout` seconds for `fd` to be readable (the compositor spoke)."""
    select.select([fd], [], [], timeout)


class RendererRoot:
    """What `start_renderer` made: the `renderer` the service drives, the `outputs` it enrolls
    with (their modes measured), and the `fault` to report (None: the new path is up)."""

    renderer: Renderer
    outputs: tuple[OutputReport, ...]
    fault: str | None

    def __init__(self, renderer: Renderer, outputs: tuple[OutputReport, ...], fault: str | None = None,
                 parts: dict[str, Any] | None = None) -> None:
        self.renderer, self.outputs, self.fault = renderer, outputs, fault
        self._parts = dict(parts or {})

    def close(self) -> None:
        """Stop the frame loop, then close the renderer, the fit and pattern passes, the GPU and the
        screens, in that order (each once; safe after a partial start)."""
        for name in _CLOSING:
            part = self._parts.pop(name, None)
            if part is None:
                continue
            try:
                part.stop() if name == "loop" else part.close()
            except Exception:
                LOGGER.exception("player: closing the %s failed", name)


def _modes(screens: PlaneScreens, output_ids: tuple[str, ...], wait: float) -> dict[str, tuple[int, int]]:
    """Each of `output_ids`' mode as built, dispatching the connection until every one is known or
    `wait` seconds passed (an Output still unknown then is left out)."""
    deadline = time.monotonic() + wait
    while True:
        screens.dispatch()
        modes = {output_id: mode for output_id in output_ids if (mode := screens.mode(output_id)) is not None}
        remaining = deadline - time.monotonic()
        if len(modes) == len(output_ids) or remaining <= 0:
            return modes
        _readable(screens.fd, remaining)


def start_renderer(glib, dispatcher: MainLoopDispatcher, luts: LutSource,
                   outputs: tuple[OutputReport, ...], *, socket: str | None = None,
                   mode_wait: float = MODE_WAIT) -> RendererRoot:
    """Build and wire the new path (module docstring) for the connected Outputs among `outputs`;
    `socket` is Weston's (None: $WAYLAND_DISPLAY). Never raises for a part that cannot be made: the
    root then holds `UnavailableRenderer` and the fault "native_initialization" (no connected
    Output: `UnavailableRenderer` and no fault, as discovery already reported "outputs_missing")."""
    connected = tuple(output.output_id for output in outputs if output.connected)
    if not connected:
        return RendererRoot(UnavailableRenderer(), outputs)
    parts: dict[str, Any] = {}
    try:
        screens = parts["screens"] = PlaneScreens(socket=socket)
        modes = _modes(screens, connected, mode_wait)
        gpu = parts["gpu"] = Gpu()
        fit = parts["fit"] = FitPass(gpu.gl)
        patterns = parts["pattern"] = PatternPass(gpu.gl)
        loop: FrameLoop | None = None

        def videos(path: Path, on_frame: Callable[[], None]) -> VideoFrames:
            lane = dispatcher.lane(VIDEO_LANE)

            def heard() -> None:            # on the main loop: the renderer's callback, then a turn
                on_frame()
                if loop is not None:
                    loop.wake()

            # On a streaming thread: only post (a full lane refuses it: dropped, the next frame comes).
            return VideoFrames(path, lambda: lane(heard))

        renderer = parts["renderer"] = PlaneRenderer(
            screens, gpu, fit, patterns, luts, stills=StillDecode, videos=videos, gl_renderer=gpu.renderer)

        def turn() -> float | None:
            with screens.turning():
                renderer.frame()
                renderer.finish()
                return renderer.due()

        loop = parts["loop"] = FrameLoop(glib, screens.fd, turn)
        screens.on_read = loop.wake         # a read outside the turn may hold the compositor's answer
    except Exception:
        LOGGER.exception("player: the renderer could not be made")
        RendererRoot(UnavailableRenderer(), outputs, parts=parts).close()
        return RendererRoot(UnavailableRenderer(), outputs, "native_initialization")
    return RendererRoot(renderer, measured(outputs, modes), parts=parts)


__all__ = ["MODE_WAIT", "VIDEO_LANE", "RendererRoot", "start_renderer"]
