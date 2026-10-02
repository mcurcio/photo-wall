"""Actual restricted units in a disposable private PID1 fixture; no DRM/reboot effects."""

import json
import os
import pty
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/usr/lib/photo-wall-app-broker")
from appliance.node.app_link import proof_directory, remove_proof_socket
from appliance.node.environment import inventory, stage_archive, verify_root
from appliance.node.process_linux import app_unit_properties
from contracts.app_environment import AppEnvironmentRefV2


def run(*argv):
    return subprocess.check_output(argv, text=True, stderr=subprocess.STDOUT)


def wait_for(fn):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            value = fn()
            if value:
                return value
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(0.1)
    raise AssertionError("fixture deadline")


source = Path("/var/lib/node-manager-environment")
ref = AppEnvironmentRefV2(**json.loads((source / "reference.json").read_text()))
abi = {k: getattr(ref, k) for k in ("base_abi", "graphics_abi", "plugin_abi")}
run("/usr/bin/python3", "-I", "-B", "/usr/lib/photo-wall-node-bootstrap", "storage")
root = stage_archive(
    source / "environment.tar", Path("/run/photo-wall-node-storage/manager-roots"), ref, **abi
)
# A real sealed Python root executes the client fixture under the exact app sandbox.
# The manager archive is used only as a compact Python dependency fixture.
server = Path("/var/lib/ipc-server.py")
server.write_text("""import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,'/usr/lib/photo-wall-app-broker')
from appliance.node.app_link import BrokerLinkService
service=BrokerLinkService(SimpleNamespace(current=lambda:None),SimpleNamespace(ensure=lambda:None),Path('/run/photo-wall-app-proof/app-link.sock'))
while True: service.serve_one()
""")
client = Path("/var/lib/ipc-client.py")
client.write_text("""import os,socket,json,time,stat
from pathlib import Path
p=Path('/run/photo-wall-client/app-link.sock')
r=Path(os.environ['XDG_RUNTIME_DIR']); st=r.stat()
assert st.st_uid==10004 and stat.S_IMODE(st.st_mode)==0o700
(r/'owned').write_text('ok')
try: (p.parent/'forbidden').write_text('x')
except OSError: pass
else: raise AssertionError('proof directory writable')
assert not Path('/run/photo-wall-app-broker').exists()
assert not Path('/run/systemd/private').exists()
while True:
 try:
  s=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET);s.settimeout(2);s.connect(str(p));s.send(b'{}'); reply=s.recv(8192);s.close()
  assert json.loads(reply)['status']=='refused'
  Path('/tmp/ipc-result.json').write_text(json.dumps({'pid':os.getpid(),'inode':p.stat().st_ino,'xdg_uid':st.st_uid,'xdg_mode':stat.S_IMODE(st.st_mode)}))
 except OSError: pass
 time.sleep(.1)
""")
drop = Path("/etc/systemd/system/photo-wall-app-broker.service.d")
drop.mkdir(exist_ok=True)
(drop / "probe.conf").write_text(
    "[Unit]\nConditionKernelCommandLine=\nConditionPathExists=\n[Service]\nExecStart=\nExecStart=/usr/bin/python3 /var/lib/ipc-server.py\nRestart=no\n"
)
Path("/etc/photo-wall").mkdir(exist_ok=True)
Path("/etc/photo-wall/public.json").write_text("{}")
runtime = Path("/run/photo-wall-display")
runtime.mkdir(mode=0o700, exist_ok=True)
os.chown(runtime, 10005, 10005)
wayland = socket.socket(socket.AF_UNIX)
wayland.bind(str(runtime / "wayland-0"))
os.chown(runtime / "wayland-0", 10005, 10005)
os.chmod(runtime / "wayland-0", 0o660)
run("systemctl", "daemon-reload")
run("systemctl", "start", "photo-wall-app-broker.service")
serverpid = int(
    run("systemctl", "show", "photo-wall-app-broker.service", "-p", "MainPID", "--value")
)


def broker_capabilities():
    before = run(
        "systemctl", "show", "photo-wall-app-broker.service", "-p", "MainPID", "-p", "InvocationID"
    )
    birth = Path(f"/proc/{serverpid}/stat").read_text().rpartition(") ")[2].split()[19]
    status = dict(
        line.split(":", 1) for line in Path(f"/proc/{serverpid}/status").read_text().splitlines()
    )
    command = Path(f"/proc/{serverpid}/cmdline").read_bytes()
    after_birth = Path(f"/proc/{serverpid}/stat").read_text().rpartition(") ")[2].split()[19]
    after = run(
        "systemctl", "show", "photo-wall-app-broker.service", "-p", "MainPID", "-p", "InvocationID"
    )
    return (
        status["CapEff"].strip()
        if b"/var/lib/ipc-server.py" in command and before == after and birth == after_birth
        else None
    )


capabilities = wait_for(broker_capabilities)
assert int(capabilities, 16) == (1 | (1 << 2) | (1 << 19)), capabilities
print("PASS actual broker effective capabilities", capabilities, flush=True)
argv = [
    "systemd-run",
    "--quiet",
    "--collect",
    "--unit=photo-wall-ipc-client.service",
    "--service-type=exec",
]
for prop in app_unit_properties(root / "rootfs"):
    if prop.startswith("BindReadOnlyPaths="):
        prop += " /var/lib/ipc-client.py:/run/ipc-client.py"
    argv += ["--property", prop]
argv += ["--", "/usr/bin/python3", "-I", "-B", "/run/ipc-client.py"]
run(*argv)
clientpid = int(
    run("systemctl", "show", "photo-wall-ipc-client.service", "-p", "MainPID", "--value")
)
identity = run(
    "systemctl", "show", "photo-wall-ipc-client.service", "-p", "MainPID", "-p", "InvocationID"
)
result = Path(f"/proc/{clientpid}/root/tmp/ipc-result.json")
first = wait_for(lambda: json.loads(result.read_text()))
proof = Path("/run/photo-wall-app-proof/app-link.sock")
# Test all bounded socket initialization crash states, while one app stays alive.
for gid, mode in ((0, 0o700), (10004, 0o700), (10004, 0o660)):
    run("systemctl", "stop", "photo-wall-app-broker.service")
    proof_directory(proof.parent)
    remove_proof_socket(proof)
    partial = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    partial.bind(str(proof))
    os.chown(proof, 0, gid)
    os.chmod(proof, mode)
    run("systemctl", "start", "photo-wall-app-broker.service")
    fresh = wait_for(
        lambda: (v if (v := json.loads(result.read_text()))["inode"] != first["inode"] else None)
    )
    assert (
        fresh["pid"] == clientpid
        and run(
            "systemctl",
            "show",
            "photo-wall-ipc-client.service",
            "-p",
            "MainPID",
            "-p",
            "InvocationID",
        )
        == identity
    )
    first = fresh
    partial.close()
print(
    "PASS retained app identity reconnected through readonly proof directory after all socket initialization crash points",
    flush=True,
)
# Kernel file permissions deny the manager UID even before protocol admission.
code = "import os,socket;os.setgroups([]);os.setgid(10003);os.setuid(10003);s=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET);s.connect('/run/photo-wall-app-proof/app-link.sock')"
refused = subprocess.run(["/usr/bin/python3", "-c", code], capture_output=True)
assert refused.returncode != 0 and b"PermissionError" in refused.stderr
print(
    "PASS manager UID denied proof socket; app XDG0700 writable and proof directory readonly",
    flush=True,
)
run("systemctl", "stop", "photo-wall-ipc-client.service", "photo-wall-app-broker.service")
actual = inventory(root / "rootfs")
expected = json.loads((root / "environment.json").read_text())["files"]
print(
    "ROOT DIFF",
    json.dumps(
        {
            "added": sorted(set(actual) - set(expected)),
            "removed": sorted(set(expected) - set(actual)),
            "changed": [p for p in actual.keys() & expected.keys() if actual[p] != expected[p]],
        }
    ),
    flush=True,
)
verify_root(root, ref, **abi)
# PAM runs on a private pseudo-terminal. No physical VT or DRM device is opened.
wayland.close()
(runtime / "wayland-0").unlink()
master, slave = pty.openpty()
pam = Path("/var/lib/pam-probe.py")
pam.write_text(
    "import os,json,time\nfrom pathlib import Path\np=Path(os.environ['XDG_RUNTIME_DIR']);p.joinpath('pam-result.json').write_text(json.dumps(dict(os.environ)))\ntime.sleep(60)\n"
)
drop = Path("/etc/systemd/system/photo-wall-display.service.d")
drop.mkdir(exist_ok=True)
(drop / "probe.conf").write_text(
    "[Unit]\nConditionKernelCommandLine=\nConditionPathExists=\n[Service]\nExecStart=\nExecStart=/usr/bin/env XDG_RUNTIME_DIR=/run/photo-wall-display /usr/bin/python3 /var/lib/pam-probe.py\nTTYPath="
    + os.ttyname(slave)
    + "\nRestart=no\n"
)
run("systemctl", "daemon-reload")
run("systemctl", "start", "photo-wall-display.service")
env = wait_for(lambda: json.loads((runtime / "pam-result.json").read_text()))
assert env["XDG_RUNTIME_DIR"] == "/run/photo-wall-display" and env["LIBSEAT_BACKEND"] == "logind"
assert env["XDG_SESSION_TYPE"] == "wayland"
info = runtime.stat()
assert info.st_uid == 10005 and stat.S_IMODE(info.st_mode) == 0o700
print(
    "PASS actual PAM service fixed runtime environment and owner0700; pseudo-terminal only, DRM seat unqualified",
    flush=True,
)
run("systemctl", "stop", "photo-wall-display.service")
os.close(master)
os.close(slave)
