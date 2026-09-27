"""`scripts/eeprom_update.py`: the pure config-text functions, plus
`build_eeprom_update`/`main` end to end against synthetic `rpi-eeprom-config`/
`rpi-eeprom-digest` stand-ins on PATH (0014 rev 5, design §2.8), and again as
explicit `--rpi-eeprom-config`/`--rpi-eeprom-digest` paths off PATH entirely
(CI's pinned build-root copy, never the runner's own). No real EEPROM image,
no hardware."""

import os
import stat

import pytest

from scripts.eeprom_update import (
    EEPROM_SETTINGS,
    EepromUpdateError,
    _resolve_tool,
    build_eeprom_update,
    eeprom_config,
    eeprom_problems,
    main,
)

EXISTING_CONFIG = "[all]\nBOOT_ORDER=0xf41\nBOOT_UART=0\n"


# --- pure functions ----------------------------------------------------

def test_eeprom_config_replaces_an_existing_line_and_appends_a_missing_one():
    result = eeprom_config(EXISTING_CONFIG)
    lines = result.splitlines()
    assert lines == ["[all]", "BOOT_ORDER=0xf21", "BOOT_UART=0", "BOOT_WATCHDOG_TIMEOUT=120"]


def test_eeprom_config_keeps_every_other_line_in_order():
    text = "# header\n[all]\nBOOT_ORDER=0xf21\nSOME_OTHER=1\nBOOT_WATCHDOG_TIMEOUT=120\n"
    result = eeprom_config(text)
    assert result.splitlines() == ["# header", "[all]", "BOOT_ORDER=0xf21",
                                    "SOME_OTHER=1", "BOOT_WATCHDOG_TIMEOUT=120"]


def test_eeprom_problems_names_a_changed_and_a_missing_setting():
    config = "BOOT_ORDER=0xf41\n"
    problems = eeprom_problems(config)
    assert any("BOOT_ORDER" in p for p in problems)
    assert any("BOOT_WATCHDOG_TIMEOUT" in p for p in problems)
    assert len(problems) == 2


def test_eeprom_problems_empty_for_a_good_config():
    config = eeprom_config(EXISTING_CONFIG)
    assert eeprom_problems(config) == []


# --- build_eeprom_update / main, against stand-in tools -----------------

_RPI_EEPROM_CONFIG_STUB = """\
#!/bin/sh
# Synthetic stand-in for the real image-format tool. `--config F --out O
# IMAGE` "rebuilds" by copying F verbatim to O -- so O now IS its own plain
# config text (a real image format is out of scope for this test). Reading
# an image back: a sibling "IMAGE.config" (this test's seeded packaged
# image) wins if present, else the image file itself IS the config text
# (a freshly built pieeprom.upd, read back).
if [ "$1" = "--config" ]; then
    cp -- "$2" "$4"
else
    image="$1"
    if [ -f "$image.config" ]; then
        cat -- "$image.config"
    else
        cat -- "$image"
    fi
fi
"""

_RPI_EEPROM_DIGEST_STUB = """\
#!/bin/sh
# Synthetic stand-in: `rpi-eeprom-digest -i IN -o OUT` writes a placeholder.
while [ "$#" -gt 0 ]; do
    case "$1" in
        -i) in_file="$2"; shift 2 ;;
        -o) out_file="$2"; shift 2 ;;
        *) shift ;;
    esac
done
echo "digest-of-$(basename "$in_file")" > "$out_file"
"""


@pytest.fixture
def stub_path(tmp_path, monkeypatch):
    """A directory on PATH holding synthetic rpi-eeprom-config/-digest, ahead
    of anything real (there is nothing real on this host)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    config_tool = bin_dir / "rpi-eeprom-config"
    digest_tool = bin_dir / "rpi-eeprom-digest"
    config_tool.write_text(_RPI_EEPROM_CONFIG_STUB)
    digest_tool.write_text(_RPI_EEPROM_DIGEST_STUB)
    config_tool.chmod(config_tool.stat().st_mode | stat.S_IEXEC)
    digest_tool.chmod(digest_tool.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bin_dir


def _seed_image(tmp_path, config_text=EXISTING_CONFIG):
    image = tmp_path / "pieeprom.original.bin"
    image.write_bytes(b"synthetic eeprom image bytes")
    (tmp_path / "pieeprom.original.bin.config").write_text(config_text)
    return image


def test_build_eeprom_update_writes_upd_and_sig_with_required_settings(tmp_path, stub_path):
    image = _seed_image(tmp_path)
    out_dir = tmp_path / "boot"
    build_eeprom_update(image, out_dir)
    upd = out_dir / "pieeprom.upd"
    sig = out_dir / "pieeprom.sig"
    assert upd.exists() and sig.exists()
    assert eeprom_problems(upd.read_text()) == []
    for key, value in EEPROM_SETTINGS.items():
        assert f"{key}={value}" in upd.read_text()


def test_build_eeprom_update_refuses_when_the_rebuilt_config_still_misses_a_setting(
    tmp_path, stub_path, monkeypatch,
):
    # A stub whose --config/--out step drops BOOT_WATCHDOG_TIMEOUT no matter
    # what it is given -- proves build_eeprom_update reads the update BACK and
    # raises, rather than trusting the tool's exit code alone.
    bin_dir = tmp_path / "bin"
    (bin_dir / "rpi-eeprom-config").write_text("""\
#!/bin/sh
if [ "$1" = "--config" ]; then
    grep -v BOOT_WATCHDOG_TIMEOUT "$2" > "$4"
else
    image="$1"
    if [ -f "$image.config" ]; then cat -- "$image.config"; else cat -- "$image"; fi
fi
""")
    image = _seed_image(tmp_path)
    with pytest.raises(EepromUpdateError, match="BOOT_WATCHDOG_TIMEOUT"):
        build_eeprom_update(image, tmp_path / "boot")


def test_main_returns_1_and_prints_the_problem_on_refusal(tmp_path, stub_path, capsys):
    bin_dir = tmp_path / "bin"
    (bin_dir / "rpi-eeprom-config").write_text("""\
#!/bin/sh
if [ "$1" = "--config" ]; then
    : > "$4"
else
    image="$1"
    if [ -f "$image.config" ]; then cat -- "$image.config"; else cat -- "$image"; fi
fi
""")
    image = _seed_image(tmp_path)
    rc = main(["--image", str(image), "--out", str(tmp_path / "boot")])
    assert rc == 1
    assert "does not match required settings" in capsys.readouterr().err


def test_main_returns_0_on_success(tmp_path, stub_path):
    image = _seed_image(tmp_path)
    rc = main(["--image", str(image), "--out", str(tmp_path / "boot")])
    assert rc == 0
    assert (tmp_path / "boot" / "pieeprom.upd").exists()


# --- explicit --rpi-eeprom-config/--rpi-eeprom-digest wiring (CI's pinned build-root tools,
# never the bare PATH lookup a dev host with the real package relies on) --------------------

def test_build_eeprom_update_runs_explicit_tool_paths_absent_from_path(tmp_path, monkeypatch):
    # The stand-ins live in a directory that is NOT on PATH; PATH itself is a plain system PATH
    # (needed by the stand-ins' own `cp`/`cat`/`[`, exactly like the real shell scripts) that
    # carries no rpi-eeprom-config/-digest of its own -- true of every CI runner. If
    # build_eeprom_update ever fell back to the bare "rpi-eeprom-config"/"rpi-eeprom-digest"
    # names instead of honoring config_tool/digest_tool, this would fail with "not found on
    # PATH" -- proving the explicit-path wiring, not PATH, is what runs.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    tool_dir = tmp_path / "pinned-tools"
    tool_dir.mkdir()
    config_tool = tool_dir / "rpi-eeprom-config"
    digest_tool = tool_dir / "rpi-eeprom-digest"
    config_tool.write_text(_RPI_EEPROM_CONFIG_STUB)
    digest_tool.write_text(_RPI_EEPROM_DIGEST_STUB)
    config_tool.chmod(config_tool.stat().st_mode | stat.S_IEXEC)
    digest_tool.chmod(digest_tool.stat().st_mode | stat.S_IEXEC)
    image = _seed_image(tmp_path)
    build_eeprom_update(image, tmp_path / "boot",
                        config_tool=str(config_tool), digest_tool=str(digest_tool))
    upd = tmp_path / "boot" / "pieeprom.upd"
    assert upd.exists()
    assert eeprom_problems(upd.read_text()) == []


def test_resolve_tool_names_a_missing_explicit_path():
    missing = "/no/such/dir/rpi-eeprom-config"
    with pytest.raises(EepromUpdateError, match="rpi-eeprom-config not found: /no/such/dir"):
        _resolve_tool(missing, "rpi-eeprom-config")


def test_resolve_tool_names_a_missing_bare_name_on_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))  # empty directory: nothing resolves
    with pytest.raises(EepromUpdateError, match="rpi-eeprom-digest not found on PATH"):
        _resolve_tool("rpi-eeprom-digest", "rpi-eeprom-digest")


def test_build_eeprom_update_refuses_a_missing_tool_with_a_named_error_not_a_traceback(
    tmp_path,
):
    image = _seed_image(tmp_path)
    with pytest.raises(EepromUpdateError, match="rpi-eeprom-config not found:"):
        build_eeprom_update(image, tmp_path / "boot",
                            config_tool=str(tmp_path / "no-such-rpi-eeprom-config"),
                            digest_tool="rpi-eeprom-digest")


def test_main_names_the_missing_tool_given_an_explicit_bad_path(tmp_path, capsys):
    image = _seed_image(tmp_path)
    rc = main(["--image", str(image), "--out", str(tmp_path / "boot"),
              "--rpi-eeprom-config", str(tmp_path / "no-such-rpi-eeprom-config")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "rpi-eeprom-config not found:" in err
    assert "Traceback" not in err
