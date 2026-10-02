"""Disposable PID1 cold/service composition. Hardware enumeration is explicitly synthetic."""

import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, "/usr/lib/photo-wall-node-bootstrap")
from appliance.node.clock import boot_id, boottime_ms
from appliance.node_boot_handoff import write_node_handoff
from contracts.node_boot import parse_node_boot_offer

config = json.loads(Path("/var/lib/node-fixture-config.json").read_text())
request = urllib.request.Request(
    config["origin"] + "/fixture/node-ready",
    data=json.dumps({"boot_id": str(boot_id()), "boottime_ms": boottime_ms()}).encode(),
    headers={"Content-Type": "application/json", "X-Fixture-Token": config["token"]},
    method="POST",
)
with urllib.request.urlopen(request, timeout=15) as response:
    ready = json.load(response)
write_node_handoff(
    Path("/"),
    central=ready["central"],
    offer=parse_node_boot_offer(json.dumps(ready["offer"]).encode()),
)
# Container has no real boot command line. Only the node cohort condition is removed;
# every production service command, sandbox and capability remains otherwise exact.
units = [
    "photo-wall-node.target",
    "photo-wall-node-storage.service",
    "photo-wall-node-handoff.service",
    "photo-wall-node-prepare.service",
    "photo-wall-display.service",
    "photo-wall-display-controller.service",
    "photo-wall-host-core.service",
    "photo-wall-app-broker.service",
    "photo-wall-manager-supervisor.service",
]
for unit in units:
    d = Path("/etc/systemd/system") / (unit + ".d")
    d.mkdir(parents=True, exist_ok=True)
    (d / "fixture-cohort.conf").write_text("[Unit]\nConditionKernelCommandLine=\n")
shutil.copy2("/var/tmp/fixture-head.so", "/usr/lib/photo-wall-fixture-head.so")
d = Path("/etc/systemd/system/photo-wall-display.service.d")
(d / "fixture-headless.conf").write_text("""[Service]
TTYPath=
StandardInput=null
UtmpIdentifier=
TTYReset=no
TTYVHangup=no
TTYVTDisallocate=no
ExecStart=
ExecStart=/usr/bin/env XDG_RUNTIME_DIR=/run/photo-wall-display /usr/bin/weston --backend=headless --no-outputs --renderer=pixman --shell=/usr/lib/photo-wall-display/photo-wall-shell.so --modules=/usr/lib/photo-wall-fixture-head.so --socket=wayland-0 --idle-time=0 --width=640 --height=480 --no-config
""")
# The real Player's normal DRM discovery reads synthetic hardware metadata. The
# compositor's matching Virtual-1 head is real headless Weston, never a DRM claim.
shutil.copyfile("/proc/cpuinfo", "/var/lib/node-fixture-cpuinfo")
d = Path("/etc/systemd/system/photo-wall-node-player.service.d")
d.mkdir(parents=True, exist_ok=True)
(d / "fixture-hardware.conf").write_text(
    "[Service]\nBindReadOnlyPaths=/var/lib/node-fixture-drm:/sys/class/drm /var/lib/node-fixture-cpuinfo:/proc/cpuinfo\n"
)
if config.get("stop_diagnostics"):
    diagnostic = Path("/etc/systemd/system/photo-wall-app-broker.service.d/fixture-diagnostic.conf")
    diagnostic.write_text(
        "[Service]\nExecStart=\nExecStart=/usr/bin/python3 -I -B /usr/lib/photo-wall-stop-diagnostic.py\n"
    )
subprocess.run(["systemctl", "daemon-reload"], check=True)
subprocess.run(
    ["systemctl", "start", "photo-wall-node-handoff.service", "photo-wall-node-storage.service"],
    check=True,
)
subprocess.run(["systemctl", "start", "photo-wall-node-prepare.service"], check=True, timeout=1200)
subprocess.run(["systemctl", "start", "photo-wall-display.service"], check=True)
for _ in range(100):
    if Path("/run/photo-wall-display/wayland-0").exists():
        break
    time.sleep(0.1)
else:
    raise AssertionError("fixture compositor socket missing")
subprocess.run(
    [
        "systemctl",
        "start",
        "photo-wall-display-controller.service",
        "photo-wall-host-core.service",
        "photo-wall-manager-supervisor.service",
        "photo-wall-app-broker.service",
    ],
    check=True,
)
print("REAL COLD SERVICES STARTED", flush=True)
