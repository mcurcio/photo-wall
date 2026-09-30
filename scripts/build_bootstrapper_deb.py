"""Build the base bootstrapper (`appliance.provision`) as a `.deb` package.

Per the owner directive that ALL of this repo's own code ships as a portable `.deb` (never a
raw-file overlay an image-build tool owns), the bootstrapper is built like the Player app
(`scripts/build_player_deb.py`): a `.deb` the image build installs.

Contents (Project 2 design §2.7, rule 2: each package ships exactly its computed closure,
privately):

- the computed first-party closure of `appliance.provision`
  (`scripts/module_closure.py` `BOOTSTRAPPER_POLICY`), staged under INSTALL_DIR with a generated
  `__main__.py` and `closure.json` (Debian Python Policy's private-module directory; a PEP 441
  directory application run as `python3 -I -B /usr/lib/photo-wall-bootstrapper`). Nothing goes
  to `/usr/lib/python3/dist-packages`, so no file is shared with the Player `.deb`, and the
  `contracts` and `uplink` modules the closure reaches ship with it;
- `appliance/systemd/photo-wall-provision.service`, enabled by a baked `.wants` symlink.

`Depends` is `debian_packages.packages("bootstrapper")`, the declaration's one list. The
closure's third-party imports must be declared there (`compute_closure` refuses an undeclared
one) and every declared import must be reached (`stage_tree` refuses `unreached_imports`).
Everything is computed over the sources `git archive`d at the given revision (`fetch_tree`).

Reuse: `scripts/build_player_deb.py`'s `control_file` and `run_dpkg_deb` are generic
(parameterized on package, version, architecture, Depends, maintainer and description);
`fetch_tree` computes the git-archived tree both `.deb` builders stage from; and
`assert_declaration_matches` refuses a revision whose `scripts/debian_packages.py` differs from
the one the builder imported. All four are imported here rather than duplicated.

Pure Python, no venv, no wheelhouse, no arm64 chroot: it builds on any host with `dpkg-deb`.

Version scheme (DELIBERATELY unlike the Player `.deb`)
------------------------------------------------------
The Player `.deb` keeps `{pyproject version}+g{revision}`. This package is baked into the
rpi-image-gen base squashfs, which is content-addressed and cached in
`.github/workflows/base-image.yml`; a per-commit suffix would change the `.deb` on every commit
even when nothing it ships changed. So its version is `{pyproject project.version}+{12 hex}`
over what the package ships: the closure digest, the install directory, the systemd unit and
the Depends. The LABEL is commit-independent; the built bytes are reproducible only when
`SOURCE_DATE_EPOCH` is exported for `dpkg-deb --build` (the base-image workflow sets it).
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import tempfile
import tomllib
from pathlib import Path, PurePosixPath
from typing import Final

from packaging.version import Version

from contracts.player_payload import base_abi
from player.output_discovery import weston_ini as render_weston_ini
from scripts.build_player import BuildError
from scripts.build_player_deb import MAINTAINER as _PLAYER_MAINTAINER
from scripts.build_player_deb import (
    assert_declaration_matches,
    control_file,
    fetch_tree,
    postinst_script,
    run_dpkg_deb,
)
from scripts.debian_packages import PIN, packages
from scripts.device_root_checks import watchdog_overrides
from scripts.module_closure import (
    BOOTSTRAPPER_POLICY,
    Closure,
    ClosureError,
    closure_for,
    stage_application,
    unreached_imports,
)

PACKAGE = "photo-wall-bootstrapper"
# Pure Python, no compiled/arch-specific content (contrast the Player .deb's
# arm64 venv) -- the correct Debian Architecture for a package shipping only
# .py files and a systemd unit is "all".
ARCHITECTURE = "all"
MAINTAINER = _PLAYER_MAINTAINER
DESCRIPTION = (
    "Photo Wall base bootstrapper: finds central, fetches and installs the "
    "Player app .deb, and hands off to it (appliance.provision). Carries no "
    "application or render-stack package -- that is the Player .deb's own "
    "Depends."
)
INSTALL_DIR: Final = PurePosixPath("/usr/lib/photo-wall-bootstrapper")
UNIT_NAME = "photo-wall-provision.service"
AGENT_UNIT_NAME = "photo-wall-os-agent.service"
AGENT_LAUNCHER = (b"import runpy, sys\n"
                  b"sys.path.insert(0, '/usr/lib/photo-wall-bootstrapper')\n"
                  b"runpy.run_module('appliance.os_agent', run_name='__main__')\n")
PLAYER_LAUNCHER = (b"import runpy, sys\n"
                   b"sys.path.insert(0, '/usr/lib/photo-wall-bootstrapper')\n"
                   b"runpy.run_module('appliance.app_launcher', run_name='__main__')\n")
PLAYER_UNIT_NAME = "photo-wall-player.service"
WESTON_UNIT_NAME = "photo-wall-weston.service"
ABI_FILE = "base-abi.txt"
_LEGACY_EXEC = (b"ExecStart=/usr/bin/python3 -I -B /usr/lib/photo-wall-player "
                b"--config /etc/photo-wall/public.json")
_BASE_EXEC = b"ExecStart=/usr/bin/python3 -I -B /usr/lib/photo-wall-bootstrapper/player-launch.py"
DEFAULT_AGENT_UNIT = (Path(__file__).resolve().parents[1] / "appliance/systemd" /
                      AGENT_UNIT_NAME).read_bytes()


def managed_player_unit(legacy: bytes) -> bytes:
    if legacy.count(_LEGACY_EXEC) != 1:
        raise BuildError("player_unit_launcher_contract_changed")
    return legacy.replace(_LEGACY_EXEC, _BASE_EXEC)


DEFAULT_PLAYER_UNIT = managed_player_unit(
    (Path(__file__).resolve().parents[1] / "appliance/systemd/player.service").read_bytes())
DEFAULT_WESTON_UNIT = (
    Path(__file__).resolve().parents[1] / "appliance/systemd/weston.service").read_bytes()
DEFAULT_WESTON_INI = render_weston_ini().encode()


def launcher_contract_digest(tree: Path) -> str:
    """Hash the committed base-owned execution bytes that an app payload depends on."""
    sources = (
        ("app_launcher", (tree / "appliance/app_launcher.py").read_bytes()),
        ("app_executor", (tree / "appliance/app_executor.py").read_bytes()),
        ("app_payload", (tree / "appliance/app_payload.py").read_bytes()),
        ("player_unit", managed_player_unit(
            (tree / "appliance/systemd/player.service").read_bytes())),
        ("player_launcher", PLAYER_LAUNCHER),
    )
    hasher = hashlib.sha256()
    for name, data in sources:
        for field in (name.encode(), data):
            hasher.update(len(field).to_bytes(8, "big"))
            hasher.update(field)
    return "sha256:" + hasher.hexdigest()


def base_abi_bytes(tree: Path | None = None) -> bytes:
    source = tree or Path(__file__).resolve().parents[1]
    return (base_abi(PIN.snapshot, packages("player"), launcher_contract_digest(source))
            + "\n").encode()


def package_version(closure: Closure, unit: bytes, project_version: str, *,
                    agent_unit: bytes = DEFAULT_AGENT_UNIT,
                    player_unit: bytes = DEFAULT_PLAYER_UNIT,
                    weston_unit: bytes = DEFAULT_WESTON_UNIT,
                    weston_ini: bytes = DEFAULT_WESTON_INI,
                    tree: Path | None = None) -> str:
    """`{project_version}+{12 hex}`: a CONTENT-DERIVED suffix over what the package ships --
    the closure digest (every staged file's path and bytes), the install directory, the unit's
    name and bytes, and the Depends -- NOT the git revision (see the module docstring,
    "Version scheme"). Fields are length-prefixed so no concatenation of distinct inputs can
    alias."""
    base_version = Version(project_version)
    if base_version.local:
        raise BuildError("unsupported project version")
    hasher = hashlib.sha256(str(base_version).encode())
    for field in (closure.digest.encode(), str(INSTALL_DIR).encode(), UNIT_NAME.encode(), unit,
                  AGENT_UNIT_NAME.encode(), agent_unit, AGENT_LAUNCHER,
                  PLAYER_UNIT_NAME.encode(), player_unit, WESTON_UNIT_NAME.encode(),
                  weston_unit, weston_ini, PLAYER_LAUNCHER, base_abi_bytes(tree),
                  postinst_script(),
                  "\n".join(packages("bootstrapper")).encode()):
        hasher.update(b"\x00" + str(len(field)).encode() + b"\x00" + field)
    return f"{base_version}+{hasher.hexdigest()[:12]}"


def stage_tree(deb_root: Path, *, closure: Closure, tree: Path, unit: bytes,
               version: str, agent_unit: bytes = DEFAULT_AGENT_UNIT,
               player_unit: bytes = DEFAULT_PLAYER_UNIT,
               weston_unit: bytes = DEFAULT_WESTON_UNIT,
               weston_ini: bytes = DEFAULT_WESTON_INI) -> None:
    """Assemble the `.deb` staging tree.

    `stage_application(closure, BOOTSTRAPPER_POLICY, repo=tree, into=deb_root / INSTALL_DIR)`;
    the control file with Depends = packages("bootstrapper"); refuses a closure that leaves a
    declared import unreached (the declaration would put an unused package in Depends and on
    the base). The unit goes to `/lib/systemd/system` (the vendor unit path), enabled by a
    baked `.wants` symlink rather than a `postinst` calling `systemctl enable`: this package is
    unpacked inside an image-build chroot with no systemd PID1 running (the rpi-image-gen
    customize-hook), and a symlink is data, not a maintainer script.

    No `/etc/photo-wall` deployment config, no venv, nothing in dist-packages.
    """
    if deb_root.exists():
        raise BuildError("stage_root_exists")
    unreached = unreached_imports(closure, BOOTSTRAPPER_POLICY)
    if unreached:
        raise BuildError(f"declared_import_unreached:{','.join(unreached)}")
    deb_root.mkdir(parents=True)

    debian = deb_root / "DEBIAN"
    debian.mkdir(mode=0o755)
    control_path = debian / "control"
    control_path.write_bytes(control_file(
        version, packages("bootstrapper"), package=PACKAGE, architecture=ARCHITECTURE,
        maintainer=MAINTAINER, description=DESCRIPTION,
    ))
    control_path.chmod(0o644)
    postinst = debian / "postinst"
    postinst.write_bytes(postinst_script())
    postinst.chmod(0o755)

    stage_application(closure, BOOTSTRAPPER_POLICY, repo=tree,
                      into=deb_root / INSTALL_DIR.relative_to("/"))
    agent_launcher = deb_root / INSTALL_DIR.relative_to("/") / "os-agent.py"
    agent_launcher.write_bytes(AGENT_LAUNCHER)
    agent_launcher.chmod(0o644)
    player_launcher = deb_root / INSTALL_DIR.relative_to("/") / "player-launch.py"
    player_launcher.write_bytes(PLAYER_LAUNCHER)
    player_launcher.chmod(0o644)
    abi_path = deb_root / INSTALL_DIR.relative_to("/") / ABI_FILE
    abi_path.write_bytes(base_abi_bytes(tree))
    abi_path.chmod(0o644)

    units_dir = deb_root / "lib/systemd/system"
    units_dir.mkdir(parents=True)
    unit_path = units_dir / UNIT_NAME
    unit_path.write_bytes(unit)
    unit_path.chmod(0o644)
    agent_unit_path = units_dir / AGENT_UNIT_NAME
    agent_unit_path.write_bytes(agent_unit)
    agent_unit_path.chmod(0o644)
    for name, data in ((PLAYER_UNIT_NAME, player_unit),
                       (WESTON_UNIT_NAME, weston_unit)):
        target = units_dir / name
        target.write_bytes(data)
        target.chmod(0o644)
    weston_config = deb_root / "etc/xdg/weston/weston.ini"
    weston_config.parent.mkdir(parents=True)
    weston_config.write_bytes(weston_ini)
    weston_config.chmod(0o644)

    wants_dir = deb_root / "etc/systemd/system/multi-user.target.wants"
    wants_dir.mkdir(parents=True)
    (wants_dir / UNIT_NAME).symlink_to("/lib/systemd/system/" + UNIT_NAME)
    (wants_dir / AGENT_UNIT_NAME).symlink_to("/lib/systemd/system/" + AGENT_UNIT_NAME)


def assert_minimal(deb_root: Path) -> None:
    """No venv, no application config, nothing under `usr/lib/python3/dist-packages` (the
    private-directory rule: a module there would be a file the Player `.deb` could also own).
    0009's "the base OS carries no application" rule extends to this .deb: it is not the app
    either. `contracts` is allowed: `uplink` and `appliance.bootstrap` import it. No systemd
    manager setting of the watchdog: stage 1's /run drop-in is the only one (design §2.8)."""
    if (deb_root / "opt/photo-wall").exists():
        raise BuildError("venv_in_bootstrapper_deb")
    if (deb_root / "etc/photo-wall").exists():
        raise BuildError("deployment_config_in_bootstrapper_deb")
    if (deb_root / "usr/lib/python3/dist-packages").exists():
        raise BuildError("dist_packages_in_bootstrapper_deb")
    overrides = watchdog_overrides(deb_root)
    if overrides:
        raise BuildError(f"watchdog_override_in_bootstrapper_deb:{';'.join(overrides)}")


def build(repository: Path, revision: str, output_dir: Path) -> Path:
    """End to end: fetch the committed tree at `revision` -> compute the closure -> stage ->
    `dpkg-deb`. No arm64 chroot needed -- pure Python, no compiled content."""
    output_dir = output_dir.resolve()
    repository = repository.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix=".photo-wall-bootstrapper-deb-", dir=output_dir) as tmp:
        tree = Path(tmp) / "tree"
        fetch_tree(repository, revision, tree)
        # The policy's third-party table and the Depends come from the declaration this
        # builder imported; refuse a revision whose declaration differs from it.
        assert_declaration_matches(tree)
        closure = closure_for(BOOTSTRAPPER_POLICY, repo=tree)
        unit = (tree / "appliance/systemd" / UNIT_NAME).read_bytes()
        agent_unit = (tree / "appliance/systemd" / AGENT_UNIT_NAME).read_bytes()
        player_unit = managed_player_unit(
            (tree / "appliance/systemd/player.service").read_bytes())
        weston_unit = (tree / "appliance/systemd/weston.service").read_bytes()
        weston_ini = render_weston_ini().encode()
        project = tomllib.loads((tree / "pyproject.toml").read_text())
        # Version is derived from the packaged CONTENT, not the revision; the revision only
        # selects WHICH content `fetch_tree` pulls.
        version = package_version(closure, unit, project["project"]["version"],
                                  agent_unit=agent_unit, player_unit=player_unit,
                                  weston_unit=weston_unit, weston_ini=weston_ini, tree=tree)
        deb_root = Path(tmp) / "deb-root"
        stage_tree(deb_root, closure=closure, tree=tree, unit=unit, version=version,
                   agent_unit=agent_unit, player_unit=player_unit,
                   weston_unit=weston_unit, weston_ini=weston_ini)
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
    except (ValueError, OSError, subprocess.SubprocessError, ClosureError) as exc:
        parser.exit(1, f"Bootstrapper .deb build failed: {exc}\n")
    print(str(output))


if __name__ == "__main__":
    main()
