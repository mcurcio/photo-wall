"""Disposable PID1 cold/service composition. Hardware enumeration is explicitly synthetic.

Argument `refused` (the node-pid1 `refused` scenario): the storage stage alone reads a fake
/proc/meminfo below the smallest memory class, and the base units start as one PID1
transaction, so the units' own Requires= decide what runs after the refusal; the display stack,
which needs no storage, then starts as in every scenario.
"""

import ast
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path[:0] = next(ast.literal_eval(node.value) for node in ast.parse(Path(
    "/usr/lib/photo-wall/node/node-bootstrap/__main__.py").read_text()).body
    if isinstance(node, ast.AnnAssign) and node.target.id == "PATH")  # its launcher's PATH
from appliance.kernel.clock import boot_id, boottime_ms  # noqa: E402
from appliance.node_boot_handoff import write_node_handoff  # noqa: E402
from contracts.node_boot import parse_node_boot_offer  # noqa: E402

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
shutil.copy2("/var/tmp/fixture-head.so", "/usr/lib/photo-wall-fixture-head.so")
# Each drop-in directory is made where it is written: nothing else in the image makes them.
d = Path("/etc/systemd/system/photo-wall-display.service.d")
d.mkdir(parents=True, exist_ok=True)
# The installed unit's own command with only the hardware swapped: headless instead of DRM, and
# the fixture head loaded before the production modules, so READY=1 (systemd-notify.so, Type=)
# and every other argument stay the package's.
(execstart,) = [
    line.split("=", 1)[1]
    for line in Path("/lib/systemd/system/photo-wall-display.service").read_text().splitlines()
    if line.startswith("ExecStart=")
]
for production, fixture in (
    ("--backend=drm", "--backend=headless --no-outputs --renderer=pixman"),
    ("--modules=", "--modules=/usr/lib/photo-wall-fixture-head.so,"),
    ("--idle-time=0", "--idle-time=0 --width=640 --height=480 --no-config"),
):
    if execstart.count(production) != 1:
        raise AssertionError("display ExecStart lacks " + production + ": " + execstart)
    execstart = execstart.replace(production, fixture)
(d / "fixture-headless.conf").write_text(f"""[Service]
TTYPath=
StandardInput=null
UtmpIdentifier=
TTYReset=no
TTYVHangup=no
TTYVTDisallocate=no
ExecStart=
ExecStart={execstart}
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
    diagnostic.parent.mkdir(parents=True, exist_ok=True)
    diagnostic.write_text(
        "[Service]\nExecStart=\nExecStart=/usr/bin/python3 -I -B /usr/lib/photo-wall-stop-diagnostic.py\n"
    )
scenario = sys.argv[1] if len(sys.argv) > 1 else None
if scenario == "refused":
    # A 2 GiB board, seen by the storage stage only. BindReadOnlyPaths gives the unit its own
    # mount namespace, so this fake is only valid while nothing it mounts must reach the host:
    # here the stage refuses before it mounts.
    Path("/var/lib/node-fixture-meminfo").write_text(
        "MemTotal:        2097152 kB\nMemFree:         1048576 kB\nMemAvailable:    1572864 kB\n"
    )
    d = Path("/etc/systemd/system/photo-wall-node-storage.service.d")
    d.mkdir(parents=True, exist_ok=True)
    (d / "fixture-memory-class.conf").write_text(
        "[Service]\nBindReadOnlyPaths=/var/lib/node-fixture-meminfo:/proc/meminfo\n"
    )
elif scenario is not None:
    raise SystemExit("unknown fixture scenario: " + scenario)
subprocess.run(["systemctl", "daemon-reload"], check=True)
if scenario == "refused":
    # Handoff runs independently of storage and writes host.json, which Host Management needs.
    subprocess.run(["systemctl", "start", "photo-wall-node-handoff.service"], check=True)
    # One transaction, as photo-wall-node.target pulls them in: storage refuses, so prepare
    # (Requires= storage) and the broker and manager supervisor (Requires= prepare) fail as
    # dependencies without running; Host Management (After= handoff only) runs and reports.
    started = subprocess.run(
        [
            "systemctl",
            "start",
            "photo-wall-node-storage.service",
            "photo-wall-node-prepare.service",
            "photo-wall-host-core.service",
            "photo-wall-app-broker.service",
            "photo-wall-manager-supervisor.service",
        ],
        timeout=120,
    )
    if started.returncode == 0:
        raise AssertionError("refused storage did not fail the base transaction")
    print("REFUSED BASE TRANSACTION FAILED", started.returncode, flush=True)
    # The display stack is independent of storage: the controller alone, as the target pulls it.
    subprocess.run(["systemctl", "start", "photo-wall-display-controller.service"], check=True)
    raise SystemExit(0)
subprocess.run(
    ["systemctl", "start", "photo-wall-node-handoff.service", "photo-wall-node-storage.service"],
    check=True,
)
subprocess.run(["systemctl", "start", "photo-wall-node-prepare.service"], check=True, timeout=1200)
# The controller alone: its BindsTo= pulls Weston in and After= holds it until READY=1, which
# tests/test_node_pid1.py's verify_display_ready checks.
subprocess.run(["systemctl", "start", "photo-wall-display-controller.service"], check=True)
subprocess.run(
    [
        "systemctl",
        "start",
        "photo-wall-display-controller.service",
        "photo-wall-host-core.service",
        "photo-wall-manager-supervisor.service",
        "photo-wall-app-broker.service",
        "photo-wall-health.service",
        "photo-wall-bus.service",
    ],
    check=True,
)
print("REAL COLD SERVICES STARTED", flush=True)
