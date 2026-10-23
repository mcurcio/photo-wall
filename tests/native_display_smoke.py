"""Manual native compositor fixture, run only inside a disposable Linux container.

The display under test is the built one: photo-wall-node-display and photo-wall-frame-client
installed from the local repo (decision 0019; scripts/run_display_harness.py builds that image,
FROM the pinned Debian build container, and runs this file). Mount tests at /smoke and
appliance/display_host/native at /current-native, both read-only: only the private protocol's XML
and the client library's header are read there, to build the test probe against the installed
library. This executes a real headless compositor, not a recorded callback simulator. It uses
fixture controller authority and does not qualify systemd, GTK or HDMI. Output pixels are read
back through Weston's own weston_capture_v1 (enabled by --debug).

Issue #123: the shell keeps a handed-off Output with its app only on hold evidence (a picture plane
the compositor shows, or Display's own drawing). The probe draws its ground alone, so the steps
that look at its pixels hold the Output by Display's drawing (the `picture` op's `drawing`, as a
test pattern does); the hold's own cases are below (`hold_cases`) and in native_screen_harness.py.
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
PROBE_STILL = "/tmp/pw-probe-still"  # native_display_probe.c: while it exists, the probe commits nothing
PROBE_UNTAGGED = "/tmp/pw-probe-untagged"  # while it exists, the probe's commits carry no tag
PROBE_ALPHA_RESET = "/tmp/pw-probe-alpha-reset"  # native_display_probe.c ALPHA_RESET
# The probe's protocol code, generated as the library's own build does.
xml = "/usr/share/wayland-protocols/stable/xdg-shell/xdg-shell.xml"
subprocess.run(["wayland-scanner", "client-header", xml, "/tmp/xdg-shell-client.h"], check=True)
subprocess.run(["wayland-scanner", "private-code", xml, "/tmp/xdg-shell.c"], check=True)
ALPHA_XML = "/usr/share/wayland-protocols/staging/alpha-modifier/alpha-modifier-v1.xml"
subprocess.run(["wayland-scanner", "client-header", ALPHA_XML, "/tmp/alpha-modifier-v1-client-protocol.h"],
               check=True)
subprocess.run(["wayland-scanner", "private-code", ALPHA_XML, "/tmp/alpha-modifier-v1.c"], check=True)
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
        "/tmp/alpha-modifier-v1.c",
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
# Frame protocol v4 (roadmap 1c, P2): a second probe, an app on the library's two v4 functions. It
# prints each new `picture` operations text it receives for the Output ("PICTURE <text>") and tags
# every buffer with tag_picture and the picture change given as argv[1].
PICTURE_PROBE = r"""
#define _GNU_SOURCE
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <wayland-client.h>
#include "xdg-shell-client.h"
#include "frame-client.h"
static struct wl_compositor *comp;
static struct wl_shm *shm;
static struct xdg_wm_base *wm;
static int configured;
static void ping(void *d, struct xdg_wm_base *w, uint32_t serial) { (void)d; xdg_wm_base_pong(w, serial); }
static const struct xdg_wm_base_listener wm_listener = {.ping = ping};
static void global(void *d, struct wl_registry *r, uint32_t n, const char *i, uint32_t v) {
    (void)d; (void)v;
    if (!strcmp(i, "wl_compositor")) comp = wl_registry_bind(r, n, &wl_compositor_interface, 1);
    if (!strcmp(i, "wl_shm")) shm = wl_registry_bind(r, n, &wl_shm_interface, 1);
    if (!strcmp(i, "xdg_wm_base")) {
        wm = wl_registry_bind(r, n, &xdg_wm_base_interface, 1); xdg_wm_base_add_listener(wm, &wm_listener, NULL);
    }
}
static void removed(void *d, struct wl_registry *r, uint32_t n) { (void)d; (void)r; (void)n; }
static const struct wl_registry_listener registry_listener = {global, removed};
static void configure(void *d, struct xdg_surface *s, uint32_t serial) {
    (void)d; xdg_surface_ack_configure(s, serial); configured = 1;
}
static const struct xdg_surface_listener surface_listener = {configure};
static void top_config(void *d, struct xdg_toplevel *t, int32_t w, int32_t h, struct wl_array *a) {
    (void)d; (void)t; (void)w; (void)h; (void)a;
}
static void top_close(void *d, struct xdg_toplevel *t) { (void)d; (void)t; exit(0); }
static const struct xdg_toplevel_listener top_listener = {.configure = top_config, .close = top_close};
int main(int argc, char **argv) {
    if (argc < 2) return 2;
    struct wl_display *d = wl_display_connect(NULL);
    if (!d) return 2;
    struct wl_registry *r = wl_display_get_registry(d);
    wl_registry_add_listener(r, &registry_listener, NULL);
    if (wl_display_roundtrip(d) < 0 || wl_display_roundtrip(d) < 0) return 3;
    struct pw_frame_client *c = pw_frame_client_create(d);
    if (!c) return 5;
    struct wl_surface *s = wl_compositor_create_surface(comp);
    struct xdg_surface *xs = xdg_wm_base_get_xdg_surface(wm, s);
    xdg_surface_add_listener(xs, &surface_listener, NULL);
    struct xdg_toplevel *top = xdg_surface_get_toplevel(xs);
    xdg_toplevel_add_listener(top, &top_listener, NULL);
    xdg_toplevel_set_app_id(top, "photo-wall-headless");
    wl_surface_commit(s);
    int fd = memfd_create("picture-probe", MFD_CLOEXEC), size = 640 * 480 * 4;
    if (ftruncate(fd, size) < 0) return 6;
    uint32_t *p = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    for (int i = 0; i < 640 * 480; i++) p[i] = 0xff445566;
    struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, size);
    struct wl_buffer *b = wl_shm_pool_create_buffer(pool, 0, 640, 480, 640 * 4, WL_SHM_FORMAT_XRGB8888);
    close(fd);
    char printed[1025] = "";
    for (int n = 0; n < 3000; n++) {
        usleep(30000);
        wl_display_dispatch_pending(d);
        struct pollfd pollfd = {.fd = wl_display_get_fd(d), .events = POLLIN};
        wl_display_flush(d);
        if (poll(&pollfd, 1, 100) > 0 && wl_display_dispatch(d) < 0) return 7;
        char operations[1025], grant[37], frame[97];
        uint64_t generation, revision;
        uint32_t state;
        int picture = pw_frame_client_picture(c, "headless", operations, sizeof operations);
        if (picture < 0) { fprintf(stderr, "picture probe: picture failed\n"); return 8; }
        if (picture == 1 && strcmp(operations, printed)) {
            strcpy(printed, operations); printf("PICTURE %s\n", operations); fflush(stdout);
        }
        int current = pw_frame_client_grant(c, "headless", grant, frame, &generation, &revision, &state);
        if (current < 0) { fprintf(stderr, "picture probe: grant lost\n"); return 9; }
        if (!configured || current != 1) continue;
        if (pw_frame_client_tag_picture_next_commit(c, s, grant, "picture-probe", argv[1]) < 0) {
            fprintf(stderr, "picture probe: tag_picture failed\n"); return 10;
        }
        wl_surface_attach(s, b, 0, 0); wl_surface_damage(s, 0, 0, 640, 480); wl_surface_commit(s);
    }
    return 0;
}
"""
pathlib.Path("/tmp/picture_probe.c").write_text(PICTURE_PROBE)
subprocess.run(
    ["cc", "-Wall", "-Wextra", "-Werror", "-I/tmp", "-I/current-native", "/tmp/picture_probe.c",
     "/tmp/xdg-shell.c", "-L" + CLIENT, "-Wl,-rpath," + CLIENT, "-lphoto-wall-frame-client",
     "-lwayland-client", "-o", "/tmp/picture-probe"],
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
    return capture(lambda pixels, stride: tuple(reversed(pixels[y * stride + x * 4: y * stride + x * 4 + 3])))


def capture_box(x0, y0, x1, y1):
    """The raw XRGB bytes of output pixels x0..x1-1, y0..y1-1, row by row."""
    return capture(lambda pixels, stride: b"".join(
        pixels[row * stride + x0 * 4: row * stride + x1 * 4] for row in range(y0, y1)))


def capture(read):
    """`read(pixels, stride)` over one capture of Weston's framebuffer (weston_capture_v1)."""
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
                        return read(pixels, stride)
                buffer.destroy()
                pool.destroy()
            finally:
                os.close(fd)
            assert state.get("done") == "retry", ("capture", state)
        raise AssertionError(("capture_retries", state))
    finally:
        display.disconnect()


def health_views():
    """Weston's own record of the health layer's views: their blocks of the one-shot `scene-graph`
    debug dump (weston_debug_v1, enabled by --debug), written into a memfd so a long dump never
    blocks the compositor; an unmapped surface's view is in no layer, so not in the dump."""
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
    return [block for block in dump.split("\tView ")[1:] if "role photo-wall-health," in block]


def assert_health_view(*lines):
    """The one health view's scene-graph block holds every one of `lines`."""
    views = health_views()
    assert len(views) == 1, views
    assert all(line in views[0] for line in lines), (lines, views[0])
    return views[0]


def assert_no_health_view():
    """No health view is mapped (render redesign S1: the layer stays unmapped while there is
    nothing to show, so the Player's pictures can stay on planes)."""
    views = health_views()
    assert not views, views


def assert_whole_output(rgb):
    """Every pixel of the captured 640 x 480 Output is exactly `rgb`: nothing drawn over the app."""
    pixels = capture_box(0, 0, 640, 480)
    wanted = bytes(reversed(rgb))
    wrong = next((i // 4 for i in range(0, len(pixels), 4) if pixels[i:i + 3] != wanted), None)
    assert wrong is None, ("pixel", wrong % 640, wrong // 640, tuple(reversed(pixels[wrong * 4:wrong * 4 + 3])))


# The production client's tint-on health view: one whole-Output shm buffer, mapped 1:1 (never a
# small shm buffer: Weston 14 DRM's cursor-plane path).
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


# Frame protocol v4 (roadmap 1c, P2). The slate's sixth line (overlay/render.py render_slate) on a
# 640-wide Output: scale 0.5, baseline y 172.5, x from 24, 9 px text.
BRIGHTNESS_BOX = (20, 160, 260, 178)
OPERATIONS = ('{"contrast_slope":0.8,"gamma_exponent":1.0,"light":{"black":0.0003,"matched_lux":1000.0},'
              '"output_id":"headless","picture_change":6,"rgb_multipliers":[1.0,1.0,1.0],"test_pattern":null}')
NOT_JSON = "not-json!!"     # 10 bytes: the shell forwards, never parses


def probe_line(proc, wanted, limit=8):
    """A probe's next stdout line that is `wanted`, within `limit` s."""
    import selectors

    end = time.monotonic() + limit
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        while time.monotonic() < end:
            if not selector.select(end - time.monotonic()):
                break
            line = proc.stdout.readline()
            assert line, ("probe_exited", proc.poll())
            if line.rstrip("\n") == wanted:
                return line
            drain()
    raise AssertionError(("probe_line", wanted))


def picture_lines(proc, wanted, limit=8):
    """The v4 probe's next `PICTURE <text>` line whose text is `wanted`, within `limit` s."""
    return probe_line(proc, "PICTURE " + wanted, limit)


def picture_v4():
    """The controller's `picture` op reaches the Output's v4 app verbatim (stored, so an app granted
    later gets it), its brightness line reaches the private client's slate, and `tag_picture`'s
    picture change comes back in `presented`; a v3 `tag` presents null."""
    global app
    presented = [e for e in events if e["event"] == "presented"]
    assert presented and all("picture_change" in e and e["picture_change"] is None for e in presented)
    print("PASS v3_tag_presents_a_null_picture_change", flush=True)
    # The production private client (manager v4) draws the brightness line; the magenta test
    # client binds v3. Back to the magenta one at the end, for the steps that follow.
    pathlib.Path(HEALTH_MODE).write_text("production")
    kill_private_client()
    read_until(lambda e: e["event"] == "diagnostic_presented", limit=6)
    time.sleep(0.5)
    before = capture_box(*BRIGHTNESS_BOX)
    assert request("picture", operations=OPERATIONS, brightness="Brightness: display 46 %",
                   drawing=True)["accepted"]
    assert not request("picture", operations="x" * 1025, brightness="", drawing=True)["accepted"]
    assert not request("picture", operations=OPERATIONS, brightness="b" * 129, drawing=True)["accepted"]
    assert not request("picture", operations=OPERATIONS, brightness="")["accepted"]   # no `drawing`
    assert not request("picture", operations=OPERATIONS, brightness="", drawing="yes")["accepted"]
    print("PASS picture_op_refuses_a_missing_or_non_boolean_drawing", flush=True)
    end = time.monotonic() + 5
    while capture_box(*BRIGHTNESS_BOX) == before:
        assert time.monotonic() < end, "slate brightness line not drawn"
        drain()
        time.sleep(0.1)
    print("PASS slate_draws_the_brightness_line", flush=True)
    app = subprocess.Popen(["/tmp/picture-probe", "6"], env=env, preexec_fn=user, stdout=subprocess.PIPE,
                           text=True)
    ticks = int(pathlib.Path(f"/proc/{app.pid}/stat").read_text().rpartition(") ")[2].split()[19])
    identity = {"frame_id": "picture-frame", "output": output,
                "process": {"pid": app.pid, "start_ticks": ticks, "invocation_id": str(uuid.uuid4())},
                "app_epoch": 1, "binding_generation": 1, "config_revision": 1}
    assert request("candidate", identity=identity, grant_id=str(uuid.uuid4()), uid=10004)["accepted"]
    picture_lines(app, OPERATIONS)
    print("PASS app_granted_after_the_op_receives_the_stored_operations", flush=True)
    tagged = read_until(lambda e: e["event"] == "presented" and e["identity"] == identity)
    assert tagged["frame_tag"] == "picture-probe" and tagged["picture_change"] == "6", tagged
    print("PASS tag_picture_echoes_the_picture_change", flush=True)
    assert request("picture", operations=NOT_JSON, brightness="", drawing=True)["accepted"]
    picture_lines(app, NOT_JSON)
    print("PASS picture_op_reaches_the_v4_app_verbatim", flush=True)
    app.terminate()
    app.wait(timeout=3)
    app = None
    read_until(lambda e: e["event"] == "invalidated")
    read_until(lambda e: e["event"] == "diagnostic_presented")
    killed = kill_private_client()
    read_until(lambda e: e["event"] == "diagnostic_presented", limit=6)
    await_pixel(*HEALTH, health_over(SLATE), 6.0 - (time.monotonic() - killed), drained=True)
    time.sleep(0.5)
    drain()     # the respawn's own presentations: later steps wait for new ones


def admit(args, frame_id):
    """Start /tmp/probe with `args` as the Output's app (its stdout piped), grant it and hand the
    Output off to it; (identity, grant id) once the handoff is presented."""
    global app
    app = subprocess.Popen(["/tmp/probe", *args], env=env, preexec_fn=user, stdout=subprocess.PIPE,
                           text=True)
    ticks = int(pathlib.Path(f"/proc/{app.pid}/stat").read_text().rpartition(") ")[2].split()[19])
    identity = {"frame_id": frame_id, "output": output,
                "process": {"pid": app.pid, "start_ticks": ticks, "invocation_id": str(uuid.uuid4())},
                "app_epoch": 1, "binding_generation": 1, "config_revision": 1}
    grant = str(uuid.uuid4())
    assert request("candidate", identity=identity, grant_id=grant, uid=10004)["accepted"]
    shown = read_until(lambda e: e["event"] == "presented" and e["identity"] == identity)
    handoff = str(uuid.uuid4())
    assert request("handoff", grant_id=grant, handoff_id=handoff, buffer_id=shown["buffer_id"])["accepted"]
    read_until(lambda e: e["event"] == "handoff_presented" and e["handoff_id"] == handoff)
    return identity, grant


def window_alpha_and_turn():
    """Render redesign S1 (bead B2). Window alpha: the probe's opaque white sub-surface at
    multiplier 0x7fffffff over its black window is half white, and back to opaque once its
    modifier is destroyed (mutation probe: no weston_view_set_alpha in shell.c alpha_applied ->
    255). The turn: transform 1 is confirmed by the `turn` event, every client's wl_output says 1
    and the Output is its mode turned; transform 4 is refused."""
    global app, output
    pathlib.Path(PROBE_ALPHA_RESET).unlink(missing_ok=True)
    # Hold (issue #123): with no drawing of Display's, the probe's sub-surface (a picture plane at
    # alpha 0.5) holds the handed-off Output. Mutation probe: no picture plane evidence in
    # `hold_evidence` -> the slate covers it and the half white is never captured.
    assert request("picture", operations=OPERATIONS, brightness="", drawing=False)["accepted"]
    admit(["alpha"], "alpha-frame")
    held = read_until(lambda e: e["event"] == "hold" and e["output"] == output)
    assert (held["holding"], held["evidence"]) == (True, "picture_plane"), held
    half = await_pixel(200, 175, (127.5, 127.5, 127.5), 3, tolerance=0.5)   # 127 or 128
    assert_pixel(40, 40, (0, 0, 0), tolerance=0)
    assert_pixel(99, 175, (0, 0, 0), tolerance=0)                           # just left of it
    print("PASS window_alpha_multiplier_halves_the_sub_surface", half, flush=True)
    print("PASS a_picture_plane_holds_the_handed_off_output", flush=True)
    pathlib.Path(PROBE_ALPHA_RESET).write_text("")
    print("PASS window_alpha_object_destroyed_resets_to_opaque",
          await_pixel(200, 175, (255, 255, 255), 3, tolerance=0), flush=True)
    assert not any(e["event"] == "hold" and not e["holding"] for e in events[events.index(held):])
    probe_line(app, "TRANSFORM 0")
    sent = len(events)
    assert not request("turn", transform=4)["accepted"]
    assert not any(e["event"] == "turn" for e in events[sent:])
    print("PASS turn_refuses_a_flipped_transform", flush=True)
    assert request("turn", transform=1)["accepted"]
    turned = [e for e in events[sent:] if e["event"] == "turn"]
    assert turned == [{"event": "turn", "output_id": output["output_id"], "transform": 1}], turned
    moved = [e for e in events[sent:] if e["event"] == "output"]
    assert moved and (moved[-1]["width"], moved[-1]["height"]) == (480, 640), moved
    lost = [e for e in events[sent:] if e["event"] == "invalidated"]
    assert lost and lost[-1]["reason"] == "output_mode_changed", lost   # a mode change, as any
    output = moved[-1]["output"]
    probe_line(app, "TRANSFORM 1")
    print("PASS turn_event_then_the_clients_output_transform", flush=True)
    sent = len(events)
    assert request("turn", transform=0)["accepted"]
    back = [e for e in events[sent:] if e["event"] == "output"]
    assert back and (back[-1]["width"], back[-1]["height"]) == (640, 480), back
    output = back[-1]["output"]
    probe_line(app, "TRANSFORM 0")
    print("PASS turn_back_to_upright", flush=True)
    app.terminate()
    app.wait(timeout=3)
    app = None


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
    # The frame client's word per surface (the Player's pacing): more surfaces than its table
    # holds (a remap gives an Output a new wl_surface) each still get their word. Mutation probe:
    # no oldest-entry reuse in frame-client.c `retire` (surface 33 gets none).
    words = subprocess.run(["/tmp/probe", "words"], env=env, preexec_fn=user,
                           capture_output=True, text=True, timeout=20)
    assert words.returncode == 0, (words.returncode, words.stderr)
    print("PASS frame_client_word_for_every_new_surface", flush=True)
    # The probe draws its ground alone: Display's drawing holds the Output for the steps that
    # look at the probe's pixels (module docstring).
    assert request("picture", operations=OPERATIONS, brightness="", drawing=True)["accepted"]
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
    held = read_until(lambda e: e["event"] == "hold")
    assert (held["holding"], held["evidence"]) == (True, "display_drawing"), held
    read_until(lambda e: e["event"] == "presented" and e["frame_tag"] == "authored-probe")
    print("PASS separate_handoff_ack_then_admitted_grant", flush=True)
    print("PASS handed_off_app_pixel_captured", assert_pixel(320, 240, PROBE_APP), flush=True)
    print("PASS health_layer_above_live_app_after_handoff",
          assert_pixel(*HEALTH, health_over(PROBE_APP)), flush=True)
    # Signal off (1b D2): a forced-off output presents nothing, so the app's surface lease
    # (LEASE_MS 5 s) holds on the app's answers to Weston's ping while it is off; on again, the
    # same grant presents. Mutation probe: drop the ping in lease_tick (invalidated after 5 s).
    def output_power(power):
        """The op, and the shell's `output_power` event (sent ahead of the op's response)."""
        sent = len(events)
        assert request("output_power", power=power)["accepted"]
        said = [e for e in events[sent:] if e["event"] == "output_power"]
        assert (said or [read_until(lambda e: e["event"] == "output_power")])[0]["power"] == power

    powered = len(events)
    output_power("off")
    quiet = time.monotonic() + 6.5
    while time.monotonic() < quiet:
        time.sleep(0.25)
        drain()
    off_events = events[powered:]
    assert not any(e["event"] == "invalidated" for e in off_events), off_events[-12:]
    output_power("on")
    resumed = read_until(lambda e: e["event"] == "presented" and e["frame_tag"] == "authored-probe")
    assert resumed["grant_id"] == grant and not any(e["event"] == "invalidated" for e in events[powered:])
    print("PASS signal_off_holds_the_apps_grant_past_its_lease", flush=True)
    # Black picture (2a, #83): the `black` op maps an opaque black solid-colour view above every
    # view on the Output, the health surface included, and unmaps it; the shell's `black` event
    # (sent ahead of the op's response) says which. The app's grant holds past its lease under it.
    # Mutation probe (P-no-event): drop the answering event -> read_until times out.
    def black(on):
        sent = len(events)
        assert request("black", on=on)["accepted"]
        said = [e for e in events[sent:] if e["event"] == "black"]
        assert (said or [read_until(lambda e: e["event"] == "black")])[0]["on"] is on

    blacked = len(events)
    black(True)
    await_pixel(320, 240, (0, 0, 0), 2, tolerance=0)
    assert_pixel(*HEALTH, (0, 0, 0), tolerance=0)
    hold_pixel(320, 240, (0, 0, 0), 6.5, tolerance=0)
    black(False)
    await_pixel(320, 240, PROBE_APP, 3, tolerance=0, drained=True)
    assert not any(e["event"] == "invalidated" for e in events[blacked:]), events[-12:]
    print("PASS black_picture_curtain_over_every_view_holds_the_apps_grant", flush=True)
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
    # The respawned client binds the manager v3 and takes its health layer but maps no buffer: it
    # is alive, so the tint goes (render redesign S1: the rule keys on the client's liveness, not
    # on a mapped health surface). Mutation probe: key it on a mapped surface again -> amber stays.
    assert await_marker(6) == "bare"
    await_pixel(320, 240, PROBE_APP, 1, tolerance=0, drained=True)
    print("PASS fallback_tint_goes_when_the_respawned_client_binds_mapping_nothing",
          hold_pixel(320, 240, PROBE_APP, 0.5, tolerance=0), flush=True)
    # Mapped (and presented), then a NULL buffer unmaps it: the client lives, so no tint.
    pathlib.Path(HEALTH_MODE).write_text("unmap")
    kill_private_client()
    assert await_marker(6) == "presented"
    await_pixel(*HEALTH, PROBE_APP, 3, tolerance=0, drained=True)
    print("PASS unmapped_health_surface_of_a_live_client_shows_no_tint",
          hold_pixel(320, 240, PROBE_APP, 0.5, tolerance=0), flush=True)
    drain()
    killed = kill_private_client()
    await_pixel(320, 240, FALLBACK, 1.0 - (time.monotonic() - killed))
    print("PASS fallback_tint_dropped_when_the_respawned_client_binds",
          await_pixel(320, 240, PROBE_APP, 5), flush=True)
    print("PASS respawned_health_layer_above_live_app",
          await_pixel(*HEALTH, health_over(PROBE_APP), 3), flush=True)
    # The production client (B11): its own health layer and judge link, against the fake judge.
    name = output["output_id"]
    pathlib.Path(HEALTH_MODE).write_text("production")
    opened = judge.opened
    killed = kill_private_client()
    await_pixel(320, 240, FALLBACK, 1.0 - (time.monotonic() - killed))
    # (1) Nothing to show before any instruction: no health view mapped, no amber (the client is
    # alive), the Output exactly the app's pixels, while the judge holds back; the shell respawns
    # the client 2 s after it is gone. Mutation probe: map the transparent base buffer again ->
    # a health view is in the scene graph.
    await_pixel(320, 240, PROBE_APP, 5.0 - (time.monotonic() - killed), tolerance=0, drained=True)
    hold_pixel(320, 240, PROBE_APP, 1.0, tolerance=0)
    judge.await_client(opened + 1, 3, drain)
    assert not judge.reports and not judge.refused, (judge.reports, judge.refused)
    assert_no_health_view()
    assert_whole_output(PROBE_APP)
    print("PASS production_health_layer_unmapped_with_nothing_to_show", flush=True)
    # (2) Tint on: the darkened Output and the card, above the still-live app, reported presented.
    judge.send(name, 7, True, CARD_LINES)
    judge.await_report(name, 7, 3, drain)
    tinted = await_pixel(320, 240, HEALTH_TINT, 1)
    assert_health_view(*HEALTH_TINT_VIEW)
    print("PASS health_tint_and_card_above_live_app", tinted,
          assert_pixel(*CARD_PIXEL, SLATE), flush=True)
    # (3) Tint off: the layer unmapped (reported presented once the compositor processed it), so
    # the app shows exactly, with no amber fallback.
    judge.send(name, 8, False, ("", ""))
    judge.await_report(name, 8, 3, drain)
    await_pixel(320, 240, PROBE_APP, 1, tolerance=0)
    hold_pixel(320, 240, PROBE_APP, 0.5, tolerance=0)
    assert_no_health_view()
    assert_whole_output(PROBE_APP)
    print("PASS health_tint_off_unmaps_and_restores_app", flush=True)
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
    # Render only what reaches the screen (2026-10-17): a still app commits nothing for longer than
    # LEASE_MS and keeps its grant, on its answers to Weston's ping (one a second), with no
    # presentation at all. Mutation probe: drop the ping in lease_tick (invalidated after 5 s).
    pathlib.Path(PROBE_STILL).write_text("")
    time.sleep(0.3)
    drain()
    still_from = len(events)
    quiet = time.monotonic() + 7
    while time.monotonic() < quiet:
        time.sleep(0.25)
        drain()
    still_events = events[still_from:]
    assert not any(e["event"] in ("invalidated", "presented") for e in still_events), \
        still_events[-12:]
    print("PASS still_app_keeps_its_grant_on_ping_answers_with_no_commit", flush=True)
    # Hold (issue #123), liveness and hold apart. An app whose draws fail commits untagged buffers
    # while it still answers every ping: it is alive, so it keeps its grant, but its ground holds
    # nothing, so without Display's drawing the holding slate covers it (`hold` false) within a
    # tick, and Display's drawing takes it back off. Mutation probe: `hold_evidence` returns
    # HOLD_PICTURE_PLANE unconditionally -> no `hold` false, read_until times out.
    pathlib.Path(PROBE_STILL).unlink()      # the replacement candidate draws again: hand it off
    shown = read_until(lambda e: e["event"] == "presented" and e["identity"] == replacement)
    handoff = str(uuid.uuid4())
    assert request("handoff", grant_id=replacement_grant, handoff_id=handoff,
                   buffer_id=shown["buffer_id"])["accepted"]
    read_until(lambda e: e["event"] == "handoff_presented" and e["handoff_id"] == handoff)
    pathlib.Path(PROBE_UNTAGGED).write_text("")   # its commits name nothing from here on
    hold_from = len(events)
    assert request("picture", operations=OPERATIONS, brightness="", drawing=False)["accepted"]
    lost = read_until(lambda e: e["event"] == "hold" and not e["holding"], limit=3)
    assert lost["evidence"] is None and lost["output"] == output, lost
    print("PASS a_ground_only_app_loses_the_screen_to_the_slate",
          await_pixel(40, 40, SLATE, 2, drained=True), flush=True)
    quiet = time.monotonic() + 6.5          # past LEASE_MS: its ping answers keep the grant
    while time.monotonic() < quiet:
        time.sleep(0.25)
        drain()
    assert not any(e["event"] == "invalidated" for e in events[hold_from:]), events[-12:]
    assert_pixel(40, 40, SLATE)
    print("PASS an_app_answering_pings_keeps_its_grant_under_the_slate", flush=True)
    assert request("picture", operations=OPERATIONS, brightness="", drawing=True)["accepted"]
    back = read_until(lambda e: e["event"] == "hold" and e["holding"], limit=3)
    assert back["evidence"] == "display_drawing", back
    print("PASS displays_drawing_takes_the_slate_back_off",
          await_pixel(320, 240, PROBE_APP, 2, drained=True), flush=True)
    pathlib.Path(PROBE_UNTAGGED).unlink()
    pathlib.Path(PROBE_STILL).write_text("")
    time.sleep(0.3)
    drain()
    # A stopped app answers no ping: its grant lapses within the lease, and the diagnostic returns.
    # Mutation probe: renew the lease on every ping sent rather than on its answer.
    os.kill(app.pid, signal.SIGSTOP)
    stopped = time.monotonic()
    lost = read_until(lambda e: e["event"] == "invalidated", limit=8)
    assert lost["reason"] == "surface_lease_or_process_lost", lost
    assert time.monotonic() - stopped < 7, time.monotonic() - stopped
    read_until(lambda e: e["event"] == "diagnostic_presented")
    os.kill(app.pid, signal.SIGCONT)
    pathlib.Path(PROBE_STILL).unlink()
    app.terminate()
    app.wait(timeout=3)
    app = None
    print("PASS stopped_app_loses_its_grant_within_the_lease_and_the_diagnostic_returns", flush=True)
    picture_v4()
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
    window_alpha_and_turn()
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
