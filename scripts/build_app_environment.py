#!/usr/bin/env python3
"""Resolve/configure a real Player .deb inside a digest-pinned Debian build image.

Docker build isolates all maintainer scripts from the host. Authenticated snapshot
sources replace the image's sources; the exported image is never started on a Pi.
The seal preserves root-confined Debian links, rejects escaping or invalid cyclic links, strips dependency setid bits and special files, and inventories every
runtime byte. The Pi has neither apt resolution nor package-script execution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tarfile
import tempfile
from dataclasses import asdict
from pathlib import Path, PurePosixPath

from appliance.apps.environment import (
    FORMAT,
    MAX_EXPANDED,
    MAX_FILES,
    canonical_bytes,
    capacity,
    file_sha256,
    inventory,
    normalize_rootfs_directories,
    resolve_member,
)
from contracts.app_environment import AppEnvironmentRefV2
from scripts.debian_packages import PIN
from scripts.node_build_inputs import BUILDER_IMAGE, docker_build, validate_builder

EXCLUDED = {"dev", "proc", "sys", "run", "tmp"}


def materialize(export: Path, root: Path) -> None:
    """Rebuild a safe root, preserving Debian symlinks without host traversal."""
    with tarfile.open(export, "r:") as archive:
        members = archive.getmembers()
        if len(members) > MAX_FILES:
            raise ValueError("closure_file_count")
        entries = {}
        for member in members:
            name = member.name.rstrip("/")
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or name in entries:
                raise ValueError("closure_member_path")
            entries[name] = member
        links = {name: ("/" + member.linkname.lstrip("/") if member.islnk() else member.linkname)
                 for name, member in entries.items() if member.issym() or member.islnk()}
        total = 0
        root.mkdir()
        for name, member in entries.items():
            if not name or PurePosixPath(name).parts[0] in EXCLUDED:
                continue
            if any("/".join(PurePosixPath(name).parts[:index]) in links for index in range(1, len(PurePosixPath(name).parts))):
                raise ValueError("closure_link_parent")
            destination = root / name
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            elif member.isfile() or member.islnk():
                source = entries[resolve_member(name, links)] if member.islnk() else member
                if not source.isfile():
                    raise ValueError("closure_hardlink_target")
                total += source.size
                if total > MAX_EXPANDED:
                    raise ValueError("closure_expansion_capacity")
                destination.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(source)
                if stream is None:
                    raise ValueError("closure_member_missing")
                with stream, destination.open("xb") as output:
                    while chunk := stream.read(1024 * 1024):
                        output.write(chunk)
                destination.chmod((source.mode & 0o755) | 0o400)
            elif not member.issym():
                raise ValueError("closure_special_member")
        # systemd binds the boot-supplied public config over this exact empty
        # mountpoint. Predeclaring it prevents launch from mutating the sealed root.
        configuration = root / "etc/photo-wall/public.json"
        if any(name in links for name in ("etc", "etc/photo-wall", "etc/photo-wall/public.json")):
            raise ValueError("closure_configuration_mountpoint_link")
        configuration.parent.mkdir(parents=True, exist_ok=True)
        if configuration.exists():
            if not configuration.is_file() or configuration.stat().st_size != 0 or configuration.stat().st_mode & 0o777 != 0o444:
                raise ValueError("closure_configuration_not_placeholder")
        else:
            configuration.touch(mode=0o444)
            configuration.chmod(0o444)
        for name in EXCLUDED:
            (root / name).mkdir(exist_ok=True)
        (root / "run/photo-wall-client").mkdir(mode=0o755)
        (root / "run/photo-wall-wayland").mkdir(mode=0o755)
        for name, member in entries.items():
            if not member.issym() or PurePosixPath(name).parts[0] in EXCLUDED:
                continue
            resolve_member(name, links)
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(member.linkname)
        normalize_rootfs_directories(root)


def build(deb: Path, output: Path, *, builder_image: str, architecture: str,
          base_abi: str, graphics_abi: str, plugin_abi: str,
          role: str = "app") -> AppEnvironmentRefV2:
    """Seal `deb`'s environment. `role` names its build cache scope (node_build_inputs): one
    per package whose dependencies differ, so one role's build never replaces another's index."""
    validate_builder(builder_image, architecture, purpose="closure")
    input_files = {name: file_sha256(Path(__file__).resolve().parents[1] / name) for name in (
        "scripts/build_app_environment.py", "scripts/debian_packages.py",
        "scripts/node_build_inputs.py", "appliance/apps/environment.py")}
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="photo-wall-closure-") as temporary:
        work = Path(temporary)
        (work / "player.deb").write_bytes(deb.read_bytes())
        (work / "snapshot.list").write_text("\n".join(source.line() for source in PIN.sources()) + "\n")
        # No mounts, host namespaces, privileged containers, install on host, or daemon
        # socket in the build. COPY input is the sole application-controlled build input.
        # The package's own relations are satisfied in a layer of their own, before the package
        # is copied: `fields` reads them out of the .deb, and BuildKit keys COPY --from on the
        # copied bytes, so a new package with unchanged relations (every commit: the version
        # names it) reuses the installed dependencies and only installs itself. A relation-less
        # package skips the satisfy; the final install resolves anything satisfy left.
        dockerfile = f'''FROM {builder_image} AS fields
COPY player.deb /tmp/player.deb
RUN dpkg-deb --show --showformat='${{Pre-Depends}},${{Depends}}' /tmp/player.deb | tr '\\n' ' ' | sed -e 's/^ *, *//' -e 's/ *, *$//' > /relations

FROM {builder_image}
COPY snapshot.list /etc/apt/sources.list
RUN rm -f /etc/apt/sources.list.d/* && printf 'Package: *\\nPin: origin snapshot.debian.org\\nPin-Priority: 1001\\n' > /etc/apt/preferences.d/snapshot && apt-get update && apt-get -y --allow-downgrades dist-upgrade
COPY --from=fields /relations /tmp/relations
RUN printf '#!/bin/sh\\nexit 101\\n' > /usr/sbin/policy-rc.d && chmod 755 /usr/sbin/policy-rc.d && if [ -s /tmp/relations ]; then apt-get satisfy -y --no-install-recommends "$(cat /tmp/relations)"; fi && rm /tmp/relations
COPY player.deb /tmp/player.deb
RUN apt-get install -y --no-install-recommends /tmp/player.deb && dpkg --audit && dpkg-query -W -f='${{binary:Package}}\\t${{Version}}\\t${{Architecture}}\\n' > /dependency-lock.tsv && dpkg-deb --show --showformat='${{Package}}\\n${{Version}}\\n${{Architecture}}\\n' /tmp/player.deb > /player-fields && rm /tmp/player.deb
RUN mkdir -p /usr/lib/photo-wall-environment && printf '#!/bin/sh\\nexec /usr/bin/python3 -I -B /usr/lib/photo-wall-player --config /etc/photo-wall/public.json\\n' > /usr/lib/photo-wall-environment/entry && chmod 755 /usr/lib/photo-wall-environment/entry
'''
        (work / "Dockerfile").write_text(dockerfile)
        image = docker_build(work, architecture=architecture, role="environment-" + role)
        container = subprocess.check_output(["docker", "create", "--platform", "linux/" + architecture, image, "/bin/true"], text=True).strip()
        try:
            subprocess.run(["docker", "export", "--output", str(work / "root.tar"), container], check=True)
        finally:
            subprocess.run(["docker", "rm", container], check=True, stdout=subprocess.DEVNULL)
        sealed = work / "sealed"
        sealed.mkdir()
        materialize(work / "root.tar", sealed / "rootfs")
        root = sealed / "rootfs"
        fields = (root / "player-fields").read_text().splitlines()
        if len(fields) != 3 or fields[2] not in (architecture, "all"):
            raise ValueError("closure_deb_metadata")
        lock = [line.split("\t") for line in (root / "dependency-lock.tsv").read_text().splitlines()]
        if any(len(row) != 3 for row in lock):
            raise ValueError("closure_package_lock")
        lock_bytes = canonical_bytes({"schema": 2, "packages": sorted(lock)})
        source_bytes = canonical_bytes({"schema": 2, "builder_image": builder_image,
                                       "built_image": image, "architecture": architecture, "snapshot": PIN.snapshot,
                                       "input_files": input_files,
                                       "sealer_sha256": input_files["scripts/build_app_environment.py"],
                                       "sources": [source.line() for source in PIN.sources()]})
        (sealed / "dependency-lock.json").write_bytes(lock_bytes)
        (sealed / "sources.json").write_bytes(source_bytes)
        entry_points = {"photo-wall-player": "/usr/lib/photo-wall-environment/entry",
                        "photo-wall-node-manager": "/usr/lib/photo-wall-node-manager/entry"}
        if fields[0] not in entry_points:
            raise ValueError("closure_package_entrypoint_unsupported")
        reference = AppEnvironmentRefV2("0" * 64, 1, file_sha256(deb), fields[0], fields[1],
                                         architecture, hashlib.sha256(lock_bytes).hexdigest(),
                                         hashlib.sha256(source_bytes).hexdigest(),
                                         entry_points[fields[0]], base_abi,
                                         graphics_abi, plugin_abi)
        metadata = asdict(reference)
        metadata.pop("environment_sha256")
        metadata.pop("size_bytes")
        files = inventory(root)
        (sealed / "environment.json").write_bytes(canonical_bytes(
            {"schema": 2, "format": FORMAT, "reference": metadata, "files": files, "capacity": capacity(files)}))
        archive_path = work / "environment.tar"
        with tarfile.open(archive_path, "w", format=tarfile.GNU_FORMAT, dereference=False) as archive:
            for path in sorted(sealed.rglob("*")):
                info = archive.gettarinfo(str(path), arcname=path.relative_to(sealed).as_posix())
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = PIN.epoch
                if info.isfile():
                    with path.open("rb") as stream:
                        archive.addfile(info, stream)
                else:
                    archive.addfile(info)
        reference = AppEnvironmentRefV2(**{**asdict(reference), "environment_sha256": file_sha256(archive_path), "size_bytes": archive_path.stat().st_size})
        (output / (reference.environment_sha256 + ".tar")).write_bytes(archive_path.read_bytes())
        (output / (reference.environment_sha256 + ".json")).write_bytes(canonical_bytes(asdict(reference)))
        return reference


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deb", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--builder-image", default=BUILDER_IMAGE)
    parser.add_argument("--architecture", choices=("arm64", "amd64"), required=True)
    for name in ("base-abi", "graphics-abi", "plugin-abi"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    print(json.dumps(asdict(build(args.deb, args.output, builder_image=args.builder_image,
                                 architecture=args.architecture, base_abi=args.base_abi,
                                 graphics_abi=args.graphics_abi, plugin_abi=args.plugin_abi)), sort_keys=True))


if __name__ == "__main__":
    main()
