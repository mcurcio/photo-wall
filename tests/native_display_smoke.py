"""Manual native compositor fixture, run only inside a disposable Linux container.

The display under test is the built one: photo-wall-node-display and photo-wall-frame-client
installed from the local repo (decision 0019; scripts/run_display_harness.py builds that image,
FROM the pinned Debian build container, and runs this file). Mount tests at /smoke and
appliance/display_host/native at /current-native, both read-only: only the private protocol's XML
and the client library's header are read there, to build the test probe against the installed
library. This executes a real headless compositor, not a recorded callback simulator. It uses
fixture controller authority and does not qualify systemd, GTK or HDMI. Output pixels are read
back through Weston's own weston_capture_v1 (enabled by --debug).
"""

import json
import mmap
import os
import pathlib
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

DISPLAY = "/usr/lib/photo-wall/node-display"
CLIENT = "/usr/lib/photo-wall/frame-client"
PROTOCOL_XML = "/current-native/photo-wall-frame-v1.xml"
# The private client the shell spawns becomes B5's overlay client plus a health layer per Output.
SPAWNED = DISPLAY + "/diagnostic-client"
shutil.copyfile("/smoke/display_harness_health_client.py", SPAWNED)
os.chmod(SPAWNED, 0o755)
HEALTH_MODE = "/tmp/pw-health-client-mode"  # display_harness_health_client.py MODE_FILE
# The production client's health drawing (overlay/render.py card_rect), as installed, and its
# fake judge.
sys.path.append(DISPLAY)
from display_harness_judge_feeder import PATH as JUDGE_SOCKET  # noqa: E402
from display_harness_judge_feeder import JudgeFeeder  # noqa: E402
from overlay.instruction import INSTRUCTION_STALE_MS  # noqa: E402
from overlay.render import card_rect  # noqa: E402

HEALTH_MARKER = "/tmp/pw-health-client-marker"  # display_harness_health_client.py MARKER_FILE
# The probe's protocol code, generated as the library's own build does.
xml = "/usr/share/wayland-protocols/stable/xdg-shell/xdg-shell.xml"
subprocess.run(["wayland-scanner", "client-header", xml, "/tmp/xdg-shell-client.h"], check=True)
subprocess.run(["wayland-scanner", "private-code", xml, "/tmp/xdg-shell.c"], check=True)
subprocess.run(["wayland-scanner", "client-header", PROTOCOL_XML,
                "/tmp/photo-wall-frame-client.h"], check=True)
subprocess.run(["wayland-scanner", "private-code", PROTOCOL_XML,
                "/tmp/photo-wall-frame-protocol.c"], check=True)
subprocess.run(
    [
        "cc",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-I/tmp",
        "-I/current-native",
        "/smoke/native_display_probe.c",
        "/tmp/xdg-shell.c",
        "/tmp/photo-wall-frame-protocol.c",
        "-L" + CLIENT,
        "-Wl,-rpath," + CLIENT,
        "-lphoto-wall-frame-client",
        "-lwayland-client",
        "-ljansson",
        "-o",
        "/tmp/probe",
    ],
    check=True,
)
# Python bindings, generated here as the overlay client's build will: the core protocol (the
# generated interfaces import it relatively), Weston's capture and debug protocols and the
# private one.
GENERATED = pathlib.Path("/tmp/pw-protocols")
subprocess.run(
    [
        sys.executable,
        "-m",
        "pywayland.scanner",
        "-i",
        "/usr/share/wayland/wayland.xml",
        "/usr/share/libweston-14/protocols/weston-output-capture.xml",
        "/usr/share/libweston-14/protocols/weston-debug.xml",
        PROTOCOL_XML,
        "-o",
        str(GENERATED / "pw_protocols"),
    ],
    check=True,
)
(GENERATED / "pw_protocols" / "__init__.py").touch()
sys.path.insert(0, str(GENERATED))
import pywayland  # noqa: E402
from pw_protocols.photo_wall_frame_v1 import PwDiagnosticManagerV1  # noqa: E402
from pw_protocols.wayland import WlOutput, WlShm  # noqa: E402
from pw_protocols.weston_debug import WestonDebugV1  # noqa: E402
from pw_protocols.weston_output_capture import WestonCaptureV1  # noqa: E402
from pywayland.client import Display  # noqa: E402

print("PYWAYLAND", pywayland.__version__, PwDiagnosticManagerV1.name,
      PwDiagnosticManagerV1.version, flush=True)
SLATE = (10, 15, 23)  # overlay/render.py slate background (0.04, 0.06, 0.09), opaque
PROBE_APP = (0x22, 0x33, 0x44)  # native_display_probe.c fill 0xff223344


def over(top, a, under):
    """pixman OVER of one premultiplied 8-bit source (`top` rgb, alpha `a`) on an opaque pixel."""
    def channel(colour, below):
        t = below * (255 - a) + 0x80
        return min(255, colour + ((t + (t >> 8)) >> 8))

    return tuple(channel(c, u) for c, u in zip(top, under))


def cairo_over(rgb, alpha, under):
    """A cairo ARGB32 paint of `rgb` at `alpha` (premultiplied, 16-bit colour, then the top byte)
    over an opaque pixel, with pixman's OVER rounding."""
    top = tuple(int(c * alpha * 65535 + 0.5) >> 8 for c in rgb)
    return over(top, int(alpha * 65535 + 0.5) >> 8, under)


def testing_slate(alpha=0.96):
    """The slate a starting candidate shows through, over the probe app."""
    return cairo_over((0.04, 0.06, 0.09), alpha, PROBE_APP)


TESTING_SLATE = testing_slate()  # (10, 16, 25); opaque would be SLATE, 2 off in blue
assert abs(TESTING_SLATE[2] - SLATE[2]) >= 2, TESTING_SLATE
# display_harness_health_client.py's tint (premultiplied 0x80 magenta, alpha 0x80) covers the
# bottom-right quarter of its health surface; HEALTH is a pixel inside it.
HEALTH = (560, 420)


def health_over(under):
    return over((0x80, 0x00, 0x80), 0x80, under)


# shell.c fallback_sync's tint RGBA (0.55, 0.35, 0.0, 0.5) over the app: Weston's solid colour is
# premultiplied and 16-bit (truncated), pixman takes the top byte.
FALLBACK = over(tuple(int(c * 0.5 * 0xFFFF) >> 8 for c in (0.55, 0.35, 0.0)),
                int(0.5 * 0xFFFF) >> 8, PROBE_APP)  # (87, 70, 34); unpremultiplied: (157, 115, 34)
# The production health page (overlay/render.py render_health): black at 0.45 over the app, and
# its opaque card, slate-coloured.
HEALTH_TINT = cairo_over((0.0, 0.0, 0.0), 0.45, PROBE_APP)
CARD_X, CARD_Y, _, _ = card_rect(640, 480)
CARD_PIXEL = (CARD_X + 2, CARD_Y + 2)
CARD_LINES = ("Photos paused - the player stopped responding", "app_unresponsive")
# DRM fourcc -> wl_shm format: only the two 32-bit formats differ (wl_shm keeps the rest).
SHM_FORMAT = {0x34325258: 1, 0x34325241: 0}  # XRGB8888, ARGB8888
root = pathlib.Path("/tmp/pw-display-smoke")
root.mkdir(mode=0o711)
root.chmod(0o711)
env = {
    **os.environ,
    "XDG_RUNTIME_DIR": str(root),
    "PHOTO_WALL_CONTROLLER_UID": "0",
    "WAYLAND_DISPLAY": "wayland-0",
    "PHOTO_WALL_HEALTH_SOCKET": JUDGE_SOCKET,  # inherited by the private client
}
judge = JudgeFeeder()
log = open("/tmp/pw-weston.log", "w")
p = subprocess.Popen(
    [
        "weston",
        "--backend=headless-backend.so",
        "--renderer=pixman",
        "--shell=/usr/lib/photo-wall/node-display/photo-wall-shell.so",
        "--width=640",
        "--height=480",
        "--idle-time=0",
        "--socket=wayland-0",
        "--no-config",
        "--debug",
    ],
    env=env,
    stdout=log,
    stderr=log,
)
app = None
events = []


def user():
    os.setgid(10004)
    os.setuid(10004)


def read_until(predicate, limit=8):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        s.settimeout(end - time.monotonic())
        e = json.loads(s.recv(16384))
        events.append(e)
        if predicate(e):
            return e
    raise AssertionError("event_timeout")


def drain():
    """Keep every event already queued (the shell drops a control peer whose queue fills)."""
    s.setblocking(False)
    try:
        while True:
            events.append(json.loads(s.recv(16384)))
    except BlockingIOError:
        pass
    finally:
        s.setblocking(True)


def capture_pixel(x, y):
    """(r, g, b) of output pixel (x, y), read from Weston's framebuffer by weston_capture_v1."""
    display = Display(str(root / "wayland-0"))
    display.connect()
    try:
        found = {}
        registry = display.get_registry()
        registry.dispatcher["global"] = lambda _r, name, iface, _v: found.setdefault(iface, name)
        display.roundtrip()
        out = registry.bind(found["wl_output"], WlOutput, 1)
        shm = registry.bind(found["wl_shm"], WlShm, 1)
        capture = registry.bind(found["weston_capture_v1"], WestonCaptureV1, 1)
        source = capture.create(out, WestonCaptureV1.source.framebuffer)
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
            fd = os.memfd_create("pw-capture")
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
                        blue, green, red = pixels[y * stride + x * 4 : y * stride + x * 4 + 3]
                    return red, green, blue
                buffer.destroy()
                pool.destroy()
            finally:
                os.close(fd)
            assert state.get("done") == "retry", ("capture", state)
        raise AssertionError(("capture_retries", state))
    finally:
        display.disconnect()


def health_view():
    """Weston's own record of the health layer's view: its block of the one-shot `scene-graph`
    debug dump (weston_debug_v1, enabled by --debug), written into a memfd so a long dump never
    blocks the compositor. Exactly one view has the role."""
    display = Display(str(root / "wayland-0"))
    display.connect()
    fd = os.memfd_create("pw-scene-graph")
    try:
        found = {}
        registry = display.get_registry()
        registry.dispatcher["global"] = lambda _r, name, iface, _v: found.setdefault(iface, name)
        display.roundtrip()
        debug = registry.bind(found["weston_debug_v1"], WestonDebugV1, 1)
        state = {}
        stream = debug.subscribe("scene-graph", fd)
        stream.dispatcher["complete"] = lambda _s: state.update(done="complete")
        stream.dispatcher["failure"] = lambda _s, message: state.update(done=message)
        end = time.monotonic() + 3
        while "done" not in state:
            assert time.monotonic() < end, "scene_graph_timeout"
            display.roundtrip()
        assert state["done"] == "complete", state
        dump = os.pread(fd, 1 << 22, 0).decode(errors="replace")
    finally:
        os.close(fd)
        display.disconnect()
    views = [block for block in dump.split("\tView ")[1:] if "role photo-wall-health," in block]
    assert len(views) == 1, dump
    return views[0]


def assert_health_view(*lines):
    """The health view's scene-graph block holds every one of `lines`."""
    view = health_view()
    assert all(line in view for line in lines), (lines, view)
    return view


# The production client's tint-off health view: a transparent single-pixel buffer, scaled over
# the whole 640 x 480 Output (never a small shm buffer: Weston 14 DRM's cursor-plane path).
HEALTH_CLEAR_VIEW = ("position: (0, 0) -> (640, 480)", "solid-colour buffer",
                     "[R 0.000000, G 0.000000, B 0.000000, A 0.000000]")
HEALTH_TINT_VIEW = ("position: (0, 0) -> (640, 480)", "SHM buffer", "width: 640, height: 480")


def assert_pixel(x, y, expected, tolerance=2):
    actual = capture_pixel(x, y)
    assert all(abs(a - e) <= tolerance for a, e in zip(actual, expected)), (x, y, actual, expected)
    return actual


def await_pixel(x, y, expected, limit, tolerance=2, drained=False):
    """The first capture of (x, y) within `limit` s that matches; else the last one fails.
    `drained` keeps the control socket drained while it waits (long waits)."""
    end = time.monotonic() + limit
    while True:
        actual = capture_pixel(x, y)
        if all(abs(a - e) <= tolerance for a, e in zip(actual, expected)):
            return actual
        assert time.monotonic() < end, (x, y, actual, expected)
        if drained:
            drain()
        time.sleep(0.05)


def hold_pixel(x, y, expected, duration, tolerance=2):
    """Every capture of (x, y) for `duration` s matches."""
    end = time.monotonic() + duration
    while True:
        actual = assert_pixel(x, y, expected, tolerance)
        if time.monotonic() >= end:
            return actual
        drain()
        time.sleep(0.05)


def await_marker(limit):
    """The private client's one-shot marker (removed once read), within `limit` s."""
    marker = pathlib.Path(HEALTH_MARKER)
    end = time.monotonic() + limit
    while not marker.exists():
        assert time.monotonic() < end, "health client marker"
        drain()
        time.sleep(0.05)
    text = marker.read_text()
    marker.unlink()
    return text


def kill_private_client():
    """SIGKILL the shell's private client (found by its cmdline); the kill's monotonic time once
    it has exited. The shell respawns it 2 s after it is gone."""
    children = pathlib.Path(f"/proc/{p.pid}/task/{p.pid}/children").read_text().split()
    diag = next(
        int(pid)
        for pid in children
        if b"diagnostic-client" in pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
    )
    os.kill(diag, signal.SIGKILL)
    killed = time.monotonic()
    while not exited(diag):
        time.sleep(0.01)
    return killed


def await_log(text, limit):
    end = time.monotonic() + limit
    while text not in pathlib.Path("/tmp/pw-weston.log").read_text(errors="replace"):
        assert time.monotonic() < end, ("weston_log", text)
        time.sleep(0.1)


# A foreign client binding the private manager through the generated bindings. libwayland logs
# the protocol error it receives; the process exits without tearing down the dead connection.
FOREIGN_BIND = f"""
import os
import sys
sys.path.insert(0, {str(GENERATED)!r})
from pw_protocols.photo_wall_frame_v1 import PwDiagnosticManagerV1
from pywayland.client import Display
display = Display({str(root / "wayland-0")!r})
display.connect()
found = {{}}
registry = display.get_registry()
registry.dispatcher["global"] = lambda _r, name, iface, _v: found.setdefault(iface, name)
display.roundtrip()
registry.bind(found["pw_diagnostic_manager_v1"], PwDiagnosticManagerV1, 2)
display.roundtrip()
display.roundtrip()
sys.stderr.flush()
os._exit(0)
"""


def exited(pid):
    try:
        return pathlib.Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2][:1] == "Z"
    except FileNotFoundError:
        return True


def foreign_bind_refused():
    foreign = subprocess.run(
        ["/usr/bin/python3", "-B", "-c", FOREIGN_BIND],
        env=env,
        preexec_fn=user,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "error 3: private_diagnostic_role" in foreign.stderr, (foreign.returncode, foreign.stderr)


def request(op, **kw):
    ident = str(uuid.uuid4())
    s.send(
        json.dumps(
            {
                "op": op,
                "request_id": ident,
                "output_id": output["output_id"],
                "output": output,
                **kw,
            }
        ).encode()
    )
    return read_until(lambda e: e["event"] == "response" and e["request_id"] == ident)


try:
    for _ in range(100):
        if (root / "control.sock").exists():
            break
        if p.poll() is not None:
            raise AssertionError("weston_failed")
        time.sleep(0.05)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    s.connect(str(root / "control.sock"))
    read_until(lambda e: e["event"] == "hello")
    output = read_until(lambda e: e["event"] == "output")["output"]
    read_until(lambda e: e["event"] == "diagnostic_presented")
    print("PASS app_absent_private_diagnostic_presentation", flush=True)
    print("PASS slate_pixel_captured", assert_pixel(40, 40, SLATE), flush=True)
    print("PASS health_layer_above_slate", await_pixel(*HEALTH, health_over(SLATE), 3), flush=True)
    (root / "wayland-0").chmod(0o666)
    neg = subprocess.run(
        ["/tmp/probe", "private"],
        env=env,
        preexec_fn=user,
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert neg.returncode == 0, (neg.returncode, neg.stderr)
    print("PASS unprivileged_private_role_rejected", flush=True)
    foreign_bind_refused()
    print("PASS foreign_generated_binding_gets_private_diagnostic_role", flush=True)
    bad = subprocess.run(
        [
            "/usr/bin/python3",
            "-c",
            f"import socket;s=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET);s.connect({str(root / 'control.sock')!r})",
        ],
        preexec_fn=user,
        capture_output=True,
        text=True,
    )
    assert bad.returncode != 0 and "PermissionError" in bad.stderr
    print("PASS unprivileged_controller_socket_rejected", flush=True)
    app = subprocess.Popen(["/tmp/probe"], env=env, preexec_fn=user)
    ticks = int(pathlib.Path(f"/proc/{app.pid}/stat").read_text().rpartition(") ")[2].split()[19])
    identity = {"frame_id": "native-frame",
        "output": output,
        "process": {"pid": app.pid, "start_ticks": ticks, "invocation_id": str(uuid.uuid4())},
        "app_epoch": 1,
        "binding_generation": 1,
        "config_revision": 1,
    }
    grant = str(uuid.uuid4())
    candidate_at = len(events)
    assert request("candidate", identity=identity, grant_id=grant, uid=10004)["accepted"]
    first = read_until(lambda e: e["event"] == "presented")
    assert first["frame_tag"] == "synthetic-probe" and first["identity"] == identity
    print("PASS candidate_exact_identity_compositor_feedback", flush=True)
    # The candidate's testing slate (alpha 0.96) is presented over the candidate's own frames.
    if not any(e["event"] == "diagnostic_presented" for e in events[candidate_at:]):
        read_until(lambda e: e["event"] == "diagnostic_presented")
    print("PASS testing_slate_pixel_captured",
          assert_pixel(40, 40, TESTING_SLATE, tolerance=1), flush=True)
    assert not request(
        "handoff", grant_id=grant, handoff_id=str(uuid.uuid4()), buffer_id="weston-0"
    )["accepted"]
    print("PASS stale_handoff_buffer_refused", flush=True)
    handoff = str(uuid.uuid4())
    # A newer presentation can supersede first while control replies are read.
    latest = next(e for e in reversed(events) if e["event"] == "presented")
    read_until(lambda e: e["event"] == "presented" and e["buffer_id"] != latest["buffer_id"])
    result = request("handoff", grant_id=grant, handoff_id=handoff, buffer_id=latest["buffer_id"])
    print("PASS exact_recent_receipt_survives_newer_frame", flush=True)
    assert result["accepted"], result
    release = read_until(lambda e: e["event"] == "handoff_presented")
    assert release["handoff_id"] == handoff
    read_until(lambda e: e["event"] == "presented" and e["frame_tag"] == "authored-probe")
    print("PASS separate_handoff_ack_then_admitted_grant", flush=True)
    print("PASS handed_off_app_pixel_captured", assert_pixel(320, 240, PROBE_APP), flush=True)
    print("PASS health_layer_above_live_app_after_handoff",
          assert_pixel(*HEALTH, health_over(PROBE_APP)), flush=True)
    revision = {**identity, "config_revision": 2}
    revision_grant = str(uuid.uuid4())
    assert request(
        "revision",
        identity=revision,
        grant_id=revision_grant,
        decision_id=str(uuid.uuid4()),
        ttl_ms=3000,
    )["accepted"]
    adopted = read_until(lambda e: e["event"] == "revision_adopted")
    assert adopted["identity"] == revision and adopted["grant_id"] == revision_grant
    assert not request("handoff", grant_id=revision_grant, handoff_id=str(uuid.uuid4()),
                       buffer_id=latest["buffer_id"])["accepted"]
    print("PASS old_revision_receipt_refused_after_promotion", flush=True)
    read_until(lambda e: e["event"] == "presented" and e["identity"] == revision)
    assert not any(
        e["event"] == "diagnostic_presented" for e in events[events.index(release) + 1 :]
    )
    print("PASS same_process_revision_atomic_promotion_without_diagnostic", flush=True)
    assert request(
        "revision",
        identity={**revision, "config_revision": 3},
        grant_id=str(uuid.uuid4()),
        decision_id=str(uuid.uuid4()),
        ttl_ms=200,
    )["accepted"]
    read_until(lambda e: e["event"] == "revision_expired")
    read_until(lambda e: e["event"] == "presented" and e["identity"] == revision)
    assert not any(e["event"] == "invalidated" for e in events[events.index(adopted) + 1 :])
    print("PASS pending_revision_expiry_preserves_active_surface", flush=True)

    def boot_ms():
        return time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1_000_000

    trial = {
        "trial_id": str(uuid.uuid4()),
        "generation": 1,
        "sequence": 1,
        "baseline": revision,
        "calibration_json": "{}",
        "candidate_sha256": "a" * 64,
        "expires_boottime_ms": boot_ms() + 1500,
        "hard_expires_boottime_ms": boot_ms() + 4000,
    }
    assert request("trial", candidate=json.dumps(trial))["accepted"]
    wrong = read_until(
        lambda e: e["event"] == "presented" and e["frame_tag"] == "trial-" + "0" * 64
    )
    assert not any(e["event"] == "overlay_presented" for e in events[events.index(adopted) + 1 :])
    print("PASS wrong_trial_hash_cannot_publish_overlay", flush=True)
    trial.update(sequence=2, candidate_sha256="b" * 64)
    assert request("trial", candidate=json.dumps(trial))["accepted"]
    overlay = read_until(lambda e: e["event"] == "overlay_presented")
    assert overlay["frame_tag"] == "trial-" + "b" * 64
    assert any(
        e["event"] == "presented"
        and e["buffer_id"] == overlay["buffer_id"]
        and e["frame_tag"] == overlay["frame_tag"]
        for e in events
    )
    print("PASS same_commit_trial_overlay_has_real_private_presentation", flush=True)
    trial.update(sequence=3, candidate_sha256="c" * 64)
    assert request("trial", candidate=json.dumps(trial))["accepted"]
    mismatched_commit = read_until(
        lambda e: e["event"] == "presented" and e["frame_tag"] == "trial-" + "c" * 64
    )
    assert not any(
        e["event"] == "overlay_presented" and e["frame_tag"] == "trial-" + "c" * 64 for e in events
    )
    print("PASS different_commit_primitives_cannot_publish_overlay", flush=True)
    time.sleep(1.6)
    delayed = {**trial, "expires_boottime_ms": trial["hard_expires_boottime_ms"]}
    assert not request("trial", candidate=json.dumps(delayed))["accepted"]
    read_until(lambda e: e["event"] == "presented" and e["frame_tag"] == "authored-probe")
    print("PASS expired_trial_tombstone_refuses_delayed_candidate", flush=True)
    # Handed off, app live, private client gone: the shell's own fallback tint covers the Output.
    pathlib.Path(HEALTH_MARKER).unlink(missing_ok=True)
    pathlib.Path(HEALTH_MODE).write_text("bare")
    killed = kill_private_client()
    print("PASS fallback_tint_without_private_client",
          await_pixel(320, 240, FALLBACK, 1.0 - (time.monotonic() - killed)), flush=True)
    foreign_bind_refused()
    assert time.monotonic() - killed < 1.8, "foreign bind missed the respawn window"
    print("PASS foreign_bind_refused_without_private_client_after_handoff", flush=True)
    # The respawned client binds the manager and takes its health layer but maps no buffer: the
    # bind alone shows nothing, so the tint stays (the rule keys on a mapped health surface).
    assert await_marker(6) == "bare"
    print("PASS fallback_tint_while_bound_client_maps_no_health_surface",
          hold_pixel(320, 240, FALLBACK, 0.5), flush=True)
    # Mapped (and presented), then a NULL buffer unmaps it: the tint comes back.
    pathlib.Path(HEALTH_MODE).write_text("unmap")
    kill_private_client()
    assert await_marker(6) == "presented"
    print("PASS fallback_tint_back_when_health_surface_unmapped",
          await_pixel(320, 240, FALLBACK, 3), flush=True)
    drain()
    kill_private_client()
    print("PASS fallback_tint_dropped_when_health_surface_mapped",
          await_pixel(320, 240, PROBE_APP, 5), flush=True)
    print("PASS respawned_health_layer_above_live_app",
          await_pixel(*HEALTH, health_over(PROBE_APP), 3), flush=True)
    # The production client (B11): its own health layer and judge link, against the fake judge.
    name = output["output_id"]
    pathlib.Path(HEALTH_MODE).write_text("production")
    opened = judge.opened
    killed = kill_private_client()
    await_pixel(320, 240, FALLBACK, 1.0 - (time.monotonic() - killed))
    # (1) Mapped (transparent) before any instruction: no magenta, no amber, while the judge
    # holds back; the shell respawns the client 2 s after it is gone.
    await_pixel(320, 240, PROBE_APP, 5.0 - (time.monotonic() - killed), tolerance=0, drained=True)
    hold_pixel(320, 240, PROBE_APP, 1.0, tolerance=0)
    assert_pixel(*HEALTH, PROBE_APP, tolerance=0)
    judge.await_client(opened + 1, 3, drain)
    assert not judge.reports and not judge.refused, (judge.reports, judge.refused)
    assert_health_view(*HEALTH_CLEAR_VIEW)
    print("PASS production_health_layer_mapped_before_instruction", flush=True)
    # (2) Tint on: the darkened Output and the card, above the still-live app, reported presented.
    judge.send(name, 7, True, CARD_LINES)
    judge.await_report(name, 7, 3, drain)
    tinted = await_pixel(320, 240, HEALTH_TINT, 1)
    assert_health_view(*HEALTH_TINT_VIEW)
    print("PASS health_tint_and_card_above_live_app", tinted,
          assert_pixel(*CARD_PIXEL, SLATE), flush=True)
    # (3) Tint off: a mapped transparent buffer, so the app shows exactly (no amber fallback).
    judge.send(name, 8, False, ("", ""))
    judge.await_report(name, 8, 3, drain)
    await_pixel(320, 240, PROBE_APP, 1, tolerance=0)
    hold_pixel(320, 240, PROBE_APP, 0.5, tolerance=0)
    assert_health_view(*HEALTH_CLEAR_VIEW)
    print("PASS health_tint_off_restores_app", assert_pixel(*CARD_PIXEL, PROBE_APP, tolerance=0),
          flush=True)
    # (4) A new connection may count from 1 again (a restarted judge): taken, drawn, reported.
    judge.drop()
    judge.await_client(opened + 2, 4, drain)
    judge.send(name, 1, True, CARD_LINES)
    judge.await_report(name, 1, 3, drain)
    print("PASS lower_serial_taken_after_reconnect", await_pixel(320, 240, HEALTH_TINT, 1),
          flush=True)
    # (5) Silence on an open link: the unavailable card after V, never reported.
    judge.send(name, 2, False, ("", ""))
    judge.await_report(name, 2, 3, drain)
    await_pixel(320, 240, PROBE_APP, 1, tolerance=0)
    silent_from, reported = time.monotonic(), len(judge.reports)
    stale = await_pixel(320, 240, HEALTH_TINT, INSTRUCTION_STALE_MS / 1000 + 3, drained=True)
    assert time.monotonic() - silent_from >= INSTRUCTION_STALE_MS / 1000 - 1.5, "stale too early"
    assert_pixel(*CARD_PIXEL, SLATE)
    time.sleep(0.5)
    assert len(judge.reports) == reported and judge.opened == opened + 2, judge.reports
    print("PASS stale_card_after_V", stale, flush=True)
    # Back to the magenta test client for the steps that follow.
    killed = kill_private_client()
    print("PASS respawned_health_layer_above_live_app",
          await_pixel(*HEALTH, health_over(PROBE_APP), 6.0 - (time.monotonic() - killed),
                      drained=True), flush=True)
    removal = str(uuid.uuid4())
    assert request("withdraw", identity=revision, decision_id=removal)["accepted"]
    removed = next((e for e in events if e["event"] == "role_removed" and e["decision_id"] == removal), None)
    if removed is None:
        removed = read_until(lambda e: e["event"] == "role_removed")
    assert removed["identity"] == revision and removed["decision_id"] == removal
    print("PASS exact_old_role_removed_after_withdrawal", flush=True)
    replacement = {**revision, "frame_id": "replacement-frame"}
    replacement_grant = str(uuid.uuid4())
    assert request("candidate", identity=replacement, grant_id=replacement_grant, uid=10004)["accepted"]
    new_frame = read_until(lambda e: e["event"] == "presented" and e["identity"] == replacement)
    assert new_frame["frame_tag"] == "synthetic-probe"
    assert not request("withdraw", identity=revision, decision_id=removal)["accepted"]
    print("PASS equal_counter_new_frame_fences_old_withdrawal", flush=True)
    app.terminate()
    app.wait(timeout=3)
    app = None
    read_until(lambda e: e["event"] == "invalidated")
    read_until(lambda e: e["event"] == "diagnostic_presented")
    print("PASS app_exit_private_diagnostic_restored", flush=True)
    print("PASS health_layer_survives_invalidate", assert_pixel(*HEALTH, health_over(SLATE)),
          flush=True)
    killed = kill_private_client()
    # While no private client is bound (the shell respawns it 2 s after it is gone), the
    # client-identity check alone refuses a foreign bind: no held resource masks it here.
    foreign_bind_refused()
    assert time.monotonic() - killed < 1.8, "foreign bind missed the respawn window"
    print("PASS foreign_bind_refused_without_private_client", flush=True)
    read_until(lambda e: e["event"] == "diagnostic_presented", limit=6)
    print("PASS diagnostic_client_crash_recovers", flush=True)
    # The next spawns misbehave once each (the client consumes the mode file at start).
    pathlib.Path(HEALTH_MODE).write_text("v2")
    kill_private_client()
    await_log("(since 2 < 3)", 6)
    print("PASS v2_private_client_cannot_get_health_layer", flush=True)
    pathlib.Path(HEALTH_MODE).write_text("duplicate")
    await_log("health_layer_exists", 6)
    print("PASS second_health_layer_per_output_refused", flush=True)
    print("PASS health_layer_restored_after_refusals",
          await_pixel(*HEALTH, health_over(SLATE), 5), flush=True)
    print(json.dumps({"events": events}, sort_keys=True), flush=True)
finally:
    print("RECENT_EVENTS", json.dumps(events[-12:]), flush=True)
    if app:
        app.terminate()
        app.wait(timeout=3)
    p.terminate()
    try:
        p.wait(timeout=3)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
    log.close()
    judge.close()
    print("WESTON_EXIT", p.returncode, flush=True)
    print(pathlib.Path("/tmp/pw-weston.log").read_text(), flush=True)
