"""`python -m scripts.render_bench --output HDMI-A-1 --photo P1 --photo P2 --photo P3 --photo P4
--video V.mp4 --out DIR [--soak SECONDS] [--budget unlimited]`: run the tracer (bead B6) and write
DIR/moments.jsonl and DIR/verdict.json; exit 0 when the verdict passes, 1 when it fails, 2 when the
bench could not run. `--budget unlimited` gives the renderer a budget it never meets (the budget
mutation probe's run).

Run as root on the test Pi with Weston, the shell and the health client running and
photo-wall-display-controller.service and the Player stopped. The bench is the controller and the
app at once: it connects the shell's control socket and Weston's socket as `--user` (pw-display;
the shell takes a control peer only with the controller's uid and grants this process by its pid,
start time and uid) and keeps root for /dev/mem, debugfs and the KMS properties.

Each loop turn (every refresh, sooner on a video's new frame) it presents the tracer's layers at
the time through the Player's new path (player/plane_renderer.py) with the real PlaneScreens, Gpu,
FitPass, StillDecode and VideoFrames, turns the screen when the step says, and ten times a second,
once the last commit was presented, records a Moment: the strengths that commit must show
(timeline.py `strengths_at` at its frame time), the kernel's planes, the pixel valve's status,
whether a picture was finished or a video played, and the GPU's busy share."""
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

from contracts.models import PREPARE_SECONDS
from scripts.render_bench import timeline
from scripts.render_bench.controller import BenchController
from scripts.render_bench.evidence import Moment, verdict
from scripts.render_bench.kms_state import KMS_DEVICE, STATE_PATH, kms_planes, read_properties
from scripts.render_bench.registers import RegisterWatch

WAYLAND_SOCKET: Final = Path("/run/photo-wall-display-wayland/wayland-0")
GPU_STATS: Final = Path("/sys/devices/platform/axi/1002000000.v3d/gpu_stats")   # bench T2
SAMPLE: Final = 0.1             # seconds between moments
REFRESH: Final = 1 / 60         # a loop turn at least this often
LEAD: Final = 3.0               # seconds from the start to the tracer's origin
TURN_TIMEOUT: Final = 5.0
GRANT_TIMEOUT: Final = 10.0


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
    parser.add_argument("--state", default=STATE_PATH)
    parser.add_argument("--device", default=KMS_DEVICE)
    return parser.parse_args(argv)


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


def _probe(path: Path, kind: str) -> timeline.Media:
    """The file's pixel size (and a video's duration), through GStreamer's discoverer."""
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GstPbutils", "1.0")
    from gi.repository import Gst, GstPbutils
    Gst.init(None)
    info = GstPbutils.Discoverer.new(10 * Gst.SECOND).discover_uri(path.resolve().as_uri())
    stream = info.get_video_streams()[0]
    duration = info.get_duration() / Gst.SECOND if kind == "video" else None
    return timeline.Media(path.resolve(), kind, (stream.get_width(), stream.get_height()), duration)


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
        photos = [_probe(path, "photo") for path in args.photo]
        clip = _probe(args.video, "video")
        self.watch = RegisterWatch()
        self.gpu_busy = _gpu_busy()
        kms_planes(Path(args.state).read_text(), read_properties(args.device))     # readable before the run
        with _as_user(args.user):
            self.controller = BenchController(args.output)
            self.screens = PlaneScreens(socket=str(WAYLAND_SOCKET))
        self.gpu = Gpu()
        self.fitter = _Counting(FitPass(self.gpu.gl))
        self.frames: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self.wake_read, self.wake_write = os.pipe()

        def videos(path: Path, on_frame: Callable[[], None]):
            def scheduled() -> None:            # a streaming thread: hand it to the loop
                self.frames.put(on_frame)
                os.write(self.wake_write, b"f")
            return VideoFrames(path, scheduled)

        budget = ControllerBudget(planes=10**6, line_pixels=10**9) if args.budget == "unlimited" else PI5
        self.renderer = PlaneRenderer(self.screens, self.gpu, self.fitter, _NoLuts(), stills=StillDecode,
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
        self.drawn = 0

    def _grant(self) -> None:
        binding = self.tracer.binding
        self.controller.grant(binding.frame_id, binding.generation, binding.configuration_revision,
                              timeout=GRANT_TIMEOUT)
        self.handed = False

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
            if now >= next_sample and self._sample(now):
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
        for local in locals_:
            if local.layer.start > now:
                self.renderer.prepare(local, self.frame)
        shown = tuple(local for local in locals_ if local.layer.start <= now)
        result = self.renderer.present(OutputComposition(self.tracer.binding, shown, timebase=0.0), self.frame)
        if result.status == "presented":
            self.last = result

    def _sample(self, now: float) -> bool:
        """A Moment of the last presented commit, once the compositor says it is on screen and the
        screen hangs as the step says; whether one was taken."""
        screen = self.screens.screen(self.args.output)
        word = self.screens.presentation(self.args.output, now)
        if (not self.handed or self.last is None or screen is None or word is None or word.at is None
                or word.at < self.last.presented_at):
            return False
        at = self.last.presented_at
        step = timeline.step_at(self.tracer, at)
        if screen.turn != step.turn:
            return False
        planes = kms_planes(Path(self.args.state).read_text(), read_properties(self.args.device))
        playing = any(local.layer.variant is not None and local.layer.variant.duration is not None
                      for local in self.last.composition.layers)
        drawing = playing or self.fitter.draws != self.drawn
        self.drawn = self.fitter.draws
        self.moments.append(Moment(at, step.name, timeline.strengths_at(self.tracer, at, screen.size), planes,
                                   self.watch.sample(), drawing, self.gpu_busy()))
        return True

    def close(self) -> None:
        for closing in (self.renderer.close, self.controller.close, self.screens.close, self.gpu.close,
                        self.watch.close):
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
    try:
        bench = _Bench(args)
    except Exception as error:           # noqa: BLE001 - any setup failure means the bench could not run
        print(f"render_bench: could not start: {error!r}", file=sys.stderr)
        return 2
    try:
        moments = bench.run()
    except Exception as error:           # noqa: BLE001 - a run cut short is not a verdict
        print(f"render_bench: stopped: {error!r}", file=sys.stderr)
        _write(args.out, bench.first, bench.moments, None)
        return 2
    finally:
        bench.close()
    result = verdict(bench.first, moments)
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
