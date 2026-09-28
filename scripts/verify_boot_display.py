#!/usr/bin/env python3
"""The netboot bundle turns the Pi 5's display on in its device tree (CI guard, no hardware).

v0.9.1's config.txt loaded no KMS overlay. In the bare bcm2712-rpi-5-b.dtb the display pipeline
and the GPU are `status = "disabled"`, so the kernel created no platform device for them, udev
never loaded vc4 or v3d, and /dev/dri never appeared, whatever modules the initrd carried. This
checks the staged boot/ directory:

  1. config.txt loads DISPLAY_OVERLAY (`dtoverlay=vc4-kms-v3d-pi5`), and every overlay config.txt
     names exists as boot/overlays/<name>.dtbo, which is where the firmware looks for it;
  2. with --apply (needs `fdtoverlay` and `fdtget`, from device-tree-compiler): every overlay
     config.txt names, applied to the staged DTB in order as the firmware does, leaves each of
     DISPLAY_NODES `okay`. This is the device tree the kernel would get, short of the firmware's
     own run-time edits.

Stdlib only.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

# The Pi 5 full-KMS overlay. The generic `vc4-kms-v3d` resolves to it only through
# overlay_map.dtb, which the bundle does not ship; applied to the Pi 5 DTB, the generic one fails.
DISPLAY_OVERLAY: Final = "vc4-kms-v3d-pi5"
DTB_NAME: Final = "bcm2712-rpi-5-b.dtb"
# The DTB's own labels (/__symbols__) for what vc4 and v3d bind to: the vc4 gpu node, its HVS,
# pixel valves and HDMI encoders, and the v3d GPU. All `disabled` in the bare DTB.
DISPLAY_NODES: Final = ("vc4", "hvs", "pixelvalve0", "pixelvalve1", "hdmi0", "hdmi1", "v3d")

Run = Callable[..., subprocess.CompletedProcess]


def overlays(config: str) -> list[str]:
    """PURE. The overlay names config.txt loads, in order: each `dtoverlay=<name>[,params]` line.
    Comments, other settings and a bare `dtoverlay=` (which only ends a parameter block) are not
    overlays. Conditional sections are not modelled: this bundle's config.txt has none, and a
    `[...]` filter line is refused so one cannot hide the overlay from this check."""
    names = []
    for raw in config.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.startswith("["):
            raise ValueError(f"conditional section {line!r} is not modelled by this check")
        key, sep, value = line.partition("=")
        if sep and key.strip() == "dtoverlay":
            name = value.split(",", 1)[0].strip()
            if name:
                names.append(name)
    return names


def config_violations(boot: Path) -> tuple[list[str], list[str]]:
    """Step 1: the overlays config.txt loads, and the violations."""
    try:
        names = overlays((boot / "config.txt").read_text())
    except (OSError, ValueError) as error:
        return [], [f"config.txt unreadable: {error}"]
    violations = []
    if DISPLAY_OVERLAY not in names:
        violations.append(f"config.txt does not load the display overlay "
                          f"(dtoverlay={DISPLAY_OVERLAY}); vc4 and v3d find no device")
    for name in names:
        if not (boot / "overlays" / f"{name}.dtbo").is_file():
            violations.append(f"config.txt loads {name}, but overlays/{name}.dtbo is not in the "
                              "bundle")
    return names, violations


def applied_violations(boot: Path, names: Sequence[str], work: Path, *,
                       run: Run = subprocess.run) -> list[str]:
    """Step 2: the named overlays applied to the staged DTB; every DISPLAY_NODES `okay`."""
    merged = work / "applied.dtb"
    if not names:        # fdtoverlay needs one overlay; with none, the kernel gets the bare DTB
        merged = boot / DTB_NAME
    else:
        applied = run(["fdtoverlay", "-i", str(boot / DTB_NAME), "-o", str(merged),
                       *(str(boot / "overlays" / f"{name}.dtbo") for name in names)],
                      capture_output=True, text=True, check=False)
        if applied.returncode != 0:
            return [f"fdtoverlay could not apply {', '.join(names)} to {DTB_NAME}: "
                    f"{(applied.stderr or applied.stdout).strip()}"]
    violations = []
    for label in DISPLAY_NODES:
        path = run(["fdtget", str(merged), "/__symbols__", label], capture_output=True,
                   text=True, check=False)
        if path.returncode != 0:
            violations.append(f"{DTB_NAME} has no node labelled {label}")
            continue
        status = run(["fdtget", str(merged), path.stdout.strip(), "status"],
                     capture_output=True, text=True, check=False)
        # No status property means enabled (devicetree specification, "status").
        value = status.stdout.strip() if status.returncode == 0 else "okay"
        print(f"{label} {path.stdout.strip()} status={value}")
        if value not in ("okay", "ok"):
            violations.append(f"{label} ({path.stdout.strip()}) is {value} after the overlays")
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("boot", type=Path, help="the bundle's boot/ directory")
    parser.add_argument("--apply", action="store_true",
                        help="apply the overlays to the DTB with fdtoverlay and check the nodes")
    args = parser.parse_args(argv)
    names, violations = config_violations(args.boot)
    if not violations and args.apply:
        with tempfile.TemporaryDirectory(prefix="boot-display-") as work:
            violations = applied_violations(args.boot, names, Path(work))
    for violation in violations:
        print(f"VIOLATION: {violation}")
    if violations:
        print(f"FAIL: the bundle does not turn the display on ({len(violations)} violation(s))")
        return 1
    print(f"OK: config.txt loads {', '.join(names)}"
          + (f"; {', '.join(DISPLAY_NODES)} are okay in the applied DTB" if args.apply else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
