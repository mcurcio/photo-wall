#!/usr/bin/env python3
"""Assert that a Linux `.config` builds a fixed set of symbols IN (not as a
module), against text -- no kernel build, no root (0014 rev 5, design §2.8
and §5).

Stage 1 needs `STAGE1_BUILTINS` built into the packaged `linux-image-rpi-2712`
kernel: the watchdog character device and the emergency-restart sysrq must
exist before stage 1 ever runs (liveness), and the kernel's own `ip=dhcp` must
run DHCP and read option 42 (R6's first time source). `base-image.yml` runs
this once against the freshly-installed kernel's `/boot/config-<kver>` on a
cache miss, and again against a copy of that config kept in the cached output
($KOUT) on a cache hit -- so a kernel package that silently drops a symbol
never rides a stale Cache B restore. A failure is a STOP with an errata entry,
never a silent fallback.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

# The one list of what stage 1 needs built in (all `=y` in Raspberry Pi's bcm2712_defconfig).
STAGE1_BUILTINS: Final = (
    # Liveness (design §2.8): the watchdog exists before stage 1, the emergency restart works,
    # and a task stuck in D state panics.
    "WATCHDOG_CORE", "BCM2835_WDT", "MAGIC_SYSRQ", "DETECT_HUNG_TASK",
    # The kernel's own DHCP, which publishes option 42 in /proc/net/ipconfig/ntp_servers
    # (design §5), and the Pi 5's Ethernet path to it: PCIe host, RP1, its MAC.
    "IP_PNP", "IP_PNP_DHCP", "PCIE_BRCMSTB", "MFD_RP1", "MACB",
)


def require_builtin(config: Path, symbols: Sequence[str]) -> list[str]:
    """The symbols in `symbols` that `config` does not build in (`=y`).

    A symbol built as a module (`=m`), left unset, or commented out
    ("# CONFIG_FOO is not set") is a miss: stage 1 needs these compiled in,
    not loadable, because nothing in the initramfs can `modprobe` them.
    Returns [] when every symbol is `=y`."""
    text = config.read_text()
    lines = {line.split("=", 1)[0]: line for line in text.splitlines()
             if "=" in line and line.split("=", 1)[0].startswith("CONFIG_")}
    missing = []
    for symbol in symbols:
        key = symbol if symbol.startswith("CONFIG_") else f"CONFIG_{symbol}"
        line = lines.get(key)
        if line is None or line.split("=", 1)[1] != "y":
            missing.append(symbol)
    return missing


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path,
                        help="the kernel .config file to check")
    parser.add_argument("--symbol", action="append", default=[], dest="symbols",
                        metavar="SYMBOL",
                        help="a CONFIG_ symbol that must be built in (repeatable; "
                             "default: STAGE1_BUILTINS)")
    args = parser.parse_args(argv)

    symbols = args.symbols or STAGE1_BUILTINS
    missing = require_builtin(args.config, symbols)
    if missing:
        print(f"kernel_config_check: not built in: {', '.join(missing)}", file=sys.stderr)
        return 1
    print(f"kernel_config_check: OK ({len(symbols)} symbol(s) built in)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
