"""Build the Player application as a `.deb` package (0009 p4-deb-full-depends; Project 2 design
§2.7 rule 2, S5).

The `.deb` ships EXACTLY the Player's computed first-party import closure
(`scripts/module_closure.py` `PLAYER_POLICY`), staged PRIVATELY under `INSTALL_DIR` with a
generated `__main__.py` and `closure.json` (a PEP 441 directory application, run as `python3 -I
-B /usr/lib/photo-wall-player`). Nothing goes to `/usr/lib/python3/dist-packages`, so this
package and the bootstrapper `.deb` never own the same file and `dpkg` installs both on one
root. `Depends` is `debian_packages.packages("player")` -- the native render stack
(GTK/GStreamer/weston/Mesa) and the Python libraries (`python3-pydantic`, `python3-httpx`, ...).
The base already carries those packages (it is built from the same declaration); provisioning
installs the Player with `dpkg --install` alone -- nothing on the device resolves packages.

The closure's third-party imports must be declared in the declaration (`compute_closure` refuses
an undeclared one) and every declared import must be reached (`stage_tree` refuses
`unreached_imports`, exactly like the bootstrapper builder). Everything is computed over the
sources `git archive`d at the given revision (`fetch_tree`).

Also ships the two systemd units (`player.service`, `weston.service`) at
`/etc/systemd/system` with baked `.wants` enablement symlinks, `weston.ini` under
`/etc/xdg/weston`, and the `postinst` that creates the `wall` user -- exactly as before.

Pure Python, no venv, no wheelhouse, no arm64 chroot: it builds on any host with `dpkg-deb`.

Version scheme matches scripts/build_player.py: `{pyproject project.version}+g{revision}`, read
from the given Git revision (not the working tree) -- one reproducible identifier across every
artefact built from that commit. (scripts/build_bootstrapper_deb.py DELIBERATELY uses a different,
content-derived scheme; see that module's docstring.)

`control_file` and `run_dpkg_deb` are generic (parameterized on package, version, architecture,
Depends, maintainer and description); `fetch_tree` computes the git-archived tree both `.deb`
builders stage from; `assert_declaration_matches` refuses a revision whose
`scripts/debian_packages.py` differs from the one the builder imported. All four are REUSED
verbatim by scripts/build_bootstrapper_deb.py (which imports them); do not move or duplicate them.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path, PurePosixPath
from typing import Final

from packaging.version import Version

from scripts import debian_packages
from scripts.build_player import BuildError
from scripts.debian_packages import packages
from scripts.device_root_checks import watchdog_overrides
from scripts.module_closure import (
    PLAYER_POLICY,
    Closure,
    ClosureError,
    closure_for,
    stage_application,
    unreached_imports,
)

PACKAGE = "photo-wall-player"
# The private install dir (design §2.7, rule 2): the computed closure, a generated
# __main__.py and closure.json, run as `python3 -I -B /usr/lib/photo-wall-player`.
INSTALL_DIR: Final = PurePosixPath("/usr/lib/photo-wall-player")
# The `.deb` now ships only interpreter-agnostic `.py` files and config -- no
# compiled/arch-specific content (contrast the old prebuilt-venv `.deb`, which
# was `arm64`). The native, arch-specific dependencies come from the base via
# `Depends`, resolved by apt on the target; the correct Debian Architecture for
# a package shipping only `.py` files plus units is "all" (same as the
# bootstrapper `.deb`).
ARCHITECTURE = "all"
MAINTAINER = "Photo Wall <photo-wall@localhost>"
DESCRIPTION = (
    "Photo Wall Player application: its computed first-party import closure staged privately "
    "under /usr/lib/photo-wall-player, plus the player and weston systemd units. Its full "
    "runtime dependency set (GTK/GStreamer/weston/Mesa and the Python libraries) is declared as "
    "apt Depends; the base carries them from the same declaration (0009, p4-deb-full-depends). "
    "Deployment configuration and central_origin are supplied at boot by the "
    "bootstrapper/central, never baked into this package."
)

# The systemd units this package ships, and the "photo-wall-" prefix the
# appliance build already uses for the same units.
UNIT_FILES = ("player.service", "weston.service")

# The full runtime dependency set (the native render stack and the distro
# `python3-*` libraries) is NOT written here: the control file's Depends is
# `debian_packages.packages("player")`, the Debian declaration's one list
# (scripts/debian_packages.py), which also builds the base that carries them.

# The bare PyPI project names a *vendored wheel* would have used. The distro
# `python3-*` names in the declaration are permitted; these bare roots
# are forbidden in Depends because their presence would imply a vendored wheel
# (the model this slice retires). The guard matches on exact name equality, so
# `python3-pydantic` passes while a bare `pydantic` is rejected.
_PYPI_ROOTS = frozenset({"pydantic", "httpx", "websockets", "cryptography", "zeroconf"})

DECLARATION: Final = "scripts/debian_packages.py"
# The only files outside the first-party packages that fetch_tree archives; a builder may
# read nothing else from the tree (tests/test_build_trees.py enforces it).
ARCHIVED_FILES: Final = (DECLARATION, "pyproject.toml")

# The first-party packages, the declaration and pyproject.toml (~1.4 MiB today); guards
# against archiving an unexpectedly huge tree.
MAX_SOURCE = 16 * 1024 * 1024


def package_version(project_version: str, revision: str) -> str:
    """The .deb version is `{pyproject version}+g{revision}` -- one reproducible identifier
    across every artefact built from that commit (deliberately unlike
    scripts/build_bootstrapper_deb.py's content-derived scheme; see that module's docstring)."""
    base_version = Version(project_version)
    if base_version.local:
        raise BuildError("unsupported project version")
    return f"{base_version}+g{revision}"


def control_file(version: str, depends: tuple[str, ...], *, package: str = PACKAGE,
                 architecture: str = ARCHITECTURE, maintainer: str = MAINTAINER,
                 description: str = DESCRIPTION) -> bytes:
    """Generate the Debian control file for the given version and Depends.

    Refuses to emit a control file naming a bare vendored-PyPI project (those
    would imply a vendored wheel, the model this slice retires); the distro
    `python3-*` package names are permitted (they resolve from apt).
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

    dpkg-deb cannot create a system user by unpacking files alone, so this is
    the one maintainer script this package needs. Installing the code itself
    remains a pure unpack -- this script only provisions the identity the
    shipped units run as.

    The device groups are required, never skipped: player.service names render and video as
    SupplementaryGroups, and a missing one fails every start at spawn (216/GROUP). The groups
    come from udev, a Depends, so a missing one stops the install here, named.
    """
    return (
        "#!/bin/sh\n"
        "set -e\n"
        "if ! getent passwd wall >/dev/null; then\n"
        "  useradd --uid 10001 --user-group --no-create-home "
        "--shell /usr/sbin/nologin wall\n"
        "fi\n"
        "for group in video render input; do\n"
        "  if ! getent group \"$group\" >/dev/null; then\n"
        "    echo \"photo-wall-player: group $group does not exist (udev creates it)\" >&2\n"
        "    exit 1\n"
        "  fi\n"
        "  usermod -a -G \"$group\" wall\n"
        "done\n"
        "systemctl daemon-reload || true\n"
    ).encode()


def fetch_tree(repository: Path, revision: str, into: Path) -> None:
    """git archive of the first-party packages (every top-level directory holding an
    `__init__.py` at `revision`), scripts/debian_packages.py and pyproject.toml at `revision`,
    extracted into `into`, so the closure and the Depends are computed over exactly the
    committed sources the package ships from. `revision` must be an explicit full commit.

    REUSED verbatim by scripts/build_bootstrapper_deb.py -- do not duplicate it there.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise BuildError("explicit full Git commit required")
    repository = repository.resolve(strict=True)
    listing = subprocess.run(
        ["git", "-C", str(repository), "ls-tree", "-r", "--name-only", "-z", revision],
        check=True, capture_output=True, timeout=30,
    ).stdout.decode().split("\0")
    first_party = sorted({name.partition("/")[0] for name in listing
                          if re.fullmatch(r"[^/]+/__init__\.py", name)})
    wanted = (*first_party, *ARCHIVED_FILES)
    archive = subprocess.run(
        ["git", "-C", str(repository), "archive", "--format=tar", revision, "--", *wanted],
        check=True, capture_output=True, timeout=60,
    ).stdout
    if len(archive) > MAX_SOURCE:
        raise BuildError("source archive too large")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        members = tar.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            inside = (path.parts[0] in first_party
                      or member.name in (*ARCHIVED_FILES, "scripts"))
            if (path.is_absolute() or ".." in path.parts or not inside
                    or not (member.isfile() or member.isdir())):
                raise BuildError("unexpected or nonregular source member")
        into.mkdir(parents=True)
        tar.extractall(into, members=members, filter="data")


def assert_declaration_matches(tree: Path) -> None:
    """Refuses a revision whose `scripts/debian_packages.py` differs from the one this builder
    imported: a closure policy's third-party table and a package's Depends must reflect exactly
    the fetched revision's declaration, never a stale one. Shared by both `.deb` builders."""
    if (tree / DECLARATION).read_bytes() != Path(debian_packages.__file__).read_bytes():
        raise BuildError("declaration_differs_from_revision")


def stage_tree(deb_root: Path, *, closure: Closure, tree: Path, systemd_source: Path,
               weston_ini: bytes, version: str, native_client: Path | None = None,
               architecture: str = ARCHITECTURE) -> None:
    """Assemble the `.deb` staging tree.

    Refuses a closure that leaves a declared import unreached (the declaration would put an
    unused package in Depends and on the base) BEFORE creating `deb_root`, exactly as the
    bootstrapper builder. Stages the computed closure PRIVATELY under `INSTALL_DIR`
    (`stage_application`: the files, a generated `__main__.py`, `closure.json`) and the units at
    `/etc/systemd/system`, shipped already enabled via `.wants` symlinks (a symlink is data, not
    a maintainer script, so "install is an unpack" still holds for activation). Carries NO
    `/etc/photo-wall` deployment configuration and NO `central_origin` -- per 0009 those arrive
    at boot from the bootstrapper/central, never from a published asset. NO venv: the runtime
    dependencies are the package's `Depends`, `debian_packages.packages("player")`.
    """
    if deb_root.exists():
        raise BuildError("stage_root_exists")
    unreached = unreached_imports(closure, PLAYER_POLICY)
    if unreached:
        raise BuildError(f"declared_import_unreached:{','.join(unreached)}")
    deb_root.mkdir(parents=True)

    debian = deb_root / "DEBIAN"
    debian.mkdir(mode=0o755)
    control_path = debian / "control"
    control_path.write_bytes(control_file(version, packages("player"), architecture=architecture))
    control_path.chmod(0o644)
    postinst_path = debian / "postinst"
    postinst_path.write_bytes(postinst_script())
    postinst_path.chmod(0o755)

    stage_application(closure, PLAYER_POLICY, repo=tree,
                      into=deb_root / INSTALL_DIR.relative_to("/"))

    if native_client is not None:
        blob = native_client.read_bytes()
        machine = {"arm64": 183, "amd64": 62}.get(architecture)
        if machine is None or len(blob) < 64 or blob[:6] != b"\x7fELF\x02\x01" or int.from_bytes(blob[18:20], "little") != machine:
            raise BuildError("native_client_elf_architecture")
        client = deb_root / "usr/lib/photo-wall-client/libphoto-wall-frame-client.so"
        client.parent.mkdir(parents=True)
        client.write_bytes(blob)
        client.chmod(0o644)

    units_dir = deb_root / "etc/systemd/system"
    units_dir.mkdir(parents=True)
    wants_dir = units_dir / "multi-user.target.wants"
    wants_dir.mkdir()
    for name in UNIT_FILES:
        target_name = "photo-wall-" + name
        target = units_dir / target_name
        target.write_bytes((systemd_source / name).read_bytes())
        target.chmod(0o644)
        (wants_dir / target_name).symlink_to("/etc/systemd/system/" + target_name)

    weston_dir = deb_root / "etc/xdg/weston"
    weston_dir.mkdir(parents=True)
    (weston_dir / "weston.ini").write_bytes(weston_ini)


_FORBIDDEN_CONFIG_NAMES = frozenset({"public.json", "bootstrap.json", "ca.pem", "release.pub.pem"})


def assert_no_deployment_config(deb_root: Path) -> None:
    """0009 invariant: no application config, no central_origin, and (this
    slice) no leaked venv or dist-packages file in the `.deb`.

    Configuration (Frame binding, assignments, calibration, central_origin)
    arrives at boot from the bootstrapper/central -- never baked into a
    published asset. A staged `/opt/photo-wall/venv` would mean the venv/
    wheelhouse this slice dropped had crept back in; forbid it too. A staged
    `/usr/lib/python3/dist-packages` file would mean the closure leaked outside its
    private directory -- the invariant that keeps this `.deb` and the bootstrapper `.deb` from
    ever owning the same file. No systemd manager setting of the watchdog either: stage 1's
    /run drop-in is the only one (design §2.8).
    """
    if (deb_root / "opt/photo-wall/venv").exists():
        raise BuildError("venv_in_deb")
    overrides = watchdog_overrides(deb_root)
    if overrides:
        raise BuildError(f"watchdog_override_in_deb:{';'.join(overrides)}")
    if (deb_root / "etc/photo-wall").exists():
        raise BuildError("deployment_config_in_deb")
    if (deb_root / "usr/lib/python3/dist-packages").exists():
        raise BuildError("dist_packages_in_deb")
    for path in deb_root.rglob("*"):
        if path.name in _FORBIDDEN_CONFIG_NAMES or path.name == "central_origin":
            raise BuildError("deployment_config_in_deb")


def run_dpkg_deb(deb_root: Path, output: Path) -> Path:
    """Invoke the real `dpkg-deb --build`. Linux only (needs `dpkg-deb`).

    REUSED verbatim by scripts/build_bootstrapper_deb.py -- do not change its
    signature without updating that caller.
    """
    if sys.platform != "linux":
        raise BuildError("dpkg_deb_requires_linux")
    if output.exists():
        raise BuildError("deb_output_exists")
    subprocess.run(
        ["dpkg-deb", "--build", "--root-owner-group", str(deb_root), str(output)],
        check=True, timeout=300,
    )
    return output


def build(repository: Path, revision: str, output_dir: Path, *,
          native_client: Path | None = None, architecture: str = ARCHITECTURE) -> Path:
    """End to end: `fetch_tree` the committed sources at `revision` -> refuse a stale declaration
    (`assert_declaration_matches`) -> compute the closure -> stage -> `dpkg-deb`.

    No arm64 chroot and no `--root`: the `.deb` carries only `.py` files, units and config, so it
    builds on any host with `dpkg-deb` (like the bootstrapper `.deb`). `weston.ini` is generated
    from the running player package, matching the previous builder's behaviour; the systemd
    units come from the COMMITTED `appliance/systemd`, not the working tree's.
    """
    from player.output_discovery import weston_ini as render_weston_ini

    output_dir = output_dir.resolve()
    repository = repository.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix=".photo-wall-player-deb-", dir=output_dir) as tmp:
        tree = Path(tmp) / "tree"
        fetch_tree(repository, revision, tree)
        assert_declaration_matches(tree)
        closure = closure_for(PLAYER_POLICY, repo=tree)
        project = tomllib.loads((tree / "pyproject.toml").read_text())
        version = package_version(project["project"]["version"], revision)
        if native_client is not None:
            version += ".client" + hashlib.sha256(native_client.read_bytes()).hexdigest()[:12]
        deb_root = Path(tmp) / "deb-root"
        stage_tree(deb_root, closure=closure, tree=tree,
                   systemd_source=tree / "appliance/systemd",
                   weston_ini=render_weston_ini().encode(), version=version,
                   native_client=native_client, architecture=architecture)
        assert_no_deployment_config(deb_root)
        output = output_dir / f"{PACKAGE}_{version}_{architecture}.deb"
        return run_dpkg_deb(deb_root, output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--native-client", type=Path)
    parser.add_argument("--architecture", choices=("all", "arm64", "amd64"), default=ARCHITECTURE)
    args = parser.parse_args()
    try:
        output = build(args.repository, args.revision, args.output_dir, native_client=args.native_client, architecture=args.architecture)
    except (ValueError, OSError, subprocess.SubprocessError, ClosureError) as exc:
        parser.exit(1, f"Player .deb build failed: {exc}\n")
    print(str(output))


if __name__ == "__main__":
    main()
