"""Device-root checks (`scripts/device_root_checks.py`, Project 2 design §2.8) over fixture roots:
watchdog overrides, time daemons, resolver writers, missing packages, foreign and missing apt
sources, and the one-line-per-violation CLI. Generated fixtures only; the real base extract is CI's."""

import re
from pathlib import Path

import pytest
from support.repo import REPO

from scripts.debian_packages import PIN
from scripts.device_root_checks import (
    SSH_UNITS,
    InstalledPackage,
    agent_ssh,
    foreign_sources,
    main,
    missing_packages,
    missing_sources,
    read_dpkg_status,
    resolver_writers,
    time_daemons,
    watchdog_overrides,
)

# An excerpt of Debian's stock /etc/systemd/system.conf: every setting commented out.
STOCK_SYSTEM_CONF = """\
#  This file is part of systemd.
#
# Use 'systemd-analyze cat-config systemd/system.conf' to display the full config.

[Manager]
#LogLevel=info
#CrashReboot=no
#RuntimeWatchdogSec=off
#RuntimeWatchdogPreSec=off
#RebootWatchdogSec=10min
#KExecWatchdogSec=off
#WatchdogDevice=
"""

# rpi-image-gen's templates/debian/apt/trixie-snapshot.sources at the pinned tool commit;
# bin/generators/snapgen substitutes the SOURCE_DATE_EPOCH timestamp into the URIs -- or the
# build's own clock, since rpi-image-gen's `env -i` pipeline clears that variable (what the base
# carried before it was built from the rendered pin).
SNAPSHOT_TEMPLATE = """\
X-IG-Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
Types: deb
URIs: https://snapshot.debian.org/archive/debian/${SNAPSHOT_ISO8601}
Suites: trixie
Components: main contrib non-free non-free-firmware
Options: check-valid-until=no

Types: deb
URIs: https://snapshot.debian.org/archive/debian-security/${SNAPSHOT_ISO8601}
Suites: trixie-security
Components: main contrib non-free non-free-firmware
Options: check-valid-until=no
"""
SNAPSHOT_SOURCES = "etc/apt/sources.list.d/trixie-snapshot.sources"
# What the base carries: `debian_packages.py sources`, rendered by base-image.yml to
# photo-wall-debian.list and copied in by mmdebstrap as the first mirror file.
PINNED_SOURCES = "etc/apt/sources.list.d/0000photo-wall-debian.list"
PINNED_LINES = "".join(f"{source.line()}\n" for source in PIN.sources())

# rpi-image-gen's rolling layer's replacement (the one the base drops).
ROLLING_SOURCES = """\
Types: deb
URIs: http://deb.debian.org/debian
Suites: trixie trixie-updates
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
"""


def stanza(package, status="install ok installed", provides=None):
    lines = [f"Package: {package}", f"Status: {status}", "Priority: required",
             "Architecture: arm64", "Version: 1.0-1"]
    if provides:
        lines.append(f"Provides: {provides}")
    lines += [f"Description: the {package} fixture", " a continuation line.", " ."]
    return "\n".join(lines) + "\n"


# A stock minbase-plus-systemd status, as the base carries before the device set.
MINBASE = [
    stanza("base-files"), stanza("bash"), stanza("coreutils"), stanza("dpkg"), stanza("apt"),
    stanza("libc6"), stanza("mawk", provides="awk"),
    stanza("systemd", provides="systemd-sysusers (= 257.8-1~deb13u2), "
                               "systemd-tmpfiles (= 257.8-1~deb13u2)"),
    stanza("systemd-sysv"), stanza("python3"), stanza("ca-certificates"),
]


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def debian_root(root: Path, *stanzas: str) -> Path:
    """A root with a dpkg database of MINBASE plus `stanzas`, the stock system.conf and the
    base's rendered pin sources."""
    write(root, "var/lib/dpkg/status", "\n".join([*MINBASE, *stanzas]))
    write(root, "etc/systemd/system.conf", STOCK_SYSTEM_CONF)
    write(root, PINNED_SOURCES, PINNED_LINES)
    return root


# --- AC3: watchdog overrides -----------------------------------------------------------------

def test_the_stock_system_conf_and_commented_settings_are_not_overrides(tmp_path):
    write(tmp_path, "etc/systemd/system.conf", STOCK_SYSTEM_CONF)
    write(tmp_path, "etc/systemd/system.conf.d/50-quiet.conf",
          "[Manager]\n#RuntimeWatchdogSec=off\n; RebootWatchdogSec=0\nLogLevel=notice\n")
    write(tmp_path, "run/systemd/system.conf.d/50-photo-wall-watchdog.conf",
          "[Manager]\nRuntimeWatchdogSec=62s\n")      # stage 1's own drop-in
    write(tmp_path, "etc/systemd/system.conf.d/99-x.conf.disabled", "RuntimeWatchdogSec=30\n")
    assert watchdog_overrides(tmp_path) == []


def test_a_drop_in_setting_is_an_override(tmp_path):
    write(tmp_path, "etc/systemd/system.conf.d/99-x.conf", "[Manager]\nRuntimeWatchdogSec=30\n")
    assert watchdog_overrides(tmp_path) == [
        "etc/systemd/system.conf.d/99-x.conf:2: RuntimeWatchdogSec"]


def test_a_setting_in_system_conf_itself_is_an_override(tmp_path):
    write(tmp_path, "etc/systemd/system.conf",
          STOCK_SYSTEM_CONF + "RuntimeWatchdogSec=30\n WatchdogDevice = \n")
    assert watchdog_overrides(tmp_path) == [
        "etc/systemd/system.conf:13: RuntimeWatchdogSec",
        "etc/systemd/system.conf:14: WatchdogDevice"]


def test_merged_usr_reports_a_vendor_drop_in_once_and_links_stay_in_the_root(tmp_path):
    write(tmp_path, "usr/lib/systemd/system.conf.d/60-vendor.conf", "RebootWatchdogSec=0\n")
    (tmp_path / "lib").symlink_to("usr/lib")
    write(tmp_path, "usr/share/photo-wall/wd.conf", "RuntimeWatchdogSec=1\n")
    (tmp_path / "etc/systemd/system.conf.d").mkdir(parents=True)
    (tmp_path / "etc/systemd/system.conf.d/70-link.conf").symlink_to(
        "/usr/share/photo-wall/wd.conf")         # absolute: read from the root, not the host
    (tmp_path / "etc/systemd/system.conf.d/80-masked.conf").symlink_to("/dev/null")
    assert watchdog_overrides(tmp_path) == [
        "etc/systemd/system.conf.d/70-link.conf:1: RuntimeWatchdogSec",
        "usr/lib/systemd/system.conf.d/60-vendor.conf:1: RebootWatchdogSec"]


# --- AC4: the dpkg status reader, time daemons and resolver writers ----------------------------

def test_read_dpkg_status_keeps_installed_stanzas_and_strips_provides_versions(tmp_path):
    debian_root(tmp_path, stanza("systemd-timesyncd", status="deinstall ok config-files",
                                 provides="time-daemon"))
    installed = {package.name: package for package in read_dpkg_status(tmp_path)}
    assert "systemd-timesyncd" not in installed
    assert installed["systemd"] == InstalledPackage(
        "systemd", frozenset({"systemd-sysusers", "systemd-tmpfiles"}))
    assert installed["mawk"].provides == frozenset({"awk"})
    assert installed["bash"].provides == frozenset()
    assert len(installed) == len(MINBASE)


def test_a_stock_minbase_has_no_time_daemon_and_no_resolver_writer(tmp_path):
    debian_root(tmp_path)
    assert time_daemons(tmp_path) == []
    assert resolver_writers(tmp_path) == []


def test_every_package_that_provides_time_daemon_is_flagged(tmp_path):
    debian_root(tmp_path, stanza("systemd-timesyncd", provides="time-daemon"),
                stanza("chrony", provides="time-daemon"))
    assert time_daemons(tmp_path) == ["systemd-timesyncd", "chrony"]


def test_a_resolver_writer_is_flagged_by_name_or_by_what_it_provides(tmp_path):
    debian_root(tmp_path, stanza("systemd-resolved"),
                stanza("openresolv-fork", provides="resolvconf"))
    assert resolver_writers(tmp_path) == ["systemd-resolved", "openresolv-fork"]


def test_a_root_without_a_dpkg_database_is_not_a_debian_root(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_dpkg_status(tmp_path)


# --- AC5: missing packages ---------------------------------------------------------------------

def test_missing_packages_counts_config_files_as_missing(tmp_path):
    debian_root(tmp_path, stanza("weston", status="deinstall ok config-files"),
                stanza("libegl1", status="install ok half-configured"))
    assert missing_packages(tmp_path, ["python3", "weston", "libegl1", "python3-gi",
                                       "weston"]) == ["weston", "libegl1", "python3-gi"]
    assert missing_packages(tmp_path, ["python3", "ca-certificates"]) == []


# --- AC6: foreign apt sources ------------------------------------------------------------------

def test_the_rendered_pin_sources_are_the_only_allowed_ones_and_all_present(tmp_path):
    debian_root(tmp_path)
    write(tmp_path, "etc/apt/sources.list.d/off.sources",
          "Enabled: no\nTypes: deb\nURIs: http://deb.debian.org/debian\nSuites: trixie\n")
    assert foreign_sources(tmp_path, PIN.sources()) == []
    assert missing_sources(tmp_path, PIN.sources()) == []


def test_snapgen_sources_at_the_pin_in_deb822_form_also_match(tmp_path):
    write(tmp_path, SNAPSHOT_SOURCES,
          SNAPSHOT_TEMPLATE.replace("${SNAPSHOT_ISO8601}", PIN.snapshot))
    assert foreign_sources(tmp_path, PIN.sources()) == []
    assert missing_sources(tmp_path, PIN.sources()) == []


def test_deb_debian_org_is_foreign(tmp_path):
    debian_root(tmp_path)
    write(tmp_path, "etc/apt/sources.list.d/trixie.sources", ROLLING_SOURCES)
    assert foreign_sources(tmp_path, PIN.sources()) == [
        "etc/apt/sources.list.d/trixie.sources: http://deb.debian.org/debian trixie",
        "etc/apt/sources.list.d/trixie.sources: http://deb.debian.org/debian trixie-updates"]


def test_a_different_snapshot_timestamp_is_foreign_and_leaves_the_pin_missing(tmp_path):
    """What CI's base carried before it was built from the rendered pin: snapgen's sources at
    the build's own time."""
    other = "20260927T024516Z"
    write(tmp_path, SNAPSHOT_SOURCES, SNAPSHOT_TEMPLATE.replace("${SNAPSHOT_ISO8601}", other))
    assert foreign_sources(tmp_path, PIN.sources()) == [
        f"{SNAPSHOT_SOURCES}: https://snapshot.debian.org/archive/debian/{other} trixie",
        f"{SNAPSHOT_SOURCES}: https://snapshot.debian.org/archive/debian-security/{other} "
        "trixie-security"]
    assert missing_sources(tmp_path, PIN.sources()) == [
        f"{source.uri} {source.suite}" for source in PIN.sources()]


def test_a_root_with_no_sources_is_missing_the_pin(tmp_path):
    assert foreign_sources(tmp_path, PIN.sources()) == []
    assert missing_sources(tmp_path, PIN.sources()) == [
        f"https://snapshot.debian.org/archive/debian/{PIN.snapshot} trixie",
        f"https://snapshot.debian.org/archive/debian-security/{PIN.snapshot} trixie-security"]


def test_a_one_line_entry_for_another_host_is_foreign(tmp_path):
    debian_root(tmp_path)
    pinned = PIN.sources()[0]
    write(tmp_path, "etc/apt/sources.list",
          "# a comment\n"
          f"deb [check-valid-until=no] {pinned.uri}/ {pinned.suite} main\n"
          "deb [ arch=arm64 trusted=yes ] http://archive.example.org/debian trixie main"
          "  # extra\n")
    assert foreign_sources(tmp_path, PIN.sources()) == [
        "etc/apt/sources.list: http://archive.example.org/debian trixie"]


# --- AC7: the CLI --------------------------------------------------------------------------------

def test_main_prints_one_line_per_violation_and_exits_1(tmp_path, capsys):
    root = debian_root(tmp_path / "root", stanza("chrony", provides="time-daemon"),
                       stanza("systemd-resolved"))
    write(root, "etc/systemd/system.conf.d/99-x.conf", "RuntimeWatchdogSec=30\n")
    write(root, "etc/apt/sources.list.d/trixie.sources", ROLLING_SOURCES)
    device = write(tmp_path, "device.txt", "python3\nweston\n")
    depends = write(tmp_path, "depends.txt", "python3, libegl1, ca-certificates\n")
    assert main(["--root", str(root), "--require-installed", str(device),
                 "--require-installed", str(depends), "--pinned-sources"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "watchdog override: etc/systemd/system.conf.d/99-x.conf:1: RuntimeWatchdogSec",
        "time daemon: chrony",
        "resolver writer: systemd-resolved",
        "missing package: weston",
        "missing package: libegl1",
        "foreign source: etc/apt/sources.list.d/trixie.sources: http://deb.debian.org/debian "
        "trixie",
        "foreign source: etc/apt/sources.list.d/trixie.sources: http://deb.debian.org/debian "
        "trixie-updates"]


def test_main_exits_0_silently_on_a_clean_root(tmp_path, capsys):
    root = debian_root(tmp_path / "root")
    device = write(tmp_path, "device.txt", "python3\nca-certificates\n")
    assert main(["--root", str(root), "--require-installed", str(device),
                 "--pinned-sources"]) == 0
    assert capsys.readouterr().out == ""


def test_main_checks_sources_only_when_asked(tmp_path, capsys):
    root = debian_root(tmp_path / "root")
    write(root, "etc/apt/sources.list.d/trixie.sources", ROLLING_SOURCES)
    assert main(["--root", str(root)]) == 0
    assert capsys.readouterr().out == ""


def test_main_refuses_a_base_that_names_no_pinned_source(tmp_path, capsys):
    """--pinned-sources means exactly the pin: a root whose sources were all dropped (or never
    written) is refused, not waved through for having nothing foreign."""
    root = debian_root(tmp_path / "root")
    (root / PINNED_SOURCES).unlink()
    assert main(["--root", str(root), "--pinned-sources"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        f"missing source: {source.uri} {source.suite}" for source in PIN.sources()]


# --- agent SSH (docs/runbook.md, "Reaching a Node over SSH") ------------------------------------

IMAGE_TREE = REPO / "appliance/rpi_image_gen"
AGENT_KEY = (IMAGE_TREE / "photo_wall_agent.pub").read_text()
# Debian trixie's stock sshd_config head, then what rpi-image-gen's openssh-server layer writes
# with pubkey_only=y (layer/net-misc/openssh-server.yaml at the pinned commit).
STOCK_SSHD_CONFIG = """\
Include /etc/ssh/sshd_config.d/*.conf
#PermitRootLogin prohibit-password
KbdInteractiveAuthentication no
UsePAM yes
X11Forwarding yes
Subsystem sftp /usr/lib/openssh/sftp-server
"""
PUBKEY_ONLY = """\
PermitRootLogin no
ChallengeResponseAuthentication no
PasswordAuthentication no
GSSAPIAuthentication no
UsePAM yes
PubkeyAuthentication yes
AuthenticationMethods publickey
"""
PASSWD = "root:x:0:0:root:/root:/bin/bash\nphotowall:x:1000:1000::/home/photowall:/bin/bash\n"


def ssh_root(root: Path) -> Path:
    """What the openssh-server and device-user-admin layers leave in the base."""
    write(root, "etc/passwd", PASSWD)
    write(root, "etc/ssh/sshd_config", STOCK_SSHD_CONFIG)
    write(root, "etc/ssh/sshd_config.d/01pubkey-only.conf", PUBKEY_ONLY)
    write(root, "home/photowall/.ssh/authorized_keys", AGENT_KEY)
    wants = root / "etc/systemd/system/multi-user.target.wants"
    wants.mkdir(parents=True)
    for unit in SSH_UNITS:
        (wants / unit).symlink_to(f"/usr/lib/systemd/system/{unit}")
    return root


def test_the_layers_output_admits_the_agent_key_by_key_only(tmp_path):
    assert agent_ssh(ssh_root(tmp_path), AGENT_KEY) == []


def test_a_disabled_or_masked_server_is_refused(tmp_path):
    root = ssh_root(tmp_path)
    (root / "etc/systemd/system/multi-user.target.wants/ssh-hostkeys-generate.service").unlink()
    (root / "etc/systemd/system/ssh.service").symlink_to("/dev/null")
    assert agent_ssh(root, AGENT_KEY) == ["masked: ssh.service",
                                          "not enabled: ssh-hostkeys-generate.service"]


def test_a_baked_host_key_is_refused(tmp_path):
    root = ssh_root(tmp_path)
    write(root, "etc/ssh/ssh_host_ed25519_key", "x")
    assert agent_ssh(root, AGENT_KEY) == ["baked host key: etc/ssh/ssh_host_ed25519_key"]


@pytest.mark.parametrize("where, text", [
    ("home/photowall/.ssh/authorized_keys", ""),                                  # no key
    ("home/photowall/.ssh/authorized_keys", "ssh-ed25519 AAAAother other\n"),     # another key
    ("root/.ssh/authorized_keys", AGENT_KEY),                                     # a second login
])
def test_anything_but_one_login_with_exactly_the_key_is_refused(tmp_path, where, text):
    root = ssh_root(tmp_path)
    write(root, where, text)
    assert [line.split(":")[0] for line in agent_ssh(root, AGENT_KEY)] == ["authorized keys"]


@pytest.mark.parametrize("drop_in, refused", [
    ("", ["passwordauthentication", "pubkeyauthentication", "permitrootlogin"]),   # pubkey_only=n
    ("PasswordAuthentication yes\n" + PUBKEY_ONLY, ["passwordauthentication"]),    # first wins
    ("Match User x\n" + PUBKEY_ONLY, ["passwordauthentication", "pubkeyauthentication",
                                       "permitrootlogin"]),                         # not global
])
def test_sshd_settings_that_are_not_key_only_are_refused(tmp_path, drop_in, refused):
    root = ssh_root(tmp_path)
    write(root, "etc/ssh/sshd_config.d/01pubkey-only.conf", drop_in)
    assert [line.split()[1] for line in agent_ssh(root, AGENT_KEY)] == refused


def test_main_checks_agent_ssh_only_when_asked(tmp_path, capsys):
    root = debian_root(tmp_path)
    assert main(["--root", str(root)]) == 0
    assert main(["--root", str(root), "--agent-key", str(IMAGE_TREE / "photo_wall_agent.pub")]) == 1
    assert "agent ssh: not enabled: ssh.service" in capsys.readouterr().out.splitlines()


def test_the_base_enables_rpi_image_gens_ssh_layer_with_the_one_key_and_ci_checks_it():
    """The config wires the openssh-server layer key-only to the key file; base-image.yml's root
    check reads that same file over the built squashfs."""
    config = (IMAGE_TREE / "config/photo-wall-base.yaml").read_text()
    sections: dict[str, dict[str, str]] = {}
    section = ""
    for line in config.splitlines():
        if re.fullmatch(r"[a-z_]+:", line):
            section = line[:-1]
        elif match := re.fullmatch(r"  ([a-z0-9_]+): *(\S+)", line):
            sections.setdefault(section, {})[match[1]] = match[2]
    assert sections["layer"]["ssh"] == "openssh-server"
    assert sections["ssh"] == {"pubkey_user1": "${@SRCROOT}/photo_wall_agent.pub",
                               "pubkey_only": "y"}
    assert sections["device"]["user1sudo"] == "nopasswd"
    lines = AGENT_KEY.splitlines()
    assert len(lines) == 1 and lines[0].split()[0] == "ssh-ed25519", lines
    assert not [path for path in IMAGE_TREE.rglob("*") if path.is_file()
                and "PRIVATE KEY" in path.read_text(errors="replace")]
    workflow = (REPO / ".github/workflows/base-image.yml").read_text()
    assert "--agent-key appliance/rpi_image_gen/photo_wall_agent.pub" in workflow
