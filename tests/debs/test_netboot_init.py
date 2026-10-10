"""photo-wall-netboot-init as the local repo holds it (decision 0019 P4), proved on the built
artifact: apt installs it through the local repo; its initramfs-tools hook, run through
initramfs-tools' own hook-functions with DESTDIR a scratch directory and MODULESDIR a fake
kernel module tree (no kernel needed: the modules it asks for are only listed), asks for every
module in that tree (the whole tree, issue 64), copies stage 1's package directories into the
interpreter's stdlib directory and the root's CA bundle beside them; `python3 -I -S` imports
appliance.netboot_init with only that directory added to its path; and stage 1's uplink runs on
the device's runtime against that tree (scripts/uplink_device_harness.py, decision 0014 §11). The
installed path file without common's directory gives a tree stage 1 cannot import from.

PHOTO_WALL_LOCAL_REPO names a debian-packaging/build-repo.sh output directory (node-components.yml's
`debs` job sets it). Everything runs in the build container build-repo.sh loaded,
photo-wall-debian-builder (trixie's python3 and OpenSSL, the Pi's); Docker is required.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BUILT = os.environ.get("PHOTO_WALL_LOCAL_REPO")
BUILDER_IMAGE = "photo-wall-debian-builder"
LIBDIR = "usr/lib/python3.13"

pytestmark = pytest.mark.skipif(not BUILT, reason="set PHOTO_WALL_LOCAL_REPO to a "
                                                  "debian-packaging/build-repo.sh output directory")

# In the container: install through the local repo, run the hook as mkinitramfs would (the
# environment initramfs-tools(7) gives a hook: DESTDIR, MODULESDIR, version, verbose, and its module
# list file), then report on the tree. A second run, from a path file without
# common's directory, is the negative case.
SCRIPT = r"""
set -eu
echo 'deb [trusted=yes] file:/repo ./' > /etc/apt/sources.list.d/local.list
printf 'Package: *\nPin: origin ""\nPin-Priority: 1002\n' > /etc/apt/preferences.d/local
apt-get -qq update 2>/dev/null
DEBIAN_FRONTEND=noninteractive apt-get -qq install -y --no-install-recommends \
    photo-wall-netboot-init >/dev/null
hook=/usr/share/initramfs-tools/hooks/photo-wall-netboot
path_file=/usr/lib/photo-wall/netboot-init/path
# A fake kernel's module tree: the hook must ask for every module in it, nested or not,
# compressed or not.
version=6.18.0-fake
modules=/tmp/lib/modules/$version
for module in kernel/fs/squashfs/squashfs.ko.xz kernel/drivers/gpu/drm/vc4/vc4.ko.xz \
        kernel/drivers/media/platform/raspberrypi/hevc_dec/rpi-hevc-dec.ko.xz \
        kernel/drivers/hwmon/raspberrypi-hwmon.ko; do
    mkdir -p "$modules/$(dirname "$module")"
    : > "$modules/$module"
done
run_hook() {
    DESTDIR=$1 MODULESDIR=$modules version=$version verbose=n __MODULES_TO_ADD=$1.modules \
        sh "$hook" >/dev/null
}
mkdir /tmp/initrd /tmp/partial
: > /tmp/initrd.modules
run_hook /tmp/initrd
grep -v '^/usr/lib/photo-wall/common$' "$path_file" > /tmp/path && cp /tmp/path "$path_file"
: > /tmp/partial.modules
run_hook /tmp/partial
cd /
tree=/tmp/initrd/usr/lib/python3.13
probe='import sys; sys.path.insert(0, sys.argv[1]); import appliance.netboot_init'
imported=0; python3 -I -S -c "$probe" "$tree" 2>/tmp/import.err || imported=$?
partial=0; python3 -I -S -c "$probe" /tmp/partial/usr/lib/python3.13 2>/tmp/partial.err || partial=$?
harness=0; python3 -I -S /src/scripts/uplink_device_harness.py run --tree "$tree" --certs /certs > /tmp/harness.out 2>&1 \
    || harness=$?
exec python3 -I -c '
import filecmp, json, os, sys
root = "/tmp/initrd"
files = sorted(os.path.relpath(os.path.join(directory, name), root)
               for directory, _, names in os.walk(root) for name in names)
print(json.dumps({
    "files": files,
    "ca_equal": filecmp.cmp(root + "/etc/ssl/certs/ca-certificates.crt",
                            "/etc/ssl/certs/ca-certificates.crt", shallow=False),
    "modules": open("/tmp/initrd.modules").read().split(),
    "import": [int(sys.argv[1]), open("/tmp/import.err").read()],
    "partial": [int(sys.argv[2]), open("/tmp/partial.err").read()],
    "harness": [int(sys.argv[3]), open("/tmp/harness.out").read()],
}))' "$imported" "$partial" "$harness"
"""


@pytest.fixture(scope="module")
def report(tmp_path_factory) -> dict:
    certs = tmp_path_factory.mktemp("certs")
    subprocess.run([sys.executable, "-m", "scripts.uplink_device_harness", "mint", str(certs)],
                   cwd=REPO, check=True, timeout=60)
    run = subprocess.run(
        ["docker", "run", "--rm", "--platform", "linux/arm64", "--network", "none",
         "--volume", f"{BUILT}:/repo:ro", "--volume", f"{certs}:/certs:ro",
         "--volume", f"{REPO / 'scripts'}:/src/scripts:ro",
         BUILDER_IMAGE, "sh", "-c", SCRIPT],
        capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_the_hook_copies_stage_1s_directories_into_the_stdlib_directory(report):
    files = set(report["files"])
    for path in ("appliance/netboot_init.py", "appliance/bootstrap.py",
                 "appliance/central_post.py", "appliance/node_boot_handoff.py",
                 "uplink/__init__.py", "uplink/locate.py", "contracts/__init__.py",
                 "contracts/node_boot.py"):
        assert f"{LIBDIR}/{path}" in files, path
    # Import roots only: the path file is no module, and `appliance` stays a PEP 420 portion.
    assert f"{LIBDIR}/path" not in files
    assert f"{LIBDIR}/appliance/__init__.py" not in files


def test_the_hook_copies_the_roots_ca_bundle(report):
    assert "etc/ssl/certs/ca-certificates.crt" in report["files"]
    assert report["ca_equal"]


def test_the_hook_asks_for_the_kernels_whole_module_tree(report):
    """Every module in MODULESDIR's kernel/ tree, none named by hand (issue 64)."""
    assert sorted(report["modules"]) == ["raspberrypi-hwmon", "rpi-hevc-dec", "squashfs", "vc4"]


def test_stage_1_imports_from_the_hooks_tree_alone(report):
    assert report["import"] == [0, ""]


def test_a_path_file_without_commons_directory_gives_a_tree_stage_1_cannot_import(report):
    status, error = report["partial"]
    assert status != 0 and "No module named 'contracts'" in error, error


def test_uplink_runs_on_the_device_runtime_against_the_hooks_tree(report):
    status, output = report["harness"]
    assert status == 0, output
    assert "isolated=1 no_site=1" in output and "OpenSSL" in output
    assert output.count("\nOK ") == 6, output
