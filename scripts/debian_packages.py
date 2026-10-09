#!/usr/bin/env python3
"""The Debian declaration: every Debian package a Photo Wall build installs, and the one snapshot
it comes from (decision 0014, Project 2 design §2.9).

This module is the only place a Debian package name or mirror is written. Every consumer is
rendered from it: the base's device set and apt sources (rpi-image-gen, from `packages` and
`sources`), the initrd build root and the CI device root (`mmdebstrap_argv`), each `.deb`'s
`Depends` (`packages`), and each closure policy's third-party table (`import_table`, read by
`scripts/module_closure.py`). `validate` runs at import, so no build can use a declaration that
breaks an invariant.

A pin bump is one edit to `PIN.snapshot`: every cache keyed on this file misses and every root
rebuilds at the new timestamp.

Build tooling: stdlib only and imports nothing first-party, so every build step (the runner's
system python3 included) can run it.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Final, Literal, get_args

Consumer = Literal["bootstrapper", "player", "initrd-build", "node-display", "node-display-build", "node-base", "node-manager"]
Archive = Literal["debian", "raspberrypi"]
# When a package lands in a root. Every root fetches the pin over https, and apt inside a root
# cannot do that until the CA bundle is there, so:
#   "bootstrap"  the root builder installs it from its own package list, fetched by the HOST's
#                apt (mmdebstrap --include; in rpi-image-gen, a required layer of the same name
#                whose mmdebstrap packages list names it -- tests/test_debian_packages.py binds
#                the device layer's requirements to this stage);
#   "apt"        anything else: apt inside the root may install it once "bootstrap" is there.
Stage = Literal["bootstrap", "apt"]
DEVICE_CONSUMERS: Final[tuple[Consumer, ...]] = ("bootstrapper", "player")

SNAPSHOT_FORMAT: Final = "%Y%m%dT%H%M%SZ"
_PACKAGE_NAME: Final = re.compile(r"[a-z0-9][a-z0-9+.-]+")   # Debian Policy §5.6.1
_SNAPSHOT_ARCHIVE: Final = "https://snapshot.debian.org/archive"
# The Debian archive keyring (debian-archive-keyring), at the same path on the Ubuntu build host
# and in every trixie root. Named in each pinned line: mmdebstrap adds signed-by only to a bare
# mirror URL and copies a full `deb ...` line verbatim, and the host's apt trusts no Debian key
# by default (Ubuntu's debian-archive-keyring installs nothing under /etc/apt/trusted.gpg.d).
DEBIAN_KEYRING: Final = "/usr/share/keyrings/debian-archive-keyring.gpg"


class DeclarationError(ValueError):
    """The declaration breaks an invariant. Raised at import, so no build can use it."""


@dataclass(frozen=True, slots=True)
class AptSource:
    """One apt source, authenticated one way: `signed_by` a keyring (a snapshot source), or
    `trusted` (the Raspberry Pi archive only). A source with neither or both cannot be built, so
    no line can reach apt relying on whatever keys the host happens to trust."""

    uri: str
    suite: str
    components: tuple[str, ...]
    signed_by: str | None = None       # the keyring apt verifies this source against
    trusted: bool = False              # [trusted=yes]: the Raspberry Pi archive only

    def __post_init__(self) -> None:
        if self.trusted == (self.signed_by is not None):
            raise DeclarationError(f"{self.uri}: exactly one of signed_by and trusted")

    def line(self) -> str:
        """One-line sources.list form: `deb [signed-by=<keyring> check-valid-until=no] ...` for
        a snapshot source (a snapshot's Release file is past its Valid-Until by design);
        `deb [trusted=yes] ...` for the Raspberry Pi archive, whose InRelease carries a SHA1
        binding signature trixie's verifier rejects (see base-image.yml)."""
        options = ("trusted=yes" if self.trusted
                   else f"signed-by={self.signed_by} check-valid-until=no")
        return f"deb [{options}] {self.uri} {self.suite} {' '.join(self.components)}"


@dataclass(frozen=True, slots=True)
class DebianPin:
    suite: str
    snapshot: str                      # snapshot.debian.org timestamp, SNAPSHOT_FORMAT
    components: tuple[str, ...]

    @property
    def epoch(self) -> int:
        """SOURCE_DATE_EPOCH for the .deb mtimes and build_boot_data's CA-age reference. It does
        not choose the base's mirror: rpi-image-gen runs its layer pipeline under `env -i`, so
        its snapgen never sees it; the base is built from `sources()` rendered into a file."""
        moment = datetime.strptime(self.snapshot, SNAPSHOT_FORMAT).replace(tzinfo=timezone.utc)
        return int(moment.timestamp())

    def sources(self) -> tuple[AptSource, AptSource]:
        """The pin's two sources, the archive and its security suite, signed by
        DEBIAN_KEYRING: every root's only Debian sources (`mmdebstrap_argv`, and the base's
        mirror file rendered by `main sources`)."""
        return (AptSource(f"{_SNAPSHOT_ARCHIVE}/debian/{self.snapshot}", self.suite,
                          self.components, signed_by=DEBIAN_KEYRING),
                AptSource(f"{_SNAPSHOT_ARCHIVE}/debian-security/{self.snapshot}",
                          f"{self.suite}-security", self.components, signed_by=DEBIAN_KEYRING))


@dataclass(frozen=True, slots=True)
class DebianPackage:
    name: str
    consumers: frozenset[Consumer]
    imports: tuple[str, ...] = ()      # Python import roots it gives first-party code
    why: str = ""                      # required when `imports` is empty
    archive: Archive = "debian"
    stage: Stage = "apt"


PIN: Final = DebianPin("trixie", "20260904T000000Z",
                       ("main", "contrib", "non-free", "non-free-firmware"))
# Unpinned: archive.raspberrypi.com has no snapshot service.
RASPBERRY_PI: Final = AptSource("http://archive.raspberrypi.com/debian", "trixie", ("main",),
                                trusted=True)

_EVERY_STAGE: Final[frozenset[Consumer]] = frozenset({"bootstrapper", "player", "initrd-build"})
_DEVICE: Final[frozenset[Consumer]] = frozenset(DEVICE_CONSUMERS)
_PLAYER: Final[frozenset[Consumer]] = frozenset({"player"})
_INITRD_BUILD: Final[frozenset[Consumer]] = frozenset({"initrd-build"})
_NODE_MANAGER: Final[frozenset[Consumer]] = frozenset({"node-manager"})
_NODE_BASE: Final[frozenset[Consumer]] = frozenset({"node-base"})
_DISPLAY: Final[frozenset[Consumer]] = frozenset({"node-display"})
_DISPLAY_BUILD: Final[frozenset[Consumer]] = frozenset({"node-display-build"})
_RENDER_STACK: Final = "the render stack, loaded through gi and GStreamer, not imported by name"
_PI_BOOT: Final = "the Pi 5 kernel, DTBs and bootloader image (unpinned archive)"

PACKAGES: Final[tuple[DebianPackage, ...]] = (
    DebianPackage("python3", _EVERY_STAGE | _NODE_BASE | _NODE_MANAGER,
                  why="the interpreter every stage runs on (one version at the pin)"),
    DebianPackage("ca-certificates", _EVERY_STAGE | _NODE_BASE | _NODE_MANAGER,
                  why="Trust.public() reads the Debian bundle (R5); apt over https in the build "
                      "root", stage="bootstrap"),
    DebianPackage("python3-zeroconf", _DEVICE, imports=("zeroconf",)),
    DebianPackage("python3-gi", _PLAYER, imports=("gi",)),
    DebianPackage("python3-opengl", _PLAYER, imports=("OpenGL",)),
    DebianPackage("python3-cryptography", _DEVICE, imports=("cryptography",)),
    DebianPackage("python3-httpx", _PLAYER, imports=("httpx",)),
    DebianPackage("python3-pydantic", _DEVICE, imports=("pydantic",)),
    DebianPackage("python3-websockets", _PLAYER, imports=("websockets",)),
    DebianPackage("python3-gst-1.0", _PLAYER, why=_RENDER_STACK),
    DebianPackage("gir1.2-gtk-3.0", _PLAYER, why=_RENDER_STACK),
    DebianPackage("gir1.2-gst-plugins-base-1.0", _PLAYER, why=_RENDER_STACK),
    DebianPackage("gstreamer1.0-plugins-base", _PLAYER, why=_RENDER_STACK),
    DebianPackage("gstreamer1.0-plugins-good", _PLAYER, why=_RENDER_STACK),
    DebianPackage("gstreamer1.0-plugins-bad", _PLAYER, why=_RENDER_STACK),
    DebianPackage("gstreamer1.0-libav", _PLAYER, why=_RENDER_STACK),
    DebianPackage("libgl1-mesa-dri", _PLAYER, why=_RENDER_STACK),
    DebianPackage("libegl1", _PLAYER, why=_RENDER_STACK),
    DebianPackage("weston", _PLAYER | _DISPLAY, why=_RENDER_STACK),
    # The base's device layer is metadata-only (appliance/rpi_image_gen/device/
    # photo-wall-device-none.yaml), so nothing else brings udev: without it there is no render
    # or input group and player.service fails at spawn, 216/GROUP.
    DebianPackage("udev", _PLAYER | _NODE_BASE,
                  why="creates the render and input groups player.service's "
                      "SupplementaryGroups name, and gives /dev/dri and /dev/input their "
                      "groups (Debian's 50-udev-default.rules); libinput and logind's seats "
                      "need its database"),
    DebianPackage("passwd", _PLAYER,
                  why="the Player postinst runs useradd/usermod (Debian Policy: a maintainer "
                      "script's non-essential tool is a Depends)"),
    DebianPackage("systemd", _NODE_BASE, why="isolated base unit manager"),
    DebianPackage("login", _NODE_BASE, why="base Weston PAMName=login session configuration"),
    DebianPackage("libpam-systemd", _NODE_BASE,
                  why="base Weston logind seat session and PAM systemd registration"),
    DebianPackage("mount", _NODE_BASE, why="bounded diskless node storage tmpfs mount"),
    # OpenSSH over dropbear: Debian's default server, enabled for boot by its own package, and it
    # brings the client tools (ssh-keygen, scp, sftp) an agent expects.
    DebianPackage("openssh-server", _NODE_BASE,
                  why="the agent's key-only root SSH on every V2 Node (docs/runbook.md, "
                      "\"Reaching a Node over SSH\")"),
    DebianPackage("libweston-14-0", _DISPLAY, why="base display compositor ABI"),
    DebianPackage("libjansson4", _DISPLAY, why="bounded native display JSON protocol"),
    DebianPackage("libcairo2", _DISPLAY, why="base diagnostic rendering"),
    # The private overlay client (appliance/display_host/overlay) is Python: pywayland at run time
    # and, through its scanner, at build time; cffi's backend because trixie's python3-pywayland
    # (0.4.18-4) imports it without declaring it.
    DebianPackage("python3-pywayland", _DISPLAY | _DISPLAY_BUILD, imports=("pywayland",)),
    DebianPackage("python3-cffi-backend", _DISPLAY | _DISPLAY_BUILD, imports=("_cffi_backend",)),
    DebianPackage("python3-cairo", _DISPLAY, imports=("cairo",)),
    DebianPackage("libwayland-client0", _DISPLAY | _PLAYER, why="private base diagnostic Wayland client"),
    DebianPackage("libweston-14-dev", _DISPLAY_BUILD, why="base display shell compiler headers"),
    DebianPackage("libwayland-dev", _DISPLAY_BUILD, why="base display Wayland protocol compiler headers"),
    DebianPackage("libjansson-dev", _DISPLAY_BUILD, why="base display JSON compiler headers"),
    DebianPackage("wayland-protocols", _DISPLAY_BUILD, why="xdg-shell protocol source"),
    DebianPackage("build-essential", _DISPLAY_BUILD, why="base native display compiler"),
    DebianPackage("meson", _DISPLAY_BUILD, why="base native display build graph"),
    DebianPackage("ninja-build", _DISPLAY_BUILD, why="base native display build executor"),
    DebianPackage("pkg-config", _DISPLAY_BUILD, why="base native display ABI discovery"),
    DebianPackage("initramfs-tools", _INITRD_BUILD, why="mkinitramfs"),
    DebianPackage("gnupg", _INITRD_BUILD, why="apt key handling"),
    DebianPackage("kmod", _INITRD_BUILD, why="depmod"),
    DebianPackage("zstd", _INITRD_BUILD, why="initrd compression"),
    DebianPackage("device-tree-compiler", _INITRD_BUILD,
                  why="fdtoverlay and fdtget at the pin for scripts/verify_boot_display.py: "
                      "Ubuntu 24.04's 1.7.0 cannot apply vc4-kms-v3d-pi5 to the Pi 5 DTB"),
    DebianPackage("linux-image-rpi-2712", _INITRD_BUILD, why=_PI_BOOT, archive="raspberrypi"),
    DebianPackage("raspi-firmware", _INITRD_BUILD, why=_PI_BOOT, archive="raspberrypi"),
    DebianPackage("rpi-eeprom", _INITRD_BUILD, why=_PI_BOOT, archive="raspberrypi"),
)


def validate(pin: DebianPin, packages: Sequence[DebianPackage]) -> None:
    """DeclarationError unless: the snapshot is exactly SNAPSHOT_FORMAT; every name follows
    Debian's package-name rule; names are unique; each import root belongs to exactly one
    package; a package with no imports says why; consumers, archive and stage are known ones;
    every package a DEVICE_CONSUMER uses comes from the pinned "debian" archive (the device set
    is fully pinned); a "bootstrap" package is pinned and serves every consumer (every root
    fetches the pin over https); and when the pin is fetched over https, the "bootstrap" stage
    is not empty (nothing else can bring the CA bundle apt inside a root needs)."""
    try:
        parsed = datetime.strptime(pin.snapshot, SNAPSHOT_FORMAT)
    except ValueError:
        parsed = None
    if parsed is None or parsed.strftime(SNAPSHOT_FORMAT) != pin.snapshot:
        raise DeclarationError(f"snapshot {pin.snapshot!r} is not {SNAPSHOT_FORMAT}")
    names: set[str] = set()
    owners: dict[str, str] = {}
    for package in packages:
        if not _PACKAGE_NAME.fullmatch(package.name):
            raise DeclarationError(f"{package.name!r} is not a Debian package name")
        if package.name in names:
            raise DeclarationError(f"{package.name} is declared twice")
        names.add(package.name)
        unknown = sorted(set(package.consumers) - set(get_args(Consumer)))
        if not package.consumers or unknown:
            raise DeclarationError(f"{package.name}: unknown or no consumers {unknown}")
        if package.archive not in get_args(Archive):
            raise DeclarationError(f"{package.name}: unknown archive {package.archive!r}")
        for root in package.imports:
            if root in owners:
                raise DeclarationError(f"import {root} is given by both {owners[root]} and "
                                       f"{package.name}")
            owners[root] = package.name
        if not package.imports and not package.why:
            raise DeclarationError(f"{package.name} names no import and no reason")
        if package.archive != "debian" and package.consumers & _DEVICE:
            raise DeclarationError(f"{package.name} is on the device but comes from the "
                                   f"unpinned {package.archive} archive")
        if package.stage not in get_args(Stage):
            raise DeclarationError(f"{package.name}: unknown stage {package.stage!r}")
        if package.stage == "bootstrap" and (package.archive != "debian"
                                             or not _EVERY_STAGE.issubset(package.consumers)):
            raise DeclarationError(f"{package.name}: a bootstrap package comes from the pinned "
                                   "archive and serves every root")
    if (any(source.uri.startswith("https://") for source in pin.sources())
            and not any(package.stage == "bootstrap" for package in packages)):
        raise DeclarationError("the pin is fetched over https but no package is in the "
                               "bootstrap stage")


def _checked(consumers: Sequence[str], archive: str) -> None:
    unknown = sorted(set(consumers) - set(get_args(Consumer)))
    if unknown or archive not in get_args(Archive):
        raise ValueError(f"unknown consumer {unknown} or archive {archive!r}")


def packages(*consumers: Consumer, archive: Archive = "debian",
             stage: Stage | None = None) -> tuple[str, ...]:
    """Sorted, unique names used by any of `consumers` from `archive` (in `stage`, when given):
      packages("bootstrapper")             the bootstrapper .deb's Depends
      packages("player")                   the Player .deb's Depends
      packages(*DEVICE_CONSUMERS)          the device set the base installs
      packages(*DEVICE_CONSUMERS, stage="bootstrap")   the base's rpi-image-gen layer requirements
      packages("initrd-build")             the initrd build root's --include
      packages("initrd-build", archive="raspberrypi")   its kernel/firmware/eeprom install"""
    _checked(consumers, archive)
    if stage is not None and stage not in get_args(Stage):
        raise ValueError(f"unknown stage {stage!r}")
    wanted = frozenset(consumers)
    return tuple(sorted(package.name for package in PACKAGES
                        if package.archive == archive and package.consumers & wanted
                        and stage in (None, package.stage)))


def import_table(consumer: Consumer) -> Mapping[str, str]:
    """Import root -> package name over the consumer's packages that give imports: the closure
    policy's third-party table (read-only)."""
    _checked((consumer,), "debian")
    return MappingProxyType({root: package.name for package in PACKAGES
                             if consumer in package.consumers for root in package.imports})


def mmdebstrap_argv(*, arch: str, target: str,
                    consumers: Sequence[Consumer]) -> tuple[str, ...]:
    """The one way a root outside rpi-image-gen is built at the pin: minbase, the consumers'
    packages, and only the pinned snapshot sources. Every package is in mmdebstrap's own
    --include, fetched by the host's apt, so the "bootstrap" stage is there before apt runs
    inside the root. Consumers: the initrd build root (base-image.yml) and the e2e device root
    (scripts/test_netboot_e2e.py)."""
    return ("mmdebstrap", f"--arch={arch}", "--variant=minbase",
            '--aptopt=Acquire::Check-Valid-Until "false"',
            f"--include={','.join(packages(*consumers))}",
            PIN.suite, target, *(source.line() for source in PIN.sources()))


def main(argv: Sequence[str] | None = None) -> int:
    """Subcommands, each printing to stdout for a workflow step:
      epoch                                   PIN.epoch
      packages [--archive A] CONSUMER...      one name per line
      sources [--archive A]                   one sources.list line per source (the base's
                                              mirror file; the initrd root's Pi archive)
      mmdebstrap --arch A --target T CONSUMER...   execs mmdebstrap_argv(...)"""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("epoch", help="the pin's SOURCE_DATE_EPOCH")
    listing = commands.add_parser("packages", help="package names, one per line")
    listing.add_argument("--archive", choices=get_args(Archive), default="debian")
    listing.add_argument("consumers", nargs="+", choices=get_args(Consumer), metavar="CONSUMER")
    sources = commands.add_parser("sources", help="sources.list lines, one per source")
    sources.add_argument("--archive", choices=get_args(Archive), default="debian")
    build = commands.add_parser("mmdebstrap", help="build a root at the pin")
    build.add_argument("--arch", required=True)
    build.add_argument("--target", required=True)
    build.add_argument("consumers", nargs="+", choices=get_args(Consumer), metavar="CONSUMER")
    args = parser.parse_args(argv)
    if args.command == "epoch":
        lines: Sequence[object] = (PIN.epoch,)
    elif args.command == "packages":
        lines = packages(*args.consumers, archive=args.archive)
    elif args.command == "sources":
        chosen = PIN.sources() if args.archive == "debian" else (RASPBERRY_PI,)
        lines = tuple(source.line() for source in chosen)
    else:
        command = mmdebstrap_argv(arch=args.arch, target=args.target, consumers=args.consumers)
        sys.stdout.flush()
        os.execvp(command[0], command)
        return 0
    for line in lines:
        print(line)
    return 0


validate(PIN, PACKAGES)

if __name__ == "__main__":
    sys.exit(main())
