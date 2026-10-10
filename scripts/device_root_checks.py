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
- Missing package (`--require-installed FILE`): every named package is installed (dpkg's own
  database).
- Pinned sources (`--pinned-sources`): the apt sources are exactly the pin's, the sources of
  debian-packaging/snapshot.list (decision 0019, rule 1) -- no foreign source, and none of the
  pin's missing.
- Agent SSH (`--agent-key FILE`): rpi-image-gen's openssh-server layer left the server and its
  per-boot host-key generator enabled, no host key baked in, exactly one login holding exactly
  that key, and sshd's effective settings key-only (docs/runbook.md, "Reaching a Node over SSH").
- Base OS configuration (`--base-os`): the binfmt units masked (the Pi kernel has no
  binfmt_misc), no build-time hostname baked in (stage 1 names the Node photo-wall-<serial> at
  boot), the hosts database resolving the machine's own name (libnss-myhostname), a default
  locale pam_env can read (rpi_image_gen/layer/photo-wall-os.yaml), nothing of the retired
  V1 Pi lane (V1_UNITS, V1_DIRECTORY): every Pi boots the node path (decision 0019), and display
  power's ddcutil, cec-ctl, DDC/CI driver loaded at boot and i2c group (`display_power`).

Run over the base squashfs extract (all six) and the Player `.deb` staging tree (watchdog only,
from the builder).
Every file is read inside the root: a symlink with an absolute target is followed from the root,
never from the build host.

Build tooling: stdlib only, so the runner's system python3 runs it.
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
# The Debian snapshot pin's one home (decision 0019, rule 1): its sources are the base's.
SNAPSHOT_LIST: Final = REPO / "debian-packaging/snapshot.list"
# An apt source's identity: (URI, suite). Options and components do not name another source.
Source = tuple[str, str]

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
# The retired V1 Pi lane's units and the bootstrapper's directory (decision 0019): a base carrying
# any of them is refused, wherever systemd would read the unit from.
V1_UNITS: Final = ("photo-wall-provision.service", "photo-wall-os-agent.service")
V1_DIRECTORY: Final = "usr/lib/photo-wall-bootstrapper"
UNIT_DIRECTORIES: Final = ("etc/systemd/system", "usr/lib/systemd/system", "lib/systemd/system")
INSTALLED: Final = "install ok installed"
APT_SOURCES: Final = ("etc/apt/sources.list", "etc/apt/sources.list.d")
_MAX_LINKS: Final = 8
SSH_UNITS: Final = ("ssh.service", "ssh-hostkeys-generate.service")
SSHD_CONFIG: Final = "etc/ssh/sshd_config"
# sshd keeps the first value it reads for a keyword; Debian's sshd_config includes
# sshd_config.d/*.conf (sorted) before its own lines. ChallengeResponseAuthentication is
# KbdInteractiveAuthentication's older name.
SSHD_KEY_ONLY: Final = {"passwordauthentication": "no", "kbdinteractiveauthentication": "no",
                        "pubkeyauthentication": "yes", "permitrootlogin": "no"}
_SSHD_ALIASES: Final = {"challengeresponseauthentication": "kbdinteractiveauthentication"}
MASKED_UNITS: Final = ("systemd-binfmt.service", "proc-sys-fs-binfmt_misc.automount",
                       "proc-sys-fs-binfmt_misc.mount")
# What rpi-image-gen bakes into /etc/hostname when no layer sets IGconf_device_hostname.
BUILD_HOSTNAME: Final = "rpi-image-gen"
# Display power on the Node (spike 2026-10-10): ddcutil (DDC/CI) and v4l-utils' cec-ctl (HDMI
# CEC), the modules-load file that loads the DDC/CI driver at boot (nothing matches it by alias),
# and the group i2c-tools' udev rule gives the /dev/i2c-N nodes. DDC_MODULES is the initrd's
# (scripts/verify_netboot_initrd.py): bound by tests/test_device_root_checks.py rather than
# imported, so the base build's inputs (scripts/release_plan.py) stay this file alone.
DISPLAY_POWER_TOOLS: Final = ("usr/bin/ddcutil", "usr/bin/cec-ctl")
DDC_MODULES: Final = ("i2c-dev",)
DDC_MODULES_LOAD: Final = "etc/modules-load.d/photo-wall-ddc.conf"
I2C_GROUP: Final = "i2c"


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


def pinned_sources(text: str) -> tuple[Source, ...]:
    """(URI, suite) of every source a one-line sources file names, in order: the pin's, read
    from SNAPSHOT_LIST's text."""
    return tuple(_one_line_sources(text))


def foreign_sources(root: Path, allowed: Sequence[Source]) -> list[str]:
    """'<path>: <uri> <suite>' for every apt source under etc/apt/sources.list and
    etc/apt/sources.list.d (one-line and deb822 forms) whose (URI, suite) is not in `allowed`
    (a trailing slash on a URI is insignificant). On the base, allowed = the pin's sources: a
    source at any other snapshot timestamp (the build's own clock, say) is foreign."""
    permitted = {(uri.rstrip("/"), suite) for uri, suite in allowed}
    return [f"{name}: {uri} {suite}" for name, uri, suite in _apt_sources(root)
            if (uri.rstrip("/"), suite) not in permitted]


def missing_sources(root: Path, required: Sequence[Source]) -> list[str]:
    """'<uri> <suite>' for every `required` source no apt source under the root names (same
    matching as foreign_sources). With foreign_sources over the same list: the root's sources
    are exactly the pin, so a base built from no pinned source at all cannot pass."""
    present = {(uri.rstrip("/"), suite) for _, uri, suite in _apt_sources(root)}
    return [f"{uri} {suite}" for uri, suite in required
            if (uri.rstrip("/"), suite) not in present]


def _sshd_settings(root: Path) -> dict[str, str]:
    """keyword (lower-cased, aliases folded) -> the value sshd would use, outside any Match
    block: the sorted sshd_config.d drop-ins first, then sshd_config's own lines."""
    paths = [*sorted((root / SSHD_CONFIG).with_suffix(".d").glob("*.conf")), root / SSHD_CONFIG]
    settings: dict[str, str] = {}
    for path in paths:
        for line in (_read(root, path) or "").splitlines():
            words = line.partition("#")[0].split(None, 1)
            if len(words) < 2:
                continue
            keyword = words[0].lower()
            if keyword == "match":
                break
            if keyword != "include":
                settings.setdefault(_SSHD_ALIASES.get(keyword, keyword), words[1].strip().lower())
    return settings


def _logins(root: Path) -> Iterator[tuple[str, str]]:
    """(user, home) for every etc/passwd entry with an absolute home."""
    for line in (_read(root, root / "etc/passwd") or "").splitlines():
        fields = line.split(":")
        if len(fields) >= 6 and fields[5].startswith("/"):
            yield fields[0], fields[5]


def _masked(root: Path, unit: str) -> bool:
    """root's etc/systemd/system/<unit> is systemctl's mask, a link to /dev/null."""
    mask = root / "etc/systemd/system" / unit
    return mask.is_symlink() and os.readlink(mask) == "/dev/null"


def agent_ssh(root: Path, key: str) -> list[str]:
    """Violations of the agent's SSH access on a base built with rpi-image-gen's openssh-server
    layer: SSH_UNITS enabled for multi-user.target and not masked; no etc/ssh/ssh_host_* baked
    in (each boot makes its own); exactly one login's .ssh/authorized_keys, holding exactly `key`;
    and sshd's effective settings SSHD_KEY_ONLY."""
    found: list[str] = []
    for unit in SSH_UNITS:
        wants = root / "etc/systemd/system/multi-user.target.wants" / unit
        if not wants.is_symlink() and not wants.exists():
            found.append(f"not enabled: {unit}")
        if _masked(root, unit):
            found.append(f"masked: {unit}")
    found += [f"baked host key: {path.relative_to(root).as_posix()}"
              for path in sorted((root / "etc/ssh").glob("ssh_host_*"))]
    holders = {user: text for user, home in _logins(root)
               if (text := _read(root, root / home.lstrip("/") / ".ssh/authorized_keys"))
               and text.strip()}
    if [text.split() for text in holders.values()] != [key.split()]:
        found.append(f"authorized keys: {sorted(holders)} hold keys, want one login with exactly "
                     "the agent key")
    settings = _sshd_settings(root)
    found += [f"sshd: {keyword} is {settings.get(keyword)}, want {value}"
              for keyword, value in SSHD_KEY_ONLY.items() if settings.get(keyword) != value]
    return found


def base_os(root: Path) -> list[str]:
    """Violations of the base's OS configuration: MASKED_UNITS each masked; etc/hostname not
    BUILD_HOSTNAME (stage 1 writes the Node's own name at boot); `myhostname` in the hosts line of
    etc/nsswitch.conf, so that name resolves with no /etc/hosts line; etc/default/locale,
    followed through its link, setting LANG (pam_env reads it at every login); no V1_UNITS
    file or link under any UNIT_DIRECTORIES (a `.wants` link included) nor V1_DIRECTORY; and
    display_power's tools, driver and group."""
    found = [f"not masked: {unit}" for unit in MASKED_UNITS if not _masked(root, unit)]
    found += [f"v1 unit: {path.relative_to(root).as_posix()}"
              for directory in UNIT_DIRECTORIES if (root / directory).is_dir()
              for path in sorted((root / directory).rglob("*")) if path.name in V1_UNITS]
    if (root / V1_DIRECTORY).is_symlink() or (root / V1_DIRECTORY).exists():
        found.append(f"v1 directory: {V1_DIRECTORY}")
    if (_read(root, root / "etc/hostname") or "").strip() == BUILD_HOSTNAME:
        found.append(f"baked hostname: {BUILD_HOSTNAME}")
    hosts = [line.partition("#")[0].split()[1:]
             for line in (_read(root, root / "etc/nsswitch.conf") or "").splitlines()
             if line.startswith("hosts:")]
    if not any("myhostname" in sources for sources in hosts):
        found.append("hosts database: no myhostname in etc/nsswitch.conf")
    locale = _read(root, root / "etc/default/locale")
    if locale is None or not any(line.strip().startswith("LANG=")
                                 for line in locale.splitlines()):
        found.append("default locale: etc/default/locale sets no LANG")
    return found + display_power(root)


def display_power(root: Path) -> list[str]:
    """Violations of what display power needs in the base: each DISPLAY_POWER_TOOLS file (followed
    through its links); DDC_MODULES_LOAD naming every DDC_MODULES module (systemd-modules-load
    reads one name per line, `#` and `;` lines are comments); and I2C_GROUP in etc/group, which
    i2c-tools' udev rule gives the /dev/i2c-N nodes."""
    found = [f"missing tool: {tool}" for tool in DISPLAY_POWER_TOOLS
             if _read(root, root / tool) is None]
    loaded = {line.strip() for line in (_read(root, root / DDC_MODULES_LOAD) or "").splitlines()
              if line.strip() and line.strip()[0] not in "#;"}
    found += [f"ddc driver: {DDC_MODULES_LOAD} does not load {module}"
              for module in DDC_MODULES if module not in loaded]
    groups = {line.partition(":")[0] for line in (_read(root, root / "etc/group") or "").splitlines()}
    if I2C_GROUP not in groups:
        found.append(f"missing group: {I2C_GROUP} (etc/group)")
    return found


def main(argv: Sequence[str] | None = None) -> int:
    """--root DIR [--require-installed FILE]... [--pinned-sources] [--agent-key FILE]
    [--base-os]; exit 1
    with one line per violation, 0 on a clean root. Watchdog, time-daemon and resolver-writer checks always run;
    each FILE names packages separated by whitespace or commas (a `packages` listing or a
    `Depends` value); --pinned-sources requires exactly SNAPSHOT_LIST's sources;
    --agent-key runs `agent_ssh`; --base-os runs `base_os`."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--require-installed", type=Path, action="append", default=[],
                        metavar="FILE", help="package names that must be installed")
    parser.add_argument("--pinned-sources", action="store_true",
                        help="the root's apt sources must be exactly the snapshot pin's")
    parser.add_argument("--agent-key", type=Path, metavar="FILE",
                        help="the agent's SSH public key: the root must admit it, and only it")
    parser.add_argument("--base-os", action="store_true",
                        help="the base's OS configuration: binfmt masked, no build hostname, "
                             "myhostname, a default locale, nothing of the V1 lane, display "
                             "power's tools, driver and group")
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
        pin = pinned_sources(SNAPSHOT_LIST.read_text())
        if not pin:
            violations.append(f"pinned sources: {SNAPSHOT_LIST.name} names no source")
        violations += [f"foreign source: {line}" for line in foreign_sources(root, pin)]
        violations += [f"missing source: {line}" for line in missing_sources(root, pin)]
    if args.agent_key is not None:
        violations += [f"agent ssh: {line}" for line in agent_ssh(root, args.agent_key.read_text())]
    if args.base_os:
        violations += [f"base os: {line}" for line in base_os(root)]
    for line in violations:
        print(line)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
