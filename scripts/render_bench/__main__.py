"""`python -m scripts.render_bench --output HDMI-A-1 --photo P1 --photo P2 --photo P3 --photo P4
--video V.mp4 --out DIR [--soak SECONDS] [--budget unlimited] [--weston-log FILE]`: run the tracer
(bead B6) and write DIR/moments.jsonl and DIR/verdict.json; exit 0 when the verdict passes, 1 when
it fails, 2 when the bench could not run. `--budget unlimited` gives the renderer a budget it never
meets (the budget mutation probe's run): its glitch bits stay set until a full modeset, so run it
last, or reboot after it.

Run as root on the test Pi with Weston (started with --debug), the shell and the health client
running and photo-wall-display-controller.service and the Player stopped. The bench freezes
`--freeze` (photo-wall-app-broker.service: it would launch the stopped Player again, whose health
tint would cover the screen) through systemd's D-Bus API for the run and thaws it after. The bench
is the controller and the app at once: it connects the shell's control socket and Weston's socket
as `--user` (pw-display; the shell takes a control peer only with the controller's uid and grants
this process by its pid, start time and uid) and keeps root for /dev/mem, debugfs and the KMS
properties. `--weston-log` (default DIR/weston-drm-backend.log) is Weston's drm-backend debug
stream recorded alongside the run (`weston-debug drm-backend`), read after it: the one witness of
which framebuffers are Weston's composites (weston_log.py).

Each loop turn (every refresh, sooner on a video's new frame) it first records a Moment, ten times
a second, once the compositor has answered the newest commit presented (the kernel then holds that
commit's planes): the strengths it must show (timeline.py `strengths_at` at its frame time), the
kernel's planes, the pixel valve's status, whether a picture was being finished or a video played,
and the GPU's busy share. Then it presents the tracer's layers at the time through the Player's new
path (player/plane_renderer.py) with the real PlaneScreens, Gpu, FitPass, StillDecode and
VideoFrames, prepares the layers about to start, and turns the screen when the step says."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import queue
import select
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Final

from contracts.display_turn import hanging
from contracts.models import PREPARE_SECONDS
from scripts.render_bench import timeline
from scripts.render_bench.controller import BenchController
from scripts.render_bench.evidence import Moment, verdict
from scripts.render_bench.kms_state import (
    PlaneProperties,
    display_controller,
    kms_planes,
    read_properties,
    shown_planes,
)
from scripts.render_bench.registers import RegisterWatch
from scripts.render_bench.weston_log import renderer_framebuffers

WAYLAND_SOCKET: Final = Path("/run/photo-wall-display-wayland/wayland-0")
GPU_STATS: Final = Path("/sys/devices/platform/axi/1002000000.v3d/gpu_stats")   # bench T2
SAMPLE: Final = 0.1             # seconds between moments
REFRESH: Final = 1 / 60         # a loop turn at least this often
LEAD: Final = 3.0               # seconds from the start to the tracer's origin
TURN_TIMEOUT: Final = 5.0
GRANT_TIMEOUT: Final = 10.0
FREEZE_ATTEMPTS: Final = 20          # half a second apart


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m scripts.render_bench", description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", required=True, help="the Output's id, as the shell names it (HDMI-A-1)")
    parser.add_argument("--photo", action="append", type=Path, required=True,
                        help="a 4K JPEG photo (at least four)")
    parser.add_argument("--video", type=Path, required=True, help="a 1080p30 HEVC MP4")
    parser.add_argument("--out", type=Path, required=True, help="where moments.jsonl and verdict.json go")
    parser.add_argument("--soak", type=float, default=600.0)
    parser.add_argument("--fade", type=float, default=3.0)
    parser.add_argument("--hold", type=float, default=10.0)
    parser.add_argument("--budget", choices=("pi5", "unlimited"), default="pi5")
    parser.add_argument("--user", default="pw-display", help="the user the shell's sockets are connected as")
    parser.add_argument("--state", help="the display controller's debugfs state (default: found by its driver)")
    parser.add_argument("--device", help="the display controller's card node (default: found by its driver)")
    parser.add_argument("--weston-log", type=Path,
                        help="Weston's drm-backend debug stream recorded alongside (default OUT/weston-drm-backend.log)")
    parser.add_argument("--freeze", action="append",
                        help="a systemd unit frozen for the run (default photo-wall-app-broker.service)")
    return parser.parse_args(argv)


@contextmanager
def _frozen(units: list[str]) -> Iterator[None]:
    """`units` frozen (systemd's FreezeUnit, over the system bus) for the block, thawed after.
    RuntimeError("bench_freeze") when one cannot be frozen."""
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

    def call(method: str, unit: str) -> None:
        bus.call_sync("org.freedesktop.systemd1", "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
                      method, GLib.Variant("(s)", (unit,)), None, Gio.DBusCallFlags.NONE, 10_000, None)

    frozen = []
    try:
        for unit in units:
            for attempt in range(FREEZE_ATTEMPTS):      # a unit still thawing or freezing answers busy
                try:
                    call("FreezeUnit", unit)
                    break
                except GLib.Error as error:
                    print(f"freeze {unit}: {error.message}", file=sys.stderr)
                    if attempt + 1 == FREEZE_ATTEMPTS:
                        raise RuntimeError("bench_freeze") from error
                    time.sleep(0.5)
            frozen.append(unit)
        yield
    finally:
        for unit in frozen:
            try:
                call("ThawUnit", unit)
            except GLib.Error as error:
                print(f"thaw {unit}: {error.message}", file=sys.stderr)


@contextmanager
def _as_user(name: str) -> Iterator[None]:
    """Run the block with `name`'s effective uid and gid (the sockets' peer credentials)."""
    entry = pwd.getpwnam(name)
    os.setegid(entry.pw_gid)
    os.seteuid(entry.pw_uid)
    try:
        yield
    finally:
        os.seteuid(0)
        os.setegid(0)


def _gst():
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    return Gst


def _probe_photo(path: Path) -> timeline.Media:
    """A photo's pixel size, through GStreamer's discoverer."""
    import gi
    gi.require_version("GstPbutils", "1.0")
    from gi.repository import GstPbutils
    gst = _gst()
    stream = GstPbutils.Discoverer.new(10 * gst.SECOND).discover_uri(path.resolve().as_uri()).get_video_streams()[0]
    return timeline.Media(path.resolve(), "photo", (stream.get_width(), stream.get_height()), None)


def _probe_video(path: Path) -> timeline.Media:
    """A video's pixel size and duration from its container and stream headers (filesrc ! qtdemux !
    h265parse ! fakesink, prerolled): never a decoder. GStreamer's discoverer would pick the
    hardware decoder, which fails without the Player's allocation probe (player/decode.py).
    RuntimeError("bench_video_probe") when the headers do not say."""
    gst = _gst()
    pipeline = gst.Pipeline.new(None)
    source, demux, parse, sink = (gst.ElementFactory.make(name, None)
                                  for name in ("filesrc", "qtdemux", "h265parse", "fakesink"))
    if None in (source, demux, parse, sink):
        raise RuntimeError("bench_video_probe")
    source.set_property("location", str(path.resolve()))
    for element in (source, demux, parse, sink):
        pipeline.add(element)
    source.link(demux)
    parse.link(sink)
    demux.connect("pad-added", lambda _demux, pad: pad.get_name().startswith("video")
                  and pad.link(parse.get_static_pad("sink")))
    try:
        pipeline.set_state(gst.State.PAUSED)
        reached, _, _ = pipeline.get_state(10 * gst.SECOND)
        caps = parse.get_static_pad("src").get_current_caps()
        if reached not in (gst.StateChangeReturn.SUCCESS, gst.StateChangeReturn.NO_PREROLL) or caps is None:
            raise RuntimeError("bench_video_probe")
        with caps.get_structure(0) as structure:        # GStreamer 1.26's StructureWrapper
            (found_width, width), (found_height, height) = structure.get_int("width"), structure.get_int("height")
        known, duration = pipeline.query_duration(gst.Format.TIME)
    finally:
        pipeline.set_state(gst.State.NULL)
    if not (found_width and found_height and known and duration > 0):
        raise RuntimeError("bench_video_probe")
    return timeline.Media(path.resolve(), "video", (width, height), duration / gst.SECOND)


def _gpu_busy() -> Callable[[], float]:
    """The GPU's busy share since the last call (v3d gpu_stats: each queue's timestamp and runtime,
    in ns). OSError when the counters cannot be read: a run blind to the GPU cannot pass."""
    def read() -> tuple[int, int]:
        stamp = runtime = 0
        for line in GPU_STATS.read_text().splitlines()[1:]:
            fields = line.split()
            if len(fields) == 4:
                stamp, runtime = int(fields[1]), runtime + int(fields[3])
        if not stamp:
            raise OSError("gpu_stats")
        return stamp, runtime

    last = read()

    def busy() -> float:
        nonlocal last
        now = read()
        span = now[0] - last[0]
        share = (now[1] - last[1]) / span if span > 0 else 0.0
        last = now
        return share

    return busy


class _Counting:
    """The fit pass, counting what it finishes (a Moment's `drawing`)."""

    def __init__(self, fitter) -> None:
        self.fitter, self.draws = fitter, 0

    def draw(self, request, target) -> None:
        self.draws += 1
        self.fitter.draw(request, target)

    def black(self, target) -> None:
        self.draws += 1
        self.fitter.black(target)


class _CountingDevice:
    """The Gpu, counting the uploads of stills' rows (GPU work too: a Moment's `drawing`)."""

    def __init__(self, gpu) -> None:
        self.gpu, self.uploads = gpu, 0

    def __getattr__(self, name: str):
        return getattr(self.gpu, name)

    def upload(self, *args) -> None:
        self.uploads += 1
        self.gpu.upload(*args)


class _Recording:
    """PlaneScreens, noting the newest commit's sequence: a Moment is taken only once the compositor
    answered that very commit (the kernel then shows its planes, and no later commit was made)."""

    def __init__(self, screens) -> None:
        self.screens, self.sequence = screens, 0

    def __getattr__(self, name: str):
        return getattr(self.screens, name)

    def present(self, *args, **kwargs):
        result = self.screens.present(*args, **kwargs)
        if isinstance(result, int):
            self.sequence = result
        return result


class _NoLuts:
    """The tracer's Frame has no paper profile, so no LUT is ever asked for."""

    def lut(self, ref):
        return None

    def generation(self) -> int:
        return 0


class _Bench:
    """One run: the renderer and its ports, the controller, the probes and the moments."""

    def __init__(self, args: argparse.Namespace) -> None:
        from player.controller_budget import PI5, ControllerBudget
        from player.decode import StillDecode, VideoFrames
        from player.fit_pass import FitPass
        from player.gpu import Gpu
        from player.plane_renderer import FrameOnScreen, PlaneRenderer
        from player.plane_screen import PlaneScreens

        self.args = args
        photos = [_probe_photo(path) for path in args.photo]
        clip = _probe_video(args.video)
        if args.state is None or args.device is None:
            found_state, found_device = display_controller()
            args.state, args.device = args.state or found_state, args.device or found_device
        self.watch = RegisterWatch()
        self.gpu_busy = _gpu_busy()
        kms_planes(Path(args.state).read_text(), read_properties(args.device))     # readable before the run
        self.properties = PlaneProperties(args.device)
        with _as_user(args.user):
            self.controller = BenchController(args.output)
            self.screens = _Recording(PlaneScreens(socket=str(WAYLAND_SOCKET)))
        self.gpu = Gpu()
        self.device = _CountingDevice(self.gpu)
        self.fitter = _Counting(FitPass(self.gpu.gl))
        self.frames: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self.wake_read, self.wake_write = os.pipe()

        def videos(path: Path, on_frame: Callable[[], None]):
            def scheduled() -> None:            # a streaming thread: hand it to the loop
                self.frames.put(on_frame)
                os.write(self.wake_write, b"f")
            return VideoFrames(path, scheduled)

        budget = ControllerBudget(planes=10**6, line_pixels=10**9) if args.budget == "unlimited" else PI5
        self.renderer = PlaneRenderer(self.screens, self.device, self.fitter, _NoLuts(), stills=StillDecode,
                                      videos=videos, budget=budget, on_change=lambda output_id: None)
        self.tracer = timeline.tracer(args.output, photos, clip, origin=time.monotonic() + LEAD, fade=args.fade,
                                      hold=args.hold, soak=args.soak)
        self.frame = FrameOnScreen(self.tracer.binding, self.tracer.rectangle)
        # Each Variant's file (the tracer names Variants by their files' bytes).
        self.files = {hashlib.sha256(item.path.read_bytes()).hexdigest(): item.path for item in [*photos, clip]}
        self.moments: list[Moment] = []
        self.first = self.watch.sample()
        self.turn = 0
        self.handed = False
        self.shown: set[str] = set()
        self.last = None                         # the newest "presented" answer
        self.work = (0, 0)                       # the fit pass's draws and the uploads, at the last moment
        self.worked = False                      # whether the interval before the last moment saw work

    def _grant(self) -> None:
        """Grant the screen again (at the start, after a turn). The last presented answer was for
        the old grant: until the renderer presents under the new one, the screen shows the
        candidate's black, so no moment is taken of the old answer (it would count as a miss)."""
        binding = self.tracer.binding
        self.controller.grant(binding.frame_id, binding.generation, binding.configuration_revision,
                              timeout=GRANT_TIMEOUT)
        self.handed = False
        self.last = None

    def run(self) -> list[Moment]:
        for _ in range(100):                     # Weston's Output announced to the frame client
            self.screens.dispatch()
            if (screen := self.screens.screen(self.args.output)) is not None:
                break
            time.sleep(0.02)
        else:
            raise RuntimeError("bench_no_screen")
        if screen.turn != 0:
            self.controller.turn(0, timeout=TURN_TIMEOUT)
        self._grant()
        end, next_sample, step = self.tracer.steps[-1].end, time.monotonic(), None
        while (now := time.monotonic()) < end:
            self.screens.dispatch()
            while not self.frames.empty():
                self.frames.get()()
            current = timeline.step_at(self.tracer, now)
            if current is not step:
                step = current
                print(f"{now:.3f} {step.name} turn {step.turn}", file=sys.stderr, flush=True)
            if step.turn != self.turn:
                self.controller.turn(step.turn, timeout=TURN_TIMEOUT)
                self.turn = step.turn
                self._grant()
            if self.handed:
                self.controller.drain()
            else:
                try:
                    self.controller.handoff(timeout=0.002)
                    self.handed = True
                except RuntimeError:
                    pass
            if now >= next_sample and self._sample(now):     # before the present: it reads the commit answered
                next_sample = now + SAMPLE
            self._present(now)
            ready = select.select([self.screens.fd, self.wake_read], [], [], REFRESH)[0]
            if self.wake_read in ready:
                os.read(self.wake_read, 4096)
        return self.moments

    def _present(self, now: float) -> None:
        from player.rendering import LocalLayer, OutputComposition
        layers = timeline.layers_at(self.tracer, now, lead=PREPARE_SECONDS)
        current = {layer.assignment_id for layer in layers}
        for gone in self.shown - current:
            self.renderer.release(gone)
        self.shown = current
        locals_ = [LocalLayer(layer, None if layer.variant is None else self.files[layer.variant.sha256],
                              layer.position(now), layer.alpha(now)) for layer in layers]
        shown = tuple(local for local in locals_ if local.layer.start <= now)
        result = self.renderer.present(OutputComposition(self.tracer.binding, shown, timebase=0.0), self.frame)
        if result.status == "presented":
            self.last = result
        for local in locals_:                    # after the present: a due commit goes first
            if local.layer.start > now:
                self.renderer.prepare(local, self.frame)

    def _sample(self, now: float) -> bool:
        """A Moment of the last presented commit, once the compositor answered that very commit
        presented (its flip done: the kernel holds its planes) and the screen hangs as the step
        says; whether one was taken. A picture's finish counts as drawing in the interval it was
        started and the next (its GPU work runs on past its draw call: fenced, not waited for), and
        while the renderer is still finishing."""
        screen = self.screens.screen(self.args.output)
        word = self.screens.presentation(self.args.output, now)
        if (not self.handed or self.last is None or screen is None or word is None or word.at is None
                or word.sequence != self.screens.sequence or word.at < self.last.presented_at):
            return False
        at = self.last.presented_at
        step = timeline.step_at(self.tracer, at)
        if screen.turn != step.turn:
            return False
        state = Path(self.args.state).read_text()
        planes = kms_planes(state, self.properties.read(shown_planes(state)))
        playing = any(local.layer.variant is not None and local.layer.variant.duration is not None
                      for local in self.last.composition.layers)
        work = (self.fitter.draws, self.device.uploads)
        worked = work != self.work
        drawing = playing or worked or self.worked or self.renderer.finishing
        self.work, self.worked = work, worked
        self.moments.append(Moment(at, step.name, timeline.strengths_at(self.tracer, at, screen.size), planes,
                                   self.watch.sample(), drawing, self.gpu_busy(), hanging(screen.size, screen.turn),
                                   timeline.ground_seen(self.tracer, at, screen.size)))
        return True

    def close(self) -> None:
        for closing in (self.renderer.close, self.controller.close, self.screens.close, self.gpu.close,
                        self.watch.close, self.properties.close):
            try:
                closing()
            except Exception as error:   # noqa: BLE001 - every part is closed, the first error reported
                print(f"close: {error!r}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Run the bench (module docstring)."""
    args = _arguments(argv)
    if len(args.photo) < 4:
        print("render_bench: four photos at least", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    if args.budget == "unlimited":
        print("render_bench: --budget unlimited sets the glitch bits until a full modeset: reboot after this run",
              file=sys.stderr)
    try:
        with _frozen(args.freeze or ["photo-wall-app-broker.service"]):
            try:
                bench = _Bench(args)
            except Exception as error:   # noqa: BLE001 - any setup failure means the bench could not run
                print(f"render_bench: could not start: {error!r}", file=sys.stderr)
                return 2
            try:
                moments = bench.run()
            except Exception as error:   # noqa: BLE001 - a run cut short is not a verdict
                print(f"render_bench: stopped: {error!r}", file=sys.stderr)
                _write(args.out, bench.first, bench.moments, None)
                return 2
            finally:
                bench.close()
    except RuntimeError as error:        # bench_freeze: the broker would relaunch the Player
        print(f"render_bench: could not start: {error!r}", file=sys.stderr)
        return 2
    time.sleep(1.0)                      # the drm-backend stream's last repaints written
    log = args.weston_log or args.out / "weston-drm-backend.log"
    try:
        renderer = renderer_framebuffers(log.read_text(errors="replace"))
    except OSError as error:
        print(f"render_bench: no drm-backend stream: {error!r}", file=sys.stderr)
        renderer = None
    result = verdict(bench.first, moments, renderer)
    _write(args.out, bench.first, moments, result)
    print(json.dumps(asdict(result)), flush=True)
    return 0 if result.passed else 1


def _write(out: Path, first, moments: list[Moment], result) -> None:
    with (out / "moments.jsonl").open("w") as written:
        for moment in moments:
            written.write(json.dumps(asdict(moment)) + "\n")
    (out / "verdict.json").write_text(json.dumps({
        "first_pv_status": first.pv_status, "moments": len(moments),
        "verdict": None if result is None else asdict(result)}, indent=2) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
