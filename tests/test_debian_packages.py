"""The Debian declaration (Project 2 design §2.9): its invariants hold at import, it renders the
same lists every consumer used to keep by hand, and it is the one place a Debian package list or
mirror is written."""

import ast
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import debian_packages
from scripts.debian_packages import (
    DEVICE_CONSUMERS,
    PACKAGES,
    PIN,
    SNAPSHOT_FORMAT,
    DebianPackage,
    DeclarationError,
    import_table,
    main,
    mmdebstrap_argv,
    packages,
    validate,
)

REPO = Path(__file__).resolve().parents[1]

# The Player builder's hand-kept DEB_DEPENDS before the declaration replaced it.
PLAYER_DEB_DEPENDS_BEFORE = (
    "python3", "python3-gi", "python3-gst-1.0", "python3-opengl", "gir1.2-gtk-3.0",
    "gir1.2-gst-plugins-base-1.0", "gstreamer1.0-plugins-base", "gstreamer1.0-plugins-good",
    "gstreamer1.0-plugins-bad", "gstreamer1.0-libav", "libgl1-mesa-dri", "libegl1", "weston",
    "python3-pydantic", "python3-httpx", "python3-websockets", "python3-cryptography",
    "python3-zeroconf",
)
DEVICE_INCLUDE = (
    "--include=ca-certificates,gir1.2-gst-plugins-base-1.0,gir1.2-gtk-3.0,gstreamer1.0-libav,"
    "gstreamer1.0-plugins-bad,gstreamer1.0-plugins-base,gstreamer1.0-plugins-good,libegl1,"
    "libgl1-mesa-dri,passwd,python3,python3-cryptography,python3-gi,python3-gst-1.0,"
    "python3-httpx,python3-opengl,python3-pydantic,python3-websockets,python3-zeroconf,weston")
SNAPSHOT_LINES = (
    "deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20260904T000000Z "
    "trixie main contrib non-free non-free-firmware",
    "deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/"
    "20260904T000000Z trixie-security main contrib non-free non-free-firmware",
)
RASPBERRY_PI_LINE = "deb [trusted=yes] http://archive.raspberrypi.com/debian trixie main"


# --- AC1: the invariants, checked at import --------------------------------------------------

ZEROCONF = DebianPackage("python3-zeroconf", frozenset({"bootstrapper"}), imports=("zeroconf",))


def test_the_shipped_declaration_is_valid():
    validate(PIN, PACKAGES)


@pytest.mark.parametrize(("pin", "declared", "message"), [
    (PIN, [ZEROCONF, ZEROCONF], "declared twice"),
    (PIN, [replace(ZEROCONF, name="Python3-zeroconf")], "not a Debian package name"),
    (PIN, [replace(ZEROCONF, name="z")], "not a Debian package name"),
    (PIN, [ZEROCONF, replace(ZEROCONF, name="python3-zeroconf-fork")], "given by both"),
    (PIN, [replace(ZEROCONF, imports=())], "no import and no reason"),
    (PIN, [replace(ZEROCONF, archive="raspberrypi")], "unpinned raspberrypi"),
    (PIN, [replace(ZEROCONF, consumers=frozenset({"player"}), archive="raspberrypi")],
     "unpinned raspberrypi"),
    (PIN, [replace(ZEROCONF, consumers=frozenset({"playr"}))], "unknown or no consumers"),
    (replace(PIN, snapshot="2026-09-04T00:00:00Z"), [ZEROCONF], "is not %Y%m%dT%H%M%SZ"),
    (replace(PIN, snapshot="202694T000000Z"), [ZEROCONF], "is not %Y%m%dT%H%M%SZ"),
])
def test_validate_refuses_a_broken_declaration(pin, declared, message):
    with pytest.raises(DeclarationError, match=re.escape(message)):
        validate(pin, declared)


def test_a_device_package_moved_to_the_unpinned_archive_is_refused():
    """Mutation probe (e): weston on the Raspberry Pi archive is an import-time error."""
    moved = tuple(replace(package, archive="raspberrypi") if package.name == "weston"
                  else package for package in PACKAGES)
    with pytest.raises(DeclarationError, match="weston is on the device"):
        validate(PIN, moved)


def test_an_initrd_build_package_may_come_from_the_raspberry_pi_archive():
    validate(PIN, [DebianPackage("rpi-eeprom", frozenset({"initrd-build"}), why="eeprom",
                                 archive="raspberrypi")])


# --- AC2: the pin ---------------------------------------------------------------------------


def test_the_pin_is_todays_snapshot_epoch_and_formats_back():
    assert PIN.epoch == 1788480000
    rendered = datetime.fromtimestamp(PIN.epoch, timezone.utc).strftime(SNAPSHOT_FORMAT)
    assert rendered == PIN.snapshot == "20260904T000000Z"


def test_the_pin_names_the_two_sources_snapgen_writes():
    archive, security = PIN.sources()
    assert (archive.uri, archive.suite) == (
        "https://snapshot.debian.org/archive/debian/20260904T000000Z", "trixie")
    assert (security.uri, security.suite) == (
        "https://snapshot.debian.org/archive/debian-security/20260904T000000Z",
        "trixie-security")
    assert archive.components == security.components == (
        "main", "contrib", "non-free", "non-free-firmware")
    assert tuple(source.line() for source in PIN.sources()) == SNAPSHOT_LINES
    assert debian_packages.RASPBERRY_PI.line() == RASPBERRY_PI_LINE


# --- AC3: the lists every consumer renders --------------------------------------------------


def test_each_consumer_gets_its_list():
    assert packages("bootstrapper") == ("ca-certificates", "python3", "python3-zeroconf")
    assert packages("player") == tuple(sorted(
        (*PLAYER_DEB_DEPENDS_BEFORE, "ca-certificates", "passwd")))
    assert packages(*DEVICE_CONSUMERS) == tuple(sorted(
        {*packages("bootstrapper"), *packages("player")}))
    assert packages("initrd-build") == (
        "ca-certificates", "gnupg", "initramfs-tools", "kmod", "python3", "zstd")
    assert packages("initrd-build", archive="raspberrypi") == (
        "linux-image-rpi-2712", "raspi-firmware", "rpi-eeprom")
    assert packages("player", archive="raspberrypi") == ()


def test_an_unknown_consumer_is_refused_rather_than_rendering_nothing():
    with pytest.raises(ValueError, match="unknown consumer"):
        packages("playr")


def test_the_import_tables_are_read_only_and_cover_the_device_imports():
    assert dict(import_table("bootstrapper")) == {"zeroconf": "python3-zeroconf"}
    assert sorted(import_table("player")) == [
        "OpenGL", "cryptography", "gi", "httpx", "pydantic", "websockets", "zeroconf"]
    assert import_table("initrd-build") == {}
    with pytest.raises(TypeError):
        import_table("player")["requests"] = "python3-requests"


# --- AC4: mmdebstrap and the CLI ------------------------------------------------------------

DEVICE_ROOT_ARGV = (
    "mmdebstrap", "--arch=arm64", "--variant=minbase",
    '--aptopt=Acquire::Check-Valid-Until "false"', DEVICE_INCLUDE, "trixie", "-",
    *SNAPSHOT_LINES)


def test_mmdebstrap_builds_the_device_root_at_the_pin():
    assert mmdebstrap_argv(arch="arm64", target="-",
                           consumers=("bootstrapper", "player")) == DEVICE_ROOT_ARGV


@pytest.mark.parametrize(("argv", "printed"), [
    (["epoch"], ["1788480000"]),
    (["packages", "bootstrapper"], ["ca-certificates", "python3", "python3-zeroconf"]),
    (["packages", "bootstrapper", "player"], list(packages(*DEVICE_CONSUMERS))),
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
    assert main(["mmdebstrap", "--arch", "arm64", "--target", "-", "bootstrapper",
                 "player"]) == 0
    assert execs == [("mmdebstrap", DEVICE_ROOT_ARGV)]


def test_the_cli_refuses_an_unknown_consumer():
    with pytest.raises(SystemExit):
        main(["packages", "playr"])


# --- AC11: the declaration is the one place -------------------------------------------------

MIRROR_HOSTS = ("deb.debian.org", "snapshot.debian.org")
IMAGE_TREE = REPO / "appliance/rpi_image_gen"


def _statements(path: Path):
    """The file's non-blank lines that are not `#` comments, stripped."""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            yield line


def _second_lists_and_mirrors() -> set[tuple[str, str]]:
    found = set()
    for path in sorted(IMAGE_TREE.rglob("*")):
        if path.is_file():
            for line in _statements(path):
                if (re.search(r"(^|[\s{,-])packages\s*:", line)
                        or any(host in line for host in MIRROR_HOSTS)):
                    found.add((path.relative_to(REPO).as_posix(), line))
    for path in (*sorted((REPO / ".github/workflows").glob("*.yml")),
                 REPO / "scripts/test_netboot_e2e.py"):
        for line in _statements(path):
            if any(host in line for host in MIRROR_HOSTS):
                found.add((path.relative_to(REPO).as_posix(), line))
    return found


def test_no_image_layer_workflow_or_e2e_line_writes_a_package_list_or_a_mirror():
    assert _second_lists_and_mirrors() == set(), "a second package list or mirror appeared"


# --- the base is built from the declaration (design §2.10) ----------------------------------

def test_the_image_tree_names_no_device_package():
    """The base installs the device set from the rendered file; a package named in the
    rpi-image-gen tree (a hook's apt-get line, an mmdebstrap list) would be a second list."""
    device_set = set(packages(*DEVICE_CONSUMERS))
    named = {(path.relative_to(REPO).as_posix(), line)
             for path in sorted(IMAGE_TREE.rglob("*")) if path.is_file()
             for line in _statements(path)
             if set(re.findall(r"[a-z0-9][a-z0-9+.-]+", line)) & device_set}
    assert named == set()


def _layer_metadata(text: str) -> dict[str, str]:
    """rpi-image-gen's `# X-Env-...: value` header fields (first line of each value)."""
    return dict(re.findall(r"^# (X-Env-[\w-]+):[ \t]*(.*)$", text, flags=re.MULTILINE))


def test_the_base_installs_the_rendered_device_set_at_the_pin():
    layer = (IMAGE_TREE / "layer/photo-wall-device.yaml").read_text()
    metadata = _layer_metadata(layer)
    assert metadata["X-Env-Layer-Name"] == "photo-wall-device"
    assert metadata["X-Env-Layer-Requires"] == "systemd-min"
    for variable in ("bootstrapper_deb", "device_packages"):
        assert metadata[f"X-Env-Var-{variable}-Valid"] == "file"
        assert metadata[f"X-Env-Var-{variable}-Required"] == "y"
    hook = layer.partition("customize-hooks:")[2]
    assert '$(cat "$IGconf_app_device_packages")' in hook
    assert '"$IGconf_app_bootstrapper_deb"' in hook
    assert "apt-get install -y --no-install-recommends" in hook
    config = (IMAGE_TREE / "config/photo-wall-base.yaml").read_text()
    layers = dict(re.findall(r"^  (base|app): *(\S+)$", config, flags=re.MULTILINE))
    assert layers == {"base": "debian-trixie-arm64-minbase-snapshot",
                      "app": metadata["X-Env-Layer-Name"]}


def test_no_deb_builder_writes_a_depends_list():
    declared = {package.name for package in PACKAGES}
    builders = sorted((REPO / "scripts").glob("build_*_deb.py"))
    assert [path.name for path in builders] == ["build_bootstrapper_deb.py",
                                                "build_player_deb.py"]
    for path in builders:
        tree = ast.parse(path.read_text())
        assert "DEB_DEPENDS" not in {node.id for node in ast.walk(tree)
                                     if isinstance(node, ast.Name)}, path.name
        for node in ast.walk(tree):
            if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                literal = {element.value for element in node.elts
                           if isinstance(element, ast.Constant) and isinstance(element.value, str)}
                assert not literal & declared, (path.name, sorted(literal & declared))
