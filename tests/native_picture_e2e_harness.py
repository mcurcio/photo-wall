"""1c I1 end-to-end picture harness: an Output document's picture part changes the pixels Weston
shows, through the picture controller, the shell and the Player.

Run only inside the display harness container (scripts/run_display_harness.py), after
native_renderer_pixel_harness.py, with `player` and `contracts` mounted read-only at /repo and
`tests` at /smoke. Everything on the path is the real one: a headless Weston with the installed
photo-wall shell (`--debug`, so `weston_capture_v1` reads its framebuffer), the installed Display
Host's WestonBackend and DisplayHost on the shell's control socket (allow, candidate, handoff, as
the runner does), the installed PictureController on a DDC/CI channel whose ddcutil never answers
(a fake bus, exit 1), and a child process running the real NativeRenderer with the real frame
client (PHOTO_WALL_DISPLAY_HOST=1) as an unprivileged user, showing one flat grey Frame on a
Cover fit whose opening is smaller than the Output. The shell's private client is the harness
health client in its default mode: a mapped health layer (so the shell raises no fallback tint)
that tints only the Output's bottom-right quarter, which no sample reads.

1. a_picture_document_changes_the_pixels_and_comes_back_presented: before any picture document,
   pixels inside the opening are the neutral reference and outside are black. A document with
   brightness 30 % and contrast 25 % as picture adjustment (picture change 3) makes the
   controller's report say `presented_change` 3, and the captured pixels inside the opening equal
   `final_pixel` of the operations the controller computed for that document; outside every
   opening they stay exactly black although contrast below 50 lifts black (the mask is last).
"""
import mmap
import os
import pathlib
import shutil
import socket
import struct
import subprocess
import sys
import time
import uuid
import zlib

sys.path[:0] = ["/repo", "/smoke"]
DISPLAY = "/usr/lib/photo-wall/node-display"
sys.path.append(DISPLAY)        # the installed Display Host (`appliance` is a namespace package)
ROOT = pathlib.Path("/tmp/pw-picture-e2e")
SOCKET = "wayland-picture"
PLAYER_UID = 10004
OUTPUT = "headless"
WIDTH_PX, HEIGHT_PX = 640, 480
PHOTO = pathlib.Path("/tmp/pw-picture-e2e-grey.png")
GREY = 128
OPENING = ((.2, .2), (.8, .25), (.75, .8), (.25, .75))
ROTATION = 7.0
TOLERANCE = 0.02            # the GL reference's 0.015 plus the framebuffer's 8-bit rounding
PICTURE_CHANGE = 3
PLAYER_ENV = {"XDG_RUNTIME_DIR": str(ROOT), "WAYLAND_DISPLAY": SOCKET, "GDK_BACKEND": "wayland",
              "LIBGL_ALWAYS_SOFTWARE": "1", "PYOPENGL_PLATFORM": "egl", "GSETTINGS_BACKEND": "memory",
              "PHOTO_WALL_DISPLAY_HOST": "1", "HOME": "/tmp/pw-picture-e2e-home"}


def grey_png(path: pathlib.Path) -> bytes:
    """A 64x48 opaque grey RGBA PNG (stdlib zlib)."""
    rows = b"".join(b"\x00" + bytes((GREY, GREY, GREY, 255)) * 64 for _ in range(48))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    payload = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 48, 8, 6, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    path.write_bytes(payload)
    return payload


# --- the Player (child process, unprivileged) ---------------------------------------------------

def player() -> None:
    """Show the grey Frame on a Cover fit for ever, presenting every 50 ms on real GLib."""
    import hashlib

    from gi.repository import GLib

    from contracts.models import Calibration, CoverFit, FrameProfile, Layer, OutputBinding, Variant
    from contracts.photo_crop import CENTRED
    from player.native import NativeOutput, NativeRenderer
    from player.rendering import LocalLayer, OutputComposition

    payload = PHOTO.read_bytes()
    variant = Variant(asset_id="asset-" + hashlib.sha256(payload).hexdigest()[:16], recipe_id="e" * 64, sha256=hashlib.sha256(payload).hexdigest(), size=len(payload),
                      media_type="image/png", width=64, height=48)
    binding = OutputBinding(output_id=OUTPUT, frame_id="frame", generation=1,
                            profile=FrameProfile(width_px=1600, height_px=900, diagonal_inches=10))
    layer = LocalLayer(Layer(assignment_id="grey", run_id="run", output_id=OUTPUT, frame_id="frame",
                             binding_generation=1, start=0, end=10**9, media_origin=0, variant=variant,
                             crop=CENTRED),
                       PHOTO, 0.0, 1.0)
    shown = OutputComposition(binding, Calibration(fit=CoverFit(corners=OPENING, rotation=ROTATION)), (layer,))
    native = NativeRenderer((NativeOutput(OUTPUT, "photo-wall-headless", WIDTH_PX, HEIGHT_PX),))
    native.set_unbound_outputs((), None)
    native.set_identify_output(None)        # as player/service.py does: no identify banner
    context = GLib.MainContext.default()
    last = None
    while True:
        native.prepare(layer)
        status = native.present(shown).status
        if status != last:
            print(f"PLAYER {status}", flush=True)
            last = status
        end = time.monotonic() + 0.05
        while time.monotonic() < end:
            while context.iteration(False):
                pass
            time.sleep(0.005)


# --- the harness (root): Weston, the controller side, the capture -------------------------------

def protocols():
    """Python bindings for Weston's capture protocol, generated as native_display_smoke.py does."""
    generated = pathlib.Path("/tmp/pw-picture-e2e-protocols")
    subprocess.run([sys.executable, "-m", "pywayland.scanner", "-i", "/usr/share/wayland/wayland.xml",
                    "/usr/share/libweston-14/protocols/weston-output-capture.xml",
                    "-o", str(generated / "pw_e2e_protocols")], check=True)
    (generated / "pw_e2e_protocols" / "__init__.py").touch()
    sys.path.insert(0, str(generated))


SHM_FORMAT = {0x34325258: 1, 0x34325241: 0}  # DRM XRGB8888, ARGB8888 -> wl_shm


def capture() -> tuple[int, int, bytes]:
    """(width, height, XRGB bytes) of Weston's framebuffer, through weston_capture_v1."""
    from pw_e2e_protocols.wayland import WlOutput, WlShm
    from pw_e2e_protocols.weston_output_capture import WestonCaptureV1
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
            fd = os.memfd_create("pw-e2e-capture")
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


def compare(operations) -> tuple[int, int]:
    """Every sampled pixel clear of the opening's edges equals the reference: inside, the grey
    through `final_pixel` with `operations`; outside, exactly black. The (inside, outside) counts;
    AssertionError at the first that differs."""
    from player.geometry import decode_channel, final_pixel, inside_opening

    width, height, data = capture()
    linear = (decode_channel(GREY / 255),) * 3
    expected_inside = final_pixel(linear, operations, inside=True)[:3]
    counts = [0, 0]
    for j in range(4, height, 9):
        for i in range(4, width, 9):
            if i >= width * 3 // 4 and j >= height * 3 // 4:
                continue                # the health client's tint
            x, y = (i + .5) / width, (j + .5) / height
            near = {inside_opening(OPENING, x + dx / width, y + dy / height)
                    for dx in (-3, 0, 3) for dy in (-3, 0, 3)}
            if len(near) != 1:
                continue
            offset = (j * width + i) * 4
            blue, green, red = data[offset:offset + 3]
            if near.pop():
                actual = (red / 255, green / 255, blue / 255)
                assert max(abs(a - e) for a, e in zip(actual, expected_inside, strict=True)) <= TOLERANCE, (
                    "inside", i, j, actual, expected_inside)
                counts[0] += 1
            else:
                assert (red, green, blue) == (0, 0, 0), ("outside_not_black", i, j, (red, green, blue))
                counts[1] += 1
    return counts[0], counts[1]


def await_compare(operations, drain, seconds: float = 8.0) -> tuple[int, int]:
    """`compare` until it holds or `seconds` pass, draining the shell's events between tries."""
    end = time.monotonic() + seconds
    while True:
        drain()
        try:
            return compare(operations)
        except AssertionError:
            if time.monotonic() >= end:
                print(f"CAPTURED {histogram()}", flush=True)
                raise
            time.sleep(0.2)


def histogram() -> list:
    """The captured frame's eight most common colours with their counts and one place each."""
    width, height, data = capture()
    counts: dict = {}
    for j in range(0, height, 4):
        for i in range(0, width, 4):
            offset = (j * width + i) * 4
            colour = tuple(reversed(data[offset:offset + 3]))
            count, where = counts.get(colour, (0, (i, j)))
            counts[colour] = (count + 1, where)
    return sorted(counts.items(), key=lambda item: -item[1][0])[:8]


class Reports:
    """PictureReports: the latest picture part of each Output's report."""

    def __init__(self) -> None:
        self.latest = {}

    def put_picture(self, output_id, picture) -> None:
        self.latest[output_id] = picture


def fake_drm() -> pathlib.Path:
    """A DRM root whose `card0-headless` names i2c bus 7, so the channel runs ddcutil there."""
    root = pathlib.Path("/tmp/pw-picture-e2e-drm")
    connector = root / f"card0-{OUTPUT}"
    connector.mkdir(parents=True, exist_ok=True)
    if not (connector / "ddc").is_symlink():
        (connector / "ddc").symlink_to("../../i2c-7")
    return root


def never_answers(argv, _timeout):
    """The fake ddcutil: every command finds no display."""
    return subprocess.CompletedProcess(list(argv), 1, "", "Display not found\n")


def main() -> None:
    protocols()
    from appliance.display_host.ddc import DdcChannel
    from appliance.display_host.picture import (
        DdcPictureAdapter,
        FileAppliedPictures,
        PictureController,
        operations_for,
    )
    from appliance.display_host.weston import (
        CompositorPresentation,
        DiagnosticRelease,
        SurfaceGrant,
        WestonBackend,
    )
    from contracts.node_display import Surface
    from contracts.node_frame import neutral_operations
    from contracts.node_output import (
        AS_THE_DISPLAY_HAS_IT,
        BEST_DETECTED,
        ActsOn,
        OutputDocument,
        OutputPicture,
        PictureSetting,
        Power,
        PowerRequest,
        RequestReason,
        decode_output_document,
        encode_output_document,
    )
    from contracts.node_protocol import NodeProcessIdentity, OutputKey

    # The shell's private client: the harness health client in its default mode.
    shutil.copyfile("/smoke/display_harness_health_client.py", DISPLAY + "/diagnostic-client")
    os.chmod(DISPLAY + "/diagnostic-client", 0o755)
    pathlib.Path("/tmp/pw-health-client-mode").unlink(missing_ok=True)
    ROOT.mkdir(mode=0o711, exist_ok=True)
    ROOT.chmod(0o711)
    home = pathlib.Path(PLAYER_ENV["HOME"])
    home.mkdir(exist_ok=True)
    os.chown(home, PLAYER_UID, PLAYER_UID)
    grey_png(PHOTO)
    PHOTO.chmod(0o644)
    env = {**os.environ, "XDG_RUNTIME_DIR": str(ROOT), "PHOTO_WALL_CONTROLLER_UID": "0"}
    weston_log = open("/tmp/pw-weston.log", "w")
    weston = subprocess.Popen(
        ["weston", "--backend=headless-backend.so", "--renderer=pixman",
         f"--shell={DISPLAY}/photo-wall-shell.so", f"--width={WIDTH_PX}", f"--height={HEIGHT_PX}",
         "--idle-time=0", f"--socket={SOCKET}", "--no-config", "--debug"],
        env=env, stdout=weston_log, stderr=weston_log)
    app = None
    player_log = open("/tmp/pw-picture-e2e-player.log", "w")
    controller = None
    try:
        for _ in range(200):
            if (ROOT / "control.sock").exists() and (ROOT / SOCKET).exists():
                break
            assert weston.poll() is None, "weston_failed"
            time.sleep(0.05)
        (ROOT / SOCKET).chmod(0o666)
        channel = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        channel.connect(str(ROOT / "control.sock"))
        app = subprocess.Popen([sys.executable, "-B", __file__, "--player"], env={**os.environ, **PLAYER_ENV},
                               preexec_fn=lambda: (os.setgid(PLAYER_UID), os.setuid(PLAYER_UID)),
                               stdout=player_log, stderr=subprocess.STDOUT)
        alive = lambda grant: pathlib.Path(f"/proc/{grant.surface.process.pid}").exists()  # noqa: E731
        backend = WestonBackend(channel, display_uid=0, compositor_pid=weston.pid, verify_process=alive)
        host = backend.initialize()

        def pump(wanted, seconds: float = 10.0):
            """Dispatch the shell's events (on this thread only, as the runner's main loop does)
            until `wanted(observed)`; that observation."""
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                observed = backend.dispatch()
                if observed is not None and wanted(observed):
                    return observed
                if observed is None:
                    time.sleep(0.01)
            raise AssertionError("pump_timeout")

        def drain() -> None:
            """Dispatch every event already queued (the shell drops a control peer that lags)."""
            for _ in range(1000):
                if backend.dispatch() is None:
                    return

        key = pump(lambda seen: isinstance(seen, OutputKey) and seen.output_id == OUTPUT)
        ticks = int(pathlib.Path(f"/proc/{app.pid}/stat").read_text().rpartition(") ")[2].split()[19])
        surface = Surface(key, NodeProcessIdentity(app.pid, ticks, uuid.uuid4()), 1, 1, 1, "frame")
        backend.allow(SurfaceGrant(surface, PLAYER_UID, True))
        host.offer(surface)
        # The handoff names a recent presented buffer; a newer one may supersede it, so retry.
        for _attempt in range(20):
            presented = pump(lambda seen: isinstance(seen, CompositorPresentation), seconds=20)
            try:
                host.authorize_handoff(surface, now_ms=presented.observed_monotonic_ms)
                break
            except ValueError:
                continue
        pump(lambda seen: isinstance(seen, DiagnosticRelease))
        print("PASS the_player_is_handed_the_output", flush=True)

        inside, outside = await_compare(neutral_operations(OUTPUT, 1), drain)
        print(f"NEUTRAL inside={inside} outside={outside}", flush=True)
        assert inside > 300 and outside > 1000, (inside, outside)

        reports = Reports()
        applied = pathlib.Path("/tmp/pw-picture-e2e-applied")
        applied.mkdir(exist_ok=True)
        controller = PictureController(DdcPictureAdapter(DdcChannel(never_answers, drm_root=fake_drm())),
                                       backend, reports, FileAppliedPictures(applied), time.monotonic)
        backend.picture_events = controller
        controller.output(OUTPUT, connected=True, identity=None)
        controller.start()
        picture = OutputPicture(PictureSetting(ActsOn.PICTURE_ADJUSTMENT, 30),
                                PictureSetting(ActsOn.PICTURE_ADJUSTMENT, 25), 6500, 22)
        standing = PowerRequest("standing", Power.ON, RequestReason.STANDING)
        # The document as it crosses the wire for HDMI-A-1, then retargeted at this compositor's one
        # Output: Weston's headless backend names it "headless", which no Node Output is called.
        document = decode_output_document(encode_output_document(OutputDocument(
            "HDMI-A-1", 1, (standing,), BEST_DETECTED, False, False, PICTURE_CHANGE, picture)))
        object.__setattr__(document, "output_id", OUTPUT)
        controller.document(OUTPUT, document)
        end = time.monotonic() + 15
        while True:
            report = reports.latest.get(OUTPUT)
            if report is not None and report.presented_change == PICTURE_CHANGE:
                break
            assert time.monotonic() < end, ("presented_change", report)
            drain()
            time.sleep(0.01)
        print(f"REPORT {report}", flush=True)
        assert report.for_change == PICTURE_CHANGE and not report.brightness_answered, report
        assert picture.brightness.value != AS_THE_DISPLAY_HAS_IT
        operations = operations_for(OUTPUT, PICTURE_CHANGE, picture)
        assert operations.contrast_slope < 1 and operations.light_factor < 1, operations
        inside, outside = await_compare(operations, drain)
        print(f"ADJUSTED inside={inside} outside={outside}", flush=True)
        assert inside > 300 and outside > 1000, (inside, outside)
        print("PASS a_picture_document_changes_the_pixels_and_comes_back_presented", flush=True)
    except BaseException:
        player_log.flush()
        print("PLAYER LOG:\n" + pathlib.Path("/tmp/pw-picture-e2e-player.log").read_text()[-4000:], flush=True)
        raise
    finally:
        if controller is not None:
            controller.stop(2.0)
        if app is not None:
            app.kill()
            app.wait(timeout=5)
        weston.terminate()
        try:
            weston.wait(timeout=3)
        except subprocess.TimeoutExpired:
            weston.kill()


if __name__ == "__main__":
    if sys.argv[1:] == ["--player"]:
        player()
    else:
        main()
