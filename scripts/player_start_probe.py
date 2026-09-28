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
  6. pass only if that start returns 0 and the unit is active/running.

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
from uplink.origin import Origin  # noqa: E402

DEVICE_GROUPS: Final = ("render", "video", "input")
PLAYER_USER: Final = "wall"
PROVISION_UNIT: Final = "photo-wall-provision.service"
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
    a container (systemd.mask= keeps the provisioner from running)."""
    return ["docker", "run", "--detach", "--name", name, "--privileged",
            "--env", "container=docker", "--tmpfs", "/run", "--tmpfs", "/run/lock",
            "--volume", f"{cpuinfo}:/proc/cpuinfo:ro",
            image, SYSTEMD, f"systemd.mask={PROVISION_UNIT}"]


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


def probe(image: str, deb: Path, work: Path, *, run: Run = subprocess.run) -> list[str]:
    """Boot, check the base, install, start, always remove the container. The violations
    (empty = pass)."""
    cpuinfo = work / "cpuinfo"
    cpuinfo.write_text(cpuinfo_text())
    container = Container(f"photo-wall-player-start-probe-{secrets.token_hex(4)}", run=run)
    run(docker_run_argv(image, container.name, cpuinfo), check=True, capture_output=True)
    violations: list[str] = []
    try:
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
        violations = probe(image, args.deb.resolve(), work.resolve())
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
