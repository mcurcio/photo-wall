"""1c I1 end-to-end picture harness: a picture document's software group (decision 0022) changes the
pixels Weston shows, through the picture controller, the shell and the Player.

Run only inside the display harness container (scripts/run_display_harness.py), after
native_fit_pass_harness.py, with `player` and `contracts` mounted read-only at /repo and
`tests` at /smoke. Everything on the path is the real one: a headless Weston with the installed
photo-wall shell (`--debug`, so `weston_capture_v1` reads its framebuffer), the installed Display
Host's WestonBackend and DisplayHost on the shell's control socket (allow, candidate, handoff, as
the runner does), the installed PictureController on a DDC/CI channel whose ddcutil never answers
(a fake bus, exit 1), and a child process, an unprivileged user, running the new path's Player
(render redesign S3, bead B7, run ledger .claude/runs/render-planes/s3-page.md): the real
player/plane_renderer.py `PlaneRenderer` with the real `PlaneScreens` on the installed frame client
library, `FitPass`, `PatternPass`, `StillDecode` and `PlayerLuts`, drawing on tests/harness_device.py's
`MemfdDevice` (software GL, memfd-backed LINEAR buffers: the container has no render node), and
presenting and finishing every 50 ms on real GLib. It shows one flat grey Frame whose Position is a
`PositionBox` (centred, 384 x 216 pixels) on the 640 x 480 headless Output, so its placement
(contracts/frame_fit.py `place_box`) is smaller than the Output. Weston runs its GL renderer on
llvmpipe: its pixman renderer offers no zwp_linux_dmabuf_v1, which the planes' pictures need
(native_screen_harness.py). The shell's private client is the harness health client in its default
mode: a mapped health layer (so the shell raises no fallback tint) that tints only the Output's
bottom-right quarter, which no sample reads.

Every comparison samples the capture clear of the placement's edges: inside, the pixel equals the
case's reference at that picture point (player/geometry.py `placement_uv`); outside, it is exactly
black (the screen's ground).

1. a_picture_document_changes_the_pixels_and_comes_back_presented: before any picture document,
   pixels inside the placement are the neutral reference and outside are black. A document with
   software contrast 25 % (100 % changes nothing; picture change 3) and no hardware value makes the
   controller's report say `presented_change` 3, and the captured pixels inside the placement equal
   `final_pixel` of the operations the controller computed for that document; outside it they stay
   exactly black although contrast below 100 lifts black (the ground is the screen's black).
2. neutral_is_todays_picture (decision 0028): a neutral document on a binding with the upgrade's
   light (one wash at the matched lux, paper that matches the TV): presented, and the pixels equal
   the 1c final pass at neutral sliders (written out here, not player/geometry.py's).
3. tracer_matched_light_and_one_far_light: a document moving only the Display's matched lux (800
   lx, picture operations v8 through the controller and the shell), on a binding lit by a wash
   and a far light from the wall's up on a 2 mm mat: presented, and the pixels equal `final_pixel`
   under those lights (`picture_lights`), including the point 1 mm below the picture's top edge, in
   the far light's shadow.
4. the_frames_paper_profile_joins_when_held: the binding's paper names a profile (a fixture table).
   The Player has no Central here: its PlayerLuts is fed by this harness (the Player holds the
   fixture bytes once a release file appears, as the service does after its digest-checked fetch).
   Before the release the pixels are the reference without the LUT; after it, with it.
5. a_test_pattern_shows_inside_the_placement: a document asking `TestPattern.WHITE` with
   non-neutral software (picture change 7): the report says `presented_change` 7 and
   `test_pattern_shown` that ask, and the pixels inside the placement equal `display_steps` of
   white under the controller's operations; outside it they are exactly black.

The Player child reads the binding's lighting from LIGHTING_FILE (absent: the upgrade's) on every
present and draws with a room-lights document holding the room light full on, so the harness moves
the Frame's lighting as Central would move the binding.
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
# Its crop (512 x 288) is larger than the Frame's placement (384 x 216), so the picture is finished at
# the placement (player/planes.py `finish_size`) and every pixel is held to the look's reference;
# a smaller source is finished smaller and enlarged, softening the mat shadow's edge.
PHOTO_SIZE = (512, 384)
GREY = 128
BOX = dict(left=.2, top=.275, width=.6, height=.45)     # the Frame's Position (a PositionBox): 384 x 216 at (128, 132)
TOLERANCE = 0.02            # the GL reference's 0.015 plus the framebuffer's 8-bit rounding
PICTURE_CHANGE = 3
# The Display's picture area, mm (its EDID's): the box's share of it is a 221 x 125 mm picture, 10" 16:9.
DISPLAY_MM = (369, 277)
LIGHTING_FILE = pathlib.Path("/tmp/pw-picture-e2e-lighting.json")   # the binding's lighting, for the Player
LUT_FILE = pathlib.Path("/tmp/pw-picture-e2e-lut.bin")              # the fixture LUT's bytes
LUT_RELEASE = pathlib.Path("/tmp/pw-picture-e2e-lut-release")       # present: the Player holds them
LUT_PROFILE_SHA256, LUT_RECIPE = "ef" * 32, "lut33-perceptual-relcol-bpc-1"
PLAYER_ENV = {"XDG_RUNTIME_DIR": str(ROOT), "WAYLAND_DISPLAY": SOCKET, "LIBGL_ALWAYS_SOFTWARE": "1",
              "PYOPENGL_PLATFORM": "egl", "HOME": "/tmp/pw-picture-e2e-home"}


def upgrade_lighting():
    """The upgrade's lighting (decision 0028): one wash of the room light at the matched lux, paper
    that matches the TV."""
    import math

    from contracts.frame_lighting import AT_FULL, FrameLighting, Paper, WashLight
    from contracts.node_picture import UPGRADE_DISPLAY_LIGHT

    return FrameLighting(mat_depth_mm=0, paper=Paper(whiteness=.9, ink_density=-math.log10(UPGRADE_DISPLAY_LIGHT.black)),
                         lights=(WashLight(light="room", lux=UPGRADE_DISPLAY_LIGHT.matched_lux,
                                           reference=AT_FULL),))


def placement():
    """The Frame's placement on the Output (contracts/frame_fit.py `place_box`)."""
    from contracts.frame_fit import place_box
    from contracts.models import PositionBox

    return place_box(PositionBox(**BOX).box(), (WIDTH_PX, HEIGHT_PX))


def room():
    from contracts.room_light import (
        FixedEntry,
        KelvinColour,
        RoomLightsDocument,
    )

    return RoomLightsDocument(change=1, lights={"room": FixedEntry(colour=KelvinColour(kelvin=6500))})


def grey_png(path: pathlib.Path) -> bytes:
    """A PHOTO_SIZE opaque grey RGBA PNG (stdlib zlib)."""
    width, height = PHOTO_SIZE
    rows = b"".join(b"\x00" + bytes((GREY, GREY, GREY, 255)) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    payload = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    path.write_bytes(payload)
    return payload


# --- the Player (child process, unprivileged) ---------------------------------------------------

def player() -> None:
    """Show the grey Frame on its rectangle for ever on the new path (module docstring), presenting
    and finishing every 50 ms on real GLib."""
    import hashlib

    from harness_device import MemfdDevice, context

    gl = context()          # before any player import: PyOpenGL resolves on it

    from gi.repository import GLib

    from contracts.frame_lighting import FrameLighting
    from contracts.lut import LutRef
    from contracts.models import (
        Calibration,
        Layer,
        OutputBinding,
        PositionBox,
        Variant,
    )
    from contracts.photo_crop import CENTRED
    from player.decode import StillDecode
    from player.fit_pass import FitPass
    from player.luts import PlayerLuts
    from player.pattern_pass import PatternPass
    from player.plane_renderer import PlaneRenderer
    from player.plane_screen import PlaneScreens
    from player.rendering import LocalLayer, OutputComposition

    def no_video(_path, _on_frame):
        raise RuntimeError("no video in the harness")

    payload = PHOTO.read_bytes()
    variant = Variant(asset_id="asset-" + hashlib.sha256(payload).hexdigest()[:16], recipe_id="e" * 64, sha256=hashlib.sha256(payload).hexdigest(), size=len(payload),
                      media_type="image/png", width=PHOTO_SIZE[0], height=PHOTO_SIZE[1])
    binding = OutputBinding(output_id=OUTPUT, frame_id="frame", generation=1, display_mm=DISPLAY_MM,
                            lighting=upgrade_lighting(),
                            calibration=Calibration(fit=PositionBox(**BOX)))
    layer = LocalLayer(Layer(assignment_id="grey", run_id="run", output_id=OUTPUT, frame_id="frame",
                             binding_generation=1, start=0, end=10**9, media_origin=0, variant=variant,
                             crop=CENTRED),
                       PHOTO, 0.0, 1.0)
    luts = PlayerLuts()
    screens = PlaneScreens()
    renderer = PlaneRenderer(screens, MemfdDevice(gl), FitPass(gl), PatternPass(gl), luts,
                             stills=StillDecode, videos=no_video)
    renderer.set_room_lights(room())
    main_context = GLib.MainContext.default()
    last = None
    while True:
        screens.dispatch()
        lighting = (FrameLighting.model_validate_json(LIGHTING_FILE.read_text()) if LIGHTING_FILE.exists()
                    else upgrade_lighting())
        shown = OutputComposition(binding.model_copy(update={"lighting": lighting}), (layer,))
        if LUT_RELEASE.exists() and luts.wanted():
            luts.hold(LutRef(LUT_PROFILE_SHA256, LUT_RECIPE), LUT_FILE.read_bytes())
            print("PLAYER lut held", flush=True)
        renderer.prepare(layer, shown.binding)
        result = renderer.present(shown)
        renderer.finish()
        if (result.status, result.code) != last:
            print(f"PLAYER {result.status} {result.code}", flush=True)
            last = (result.status, result.code)
        end = time.monotonic() + 0.05
        while time.monotonic() < end:
            while main_context.iteration(False):
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


def lights_of(operations, lighting=None):
    """The Frame's lights (`picture_lights`) as the Player draws them with `operations`: at the
    box's share of the Display's size (`frame_size_mm`, the screen unturned)."""
    from contracts.models import PositionBox, frame_size_mm
    from player.geometry import picture_lights

    return picture_lights(lighting or upgrade_lighting(), room(), frame_size_mm(PositionBox(**BOX), DISPLAY_MM, 0),
                          operations.light)


def compare(operations, *, reference=None) -> tuple[int, int]:
    """Every sampled pixel clear of the placement's edges equals the reference: inside, the grey
    through `final_pixel` with `operations` under the upgrade's light (or `reference(u, v)` at the
    picture point, an RGB, when given); outside, exactly black. The (inside, outside) counts;
    AssertionError at the first that differs."""
    from player.geometry import final_pixel, placement_uv

    width, height, data = capture()
    assert (width, height) == (WIDTH_PX, HEIGHT_PX), ("capture_size", width, height)
    placed = placement()
    flat = (final_pixel(grey_linear(), operations, inside=True, lights=lights_of(operations), x=.5, y=.5)[:3]
            if reference is None else None)
    counts = [0, 0]
    for j in range(4, height, 9):
        for i in range(4, width, 9):
            if i >= width * 3 // 4 and j >= height * 3 // 4:
                continue                # the health client's tint
            x, y = (i + .5) / width, (j + .5) / height
            near = {placement_uv(x + dx / width, y + dy / height, placed, (width, height)) is not None
                    for dx in (-3, 0, 3) for dy in (-3, 0, 3)}
            if len(near) != 1:
                continue
            offset = (j * width + i) * 4
            blue, green, red = data[offset:offset + 3]
            if near.pop():
                expected_inside = (flat if reference is None
                                   else reference(*placement_uv(x, y, placed, (width, height))))
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
    UPGRADE_DISPLAY_LIGHT,
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
    # The GL renderer on llvmpipe: pixman offers no zwp_linux_dmabuf_v1 (module docstring).
    env = {**os.environ, "XDG_RUNTIME_DIR": str(ROOT), "PHOTO_WALL_CONTROLLER_UID": "0",
           "LIBGL_ALWAYS_SOFTWARE": "1"}
    weston_log = open("/tmp/pw-weston.log", "w")
    weston = subprocess.Popen(
        ["weston", "--backend=headless-backend.so", "--renderer=gl",
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

        stand_in = neutral_operations(OUTPUT, 1, UPGRADE_DISPLAY_LIGHT)
        inside, outside = await_compare(stand_in, drain)
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
        wanted = SoftwarePicture(contrast_percent=25, colour_temperature_kelvin=6500, gamma_tenths=22)
        # The document as it crosses the wire for HDMI-A-1, then retargeted at the headless Output.
        document = replace(decode_picture_document(encode_picture_document(
            PictureDocument("HDMI-A-1", PICTURE_CHANGE, {}, wanted, light=UPGRADE_DISPLAY_LIGHT))), output_id=OUTPUT)
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
        operations = operations_for(OUTPUT, PICTURE_CHANGE, wanted, UPGRADE_DISPLAY_LIGHT, None)
        assert operations.contrast_slope < 1, operations
        inside, outside = await_compare(operations, drain)
        print(f"ADJUSTED inside={inside} outside={outside}", flush=True)
        assert inside > 300 and outside > 1000, (inside, outside)
        print("PASS a_picture_document_changes_the_pixels_and_comes_back_presented", flush=True)

        def send(change: int, software, light=UPGRADE_DISPLAY_LIGHT, test_pattern=None) -> None:
            """The document as it crosses the wire for HDMI-A-1, retargeted at the headless Output."""
            controller.document(OUTPUT, replace(decode_picture_document(encode_picture_document(
                PictureDocument("HDMI-A-1", change, {}, software, test_pattern=test_pattern, light=light))),
                output_id=OUTPUT))

        def presented(change: int, seconds: float = 15.0):
            end = time.monotonic() + seconds
            while True:
                report = reports.latest.get(OUTPUT)
                if report is not None and report.presented_change == change:
                    return report
                assert time.monotonic() < end, ("presented_change", change, report)
                drain()
                time.sleep(0.01)

        room_light_cases(send, presented, drain, SoftwarePicture)
        pattern_case(send, presented, drain, reports, SoftwarePicture, operations_for)
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


# --- room light (decision 0028): the Display's light and the Frame's lighting, end to end -----------

def today_pixel(linear, operations) -> tuple[float, float, float]:
    """The 1c final pass at neutral sliders, written out: the oracle for "the upgrade is today"."""
    def encode(value: float) -> float:
        value = min(1.0, max(0.0, value))
        return 12.92 * value if value <= .0031308 else 1.055 * value ** (1 / 2.4) - .055
    pixel = []
    for value, multiplier in zip(linear, operations.rgb_multipliers, strict=True):
        value = min(1.0, max(0.0, value * multiplier))
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


def write_lighting(lighting) -> None:
    LIGHTING_FILE.write_text(lighting.model_dump_json())
    LIGHTING_FILE.chmod(0o644)


def room_light_cases(send, presented, drain, SoftwarePicture) -> None:
    from contracts.frame_lighting import AT_FULL, FarLight, WashLight
    from contracts.lut import LutRef
    from contracts.node_frame import PictureOperations
    from contracts.node_picture import UPGRADE_DISPLAY_LIGHT, DisplayLight
    from player.geometry import final_pixel, irradiance, placement_uv

    neutral = SoftwarePicture(contrast_percent=100, colour_temperature_kelvin=6500, gamma_tenths=22)
    matched, black = UPGRADE_DISPLAY_LIGHT.matched_lux, UPGRADE_DISPLAY_LIGHT.black

    # 2. neutral_is_todays_picture
    LIGHTING_FILE.unlink(missing_ok=True)
    send(4, neutral)
    presented(4)
    operations = PictureOperations(OUTPUT, 4, (1.0, 1.0, 1.0), 1.0, 1.0, DisplayLight(matched, black))
    today = today_pixel(grey_linear(), operations)
    inside, outside = await_compare(operations, drain, reference=lambda _x, _y: today)
    print(f"NEUTRAL_TODAY inside={inside} outside={outside}", flush=True)
    assert inside > 300 and outside > 1000, (inside, outside)
    print("PASS neutral_is_todays_picture", flush=True)

    # 3. tracer_matched_light_and_one_far_light
    lighting = upgrade_lighting().model_copy(update={"mat_depth_mm": 2.0, "lights": (
        WashLight(light="room", lux=500, reference=AT_FULL),
        FarLight(light="room", lux=500, reference=AT_FULL, from_degrees=0, steep_degrees=45,
                 softness_degrees=0))})
    write_lighting(lighting)
    light = DisplayLight(matched_lux=800.0, black=black)
    send(5, neutral, light)
    report = presented(5)
    expected = PictureOperations(OUTPUT, 5, (1.0, 1.0, 1.0), 1.0, 1.0, DisplayLight(800.0, black))
    lights = lights_of(expected, lighting)

    def lit(u: float, v: float) -> tuple[float, float, float]:
        return final_pixel(grey_linear(), expected, inside=True, lights=lights, x=u, y=v)[:3]

    inside, outside = await_compare(expected, drain, reference=lit)
    # The point 1 mm below the picture's top edge (mat space), mid-width: in the far light's shadow.
    u, v = .5, 1 / lights.size_mm[1]
    assert abs(irradiance(lights, u, v)[0] - 500) < 1e-6, irradiance(lights, u, v)
    placed = placement()
    width, height, data = capture()
    i, j = int(placed.x + u * placed.width), int(placed.y + v * placed.height)
    offset = (j * width + i) * 4
    blue, green, red = data[offset:offset + 3]
    want = lit(*placement_uv((i + .5) / width, (j + .5) / height, placed, (width, height)))
    actual = (red / 255, green / 255, blue / 255)
    print(f"TRACER inside={inside} outside={outside} report={report} one_mm={actual} want={want}", flush=True)
    assert max(abs(a - e) for a, e in zip(actual, want, strict=True)) <= TOLERANCE, ("one_mm", actual, want)
    assert want[0] < lit(.5, .5)[0] - .1, want                                       # shaded
    assert inside > 300 and outside > 1000, (inside, outside)
    print("PASS tracer_matched_light_and_one_far_light", flush=True)

    # 4. the_frames_paper_profile_joins_when_held
    table = lut_fixture()
    LUT_FILE.write_bytes(table)
    LUT_FILE.chmod(0o644)
    LUT_RELEASE.unlink(missing_ok=True)
    ref = LutRef(LUT_PROFILE_SHA256, LUT_RECIPE)
    upgrade = upgrade_lighting()
    profiled = upgrade.model_copy(update={"paper": upgrade.paper.model_copy(update={"profile": ref})})
    write_lighting(profiled)
    send(6, neutral)
    presented(6)                            # nothing waits on the Frame's LUT
    operations = PictureOperations(OUTPUT, 6, (1.0, 1.0, 1.0), 1.0, 1.0, DisplayLight(matched, black))
    inside, _ = await_compare(operations, drain)
    LUT_RELEASE.touch()
    LUT_RELEASE.chmod(0o644)
    lut_pixel = final_pixel(grey_linear(), operations, inside=True, lights=lights_of(operations, profiled),
                            x=.5, y=.5, lut=table)[:3]
    inside_after, _ = await_compare(operations, drain, reference=lambda _x, _y: lut_pixel)
    print(f"LUT before={inside} after={inside_after} pixel={lut_pixel}", flush=True)
    assert lut_pixel[2] < lut_pixel[0] - .1, lut_pixel          # the warm table is drawn
    print("PASS the_frames_paper_profile_joins_when_held", flush=True)


def pattern_case(send, presented, drain, reports, SoftwarePicture, operations_for) -> None:
    """5. a_test_pattern_shows_inside_the_placement (module docstring)."""
    from contracts.node_picture import UPGRADE_DISPLAY_LIGHT, TestPatternAsk
    from contracts.tuning import TestPattern
    from player.geometry import display_steps

    tuned = SoftwarePicture(contrast_percent=80, colour_temperature_kelvin=5000, gamma_tenths=24)
    ask = TestPatternAsk(TestPattern.WHITE, 1)
    send(7, tuned, test_pattern=ask)
    report = presented(7)
    end = time.monotonic() + 15
    while report.test_pattern_shown != ask:
        assert time.monotonic() < end, ("test_pattern_shown", report)
        drain()
        time.sleep(0.01)
        report = reports.latest[OUTPUT]
    operations = operations_for(OUTPUT, 7, tuned, UPGRADE_DISPLAY_LIGHT, TestPattern.WHITE)
    white = display_steps((1.0, 1.0, 1.0), operations)
    inside, outside = await_compare(operations, drain, reference=lambda _u, _v: white)
    print(f"PATTERN inside={inside} outside={outside} white={white} report={report}", flush=True)
    assert min(white) < max(white) - .05, white                 # the software group is drawn
    assert inside > 300 and outside > 1000, (inside, outside)
    print("PASS a_test_pattern_shows_inside_the_placement", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--player"]:
        player()
    else:
        main()
