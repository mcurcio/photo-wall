#!/usr/bin/env python3
"""Build separately isolated node launchers as an opt-in portable Debian base package.

Install this .deb in an image build root. Kernel flag photowall.node=v2 selects the
new units and disables legacy app/provision/watchdog reboot policy for that cohort.
The default/legacy boot is unchanged. The base handoff must provide protected V2
configuration; missing handoff refuses node effects rather than inventing authority.

The package also carries the Node bus (E3c): the pinned linux-arm64 nats-server and the shipped
`node-bus.conf` under BUS_DIRECTORY, run by photo-wall-bus.service, so the package is arm64; the
release's Apache-2.0 LICENSE ships beside the binary, as each vendored wheel's licences ship in its
dist-info.
`stage_tree` stays network-free (unit-tier tests stage it); `stage_vendored` adds the pinned bytes,
and `stage_package` (both) is the one path to a .deb (erratum E-E3C-CUT-4).

A launcher whose policy declares a vendored root (`scripts/vendored_packages.py`: nats-py, which
Debian does not package, erratum E-E3C-CUT-3) gets that wheel staged in its own directory beside its
closure, by `stage_vendored`: HostCore runs the host component's `nodeapi` session (E-E3C-CUT-2). A
declared root no code reaches is refused, as the other .deb builders refuse one.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from types import MappingProxyType
from typing import Final

from scripts.build_player_deb import control_file, fetch_tree, run_dpkg_deb
from scripts.debian_packages import packages
from scripts.module_closure import (
    ClosureError,
    ClosurePolicy,
    closure_for,
    stage_application,
    unreached_imports,
)
from scripts.nats_server import ASSETS, LICENSE, NODE_PLATFORM
from scripts.nats_server import fetch as fetch_nats_server
from scripts.vendored_packages import import_table as vendored_imports
from scripts.vendored_packages import stage_wheel, wheel

POLICIES = {
    "root-import": ClosurePolicy("root-import", ("appliance.apps.root_import",),
                                ("player", "central", "gi", "appliance.host", "appliance.apps.broker",
                                 "appliance.apps.broker_runner", "appliance.apps.process_linux"), MappingProxyType({})),
    "node-bootstrap": ClosurePolicy("node-bootstrap", ("appliance.boot.node_bootstrap",),
                                    ("player", "central", "media", "gi", "appliance.host",
                                     "appliance.health", "appliance.display_host",
                                     "appliance.central_session"), MappingProxyType({})),
    "display-controller": ClosurePolicy("display-controller", ("appliance.display_host.runner",),
                                        ("player", "central", "media", "gi"), MappingProxyType({})),
    "host-core": ClosurePolicy("host-core", ("appliance.host.host_runner",),
                               ("player", "central", "media", "gi", "appliance.apps", "appliance.display_host",
                                "appliance.health", "appliance.node.manager",
                                "appliance.node.manager_desired", "appliance.node.manager_launcher",
                                "appliance.node.manager_observation", "appliance.node.manager_runner"),
                               MappingProxyType({}),
                               MappingProxyType({"nats": vendored_imports()["nats"]})),
    "app-broker": ClosurePolicy("app-broker", ("appliance.apps.broker_runner",),
                                ("player", "central", "media", "gi", "appliance.host"), MappingProxyType({})),
    "manager-supervisor": ClosurePolicy("manager-supervisor", ("appliance.node.manager_launcher",),
                                        ("player", "central", "media", "gi"), MappingProxyType({})),
    "health-judge": ClosurePolicy("health-judge", ("appliance.health.runner",),
                                  ("player", "central", "media", "gi"), MappingProxyType({})),
}
UNITS = ("photo-wall-node.target", "photo-wall-host-core.service", "photo-wall-app-broker.service",
         "photo-wall-manager-supervisor.service", "photowallbase.slice", "photowallhostcore.slice", "photowallapp.slice",
         "photowallpreparation.slice", "photowallbus.slice", "photo-wall-node-handoff.service", "photo-wall-node-prepare.service", "photo-wall-node-storage.service", "photo-wall-display.service", "photo-wall-display-controller.service",
         "photo-wall-health.service", "photo-wall-bus.service")
BUS_DIRECTORY: Final = "usr/lib/photo-wall-bus"   # nats-server and node-bus.conf
BUS_CONF: Final = "appliance/bus/node-bus.conf"


def sources(tree: Path) -> set[str]:
    """Every tree path `stage_tree` reads: each launcher's closure, the units and the bus's
    configuration. The scripts that pin and fetch the bus's binary are the build process's own
    imports, keyed by `node_component_inputs.builder_files` (the fetched tree holds no script)."""
    return {*(path.as_posix() for policy in POLICIES.values()
              for path in closure_for(policy, repo=tree).files),
            *(f"appliance/systemd/{name}" for name in UNITS), BUS_CONF}


def stage_tree(tree: Path, destination: Path) -> str:
    destination.mkdir(parents=True)
    dependencies = packages("node-base")
    # Dependency-only changes must invalidate the base and all environment refs.
    digests = ["debian-depends", json.dumps(dependencies, separators=(",", ":"))]
    for name, policy in POLICIES.items():
        closure = closure_for(policy, repo=tree)
        if unreached := unreached_imports(closure, policy):
            raise ClosureError(f"{name} declares imports no code reaches: {', '.join(unreached)}")
        stage_application(closure, policy, repo=tree, into=destination / ("usr/lib/photo-wall-" + name))
        digests.append(closure.digest)
    # Each vendored wheel's pinned digest (stage_vendored adds its files), so a new pin is a new base.
    for name, distribution in _vendored():
        digests.extend(("vendored", name, distribution, wheel(distribution).sha256))
    unit_dir = destination / "lib/systemd/system"
    unit_dir.mkdir(parents=True)
    for name in UNITS:
        data = (tree / "appliance/systemd" / name).read_bytes()
        (unit_dir / name).write_bytes(data)
        digests.append(hashlib.sha256(data).hexdigest())
    # The bus: its configuration byte for byte, and the pinned binary's digest (stage_vendored
    # adds the binary), so a new nats-server pin is a new base.
    bus = destination / BUS_DIRECTORY
    bus.mkdir(parents=True)
    (bus / "node-bus.conf").write_bytes((tree / BUS_CONF).read_bytes())
    digests.extend(("nats-server", ASSETS[NODE_PLATFORM][1]))
    wants = destination / "etc/systemd/system/multi-user.target.wants"
    wants.mkdir(parents=True)
    (wants / "photo-wall-node.target").symlink_to("/lib/systemd/system/photo-wall-node.target")
    for name in ("photo-wall-provision.service", "photo-wall-player.service", "photo-wall-os-agent.service", "photo-wall-weston.service"):
        override = unit_dir / (name + ".d")
        override.mkdir()
        (override / "node-cohort.conf").write_text("[Unit]\nConditionKernelCommandLine=!photowall.node=v2\n")
    users = destination / "usr/lib/sysusers.d"
    users.mkdir(parents=True)
    (users / "photo-wall-node.conf").write_text('u pw-manager 10003 "Photo Wall manager" /nonexistent\nu pw-player 10004 "Photo Wall Player" /nonexistent\nu pw-display 10005 "Photo Wall display" /nonexistent\n'
                                                  'u pw-health 10006 "Photo Wall health judge" /nonexistent\n'
                                                  'g pw-node-feeds 10007\nm pw-health pw-node-feeds\n'
                                                  'u pw-bus 10008 "Photo Wall Node bus" /nonexistent\n')
    temporary = destination / "usr/lib/tmpfiles.d"
    temporary.mkdir(parents=True)
    (temporary / "photo-wall-node.conf").write_text("d /run/photo-wall-node 0700 root root -\nd /run/photo-wall-app-proof 0755 root root -\n"
                                                     "d /run/photo-wall-boot-stage 0755 root root -\n"
                                                     "d /run/photo-wall-app-feed 0750 root pw-node-feeds -\n"
                                                     "d /run/photo-wall-display-feed 0750 pw-display pw-node-feeds -\n")
    # Include units, generated cohort policy and UID/tmpfiles contracts in identity.
    for path in sorted(destination.rglob("*")):
        if path.is_file() and not path.is_symlink():
            digests.extend((path.relative_to(destination).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    identity = hashlib.sha256("\n".join(digests).encode()).hexdigest()
    version = "2.0+" + identity[:12]
    marker = destination / "usr/lib/photo-wall-node-base/abi.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"base_abi": "node-v2-" + identity}, sort_keys=True))
    control = destination / "DEBIAN"
    control.mkdir()
    (control / "control").write_bytes(control_file(version, dependencies,
        package="photo-wall-node-base", architecture=NODE_PLATFORM[1], description="Isolated Photo Wall node management; opt-in V2 base services"))
    return version


def _vendored() -> list[tuple[str, str]]:
    """(launcher name, distribution) for every vendored wheel a launcher ships, sorted."""
    return sorted({(name, distribution) for name, policy in POLICIES.items()
                   for distribution in policy.vendored.values()})


def stage_vendored(destination: Path, downloads: Path) -> None:
    """The pinned NODE_PLATFORM nats-server at BUS_DIRECTORY/nats-server, mode 0755, its release's
    LICENSE at BUS_DIRECTORY/LICENSE, and the wheel of every vendored root of every launcher in
    usr/lib/photo-wall-<name>, from the pinned downloads."""
    binary = fetch_nats_server(downloads, system=NODE_PLATFORM[0], machine=NODE_PLATFORM[1])
    target = destination / BUS_DIRECTORY / "nats-server"
    shutil.copyfile(binary, target)
    os.chmod(target, 0o755)
    shutil.copyfile(binary.parent / LICENSE, destination / BUS_DIRECTORY / LICENSE)
    for name, distribution in _vendored():
        stage_wheel(wheel(distribution), destination / ("usr/lib/photo-wall-" + name), downloads)


def stage_package(tree: Path, destination: Path, downloads: Path) -> str:
    """stage_tree, then stage_vendored: the one path to a .deb."""
    version = stage_tree(tree, destination)
    stage_vendored(destination, downloads)
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--downloads", type=Path, default=None,
                        help="the pinned downloads' cache (default: inside the work directory)")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="photo-wall-node-base-") as temporary:
        work = Path(temporary)
        fetch_tree(args.repository, args.revision, work / "tree")
        version = stage_package(work / "tree", work / "package", args.downloads or work / "downloads")
        print(run_dpkg_deb(work / "package",
                           args.output_dir / f"photo-wall-node-base_{version}_{NODE_PLATFORM[1]}.deb"))


if __name__ == "__main__":
    main()
