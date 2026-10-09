"""Disposable Linux fixture for the actual DisplayService/HTTP/Weston chain.

The test supplies process authority in place of PID1; every presentation and
handoff acknowledgment still comes from the real compositor and app buffer.
"""

import json
import os
import select
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, "/repo")
from appliance.central_session.http import NodeHTTP
from appliance.display_host.runner import Controller
from appliance.display_host.service import DisplayService
from appliance.display_host.weston import WestonBackend
from appliance.kernel.clock import boottime_ms

original_request = NodeHTTP.request
def trace_request(self, method, path, *args, **kwargs):
    status, raw = original_request(self, method, path, *args, **kwargs)
    if status != 200 and path in ("/v2/node/display", "/v2/node/sessions"):
        print("FIXTURE_HTTP", path, status, json.loads(raw).get("error"), flush=True)
    return status, raw
NodeHTTP.request = trace_request

# The built display and frame client, from a local repo mounted at /node-debs (decision 0019).
if Path("/node-debs/Packages").exists():
    subprocess.run(["sh", "-c", "dpkg -i /node-debs/photo-wall-node-display_*.deb "
                    "/node-debs/photo-wall-frame-client_*.deb"], check=True, stdout=subprocess.DEVNULL)
CLIENT = "/usr/lib/photo-wall/frame-client"
native_source = "/repo/appliance/display_host/native"
if os.environ.get("PHOTO_WALL_GTK_PROBE") != "1":
    xml = "/usr/share/wayland-protocols/stable/xdg-shell/xdg-shell.xml"
    subprocess.run(["wayland-scanner", "client-header", xml, "/tmp/xdg-shell-client.h"], check=True)
    subprocess.run(["wayland-scanner", "private-code", xml, "/tmp/xdg-shell.c"], check=True)
    protocol = native_source + "/photo-wall-frame-v1.xml"
    subprocess.run(["wayland-scanner", "client-header", protocol, "/tmp/photo-wall-frame-client.h"], check=True)
    subprocess.run(["wayland-scanner", "private-code", protocol, "/tmp/photo-wall-frame-protocol.c"], check=True)
    subprocess.run(
        [
            "cc",
            "-I/tmp",
            "-I" + native_source,
            "/repo/tests/native_display_probe.c",
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
root = Path("/tmp/display-service")
root.mkdir(mode=0o711)
root.chmod(0o711)
env = {
    **os.environ,
    "XDG_RUNTIME_DIR": str(root),
    "WAYLAND_DISPLAY": "wayland-0",
    "PHOTO_WALL_CONTROLLER_UID": "0",
    "PHOTO_WALL_DISPLAY_HOST": "1",
    "GDK_BACKEND": "wayland",
    "LIBGL_ALWAYS_SOFTWARE": "1",
    "PYOPENGL_PLATFORM": "egl",
}
log = open("/tmp/service-weston.log", "w")
weston = subprocess.Popen(
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
    ],
    env=env,
    stdout=log,
    stderr=log,
)
app = service = None
try:
    for _ in range(100):
        if (root / "control.sock").exists():
            break
        time.sleep(0.05)
    (root / "wayland-0").chmod(0o666)

    def user():
        os.setgid(10004)
        os.setuid(10004)

    gtk = os.environ.get("PHOTO_WALL_GTK_PROBE") == "1"
    if gtk:
        env.update(WAYLAND_DEBUG="client", GSETTINGS_BACKEND="memory", XDG_CACHE_HOME="/tmp/gtk-cache-dir")
    gtk_log = open("/tmp/gtk-wayland.log", "w") if gtk else None
    app = subprocess.Popen(["/usr/bin/python3", "/repo/tests/native_display_gtk_probe.py"] if gtk else ["/tmp/probe"], env=env, preexec_fn=user, stderr=gtk_log)
    ticks = int(Path(f"/proc/{app.pid}/stat").read_text().rpartition(") ")[2].split()[19])
    invocation = uuid4()
    channel = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    channel.connect(str(root / "control.sock"))

    def verify(grant):
        return (
            grant.surface.process.pid == app.pid
            and grant.surface.process.start_ticks == ticks
            and grant.surface.process.invocation_id == invocation
            and app.poll() is None
        )

    backend = WestonBackend(
        channel, display_uid=0, compositor_pid=weston.pid, verify_process=verify
    )
    controller = Controller(backend)
    transport = NodeHTTP(sys.argv[1])
    status, raw = transport.request(
        "POST",
        "/fixture/ready",
        json.dumps(
            {
                "boot_id": str(controller.host.boot_id),
                "pid": app.pid,
                "start_ticks": ticks,
                "invocation_id": str(invocation),
                "boottime_ms": boottime_ms(),
            }
        ).encode(),
    )
    assert status == 200, (status, raw.decode())
    fixture = json.loads(raw)
    if gtk:
        Path("/tmp/gtk-configuration.json").write_text(json.dumps(fixture["configuration"]))
    service = DisplayService(
        controller,
        central=sys.argv[1],
        serial=fixture["serial"],
        offer_id=UUID(fixture["offer_id"]),
        runtime=root,
    )
    controller.service = service
    browser = os.environ.get("PHOTO_WALL_BROWSER_PROBE") == "1"
    trial = None
    withdrawing = False
    media_waiting = False
    partition_until = None
    expiry_checked = False
    trial_path = "/v1/operator/frames/native-frame/calibration-trials"
    end = time.monotonic() + (330 if browser else 50)
    while time.monotonic() < end:
        while backend.pending:
            controller.observe()
        if partition_until is None:
            service.tick()
        if select.select([channel], [], [], 0.05)[0]:
            controller.observe()
        if withdrawing:
            status, raw = transport.request("GET", "/fixture/completed")
            if status == 200 and json.loads(raw)["withdrawals"] == 1:
                state = controller.host.state("headless")
                assert state.admitted is None and state.candidate is None
                assert service.completed.get("headless")
                print("PASS production_driver_native_role_removal_before_response", flush=True)
                break
            continue
        if partition_until is not None:
            if time.monotonic() < partition_until:
                continue
            receipt = service.receipts.get("headless")
            assert receipt and not receipt.frame_tag.startswith("trial-"), receipt
            assert controller.host.state("headless").diagnostic == "released"
            print("PASS actual_gtk_partition_expiry_baseline_presentation", flush=True)
            transport.request("POST", "/fixture/advance", b"{}")
            partition_until = None
            expiry_checked = True
            trial = None
            trial_path = "/v1/operator/frames/native-frame/calibration-trials"
        if controller.host.states() and all(
            s.diagnostic == "released" for s in controller.host.states()
        ):
            status, raw = transport.request("GET", "/fixture/completed")
            if status == 200 and json.loads(raw)["completed"]:
                if gtk:
                    Path("/tmp/gtk-configuration.next").write_text(json.dumps(json.loads(raw)["configuration"]))
                    Path("/tmp/gtk-configuration.next").replace("/tmp/gtk-configuration.json")
                if browser:
                    if trial is None:
                        print("PASS production_driver_http_runtime_native_handoff", flush=True)
                        trial = {"browser": True}
                    if json.loads(raw).get("browser_done"):
                        print("PASS interactive_operator_browser_fixture", flush=True)
                        break
                    continue
                if trial is None:
                    print("PASS production_driver_http_runtime_native_handoff", flush=True)
                    status, raw = transport.request("POST", trial_path, b"{}")
                    if status == 409 and json.loads(raw).get("error") in (
                        "trial_admitted_surface_required", "trial_current_output_required"
                    ):
                        continue  # Recovery still needs a newly uploaded original presentation.
                    assert status == 200, (status, raw)
                    trial = json.loads(raw)
                    trial_path += "/" + trial["trial_id"]
                    calibration = {**trial["calibration"], "gain": 0.5}
                    status, raw = transport.request("POST", trial_path, json.dumps({
                        "operation": "edit", "expected_sequence": 1, "calibration": calibration,
                    }).encode())
                    assert status == 200, (status, raw)
                    trial = json.loads(raw)
                elif media_waiting:
                    from native_display_media import image_fixture

                    from contracts.models import OutputBinding
                    from contracts.node_frame import frame_witness_tag

                    _, variant = image_fixture()
                    binding = OutputBinding.model_validate(json.loads(raw)["configuration"]["bindings"][0])
                    expected = frame_witness_tag(output_id=binding.output_id, frame_id=binding.frame_id,
                        binding_generation=binding.generation, config_revision=binding.configuration_revision,
                        calibration=binding.calibration.model_dump(mode="json"),
                        layers=({"assignment_id": "fixture-media", "variant_sha256": variant.sha256},))
                    receipt = service.receipts.get("headless")
                    if receipt and receipt.frame_tag == expected and receipt.surface.config_revision == binding.configuration_revision:
                        print("PASS frozen_player_actual_media_assignment_witness", flush=True)
                        status, raw = transport.request("POST", "/fixture/unbind", b"{}")
                        assert status == 200, (status, raw)
                        withdrawing = True
                else:
                    status, raw = transport.request("POST", trial_path, json.dumps({
                        "operation": "status", "expected_sequence": 2,
                    }).encode())
                    assert status == 200, (status, raw)
                    trial = json.loads(raw)
                    assert trial["state"] == "active", trial
                    if trial["presented_sequence"] == 2:
                        if gtk and not expiry_checked:
                            partition_until = time.monotonic() + 6
                            continue
                        status, raw = transport.request("POST", trial_path, json.dumps({
                            "operation": "save", "expected_sequence": 2,
                        }).encode())
                        assert status == 200, (status, raw)
                        saved = json.loads(raw)
                        assert saved["state"] == "saved" and saved["saved_calibration"]["gain"] == 0.5
                        print("PASS first_calibration_operator_trial_native_save", flush=True)
                        if gtk and os.environ.get("PHOTO_WALL_MEDIA_PROBE") == "1":
                            Path("/tmp/fixture-media-authorized").touch()
                            media_waiting = True
                        else:
                            status, raw = transport.request("POST", "/fixture/unbind", b"{}")
                            assert status == 200, (status, raw)
                            withdrawing = True
    else:
        raise AssertionError(("production_driver_trial_timeout", trial, service.receipts, controller.host.states()))
finally:
    if service:
        service.close()
    if app:
        app.terminate()
        app.wait(timeout=3)
    weston.terminate()
    weston.wait(timeout=3)
    log.close()
    print(Path("/tmp/service-weston.log").read_text())
    if Path("/tmp/gtk-wayland.log").exists():
        lines = Path("/tmp/gtk-wayland.log").read_text().splitlines()
        print("GTK_WAYLAND", "\n".join(line for line in lines if any(token in line for token in
            ("pw_frame", "wp_presentation", ".commit", ".attach", "Error", "Traceback")))[-18000:])
