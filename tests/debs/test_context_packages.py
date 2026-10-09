"""The Node's Python context packages and python3-nats as the local repo holds them (decision 0019
P2), proved on the built artifact: apt installs every context package and python3-nats through
the local repo; each package holds its import roots under its one directory; and each package's
every module imports in an isolated interpreter whose path is exactly the package directories
scripts/import_check.py's runtime_directories names for it (its own, its photo-wall Depends' and
the targets of its exempt edges, recursively), so the declared Depends plus the exempt targets
suffice at run time. The installed nats is uv.lock's nats-py.

PHOTO_WALL_LOCAL_REPO names a debian-packaging/build-repo.sh output directory (node-components.yml's
`debs` job sets it). Everything runs in the build container build-repo.sh loaded,
photo-wall-debian-builder, which reads the repository's scripts/import_check.py, debian/control and
pyproject.toml from a read-only mount; Docker is required.
"""

import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BUILT = os.environ.get("PHOTO_WALL_LOCAL_REPO")
BUILDER_IMAGE = "photo-wall-debian-builder"
# The frozen contents (decision 0019 P2): each context package's import roots, under its directory.
CONTENTS = {
    "photo-wall-common": ("contracts", "nodeapi"),
    "photo-wall-uplink": ("uplink",),
    "photo-wall-node-kernel": ("appliance.kernel", "appliance.feed", "appliance.feed_socket"),
    "photo-wall-node-central-session": ("appliance.central_session",),
    "photo-wall-node-host": ("appliance.host",),
    "photo-wall-node-display": ("appliance.display_host",),
    "photo-wall-node-health": ("appliance.health",),
    "photo-wall-node-apps": ("appliance.apps", "appliance.app_launcher", "appliance.process_identity"),
    "photo-wall-node-boot": ("appliance.boot", "appliance.node_boot_handoff"),
    "photo-wall-node-manager": ("appliance.node",),
}

pytestmark = pytest.mark.skipif(not BUILT, reason="set PHOTO_WALL_LOCAL_REPO to a "
                                                  "debian-packaging/build-repo.sh output directory")

INSTALL = ("echo 'deb [trusted=yes] file:/repo ./' > /etc/apt/sources.list.d/local.list\n"
           "printf 'Package: *\\nPin: origin \"\"\\nPin-Priority: 1002\\n' "
           "> /etc/apt/preferences.d/local\n"
           "apt-get -qq update\n"
           "DEBIAN_FRONTEND=noninteractive apt-get -qq install -y --no-install-recommends "
           + " ".join(sorted(CONTENTS)) + " python3-nats >/dev/null\n"
           "dpkg --audit\n")

# Run by the build root's python3 with the repository on its path: the plan comes from the import
# check's own functions over the installed system (each package's tree is the root, /), and each
# package's modules are imported by a fresh `python3 -I -B` whose path starts with exactly its
# runtime directories; every module must come from one of them.
DRIVER = """
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, "/src")
from scripts import import_check

packages = json.loads(sys.argv[1])
staged = Path("/tmp/staged")
staged.mkdir()
for package in packages:
    (staged / package).symlink_to("/")
installed = import_check.installed_modules(staged, packages)
declared = import_check.declared(Path("/src/debian/control"))
exempt = import_check.exemptions(Path("/src/pyproject.toml"))
probe = '''
import importlib, json, sys
directories, modules = json.loads(sys.argv[1]), json.loads(sys.argv[2])
sys.path[:0] = directories
failed = []
for name in modules:
    try:
        module = importlib.import_module(name)
    except Exception as error:
        failed.append(f"{name}: {type(error).__name__}: {error}")
        continue
    origin = getattr(module, "__file__", None) or ""
    if not any(origin.startswith(directory + "/") for directory in directories):
        failed.append(f"{name}: imported from {origin or 'no file'}")
print(json.dumps(failed))
'''
report = {}
for package in packages:
    directories = [str(each) for each in import_check.runtime_directories(
        package, declared=declared, exempt=exempt, installed=installed)]
    modules = sorted(name for name, owner in installed.items() if owner == package)
    run = subprocess.run([sys.executable, "-I", "-B", "-c", probe, json.dumps(directories),
                          json.dumps(modules)], capture_output=True, text=True, cwd="/")
    failed = json.loads(run.stdout) if run.returncode == 0 else [run.stderr]
    report[package] = {"directories": directories, "modules": modules, "failed": failed}
import nats.aio.client
report["nats"] = nats.aio.client.__version__
print(json.dumps(report))
"""


@pytest.fixture(scope="module")
def report() -> dict:
    run = subprocess.run(
        ["docker", "run", "--rm", "--platform", "linux/arm64", "--volume", f"{BUILT}:/repo:ro",
         "--volume", f"{REPO}:/src:ro", BUILDER_IMAGE, "sh", "-ec",
         INSTALL + 'exec python3 -I -B -c "$0" "$1"', DRIVER, json.dumps(sorted(CONTENTS))],
        check=True, capture_output=True, text=True, timeout=300)
    return json.loads(run.stdout)


def test_each_context_package_holds_its_import_roots(report):
    for package, roots in CONTENTS.items():
        modules = report[package]["modules"]
        assert [root for root in roots if not any(
            name == root or name.startswith(root + ".") for name in modules)] == [], package


@pytest.mark.parametrize("package", sorted(CONTENTS))
def test_each_module_imports_from_the_runtime_directories_alone(report, package):
    assert report[package]["failed"] == [], report[package]["directories"]


def test_an_exempt_edge_puts_its_target_on_the_path_without_a_depends(report):
    """HostCore's retiring host_runner -> node.recovery: node-manager's directory is on the
    host's path although photo-wall-node-host does not Depend on it."""
    assert "/usr/lib/photo-wall/node-manager" in report["photo-wall-node-host"]["directories"]


def test_the_installed_nats_is_the_locked_nats_py(report):
    locked = next(entry for entry in tomllib.loads((REPO / "uv.lock").read_text())["package"]
                  if entry["name"] == "nats-py")
    assert report["nats"] == locked["version"]
