"""Build the base bootstrapper (`appliance.provision`) as a `.deb` package.

Per the owner directive that ALL of this repo's own code must be built as a
portable `.deb` so it migrates cleanly across image-build tool chains, the
bootstrapper -- previously baked into the rpi-image-gen base OS via a
raw-file rsync overlay (`appliance/rpi_image_gen/stage_overlay.sh` +
`layer/photo-wall-bootstrapper.rootfs-overlay/`) -- is built here exactly
like the Player app already is (`scripts/build_player_deb.py`, 0009 slice
4): a `.deb` the image-build tool installs, never a tool-coupled overlay it
owns.

Contents (fixed list, mirrors `appliance/build.py`'s own
`BASE_MINIMAL_MODULES` plus `player/discovery.py`, which
`appliance/rpi_image_gen/stage_overlay.sh`'s docstring already establishes as
part of this closure -- the `Bootstrapper.discovery` Protocol
`appliance/provision.py`'s class docstring documents matches
`player.discovery.CentralDiscovery` even though `provision.py` does not
import it directly):

- `appliance/__init__.py`, `appliance/provision.py`
- `player/__init__.py`, `player/mdns_discovery.py`, `player/discovery.py`
- `appliance/systemd/photo-wall-provision.service`

NO `contracts` module is staged: grepping `appliance/provision.py`'s actual
imports (its module docstring, "Reuse and the import-boundary decision")
shows it imports no `contracts` submodule at all in this slice -- already
recorded in `.claude/errata.md` (`p3-base-bootstrapper`: "the boot-context/
contracts.equipment production is explicitly out of scope for this
bootstrapper"). A "minimal contracts closure" to stage is therefore empty,
not merely small.

Reuse decision (shared-vs-parallel)
------------------------------------
`scripts/build_player_deb.py`'s `control_file` and `run_dpkg_deb` are
already fully generic -- parameterized on package/version/architecture/
depends/maintainer/description, with no Player-specific behaviour that fires
for this package's inputs (its one Player-specific guard, rejecting a
vendored-PyPI-root name in Depends, never trips here: this package's
Depends are Debian package names -- `python3-zeroconf`, not `zeroconf` --
which never match `_PYPI_ROOTS`). Importing them directly is therefore
reuse, not duplication, and touches zero lines of the tested, working
Player builder -- preferred here over extracting a new shared module, which
would only move two already-generic functions sideways for no behavioural
gain and would risk the Player `.deb`'s passing test suite for a
one-file win.

Unlike the Player `.deb`, this package is pure Python (stdlib + the
`player.mdns_discovery` import of `zeroconf`, satisfied at runtime by the
distro's `python3-zeroconf`/`python3-ifaddr`, never vendored) -- no venv, no
wheelhouse, no arm64 chroot. It can be built on any host with `dpkg-deb`
(gated identically to the Player builder's Tier 2, see the "gated" banner
below) and needs no `--root`.

Version scheme matches `scripts/build_player.py`/`scripts/build_player_deb.py`
exactly: `{pyproject project.version}+g{revision}`, read from the given
Git revision (not the working tree), for one reproducible identifier across
every artifact built from that commit.
"""

from __future__ import annotations

import argparse
import io
import re
import subprocess
import tarfile
import tempfile
import tomllib
from pathlib import Path, PurePosixPath

from packaging.version import Version

from scripts.build_player import BuildError
from scripts.build_player_deb import MAINTAINER as _PLAYER_MAINTAINER
from scripts.build_player_deb import control_file, run_dpkg_deb

PACKAGE = "photo-wall-bootstrapper"
# Pure Python, no compiled/arch-specific content (contrast the Player .deb's
# arm64 venv) -- the correct Debian Architecture for a package shipping only
# .py files and a systemd unit is "all".
ARCHITECTURE = "all"
MAINTAINER = _PLAYER_MAINTAINER
DESCRIPTION = (
    "Photo Wall base bootstrapper: discovers central by mDNS, fetches and "
    "installs the Player app .deb, and hands off the resolved origin "
    "(appliance.provision, 0009 slice 2). Carries no application or "
    "render-stack package -- that is the Player .deb's own Depends."
)

# The Debian package names this closure's one third-party import needs
# (player/mdns_discovery.py's `import zeroconf`) -- never the render stack
# (GTK/GStreamer/weston), which is the Player .deb's Depends, not this one's.
DEB_DEPENDS = ("python3", "python3-ifaddr", "python3-zeroconf")

UNIT_NAME = "photo-wall-provision.service"

# (repo-relative source path, dist-packages-relative destination path).
_MODULE_FILES = (
    ("appliance/__init__.py", "appliance/__init__.py"),
    ("appliance/provision.py", "appliance/provision.py"),
    ("player/__init__.py", "player/__init__.py"),
    ("player/mdns_discovery.py", "player/mdns_discovery.py"),
    ("player/discovery.py", "player/discovery.py"),
)

MAX_SOURCE = 256 * 1024


def package_version(sources: dict[str, bytes], revision: str) -> str:
    """The .deb version is `{pyproject version}+g{revision}` -- the same
    scheme `scripts/build_player.py:build` uses for the Player wheel/`.deb`,
    read from the given revision's `pyproject.toml`, not the working tree."""
    project = tomllib.loads(sources["pyproject.toml"].decode())
    base_version = Version(project["project"]["version"])
    if base_version.local:
        raise BuildError("unsupported project version")
    return f"{base_version}+g{revision}"


def fetch_sources(repository: Path, revision: str) -> dict[str, bytes]:
    """`git archive` the exact fixed file list this package ships, from
    `revision` (not the working tree) -- an explicit full commit, exactly
    like `scripts/build_player.py:build`'s own requirement."""
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise BuildError("explicit full Git commit required")
    repository = repository.resolve(strict=True)
    wanted = tuple(source for source, _dest in _MODULE_FILES) + ("pyproject.toml",)
    result = subprocess.run(
        ["git", "-C", str(repository), "archive", "--format=tar", revision, *wanted],
        check=True, capture_output=True, timeout=30,
    )
    archive = result.stdout
    if len(archive) > MAX_SOURCE:
        raise BuildError("source archive too large")
    sources: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar:
            if member.isdir():
                # `git archive` on an explicit file path also emits its
                # parent directory members (e.g. "appliance/") -- harmless,
                # skipped rather than treated as an unexpected member.
                continue
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or member.name not in wanted
                    or not member.isfile() or member.name in sources):
                raise BuildError("unexpected or nonregular source member")
            sources[member.name] = tar.extractfile(member).read()
    missing = set(wanted) - sources.keys()
    if missing:
        raise BuildError(f"missing_source:{','.join(sorted(missing))}")
    return sources


def stage_tree(deb_root: Path, *, sources: dict[str, bytes], unit: bytes, version: str,
               depends: tuple[str, ...] = DEB_DEPENDS) -> None:
    """Assemble the `.deb` staging tree.

    Places the module closure at `/usr/lib/python3/dist-packages` -- the
    same distro `python3` default `sys.path` entry
    `appliance/build.py:configure_root_base` already uses for the identical
    Ubuntu-base closure -- and the unit at `/lib/systemd/system` (the vendor
    unit-file path, `systemctl`'s own search path alongside
    `/etc/systemd/system`), enabled via a baked `.wants` symlink under
    `/etc/systemd/system/multi-user.target.wants/` rather than a `postinst`
    calling `systemctl enable`: this package is unpacked by `apt-get
    install`/`dpkg -i` *inside an image-build chroot with no systemd PID1
    running* (the rpi-image-gen customize-hook), where a maintainer script
    shelling out to `systemctl` would have nothing to talk to. A symlink is
    data, not a maintainer script -- the same "install is an unpack"
    reasoning `scripts/build_player_deb.py:stage_tree` already applies to
    its own two units.

    No `/etc/photo-wall` deployment config, no venv: this package carries
    only the bootstrapper's own minimal Python module closure and its unit.
    """
    if deb_root.exists():
        raise BuildError("stage_root_exists")
    deb_root.mkdir(parents=True)

    debian = deb_root / "DEBIAN"
    debian.mkdir(mode=0o755)
    control_path = debian / "control"
    control_path.write_bytes(control_file(
        version, depends, package=PACKAGE, architecture=ARCHITECTURE,
        maintainer=MAINTAINER, description=DESCRIPTION,
    ))
    control_path.chmod(0o644)

    dist_packages = deb_root / "usr/lib/python3/dist-packages"
    for source, dest in _MODULE_FILES:
        target = dist_packages / dest
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        target.write_bytes(sources[source])
        target.chmod(0o644)

    units_dir = deb_root / "lib/systemd/system"
    units_dir.mkdir(parents=True)
    unit_path = units_dir / UNIT_NAME
    unit_path.write_bytes(unit)
    unit_path.chmod(0o644)

    wants_dir = deb_root / "etc/systemd/system/multi-user.target.wants"
    wants_dir.mkdir(parents=True)
    (wants_dir / UNIT_NAME).symlink_to("/lib/systemd/system/" + UNIT_NAME)


def assert_minimal(deb_root: Path) -> None:
    """No venv, no application config, no `contracts` module -- this
    package's whole point is staying the minimal thing the image-build tool
    installs before any app exists (0009's "the base OS carries no
    application" rule extends to this .deb: it is not the app either)."""
    if (deb_root / "opt/photo-wall").exists():
        raise BuildError("venv_in_bootstrapper_deb")
    if (deb_root / "etc/photo-wall").exists():
        raise BuildError("deployment_config_in_bootstrapper_deb")
    if (deb_root / "usr/lib/python3/dist-packages/contracts").exists():
        raise BuildError("contracts_in_bootstrapper_deb")


def build(repository: Path, revision: str, output_dir: Path) -> Path:
    """End to end: fetch the fixed source closure at `revision` -> stage ->
    `dpkg-deb`. No arm64 chroot needed -- pure Python, no compiled content."""
    output_dir = output_dir.resolve()
    repository = repository.resolve(strict=True)
    sources = fetch_sources(repository, revision)
    version = package_version(sources, revision)
    unit = (repository / "appliance/systemd" / UNIT_NAME).read_bytes()
    with tempfile.TemporaryDirectory(prefix=".photo-wall-bootstrapper-deb-", dir=output_dir) as tmp:
        deb_root = Path(tmp) / "deb-root"
        stage_tree(deb_root, sources=sources, unit=unit, version=version)
        assert_minimal(deb_root)
        output = output_dir / f"{PACKAGE}_{version}_{ARCHITECTURE}.deb"
        return run_dpkg_deb(deb_root, output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        output = build(args.repository, args.revision, args.output_dir)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Bootstrapper .deb build failed: {exc}\n")
    print(str(output))


if __name__ == "__main__":
    main()
