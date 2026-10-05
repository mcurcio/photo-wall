"""Manual native compositor fixture, run only inside a disposable Linux container.

Mount tests at /smoke and appliance/display_host at /current-source, both read-only.
Requires the pinned native build image's compiler, Weston14, Cairo and Wayland deps,
plus pywayland (scripts/run_display_harness.py builds that image and runs this file).
This executes a real headless compositor, not a recorded callback simulator. It
uses fixture controller authority and does not qualify systemd, GTK or HDMI.
Output pixels are read back through Weston's own weston_capture_v1 (enabled by --debug).
"""

import json
import mmap
import os
import pathlib
import signal
import socket
import subprocess
import sys
import time
import uuid

subprocess.run(
    ["meson", "setup", "/tmp/native-build", "/current-source", "--prefix=/usr", "--libdir=lib"],
    check=True,
)
subprocess.run(["meson", "compile", "-C", "/tmp/native-build"], check=True)
subprocess.run(["meson", "install", "-C", "/tmp/native-build"], check=True)
xml = "/usr/share/wayland-protocols/stable/xdg-shell/xdg-shell.xml"
subprocess.run(["wayland-scanner", "client-header", xml, "/tmp/xdg-shell-client.h"], check=True)
subprocess.run(["wayland-scanner", "private-code", xml, "/tmp/xdg-shell.c"], check=True)
subprocess.run(
    [
        "cc",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-I/tmp",
        "-I/tmp/native-build",
        "-I/current-source/native",
        "/smoke/native_display_probe.c",
        "/tmp/xdg-shell.c",
        "/tmp/native-build/photo-wall-frame-protocol.c",
        "-L/usr/lib/photo-wall-client",
        "-Wl,-rpath,/usr/lib/photo-wall-client",
        "-lphoto-wall-frame-client",
        "-lwayland-client",
        "-ljansson",
        "-o",
        "/tmp/probe",
    ],
    check=True,
)
# Python bindings, generated here as the overlay client's build will: the core protocol (the
# generated interfaces import it relatively), Weston's capture protocol and the private one.
GENERATED = pathlib.Path("/tmp/pw-protocols")
subprocess.run(
    [
        sys.executable,
        "-m",
        "pywayland.scanner",
        "-i",
        "/usr/share/wayland/wayland.xml",
        "/usr/share/libweston-14/protocols/weston-output-capture.xml",
        "/current-source/native/photo-wall-frame-v1.xml",
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
from pw_protocols.weston_output_capture import WestonCaptureV1  # noqa: E402
from pywayland.client import Display  # noqa: E402

print("PYWAYLAND", pywayland.__version__, PwDiagnosticManagerV1.name,
      PwDiagnosticManagerV1.version, flush=True)
SLATE = (10, 15, 23)  # diagnostic-client.c background (0.04, 0.06, 0.09), opaque
PROBE_APP = (0x22, 0x33, 0x44)  # native_display_probe.c fill 0xff223344
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
}
log = open("/tmp/pw-weston.log", "w")
p = subprocess.Popen(
    [
        "weston",
        "--backend=headless-backend.so",
        "--renderer=pixman",
        "--shell=/usr/lib/photo-wall-display/photo-wall-shell.so",
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


def assert_pixel(x, y, expected, tolerance=2):
    actual = capture_pixel(x, y)
    assert all(abs(a - e) <= tolerance for a, e in zip(actual, expected)), (x, y, actual, expected)
    return actual


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
    assert request("candidate", identity=identity, grant_id=grant, uid=10004)["accepted"]
    first = read_until(lambda e: e["event"] == "presented")
    assert first["frame_tag"] == "synthetic-probe" and first["identity"] == identity
    print("PASS candidate_exact_identity_compositor_feedback", flush=True)
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
    children = pathlib.Path(f"/proc/{p.pid}/task/{p.pid}/children").read_text().split()
    diag = next(
        int(pid)
        for pid in children
        if b"diagnostic-client" in pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
    )
    os.kill(diag, signal.SIGKILL)
    killed = time.monotonic()
    # While no private client is bound (the shell respawns it 2 s after it is gone), the
    # client-identity check alone refuses a foreign bind: no held resource masks it here.
    while not exited(diag):
        time.sleep(0.01)
    foreign_bind_refused()
    assert time.monotonic() - killed < 1.8, "foreign bind missed the respawn window"
    print("PASS foreign_bind_refused_without_private_client", flush=True)
    read_until(lambda e: e["event"] == "diagnostic_presented", limit=6)
    print("PASS diagnostic_client_crash_recovers", flush=True)
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
    print("WESTON_EXIT", p.returncode, flush=True)
    print(pathlib.Path("/tmp/pw-weston.log").read_text(), flush=True)
