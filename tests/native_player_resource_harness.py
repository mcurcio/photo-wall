"""1b P1a real-GLib harness: the Player releases what it opens, and a surface failure ends with
its episode.

Run only inside the display harness container (scripts/run_display_harness.py), after
native_player_mainloop_harness.py, with `player` and `contracts` mounted read-only at /repo and
`tests` at /smoke. It starts its own headless Weston (kiosk shell, pixman) and drives the real
NativeRenderer on software GL. Its JPEG fixture is made here with GStreamer.

1. decoder_churn_releases_every_descriptor: 50 decoders prepared and released through
   NativeRenderer.prepare/release leave /proc/self/fd at its baseline, and none of their
   pipelines alive (each leaked pipeline held its bus's wakeup socketpair: 2 fds).
2. slideshow_then_output_remap_survives_fd_limit: a child (--remap-child) at a soft
   RLIMIT_NOFILE of 256 shows 150 slides on one Output, hides and re-shows the window, and the
   next slide presents (a remap under fd exhaustion is Mesa's software EGL SEGV, rc -11).
3. a_failed_surface_presents_again_after_a_remap: a render that raised once fails the surface
   until the window is mapped again, not forever.
4. a_failed_surface_presents_again_under_a_new_grant: a failure under grant G1 ends when the
   display host hands the Output a new grant G2.
"""
import gc
import hashlib
import os
import pathlib
import resource
import subprocess
import sys
import time
import uuid

sys.path[:0] = ["/repo", "/smoke"]
RUNTIME = pathlib.Path("/tmp/pw-resource")
RUNTIME.mkdir(mode=0o700, exist_ok=True)
SOCKET = "wayland-resource"
WESTON_LOG = pathlib.Path("/tmp/pw-resource-weston.log")
PHOTO = RUNTIME / "slide.jpg"
os.environ.update(XDG_RUNTIME_DIR=str(RUNTIME), WAYLAND_DISPLAY=SOCKET,
                  GDK_BACKEND="wayland", LIBGL_ALWAYS_SOFTWARE="1", PYOPENGL_PLATFORM="egl",
                  GSETTINGS_BACKEND="memory")

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from contracts.models import FrameProfile, Layer, OutputBinding, Variant  # noqa: E402
from player.native import NativeOutput, NativeRenderer  # noqa: E402
from player.rendering import LocalLayer, OutputComposition  # noqa: E402

WIDTH, HEIGHT = 64, 48
BINDING = OutputBinding(output_id="headless", frame_id="frame", generation=1,
                        profile=FrameProfile(width_px=640, height_px=480, diagonal_inches=10))


def jpeg_fixture() -> Variant:
    """A 64x48 JPEG made with GStreamer, written once to PHOTO."""
    Gst.init(None)
    if not PHOTO.exists():
        pipeline = Gst.parse_launch(
            f"videotestsrc num-buffers=1 ! video/x-raw,width={WIDTH},height={HEIGHT} "
            f"! jpegenc ! filesink location={PHOTO}")
        pipeline.set_state(Gst.State.PLAYING)
        pipeline.get_bus().timed_pop_filtered(5 * Gst.SECOND,
                                              Gst.MessageType.EOS | Gst.MessageType.ERROR)
        pipeline.set_state(Gst.State.NULL)
    payload = PHOTO.read_bytes()
    assert payload[:2] == b"\xff\xd8", ("jpeg_fixture", len(payload))
    return Variant(sha256=hashlib.sha256(payload).hexdigest(), size=len(payload),
                   media_type="image/jpeg", width=WIDTH, height=HEIGHT)


def slide(assignment_id: str, variant: Variant) -> OutputComposition:
    layer = Layer(assignment_id=assignment_id, run_id="run", output_id="headless",
                  frame_id="frame", binding_generation=1, start=0, end=10**9,
                  media_origin=0, variant=variant)
    return OutputComposition(BINDING, BINDING.calibration, (LocalLayer(layer, PHOTO, 0.0, 1.0),))


def fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def spin(seconds: float) -> None:
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while context.iteration(False):
            pass
        time.sleep(0.005)


def until(call, wanted: str, seconds: float = 5.0) -> str:
    """Call `call()` (a status), spinning the main context between calls, until it answers
    `wanted` or `seconds` pass; the last status."""
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while True:
        status = call()
        if status == wanted or time.monotonic() >= end:
            return status
        while context.iteration(False):
            pass
        time.sleep(0.005)


def renderer() -> NativeRenderer:
    native = NativeRenderer((NativeOutput("headless", "photo-wall-headless", 640, 480),))
    native.set_unbound_outputs((), None)
    spin(1.0)                               # the first render compiles the surface's programs
    return native


def show(native: NativeRenderer, composition: OutputComposition) -> str:
    return until(lambda: native.present(composition).status, "presented")


def remap(native: NativeRenderer) -> None:
    window = native._surfaces["headless"].window
    window.hide()
    spin(0.5)
    window.show_all()
    spin(1.0)


def fail_one_render(native: NativeRenderer) -> None:
    """The surface's next render raises once, inside its own try (as a GL error would)."""
    original = native._targets

    def raises_once(*args):
        native._targets = original
        raise RuntimeError("injected render failure")

    native._targets = raises_once


def decoder_churn_releases_every_descriptor(native: NativeRenderer, variant: Variant) -> None:
    run = uuid.uuid4().hex[:8]
    pipelines: set[str] = set()

    def churn(count: int, prefix: str) -> None:
        for index in range(count):
            local = slide(f"{prefix}-{run}-{index}", variant).layers[0]
            status = until(lambda: native.prepare(local).status, "prepared")
            assert status == "prepared", ("churn_prepare", index, status, native.diagnostics())
            pipelines.add(native._decoders[local.layer.assignment_id].pipeline.get_name())
            native.release(local.layer.assignment_id)

    churn(1, "warm")                        # plugin loading is not a per-decoder cost
    gc.collect()
    baseline = fds()
    pipelines.clear()
    churn(50, "churn")
    gc.collect()
    after = fds()
    alive = [obj.get_name() for obj in gc.get_objects()
             if isinstance(obj, Gst.Pipeline) and obj.get_name() in pipelines]
    print(f"CHURN fds baseline={baseline} after={after} pipelines_alive={len(alive)}",
          flush=True)
    assert after <= baseline, ("descriptors_leaked", baseline, after)
    assert not alive, ("pipelines_leaked", len(alive))
    print("PASS decoder_churn_releases_every_descriptor", flush=True)


def remap_child() -> None:
    """Case 2's child: 150 slide changes at a soft limit of 256 fds, a remap, one more slide."""
    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
    variant = jpeg_fixture()
    native = renderer()
    previous = None
    for index in range(150):
        composition = slide(f"slide-{index}", variant)
        native.prepare(composition.layers[0])
        status = show(native, composition)
        if status != "presented":
            print(f"SLIDE {index} {status} fds={fds()} {native.diagnostics()}", flush=True)
            sys.exit(1)
        if previous is not None:
            native.release(previous)
        previous = composition.layers[0].layer.assignment_id
    print(f"SLIDES 150 fds={fds()}", flush=True)
    remap(native)
    composition = slide("slide-after-remap", variant)
    native.release(previous)
    print(f"REMAP {show(native, composition)}", flush=True)


def slideshow_then_output_remap_survives_fd_limit() -> None:
    child = subprocess.run([sys.executable, "-X", "faulthandler", __file__, "--remap-child"],
                           capture_output=True, text=True, timeout=120)
    lines = child.stdout.strip().splitlines()
    print(f"REMAP_CHILD rc={child.returncode} last={lines[-1] if lines else None!r}", flush=True)
    # Out of descriptors, GStreamer repeats one critical per poll; the rest is the evidence.
    stderr = [line for line in child.stderr.splitlines() if line.strip() and "CRITICAL" not in line]
    assert child.returncode == 0 and lines and lines[-1] == "REMAP presented", (
        "remap_child", child.returncode, child.stdout[-2000:], "\n".join(stderr[-40:]))
    print("PASS slideshow_then_output_remap_survives_fd_limit", flush=True)


def a_failed_surface_presents_again_after_a_remap(native: NativeRenderer,
                                                  variant: Variant) -> None:
    before = slide("remap-before", variant)
    assert show(native, before) == "presented", ("remap_first_present", native.diagnostics())
    failing = slide("remap-failing", variant)
    native.release("remap-before")
    fail_one_render(native)
    status = until(lambda: native.present(failing).status, "failed")
    assert status == "failed", ("injected_failure_not_seen", status)
    remap(native)
    status = show(native, failing)
    print(f"REMAP_FAILED present_after_remap={status}", flush=True)
    assert status == "presented", ("failed_after_remap", status, native.diagnostics())
    native.release("remap-failing")
    print("PASS a_failed_surface_presents_again_after_a_remap", flush=True)


class Frames:
    """The display host's grants, faked: one admitted grant at a time for the one Output."""

    def __init__(self):
        self.current = self.new_grant()

    @staticmethod
    def new_grant():
        from player.wayland_frames import FrameGrant
        return FrameGrant(grant_id=uuid.uuid4(), binding_generation=BINDING.generation,
                          config_revision=BINDING.configuration_revision,
                          frame_id=BINDING.frame_id, admitted=True)

    def grant(self, _output_id, _composition=None):
        return self.current

    def trial(self, _composition, _grant):
        return None

    def tag_rendered_buffer(self, *_args):
        pass

    def close(self):
        pass


def a_failed_surface_presents_again_under_a_new_grant(native: NativeRenderer,
                                                      variant: Variant) -> None:
    frames = Frames()
    native._frames = frames
    before = slide("grant-before", variant)
    assert show(native, before) == "presented", ("grant_first_present", native.diagnostics())
    failing = slide("grant-failing", variant)
    native.release("grant-before")
    fail_one_render(native)
    status = until(lambda: native.present(failing).status, "failed")
    assert status == "failed", ("injected_failure_not_seen", status)
    frames.current = frames.new_grant()
    status = show(native, failing)
    print(f"GRANT_FAILED present_under_new_grant={status}", flush=True)
    assert status == "presented", ("failed_under_new_grant", status, native.diagnostics())
    native.release("grant-failing")
    native._frames = None
    print("PASS a_failed_surface_presents_again_under_a_new_grant", flush=True)


def main() -> None:
    variant = jpeg_fixture()
    with WESTON_LOG.open("w") as log:
        weston = subprocess.Popen(
            ["weston", "--backend=headless-backend.so", "--renderer=pixman",
             "--shell=kiosk-shell.so", "--width=640", "--height=480", "--idle-time=0",
             f"--socket={SOCKET}", "--no-config"],
            stdout=log, stderr=log)
        try:
            for _ in range(100):
                if (RUNTIME / SOCKET).exists():
                    break
                time.sleep(0.05)
            failed = []
            for case in (decoder_churn_releases_every_descriptor,
                         a_failed_surface_presents_again_after_a_remap,
                         a_failed_surface_presents_again_under_a_new_grant):
                native = renderer()     # each case on its own surface: one red stays one red
                try:
                    case(native, variant)
                except AssertionError as error:
                    failed.append(case.__name__)
                    print(f"FAIL {case.__name__}: {error!r}"[:4000], flush=True)
                finally:
                    native.close()
                    spin(0.2)
            try:
                slideshow_then_output_remap_survives_fd_limit()
            except AssertionError as error:
                failed.append("slideshow_then_output_remap_survives_fd_limit")
                print(f"FAIL slideshow_then_output_remap_survives_fd_limit: {error!r}"[:4000],
                      flush=True)
            assert not failed, ("failed", failed)
        finally:
            weston.terminate()
            try:
                weston.wait(timeout=3)
            except subprocess.TimeoutExpired:
                weston.kill()
                weston.wait()


if __name__ == "__main__":
    if sys.argv[1:] == ["--remap-child"]:
        remap_child()
    else:
        try:
            main()
        except BaseException:
            if WESTON_LOG.exists():
                print(WESTON_LOG.read_text(errors="replace"), flush=True)
            raise
