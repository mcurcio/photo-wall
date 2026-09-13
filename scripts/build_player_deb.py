"""Build the Player application as a `.deb` package (0009; p4-deb-full-depends).

Re-architected per the owner ruling of 2026-09-12 ("proceed full-steam",
docs/decisions/0008-delivery-ledger.md p4-deb-full-depends, option b): the
Player `.deb` no longer bakes a prebuilt venv/wheelhouse assembled in an arm64
appliance chroot. Instead it **declares its full runtime dependency set as apt
`Depends`** -- the native render stack (GTK/GStreamer/weston/Mesa) *and* the
Python libraries (`python3-pydantic`, `python3-httpx`, ...) -- and the
bootstrapper `apt-get install`s the `.deb` so every dependency is resolved from
the distro repo (Debian trixie) at boot. The base OS stays minimal.

Consequences of the ruling (recorded, not relitigated):

- **Python 3.12 -> 3.13.** trixie ships no `python3.12`; the `.deb` targets
  whatever `python3` the base provides (trixie == 3.13). The cp312-pinned
  wheelhouse is therefore dropped.
- **Hash-pinned wheels -> distro versions.** The hashed-wheelhouse integrity
  model is traded for trixie's own `python3-*` package versions. Accepted:
  home LAN, no threat model (0009 owner ruling). App-compat on 3.13 with the
  unpinned distro versions is validated on the owner's Pi later; this builder's
  job is to produce the `.deb` and let CI prove the `Depends` resolve.

Payload shape (mirrors scripts/build_bootstrapper_deb.py exactly -- pure
staging + `control_file` + `run_dpkg_deb`, `Architecture: all`, buildable on
any host with `dpkg-deb`, NO chroot/venv):

- The Player's FIRST-PARTY import closure (`player/*` + a subset of
  `contracts/*`, empirically confirmed by importing `player.service` plus its
  two lazy imports `player.native` and `player.mdns_discovery` and filtering
  `sys.modules` to first-party roots) staged as `.py` files under
  `/usr/lib/python3/dist-packages/` -- the distro `python3` default `sys.path`
  entry. No `appliance/*` module is in the app's closure; `contracts/release.py`
  (the retiring boot-ticket module) is deliberately NOT imported and NOT staged.
- The two systemd units (`player.service`, `weston.service`) at
  `/etc/systemd/system` with baked `.wants` enablement symlinks, `weston.ini`
  under `/etc/xdg/weston`, and the `postinst` that creates the `wall` user --
  exactly as before.

`control_file` and `run_dpkg_deb` are defined here and REUSED verbatim by
scripts/build_bootstrapper_deb.py (which imports them); do not move them.

Version scheme matches scripts/build_bootstrapper_deb.py / scripts/build_player.py:
`{pyproject project.version}+g{revision}`, read from the given Git revision (not
the working tree) -- one reproducible identifier across every artifact built
from that commit.
"""

from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path, PurePosixPath

from packaging.version import Version

from scripts.build_player import BuildError

PACKAGE = "photo-wall-player"
# The `.deb` now ships only interpreter-agnostic `.py` files and config -- no
# compiled/arch-specific content (contrast the old prebuilt-venv `.deb`, which
# was `arm64`). The native, arch-specific dependencies come from the base via
# `Depends`, resolved by apt on the target; the correct Debian Architecture for
# a package shipping only `.py` files plus units is "all" (same as the
# bootstrapper `.deb`).
ARCHITECTURE = "all"
MAINTAINER = "Photo Wall <photo-wall@localhost>"
DESCRIPTION = (
    "Photo Wall Player application: the player import closure staged under "
    "/usr/lib/python3/dist-packages plus the player and weston systemd units. "
    "Its full runtime dependency set (GTK/GStreamer/weston/Mesa and the Python "
    "libraries) is declared as apt Depends and resolved from the distro repo "
    "at install time (0009, p4-deb-full-depends). Deployment configuration and "
    "central_origin are supplied at boot by the bootstrapper/central, never "
    "baked into this package."
)

# The systemd units this package ships, and the "photo-wall-" prefix the
# appliance build already uses for the same units.
UNIT_FILES = ("player.service", "weston.service")

# The full runtime dependency set, OWNED here as a trixie-oriented constant
# (deliberately NOT derived from appliance/os_definition.json RUNTIME_PACKAGES,
# which is Ubuntu-named and retires with the OS-base pipeline in p4-retire).
# Every name is a real Debian trixie package (verified against
# packages.debian.org, 2026-09-12):
#   - Native render stack: GTK 3, GStreamer (base/good/bad + libav), Mesa
#     (DRI + EGL), weston.
#   - GObject-introspection + typelibs the Python bindings need at runtime.
#   - The Python libraries formerly vendored in the wheelhouse, now the distro's
#     own `python3-*` packages (pydantic/httpx/websockets/cryptography/zeroconf).
#   - `python3` itself (trixie == 3.13), under which the staged dist-packages
#     closure runs.
DEB_DEPENDS = (
    "python3",
    "python3-gi",
    "python3-gst-1.0",
    "python3-opengl",
    "gir1.2-gtk-3.0",
    "gir1.2-gst-plugins-base-1.0",
    "gstreamer1.0-plugins-base",
    "gstreamer1.0-plugins-good",
    "gstreamer1.0-plugins-bad",
    "gstreamer1.0-libav",
    "libgl1-mesa-dri",
    "libegl1",
    "weston",
    "python3-pydantic",
    "python3-httpx",
    "python3-websockets",
    "python3-cryptography",
    "python3-zeroconf",
)

# The bare PyPI project names a *vendored wheel* would have used. The distro
# `python3-*` names above resolve from apt and are permitted; these bare roots
# are forbidden in Depends because their presence would imply a vendored wheel
# (the model this slice retires). The guard matches on exact name equality, so
# `python3-pydantic` passes while a bare `pydantic` is rejected.
_PYPI_ROOTS = frozenset({"pydantic", "httpx", "websockets", "cryptography", "zeroconf"})

# The Player's first-party import closure, as (repo-relative source path,
# dist-packages-relative destination path) pairs. Confirmed empirically:
#
#   .venv/bin/python -c "import sys; import player.service; \
#     import player.native, player.mdns_discovery; \
#     print('\n'.join(sorted(m for m in sys.modules \
#       if m.split('.')[0] in {'player','contracts'} \
#       and getattr(sys.modules[m],'__file__',None))))"
#
# (`player.native` and `player.mdns_discovery` are lazy imports inside
# player/service.py:999,1022, reached at runtime but not at import of
# `player.service`, so they are added explicitly to observe the true runtime
# closure.) The closure is all 11 player modules + 5 contracts modules; NO
# `appliance/*` module and NOT `contracts/release.py` (the retiring
# boot-ticket module, imported by nothing on this path).
_MODULE_FILES = (
    ("player/__init__.py", "player/__init__.py"),
    ("player/cache.py", "player/cache.py"),
    ("player/discovery.py", "player/discovery.py"),
    ("player/executor.py", "player/executor.py"),
    ("player/geometry.py", "player/geometry.py"),
    ("player/identity.py", "player/identity.py"),
    ("player/mdns_discovery.py", "player/mdns_discovery.py"),
    ("player/native.py", "player/native.py"),
    ("player/output_discovery.py", "player/output_discovery.py"),
    ("player/rendering.py", "player/rendering.py"),
    ("player/service.py", "player/service.py"),
    ("contracts/__init__.py", "contracts/__init__.py"),
    ("contracts/enrollment.py", "contracts/enrollment.py"),
    ("contracts/equipment.py", "contracts/equipment.py"),
    ("contracts/models.py", "contracts/models.py"),
    ("contracts/time.py", "contracts/time.py"),
)

# Generous ceiling for the git-archive of the closure + pyproject (the closure
# is ~190 KiB today); guards against archiving an unexpectedly huge tree.
MAX_SOURCE = 4 * 1024 * 1024


def package_version(sources: dict[str, bytes], revision: str) -> str:
    """The .deb version is `{pyproject version}+g{revision}` -- the same scheme
    scripts/build_bootstrapper_deb.py / scripts/build_player.py use, read from
    the given revision's `pyproject.toml`, not the working tree."""
    project = tomllib.loads(sources["pyproject.toml"].decode())
    base_version = Version(project["project"]["version"])
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


def fetch_sources(repository: Path, revision: str) -> dict[str, bytes]:
    """`git archive` the exact fixed file list this package ships, from
    `revision` (not the working tree) -- an explicit full commit, exactly like
    scripts/build_bootstrapper_deb.py:fetch_sources."""
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
                # `git archive` on explicit file paths also emits their parent
                # directory members (e.g. "player/") -- harmless, skipped.
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


def stage_tree(deb_root: Path, *, sources: dict[str, bytes], systemd_source: Path,
               weston_ini: bytes, version: str, depends: tuple[str, ...] = DEB_DEPENDS) -> None:
    """Assemble the `.deb` staging tree.

    Places the Player import closure at `/usr/lib/python3/dist-packages` (the
    distro `python3` default `sys.path` entry) and the units at
    `/etc/systemd/system`, shipped already enabled via `.wants` symlinks (a
    symlink is data, not a maintainer script, so "install is an unpack" still
    holds for activation). Carries NO `/etc/photo-wall` deployment
    configuration and NO `central_origin` -- per 0009 those arrive at boot from
    the bootstrapper/central, never from a published asset. NO venv: the runtime
    dependencies are the package's `Depends`, resolved by apt.
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

    dist_packages = deb_root / "usr/lib/python3/dist-packages"
    for source, dest in _MODULE_FILES:
        target = dist_packages / dest
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        target.write_bytes(sources[source])
        target.chmod(0o644)

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
    slice) no leaked venv in the `.deb`.

    Configuration (Frame binding, assignments, calibration, central_origin)
    arrives at boot from the bootstrapper/central -- never baked into a
    published asset. A staged `/opt/photo-wall/venv` would mean the venv/
    wheelhouse this slice dropped had crept back in; forbid it too.
    """
    if (deb_root / "opt/photo-wall/venv").exists():
        raise BuildError("venv_in_deb")
    if (deb_root / "etc/photo-wall").exists():
        raise BuildError("deployment_config_in_deb")
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


def build(repository: Path, revision: str, output_dir: Path) -> Path:
    """End to end: fetch the closure at `revision` -> stage -> `dpkg-deb`.

    No arm64 chroot and no `--root`: the `.deb` carries only `.py` files and
    config, so it builds on any host with `dpkg-deb` (like the bootstrapper
    `.deb`). The `weston.ini` content is generated from the running player
    package, matching the previous builder's behaviour.
    """
    from player.output_discovery import weston_ini as render_weston_ini

    output_dir = output_dir.resolve()
    repository = repository.resolve(strict=True)
    sources = fetch_sources(repository, revision)
    version = package_version(sources, revision)
    with tempfile.TemporaryDirectory(prefix=".photo-wall-player-deb-", dir=output_dir) as tmp:
        deb_root = Path(tmp) / "deb-root"
        stage_tree(deb_root, sources=sources, systemd_source=repository / "appliance/systemd",
                   weston_ini=render_weston_ini().encode(), version=version)
        assert_no_deployment_config(deb_root)
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
        parser.exit(1, f"Player .deb build failed: {exc}\n")
    print(str(output))


if __name__ == "__main__":
    main()
