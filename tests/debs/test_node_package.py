"""photo-wall-node, the composition the base installs (decision 0019 P2), proved on the built
artifact: apt installs it from the local repo with every context, nats-server at the pin, systemd
and udev; each of the seven launchers' ENTRY imports in an isolated interpreter whose path starts
with exactly that launcher's installed PATH, every first-party module coming from those
directories; every installed unit's ExecStart names an installed launcher directory or binary; the
target is enabled; the system users and runtime directories are installed (and the users made);
the bus configuration and nats-server's licence are in place; and abi.json's base_abi, recomputed
here from the installed Depends and debian-packaging/image-format.env, is the one the package
ships. The node-pid1 legs are the end-to-end proof (the units under real systemd).

PHOTO_WALL_LOCAL_REPO names a debian-packaging/build-repo.sh output directory (node-components.yml's
`debs` job sets it). Everything runs in the build container build-repo.sh loaded,
photo-wall-debian-builder, which reads scripts/import_check.py from a read-only mount; Docker is
required.
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BUILT = os.environ.get("PHOTO_WALL_LOCAL_REPO")
BUILDER_IMAGE = "photo-wall-debian-builder"
NODE = "photo-wall-node"
# The frozen launchers (decision 0019 P2) and the module each runs.
LAUNCHERS = {
    "root-import": "appliance.apps.root_import",
    "node-bootstrap": "appliance.boot.node_bootstrap",
    "display-controller": "appliance.display_host.runner",
    "host-core": "appliance.host.host_runner",
    "app-broker": "appliance.apps.broker_runner",
    "manager-supervisor": "appliance.node.manager_launcher",
    "health-judge": "appliance.health.runner",
}

pytestmark = pytest.mark.skipif(not BUILT, reason="set PHOTO_WALL_LOCAL_REPO to a "
                                                  "debian-packaging/build-repo.sh output directory")

# The local repo preferred above the snapshot's 1001 (erratum E-0019-P1A-3), the slim image's
# documentation excludes lifted so the licence is on disk.
INSTALL = (f"echo 'deb [trusted=yes] file:/repo ./' > /etc/apt/sources.list.d/local.list\n"
           "printf 'Package: *\\nPin: origin \"\"\\nPin-Priority: 1002\\n' "
           "> /etc/apt/preferences.d/local\n"
           "rm -f /etc/dpkg/dpkg.cfg.d/docker\n"
           "apt-get -qq update\n"
           f"DEBIAN_FRONTEND=noninteractive apt-get -qq install -y --no-install-recommends {NODE}"
           " >/dev/null\n"
           "dpkg --audit\n")

DRIVER = """
import json, pwd, grp, shlex, subprocess, sys
from pathlib import Path
sys.path.insert(0, "/src")
from scripts import import_check

staged = Path("/tmp/staged")
staged.mkdir()
(staged / "photo-wall-node").symlink_to("/")
launchers = import_check.launchers(staged)
probe = '''
import importlib, json, sys
directories, entry = json.loads(sys.argv[1]), sys.argv[2]
sys.path[:0] = directories
try:
    importlib.import_module(entry)
except Exception as error:
    print(json.dumps([f"{entry}: {type(error).__name__}: {error}"]))
    raise SystemExit
outside = sorted(f"{name}: {module.__file__}" for name, module in list(sys.modules.items())
                 if name.partition(".")[0] in ("appliance", "contracts", "nodeapi", "uplink")
                 and getattr(module, "__file__", None)
                 and not any(module.__file__.startswith(d + "/") for d in directories))
print(json.dumps(outside))
'''
report = {"launchers": {}}
for name, (entry, path) in launchers.items():
    run = subprocess.run([sys.executable, "-I", "-B", "-c", probe, json.dumps(path), entry],
                         capture_output=True, text=True, cwd="/")
    report["launchers"][name] = {"entry": entry, "path": list(path),
                                 "failed": json.loads(run.stdout) if run.returncode == 0
                                 else [run.stderr]}
files = subprocess.run(["dpkg", "-L", "photo-wall-node"], capture_output=True, text=True,
                       check=True).stdout.split()
units = {}
for file in files:
    if file.startswith("/usr/lib/systemd/system/") and Path(file).is_file():
        units[Path(file).name] = [line.partition("=")[2] for line in Path(file).read_text().splitlines()
                                  if line.startswith("ExecStart=")]
report["units"] = units
paths = {}
for commands in units.values():
    for command in commands:
        for token in shlex.split(command):
            value = token.partition("=")[2] if token.startswith("--") else token
            if value.startswith("/usr/"):
                paths[value] = Path(value).is_file() or (Path(value) / "__main__.py").is_file()
report["paths"] = paths
wants = Path("/etc/systemd/system/multi-user.target.wants/photo-wall-node.target")
report["wants"] = str(wants.resolve()) if wants.is_symlink() else None
report["sysusers"] = Path("/usr/lib/sysusers.d/photo-wall-node.conf").read_text()
report["tmpfiles"] = Path("/usr/lib/tmpfiles.d/photo-wall-node.conf").read_text()
report["users"] = {name: pwd.getpwnam(name).pw_uid for name in
                   ("pw-manager", "pw-player", "pw-display", "pw-health", "pw-bus")}
report["feeds"] = [grp.getgrnam("pw-node-feeds").gr_gid, grp.getgrnam("pw-node-feeds").gr_mem]
report["abi"] = json.loads(Path("/usr/lib/photo-wall/node/abi.json").read_text())
report["depends"] = subprocess.run(["dpkg-query", "-W", "-f", "${Depends}", "photo-wall-node"],
                                   capture_output=True, text=True, check=True).stdout
report["bus_conf"] = Path("/usr/lib/photo-wall/node/bus/node-bus.conf").read_text()
report["license"] = Path("/usr/share/doc/photo-wall-node/nats-server.LICENSE").read_text()
report["nats_server"] = subprocess.run(["/usr/bin/nats-server", "--version"],
                                       capture_output=True, text=True, check=True).stdout
print(json.dumps(report))
"""


@pytest.fixture(scope="module")
def report() -> dict:
    run = subprocess.run(
        ["docker", "run", "--rm", "--platform", "linux/arm64",
         "--volume", f"{BUILT}:/repo:ro", "--volume", f"{REPO}:/src:ro", BUILDER_IMAGE, "sh",
         "-ec", INSTALL + 'exec python3 -I -B -c "$0"', DRIVER],
        check=True, capture_output=True, text=True, timeout=300)
    return json.loads(run.stdout)


def test_the_seven_launchers_run_their_frozen_entries(report):
    assert {name: each["entry"] for name, each in report["launchers"].items()} == LAUNCHERS


@pytest.mark.parametrize("launcher", sorted(LAUNCHERS))
def test_each_entry_imports_from_its_launchers_path_alone(report, launcher):
    assert report["launchers"][launcher]["failed"] == [], report["launchers"][launcher]["path"]


def test_an_exempt_edge_puts_its_target_on_the_launchers_path(report):
    """HostCore's retiring host_runner -> node.recovery reaches node-manager's directory."""
    assert "/usr/lib/photo-wall/node-manager" in report["launchers"]["host-core"]["path"]


def test_every_unit_starts_an_installed_launcher_or_binary(report):
    assert len(report["units"]) == 16 and "photo-wall-node.target" in report["units"]
    assert {path for path, present in report["paths"].items() if not present} == set()
    launched = {path.removeprefix("/usr/lib/photo-wall/node/") for path in report["paths"]
                if path.startswith("/usr/lib/photo-wall/node/")}
    assert launched == set(LAUNCHERS) - {"root-import"} | {"bus/node-bus.conf"}
    assert "/usr/bin/nats-server" in report["paths"]


def test_the_target_is_enabled_for_multi_user(report):
    assert report["wants"] == "/usr/lib/systemd/system/photo-wall-node.target"


def test_the_system_users_and_runtime_directories_are_installed(report):
    assert report["sysusers"] == (REPO / "debian/photo-wall-node.sysusers").read_text()
    assert report["tmpfiles"] == (REPO / "debian/photo-wall-node.tmpfiles").read_text()
    assert report["users"] == {"pw-manager": 10003, "pw-player": 10004, "pw-display": 10005,
                               "pw-health": 10006, "pw-bus": 10008}
    assert report["feeds"] == [10007, ["pw-health"]]


def test_the_bus_runs_upstreams_server_at_the_pin_with_the_shipped_configuration(report):
    pin = dict(line.split("=", 1) for line in
               (REPO / "debian-packaging/nats-server.env").read_text().splitlines()
               if line and not line.startswith("#"))
    assert f"v{pin['NATS_SERVER_VERSION']}" in report["nats_server"]
    assert f"nats-server (= {pin['NATS_SERVER_VERSION']})" in report["depends"]
    assert report["bus_conf"] == (REPO / "appliance/bus/node-bus.conf").read_text()
    assert report["license"] == (REPO / "debian-packaging/nats-server.LICENSE").read_text()


def test_the_base_abi_is_the_installed_depends_and_the_image_format(report):
    format_lines = [line for line in
                    (REPO / "debian-packaging/image-format.env").read_text().splitlines()
                    if line.strip() and not line.strip().startswith("#")]
    identity = hashlib.sha256(("Depends: " + report["depends"] + "\n"
                               + "".join(line + "\n" for line in format_lines)).encode())
    assert report["abi"] == {"base_abi": "node-v2-" + identity.hexdigest()}
    for sibling in ("photo-wall-common", "photo-wall-node-display", "photo-wall-node-manager"):
        assert f"{sibling} (= 0+" in report["depends"]
