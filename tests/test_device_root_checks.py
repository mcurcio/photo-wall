"""Device-root checks (`scripts/device_root_checks.py`, Project 2 design §2.8) over fixture roots:
watchdog overrides, time daemons, resolver writers, missing packages, foreign and missing apt
sources, and the one-line-per-violation CLI. Generated fixtures only; the real base extract is CI's."""

from pathlib import Path

import pytest

from scripts.debian_packages import PIN
from scripts.device_root_checks import (
    InstalledPackage,
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
