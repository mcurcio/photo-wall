#!/usr/bin/env python3
"""Compile the base Weston shell/diagnostic from a pinned Debian snapshot in Docker."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from scripts import node_build_inputs
from scripts.build_player_deb import fetch_tree
from scripts.debian_packages import PIN, packages
from scripts.node_build_inputs import BUILDER_IMAGE, validate_builder


def build(tree: Path, output: Path, *, builder_image: str, architecture: str) -> Path:
    validate_builder(builder_image, architecture, purpose="display")
    builder_script_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    # The inputs module this builder imported (like the script above and build_app_environment),
    # not a tree copy: fetch_tree archives only first-party packages and ARCHIVED_FILES.
    build_inputs_sha256 = hashlib.sha256(Path(node_build_inputs.__file__).read_bytes()).hexdigest()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="photo-wall-display-build-") as temporary:
        work = Path(temporary)
        shutil.copytree(tree / "appliance/display_host", work / "source")
        (work / "snapshot.list").write_text("\n".join(source.line() for source in PIN.sources()) + "\n")
        dependencies = " ".join(packages("node-display", "node-display-build"))
        runtime = " ".join(packages("node-display"))
        source_hash = hashlib.sha256((builder_image + architecture + PIN.snapshot + dependencies).encode())
        for source in sorted((work / "source").rglob("*")):
            if source.is_file() and "__pycache__" not in source.parts:
                source_hash.update(("appliance/display_host/" + source.relative_to(work / "source").as_posix()).encode() + b"\x00" + source.read_bytes())
        version = "2.0+" + source_hash.hexdigest()[:12]
        # The protocol ABI identity binds compiled bytes and resolved runtime
        # versions, not only a source label. This script runs inside the build root.
        seal_script = f"""import hashlib,json,subprocess
from pathlib import Path
h=hashlib.sha256({(source_hash.hexdigest() + architecture)!r}.encode())
for p in sorted(Path('/package/usr/lib').rglob('*')):
    if p.is_file():
        h.update(str(p.relative_to('/package')).encode()+b'\\0'+p.read_bytes())
h.update(Path('/client.so').read_bytes())
h.update(subprocess.check_output(['dpkg-query','-W','-f=${{binary:Package}} ${{Version}} ${{Architecture}}\\n', *{packages("node-display")!r}]))
Path('/package/usr/lib/photo-wall-display/abi.json').write_text(json.dumps({{'graphics_abi':'weston14-'+h.hexdigest(),'plugin_abi':'frame-v3'}},sort_keys=True))
"""
        (work / "seal-display.py").write_text(seal_script)
        dockerfile = f'''FROM {builder_image}
COPY snapshot.list /etc/apt/sources.list
RUN rm -f /etc/apt/sources.list.d/* && printf 'Package: *\\nPin: origin snapshot.debian.org\\nPin-Priority: 1001\\n' > /etc/apt/preferences.d/snapshot && apt-get update && apt-get -y --allow-downgrades dist-upgrade && apt-get install -y --no-install-recommends {dependencies}
COPY source /source
COPY seal-display.py /seal-display.py
RUN meson setup /build /source --prefix=/usr --libdir=lib && meson compile -C /build && DESTDIR=/package meson install -C /build
RUN cp /package/usr/lib/photo-wall-client/libphoto-wall-frame-client.so /client.so && rm -rf /package/usr/lib/photo-wall-client && python3 /seal-display.py
RUN mkdir /package/DEBIAN && dpkg-query -W -f='${{Package}} (= ${{Version}}), ' {runtime} > /depends && printf 'Package: photo-wall-node-display\\nVersion: {version}\\nArchitecture: {architecture}\\nMaintainer: Photo Wall <noreply@example.invalid>\\nDescription: Base-owned Weston display shell and private diagnostic\\nDepends: ' > /package/DEBIAN/control && cat /depends >> /package/DEBIAN/control && sed -i 's/, $//' /package/DEBIAN/control && printf '\\n' >> /package/DEBIAN/control && dpkg-deb --root-owner-group --build /package /node-display.deb
RUN dpkg-query -W -f='${{binary:Package}}\\t${{Version}}\\t${{Architecture}}\\n' > /build-packages.tsv
'''
        (work / "Dockerfile").write_text(dockerfile)
        subprocess.run(["docker", "build", "--platform", "linux/" + architecture,
                        "--iidfile", str(work / "image-id"), str(work)], check=True)
        image = (work / "image-id").read_text().strip()
        container = subprocess.check_output(["docker", "create", "--platform", "linux/" + architecture, image, "/bin/true"], text=True).strip()
        artifact = output / f"photo-wall-node-display_{architecture}.deb"
        try:
            subprocess.run(["docker", "cp", container + ":/node-display.deb", str(artifact)], check=True)
            subprocess.run(["docker", "cp", container + ":/client.so", str(output / "libphoto-wall-frame-client.so")], check=True)
            subprocess.run(["docker", "cp", container + ":/package/usr/lib/photo-wall-display/abi.json", str(output / "display-abi.json")], check=True)
            subprocess.run(["docker", "cp", container + ":/build-packages.tsv", str(output / "display-build-packages.tsv")], check=True)
        finally:
            subprocess.run(["docker", "rm", container], check=True, stdout=subprocess.DEVNULL)
        (output / "display-build-source.json").write_text(json.dumps({"builder_image": builder_image,
            "built_image": image, "architecture": architecture, "source_sha256": source_hash.hexdigest(),
            "builder_script_sha256": builder_script_sha256,
            "build_inputs_sha256": build_inputs_sha256,
            "abi": json.loads((output / "display-abi.json").read_text()),
            "snapshot": PIN.snapshot, "sources": [s.line() for s in PIN.sources()]}, sort_keys=True))
        return artifact


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", required=True)
    parser.add_argument("--builder-image", default=BUILDER_IMAGE)
    parser.add_argument("--architecture", choices=("arm64", "amd64"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="photo-wall-display-source-") as temporary:
        tree = Path(temporary) / "tree"
        fetch_tree(args.repository, args.revision, tree)
        print(build(tree, args.output_dir, builder_image=args.builder_image, architecture=args.architecture))


if __name__ == "__main__":
    main()
