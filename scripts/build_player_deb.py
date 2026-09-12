"""Build the Player application as a `.deb` package (0009 slice 4, gate #6).

The base image no longer carries the Player; central publishes it as a
downloadable `.deb` that a bootstrapper fetches at boot and unpacks. Gate #6
picked the payload shape: a prebuilt venv baked to the fixed path
`/opt/photo-wall/venv` -- "install is an unpack," needing no pip or build
tools in the base at boot
(docs/decisions/0009-minimal-base-and-app-package.md, "Storage, lifecycle,
migration -- The `.deb` (record and build)").

This is the SAME player code and venv the appliance installs today:

- The wheelhouse comes from `scripts/build_player.py`'s existing hashed,
  offline builder (`build`, `ROOTS`, `locked_runtime`, `make_player_wheel`) --
  reused verbatim, not reimplemented.
- The venv is assembled the way `appliance/build.py:configure_root`
  (~:749-808) does today: `python3.12 -m venv --system-site-packages
  /opt/photo-wall/venv`, then `pip install --no-index --require-hashes` from
  the wheelhouse, inside the same arm64/24.04 chroot the appliance build uses
  (`appliance.build._root` / `in_root`).
- The `.deb` version is exactly the player wheel version
  (`base_version+g<commit>`, `scripts/build_player.py:382`) -- one identifier
  for source, wheel, and `.deb` (gate #6).
- `Depends:` names only the native system libraries the venv needs at run
  time (GTK/GStreamer/weston), derived from the same
  `appliance.os_packages.RUNTIME_PACKAGES` the base installs -- never a PyPI
  package, which stays vendored inside the venv
  (`scripts/build_player.py:ROOTS`).
- The package carries NO deployment configuration and NO `central_origin`:
  per 0009, that is supplied by the bootstrapper/central at boot, never baked
  into a published asset.

Split for testability: everything above the "gated: Linux + root" banner is
pure staging/metadata logic, exercised on any host. Everything below it
needs an arm64 Linux chroot (to build the venv) and `dpkg-deb`, and is
gated exactly like `tests/test_appliance_build.py`'s `linux_tools` marker:
`sys.platform == "linux" and PHOTO_WALL_IMAGE_TOOL_TESTS=1`.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from appliance.os_packages import RUNTIME_PACKAGES
from scripts.build_player import BuildError
from scripts.build_player import build as build_player_wheelhouse

PACKAGE = "photo-wall-player"
ARCHITECTURE = "arm64"
MAINTAINER = "Photo Wall <photo-wall@localhost>"
DESCRIPTION = (
    "Photo Wall Player application: a prebuilt venv unpacked to "
    "/opt/photo-wall/venv plus the player and weston systemd units. "
    "Deployment configuration and central_origin are supplied at boot by "
    "the bootstrapper/central (0009), never baked into this package."
)

# The systemd units this package ships, and the "photo-wall-" prefix the
# appliance build already uses for the same units (appliance/build.py:789).
UNIT_FILES = ("player.service", "weston.service")

# Packages RUNTIME_PACKAGES installs into the base for reasons unrelated to
# running the Player (venv *creation* only, or base/boot concerns) never
# belong in the .deb's Depends -- the base already has them, or they are not
# needed once the venv exists.
_DEB_EXCLUDE_PACKAGES = frozenset({
    "python3.12-venv",  # only needed to create the venv, not to run it
    "chrony", "ca-certificates", "openssl",  # base OS/boot concerns
    "initramfs-tools", "busybox-initramfs", "iproute2", "kmod",  # boot/base only
})

# Native libraries (GTK/GStreamer/weston, plus the interpreter the venv's
# --system-site-packages relies on) the venv needs at runtime, derived from
# the base's own package list rather than a second hardcoded copy.
DEB_DEPENDS = tuple(
    name for name in RUNTIME_PACKAGES if name not in _DEB_EXCLUDE_PACKAGES
)

# The wheelhouse's vendored PyPI roots (scripts/build_player.py:ROOTS) must
# never leak into Depends: they are installed inside the venv --no-index,
# hash-locked, never from the distro (0009 gate #6: "Depends: only the
# native libraries the base provides").
_PYPI_ROOTS = frozenset({"pydantic", "httpx", "websockets", "cryptography", "zeroconf"})


def package_version(inventory: dict) -> str:
    """The .deb version is exactly the player wheel version (gate #6)."""
    version = inventory.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+~-]*", version):
        raise BuildError("invalid_inventory_version")
    return version


def control_file(version: str, depends: tuple[str, ...], *, package: str = PACKAGE,
                 architecture: str = ARCHITECTURE, maintainer: str = MAINTAINER,
                 description: str = DESCRIPTION) -> bytes:
    """Generate the Debian control file for the given version and Depends.

    Refuses to emit a control file naming a vendored PyPI dependency: those
    stay inside the venv, never a distro Depends (0009 gate #6).
    """
    if not depends or any(not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name) for name in depends):
        raise BuildError("invalid_deb_depends")
    leaked = sorted({name for name in depends if name.lower() in _PYPI_ROOTS})
    if leaked:
        raise BuildError(f"pypi_dependency_in_deb_depends:{','.join(leaked)}")
    lines = [
        f"Package: {package}",
        f"Version: {version}",
        f"Architecture: {architecture}",
        f"Maintainer: {maintainer}",
        f"Depends: {', '.join(sorted(set(depends)))}",
        f"Description: {description}",
        "",
    ]
    return "\n".join(lines).encode()


def postinst_script() -> bytes:
    """Idempotent 'wall' user/group creation.

    Mirrors appliance/build.py's configure_root (:812-818) exactly: dpkg-deb
    cannot create a system user by unpacking files alone, so this is the one
    maintainer script this package needs. Installing the venv itself remains
    a pure unpack (gate #6) -- this script only provisions the identity the
    shipped units run as.
    """
    return (
        "#!/bin/sh\n"
        "set -e\n"
        "if ! getent passwd wall >/dev/null; then\n"
        "  useradd --uid 10001 --user-group --no-create-home "
        "--shell /usr/sbin/nologin wall\n"
        "fi\n"
        "for group in video render input; do\n"
        "  if getent group \"$group\" >/dev/null; then\n"
        "    usermod -a -G \"$group\" wall\n"
        "  fi\n"
        "done\n"
        "systemctl daemon-reload || true\n"
    ).encode()


def stage_tree(deb_root: Path, *, venv_source: Path, systemd_source: Path,
               weston_ini: bytes, version: str, depends: tuple[str, ...] = DEB_DEPENDS) -> None:
    """Assemble the `.deb` staging tree.

    `venv_source` is an already-built venv directory (dependency-injected so
    this can be exercised on any host with a stub directory; the real venv
    build needs the gated Linux/root path below). Places the venv at the
    fixed `/opt/photo-wall/venv` (gate #6) and the units at
    `/etc/systemd/system`, mirroring appliance/build.py's configure_root
    exactly. Carries NO `/etc/photo-wall` deployment configuration and NO
    `central_origin` -- per 0009 those arrive at boot from the
    bootstrapper/central, never from a published asset.
    """
    if deb_root.exists():
        raise BuildError("stage_root_exists")
    deb_root.mkdir(parents=True)

    debian = deb_root / "DEBIAN"
    debian.mkdir(mode=0o755)
    control_path = debian / "control"
    control_path.write_bytes(control_file(version, depends))
    control_path.chmod(0o644)
    postinst_path = debian / "postinst"
    postinst_path.write_bytes(postinst_script())
    postinst_path.chmod(0o755)

    venv_target = deb_root / "opt/photo-wall/venv"
    venv_target.parent.mkdir(parents=True)
    shutil.copytree(venv_source, venv_target, symlinks=True)

    units_dir = deb_root / "etc/systemd/system"
    units_dir.mkdir(parents=True)
    wants_dir = units_dir / "multi-user.target.wants"
    wants_dir.mkdir()
    for name in UNIT_FILES:
        target_name = "photo-wall-" + name
        target = units_dir / target_name
        shutil.copyfile(systemd_source / name, target)
        target.chmod(0o644)
        # Ship the unit already enabled: a symlink is data, not a maintainer
        # script, so "install is an unpack" (gate #6) also covers activation.
        (wants_dir / target_name).symlink_to("/etc/systemd/system/" + target_name)

    weston_dir = deb_root / "etc/xdg/weston"
    weston_dir.mkdir(parents=True)
    (weston_dir / "weston.ini").write_bytes(weston_ini)


_FORBIDDEN_CONFIG_NAMES = frozenset({"public.json", "bootstrap.json", "ca.pem", "release.pub.pem"})


def assert_no_deployment_config(deb_root: Path) -> None:
    """0009 invariant: no application config and no central_origin in the `.deb`.

    Configuration (Frame binding, assignments, calibration, central_origin)
    arrives at boot from the bootstrapper/central -- never baked into a
    published asset (0009, "The three rules that make it hold", rule 2).
    """
    if (deb_root / "etc/photo-wall").exists():
        raise BuildError("deployment_config_in_deb")
    for path in deb_root.rglob("*"):
        if path.name in _FORBIDDEN_CONFIG_NAMES or path.name == "central_origin":
            raise BuildError("deployment_config_in_deb")


# --- gated: Linux + root (arm64 chroot) + dpkg-deb -------------------------
#
# Everything below builds the real venv (needs the appliance's arm64/24.04
# chroot, exactly like appliance/build.py:configure_root) and invokes
# dpkg-deb. Mirrors tests/test_appliance_build.py's `linux_tools` gate:
# sys.platform == "linux" and PHOTO_WALL_IMAGE_TOOL_TESTS=1 (CI only).


def build_venv(root: Path, wheelhouse: Path, evidence: Path) -> Path:
    """Build the prebuilt venv this package ships.

    Reproduces appliance/build.py:751-754 (`configure_root`) verbatim: the
    same chroot (`appliance.build._root`/`in_root`), the same
    `--system-site-packages` venv (the package's Depends supplies python3-gi
    and python3-opengl at the system site level), and the same offline,
    hash-required `pip install --no-index` from the wheelhouse. Linux/root
    only -- `in_root` shells out to `chroot`.
    """
    if sys.platform != "linux":
        raise BuildError("venv_build_requires_linux")
    from appliance.build import in_root

    wheel_stage = root / "tmp/photo-wall-deb-wheels"
    if wheel_stage.exists():
        shutil.rmtree(wheel_stage)
    wheel_stage.mkdir(mode=0o700, parents=True)
    shutil.copyfile(wheelhouse / "requirements.txt", wheel_stage / "requirements.txt")
    shutil.copytree(wheelhouse / "wheels", wheel_stage / "wheels")
    venv_path = root / "opt/photo-wall/venv"
    if venv_path.exists():
        shutil.rmtree(venv_path)
    try:
        in_root(root, "python3.12", "-m", "venv", "--system-site-packages",
                "/opt/photo-wall/venv")
        in_root(root, "/opt/photo-wall/venv/bin/python", "-m", "pip", "install", "--no-index",
                "--require-hashes", "--find-links", "/tmp/photo-wall-deb-wheels/wheels", "-r",
                "/tmp/photo-wall-deb-wheels/requirements.txt",
                log=evidence / "deb-pip-install.log")
    finally:
        shutil.rmtree(wheel_stage, ignore_errors=True)
    return venv_path


def run_dpkg_deb(deb_root: Path, output: Path) -> Path:
    """Invoke the real `dpkg-deb --build`. Linux only."""
    if sys.platform != "linux":
        raise BuildError("dpkg_deb_requires_linux")
    if output.exists():
        raise BuildError("deb_output_exists")
    subprocess.run(
        ["dpkg-deb", "--build", "--root-owner-group", str(deb_root), str(output)],
        check=True, timeout=300,
    )
    return output


def build(repository: Path, revision: str, root: Path, output_dir: Path) -> Path:
    """End to end: wheelhouse -> venv (chroot) -> stage -> dpkg-deb.

    `root` is an already-assembled appliance base root (arm64/24.04), the
    same one `appliance/build.py:configure_root` uses. Linux/root only.
    """
    if sys.platform != "linux":
        raise BuildError("deb_build_requires_linux")
    from player.output_discovery import weston_ini as render_weston_ini

    output_dir = output_dir.resolve()
    with tempfile.TemporaryDirectory(prefix=".photo-wall-player-deb-", dir=output_dir) as tmp:
        tmp_path = Path(tmp)
        wheelhouse = tmp_path / "wheelhouse"
        inventory = build_player_wheelhouse(repository, revision, wheelhouse)
        version = package_version(inventory)
        evidence = tmp_path / "evidence"
        evidence.mkdir()
        venv_path = build_venv(root, wheelhouse, evidence)
        deb_root = tmp_path / "deb-root"
        stage_tree(deb_root, venv_source=venv_path, systemd_source=repository / "appliance/systemd",
                  weston_ini=render_weston_ini().encode(), version=version)
        assert_no_deployment_config(deb_root)
        output = output_dir / f"{PACKAGE}_{version}_{ARCHITECTURE}.deb"
        return run_dpkg_deb(deb_root, output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", required=True)
    parser.add_argument("--root", required=True, type=Path,
                        help="already-assembled arm64/24.04 appliance base root (chroot)")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        output = build(args.repository, args.revision, args.root, args.output_dir)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Player .deb build failed: {exc}\n")
    print(str(output))


if __name__ == "__main__":
    main()
