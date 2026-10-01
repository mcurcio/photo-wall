"""Container-only fixture for scripts.node_service_probe; never a physical effect proof."""
# ruff: noqa: E402 -- imports intentionally resolve the installed private base closures.
import json
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, "/usr/lib/photo-wall-manager-supervisor")
import contracts
from appliance.node.environment import verify_root
from appliance.node.manager_launcher import UNIT, SystemdManagerLauncher

contracts.__path__.append("/usr/lib/photo-wall-app-broker/contracts")
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_commands import NodeSessionGrant, encode_session_grant, parse_session_claim
from contracts.node_protocol import NodeProducerV2

seen = []
session = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def response(self, status, raw):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self):
        return (
            bool(session)
            and self.headers.get("Authorization") == "Bearer " + session[0].credential
            and self.headers.get("X-Node-Session") == str(session[0].session_id)
        )

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/v2/node/sessions":
            claim = parse_session_claim(raw)
            assert claim.owner == "app_manager"
            session[:] = [claim]
            producer = NodeProducerV2(
                "pid1-fixture",
                "device-" + "a" * 64,
                1,
                claim.kernel_boot_id,
                claim.owner,
                claim.incarnation_id,
            )
            seen.append("session")
            self.response(
                200,
                encode_session_grant(
                    NodeSessionGrant(
                        producer,
                        claim.session_id,
                        claim.offer_id,
                        600000,
                        "evidence",
                    )
                ),
            )
        elif self.path == "/v2/node/app-preparation" and self.authorized():
            value = json.loads(raw)
            assert (
                value["kind"] == "manager_preparation"
                and value["producer"]["owner"] == "app_manager"
                and value["state"] == "idle"
            )
            seen.append("observation")
            self.response(200, b'{"stored":true,"authority_granted":false}')
        else:
            self.response(403, b"{}")

    def do_GET(self):
        if self.path == "/v2/node/app-desired" and self.authorized():
            seen.append("desired")
            self.response(200, b'{"commands":[],"scope":"preparation_read_only"}')
        else:
            self.response(403, b"{}")


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
source = Path("/var/lib/node-manager-environment")
ref = AppEnvironmentRefV2(**json.loads(next(source.glob("*.json")).read_text()))
roots = Path("/run/photo-wall-node-storage/manager-roots")
abi = {key: getattr(ref, key) for key in ("base_abi", "graphics_abi", "plugin_abi")}
preparation = Path("/run/photo-wall-node-storage/preparation")
if (preparation / "session").exists():
    shutil.rmtree(preparation / "session")
config = Path("/run/photo-wall-node/manager-client.json")
config.write_text(
    json.dumps(
        {
            "central": f"http://127.0.0.1:{server.server_port}",
            "serial": "pid1-probe",
            "offer_id": str(uuid4()),
            **abi,
        }
    )
)
config.chmod(0o600)
launcher = SystemdManagerLauncher(roots, {ref.environment_sha256: ref}, **abi)
try:
    assert launcher.start(ref.environment_sha256)
    for _ in range(100):
        if "observation" in seen:
            break
        time.sleep(0.1)
    assert {"session", "desired", "observation"} <= set(seen), seen
    props = subprocess.check_output(
        [
            "systemctl",
            "show",
            UNIT,
            "-p",
            "MainPID",
            "-p",
            "User",
            "-p",
            "Group",
            "-p",
            "ControlGroup",
            "-p",
            "RootDirectory",
            "-p",
            "InvocationID",
            "-p",
            "MemoryMax",
            "-p",
            "NoNewPrivileges",
        ],
        text=True,
    )
    print(props, flush=True)
    pid = int(dict(line.split("=", 1) for line in props.splitlines())["MainPID"])
    for uid, expected in ((10003, 0), (10004, 23)):
        code = f'import os,pathlib;os.setgroups([]);os.setgid({uid});os.setuid({uid});p=pathlib.Path("/run/credentials/{UNIT}/node-config");\ntry:p.read_bytes()\nexcept PermissionError:raise SystemExit(23)'
        result = subprocess.run(
            [
                "nsenter",
                "--target",
                str(pid),
                "--mount",
                "--root",
                "--",
                "/usr/bin/python3",
                "-c",
                code,
            ]
        )
        assert result.returncode == expected, (uid, result.returncode)
    root = Path(f"/proc/{pid}/root")
    assert not (root / "run/photo-wall-node/manager-client.json").exists()
    assert not (root / "run/systemd/private").exists()
    assert (root / "run/photo-wall-preparation/session/session.json").exists()
    assert launcher.observed(ref.environment_sha256)
    print(
        "PASS real manager session grant, authenticated desired GET, idle observation POST; credential UID10003 allowed/UID10004 denied; host runtime absent",
        flush=True,
    )
finally:
    subprocess.run(["systemctl", "stop", UNIT], check=True)
    server.shutdown()
    server.server_close()
verify_root(roots / ref.environment_sha256, ref, **abi)
print("PASS immutable manager root verified after real PID1 stop", flush=True)
