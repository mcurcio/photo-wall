"""One release build's outputs, synthetic: what base-image.yml, node-components.yml and the
pipeline's `images` job hand the packager (scripts/package_release_artifacts.py) and the seal
(scripts/release_seal.py).

The bundle is shaped like `scripts/build_netboot_bundle.sh`'s, the component set like
`scripts/build_node_components.py`'s (stamped for REVISION), and each service image is a
`<repository>@<digest>` reference.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from contracts.app_environment import AppEnvironmentRefV2
from contracts.release import CMDLINE
from scripts.node_release_artifacts import COMPONENTS_SCHEMA, write_stamp

BUNDLE_BUILDER = Path(__file__).resolve().parents[2] / "scripts" / "build_netboot_bundle.sh"


def cmdline_template(script: str | None = None) -> str:
    """The whole cmdline.txt `script` (by default scripts/build_netboot_bundle.sh) writes: its
    heredoc's body, so a fixture bundle carries the builder's own template."""
    text = BUNDLE_BUILDER.read_text() if script is None else script
    match = re.search(r'cat > "\$boot_dir/cmdline\.txt" <<\'EOF\'\n(.*?\n)EOF\n', text, re.DOTALL)
    assert match, "cmdline.txt heredoc not found in build_netboot_bundle.sh"
    return match.group(1)


REVISION = "a" * 40
TAG = "v2.0.0"
COMPONENT_INPUTS = "c" * 64          # the component set's recorded input digest
EPOCH = 1_790_000_000               # a fixed source date for the base tarball's members
REPOSITORY = "ghcr.io/owner/repo"


def manifest_blob(name: str, cut: str = "") -> bytes:
    """The bytes of a pushed image's manifest; its digest is their sha256."""
    return f'{{"schemaVersion":2,"image":"{name}{cut}"}}'.encode()


def digest(name: str, cut: str = "") -> str:
    return f"sha256:{hashlib.sha256(manifest_blob(name, cut)).hexdigest()}"


def reference(name: str, cut: str = "") -> str:
    return f"{REPOSITORY}/{name}@{digest(name, cut)}"


IMAGE_REFERENCES = {"central": reference("central"), "media-worker": reference("media-worker")}


def write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def base_bundle(root: Path) -> Path:
    bundle = root / "base-bundle"
    write(bundle / "photo-wall-base.squashfs", b"fake-base-squashfs-bytes")
    write(bundle / "boot" / "config.txt", b"[all]\narm_64bit=1\n")
    write(bundle / "boot" / CMDLINE, cmdline_template().encode())
    write(bundle / "boot" / "kernel_2712.img", b"fake-kernel-bytes")
    write(bundle / "boot" / "initrd.img", b"fake-initrd-bytes")
    write(bundle / "boot" / "bcm2712-rpi-5-b.dtb", b"fake-dtb-bytes")
    write(bundle / "boot" / "overlays" / "fake.dtbo", b"fake-overlay-bytes")
    write(bundle / "boot" / "pieeprom.upd", b"fake-eeprom-bytes")
    write(bundle / "boot" / "pieeprom.sig", b"fake-eeprom-digest")
    write(bundle / "SHA256SUMS", b"deadbeef  photo-wall-base.squashfs\n")
    return bundle


def node_components(root: Path, revision: str = REVISION) -> Path:
    """A component set stamped for `revision`: the app and the primary manager roots with their
    .debs, the node base and display .debs, components.json and build-provenance.json."""
    components = root / "components"
    components.mkdir(parents=True)
    abi = {"base_abi": "node-v2-test", "graphics_abi": "weston14-test", "plugin_abi": "frame-v2"}
    refs = {}
    for role, name in (("manager-primary", "photo-wall-node-manager"), ("app", "photo-wall-player")):
        deb = (role + "-deb").encode()
        archive = (role + "-root").encode()
        (components / (role + ".deb")).write_bytes(deb)
        (components / (role + ".squashfs")).write_bytes(archive)
        refs[role] = asdict(AppEnvironmentRefV2(
            hashlib.sha256(archive).hexdigest(), len(archive), hashlib.sha256(deb).hexdigest(),
            name, "2.0", "arm64", "a" * 64, "b" * 64, "/usr/bin/entry", **abi))
    for role in ("node-base", "node-display"):
        (components / (role + ".deb")).write_bytes(role.encode())
    (components / "components.json").write_text(json.dumps({
        "schema": COMPONENTS_SCHEMA, "abi": abi, "app_environment": refs["app"],
        "manager_primary": refs["manager-primary"], "manager_fallback": None}))
    (components / "build-provenance.json").write_text(json.dumps(
        {"schema": COMPONENTS_SCHEMA, "abi": abi, "inputs_sha256": COMPONENT_INPUTS}))
    write_stamp(components, revision=revision, inputs_sha256=COMPONENT_INPUTS)
    return components


@dataclass(frozen=True)
class Inputs:
    base_bundle: Path
    node_components: Path


def inputs(root: Path) -> Inputs:
    return Inputs(base_bundle(root), node_components(root))
