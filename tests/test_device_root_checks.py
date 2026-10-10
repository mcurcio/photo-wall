"""Device-root checks (`scripts/device_root_checks.py`, Project 2 design §2.8) over fixture roots:
watchdog overrides, time daemons, resolver writers, missing packages, foreign and missing apt
sources, and the one-line-per-violation CLI. Generated fixtures only; the real base extract is CI's.
Also the base's rpi-image-gen layers' bindings to the pin (debian-packaging/snapshot.list)."""

import re
import subprocess
from pathlib import Path

import pytest
from support.repo import REPO

from scripts.device_root_checks import (
    BUILD_HOSTNAME,
    DDC_MODULES,
    DDC_MODULES_LOAD,
    DISPLAY_POWER_TOOLS,
    MASKED_UNITS,
    SNAPSHOT_LIST,
    SSH_UNITS,
    InstalledPackage,
    agent_ssh,
    base_os,
    foreign_sources,
    main,
    missing_packages,
    missing_sources,
    pinned_sources,
    read_dpkg_status,
    resolver_writers,
    time_daemons,
    watchdog_overrides,
)
from scripts.import_check import declared
from scripts.verify_netboot_initrd import DDC_MODULES as INITRD_DDC_MODULES

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
# What the base carries: snapshot.list's sources, copied by base-image.yml to
# photo-wall-debian.list and in by mmdebstrap as the first mirror file.
PINNED_SOURCES = "etc/apt/sources.list.d/0000photo-wall-debian.list"
PINNED_LINES = "".join(f"{line}\n" for line in SNAPSHOT_LIST.read_text().splitlines()
                       if not line.startswith("#"))
PIN = pinned_sources(SNAPSHOT_LIST.read_text())
SNAPSHOT = "20260904T000000Z"

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
    assert foreign_sources(tmp_path, PIN) == []
    assert missing_sources(tmp_path, PIN) == []


def test_snapgen_sources_at_the_pin_in_deb822_form_also_match(tmp_path):
    write(tmp_path, SNAPSHOT_SOURCES,
          SNAPSHOT_TEMPLATE.replace("${SNAPSHOT_ISO8601}", SNAPSHOT))
    assert foreign_sources(tmp_path, PIN) == []
    assert missing_sources(tmp_path, PIN) == []


def test_deb_debian_org_is_foreign(tmp_path):
    debian_root(tmp_path)
    write(tmp_path, "etc/apt/sources.list.d/trixie.sources", ROLLING_SOURCES)
    assert foreign_sources(tmp_path, PIN) == [
        "etc/apt/sources.list.d/trixie.sources: http://deb.debian.org/debian trixie",
        "etc/apt/sources.list.d/trixie.sources: http://deb.debian.org/debian trixie-updates"]


def test_a_different_snapshot_timestamp_is_foreign_and_leaves_the_pin_missing(tmp_path):
    """What CI's base carried before it was built from the rendered pin: snapgen's sources at
    the build's own time."""
    other = "20260927T024516Z"
    write(tmp_path, SNAPSHOT_SOURCES, SNAPSHOT_TEMPLATE.replace("${SNAPSHOT_ISO8601}", other))
    assert foreign_sources(tmp_path, PIN) == [
        f"{SNAPSHOT_SOURCES}: https://snapshot.debian.org/archive/debian/{other} trixie",
        f"{SNAPSHOT_SOURCES}: https://snapshot.debian.org/archive/debian-security/{other} "
        "trixie-security"]
    assert missing_sources(tmp_path, PIN) == [
        f"{uri} {suite}" for uri, suite in PIN]


def test_a_root_with_no_sources_is_missing_the_pin(tmp_path):
    assert foreign_sources(tmp_path, PIN) == []
    assert missing_sources(tmp_path, PIN) == [
        f"https://snapshot.debian.org/archive/debian/{SNAPSHOT} trixie",
        f"https://snapshot.debian.org/archive/debian-security/{SNAPSHOT} trixie-security"]


def test_a_one_line_entry_for_another_host_is_foreign(tmp_path):
    debian_root(tmp_path)
    uri, suite = PIN[0]
    write(tmp_path, "etc/apt/sources.list",
          "# a comment\n"
          f"deb [check-valid-until=no] {uri}/ {suite} main\n"
          "deb [ arch=arm64 trusted=yes ] http://archive.example.org/debian trixie main"
          "  # extra\n")
    assert foreign_sources(tmp_path, PIN) == [
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
        f"missing source: {uri} {suite}" for uri, suite in PIN]


def test_main_refuses_when_the_snapshot_list_names_no_source(tmp_path, capsys, monkeypatch):
    """An emptied pin cannot wave a base through with nothing to compare against."""
    empty = write(tmp_path, "snapshot.list", "# only a comment\n")
    monkeypatch.setattr("scripts.device_root_checks.SNAPSHOT_LIST", empty)
    root = debian_root(tmp_path / "root")
    (root / PINNED_SOURCES).unlink()
    assert main(["--root", str(root), "--pinned-sources"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "pinned sources: snapshot.list names no source"]


# --- the pin and the base's layers (decision 0019, rule 1; R4) ----------------------------------

def test_the_pin_is_the_archive_and_its_security_suite_at_one_snapshot():
    assert PIN == (
        (f"https://snapshot.debian.org/archive/debian/{SNAPSHOT}", "trixie"),
        (f"https://snapshot.debian.org/archive/debian-security/{SNAPSHOT}", "trixie-security"))


GNU_DATE = subprocess.run(["date", "-u", "-d", "20260904 00:00:00", "+%s"],
                          capture_output=True, check=False).returncode == 0


@pytest.mark.skipif(not GNU_DATE, reason="snapshot-epoch.sh needs GNU date (Linux build hosts)")
def test_the_pins_epoch_is_its_snapshot_instant(tmp_path):
    """SOURCE_DATE_EPOCH for the roots and the base: the first source's instant, in seconds."""
    script = REPO / "debian-packaging/snapshot-epoch.sh"
    done = subprocess.run([script], capture_output=True, text=True, check=True)
    assert done.stdout == "1788480000\n"
    empty = write(tmp_path, "snapshot.list", "# only a comment\n")
    refused = subprocess.run([script, empty], capture_output=True, text=True, check=False)
    assert refused.returncode == 1 and "names no snapshot.debian.org instant" in refused.stderr


def _statements(path: Path):
    """The file's non-blank lines that are not `#` comments, stripped."""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            yield line


def test_no_image_layer_or_workflow_line_writes_a_mirror():
    """snapshot.list is the pin's one home: no rpi-image-gen file or workflow names a Debian
    mirror of its own."""
    found = {(path.relative_to(REPO).as_posix(), line)
             for path in (*sorted(path for path in IMAGE_TREE.rglob("*") if path.is_file()),
                          *sorted((REPO / ".github/workflows").glob("*.yml")))
             for line in _statements(path)
             if "deb.debian.org" in line or "snapshot.debian.org" in line}
    assert found == set(), "a second mirror appeared"


def _layer_metadata(text: str) -> dict[str, str]:
    """rpi-image-gen's `# X-Env-...: value` header fields (first line of each value)."""
    return dict(re.findall(r"^# (X-Env-[\w-]+):[ \t]*(.*)$", text, flags=re.MULTILINE))


def test_the_base_is_built_at_the_pins_suite_from_the_sources_file():
    """rpi-image-gen's snapshot layer takes its timestamp from SOURCE_DATE_EPOCH, which its
    `env -i` pipeline clears; the base's own layer takes the mirror from the copy of
    snapshot.list base-image.yml hands it, at the pin's suite, and names no other mirror."""
    layer = (IMAGE_TREE / "layer/photo-wall-debian.yaml").read_text()
    assert _layer_metadata(layer)["X-Env-Var-debian_sources-Valid"] == "file"
    body = [line.strip() for line in layer.partition("METAEND")[2].splitlines()
            if line.strip() and not line.strip().startswith("#")]
    assert body == ["---", "mmdebstrap:", "architectures:", "- arm64", "mode: auto",
                    "variant: minbase", f"suite: {PIN[0][1]}", "mirrors:",
                    "- ${IGconf_app_debian_sources}"]


def test_the_ca_bundle_is_installed_before_apt_runs_in_the_base():
    """The device layer's apt-get runs inside the chroot over https (the pin), so the CA bundle
    must be there first: ca-certificates is a required rpi-image-gen layer, installed by
    mmdebstrap's own package list with the host's apt, and nothing else is required but the
    init layer. The requirement dropped (the PR 28 regression) fails here."""
    metadata = _layer_metadata((IMAGE_TREE / "layer/photo-wall-device.yaml").read_text())
    assert metadata["X-Env-Layer-Requires"].split(",") == ["systemd-min", "ca-certificates"]


def test_the_image_tree_names_no_package_the_composition_depends_on():
    """The base's OS packages are its layers' (R4), but a package photo-wall-node already
    Depends on (its debian control stanza), named again in the rpi-image-gen tree, would be a
    second home for one fact."""
    node_base = set(declared(REPO / "debian/control")["photo-wall-node"].third_party)
    assert {"systemd", "udev", "nats-server"} <= node_base
    named = {(path.relative_to(REPO).as_posix(), line)
             for path in sorted(IMAGE_TREE.rglob("*")) if path.is_file()
             for line in _statements(path)
             if set(re.findall(r"[a-z0-9][a-z0-9+.-]+", line)) & node_base}
    assert named == set()


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


# --- base OS configuration (rpi_image_gen/layer/photo-wall-os.yaml) ----------------------------

def base_os_root(root: Path) -> Path:
    """What the photo-wall-os layer, the device layer's neutral hostname and libnss-myhostname's
    postinst leave in the base; trixie ships etc/default/locale as a link to ../locale.conf.
    ddcutil, v4l-utils (cec-ctl) and i2c-tools' postinst (the i2c group) add display power's
    tools; the layer's hook writes the DDC/CI modules-load file."""
    system = root / "etc/systemd/system"
    system.mkdir(parents=True)
    for unit in MASKED_UNITS:
        (system / unit).symlink_to("/dev/null")
    write(root, "etc/hostname", "localhost\n")
    write(root, "etc/nsswitch.conf", "passwd:         files\nhosts:          files myhostname dns\n")
    write(root, "etc/locale.conf", "LANG=C.UTF-8\n")
    (root / "etc/default").mkdir()
    (root / "etc/default/locale").symlink_to("../locale.conf")
    for tool in DISPLAY_POWER_TOOLS:
        write(root, tool, "\x7fELF")
    write(root, DDC_MODULES_LOAD, "i2c-dev\n")
    write(root, "etc/group", "root:x:0:\nvideo:x:44:\ni2c:x:994:\n")
    return root


def test_the_layers_output_is_a_configured_base_os(tmp_path):
    assert base_os(base_os_root(tmp_path)) == []


def _unmask_binfmt(root):
    (root / "etc/systemd/system/systemd-binfmt.service").unlink()


def _mask_elsewhere(root):
    unit = root / "etc/systemd/system/proc-sys-fs-binfmt_misc.mount"
    unit.unlink()
    unit.symlink_to("/usr/lib/systemd/system/proc-sys-fs-binfmt_misc.mount")


@pytest.mark.parametrize("break_it, refused", [
    (_unmask_binfmt, "not masked: systemd-binfmt.service"),
    (_mask_elsewhere, "not masked: proc-sys-fs-binfmt_misc.mount"),
    (lambda root: write(root, "etc/hostname", f"{BUILD_HOSTNAME}\n"),
     f"baked hostname: {BUILD_HOSTNAME}"),
    (lambda root: write(root, "etc/nsswitch.conf", "hosts:          files dns\n"),
     "hosts database: no myhostname in etc/nsswitch.conf"),
    (lambda root: write(root, "etc/nsswitch.conf", "hosts: files dns # myhostname\n"),
     "hosts database: no myhostname in etc/nsswitch.conf"),
    # The test Pi's v0.22.1 image: the link with nothing at its end, so pam_env warns.
    (lambda root: (root / "etc/locale.conf").unlink(),
     "default locale: etc/default/locale sets no LANG"),
    (lambda root: write(root, "etc/locale.conf", "LANGUAGE=C\n"),
     "default locale: etc/default/locale sets no LANG"),
    # Display power (1b): the two tools, the DDC/CI driver loaded at boot, the i2c group.
    (lambda root: (root / "usr/bin/ddcutil").unlink(), "missing tool: usr/bin/ddcutil"),
    (lambda root: (root / "usr/bin/cec-ctl").unlink(), "missing tool: usr/bin/cec-ctl"),
    (lambda root: (root / DDC_MODULES_LOAD).unlink(),
     f"ddc driver: {DDC_MODULES_LOAD} does not load i2c-dev"),
    (lambda root: write(root, DDC_MODULES_LOAD, "# i2c-dev\ni2c_dev_x\n"),
     f"ddc driver: {DDC_MODULES_LOAD} does not load i2c-dev"),
    (lambda root: write(root, "etc/group", "root:x:0:\ni2cx:x:994:\n"),
     "missing group: i2c (etc/group)"),
    (lambda root: (root / "etc/group").unlink(), "missing group: i2c (etc/group)"),
])
def test_an_unconfigured_base_os_is_refused(tmp_path, break_it, refused):
    root = base_os_root(tmp_path)
    break_it(root)
    assert base_os(root) == [refused]


@pytest.mark.parametrize("remnant, refused", [
    ("usr/lib/systemd/system/photo-wall-provision.service",
     "v1 unit: usr/lib/systemd/system/photo-wall-provision.service"),
    ("lib/systemd/system/photo-wall-os-agent.service",
     "v1 unit: lib/systemd/system/photo-wall-os-agent.service"),
    ("etc/systemd/system/multi-user.target.wants/photo-wall-provision.service",
     "v1 unit: etc/systemd/system/multi-user.target.wants/photo-wall-provision.service"),
    ("usr/lib/photo-wall-bootstrapper/__main__.py",
     "v1 directory: usr/lib/photo-wall-bootstrapper"),
])
def test_a_base_carrying_the_v1_lane_is_refused(tmp_path, remnant, refused):
    """Every Pi boots the node path (decision 0019): a base still carrying the provisioner, the
    OS agent or the bootstrapper's directory is refused, wherever systemd would find the unit."""
    root = base_os_root(tmp_path)
    write(root, remnant, "[Unit]\n")
    assert base_os(root) == [refused]


def test_no_hostname_file_is_not_a_baked_one(tmp_path):
    root = base_os_root(tmp_path)
    (root / "etc/hostname").unlink()
    assert base_os(root) == []


def test_main_checks_the_base_os_only_when_asked(tmp_path, capsys):
    root = debian_root(tmp_path)
    assert main(["--root", str(root)]) == 0
    assert main(["--root", str(root), "--base-os"]) == 1
    assert "base os: not masked: systemd-binfmt.service" in capsys.readouterr().out.splitlines()


def _hook_lines(text: str) -> list[str]:
    """A layer's body as shell lines: continuations joined, whitespace collapsed."""
    body = text.partition("METAEND")[2].replace("\\\n", " ")
    return [" ".join(line.split()) for line in body.splitlines()]


def test_the_base_is_built_with_its_os_configuration_and_ci_checks_it():
    """The config wires the photo-wall-os layer, whose hook masks exactly MASKED_UNITS and writes
    the locale; the device layer bakes a neutral hostname, not rpi-image-gen's; base-image.yml runs
    --base-os over an extract that holds every file base_os reads."""
    config = (IMAGE_TREE / "config/photo-wall-base.yaml").read_text()
    assert re.search(r"^  os: photo-wall-os$", config, flags=re.MULTILINE)
    layer = (IMAGE_TREE / "layer/photo-wall-os.yaml").read_text()
    assert re.search(r"^# X-Env-Layer-Name: photo-wall-os$", layer, flags=re.MULTILINE)
    hook = _hook_lines(layer)
    command = 'chroot "$1" systemctl mask '
    assert [tuple(line.removeprefix(command).split()) for line in hook
            if line.startswith(command)] == [MASKED_UNITS]
    assert """printf 'LANG=C.UTF-8\\n' > "$1/etc/locale.conf\"""" in hook
    # The OS's own packages are in this layer's list (decision 0019, R4): libnss-myhostname, and
    # display power's ddcutil and v4l-utils (cec-ctl).
    packages = re.search(r"^  packages:\n((?:    - \S+\n)+)", layer, flags=re.MULTILINE)
    assert packages and {"libnss-myhostname", "ddcutil", "v4l-utils"} <= {
        line.removeprefix("    - ") for line in packages[1].splitlines()}
    # The DDC/CI driver loads at boot: nothing matches it by alias.
    # The same driver the netboot initrd must carry (it cannot load what stage 1 did not ship).
    assert DDC_MODULES == INITRD_DDC_MODULES
    modules = "".join(f"{module}\\n" for module in DDC_MODULES)
    assert f"""printf '{modules}' > "$1/{DDC_MODULES_LOAD}\"""" in hook
    device = (IMAGE_TREE / "device/photo-wall-device-none.yaml").read_text()
    hostname = re.search(r"^# X-Env-Var-hostname: (\S+)$", device, flags=re.MULTILINE)
    assert hostname and hostname[1] == "localhost" != BUILD_HOSTNAME
    workflow = (REPO / ".github/workflows/base-image.yml").read_text()
    assert "--base-os; then" in workflow
    extract = workflow.partition('unsquashfs -no-xattrs -d "$extract"')[2].partition(">/dev/null")[0]
    for path in ("etc/systemd", "etc/hostname", "etc/nsswitch.conf", "etc/default/locale",
                 "etc/locale.conf", "usr/lib/systemd/system", "usr/lib/photo-wall-bootstrapper",
                 *DISPLAY_POWER_TOOLS, "etc/modules-load.d", "etc/group"):
        assert re.search(rf"(^|\s){re.escape(path)}(\s|$)", extract), path
