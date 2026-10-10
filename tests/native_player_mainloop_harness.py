"""G8 real-GLib harness: the Player's main-loop policy holds on real GLib, GDK and GL.

Run only inside the display harness container (scripts/run_display_harness.py), after
native_display_smoke.py, with `player` and `contracts` mounted read-only at /repo and `tests`
at /smoke. It starts its own headless Weston (kiosk shell, no photo-wall shell or controller).

1. player.mainloop's numbers are real GLib/GDK priorities.
2. Control dispatch answers on a saturated loop: a priority-0 source costing 40 ms every
   33 ms, plus GDK-priority paint, never starves it (p99 under 100 ms). On origin/main's
   default-idle dispatcher this answers nothing (G8 probe rows C and D).
3. A static photo is not redrawn every tick: after it is presented, NativeRenderer paints it
   at most twice a second while present() is still called every 33 ms, and every present()
   meanwhile reads "presented" (the acknowledgment freshness covers the renewal).
"""
import os
import pathlib
import subprocess
import sys
import threading
import time

sys.path[:0] = ["/repo", "/smoke"]
RUNTIME = pathlib.Path("/tmp/pw-mainloop")
RUNTIME.mkdir(mode=0o700, exist_ok=True)
os.environ.update(XDG_RUNTIME_DIR=str(RUNTIME), WAYLAND_DISPLAY="wayland-mainloop",
                  GDK_BACKEND="wayland", LIBGL_ALWAYS_SOFTWARE="1", PYOPENGL_PLATFORM="egl",
                  GSETTINGS_BACKEND="memory")

import gi  # noqa: E402

gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib  # noqa: E402

from player import mainloop  # noqa: E402
from player.mainloop import MainLoopDispatcher, Tick  # noqa: E402


def busy(ms):
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        pass


def priorities():
    redraw = getattr(Gdk, "PRIORITY_REDRAW", GLib.PRIORITY_HIGH_IDLE + 20)
    assert (mainloop.CONTROL, mainloop.TICK, mainloop.BUS, mainloop.GDK_PRIORITY_REDRAW) == (
        GLib.PRIORITY_HIGH, GLib.PRIORITY_DEFAULT, GLib.PRIORITY_DEFAULT, redraw), (
        "mainloop_priorities", GLib.PRIORITY_HIGH, GLib.PRIORITY_DEFAULT, redraw)
    print("PASS mainloop_priorities_are_glib_and_gdk", flush=True)


def saturated_control(seconds=3.0):
    """A repeating priority-0 source that overruns its 33 ms interval and queues GDK-priority
    paint each turn: the loop never idles. Control dispatch must still answer promptly."""
    loop = GLib.MainLoop()
    dispatch = MainLoopDispatcher(GLib)
    ticks, paints, done = [0], [0], threading.Event()

    def paint():
        if not done.is_set():
            busy(5)
            paints[0] += 1
        return False

    def tick():
        busy(40)
        ticks[0] += 1
        GLib.idle_add(paint, priority=mainloop.GDK_PRIORITY_REDRAW)
        return True

    saturating = GLib.timeout_add(33, tick, priority=GLib.PRIORITY_DEFAULT)
    latencies, missed = [], [0]

    def control():
        time.sleep(0.3)                       # let the loop saturate first
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            sent = time.monotonic()
            try:
                latencies.append(dispatch(time.monotonic).result(timeout=1.0) - sent)
            except Exception:
                missed[0] += 1
            time.sleep(0.05)
        dispatch(loop.quit)

    threading.Thread(target=control, daemon=True).start()
    deadline = GLib.timeout_add(int((seconds + 5) * 1000), loop.quit)  # a starved quit ends too
    loop.run()
    done.set()                 # the queued paints are no-ops; the next part gets an idle loop
    GLib.source_remove(saturating)
    GLib.source_remove(deadline)
    latencies.sort()
    p99 = latencies[int(len(latencies) * 0.99) - 1] if latencies else float("inf")
    print(f"SATURATED ticks={ticks[0]} paints={paints[0]} answered={len(latencies)} "
          f"missed={missed[0]} p99={p99 * 1000:.1f} ms", flush=True)
    assert ticks[0] >= seconds * 15, ("loop_not_saturated", ticks[0])
    assert missed[0] == 0 and len(latencies) >= seconds * 10, ("control_starved", missed[0])
    assert p99 < 0.100, ("control_p99", p99)
    print("PASS control_dispatch_answers_on_a_saturated_loop", flush=True)


def static_photo_render_rate(seconds=6.0):
    from native_display_media import image_fixture

    from contracts.models import FrameProfile, Layer, OutputBinding
    from player.native import NativeOutput, NativeRenderer
    from player.rendering import LocalLayer, OutputComposition

    log = open("/tmp/pw-mainloop-weston.log", "w")
    weston = subprocess.Popen(
        ["weston", "--backend=headless-backend.so", "--renderer=pixman",
         "--shell=kiosk-shell.so", "--width=640", "--height=480", "--idle-time=0",
         "--socket=wayland-mainloop", "--no-config"],
        stdout=log, stderr=log)
    try:
        for _ in range(100):
            if (RUNTIME / "wayland-mainloop").exists():
                break
            time.sleep(0.05)
        renders = []
        original = NativeRenderer._render

        def counted(self, area, context, surface):
            renders.append(time.monotonic())
            return original(self, area, context, surface)

        NativeRenderer._render = counted
        renderer = NativeRenderer((NativeOutput("headless", "photo-wall-headless", 640, 480),))
        renderer.set_unbound_outputs((), None)
        payload, variant = image_fixture()
        photo = pathlib.Path("/tmp/pw-mainloop-photo.png")
        photo.write_bytes(payload)
        binding = OutputBinding(output_id="headless", frame_id="frame", generation=1,
                                profile=FrameProfile(width_px=640, height_px=480,
                                                     diagonal_inches=10))
        layer = Layer(assignment_id="photo", run_id="run", output_id="headless",
                      frame_id="frame", binding_generation=1, start=0, end=10**9,
                      media_origin=0, variant=variant)
        composition = OutputComposition(binding, binding.calibration,
                                        (LocalLayer(layer, photo, 0.0, 1.0),))
        loop = GLib.MainLoop()
        results = []
        started = time.monotonic()

        def tick():
            results.append((time.monotonic(), renderer.present(composition).status))
            if time.monotonic() - started > seconds + 3:
                loop.quit()
                return False
            return True

        Tick(GLib, 33, tick)
        loop.run()
        presented = [at for at, status in results if status == "presented"]
        failed = [status for _, status in results if status == "failed"]
        assert presented and not failed, ("never_presented", results[-5:], renderer.diagnostics())
        first = presented[0]
        after = [status for at, status in results if at > first]
        window = [at for at in renders if first + 0.5 <= at < first + 0.5 + seconds]
        print(f"STATIC presents={len(results)} after_presented={len(after)} "
              f"renders_total={len(renders)} renders_in_{seconds:.0f}s={len(window)}", flush=True)
        assert len(after) >= seconds * 15, ("present_not_called_per_tick", len(after))
        assert all(status == "presented" for status in after), (
            "presented_lapsed", sorted(set(after)))
        assert len(window) <= 2 * seconds, ("static_photo_redrawn", len(window))
        print("PASS static_photo_is_not_redrawn_every_tick", flush=True)
        renderer.close()
    finally:
        weston.terminate()
        try:
            weston.wait(timeout=3)
        except subprocess.TimeoutExpired:
            weston.kill()
            weston.wait()
        log.close()


try:
    priorities()
    saturated_control()
    static_photo_render_rate()
except BaseException:
    log = pathlib.Path("/tmp/pw-mainloop-weston.log")
    if log.exists():
        print(log.read_text(errors="replace"), flush=True)
    raise
