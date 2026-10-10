"""The private overlay client: the one Wayland client the shell spawns with a private socket.

Weston's shell (native/shell.c `start_diagnostic`) execs the `diagnostic-client` launcher with
WAYLAND_SOCKET set and binds `pw_diagnostic_manager_v1` only for this client. Per Output it keeps
one surface: an `output` configure paints the slate (render_slate), an `overlay` configure the
calibration trial (render_trial). Before every commit it asks for `wp_presentation` feedback and
acks the configure serial, so the shell can report `diagnostic_presented` / `overlay_presented`
from the real presentation of that exact commit. Caps as the retired C client: at most 2 buffers
per Output, 128 MB of buffers in all, 8192 px a side and 4096 x 2160 px in area, 16 Outputs.

The health layer (manager v3): `HealthLayer`, the production hook, takes one health surface per
Output, with a `wp_viewport`, on its first sized configure and commits a fully transparent solid
buffer (`wp_single_pixel_buffer_manager_v1`, rgba 0) at once, so the layer is mapped before any
instruction and the shell's amber fallback tint stays off on a healthy wall. It reads the judge's
overlay instructions on `health.sock` (`JudgeLink`) and draws each page `overlay.health` decides:
tint on = one whole-Output ARGB buffer (darkening plus the card), tint off = the transparent solid
buffer. Every health commit sets the viewport destination to the Output size, so the solid buffer
is scaled over the whole Output and the ARGB buffer maps 1:1. The layer never shows a small ARGB
shm buffer: Weston 14's DRM backend takes one for a cursor-plane candidate and aborts on the Pi 5
(docs/display-host-backend.md, "The health layer's buffers"). A health commit never acks (ack
binds the slate/trial configure); its presentation feedback is reported to the judge as a
`PresentedReport`.

pywayland and the generated bindings (`overlay/protocol`, built by meson) are imported in `main`,
so the pure parts import anywhere. Per-Output state lives here once; a composing client adds a
surface per Output through `OutputHook` and binds a newer manager through `manager_version`.
One thread: `run` serves the Wayland fd and the judge socket from one selectors loop.
"""

from __future__ import annotations

import json
import logging
import mmap
import os
import selectors
import socket
import struct
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .health import (
    JUDGE_UIDS,
    RECONNECT_MS,
    HealthBoard,
    HealthOutput,
    HealthPage,
    health_socket_path,
)
from .instruction import (
    PULSE_DEADLINE_MS,
    OverlayInstruction,
    PresentedReport,
    encode_presented_report,
    parse_overlay_instruction,
)
from .paint import paint, surface_for
from .render import DrawList, Point, parse_trial_points, render_health, render_slate, render_trial

MAX_OUTPUTS = 16
MAX_SIDE = 8192
MAX_PIXELS = 4096 * 2160
MAX_MEMORY = 128 * 1024 * 1024
MAX_BUFFERS = 2                     # per Output: one on screen, one being drawn
MAX_NAME = 96
MANAGER_VERSION = 3                 # v3: the health layer (get_health_layer)
OOM_SCORE_ADJ = 900                 # the first thing the kernel kills, never Weston (-500)
WL_SHM_FORMAT_ARGB8888 = 0
MAX_INSTRUCTION = 4096              # one judge packet (an encoded OverlayInstruction is far less)
INSTRUCTIONS_PER_WAKE = 64
LOOP_MS = 1000                      # the loop wakes at least this often

log = logging.getLogger("photo-wall-overlay")


def size_admissible(width: int, height: int) -> bool:
    """PURE. Whether a width x height buffer is within the side and area caps."""
    return 0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE and width * height <= MAX_PIXELS


def buffer_admissible(width: int, height: int, buffers: int, memory: int) -> bool:
    """PURE. Whether one more width x height ARGB buffer fits the caps."""
    return (size_admissible(width, height)
            and buffers < MAX_BUFFERS and memory + width * height * 4 <= MAX_MEMORY)


def monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


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

    def __init__(self, *, compositor, shm, presentation, manager, viewporter, single_pixel,
                 hooks: Sequence[OutputHook] = (),
                 painter: Callable[[DrawList, object], None] = paint) -> None:
        self.compositor, self.shm, self.presentation, self.manager = (
            compositor, shm, presentation, manager)
        # Bound for the hooks: the health layer's viewports and its transparent solid buffer.
        self.viewporter, self.single_pixel = viewporter, single_pixel
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
        buffer, pixels, size = self.allocate(width, height)
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

    def allocate(self, width: int, height: int, name: str = "photo-wall-diagnostic"):
        """A zeroed (fully transparent) width x height ARGB8888 wl_buffer and its writable
        pixels: (buffer, pixels, size). The caller counts it against the caps once committed."""
        size = width * height * 4
        fd = os.memfd_create(name, os.MFD_CLOEXEC)
        try:
            os.ftruncate(fd, size)
            pixels = mmap.mmap(fd, size)
            pool = self.shm.create_pool(fd, size)
            buffer = pool.create_buffer(0, width, height, width * 4, WL_SHM_FORMAT_ARGB8888)
            pool.destroy()
        finally:
            os.close(fd)
        return buffer, pixels, size

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


OVERLAY_REQUEST = json.dumps({"op": "overlay"}, separators=(",", ":")).encode()
_UCRED = struct.Struct("3i")         # struct ucred: pid, uid, gid


def peer_uid(connection: socket.socket) -> int:
    """The connected peer's uid (SO_PEERCRED, Linux)."""
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
    return _UCRED.unpack(raw)[1]


def connect_seqpacket(path: str) -> socket.socket:
    connection = socket.socket(socket.AF_UNIX,
                               socket.SOCK_SEQPACKET | getattr(socket, "SOCK_CLOEXEC", 0))
    try:
        connection.settimeout(1.0)
        connection.connect(path)
    except BaseException:
        connection.close()
        raise
    return connection


class JudgeLink:
    """The client's link to the judge's `health.sock` (SOCK_SEQPACKET, non-blocking once open).

    Opens only to a peer whose uid is in JUDGE_UIDS (the directory is pw-health's 0755, so this is
    defence in depth), sends `{"op":"overlay"}` once, then every packet it receives is one
    `OverlayInstruction`; each `PresentedReport` goes back as one packet with MSG_DONTWAIT. A send
    error, EOF or an unparseable packet closes it (`on_closed` runs); it retries every
    RECONNECT_MS."""

    def __init__(self, path: str, *, on_instruction: Callable[[OverlayInstruction, int], None],
                 on_closed: Callable[[], None], clock: Callable[[], int] = monotonic_ms,
                 connector: Callable[[str], socket.socket] = connect_seqpacket,
                 peer: Callable[[socket.socket], int] = peer_uid) -> None:
        self.path, self.on_instruction, self.on_closed = path, on_instruction, on_closed
        self.clock, self.connector, self.peer = clock, connector, peer
        self.connection: socket.socket | None = None
        self.selector: selectors.BaseSelector | None = None
        self.next_attempt_ms = 0
        self._quiet = False              # one log line per run of failed attempts

    def attach(self, selector: selectors.BaseSelector) -> None:
        self.selector = selector

    def service(self, now_ms: int) -> int | None:
        """Connect when due. Returns the next attempt's time, or None while open."""
        if self.connection is not None:
            return None
        if now_ms < self.next_attempt_ms:
            return self.next_attempt_ms
        self.next_attempt_ms = now_ms + RECONNECT_MS
        try:
            connection = self.connector(self.path)
        except OSError as error:
            self._attempt_failed("connect %s: %s", self.path, error)
            return self.next_attempt_ms
        try:
            uid = self.peer(connection)
            if uid not in JUDGE_UIDS:
                connection.close()
                self._attempt_failed("judge socket %s: peer uid %s refused", self.path, uid)
                return self.next_attempt_ms
            connection.setblocking(False)
            connection.send(OVERLAY_REQUEST)
        except OSError as error:
            connection.close()
            self._attempt_failed("judge socket %s: %s", self.path, error)
            return self.next_attempt_ms
        self.connection, self._quiet = connection, False
        if self.selector is not None:
            self.selector.register(connection, selectors.EVENT_READ, self.readable)
        log.info("judge link open on %s", self.path)
        return None

    def _attempt_failed(self, message: str, *args: object) -> None:
        if not self._quiet:
            log.warning(message + "; retrying every %d ms", *args, RECONNECT_MS)
        self._quiet = True

    def readable(self) -> None:
        for _ in range(INSTRUCTIONS_PER_WAKE):
            connection = self.connection
            if connection is None:
                return
            try:
                raw = connection.recv(MAX_INSTRUCTION + 1)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as error:
                self.close(f"receive: {error}")
                return
            if not raw:
                self.close("closed by the judge")
                return
            try:
                if len(raw) > MAX_INSTRUCTION:
                    raise ValueError("overlay_instruction")
                instruction = parse_overlay_instruction(raw)
            except ValueError:
                self.close("not an overlay instruction")
                return
            self.on_instruction(instruction, self.clock())

    def send(self, report: PresentedReport) -> bool:
        connection = self.connection
        if connection is None:
            return False
        try:
            connection.send(encode_presented_report(report), socket.MSG_DONTWAIT)
            return True
        except OSError as error:        # full (EAGAIN) or gone: the judge re-pushes on reconnect
            self.close(f"send: {error}")
            return False

    def close(self, reason: str) -> None:
        connection, self.connection = self.connection, None
        if connection is None:
            return
        if self.selector is not None:
            self.selector.unregister(connection)
        connection.close()
        self.next_attempt_ms = self.clock() + RECONNECT_MS
        log.warning("judge link closed (%s)", reason)
        self.on_closed()


@dataclass
class HealthSurface:
    """One Output's health surface: the page shown, its buffers and unacknowledged commits."""
    output: OutputState
    health: HealthOutput
    surface: object
    viewport: object                    # its destination is the Output size on every commit
    based: bool = False                 # the first (transparent) buffer was committed
    buffers: int = 0                    # ARGB buffers held by the compositor (not the solid one)
    shown: HealthPage | None = None
    size: tuple[int, int] = (0, 0)      # the Output size `shown` was drawn for
    commits: dict[int, int] = field(default_factory=dict)   # commit id -> committed at (ms)
    held: set = field(default_factory=set)


class HealthLayer:
    """The production OutputHook: one health surface per Output, the judge link, and the pages
    `overlay.health` decides. Also a loop hook (`attach`, `service`) for `run`."""

    def __init__(self, *, path: str | None = None, clock: Callable[[], int] = monotonic_ms,
                 painter: Callable[[DrawList, object], None] = paint,
                 link: JudgeLink | None = None) -> None:
        self.clock, self.painter = clock, painter
        self.board = HealthBoard(limit=MAX_OUTPUTS)
        self.link = link if link is not None else JudgeLink(
            path or health_socket_path(), on_instruction=self.instruction,
            on_closed=self.board.reconnected, clock=clock)
        self.client: OverlayClient | None = None
        self.surfaces: dict[str, HealthSurface] = {}
        self.clear: object | None = None    # the transparent solid buffer every surface shares
        self.late = 0                   # commits not presented within D (the watchdog is M3)
        self._commit_ids = iter(range(1, 2**63))

    # -- hooks -------------------------------------------------------------------------------

    def output_configured(self, client: OverlayClient, output: OutputState) -> None:
        self.client = client
        if not output.width or not output.height:
            return
        state = self.surfaces.get(output.name)
        now = self.clock()
        if state is None:
            if self.clear is None:
                self.clear = client.single_pixel.create_u32_rgba_buffer(0, 0, 0, 0)
            surface = client.compositor.create_surface()
            layer = client.manager.get_health_layer(surface, output.name)
            viewport = client.viewporter.get_viewport(surface)
            state = self.surfaces[output.name] = HealthSurface(
                output, self.board.configure(output.name, now), surface, viewport)
            state.held.update((surface, layer, viewport))
        elif (state.shown is not None and state.shown.tint
              and state.size != (output.width, output.height)):
            state.health.repaint()
        self.draw(state, now)

    def attach(self, selector: selectors.BaseSelector) -> None:
        self.link.attach(selector)

    def service(self, now_ms: int) -> int | None:
        """One loop pass: connect the judge link when due, count late commits, draw what is due
        (a stale page, a draw a cap held back). Returns the next time it needs a wake."""
        wakes = [self.link.service(now_ms), self.board.deadline(now_ms)]
        for state in self.surfaces.values():
            self._count_late(state, now_ms)
            self.draw(state, now_ms)
        return min((wake for wake in wakes if wake is not None), default=None)

    def instruction(self, instruction: OverlayInstruction, now_ms: int) -> None:
        output = self.board.instruction(instruction, now_ms)
        state = self.surfaces.get(output.name) if output is not None else None
        if state is not None:
            self.draw(state, now_ms)

    # -- drawing -----------------------------------------------------------------------------

    def draw(self, state: HealthSurface, now_ms: int) -> None:
        """Map the layer (transparent) first, then draw the page `overlay.health` says is due."""
        if not state.based:
            if not self._commit(state, None):
                return
            state.based = True
        page = state.health.page(now_ms)
        if page is not None and self._commit(state, page):
            state.health.drawn(page)

    def _commit(self, state: HealthSurface, page: HealthPage | None) -> bool:
        """Commit `page` over the whole Output (None or tint off: the transparent solid buffer,
        scaled by the viewport; tint on: a whole-Output ARGB buffer). False when the buffer count
        or memory cap holds a tint-on page back (it stays due); the solid buffer costs no cap."""
        client, output = self.client, state.output
        assert client is not None and self.clear is not None
        width, height = output.width, output.height
        argb = None                     # (pixels, size) of a tint-on page's ARGB buffer
        if page is None or not page.tint:
            buffer = self.clear
        elif not size_admissible(width, height):
            self._fail_visible(state, page, f"{width} x {height} is over the size caps")
            return True
        elif not buffer_admissible(width, height, state.buffers, client.memory):
            return False
        else:
            buffer, pixels, size = client.allocate(width, height, "photo-wall-health")
            if not self._paint(page, pixels, width, height):
                buffer.destroy()
                pixels.close()
                self._fail_visible(state, page, "paint failed")
                return True
            argb = pixels, size
        serial = page.serial if page is not None else None
        commit_id = next(self._commit_ids)
        feedback = client.presentation.feedback(state.surface)
        state.held.add(feedback)
        feedback.dispatcher["presented"] = (
            lambda proxy, *_: self._feedback(state, proxy, commit_id, serial, presented=True))
        feedback.dispatcher["discarded"] = (
            lambda proxy: self._feedback(state, proxy, commit_id, serial, presented=False))
        state.viewport.set_destination(width, height)
        state.surface.attach(buffer, 0, 0)
        state.surface.damage(0, 0, width, height)
        state.surface.commit()          # never `ack`: that binds the slate/trial configure serial
        state.commits[commit_id] = self.clock()
        if argb is not None:
            pixels, size = argb
            state.buffers += 1
            client.memory += size
            state.held.add(buffer)
            buffer.dispatcher["release"] = (
                lambda proxy: self._release(state, proxy, pixels, size))
        state.shown, state.size = page, (width, height)
        return True

    def _fail_visible(self, state: HealthSurface, page: HealthPage | None, why: str) -> None:
        """A tint-on page that cannot be drawn: unmap the layer (NULL buffer) so the shell's
        amber fallback tint shows, never the transparent buffer under a fault."""
        log.error("health page for %s not drawn (%s); showing the shell's fallback tint",
                  state.output.name, why)
        state.surface.attach(None, 0, 0)
        state.surface.commit()
        state.shown, state.size = page, (state.output.width, state.output.height)

    def _paint(self, page: HealthPage, pixels: mmap.mmap, width: int, height: int) -> bool:
        try:
            surface = surface_for(pixels, width, height)
            try:
                self.painter(render_health(width, height, page), surface)
            finally:
                surface.finish()
                del surface            # drops cairo's export of `pixels`, so release can close it
            return True
        except Exception:
            log.exception("health paint failed")
            return False

    def _feedback(self, state: HealthSurface, feedback, commit_id: int, serial: int | None, *,
                  presented: bool) -> None:
        feedback.destroy()
        state.held.discard(feedback)
        state.commits.pop(commit_id, None)
        report = (state.health.presented(serial) if presented
                  else state.health.discarded(serial))
        if report is not None:
            self.link.send(report)

    def _release(self, state: HealthSurface, buffer, pixels: mmap.mmap, size: int) -> None:
        buffer.destroy()
        state.held.discard(buffer)
        assert self.client is not None
        self.client.memory -= size
        state.buffers -= 1
        try:
            pixels.close()
        except BufferError as error:
            log.warning("health buffer pixels not closed on %s: %s", state.output.name, error)
        self.draw(state, self.clock())

    def _count_late(self, state: HealthSurface, now_ms: int) -> None:
        for commit_id, committed_ms in list(state.commits.items()):
            if now_ms - committed_ms >= PULSE_DEADLINE_MS:
                del state.commits[commit_id]
                self.late += 1
                log.warning("health commit on %s not presented within %d ms (%d late)",
                            state.output.name, PULSE_DEADLINE_MS, self.late)


class LoopHook(Protocol):
    """An OutputHook that also has sockets or deadlines of its own (HealthLayer)."""

    def attach(self, selector: selectors.BaseSelector) -> None: ...

    def service(self, now_ms: int) -> int | None: ...


def run(display, hooks: Sequence[OutputHook], *, clock: Callable[[], int] = monotonic_ms) -> None:
    """One thread, one selectors loop over the Wayland fd and every loop hook's sockets. Each
    pass dispatches queued events, services the hooks, flushes, then waits until the display or
    a hook socket is readable or the earliest hook deadline (at most LOOP_MS). A readable display
    fd makes `dispatch(block=True)` read without waiting (pywayland 0.4.18 has no
    prepare_read/cancel_read pair for this). Raises RuntimeError when the connection fails."""
    selector = selectors.DefaultSelector()
    selector.register(display.get_fd(), selectors.EVENT_READ, None)
    loop_hooks: tuple[LoopHook, ...] = tuple(
        hook for hook in hooks if callable(getattr(hook, "service", None)))  # type: ignore[misc]
    for hook in loop_hooks:
        hook.attach(selector)
    while True:
        display.dispatch(block=False)
        now = clock()
        wake = now + LOOP_MS
        for hook in loop_hooks:
            due = hook.service(now)
            if due is not None:
                wake = min(wake, due)
        display.flush()
        for key, _ in selector.select(max(0, wake - clock()) / 1000):
            if key.data is None:
                display.dispatch(block=True)
            else:
                key.data()


def raise_oom_score(path: str = "/proc/self/oom_score_adj") -> None:
    """Raise this process's OOM score (unprivileged: raising needs no capability). Not fatal."""
    try:
        with open(path, "w") as handle:
            handle.write(str(OOM_SCORE_ADJ))
    except OSError as error:
        log.warning("oom_score_adj not raised: %s", error)


def main(*, hooks: Sequence[OutputHook] | None = None,
         manager_version: int = MANAGER_VERSION) -> int:
    """2 without the shell's private socket (never discover a public display); 1 when the
    connection fails or ends. `hooks` None = the production hooks (`HealthLayer`); explicit
    hooks replace them (a fault-injection client brings its own health layer)."""
    if "WAYLAND_SOCKET" not in os.environ:
        return 2
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    raise_oom_score()
    from pywayland.client import Display

    from .protocol.photo_wall_frame_v1 import PwDiagnosticManagerV1
    from .protocol.presentation_time import WpPresentation
    from .protocol.single_pixel_buffer_v1 import WpSinglePixelBufferManagerV1
    from .protocol.viewporter import WpViewporter
    from .protocol.wayland import WlCompositor, WlShm

    if hooks is None:
        hooks = (HealthLayer(),)
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
        wanted = ("wl_compositor", "wl_shm", "wp_presentation", "pw_diagnostic_manager_v1",
                  "wp_viewporter", "wp_single_pixel_buffer_manager_v1")
        missing = [interface for interface in wanted if interface not in names]
        if missing:
            log.error("compositor lacks %s", ", ".join(missing))
            return 1
        # Held for the loop: its proxies (and their dispatchers) die with it.
        _client = OverlayClient(
            compositor=registry.bind(names["wl_compositor"], WlCompositor, 1),
            shm=registry.bind(names["wl_shm"], WlShm, 1),
            presentation=registry.bind(names["wp_presentation"], WpPresentation, 1),
            manager=registry.bind(names["pw_diagnostic_manager_v1"], PwDiagnosticManagerV1,
                                  manager_version),
            viewporter=registry.bind(names["wp_viewporter"], WpViewporter, 1),
            single_pixel=registry.bind(names["wp_single_pixel_buffer_manager_v1"],
                                       WpSinglePixelBufferManagerV1, 1),
            hooks=hooks)
        run(display, hooks)
    except RuntimeError as error:      # pywayland: the connection failed (-1)
        log.warning("display connection ended: %s", error)
        return 1
    finally:
        display.disconnect()
