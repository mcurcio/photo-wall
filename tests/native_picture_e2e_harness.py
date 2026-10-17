"""1c I1 end-to-end picture harness: a picture document's software group (decision 0022) changes the
pixels Weston shows, through the picture controller, the shell and the Player.

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
   software brightness 30 % and contrast 25 % (100 % changes nothing; picture change 3) and no
   hardware value makes the controller's report say `presented_change` 3, and the captured pixels
   inside the opening equal `final_pixel` of the operations the controller computed for that
   document; outside every opening they stay exactly black although contrast below 100 lifts black
   (the mask is last).
2. neutral_is_v4 (print look, 2026-10-12, P3): a v5 document whose software group is neutral,
   print look included, on a binding whose Mat is Mat(): presented, and the pixels equal picture
   operations v4's formula (written out here, not player/geometry.py's).
3. tracer_black_floor_and_one_light (the design's tracer): a document with software black floor
   2 % (black_floor_tenths 20) and all else neutral, on a binding whose Mat is 2 mm deep with one
   light from the wall's up (steep 45, no softness, strength 1.0, 6500 K): presented, and the
   pixels equal `final_pixel` with the floor share 0.02 (worked here from the tenths, so a
   controller that compiled the floor another way fails) and `mat_shade`'s shade, including the
   point 1 mm below the picture's top edge, in the light's shadow.
4. lut_leg_waits_for_the_lut (the tracer's second leg): a document naming a LUT (a fixture table)
   at amount 80. The Player has no Central here: its PlayerLuts is fed by this harness (the
   Player holds the fixture bytes once a release file appears, as the service does after its
   digest-checked fetch). Before the release the report's presented change stays below the
   document's and the pixels are the sliders-only reference; after it the presented change
   reaches the document's and the pixels are the reference with the LUT.

The Player child reads the binding's Mat from MAT_FILE (absent: Mat()) on every present, so the
harness moves the Frame's mat as Central would move the binding.
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
from dataclasses import replace

sys.path[:0] = ["/repo", "/smoke"]
DISPLAY = "/usr/lib/photo-wall/node-display"
KERNEL = "/usr/lib/photo-wall/node-kernel"
# The installed Display Host and the node kernel it imports (`appliance` is a namespace package),
# on the path as the display-controller launcher puts them.
sys.path += [DISPLAY, KERNEL]
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
PROFILE = dict(width_px=1600, height_px=900, diagonal_inches=10)
MAT_FILE = pathlib.Path("/tmp/pw-picture-e2e-mat.json")             # the binding's Mat, for the Player
LUT_FILE = pathlib.Path("/tmp/pw-picture-e2e-lut.bin")              # the fixture LUT's bytes
LUT_RELEASE = pathlib.Path("/tmp/pw-picture-e2e-lut-release")       # present: the Player holds them
LUT_PROFILE_SHA256, LUT_RECIPE = "ef" * 32, "lut33-perceptual-relcol-bpc-1"
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

    from contracts.lut import LutRef
    from contracts.models import (
        Calibration,
        CoverFit,
        FrameProfile,
        Layer,
        Mat,
        OutputBinding,
        Variant,
    )
    from contracts.photo_crop import CENTRED
    from player.luts import PlayerLuts
    from player.native import NativeOutput, NativeRenderer
    from player.rendering import LocalLayer, OutputComposition

    payload = PHOTO.read_bytes()
    variant = Variant(asset_id="asset-" + hashlib.sha256(payload).hexdigest()[:16], recipe_id="e" * 64, sha256=hashlib.sha256(payload).hexdigest(), size=len(payload),
                      media_type="image/png", width=64, height=48)
    binding = OutputBinding(output_id=OUTPUT, frame_id="frame", generation=1, profile=FrameProfile(**PROFILE), mat=Mat(),
                            calibration=Calibration(fit=CoverFit(corners=OPENING, rotation=ROTATION)))
    layer = LocalLayer(Layer(assignment_id="grey", run_id="run", output_id=OUTPUT, frame_id="frame",
                             binding_generation=1, start=0, end=10**9, media_origin=0, variant=variant,
                             crop=CENTRED),
                       PHOTO, 0.0, 1.0)
    luts = PlayerLuts()
    native = NativeRenderer((NativeOutput(OUTPUT, "photo-wall-headless", WIDTH_PX, HEIGHT_PX),), luts=luts)
    native.set_unbound_outputs((), None)
    native.set_identify_output(None)        # as player/service.py does: no identify banner
    context = GLib.MainContext.default()
    last = None
    while True:
        mat = Mat.model_validate_json(MAT_FILE.read_text()) if MAT_FILE.exists() else Mat()
        shown = OutputComposition(binding.model_copy(update={"mat": mat}), (layer,))
        if LUT_RELEASE.exists() and luts.wanted():
            luts.hold(LutRef(LUT_PROFILE_SHA256, LUT_RECIPE), LUT_FILE.read_bytes())
            print("PLAYER lut held", flush=True)
        native.prepare(layer, shown.binding)
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


def grey_linear() -> tuple[float, float, float]:
    from player.geometry import decode_channel

    return (decode_channel(GREY / 255),) * 3


def compare(operations, *, reference=None) -> tuple[int, int]:
    """Every sampled pixel clear of the opening's edges equals the reference: inside, the grey
    through `final_pixel` with `operations` (or `reference(x, y)`, an RGB, when given); outside,
    exactly black. The (inside, outside) counts; AssertionError at the first that differs."""
    from player.geometry import final_pixel, inside_opening

    width, height, data = capture()
    flat = final_pixel(grey_linear(), operations, inside=True)[:3] if reference is None else None
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
                expected_inside = flat if reference is None else reference(x, y)
                actual = (red / 255, green / 255, blue / 255)
                assert max(abs(a - e) for a, e in zip(actual, expected_inside, strict=True)) <= TOLERANCE, (
                    "inside", i, j, actual, expected_inside)
                counts[0] += 1
            else:
                assert (red, green, blue) == (0, 0, 0), ("outside_not_black", i, j, (red, green, blue))
                counts[1] += 1
    return counts[0], counts[1]


def await_compare(operations, drain, seconds: float = 8.0, *, reference=None) -> tuple[int, int]:
    """`compare` until it holds or `seconds` pass, draining the shell's events between tries."""
    end = time.monotonic() + seconds
    while True:
        drain()
        try:
            return compare(operations, reference=reference)
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
    """PictureReports: the latest picture report of each Output."""

    def __init__(self) -> None:
        self.latest = {}

    def put_picture(self, report) -> None:
        self.latest[report.output_id] = report


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
    import contracts.node_picture as node_picture
    from appliance.display_host.ddc import DdcChannel
    from appliance.display_host.picture import (
        DdcPictureAdapter,
        FileAppliedPictures,
        PictureController,
        RestartSignal,
        operations_for,
    )
    from appliance.display_host.picture_list import FileOfferedCache, PictureListAsker
    from appliance.display_host.signal_range import DrmConnectorRanges, FileRangeRequests
    from appliance.display_host.weston import (
        CompositorPresentation,
        DiagnosticRelease,
        SurfaceGrant,
        WestonBackend,
    )
    from contracts.node_display import Surface
    from contracts.node_frame import neutral_operations
    from contracts.node_picture import (
        PictureDocument,
        SoftwarePicture,
        decode_picture_document,
        encode_picture_document,
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
        # Weston's headless backend names its one Output "headless", which no Node Output is called:
        # the picture wire's Output ids are widened by that one name for this harness only, so the
        # controller's documents and reports name the compositor's Output.
        node_picture.OUTPUT_IDS = (*node_picture.OUTPUT_IDS, OUTPUT)
        channel = DdcChannel(never_answers, drm_root=fake_drm())
        controller = PictureController(DdcPictureAdapter(channel), PictureListAsker(channel), FileOfferedCache(applied),
                                       backend, reports, FileAppliedPictures(applied), time.monotonic,
                                       RestartSignal(DrmConnectorRanges(fake_drm()),
                                                     FileRangeRequests(applied / "signal-range.json"), channel))
        backend.picture_events = controller
        controller.output(OUTPUT, connected=True, identity=None)
        controller.start()
        wanted = SoftwarePicture(30, 25, 6500, 22)
        # The document as it crosses the wire for HDMI-A-1, then retargeted at the headless Output.
        document = replace(decode_picture_document(encode_picture_document(
            PictureDocument("HDMI-A-1", PICTURE_CHANGE, {}, wanted))), output_id=OUTPUT)
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
        assert report.for_change == PICTURE_CHANGE and report.results == {}, report   # no hardware value
        operations = operations_for(OUTPUT, PICTURE_CHANGE, wanted, None)
        assert operations.contrast_slope < 1 and operations.light_factor < 1, operations
        inside, outside = await_compare(operations, drain)
        print(f"ADJUSTED inside={inside} outside={outside}", flush=True)
        assert inside > 300 and outside > 1000, (inside, outside)
        print("PASS a_picture_document_changes_the_pixels_and_comes_back_presented", flush=True)

        def send(change: int, software) -> None:
            """The document as it crosses the wire for HDMI-A-1, retargeted at the headless Output."""
            controller.document(OUTPUT, replace(decode_picture_document(encode_picture_document(
                PictureDocument("HDMI-A-1", change, {}, software))), output_id=OUTPUT))

        def presented(change: int, seconds: float = 15.0):
            end = time.monotonic() + seconds
            while True:
                report = reports.latest.get(OUTPUT)
                if report is not None and report.presented_change == change:
                    return report
                assert time.monotonic() < end, ("presented_change", change, report)
                drain()
                time.sleep(0.01)

        print_look_cases(send, presented, drain, reports, SoftwarePicture)
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


# --- the print look (P3): the design's tracer, end to end ------------------------------------------

def v4_pixel(linear, operations) -> tuple[float, float, float]:
    """Picture operations v4's final pass as it shipped (1c), written out: the oracle for "neutral
    is today"."""
    def encode(value: float) -> float:
        value = min(1.0, max(0.0, value))
        return 12.92 * value if value <= .0031308 else 1.055 * value ** (1 / 2.4) - .055
    pixel = []
    for value, multiplier in zip(linear, operations.rgb_multipliers, strict=True):
        value = min(1.0, max(0.0, value * multiplier * operations.light_factor))
        value = (encode(value) - .5) * operations.contrast_slope + .5
        pixel.append(min(1.0, max(0.0, value) ** operations.gamma_exponent))
    return tuple(pixel)


def lut_fixture() -> bytes:
    """A warm table (contracts/lut.py layout): red kept, green 85 %, blue 60 %."""
    from contracts.lut import LUT_BYTES, LUT_GRID, lut_offset

    body = bytearray(LUT_BYTES)
    for r in range(LUT_GRID):
        for g in range(LUT_GRID):
            for b in range(LUT_GRID):
                offset = lut_offset(r, g, b)
                body[offset:offset + 3] = bytes((round(r * 255 / (LUT_GRID - 1)),
                                                 round(g * 255 / (LUT_GRID - 1) * .85),
                                                 round(b * 255 / (LUT_GRID - 1) * .6)))
    return bytes(body)


def print_look_cases(send, presented, drain, reports, SoftwarePicture) -> None:
    from contracts.lut import LutRef
    from contracts.models import CoverFit, FrameProfile, Light, Mat
    from contracts.node_frame import LutMix, PictureOperations
    from player.geometry import final_pixel, homography, mat_shade, mat_shadow, transform

    fit = CoverFit(corners=OPENING, rotation=ROTATION)
    profile = FrameProfile(**PROFILE)
    neutral = SoftwarePicture(100, 100, 6500, 22)

    # 2. neutral_is_v4
    MAT_FILE.unlink(missing_ok=True)
    send(4, neutral)
    presented(4)
    operations = PictureOperations(OUTPUT, 4, (1.0, 1.0, 1.0), 1.0, 1.0, 1.0)
    v4 = v4_pixel(grey_linear(), operations)
    inside, outside = await_compare(operations, drain, reference=lambda _x, _y: v4)
    print(f"NEUTRAL_V4 inside={inside} outside={outside}", flush=True)
    assert inside > 300 and outside > 1000, (inside, outside)
    print("PASS neutral_is_v4", flush=True)

    # 3. tracer_black_floor_and_one_light
    mat = Mat(depth_mm=2, lights=(Light(from_degrees=0, steep_degrees=45, softness_degrees=0,
                                        strength_tenths=10, colour_kelvin=6500),))
    MAT_FILE.write_text(mat.model_dump_json())
    MAT_FILE.chmod(0o644)
    floored = SoftwarePicture(100, 100, 6500, 22, black_floor_tenths=20)
    send(5, floored)
    report = presented(5)
    expected = PictureOperations(OUTPUT, 5, (1.0, 1.0, 1.0), 1.0, 1.0, 1.0,
                                 black_floor=20 / 1000)
    shadow = mat_shadow(fit, profile, mat)

    def shaded(x: float, y: float) -> tuple[float, float, float]:
        return final_pixel(grey_linear(), expected, inside=True, shade=mat_shade(shadow, x, y))[:3]

    inside, outside = await_compare(expected, drain, reference=shaded)
    # The point 1 mm below the opening's top edge (mat space), mid-width: in the light's shadow.
    to_output = homography(fit.corners)
    u, v = .5, 1 / shadow.size_mm[1]
    x, y = transform(to_output, u, v)
    assert mat_shade(shadow, x, y) == (.5, .5, .5), mat_shade(shadow, x, y)
    width, height, data = capture()
    i, j = int(x * width), int(y * height)
    centre_x, centre_y = (i + .5) / width, (j + .5) / height
    offset = (j * width + i) * 4
    blue, green, red = data[offset:offset + 3]
    want = shaded(centre_x, centre_y)
    actual = (red / 255, green / 255, blue / 255)
    print(f"TRACER inside={inside} outside={outside} report={report} one_mm={actual} want={want}", flush=True)
    assert max(abs(a - e) for a, e in zip(actual, want, strict=True)) <= TOLERANCE, ("one_mm", actual, want)
    assert want[0] < final_pixel(grey_linear(), expected, inside=True)[0] - .1, want      # shaded
    assert inside > 300 and outside > 1000, (inside, outside)
    print("PASS tracer_black_floor_and_one_light", flush=True)

    # 4. lut_leg_waits_for_the_lut
    MAT_FILE.unlink(missing_ok=True)
    table = lut_fixture()
    LUT_FILE.write_bytes(table)
    LUT_FILE.chmod(0o644)
    LUT_RELEASE.unlink(missing_ok=True)
    ref = LutRef(LUT_PROFILE_SHA256, LUT_RECIPE)
    looked = SoftwarePicture(100, 100, 6500, 22, black_floor_tenths=20, lut=ref, lut_amount_percent=80)
    send(6, looked)
    sliders = PictureOperations(OUTPUT, 6, (1.0, 1.0, 1.0), 1.0, 1.0, 1.0,
                                black_floor=20 / 1000)
    inside, _ = await_compare(sliders, drain)
    end = time.monotonic() + 2.0
    while time.monotonic() < end:           # the Player draws on with the sliders; Done waits
        drain()
        report = reports.latest.get(OUTPUT)
        assert report is None or report.presented_change is None or report.presented_change < 6, (
            "presented_before_the_lut", report)
        time.sleep(0.05)
    compare(sliders)
    LUT_RELEASE.touch()
    LUT_RELEASE.chmod(0o644)
    report = presented(6)
    with_lut = replace(sliders, lut=LutMix(ref, .8))
    lut_pixel = final_pixel(grey_linear(), with_lut, inside=True, lut=table)[:3]
    inside_after, _ = await_compare(with_lut, drain, reference=lambda _x, _y: lut_pixel)
    print(f"LUT before={inside} after={inside_after} report={report} pixel={lut_pixel}", flush=True)
    assert lut_pixel[2] < lut_pixel[0] - .1, lut_pixel          # the warm table is drawn
    print("PASS lut_leg_waits_for_the_lut", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--player"]:
        player()
    else:
        main()
