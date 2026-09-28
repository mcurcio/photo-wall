#!/usr/bin/env python3
"""Start the Player unit on the BUILT base under real systemd, as provisioning does (CI guard).

v0.9.1's base had no udev, so no `render` group: player.service (`SupplementaryGroups=render
video`) failed at spawn, 216/GROUP, on every start, and the Pi reboot-looped. Every check passed,
because none ran systemd. This does, with no fake of the base or the package:

  1. import the built base squashfs as a container image (unsquashfs, tar, docker import);
  2. boot it with systemd as PID 1, the provisioner masked (this probe plays its part);
  3. require udev installed and the render, video and input groups;
  4. install the real Player .deb with `dpkg --install` alone, as provisioning does, and require
     `wall` in each of those groups;
  5. write provisioning's own handoff (appliance.provision.write_handoff) naming a Central that
     never resolves, and run `systemctl start photo-wall-player.service`;
  6. pass only if that start returns 0 and the unit is active/running;
  7. with --initrd: stage 1's module hand-over (appliance.netboot_init.hand_over_modules) from
     the BUILT initrd into the container, then the BASE's own libkmod -- the library its udev
     loads drivers through -- must find each display driver by its device's alias, list its
     dependencies, and read (decompress) every one of those .ko.xz files.

Step 7 loads nothing: the runner boots its own kernel. It proves the base can resolve and read
the kernel's modules, which the base's udev needs if it loads one; kmod's `modprobe` is not in
the base and is not needed for that.

The pass condition is provisioning's own: its `systemctl start` returns 0 only once the Player
has sent READY=1 (Type=notify), which the Player does after its own start-up code has run. That
is strictly more than "not 216/217/203": any spawn failure (systemd's own statuses, named from
appliance.provision.SYSTEMD_EXIT_STATUSES) and any failure of the Player's start-up both fail it.
With no display the Player starts with its renderer unavailable, by design (a native failure
must still register), so this proves the unit STARTS, not that it renders. It does not prove
device-node permissions either: a container has no /dev/dri.

Two things are given to the container that a Pi has and a CI runner does not. It is
--privileged, because the unit's sandboxing (ProtectSystem=strict, PrivateTmp, ...) needs mount
namespaces that docker's default profile refuses (226/NAMESPACE: the container, not the base).
And it gets an equipment identity: a Pi serial as the `Serial` line of a bind-mounted
/proc/cpuinfo, the fallback player.service.read_pi_serial reads when there is no devicetree.

A privileged container sees the runner's own /sys and may load modules into the runner's kernel,
so the two boot units that act on the host's devices are masked in it: systemd-udev-trigger
(writes `add` into every device's uevent file under /sys, replaying every host device to the
runner's udev) and systemd-modules-load (loads modules into the running kernel). udevd itself
still runs, in the container's own network namespace, where no kernel uevent reaches it. /sys
is not made read-only instead: systemd as PID 1 needs a writable cgroup tree under it.

Needs root (unsquashfs keeps ownership) and docker on a host that runs the base's binaries
(base-image.yml: an arm64 runner). `--image` probes an already imported image instead.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from appliance.netboot_init import hand_over_modules  # noqa: E402
from appliance.provision import (  # noqa: E402
    DEFAULT_UNIT,
    START_UNIT_SECONDS,
    SYSTEMD_EXIT_STATUSES,
    UNIT_PROPERTIES,
    Handoff,
    parse_unit_properties,
    unit_ending,
    write_handoff,
)
from scripts.initrd_mount_probe import INITRD_MODULES, kernel_release, unpack  # noqa: E402
from scripts.verify_netboot_initrd import DISPLAY_MODULES  # noqa: E402
from uplink.origin import Origin  # noqa: E402

DEVICE_GROUPS: Final = ("render", "video", "input")
PLAYER_USER: Final = "wall"
PROVISION_UNIT: Final = "photo-wall-provision.service"
# Masked in the container: they act on the runner's devices and kernel (module docstring).
HOST_ACTING_UNITS: Final = ("systemd-udev-trigger.service", "systemd-modules-load.service")
# The OF modalias of the device each display driver binds, as the kernel reports it (node name,
# no device_type, compatible) for the Pi 5 DTB with vc4-kms-v3d-pi5 applied: /axi/gpu and
# /axi/v3d@2000000. What udev hands libkmod at coldplug.
DISPLAY_ALIASES: Final = {"vc4": "of:NgpuT<NULL>Cbrcm,bcm2712-vc6",
                          "v3d": "of:Nv3dT<NULL>Cbrcm,2712-v3d"}
# Run by the BASE's python3 inside the container, against the base's libkmod.so.2 (ctypes: the
# base has no kmod tools). argv: the kernel release, then module=alias pairs. One `kmod` line per
# module (kmod_violations).
KMOD_PROGRAM: Final = """\
import ctypes
import sys
from ctypes import POINTER, byref, c_char_p, c_int, c_void_p
try:
    kmod = ctypes.CDLL("libkmod.so.2")
except OSError as error:
    print(f"nokmod {error}")
    sys.exit(0)
for name, restype, argtypes in (
        ("kmod_new", c_void_p, [c_char_p, c_void_p]),
        ("kmod_module_new_from_lookup", c_int, [c_void_p, c_char_p, POINTER(c_void_p)]),
        ("kmod_list_next", c_void_p, [c_void_p, c_void_p]),
        ("kmod_module_get_module", c_void_p, [c_void_p]),
        ("kmod_module_get_name", c_char_p, [c_void_p]),
        ("kmod_module_get_path", c_char_p, [c_void_p]),
        ("kmod_module_get_dependencies", c_void_p, [c_void_p]),
        ("kmod_module_get_info", c_int, [c_void_p, POINTER(c_void_p)])):
    function = getattr(kmod, name)
    function.restype, function.argtypes = restype, argtypes
release = sys.argv[1]
context = kmod.kmod_new(f"/usr/lib/modules/{release}".encode(), None)

def members(head):
    entry = head
    while entry:
        yield kmod.kmod_module_get_module(entry)
        entry = kmod.kmod_list_next(head, entry)

for pair in sys.argv[2:]:
    module, alias = pair.split("=", 1)
    found = c_void_p()
    code = kmod.kmod_module_new_from_lookup(context, alias.encode(), byref(found))
    matches = {kmod.kmod_module_get_name(m).decode(): m for m in members(found.value)}
    target = matches.get(module)
    dependencies = list(members(kmod.kmod_module_get_dependencies(target))) if target else []
    unreadable = 0
    for each in ([target] if target else []) + dependencies:
        info = c_void_p()
        if kmod.kmod_module_get_info(each, byref(info)) < 0:
            unreadable += 1
    path = (kmod.kmod_module_get_path(target) or b"none").decode() if target else "none"
    print(f"kmod {module} code={code} found={','.join(sorted(matches)) or 'none'} "
          f"dependencies={len(dependencies)} unreadable={unreadable} path={path}")
"""
# RFC 6761: .invalid never resolves, so the started Player only ever retries its first request.
PROBE_CENTRAL: Final = "http://central.invalid"
PROBE_SERIAL: Final = "10000000c0ffee00"
# Not /tmp: systemd mounts a tmpfs over it at boot, hiding anything docker cp put there.
DEB_IN_CONTAINER: Final = "/var/tmp/photo-wall-player.deb"
HANDOFF_IN_CONTAINER: Final = "/etc/photo-wall/public.json"
SYSTEMD: Final = "/usr/lib/systemd/systemd"
BOOT_SECONDS: Final = 180.0
# A booted system: `degraded` too, since some units (systemd-modules-load: no modules for the
# host's kernel) cannot work in a container, and none of them is the Player's concern.
BOOTED: Final = frozenset({"running", "degraded"})
EXEC_SECONDS: Final = 120.0
START_SECONDS: Final = START_UNIT_SECONDS + 30.0
JOURNAL_LINES: Final = 40

Run = Callable[..., subprocess.CompletedProcess]


def cpuinfo_text(serial: str = PROBE_SERIAL) -> str:
    """PURE. A /proc/cpuinfo carrying only the `Serial` line the Player's identity reads."""
    return f"processor\t: 0\nSerial\t\t: {serial}\n"


def docker_run_argv(image: str, name: str, cpuinfo: Path) -> list[str]:
    """PURE. Boot `image` with systemd as PID 1. Arguments after SYSTEMD are its command line in
    a container: systemd.mask= keeps the provisioner (this probe plays its part) and the units
    that act on the runner's devices and kernel (HOST_ACTING_UNITS) from running."""
    return ["docker", "run", "--detach", "--name", name, "--privileged",
            "--env", "container=docker", "--tmpfs", "/run", "--tmpfs", "/run/lock",
            "--volume", f"{cpuinfo}:/proc/cpuinfo:ro",
            image, SYSTEMD, *(f"systemd.mask={unit}"
                              for unit in (PROVISION_UNIT, *HOST_ACTING_UNITS))]


def kmod_violations(output: str, modules: Sequence[str] = DISPLAY_MODULES) -> list[str]:
    """PURE. KMOD_PROGRAM's output -> the display drivers the base's libkmod cannot find by
    their device's alias, resolve or read."""
    if output.startswith("nokmod"):
        return [f"the base has no libkmod, so its udev can load no module ({output.strip()})"]
    results = {fields[1]: dict(field.split("=", 1) for field in fields[2:] if "=" in field)
               for fields in (line.split() for line in output.splitlines())
               if len(fields) > 1 and fields[0] == "kmod"}
    violations = []
    for module in modules:
        result = results.get(module)
        if result is None:
            violations.append(f"{module}: the base's libkmod gave no result")
        elif (result.get("code") != "0" or module not in result.get("found", "").split(",")
              or result.get("dependencies", "0") == "0" or result.get("unreadable") != "0"):
            violations.append(f"{module}: the base's libkmod cannot load it by its alias "
                              + " ".join(f"{key}={value}" for key, value in result.items()))
    return violations


def group_violations(getent: str) -> list[str]:
    """PURE. `getent group render video input` output -> the device groups it lacks."""
    present = {line.split(":", 1)[0] for line in getent.splitlines() if ":" in line}
    return [f"group {group} does not exist (udev creates it)"
            for group in DEVICE_GROUPS if group not in present]


def membership_violations(id_groups: str) -> list[str]:
    """PURE. `id -nG wall` output -> the device groups the Player's user is not in."""
    present = set(id_groups.split())
    return [f"{PLAYER_USER} is not in group {group}"
            for group in DEVICE_GROUPS if group not in present]


def udev_violations(status: str) -> list[str]:
    """PURE. `dpkg-query -W -f='${Status}' udev` output -> [] only when udev is installed."""
    if status.strip() == "install ok installed":
        return []
    return [f"udev is not installed (dpkg status: {status.strip() or 'none'})"]


def start_violations(returncode: int | None, properties: Mapping[str, str]) -> list[str]:
    """PURE. `systemctl start` returned `returncode` (None: it outlived its bound); `properties`
    is the unit's `systemctl show` afterwards. [] only when the start returned 0 and the unit is
    active/running. A spawn failure (systemd's own status) is named as one."""
    state = (properties.get("ActiveState"), properties.get("SubState"))
    if returncode == 0 and state == ("active", "running"):
        return []
    ending = unit_ending(properties)
    status = properties.get("ExecMainStatus", "")
    if (properties.get("ExecMainCode") == "1" and status.isdigit()
            and int(status) in SYSTEMD_EXIT_STATUSES):
        return [f"{DEFAULT_UNIT} failed at spawn, before the Player ran: {ending} "
                f"(result {properties.get('Result', 'unknown')})"]
    started = "timed out" if returncode is None else f"returned {returncode}"
    return [f"{DEFAULT_UNIT} did not start: systemctl start {started}; unit "
            f"{state[0]}/{state[1]}, result {properties.get('Result', 'unknown')}, {ending}"]


def import_squashfs(squashfs: Path, tag: str, work: Path, *, run: Run = subprocess.run) -> None:
    """The squashfs's tree as image `tag`: unsquashfs as root (ownership kept), a numeric-owner
    tar, docker import. The extracted tree and the tar are removed afterwards."""
    root, archive = work / "rootfs", work / "rootfs.tar"
    try:
        run(["unsquashfs", "-no-progress", "-no-xattrs", "-d", str(root), str(squashfs)],
            check=True, capture_output=True)
        run(["tar", "--numeric-owner", "-C", str(root), "-cf", str(archive), "."],
            check=True, capture_output=True)
        run(["docker", "import", str(archive), tag], check=True, capture_output=True)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        archive.unlink(missing_ok=True)


class Container:
    """One booted container; every call a `docker` command with text output."""

    def __init__(self, name: str, *, run: Run) -> None:
        self.name, self._run = name, run

    def exec(self, *argv: str, timeout: float = EXEC_SECONDS,
             env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess:
        options = [item for key, value in (env or {}).items()
                   for item in ("--env", f"{key}={value}")]
        return self._run(["docker", "exec", *options, self.name, *argv], check=False,
                         capture_output=True, text=True, timeout=timeout)

    def copy_in(self, source: Path, target: str) -> None:
        self._run(["docker", "cp", str(source), f"{self.name}:{target}"], check=True,
                  capture_output=True)

    def wait_booted(self, *, seconds: float = BOOT_SECONDS,
                    sleep: Callable[[float], None] = time.sleep,
                    clock: Callable[[], float] = time.monotonic) -> str:
        """systemd's own view once boot is over (`systemctl is-system-running --wait`), polled
        until its bus answers; the last answer when `seconds` pass first."""
        deadline, state = clock() + seconds, ""
        while True:
            try:
                state = self.exec("systemctl", "is-system-running", "--wait",
                                  timeout=max(1.0, deadline - clock())).stdout.strip()
            except subprocess.TimeoutExpired:
                return state
            if state in BOOTED or clock() >= deadline:
                return state
            sleep(1.0)


def check_base(container: Container) -> list[str]:
    """Step 3: what the base itself must carry for the Player to start and open its devices."""
    violations = udev_violations(container.exec(
        "dpkg-query", "-W", "-f=${Status}", "udev").stdout)
    violations += group_violations(container.exec("getent", "group", *DEVICE_GROUPS).stdout)
    return violations


def install_player(container: Container, deb: Path) -> tuple[bool, list[str]]:
    """Step 4: `dpkg --install` alone, as appliance.provision.install_package runs it. Whether
    it installed, and the violations."""
    container.copy_in(deb, DEB_IN_CONTAINER)
    installed = container.exec("dpkg", "--install", DEB_IN_CONTAINER,
                               env={"DEBIAN_FRONTEND": "noninteractive"})
    print(installed.stdout, end="")
    print(installed.stderr, end="", file=sys.stderr)
    if installed.returncode != 0:
        return False, [f"dpkg --install of the Player failed (exit {installed.returncode})"]
    return True, membership_violations(container.exec("id", "-nG", PLAYER_USER).stdout)


def start_player(container: Container, work: Path) -> list[str]:
    """Steps 5-6: provisioning's handoff, then provisioning's `systemctl start`."""
    handoff = work / "public.json"
    write_handoff(Handoff(Origin.parse_root(PROBE_CENTRAL), None), path=handoff)
    container.exec("mkdir", "-p", str(Path(HANDOFF_IN_CONTAINER).parent))
    container.copy_in(handoff, HANDOFF_IN_CONTAINER)
    try:
        started: int | None = container.exec("systemctl", "start", DEFAULT_UNIT,
                                             timeout=START_SECONDS).returncode
    except subprocess.TimeoutExpired:
        started = None
    shown = container.exec("systemctl", "show",
                           *(f"--property={name}" for name in UNIT_PROPERTIES), DEFAULT_UNIT)
    properties = parse_unit_properties(shown.stdout)
    print("unit: " + " ".join(f"{name}={properties.get(name, '')}" for name in UNIT_PROPERTIES))
    return start_violations(started, properties)


def check_modules(container: Container, initrd: Path, work: Path, *,
                  run: Run = subprocess.run) -> list[str]:
    """Step 7: stage 1's hand-over from the built initrd, then the base's own libkmod."""
    root = work / "initrd"
    root.mkdir()
    violations = unpack(initrd, root, run=run)
    if violations:
        return violations
    release = kernel_release(root)
    if isinstance(release, list):
        return release
    staged = work / "stage2"
    print("modules: " + hand_over_modules(staged, pet=lambda: None, release=release,
                                          source=root / INITRD_MODULES))
    container.exec("mkdir", "-p", "/usr/lib/modules")
    container.copy_in(staged / INITRD_MODULES / release, f"/usr/lib/modules/{release}")
    shown = container.exec("python3", "-I", "-c", KMOD_PROGRAM, release,
                           *(f"{module}={DISPLAY_ALIASES[module]}" for module in DISPLAY_MODULES))
    print(shown.stdout, end="")
    return kmod_violations(shown.stdout)


def probe(image: str, deb: Path, work: Path, *, initrd: Path | None = None,
          run: Run = subprocess.run) -> list[str]:
    """Boot, check the base, install, start, check the modules; the container is always removed,
    even when `docker run` itself fails. The violations (empty = pass)."""
    cpuinfo = work / "cpuinfo"
    cpuinfo.write_text(cpuinfo_text())
    container = Container(f"photo-wall-player-start-probe-{secrets.token_hex(4)}", run=run)
    violations: list[str] = []
    try:
        run(docker_run_argv(image, container.name, cpuinfo), check=True, capture_output=True)
        state = container.wait_booted()
        if state not in BOOTED:
            return [f"the base did not boot under systemd (is-system-running: {state or 'none'})"]
        print(f"booted: {state}")
        failed = container.exec("systemctl", "--failed", "--no-legend", "--plain").stdout
        for line in failed.splitlines():
            print(f"note: failed in the container: {line.strip()}")
        violations += check_base(container)
        installed, found = install_player(container, deb)
        violations += found
        if installed:
            violations += start_player(container, work)
        if initrd is not None:
            violations += check_modules(container, initrd, work, run=run)
        if violations:
            journal = container.exec("journalctl", "--no-pager", "-n", str(JOURNAL_LINES),
                                     "-u", DEFAULT_UNIT)
            print(journal.stdout, end="")
        return violations
    finally:
        run(["docker", "rm", "--force", container.name], check=False, capture_output=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    base = parser.add_mutually_exclusive_group(required=True)
    base.add_argument("--squashfs", type=Path, help="the built base squashfs (needs root)")
    base.add_argument("--image", help="an already imported base image")
    parser.add_argument("--deb", type=Path, required=True, help="the built Player .deb")
    parser.add_argument("--initrd", type=Path,
                        help="the built initrd.img: its modules must load on the base (needs "
                             "root, cpio and the initrd's decompressor)")
    parser.add_argument("--work", type=Path,
                        help="an empty or absent directory to work in (default: a temp dir)")
    args = parser.parse_args(argv)
    if args.squashfs and os.geteuid() != 0:
        parser.error("--squashfs must run as root (unsquashfs keeps the base's ownership)")
    work = args.work or Path(tempfile.mkdtemp(prefix="player-start-probe-"))
    work.mkdir(parents=True, exist_ok=True)
    image = args.image or f"photo-wall-player-start-probe:{secrets.token_hex(4)}"
    try:
        if args.squashfs:
            import_squashfs(args.squashfs, image, work)
        violations = probe(image, args.deb.resolve(), work.resolve(),
                           initrd=args.initrd.resolve() if args.initrd else None)
    except subprocess.CalledProcessError as error:
        stderr = error.stderr or b""
        detail = (stderr.decode("utf-8", "replace") if isinstance(stderr, bytes)
                  else stderr).strip()
        violations = [f"{' '.join(map(str, error.cmd[:2]))} failed: "
                      f"{detail or f'exit {error.returncode}'}"]
    finally:
        if args.squashfs:
            subprocess.run(["docker", "rmi", "--force", image], check=False,
                           capture_output=True)
        if not args.work:
            shutil.rmtree(work, ignore_errors=True)
    for violation in violations:
        print(f"VIOLATION: {violation}")
    if violations:
        print(f"FAIL: the Player unit does not start on this base ({len(violations)} "
              "violation(s))")
        return 1
    print(f"OK: {DEFAULT_UNIT} started on the base under systemd (active/running)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
