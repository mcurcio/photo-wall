"""The Debian declaration (Project 2 design §2.9): its invariants hold at import, it renders the
same lists every consumer used to keep by hand, and it is the one place a mirror is written. The
base's own OS packages live in its rpi-image-gen layers (decision 0019, R4), never beside a
package a Photo Wall `.deb` already names."""

import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import debian_packages
from scripts.debian_packages import (
    DEBIAN_KEYRING,
    PACKAGES,
    PIN,
    SNAPSHOT_FORMAT,
    AptSource,
    DebianPackage,
    DeclarationError,
    main,
    mmdebstrap_argv,
    packages,
    read_pin,
    validate,
)
from scripts.import_check import declared

REPO = Path(__file__).resolve().parents[1]

INITRD_INCLUDE = (
    "--include=ca-certificates,device-tree-compiler,gnupg,initramfs-tools,kmod,python3,zstd")
SNAPSHOT_LINES = (
    "deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg check-valid-until=no] "
    "https://snapshot.debian.org/archive/debian/20260904T000000Z "
    "trixie main contrib non-free non-free-firmware",
    "deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg check-valid-until=no] "
    "https://snapshot.debian.org/archive/debian-security/"
    "20260904T000000Z trixie-security main contrib non-free non-free-firmware",
)
RASPBERRY_PI_LINE = "deb [trusted=yes] http://archive.raspberrypi.com/debian trixie main"


# --- AC1: the invariants, checked at import --------------------------------------------------

KMOD = DebianPackage("kmod", frozenset({"initrd-build"}), why="depmod")
CA = DebianPackage("ca-certificates", frozenset({"initrd-build"}), why="the CA bundle",
                   stage="bootstrap")


def test_the_shipped_declaration_is_valid():
    validate(PIN, PACKAGES)


@pytest.mark.parametrize(("pin", "declared", "message"), [
    (PIN, [CA, KMOD, KMOD], "declared twice"),
    (PIN, [CA, replace(KMOD, name="Kmod")], "not a Debian package name"),
    (PIN, [CA, replace(KMOD, name="k")], "not a Debian package name"),
    (PIN, [CA, replace(KMOD, why="")], "names no reason"),
    # The Player's and the manager's consumers went with their V1 builders (decision 0019).
    (PIN, [CA, replace(KMOD, consumers=frozenset({"player"}))], "unknown or no consumers"),
    (PIN, [CA, replace(KMOD, consumers=frozenset())], "unknown or no consumers"),
    (replace(PIN, snapshot="2026-09-04T00:00:00Z"), [CA], "is not %Y%m%dT%H%M%SZ"),
    (replace(PIN, snapshot="202694T000000Z"), [CA], "is not %Y%m%dT%H%M%SZ"),
    (PIN, [CA, replace(KMOD, stage="early")], "unknown stage"),
    (PIN, [replace(CA, archive="raspberrypi")], "serves every root"),
    (PIN, [KMOD], "no package is in the bootstrap stage"),
])
def test_validate_refuses_a_broken_declaration(pin, declared, message):
    with pytest.raises(DeclarationError, match=re.escape(message)):
        validate(pin, declared)


def test_an_initrd_build_package_may_come_from_the_raspberry_pi_archive():
    validate(PIN, [CA, DebianPackage("rpi-eeprom", frozenset({"initrd-build"}), why="eeprom",
                                     archive="raspberrypi")])


def test_the_ca_bundle_is_the_bootstrap_stage():
    """Mutation probe: ca-certificates moved to the "apt" stage empties the bootstrap stage,
    an import-time error (apt inside a root could not fetch the https pin)."""
    assert packages("initrd-build", stage="bootstrap") == ("ca-certificates",)
    moved = tuple(replace(package, stage="apt") if package.stage == "bootstrap" else package
                  for package in PACKAGES)
    with pytest.raises(DeclarationError, match="no package is in the bootstrap stage"):
        validate(PIN, moved)


@pytest.mark.parametrize("auth", [{}, {"signed_by": DEBIAN_KEYRING, "trusted": True}])
def test_a_source_is_authenticated_exactly_one_way(auth):
    """A source with neither a keyring nor trusted=yes would fall back to whatever keys the
    host's apt happens to trust (none for Debian on the Ubuntu runner): not constructible."""
    with pytest.raises(DeclarationError, match="exactly one of signed_by and trusted"):
        AptSource("https://snapshot.debian.org/archive/debian/x", "trixie", ("main",), **auth)


# --- AC2: the pin ---------------------------------------------------------------------------


def test_the_pin_is_todays_snapshot_epoch_and_formats_back():
    assert PIN.epoch == 1788480000
    rendered = datetime.fromtimestamp(PIN.epoch, timezone.utc).strftime(SNAPSHOT_FORMAT)
    assert rendered == PIN.snapshot == "20260904T000000Z"


def test_the_pin_names_its_two_sources_each_signed_by_the_debian_keyring():
    archive, security = PIN.sources()
    assert (archive.uri, archive.suite) == (
        "https://snapshot.debian.org/archive/debian/20260904T000000Z", "trixie")
    assert (security.uri, security.suite) == (
        "https://snapshot.debian.org/archive/debian-security/20260904T000000Z",
        "trixie-security")
    assert archive.components == security.components == (
        "main", "contrib", "non-free", "non-free-firmware")
    assert archive.signed_by == security.signed_by == DEBIAN_KEYRING
    assert tuple(source.line() for source in PIN.sources()) == SNAPSHOT_LINES
    assert debian_packages.RASPBERRY_PI.line() == RASPBERRY_PI_LINE


def test_the_pin_is_read_from_its_one_home_the_snapshot_list():
    """debian-packaging/snapshot.list is the pin (decision 0019, rule 1): the build container copies it
    as it is, and the declaration reads PIN from it."""
    text = (REPO / "debian-packaging/snapshot.list").read_text()
    assert debian_packages.SNAPSHOT_LIST == REPO / "debian-packaging/snapshot.list"
    assert read_pin(text) == PIN
    assert [line for line in text.splitlines() if not line.startswith("#")] == list(SNAPSHOT_LINES)


def test_a_bumped_snapshot_list_is_a_new_pin():
    bumped = "\n".join(SNAPSHOT_LINES).replace("20260904T000000Z", "20261001T000000Z")
    assert read_pin(bumped) == replace(PIN, snapshot="20261001T000000Z")


@pytest.mark.parametrize("text", [
    "",                                                                   # no source
    "# only a comment\n",
    SNAPSHOT_LINES[0],                                                    # the security suite missing
    "\n".join((SNAPSHOT_LINES[0], SNAPSHOT_LINES[1].replace("20260904", "20260905"))),  # two instants
    "\n".join((*SNAPSHOT_LINES, RASPBERRY_PI_LINE)),                       # a second mirror
    "\n".join(line.replace(" check-valid-until=no", "") for line in SNAPSHOT_LINES),  # an option
    "\n".join(line.replace("https://snapshot.debian.org", "https://deb.debian.org")
              for line in SNAPSHOT_LINES),                                # not the snapshot
])
def test_a_snapshot_list_that_is_not_exactly_the_pin_is_refused(text):
    with pytest.raises(DeclarationError):
        read_pin(text)


# --- AC3: the lists every consumer renders --------------------------------------------------


def test_each_consumer_gets_its_list():
    assert packages("initrd-build") == (
        "ca-certificates", "device-tree-compiler", "gnupg", "initramfs-tools", "kmod", "python3",
        "zstd")
    assert packages("initrd-build", archive="raspberrypi") == (
        "linux-image-rpi-2712", "raspi-firmware", "rpi-eeprom")
    assert packages("initrd-build", stage="apt") == tuple(
        name for name in packages("initrd-build") if name != "ca-certificates")


def test_an_unknown_consumer_is_refused_rather_than_rendering_nothing():
    with pytest.raises(ValueError, match="unknown consumer"):
        packages("player")


# --- AC4: mmdebstrap and the CLI ------------------------------------------------------------

INITRD_ROOT_ARGV = (
    "mmdebstrap", "--arch=arm64", "--variant=minbase",
    '--aptopt=Acquire::Check-Valid-Until "false"', INITRD_INCLUDE, "trixie", "-",
    *SNAPSHOT_LINES)


def test_mmdebstrap_builds_the_initrd_root_at_the_pin():
    assert mmdebstrap_argv(arch="arm64", target="-",
                           consumers=("initrd-build",)) == INITRD_ROOT_ARGV


@pytest.mark.parametrize(("argv", "printed"), [
    (["epoch"], ["1788480000"]),
    (["packages", "initrd-build"], list(packages("initrd-build"))),
    (["packages", "--archive", "raspberrypi", "initrd-build"],
     ["linux-image-rpi-2712", "raspi-firmware", "rpi-eeprom"]),
    (["sources"], list(SNAPSHOT_LINES)),
    (["sources", "--archive", "raspberrypi"], [RASPBERRY_PI_LINE]),
])
def test_the_cli_prints_the_same_values(argv, printed, capsys):
    assert main(argv) == 0
    assert capsys.readouterr().out.splitlines() == printed


def test_the_cli_execs_mmdebstrap_with_the_same_argv(monkeypatch):
    execs = []
    monkeypatch.setattr(debian_packages.os, "execvp", lambda file, args: execs.append(
        (file, tuple(args))))
    assert main(["mmdebstrap", "--arch", "arm64", "--target", "-", "initrd-build"]) == 0
    assert execs == [("mmdebstrap", INITRD_ROOT_ARGV)]


def test_the_cli_refuses_an_unknown_consumer():
    with pytest.raises(SystemExit):
        main(["packages", "player"])


# --- AC11: the declaration is the one place -------------------------------------------------

MIRROR_HOSTS = ("deb.debian.org", "snapshot.debian.org")
IMAGE_TREE = REPO / "appliance/rpi_image_gen"


def _statements(path: Path):
    """The file's non-blank lines that are not `#` comments, stripped."""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            yield line


def _second_mirrors() -> set[tuple[str, str]]:
    found = set()
    for path in (*sorted(path for path in IMAGE_TREE.rglob("*") if path.is_file()),
                 *sorted((REPO / ".github/workflows").glob("*.yml"))):
        for line in _statements(path):
            if any(host in line for host in MIRROR_HOSTS):
                found.add((path.relative_to(REPO).as_posix(), line))
    return found


def test_no_image_layer_or_workflow_line_writes_a_mirror():
    assert _second_mirrors() == set(), "a second mirror appeared"


# --- the base is built from the declaration (design §2.10) ----------------------------------

def test_the_image_tree_names_no_package_the_composition_depends_on():
    """The base's OS packages are its layers' (R4), but a package photo-wall-node already
    Depends on (debian/control), named again in the rpi-image-gen tree (a hook's apt-get line, an
    mmdebstrap list), would be a second home for one fact."""
    node_base = set(declared(REPO / "debian/control")["photo-wall-node"].third_party)
    assert {"systemd", "udev", "nats-server"} <= node_base
    named = {(path.relative_to(REPO).as_posix(), line)
             for path in sorted(IMAGE_TREE.rglob("*")) if path.is_file()
             for line in _statements(path)
             if set(re.findall(r"[a-z0-9][a-z0-9+.-]+", line)) & node_base}
    assert named == set()


def _layer_metadata(text: str) -> dict[str, str]:
    """rpi-image-gen's `# X-Env-...: value` header fields (first line of each value)."""
    return dict(re.findall(r"^# (X-Env-[\w-]+):[ \t]*(.*)$", text, flags=re.MULTILINE))


INIT_LAYER = "systemd-min"


def test_the_base_installs_the_node_packages_and_nothing_of_the_v1_lane():
    layer = (IMAGE_TREE / "layer/photo-wall-device.yaml").read_text()
    metadata = _layer_metadata(layer)
    assert metadata["X-Env-Layer-Name"] == "photo-wall-device"
    assert metadata["X-Env-Var-local_repo-Valid"] == "dir"
    assert metadata["X-Env-Var-local_repo-Required"] == "y"
    assert {name.removeprefix("X-Env-Var-").partition("-")[0] for name in metadata
            if name.startswith("X-Env-Var-")} == {"local_repo"}
    hook = layer.partition("customize-hooks:")[2]
    assert '"$IGconf_app_local_repo"' in hook
    assert "deb [trusted=yes] file:/tmp/photo-wall-repo ./" in hook
    assert re.search(r"--no-install-recommends[^\n]*\n?[^\n]* photo-wall-node$", hook,
                     flags=re.MULTILINE)
    assert 'rm -rf "$1/tmp/photo-wall-repo"' in hook
    assert "apt-get install -y --no-install-recommends" in hook
    for word in ("bootstrapper", "device_packages"):
        assert word not in layer, word
    config = (IMAGE_TREE / "config/photo-wall-base.yaml").read_text()
    layers = dict(re.findall(r"^  (base|init|app): *(\S+)$", config, flags=re.MULTILINE))
    assert layers == {"base": "photo-wall-debian", "init": INIT_LAYER,
                      "app": metadata["X-Env-Layer-Name"]}


def test_the_bootstrap_stage_is_installed_before_apt_runs_in_the_base():
    """The hook's apt-get runs inside the chroot over https, so the CA bundle must be there
    first: every "bootstrap" package of the declaration is a required rpi-image-gen layer of the
    same name (installed by mmdebstrap's own package list, with the host's apt), and nothing else
    is required but the init layer. A bootstrap package added to the declaration, or the layer
    requirement dropped (the PR #28 regression), fails here."""
    metadata = _layer_metadata((IMAGE_TREE / "layer/photo-wall-device.yaml").read_text())
    required = metadata["X-Env-Layer-Requires"].split(",")
    assert required[0] == INIT_LAYER
    assert sorted(required[1:]) == sorted(package.name for package in PACKAGES
                                          if package.stage == "bootstrap")


def test_the_base_is_built_from_the_rendered_pin_not_the_environment():
    """rpi-image-gen's snapshot layer takes its timestamp from SOURCE_DATE_EPOCH, which its
    `env -i` pipeline clears; the base's own layer takes the mirror from the file base-image.yml
    renders from the declaration, at the declaration's suite, and names no other mirror."""
    layer = (IMAGE_TREE / "layer/photo-wall-debian.yaml").read_text()
    metadata = _layer_metadata(layer)
    assert metadata["X-Env-Layer-Name"] == "photo-wall-debian"
    assert metadata["X-Env-Var-debian_sources-Valid"] == "file"
    assert metadata["X-Env-Var-debian_sources-Required"] == "y"
    body = [line.strip() for line in layer.partition("METAEND")[2].splitlines()
            if line.strip() and not line.strip().startswith("#")]
    assert body == ["---", "mmdebstrap:", "architectures:", "- arm64", "mode: auto",
                    "variant: minbase", f"suite: {PIN.suite}", "mirrors:",
                    "- ${IGconf_app_debian_sources}"]
    workflow = (REPO / ".github/workflows/base-image.yml").read_text()
    assert 'python3 scripts/debian_packages.py sources > "$debian_sources"' in workflow
    assert '"IGconf_app_debian_sources=$DEBIAN_SOURCES"' in workflow
    assert "--pinned-sources" in workflow


def test_no_python_builds_a_deb():
    """Decision 0019 (R3): the Node's packages are debhelper's; no script stages a package tree,
    writes a control file or calls dpkg-deb --build."""
    assert sorted((REPO / "scripts").glob("build_*_deb.py")) == []
    for path in sorted((REPO / "scripts").glob("*.py")):
        assert '"dpkg-deb", "--build"' not in path.read_text(), path.name

