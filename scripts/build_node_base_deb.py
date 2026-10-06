#!/usr/bin/env python3
"""Build separately isolated node launchers as an opt-in portable Debian base package.

Install this .deb in an image build root. Kernel flag photowall.node=v2 selects the
new units and disables legacy app/provision/watchdog reboot policy for that cohort.
The default/legacy boot is unchanged. The base handoff must provide protected V2
configuration; missing handoff refuses node effects rather than inventing authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from types import MappingProxyType

from scripts.build_player_deb import control_file, fetch_tree, run_dpkg_deb
from scripts.debian_packages import packages
from scripts.module_closure import ClosurePolicy, closure_for, stage_application

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
                               ("player", "central", "media", "gi", "appliance.apps", "appliance.node.manager",
                                "appliance.node.manager_desired", "appliance.node.manager_launcher",
                                "appliance.node.manager_observation", "appliance.node.manager_runner"),
                               MappingProxyType({})),
    "app-broker": ClosurePolicy("app-broker", ("appliance.apps.broker_runner",),
                                ("player", "central", "media", "gi", "appliance.host"), MappingProxyType({})),
    "manager-supervisor": ClosurePolicy("manager-supervisor", ("appliance.node.manager_launcher",),
                                        ("player", "central", "media", "gi"), MappingProxyType({})),
    "health-judge": ClosurePolicy("health-judge", ("appliance.health.runner",),
                                  ("player", "central", "media", "gi"), MappingProxyType({})),
}
UNITS = ("photo-wall-node.target", "photo-wall-host-core.service", "photo-wall-app-broker.service",
         "photo-wall-manager-supervisor.service", "photowallbase.slice", "photowallhostcore.slice", "photowallapp.slice",
         "photowallpreparation.slice", "photo-wall-node-handoff.service", "photo-wall-node-prepare.service", "photo-wall-node-storage.service", "photo-wall-display.service", "photo-wall-display-controller.service",
         "photo-wall-health.service")


def sources(tree: Path) -> set[str]:
    """Every tree path `stage_tree` reads: each launcher's closure and the units."""
    return {*(path.as_posix() for policy in POLICIES.values()
              for path in closure_for(policy, repo=tree).files),
            *(f"appliance/systemd/{name}" for name in UNITS)}


def stage_tree(tree: Path, destination: Path) -> str:
    destination.mkdir(parents=True)
    dependencies = packages("node-base")
    # Dependency-only changes must invalidate the base and all environment refs.
    digests = ["debian-depends", json.dumps(dependencies, separators=(",", ":"))]
    for name, policy in POLICIES.items():
        closure = closure_for(policy, repo=tree)
        stage_application(closure, policy, repo=tree, into=destination / ("usr/lib/photo-wall-" + name))
        digests.append(closure.digest)
    unit_dir = destination / "lib/systemd/system"
    unit_dir.mkdir(parents=True)
    for name in UNITS:
        data = (tree / "appliance/systemd" / name).read_bytes()
        (unit_dir / name).write_bytes(data)
        digests.append(hashlib.sha256(data).hexdigest())
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
                                                  'g pw-node-feeds 10007\nm pw-health pw-node-feeds\n')
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
        package="photo-wall-node-base", architecture="all", description="Isolated Photo Wall node management; opt-in V2 base services"))
    return version


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="photo-wall-node-base-") as temporary:
        work = Path(temporary)
        fetch_tree(args.repository, args.revision, work / "tree")
        version = stage_tree(work / "tree", work / "package")
        print(run_dpkg_deb(work / "package", args.output_dir / f"photo-wall-node-base_{version}_all.deb"))


if __name__ == "__main__":
    main()
