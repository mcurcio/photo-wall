"""The private overlay client: the one Wayland client the shell spawns with a private socket.

Weston's shell (native/shell.c `start_diagnostic`) execs the `diagnostic-client` launcher with
WAYLAND_SOCKET set and binds `pw_diagnostic_manager_v1` only for this client. Per Output it keeps
one surface: an `output` configure paints the slate (render_slate), an `overlay` configure the
calibration trial (render_trial). Before every commit it asks for `wp_presentation` feedback and
acks the configure serial, so the shell can report `diagnostic_presented` / `overlay_presented`
from the real presentation of that exact commit. Caps as the retired C client: at most 2 buffers
per Output, 128 MB of buffers in all, 8192 px a side and 4096 x 2160 px in area, 16 Outputs.

pywayland and the generated bindings (`overlay/protocol`, built by meson) are imported in `main`,
so the pure parts import anywhere. Per-Output state lives here once; a composing client adds a
surface per Output through `OutputHook` and binds a newer manager through `manager_version`.
"""

from __future__ import annotations

import logging
import mmap
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .paint import paint, surface_for
from .render import DrawList, Point, parse_trial_points, render_slate, render_trial

MAX_OUTPUTS = 16
MAX_SIDE = 8192
MAX_PIXELS = 4096 * 2160
MAX_MEMORY = 128 * 1024 * 1024
MAX_BUFFERS = 2                     # per Output: one on screen, one being drawn
MAX_NAME = 96
MANAGER_VERSION = 2
OOM_SCORE_ADJ = 900                 # the first thing the kernel kills, never Weston (-500)
WL_SHM_FORMAT_ARGB8888 = 0

log = logging.getLogger("photo-wall-overlay")


def buffer_admissible(width: int, height: int, buffers: int, memory: int) -> bool:
    """PURE. Whether one more width x height ARGB buffer fits the caps."""
    return (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE and width * height <= MAX_PIXELS
            and buffers < MAX_BUFFERS and memory + width * height * 4 <= MAX_MEMORY)


@dataclass
class OutputState:
    name: str
    reason: str = ""
    width: int = 0
    height: int = 0
    serial: int = 0
    testing: bool = False
    overlay: bool = False
    points: tuple[Point, Point, Point, Point] = ((0.0, 0.0),) * 4
    dirty: bool = False
    buffers: int = 0
    surface: object | None = None
    # Live proxies: pywayland destroys a proxy whose Python object is collected.
    held: set = field(default_factory=set)

    def draw_list(self) -> DrawList:
        if self.overlay:
            return render_trial(self.width, self.height, self.points)
        return render_slate(self.name, self.width, self.height, self.reason, self.testing)


class OutputHook(Protocol):
    def output_configured(self, client: OverlayClient, output: OutputState) -> None:
        """After the client handled an Output's configure (its surface exists)."""
        ...


class OverlayClient:
    """Per-Output slate and trial surfaces on the bound private manager."""

    def __init__(self, *, compositor, shm, presentation, manager,
                 hooks: Sequence[OutputHook] = (),
                 painter: Callable[[DrawList, object], None] = paint) -> None:
        self.compositor, self.shm, self.presentation, self.manager = (
            compositor, shm, presentation, manager)
        self.hooks = tuple(hooks)
        self.painter = painter
        self.outputs: dict[str, OutputState] = {}
        self.memory = 0
        manager.dispatcher["output"] = self._output_configure
        manager.dispatcher["overlay"] = self._overlay_configure

    def _output_configure(self, manager, name: str, width: int, height: int, serial: int,
                          reason: str, testing: int) -> None:
        output = self.outputs.get(name)
        if output is None and len(self.outputs) < MAX_OUTPUTS and len(name) <= MAX_NAME:
            output = self.outputs[name] = OutputState(name)
        if output is None or len(reason) > MAX_NAME:
            return
        output.reason, output.width, output.height = reason, width, height
        output.serial, output.testing = serial, bool(testing)
        output.dirty, output.overlay = True, False
        if output.surface is None:
            output.surface = self.compositor.create_surface()
            manager.surface(output.surface, name)
        self.draw(output)
        for hook in self.hooks:
            hook.output_configured(self, output)

    def _overlay_configure(self, manager, name: str, serial: int, primitives: str,
                           visible: int) -> None:
        output = self.outputs.get(name)
        points = parse_trial_points(primitives) if output is not None and visible else None
        if output is None or points is None:
            return
        output.points, output.serial = points, serial
        output.overlay = output.dirty = True
        self.draw(output)

    def draw(self, output: OutputState) -> None:
        """Paint the Output's current page into a fresh buffer and commit it, or wait: a buffer
        release redraws a still-dirty Output. A paint failure still commits (a cleared buffer)
        and acks, as the C client did on a cairo error, so an Output never sticks; a buffer
        counts against the caps only once committed, so nothing raised before that leaks one."""
        width, height = output.width, output.height
        if (not output.dirty or output.surface is None
                or not buffer_admissible(width, height, output.buffers, self.memory)):
            return
        size = width * height * 4
        fd = os.memfd_create("photo-wall-diagnostic", os.MFD_CLOEXEC)
        try:
            os.ftruncate(fd, size)
            pixels = mmap.mmap(fd, size)
            pool = self.shm.create_pool(fd, size)
            buffer = pool.create_buffer(0, width, height, width * 4, WL_SHM_FORMAT_ARGB8888)
            pool.destroy()
        finally:
            os.close(fd)
        self._paint(output, pixels, size)
        feedback = self.presentation.feedback(output.surface)
        output.held.add(feedback)
        feedback.dispatcher["presented"] = lambda proxy, *_: self._feedback_done(output, proxy)
        feedback.dispatcher["discarded"] = lambda proxy: self._feedback_done(output, proxy)
        # The ack precedes the commit: the shell binds the serial to this commit's feedback.
        self.manager.ack(output.surface, output.serial)
        output.surface.attach(buffer, 0, 0)
        output.surface.damage(0, 0, width, height)
        output.surface.commit()
        output.dirty = False
        output.buffers += 1
        self.memory += size
        output.held.add(buffer)
        buffer.dispatcher["release"] = lambda proxy: self._release(output, proxy, pixels, size)

    def _paint(self, output: OutputState, pixels: mmap.mmap, size: int) -> None:
        """Draw the page into `pixels`; never raises. On failure the buffer is cleared (draws
        nothing) and the failure logged."""
        try:
            surface = surface_for(pixels, output.width, output.height)
            try:
                self.painter(output.draw_list(), surface)
            finally:
                surface.finish()
                del surface            # drops cairo's export of `pixels`, so release can close it
        except Exception:
            log.exception("paint failed on %s; committing a cleared buffer", output.name)
            pixels[:] = bytes(size)

    def _release(self, output: OutputState, buffer, pixels: mmap.mmap, size: int) -> None:
        buffer.destroy()
        output.held.discard(buffer)
        self.memory -= size
        output.buffers -= 1
        try:
            pixels.close()
        except BufferError as error:   # an export still holds the map; it unmaps when that goes
            log.warning("buffer pixels not closed on %s: %s", output.name, error)
        if output.dirty:
            self.draw(output)

    @staticmethod
    def _feedback_done(output: OutputState, feedback) -> None:
        feedback.destroy()
        output.held.discard(feedback)


def raise_oom_score(path: str = "/proc/self/oom_score_adj") -> None:
    """Raise this process's OOM score (unprivileged: raising needs no capability). Not fatal."""
    try:
        with open(path, "w") as handle:
            handle.write(str(OOM_SCORE_ADJ))
    except OSError as error:
        log.warning("oom_score_adj not raised: %s", error)


def main(*, hooks: Sequence[OutputHook] = (), manager_version: int = MANAGER_VERSION) -> int:
    """2 without the shell's private socket (never discover a public display); 1 when the
    connection fails or ends."""
    if "WAYLAND_SOCKET" not in os.environ:
        return 2
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    raise_oom_score()
    from pywayland.client import Display

    from .protocol.photo_wall_frame_v1 import PwDiagnosticManagerV1
    from .protocol.presentation_time import WpPresentation
    from .protocol.wayland import WlCompositor, WlShm

    display = Display()
    try:
        display.connect()
    except ValueError:
        return 1
    try:
        names: dict[str, int] = {}
        registry = display.get_registry()
        registry.dispatcher["global"] = (
            lambda _registry, name, interface, _version: names.setdefault(interface, name))
        display.roundtrip()
        wanted = ("wl_compositor", "wl_shm", "wp_presentation", "pw_diagnostic_manager_v1")
        if any(interface not in names for interface in wanted):
            return 1
        # Held for the loop: its proxies (and their dispatchers) die with it.
        _client = OverlayClient(
            compositor=registry.bind(names["wl_compositor"], WlCompositor, 1),
            shm=registry.bind(names["wl_shm"], WlShm, 1),
            presentation=registry.bind(names["wp_presentation"], WpPresentation, 1),
            manager=registry.bind(names["pw_diagnostic_manager_v1"], PwDiagnosticManagerV1,
                                  manager_version),
            hooks=hooks)
        while True:
            display.dispatch(block=True)
    except RuntimeError as error:      # pywayland: the connection failed (-1)
        log.warning("display connection ended: %s", error)
        return 1
    finally:
        display.disconnect()
