#!/usr/bin/env python3
"""Build the netboot bundle's `pieeprom.upd`/`.sig` self-update pair (0014
rev 5, design §2.8).

The Pi 5's SPI-EEPROM bootloader updates itself from the TFTP boot directory
by default: when `pieeprom.upd` differs from the image it is running, it
flashes it and resets once, so every Pi netbooting from the staged directory
converges on our settings with no per-Pi step. This wraps the PACKAGED
`rpi-eeprom` tools (`rpi-eeprom-config`, `rpi-eeprom-digest`) -- it never
touches the EEPROM image format itself.

Settings:

- `BOOT_ORDER=0xf21` -- SD card, then network, then start again: after a
  failed network boot the bootloader retries rather than waiting for a card
  or stopping (0xe, which would need a power cycle).
- `BOOT_WATCHDOG_TIMEOUT=120` -- resets a bootloader that wedges (for
  example waiting forever for the link, rpi-eeprom#417). It ends at kernel
  start, so it never overlaps stage 1's own watchdog.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

EEPROM_SETTINGS: Final[Mapping[str, str]] = {"BOOT_ORDER": "0xf21", "BOOT_WATCHDOG_TIMEOUT": "120"}
PIEEPROM_UPD_NAME: Final = "pieeprom.upd"
PIEEPROM_SIG_NAME: Final = "pieeprom.sig"


class EepromUpdateError(RuntimeError):
    """The rebuilt `pieeprom.upd`, read back, does not carry EEPROM_SETTINGS."""


def eeprom_config(packaged: str, settings: Mapping[str, str] = EEPROM_SETTINGS) -> str:
    """PURE. `packaged`'s config text with each of `settings` replaced where it
    already appears, appended where it is missing; every other line is kept,
    in order."""
    remaining = dict(settings)
    lines: list[str] = []
    for line in packaged.splitlines():
        key, sep, _ = line.partition("=")
        if sep and key in remaining:
            lines.append(f"{key}={remaining.pop(key)}")
        else:
            lines.append(line)
    for key, value in remaining.items():
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def eeprom_problems(config: str, settings: Mapping[str, str] = EEPROM_SETTINGS) -> list[str]:
    """PURE. One entry per setting in `settings` that is missing from, or
    different in, `config` (a config read back from a built `pieeprom.upd`);
    [] means every setting is exactly as required."""
    present: dict[str, str] = {}
    for line in config.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            present[key] = value
    problems = []
    for key, expected in settings.items():
        actual = present.get(key)
        if actual is None:
            problems.append(f"missing {key}")
        elif actual != expected:
            problems.append(f"{key}={actual} (want {expected})")
    return problems


def _read_config(image: Path) -> str:
    result = subprocess.run(["rpi-eeprom-config", str(image)],
                            check=True, capture_output=True, text=True, timeout=30)
    return result.stdout


def build_eeprom_update(image: Path, out_dir: Path, *,
                        settings: Mapping[str, str] = EEPROM_SETTINGS) -> None:
    """Write `out_dir/pieeprom.upd` (`rpi-eeprom-config --config ... --out`)
    and `out_dir/pieeprom.sig` (`rpi-eeprom-digest`) from the packaged
    bootloader `image`, then read the built update's config back and raise
    `EepromUpdateError` on any `eeprom_problems` -- never ship a bundle whose
    self-update silently missed a setting."""
    out_dir.mkdir(parents=True, exist_ok=True)
    upd = out_dir / PIEEPROM_UPD_NAME
    sig = out_dir / PIEEPROM_SIG_NAME
    new_config = eeprom_config(_read_config(image), settings)
    fd, config_name = tempfile.mkstemp(suffix=".conf", prefix="pieeprom-")
    config_path = Path(config_name)
    try:
        with open(fd, "w") as handle:
            handle.write(new_config)
        subprocess.run(
            ["rpi-eeprom-config", "--config", str(config_path), "--out", str(upd), str(image)],
            check=True, timeout=60,
        )
    finally:
        config_path.unlink(missing_ok=True)
    subprocess.run(["rpi-eeprom-digest", "-i", str(upd), "-o", str(sig)], check=True, timeout=30)
    problems = eeprom_problems(_read_config(upd), settings)
    if problems:
        raise EepromUpdateError(f"{upd} does not match required settings: {'; '.join(problems)}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, type=Path,
                        help="the packaged rpi-eeprom PIEEPROM_BIN image")
    parser.add_argument("--out", required=True, type=Path,
                        help="bundle boot/ directory to write pieeprom.upd/.sig into")
    args = parser.parse_args(argv)
    try:
        build_eeprom_update(args.image, args.out)
    except (OSError, subprocess.CalledProcessError, EepromUpdateError) as error:
        print(f"eeprom_update: {error}", file=sys.stderr)
        return 1
    print(f"eeprom_update: wrote {args.out / PIEEPROM_UPD_NAME} and {args.out / PIEEPROM_SIG_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
