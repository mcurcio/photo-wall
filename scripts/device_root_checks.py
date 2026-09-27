#!/usr/bin/env python3
"""Checks over a device root (decision 0014; Project 2 design §2.8): nothing in stage 2 may take
over what stage 1 owns, every package the Player needs is installed, and the image names only
the snapshot it was built from.

- Watchdog: stage 1's `/run/systemd/system.conf.d` drop-in must be the only watchdog setting; a
  same-named or later-sorting file under `/etc` or `/usr/lib` would override it
  (systemd-system.conf(5)).
- Time daemon: a package that Provides `time-daemon` would step the clock stage 1 settled
  (rule 3).
- Resolver writer: a package in RESOLVER_WRITERS would replace the resolver stage 1 hands over.
- Missing package: `dpkg --install` of the Player refuses to configure when its `Depends` are
  not installed, so the base must carry them.
- Pinned sources (`--pinned-sources`): the apt sources are exactly the declaration's
  `PIN.sources()` -- no foreign source, and none of the pin's missing.

Run over the base squashfs extract (all five), both `.deb` staging trees (watchdog only, from the
builders) and the e2e device root after provisioning (watchdog, time daemon, resolver writer).
Every file is read inside the root: a symlink with an absolute target is followed from the root,
never from the build host.

Build tooling: stdlib only (plus the stdlib-only `scripts.debian_packages`), so the runner's
system python3 runs it.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.debian_packages import PIN, AptSource  # noqa: E402

WATCHDOG_KEYS: Final = ("RuntimeWatchdogSec", "RebootWatchdogSec", "WatchdogDevice")
# Everything systemd reads for the manager's settings except /run, where stage 1's drop-in lives.
SYSTEM_CONF: Final = ("etc/systemd/system.conf", "etc/systemd/system.conf.d",
                      "usr/lib/systemd/system.conf.d", "lib/systemd/system.conf.d",
                      "usr/local/lib/systemd/system.conf.d")
RESOLVER_WRITERS: Final = frozenset({"systemd-resolved", "resolvconf", "openresolv",
                                     "network-manager", "connman", "dhcpcd", "dhcpcd-base",
                                     "isc-dhcp-client", "udhcpc"})
TIME_DAEMON: Final = "time-daemon"     # the virtual package every Debian NTP client Provides
DPKG_STATUS: Final = "var/lib/dpkg/status"
INSTALLED: Final = "install ok installed"
APT_SOURCES: Final = ("etc/apt/sources.list", "etc/apt/sources.list.d")
_MAX_LINKS: Final = 8


@dataclass(frozen=True, slots=True)
class InstalledPackage:
    name: str
    provides: frozenset[str]          # virtual names from Provides:, versions stripped


def _read(root: Path, path: Path) -> str | None:
    """The text of `path` (a path under `root`), following its symlinks as the device would: an
    absolute target, or `..` past the top, stays inside `root`. None when it resolves to nothing
    readable as a file (a dangling link, a mask to /dev/null, a directory, a link loop)."""
    hops = 0
    while path.is_symlink():
        hops += 1
        if hops > _MAX_LINKS:
            return None
        on_device = posixpath.join("/", path.parent.relative_to(root).as_posix(),
                                   os.readlink(path))
        path = root / posixpath.normpath(on_device).lstrip("/")
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


def _stanzas(text: str) -> list[dict[str, str]]:
    """deb822 paragraphs (dpkg's status, apt's .sources): field names lower-cased, continuation
    lines joined with a newline, comment lines dropped."""
    stanzas: list[dict[str, str]] = []
    fields: dict[str, str] = {}
    key: str | None = None
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        if not line.strip():
            if fields:
                stanzas.append(fields)
            fields, key = {}, None
        elif line[0] in " \t":
            if key is not None:
                fields[key] += "\n" + line.strip()
        else:
            name, _, value = line.partition(":")
            key = name.strip().lower()
            fields[key] = value.strip()
    if fields:
        stanzas.append(fields)
    return stanzas


def read_dpkg_status(root: Path) -> tuple[InstalledPackage, ...]:
    """Every stanza of root/var/lib/dpkg/status whose Status is 'install ok installed' (a removed
    package's `deinstall ok config-files` is not installed). The one dpkg-status reader:
    time_daemons, resolver_writers and missing_packages use it. A root with no dpkg database
    raises FileNotFoundError: it is not a Debian root, so no check over it can pass."""
    text = _read(root, root / DPKG_STATUS)
    if text is None:
        raise FileNotFoundError(f"{root / DPKG_STATUS}: no dpkg database")
    installed: list[InstalledPackage] = []
    for fields in _stanzas(text):
        if " ".join(fields.get("status", "").split()) != INSTALLED or "package" not in fields:
            continue
        provides = (item.split("(")[0].strip() for item in fields.get("provides", "").split(","))
        installed.append(InstalledPackage(fields["package"],
                                          frozenset(name for name in provides if name)))
    return tuple(installed)


def _system_conf_files(root: Path) -> Iterator[Path]:
    """system.conf and every *.conf drop-in, once each: with merged /usr, lib/ and usr/lib/ are
    one directory."""
    seen: set[tuple[Path, str]] = set()
    for entry in SYSTEM_CONF:
        location = root / entry
        for path in sorted(location.glob("*.conf")) if location.is_dir() else (location,):
            identity = (path.parent.resolve(), path.name)
            if identity not in seen:
                seen.add(identity)
                yield path


def watchdog_overrides(root: Path) -> list[str]:
    """'<path>:<line>: <key>' for every uncommented WATCHDOG_KEYS setting under SYSTEM_CONF
    (an empty assignment is a setting too: it resets stage 1's). Stage 1's /run drop-in must be
    the only one (a later-sorting or same-named /etc file would override it:
    systemd-system.conf(5))."""
    found: list[str] = []
    for path in _system_conf_files(root):
        text = _read(root, path)
        for number, line in enumerate((text or "").splitlines(), start=1):
            setting = line.strip()
            if not setting or setting[0] in "#;" or "=" not in setting:
                continue
            key = setting.partition("=")[0].strip()
            if key in WATCHDOG_KEYS:
                found.append(f"{path.relative_to(root).as_posix()}:{number}: {key}")
    return found


def time_daemons(root: Path) -> list[str]:
    """Installed packages that Provide time-daemon (rule 3: stage 1 alone sets the clock)."""
    return [package.name for package in read_dpkg_status(root)
            if TIME_DAEMON in package.provides]


def resolver_writers(root: Path) -> list[str]:
    """Installed packages in RESOLVER_WRITERS, by name or by a virtual name they Provide (they
    would replace stage 1's resolver)."""
    return [package.name for package in read_dpkg_status(root)
            if package.name in RESOLVER_WRITERS or package.provides & RESOLVER_WRITERS]


def missing_packages(root: Path, required: Iterable[str]) -> list[str]:
    """Required names not installed, once each, in the order given. On the base: the device set
    and the Player .deb's own Depends (read from the built .deb), so `dpkg --install` of that
    Player cannot fail on a dependency."""
    installed = {package.name for package in read_dpkg_status(root)}
    return [name for name in dict.fromkeys(required) if name not in installed]


def _one_line_sources(text: str) -> Iterator[tuple[str, str]]:
    """(URI, suite) of every `deb`/`deb-src` line of a one-line sources file (sources.list(5)),
    `[options]` skipped. A line too short to name both yields what it has, so it is flagged."""
    for line in text.splitlines():
        words = line.partition("#")[0].split()
        if not words or words[0] not in ("deb", "deb-src"):
            continue
        rest = words[1:]
        if rest and rest[0].startswith("["):
            while rest and not rest[0].endswith("]"):
                rest = rest[1:]
            rest = rest[1:]
        yield (rest[0] if rest else "", rest[1] if len(rest) > 1 else "")


def _deb822_sources(text: str) -> Iterator[tuple[str, str]]:
    """(URI, suite) of every enabled stanza of a deb822 .sources file, URIs x Suites; other
    fields (Options, Signed-By, rpi-image-gen's X-IG-Signed-By) are not the source's identity."""
    for fields in _stanzas(text):
        if fields.get("enabled", "yes").strip().lower() == "no":
            continue
        for uri in fields.get("uris", "").split():
            for suite in fields.get("suites", "").split():
                yield uri, suite


def _apt_sources(root: Path) -> Iterator[tuple[str, str, str]]:
    """(root-relative file, URI, suite) for every source apt would read under APT_SOURCES."""
    listing, directory = (root / entry for entry in APT_SOURCES)
    files = [listing, *sorted(directory.glob("*.list")), *sorted(directory.glob("*.sources"))]
    for path in files:
        text = _read(root, path)
        if text is None:
            continue
        parse = _deb822_sources if path.suffix == ".sources" else _one_line_sources
        for uri, suite in parse(text):
            yield path.relative_to(root).as_posix(), uri, suite


def foreign_sources(root: Path, allowed: Sequence[AptSource]) -> list[str]:
    """'<path>: <uri> <suite>' for every apt source under etc/apt/sources.list and
    etc/apt/sources.list.d (one-line and deb822 forms) whose (URI, suite) is not in `allowed`
    (a trailing slash on a URI is insignificant). On the base, allowed = PIN.sources(): a
    source at any other snapshot timestamp (the build's own clock, say) is foreign."""
    permitted = {(source.uri.rstrip("/"), source.suite) for source in allowed}
    return [f"{name}: {uri} {suite}" for name, uri, suite in _apt_sources(root)
            if (uri.rstrip("/"), suite) not in permitted]


def missing_sources(root: Path, required: Sequence[AptSource]) -> list[str]:
    """'<uri> <suite>' for every `required` source no apt source under the root names (same
    matching as foreign_sources). With foreign_sources over the same list: the root's sources
    are exactly the pin, so a base built from no pinned source at all cannot pass."""
    present = {(uri.rstrip("/"), suite) for _, uri, suite in _apt_sources(root)}
    return [f"{source.uri} {source.suite}" for source in required
            if (source.uri.rstrip("/"), source.suite) not in present]


def main(argv: Sequence[str] | None = None) -> int:
    """--root DIR [--require-installed FILE]... [--pinned-sources]; exit 1 with one line per
    violation, 0 on a clean root. Watchdog, time-daemon and resolver-writer checks always run;
    each FILE names packages separated by whitespace or commas (a `packages` listing or a
    `Depends` value); --pinned-sources requires exactly debian_packages.PIN.sources()."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--require-installed", type=Path, action="append", default=[],
                        metavar="FILE", help="package names that must be installed")
    parser.add_argument("--pinned-sources", action="store_true",
                        help="the root's apt sources must be exactly the declaration's pin")
    args = parser.parse_args(argv)
    root: Path = args.root
    violations = [f"watchdog override: {line}" for line in watchdog_overrides(root)]
    violations += [f"time daemon: {name}" for name in time_daemons(root)]
    violations += [f"resolver writer: {name}" for name in resolver_writers(root)]
    if args.require_installed:
        required = [name for path in args.require_installed
                    for name in re.split(r"[,\s]+", path.read_text()) if name]
        violations += [f"missing package: {name}" for name in missing_packages(root, required)]
    if args.pinned_sources:
        violations += [f"foreign source: {line}" for line in foreign_sources(root, PIN.sources())]
        violations += [f"missing source: {line}" for line in missing_sources(root, PIN.sources())]
    for line in violations:
        print(line)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
