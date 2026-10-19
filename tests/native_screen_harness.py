"""Render redesign S1, bead B3 (run ledger .claude/runs/render-planes/s1-page.md): the frame client's
screens under a real headless Weston with the installed photo-wall shell and frame client library.

Run only inside the display harness container (scripts/run_display_harness.py), after
native_picture_e2e_harness.py, with `player` and `contracts` mounted read-only at /repo and `tests`
at /smoke. The harness is Display Host's controller (the installed WestonBackend and DisplayHost on
the shell's control socket: allow, candidate, handoff, turn, as native_picture_e2e_harness.py does)
and reads Weston's pixels through weston_capture_v1 (`--debug`). The app is a child process, an
unprivileged user, driving player/plane_screen.py's PlaneScreens on its own connection; it answers
one JSON command per line on stdin. Its pictures are memfd-backed LINEAR XR24 buffers passed through
zwp_linux_dmabuf_v1. Weston 14's pixman renderer offers no zwp_linux_dmabuf_v1 (CI, 2026-10-18), so
the harness falls back to the GL renderer on llvmpipe, which imports them. The shell's private
client is the harness health client in its `bare` mode (alive, its health layer unmapped), so
nothing but the app is on screen.

1. the_handed_off_screen_shows_black: the candidate's empty presents (tagged
   "candidate-<grant id>") bring the handoff; then every pixel of the Output is black.
2. two_planes_mix_their_stored_values: red under green, green at strength 0.5: inside them the
   capture is the stored-value mix (127.5, 127.5, 0) within 2 levels, outside black; the pictures
   are busy after the commit that shows them.
3. a_present_with_fewer_planes_unmaps_the_rest: the following present of red alone captures red (the
   green sub-window shows nothing); once that commit is presented the green picture is released and
   red stays busy; every present answered a rising sequence and the shell's `presented` named each
   tag.
4. an_import_with_a_short_stride_is_refused: a stride below width x 4 imports nothing.
5. a_turned_screen_shows_a_turned_target_upright: after the `turn` op (transform 1) and the new
   handoff it brings (a turn is a mode change: the shell takes the grant back) `screen()` reports
   turn 90 and the Output's size turned; four solid planes on the hanging quadrants and one
   full-screen target laid out turned (player/geometry.py `buffer_to_picture`) with the same four
   colours capture alike: the buffer transform shows the target upright, in any capture layout.
6. a_full_screen_frame_is_answered_past_the_feedback_table: the screen turned back (and handed
   off again), SOAK_COMMITS (well past the frame client's 128 feedback slots) presents of one
   opaque full-screen plane, alternating red and blue, each waited on: every one is answered
   `presented` with its own sequence (none overdue, none lost), the shell's `presented` names
   every tag, and the capture is the last colour. The screen's black is covered the whole time, so
   Weston answers only feedback asked on what it presents (frame-client.h `pw_screen_present`).
7. the_ground_shows_beneath_the_planes: a present with a ground (a full-screen grey target laid out
   as the screen hangs) and red at strength 0.5: outside the plane the capture is the ground's
   grey, inside red mixed over grey; the next present without a ground shows the black again.
8. a_fade_restacks_moves_and_attaches_nothing: after one present of two planes, FADE_PRESENTS more
   with only the upper plane's strength changing send no wl_subsurface.place_above, no
   set_position and no wl_surface.attach (the app's own WAYLAND_DEBUG log), one multiplier each:
   Weston answers a restack or a move by damaging and repainting beneath the planes.
   8b. a_burst_ends_on_a_presented_commit: those presents come within one refresh, past the frame
   client's FEEDBACK_PER_SURFACE answers on the screen; the shell still names the last one
   presented (frame-client.c `feedback_slot` drops the oldest answer of a burst rather than leave
   the newest commit untracked). Mutation probe: FEEDBACK_STALE_NS 0 (every full table untracked)
   -> the last fade, and case 9's trial after it, are never presented.
9. a_trial_overlay_rides_the_present_that_carries_it (render redesign S3, bead B3, run ledger
   .claude/runs/render-planes/s3-page.md): a Position trial set through the controller
   (`WestonBackend.trial`, as native_display_smoke.py's overlay cases set theirs), the app reads it on
   its own connection (`PlaneScreens.trial`) and presents under the trial's tag: without an overlay
   the shell presents that tag and sends no `overlay_presented`; with the overlay of the drawn
   placement (`overlay_corners`) it sends `overlay_presented` naming that tag.
10. an_enlarged_picture_fills_its_placement (owner, 2026-10-18: the look applied "at the minimum of
   the asset size and the frame size"): a photo whose source is half its placement is finished at
   half size (player/planes.py `finish_size`) and the sub-window's viewport enlarges it: the four
   quadrants fill RECTANGLE, black outside. Mutation probe: skip `wp_viewport_set_destination` in
   frame-client.c -> the picture shows at half size in RECTANGLE's top-left quarter.
"""
import json
import mmap
import os
import pathlib
import re
import select
import shutil
import socket
import subprocess
import sys
import time
import uuid

sys.path[:0] = ["/repo", "/smoke"]
DISPLAY = "/usr/lib/photo-wall/node-display"
KERNEL = "/usr/lib/photo-wall/node-kernel"
sys.path += [DISPLAY, KERNEL]       # the installed Display Host, as the display-controller launcher puts it
ROOT = pathlib.Path("/tmp/pw-screen")
SOCKET = "wayland-screen"
APP_UID = 10005
OUTPUT = "headless"
WIDTH_PX, HEIGHT_PX = 640, 480
WESTON_LOG = pathlib.Path("/tmp/pw-weston.log")        # scripts/run_display_harness.py prints it on failure
APP_LOG = pathlib.Path("/tmp/pw-screen-app.log")
APP_ENV = {"XDG_RUNTIME_DIR": str(ROOT), "WAYLAND_DISPLAY": SOCKET, "HOME": "/tmp/pw-screen-home"}
XR24, LINEAR = 0x34325258, 0
RED, GREEN, BLUE, WHITE, BLACK = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255), (0, 0, 0)
QUADRANTS = (RED, GREEN, BLUE, WHITE)       # top-left, top-right, bottom-left, bottom-right as it hangs
TOLERANCE = 2
SOAK_COMMITS, SOAK_CHUNK = 320, 40         # case 6; the harness drains the shell between chunks
RECTANGLE = (40, 40, 200, 150)              # x, y, width, height of the mixed planes
GREY = (64, 64, 64)                         # case 7's ground
FADE_PRESENTS = 10                          # case 8


def frame_lighting():
    """The app's Frame lighting, which a trial's envelope carries too (case 9)."""
    from contracts.frame_lighting import FrameLighting, Paper

    return FrameLighting(mat_depth_mm=0, lights=(), paper=Paper(whiteness=.9, ink_density=2))


# --- the app (child process, unprivileged) ------------------------------------------------------

def quadrant_colour(u: float, v: float) -> tuple[int, int, int]:
    return QUADRANTS[(2 if v >= .5 else 0) + (1 if u >= .5 else 0)]


def target_bytes(size: tuple[int, int], turn: int, fill) -> bytes:
    """XR24 pixels (B, G, R, X) of a target laid out `size` for a screen turned `turn`: `fill` is
    one colour, or "quadrants" (QUADRANTS over the picture as it hangs, through
    player/geometry.py `buffer_to_picture`)."""
    width, height = size
    if fill != "quadrants":
        red, green, blue = fill
        return bytes((blue, green, red, 255)) * (width * height)
    from player.geometry import buffer_to_picture

    pixels = bytearray(width * height * 4)
    for y in range(height):
        for x in range(width):
            red, green, blue = quadrant_colour(*buffer_to_picture(x + .5, y + .5, size, turn))
            offset = (y * width + x) * 4
            pixels[offset:offset + 4] = bytes((blue, green, red, 255))
    return bytes(pixels)


def app() -> None:
    from contracts.frame_fit import Placement
    from contracts.models import FrameProfile, Layer, OutputBinding, Variant
    from contracts.photo_crop import CENTRED
    from player.fit_pass import target_size
    from player.gpu import Target
    from player.plane_screen import PlaneScreens, overlay_corners
    from player.planes import Composition, Plane
    from player.rendering import LocalLayer, OutputComposition

    screens = PlaneScreens()
    binding = OutputComposition(OutputBinding(
        output_id=OUTPUT, frame_id="frame", generation=1,
        profile=FrameProfile(width_px=WIDTH_PX, height_px=HEIGHT_PX, diagonal_inches=10),
        lighting=frame_lighting()))
    pictures, fds = {}, []
    candidate, last, pending = True, 0.0, b""

    def target(size, data: bytes, stride: int):
        fd = os.memfd_create("pw-screen-picture", os.MFD_CLOEXEC)
        os.ftruncate(fd, len(data))
        with mmap.mmap(fd, len(data)) as memory:
            memory[:] = data
        fds.append(fd)
        return Target(size, 0, 0, fd, stride, XR24, LINEAR)

    def present(planes, tag, ground=None, overlay=None):
        screen = screens.screen(OUTPUT)
        grant = screens.grant(binding)
        if screen is None or grant is None:
            return {"result": None}
        if ground is not None:
            size = target_size(screen.size, screen.turn)
            key = ("ground", tuple(ground), size)
            if key not in pictures:
                pictures[key] = screens.import_picture(target(size, target_bytes(size, screen.turn, tuple(ground)),
                                                              size[0] * 4))
                assert pictures[key] is not None, ("import_refused", "ground", size)
            ground = pictures[key]
        shown = []
        for name, fill, x, y, width, height, strength, *source in planes:
            at = Placement(x, y, width, height)
            if source:      # a photo of that source size: finished at player/planes.py `finish_size`
                variant = Variant(asset_id=f"asset-{name}", recipe_id="e" * 64, sha256="a" * 64, size=1,
                                  media_type="image/jpeg", width=source[0][0], height=source[0][1])
                layer = Layer(assignment_id=name, run_id="run", output_id=OUTPUT, frame_id="frame",
                              binding_generation=1, start=0, end=10**9, media_origin=0, variant=variant,
                              crop=CENTRED)
            else:
                layer = Layer(assignment_id=name, run_id="run", output_id=OUTPUT, frame_id="frame",
                              binding_generation=1, start=0, end=10**9, media_origin=0, presentation="black")
            plane = Plane("frame", LocalLayer(layer, None, 0.0, 1.0), at, strength)
            size = target_size(plane.size, screen.turn)
            key = (name, size)
            if key not in pictures:
                fill = fill if fill == "quadrants" else tuple(fill)
                pictures[key] = screens.import_picture(
                    target(size, target_bytes(size, screen.turn, fill), size[0] * 4))
                assert pictures[key] is not None, ("import_refused", name, size)
            shown.append((plane, pictures[key]))
        result = screens.present(Composition(screen, tuple(plane for plane, _ in shown)),
                                 {plane.layer.layer.assignment_id: picture for plane, picture in shown},
                                 grant, tag, None, ground, overlay)
        return {"result": result, "busy": {plane.layer.layer.assignment_id: screens.busy(picture)
                                           for plane, picture in shown}}

    def trial_present(with_overlay: bool):
        """The trial Display Host holds for the grant (waited on up to 5 s), then one present of red
        at RECTANGLE under the trial's tag, with that placement's overlay when `with_overlay`."""
        end, applied = time.monotonic() + 5, None
        while applied is None and time.monotonic() < end:
            screens.dispatch()
            grant = screens.grant(binding)
            applied = None if grant is None else screens.trial(binding, grant)
            if applied is None:
                select.select([screens.fd], [], [], 0.02)
        assert applied is not None, "trial_unseen"
        corners = overlay_corners(Placement(*RECTANGLE), screens.screen(OUTPUT).size) if with_overlay else None
        tag = applied.candidate.frame_tag
        return {**present([["red", RED, *RECTANGLE, 1.0]], tag, overlay=corners), "tag": tag}

    def soak(count: int, tag: str):
        """`count` presents of one opaque full-screen plane, red and blue in turn, each waited on
        (up to 1 s; headless Weston answers within a few refreshes) for its word: the answers'
        kinds, by count."""
        answers: dict[str, int] = {}
        for index in range(count):
            screen = screens.screen(OUTPUT)
            width, height = screen.size
            name, colour = (("full-red", RED), ("full-blue", BLUE))[index % 2]
            result = present([[name, colour, 0, 0, width, height, 1.0]], f"{tag}-{index}")["result"]
            assert isinstance(result, int), ("soak_present", index, result)
            end, word = time.monotonic() + 1.0, None
            while time.monotonic() < end:
                screens.dispatch()
                word = screens.presentation(OUTPUT, time.monotonic())
                if word is not None and word.sequence >= result:
                    break
                select.select([screens.fd], [], [], 0.01)
            kind = ("none" if word is None or word.sequence < result else
                    "discarded" if word.at is None else "presented")
            answers[kind] = answers.get(kind, 0) + 1
        return {"answers": answers}

    def handle(command):
        nonlocal candidate
        op = command["op"]
        if op == "candidate":
            candidate = command["on"]
            return {}
        if op == "present":
            return present(command["planes"], command["tag"], command.get("ground"))
        if op == "busy":
            screens.dispatch()
            return {"busy": {key[0]: screens.busy(picture) for key, picture in pictures.items()
                             if key[0] != "ground"}}
        if op == "screen":
            screen = screens.screen(OUTPUT)
            return {"screen": None if screen is None else [*screen.size, screen.turn]}
        if op == "soak":
            return soak(command["count"], command["tag"])
        if op == "trial_present":
            return trial_present(command["overlay"])
        if op == "short_stride":
            size = (64, 48)
            short = target(size, target_bytes(size, 0, RED), 64 * 4 - 4)
            return {"refused": screens.import_picture(short) is None}
        raise ValueError(op)

    while True:
        readable, _, _ = select.select([0, screens.fd], [], [], 0.05)
        if screens.fd in readable:
            screens.dispatch()
        if 0 in readable:
            chunk = os.read(0, 65536)
            if not chunk:
                break
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                reply = handle(json.loads(line))
                os.write(1, json.dumps(reply).encode() + b"\n")
        if candidate and time.monotonic() - last > .05:      # the candidate's black, as the Player draws it
            last = time.monotonic()
            screen, grant = screens.screen(OUTPUT), screens.grant(binding)
            if screen is not None and grant is not None:
                screens.present(Composition(screen, ()), {}, grant, f"candidate-{grant.grant_id}", None)
    screens.close()


# --- the harness (root): Weston, the controller side, the capture -------------------------------

def protocols() -> None:
    """Python bindings for Weston's capture protocol, generated as native_display_smoke.py does."""
    generated = pathlib.Path("/tmp/pw-screen-protocols")
    subprocess.run([sys.executable, "-m", "pywayland.scanner", "-i", "/usr/share/wayland/wayland.xml",
                    "/usr/share/libweston-14/protocols/weston-output-capture.xml",
                    "-o", str(generated / "pw_screen_protocols")], check=True)
    (generated / "pw_screen_protocols" / "__init__.py").touch()
    sys.path.insert(0, str(generated))


SHM_FORMAT = {0x34325258: 1, 0x34325241: 0}  # DRM XRGB8888, ARGB8888 -> wl_shm


def capture() -> tuple[int, int, bytes]:
    """(width, height, XRGB bytes) of Weston's framebuffer, through weston_capture_v1."""
    from pw_screen_protocols.wayland import WlOutput, WlShm
    from pw_screen_protocols.weston_output_capture import WestonCaptureV1
    from pywayland.client import Display

    display = Display(str(ROOT / SOCKET))
    display.connect()
    try:
        found = {}
        registry = display.get_registry()
        registry.dispatcher["global"] = lambda _r, name, iface, _v: found.setdefault(iface, name)
        display.roundtrip()
        out = registry.bind(found["wl_output"], WlOutput, 1)
        shm = registry.bind(found["wl_shm"], WlShm, 1)
        source = registry.bind(found["weston_capture_v1"], WestonCaptureV1, 1).create(
            out, WestonCaptureV1.source.framebuffer)
        state = {}
        source.dispatcher["format"] = lambda _s, drm: state.update(format=drm)
        source.dispatcher["size"] = lambda _s, w, h: state.update(size=(w, h))
        source.dispatcher["complete"] = lambda _s: state.update(done="complete")
        source.dispatcher["retry"] = lambda _s: state.update(done="retry")
        source.dispatcher["failed"] = lambda _s, msg: state.update(done="failed", message=msg)
        display.roundtrip()
        for _ in range(5):
            assert state.get("format") in SHM_FORMAT and "size" in state, state
            width, height = state["size"]
            stride = width * 4
            fd = os.memfd_create("pw-screen-capture")
            try:
                os.ftruncate(fd, stride * height)
                pool = shm.create_pool(fd, stride * height)
                buffer = pool.create_buffer(0, width, height, stride, SHM_FORMAT[state["format"]])
                state.pop("done", None)
                source.capture(buffer)
                end = time.monotonic() + 3
                while "done" not in state and time.monotonic() < end:
                    display.roundtrip()
                    time.sleep(0.02)
                if state.get("done") == "complete":
                    with mmap.mmap(fd, stride * height, prot=mmap.PROT_READ) as pixels:
                        return width, height, bytes(pixels)
                buffer.destroy()
                pool.destroy()
            finally:
                os.close(fd)
            assert state.get("done") == "retry", ("capture", state)
        raise AssertionError(("capture_retries", state))
    finally:
        display.disconnect()


def pixel(frame, x: int, y: int) -> tuple[int, int, int]:
    width, _, data = frame
    offset = (y * width + x) * 4
    blue, green, red = data[offset:offset + 3]
    return red, green, blue


def near(actual, wanted) -> bool:
    return max(abs(a - w) for a, w in zip(actual, wanted, strict=True)) <= TOLERANCE


def histogram(frame) -> list:
    width, height, _ = frame
    counts: dict = {}
    for y in range(0, height, 8):
        for x in range(0, width, 8):
            colour = pixel(frame, x, y)
            counts[colour] = counts.get(colour, 0) + 1
    return sorted(counts.items(), key=lambda item: -item[1])[:8]


def rectangle_check(inside_colour, frame) -> tuple[int, int]:
    """Every sampled pixel 3 px clear of RECTANGLE's edges: inside near `inside_colour`, outside
    exactly black. The (inside, outside) counts; AssertionError at the first that differs."""
    width, height, _ = frame
    left, top, w, h = RECTANGLE
    counts = [0, 0]
    for y in range(1, height, 7):
        for x in range(1, width, 7):
            within = [left + d <= x < left + w - d and top + d <= y < top + h - d for d in (-3, 3)]
            if within[0] != within[1]:
                continue
            actual = pixel(frame, x, y)
            if within[1]:
                assert near(actual, inside_colour), ("inside", x, y, actual, inside_colour)
                counts[0] += 1
            else:
                assert actual == BLACK, ("outside_not_black", x, y, actual)
                counts[1] += 1
    return counts[0], counts[1]


# What pw_frame_client_connect binds, at the lowest version it takes (frame-client.h).
REQUIRED = {"pw_frame_manager_v1": 4, "xdg_wm_base": 1, "wl_compositor": 4, "wl_subcompositor": 1,
            "zwp_linux_dmabuf_v1": 3, "wp_viewporter": 1, "wp_single_pixel_buffer_manager_v1": 1,
            "wp_alpha_modifier_v1": 1, "wp_presentation": 1, "wl_output": 4}


def start_weston(renderer: str) -> subprocess.Popen:
    """Headless Weston with the installed shell and `renderer`, once its sockets are up (the app's
    opened to the app's user)."""
    for stale in (ROOT / "control.sock", ROOT / SOCKET, ROOT / f"{SOCKET}.lock"):
        stale.unlink(missing_ok=True)
    env = {**os.environ, "XDG_RUNTIME_DIR": str(ROOT), "PHOTO_WALL_CONTROLLER_UID": "0",
           "LIBGL_ALWAYS_SOFTWARE": "1"}
    with WESTON_LOG.open("a") as log:
        weston = subprocess.Popen(
            ["weston", "--backend=headless-backend.so", f"--renderer={renderer}",
             f"--shell={DISPLAY}/photo-wall-shell.so", f"--width={WIDTH_PX}", f"--height={HEIGHT_PX}",
             "--idle-time=0", f"--socket={SOCKET}", "--no-config", "--debug"],
            env=env, stdout=log, stderr=log)
    for _ in range(200):
        if (ROOT / "control.sock").exists() and (ROOT / SOCKET).exists():
            break
        assert weston.poll() is None, ("weston_failed", renderer)
        time.sleep(0.05)
    (ROOT / SOCKET).chmod(0o666)
    return weston


def stop_weston(weston: subprocess.Popen) -> None:
    weston.terminate()
    try:
        weston.wait(timeout=3)
    except subprocess.TimeoutExpired:
        weston.kill()
        weston.wait()


def advertised() -> dict[str, int]:
    """Every global Weston offers a client, with its version."""
    from pywayland.client import Display

    display = Display(str(ROOT / SOCKET))
    display.connect()
    try:
        offered: dict[str, int] = {}
        registry = display.get_registry()
        registry.dispatcher["global"] = lambda _r, _name, iface, version: offered.update(
            {iface: max(version, offered.get(iface, 0))})
        display.roundtrip()
        return offered
    finally:
        display.disconnect()


def main() -> None:
    protocols()
    WESTON_LOG.write_text("")
    from appliance.display_host.weston import (
        CompositorPresentation,
        DiagnosticRelease,
        OverlayPresentation,
        SurfaceGrant,
        WestonBackend,
    )
    from contracts.display_turn import hanging
    from contracts.models import Calibration, PositionBox
    from contracts.node_calibration import TrialCandidate, candidate_hash, canonical, encode_trial
    from contracts.node_display import Surface
    from contracts.node_protocol import NodeProcessIdentity, OutputKey

    shutil.copyfile("/smoke/display_harness_health_client.py", DISPLAY + "/diagnostic-client")
    os.chmod(DISPLAY + "/diagnostic-client", 0o755)
    pathlib.Path("/tmp/pw-health-client-mode").write_text("bare")   # alive, its layer unmapped
    ROOT.mkdir(mode=0o711, exist_ok=True)
    ROOT.chmod(0o711)
    home = pathlib.Path(APP_ENV["HOME"])
    home.mkdir(exist_ok=True)
    os.chown(home, APP_UID, APP_UID)
    # The pixman renderer first; the GL renderer on llvmpipe only when pixman offers no linux-dmabuf.
    for renderer in ("pixman", "gl"):
        weston = start_weston(renderer)
        offered = advertised()
        missing = {name for name, version in REQUIRED.items() if offered.get(name, 0) < version}
        print(f"RENDERER {renderer} missing={sorted(missing)} offered={sorted(offered.items())}", flush=True)
        if not missing:
            break
        stop_weston(weston)
        assert missing == {"zwp_linux_dmabuf_v1"}, ("globals_missing", renderer, missing)
    else:
        raise AssertionError("no_renderer_offers_linux_dmabuf")
    child = None
    app_log = APP_LOG.open("w")
    try:
        channel = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        channel.connect(str(ROOT / "control.sock"))
        # WAYLAND_DEBUG: the app's requests in its log, for case 8.
        child = subprocess.Popen([sys.executable, "-B", __file__, "--app"],
                                 env={**os.environ, **APP_ENV, "WAYLAND_DEBUG": "client"},
                                 preexec_fn=lambda: (os.setgid(APP_UID), os.setuid(APP_UID)),
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=app_log)
        alive = lambda grant: pathlib.Path(f"/proc/{grant.surface.process.pid}").exists()  # noqa: E731
        backend = WestonBackend(channel, display_uid=0, compositor_pid=weston.pid, verify_process=alive)
        host = backend.initialize()
        presented_tags: list[str] = []
        overlay_tags: list[str] = []    # each overlay_presented's frame tag (case 9)
        keys: list = []                 # the Output's keys, as the shell announces them

        def observe(observed) -> None:
            if isinstance(observed, CompositorPresentation):
                presented_tags.append(observed.frame_tag)
            if isinstance(observed, OverlayPresentation):
                overlay_tags.append(observed.frame_tag)
            if isinstance(observed, OutputKey) and observed.output_id == OUTPUT:
                keys.append(observed)

        def pump(wanted, seconds: float = 10.0):
            """Dispatch the shell's events until `wanted(observed)`; that observation."""
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                observed = backend.dispatch()
                observe(observed)
                if observed is not None and wanted(observed):
                    return observed
                if observed is None:
                    time.sleep(0.01)
            raise AssertionError("pump_timeout")

        def drain() -> None:
            """Dispatch every event already queued (the shell drops a control peer that lags)."""
            for _ in range(1000):
                observed = backend.dispatch()
                if observed is None:
                    return
                observe(observed)

        def ask(**command):
            drain()
            assert child.poll() is None, ("app_exited", child.returncode)
            child.stdin.write(json.dumps(command).encode() + b"\n")
            child.stdin.flush()
            line = child.stdout.readline()
            assert line, ("app_exited", child.wait(timeout=5))
            drain()
            return json.loads(line)

        def await_frame(check, seconds: float = 8.0, again=None):
            """`check(capture())` until it holds or `seconds` pass (calling `again()` between tries)."""
            end = time.monotonic() + seconds
            while True:
                drain()
                frame = capture()
                try:
                    return check(frame)
                except AssertionError:
                    if time.monotonic() >= end:
                        print(f"CAPTURED {frame[:2]} {histogram(frame)}", flush=True)
                        raise
                    if again is not None:
                        again()
                    time.sleep(0.1)

        def hand_off(known: int):
            """The Output's first key after the `known` ones offered to the app and handed off, as
            the runner does: the app presents its candidate's black meanwhile. The admitted Surface."""
            del presented_tags[:]
            ask(op="candidate", on=True)
            end = time.monotonic() + 10
            while len(keys) <= known:
                assert time.monotonic() < end, ("no_new_output_key", keys)
                observed = backend.dispatch()
                observe(observed)
                if observed is None:
                    time.sleep(0.01)
            key = keys[-1]
            ticks = int(pathlib.Path(f"/proc/{child.pid}/stat").read_text().rpartition(") ")[2].split()[19])
            surface = Surface(key, NodeProcessIdentity(child.pid, ticks, uuid.uuid4()), 1, 1, 1, "frame")
            backend.allow(SurfaceGrant(surface, APP_UID, True))
            host.offer(surface)
            for _attempt in range(20):     # the handoff names a recent presented buffer; a newer may supersede it
                presented = pump(lambda seen: isinstance(seen, CompositorPresentation), seconds=20)
                try:
                    host.authorize_handoff(surface, now_ms=presented.observed_monotonic_ms)
                    break
                except ValueError:
                    continue
            pump(lambda seen: isinstance(seen, DiagnosticRelease))
            assert any(tag.startswith("candidate-") for tag in presented_tags), presented_tags
            ask(op="candidate", on=False)
            return surface

        # 1. the_handed_off_screen_shows_black
        hand_off(0)

        def all_black(frame) -> int:
            width, height, _ = frame
            samples = [pixel(frame, x, y) for y in range(0, height, 5) for x in range(0, width, 5)]
            assert all(colour == BLACK for colour in samples), ("not_black", histogram(frame))
            return len(samples)

        print(f"BLACK samples={await_frame(all_black)}", flush=True)
        print("PASS the_handed_off_screen_shows_black", flush=True)

        # 2. two_planes_mix_their_stored_values
        sequences = []
        left, top, width, height = RECTANGLE
        mixed = ask(op="present", tag="mix", planes=[["red", RED, left, top, width, height, 1.0],
                                                      ["green", GREEN, left, top, width, height, 0.5]])
        assert isinstance(mixed["result"], int), mixed
        sequences.append(mixed["result"])
        assert mixed["busy"] == {"red": True, "green": True}, mixed
        inside, outside = await_frame(lambda frame: rectangle_check((127.5, 127.5, 0), frame))
        print(f"MIX inside={inside} outside={outside}", flush=True)
        assert inside > 300 and outside > 3000, (inside, outside)
        print("PASS two_planes_mix_their_stored_values", flush=True)

        # 3. a_present_with_fewer_planes_unmaps_the_rest
        single = ask(op="present", tag="single", planes=[["red", RED, left, top, width, height, 1.0]])
        assert isinstance(single["result"], int), single
        sequences.append(single["result"])
        inside, outside = await_frame(lambda frame: rectangle_check(RED, frame))
        pump(lambda seen: isinstance(seen, CompositorPresentation) and seen.frame_tag == "single")
        end = time.monotonic() + 5
        while (busy := ask(op="busy")["busy"]).get("green") and time.monotonic() < end:
            time.sleep(0.05)
        print(f"SINGLE inside={inside} outside={outside} busy={busy} sequences={sequences} "
              f"tags={sorted(set(presented_tags) - {t for t in presented_tags if t.startswith('candidate-')})}",
              flush=True)
        assert busy == {"red": True, "green": False}, busy
        assert sequences[0] < sequences[1], sequences
        assert {"mix", "single"} <= set(presented_tags), presented_tags
        print("PASS a_present_with_fewer_planes_unmaps_the_rest", flush=True)

        # 4. an_import_with_a_short_stride_is_refused
        assert ask(op="short_stride") == {"refused": True}
        print("PASS an_import_with_a_short_stride_is_refused", flush=True)

        # 5. a_turned_screen_shows_a_turned_target_upright
        known = len(keys)
        assert backend.turn(OUTPUT, 90, timeout=5.0), "turn_refused"
        hand_off(known)                 # a turn is a mode change: the shell takes the grant back
        turned = list(hanging((WIDTH_PX, HEIGHT_PX), 90))
        end = time.monotonic() + 5
        while (screen := ask(op="screen")["screen"]) != [*turned, 90] and time.monotonic() < end:
            time.sleep(0.05)
        assert screen == [*turned, 90], screen
        across, down = turned[0] // 2, turned[1] // 2
        quadrants = [[f"quadrant-{index}", list(colour), across * (index % 2), down * (index // 2), across, down,
                      1.0] for index, colour in enumerate(QUADRANTS)]

        def show(planes, tag):
            result = ask(op="present", tag=tag, planes=planes)["result"]
            assert isinstance(result, int), result
            sequences.append(result)

        def four_colours(frame):
            width, height, _ = frame
            counts = {colour: 0 for colour in QUADRANTS}
            for y in range(1, height, 7):
                for x in range(1, width, 7):
                    colour = pixel(frame, x, y)
                    assert colour in counts, ("not_a_quadrant", x, y, colour)
                    counts[colour] += 1
            total = sum(counts.values())
            assert all(count > total // 5 for count in counts.values()), counts
            return frame

        show(quadrants, "quadrants")
        reference = await_frame(four_colours, again=lambda: show(quadrants, "quadrants"))
        upright = [["upright", "quadrants", 0, 0, *turned, 1.0]]
        show(upright, "upright")

        def like_reference(frame):
            assert frame[:2] == reference[:2], (frame[:2], reference[:2])
            width, height, _ = frame
            compared = 0
            for y in range(4, height - 4, 5):
                for x in range(4, width - 4, 5):
                    wanted = pixel(reference, x, y)
                    if any(pixel(reference, x + dx, y + dy) != wanted for dx in (-3, 3) for dy in (-3, 3)):
                        continue                    # a quadrant's edge
                    actual = pixel(frame, x, y)
                    assert near(actual, wanted), ("turned_target", x, y, actual, wanted)
                    compared += 1
            return compared

        compared = await_frame(like_reference, again=lambda: show(upright, "upright"))
        print(f"TURNED capture={reference[:2]} screen={screen} compared={compared} sequences={sequences}", flush=True)
        assert compared > 5000, compared
        assert sequences == sorted(set(sequences)), sequences
        print("PASS a_turned_screen_shows_a_turned_target_upright", flush=True)

        # 6. a_full_screen_frame_is_answered_past_the_feedback_table
        known = len(keys)
        assert backend.turn(OUTPUT, 0, timeout=5.0), "turn_back_refused"
        admitted = hand_off(known)
        end = time.monotonic() + 5
        while (screen := ask(op="screen")["screen"]) != [WIDTH_PX, HEIGHT_PX, 0] and time.monotonic() < end:
            time.sleep(0.05)
        assert screen == [WIDTH_PX, HEIGHT_PX, 0], screen
        answers: dict[str, int] = {}
        for chunk in range(SOAK_COMMITS // SOAK_CHUNK):
            soaked = ask(op="soak", count=SOAK_CHUNK, tag=f"soak{chunk}")
            for kind, count in soaked["answers"].items():
                answers[kind] = answers.get(kind, 0) + count
        last = f"soak{SOAK_COMMITS // SOAK_CHUNK - 1}-{SOAK_CHUNK - 1}"
        end = time.monotonic() + 10
        while last not in presented_tags:       # the shell's event may have come with the last ask
            assert time.monotonic() < end, ("soak_unpresented", last)
            drain()
            time.sleep(0.01)
        last_colour = (RED, BLUE)[(SOAK_CHUNK - 1) % 2]

        def all_last_colour(frame):
            width, height, _ = frame
            samples = [pixel(frame, x, y) for y in range(0, height, 9) for x in range(0, width, 9)]
            assert all(colour == last_colour for colour in samples), ("not_full_screen", histogram(frame))
            return len(samples)

        samples = await_frame(all_last_colour)
        soak_tags = [tag for tag in presented_tags if tag.startswith("soak")]
        print(f"SOAK answers={answers} shell_presented={len(soak_tags)} samples={samples}", flush=True)
        assert answers == {"presented": SOAK_COMMITS}, answers
        assert len(set(soak_tags)) == SOAK_COMMITS, len(set(soak_tags))      # the shell's evidence of each
        print("PASS a_full_screen_frame_is_answered_past_the_feedback_table", flush=True)

        # 7. the_ground_shows_beneath_the_planes
        half_red = [["red", RED, left, top, width, height, 0.5]]
        grounded = ask(op="present", tag="ground", planes=half_red, ground=list(GREY))
        assert isinstance(grounded["result"], int), grounded

        def red_over_grey(frame):
            frame_width, frame_height, _ = frame
            inside = outside = 0
            for y in range(1, frame_height, 7):
                for x in range(1, frame_width, 7):
                    within = [left + d <= x < left + width - d and top + d <= y < top + height - d for d in (-3, 3)]
                    if within[0] != within[1]:
                        continue
                    actual = pixel(frame, x, y)
                    if within[1]:
                        assert near(actual, (159.5, 32, 32)), ("inside", x, y, actual)
                        inside += 1
                    else:
                        assert actual == GREY, ("outside_not_ground", x, y, actual)
                        outside += 1
            return inside, outside

        inside, outside = await_frame(red_over_grey)
        ungrounded = ask(op="present", tag="no-ground", planes=half_red)
        assert isinstance(ungrounded["result"], int), ungrounded
        await_frame(lambda frame: rectangle_check((127.5, 0, 0), frame))
        print(f"GROUND inside={inside} outside={outside}", flush=True)
        print("PASS the_ground_shows_beneath_the_planes", flush=True)

        # 8. a_fade_restacks_moves_and_attaches_nothing
        def fading(strength):
            return [["red", RED, left, top, width, height, 1.0], ["green", GREEN, left, top, width, height, strength]]

        assert isinstance(ask(op="present", tag="fade-0", planes=fading(0.05))["result"], int)
        app_log.flush()
        mark = APP_LOG.stat().st_size
        for index in range(1, FADE_PRESENTS + 1):
            result = ask(op="present", tag=f"fade-{index}", planes=fading(0.05 + 0.09 * index))["result"]
            assert isinstance(result, int), result
        with APP_LOG.open("rb") as log:
            log.seek(mark)
            sent = log.read().decode(errors="replace")
        counts = {request: len(re.findall(pattern, sent)) for request, pattern in (
            ("place_above", r"-> wl_subsurface[#@]\d+\.place_above\("),
            ("set_position", r"-> wl_subsurface[#@]\d+\.set_position\("),
            ("attach", r"-> wl_surface[#@]\d+\.attach\("),
            ("set_multiplier", r"-> wp_alpha_modifier_surface_v1[#@]\d+\.set_multiplier\("),
            ("commit", r"-> wl_surface[#@]\d+\.commit\("))}
        print(f"FADE requests={counts}", flush=True)
        assert counts["commit"] >= 3 * FADE_PRESENTS, ("debug_log_unread", counts)
        assert (counts["place_above"], counts["set_position"], counts["attach"]) == (0, 0, 0), counts
        assert counts["set_multiplier"] == FADE_PRESENTS, counts
        print("PASS a_fade_restacks_moves_and_attaches_nothing", flush=True)

        # 8b. a_burst_ends_on_a_presented_commit: those presents came within one refresh, past the
        # frame client's FEEDBACK_PER_SURFACE answers; the last is still tracked, so the shell
        # names it presented (the trial that follows depends on it).
        last_fade = f"fade-{FADE_PRESENTS}"
        end = time.monotonic() + 5.0
        while last_fade not in presented_tags:
            assert time.monotonic() < end, ("burst_last_unpresented", presented_tags[-5:])
            drain()
            time.sleep(0.01)
        print("PASS a_burst_ends_on_a_presented_commit", flush=True)

        # 9. a_trial_overlay_rides_the_present_that_carries_it
        def until(holds, what: str, seconds: float = 10.0) -> None:
            end = time.monotonic() + seconds
            while not holds():
                assert time.monotonic() < end, (what, presented_tags[-5:], overlay_tags)
                drain()
                time.sleep(0.01)

        boot_ms = time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1_000_000
        trial_id, lighting = uuid.uuid4(), frame_lighting()
        calibration = canonical(Calibration(fit=PositionBox(left=0.25, top=0.25, width=0.5, height=0.5)).model_dump(mode="json"))
        trial = TrialCandidate(
            trial_id=trial_id, generation=1, sequence=1, baseline=admitted, calibration_json=calibration,
            candidate_sha256=candidate_hash(trial_id, 1, 1, admitted, calibration, lighting),
            expires_boottime_ms=boot_ms + 30_000, hard_expires_boottime_ms=boot_ms + 60_000, lighting=lighting)
        backend.trial(admitted.output, encode_trial(trial))
        plain = ask(op="trial_present", overlay=False)
        assert isinstance(plain["result"], int) and plain["tag"] == trial.frame_tag, plain
        until(lambda: trial.frame_tag in presented_tags, "trial_present_unpresented")
        settle = time.monotonic() + 1.0             # long enough for an overlay the shell would draw
        until(lambda: time.monotonic() >= settle, "settle", seconds=5.0)
        assert overlay_tags == [], overlay_tags
        shown = ask(op="trial_present", overlay=True)
        assert isinstance(shown["result"], int) and shown["tag"] == trial.frame_tag, shown
        until(lambda: trial.frame_tag in overlay_tags, "overlay_unpresented")
        print(f"TRIAL tag={trial.frame_tag} overlays={overlay_tags}", flush=True)
        backend.trial(admitted.output, None)
        print("PASS a_trial_overlay_rides_the_present_that_carries_it", flush=True)

        # 10. an_enlarged_picture_fills_its_placement
        half = [["small", "quadrants", left, top, width, height, 1.0, [width // 2, height // 2]]]
        enlarged = ask(op="present", tag="enlarged", planes=half)
        assert isinstance(enlarged["result"], int), enlarged

        def quadrants_fill_the_rectangle(frame):
            frame_width, frame_height, _ = frame
            inside = outside = 0
            for y in range(1, frame_height, 5):
                for x in range(1, frame_width, 5):
                    u, v = (x - left) / width, (y - top) / height
                    margin = 4 / min(width, height)
                    if any(abs(c - edge) < margin for c in (u, v) for edge in (0, .5, 1)):
                        continue            # an edge of the rectangle or of a quadrant
                    actual = pixel(frame, x, y)
                    if 0 < u < 1 and 0 < v < 1:
                        assert near(actual, quadrant_colour(u, v)), ("enlarged", x, y, actual)
                        inside += 1
                    else:
                        assert actual == BLACK, ("outside_not_black", x, y, actual)
                        outside += 1
            return inside, outside

        inside, outside = await_frame(quadrants_fill_the_rectangle)
        print(f"ENLARGED inside={inside} outside={outside}", flush=True)
        assert inside > 800 and outside > 5000, (inside, outside)
        print("PASS an_enlarged_picture_fills_its_placement", flush=True)
    except BaseException:
        app_log.flush()
        print("APP LOG:\n" + APP_LOG.read_text()[-4000:], flush=True)
        raise
    finally:
        if child is not None:
            child.kill()
            child.wait(timeout=5)
        stop_weston(weston)


if __name__ == "__main__":
    if sys.argv[1:] == ["--app"]:
        app()
    else:
        main()
