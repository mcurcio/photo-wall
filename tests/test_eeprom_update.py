"""`scripts/eeprom_update.py`: the pure config-text functions, plus
`build_eeprom_update`/`main` end to end against synthetic `rpi-eeprom-config`/
`rpi-eeprom-digest` stand-ins on PATH (0014 rev 5, design §2.8). No real
EEPROM image, no hardware."""

import os
import stat

import pytest

from scripts.eeprom_update import (
    EEPROM_SETTINGS,
    EepromUpdateError,
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
