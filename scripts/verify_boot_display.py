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

The tools' version matters: Ubuntu 24.04's device-tree-compiler 1.7.0 fails to apply
vc4-kms-v3d-pi5 to the Pi 5 DTB (FDT_ERR_NOTFOUND) where Debian trixie's 1.7.2 applies the same
bytes. So --tools-root runs them inside a root built from the Debian declaration's pin
(base-image.yml: the kernel's own scratch root, where device-tree-compiler is an initrd-build
package), never the runner's.

Stdlib only.
"""

from __future__ import annotations

import argparse
import shutil
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


class Tools:
    """How fdtoverlay and fdtget run: on this host, or chrooted into `root` (the pinned tools),
    where every file they touch must be under `root` and is named by its path inside it."""

    def __init__(self, root: Path | None = None, *, run: Run = subprocess.run) -> None:
        self.root, self._run = root, run

    def path(self, path: Path) -> str:
        return str(path) if self.root is None else "/" + str(path.relative_to(self.root))

    def __call__(self, *argv: str) -> subprocess.CompletedProcess:
        prefix = [] if self.root is None else ["chroot", str(self.root)]
        return self._run([*prefix, *argv], capture_output=True, text=True, check=False)


def applied_violations(boot: Path, names: Sequence[str], work: Path, *,
                       tools: Tools | None = None) -> list[str]:
    """Step 2: the named overlays applied to the staged DTB; every DISPLAY_NODES `okay`. With a
    chrooted `tools`, `boot` and `work` must be under its root."""
    tools = tools or Tools()
    merged = work / "applied.dtb"
    if not names:        # fdtoverlay needs one overlay; with none, the kernel gets the bare DTB
        merged = boot / DTB_NAME
    else:
        applied = tools("fdtoverlay", "-i", tools.path(boot / DTB_NAME), "-o",
                        tools.path(merged),
                        *(tools.path(boot / "overlays" / f"{name}.dtbo") for name in names))
        if applied.returncode != 0:
            return [f"fdtoverlay could not apply {', '.join(names)} to {DTB_NAME}: "
                    f"{(applied.stderr or applied.stdout).strip()}"]
    violations = []
    for label in DISPLAY_NODES:
        path = tools("fdtget", tools.path(merged), "/__symbols__", label)
        if path.returncode != 0:
            violations.append(f"{DTB_NAME} has no node labelled {label}")
            continue
        status = tools("fdtget", tools.path(merged), path.stdout.strip(), "status")
        # No status property means enabled (devicetree specification, "status").
        value = status.stdout.strip() if status.returncode == 0 else "okay"
        print(f"{label} {path.stdout.strip()} status={value}")
        if value not in ("okay", "ok"):
            violations.append(f"{label} ({path.stdout.strip()}) is {value} after the overlays")
    return violations


def apply_in(boot: Path, names: Sequence[str], tools_root: Path | None, *,
             run: Run = subprocess.run) -> list[str]:
    """Step 2 in a scratch directory: on this host, or under `tools_root/tmp` with copies of the
    DTB and the named overlays, removed afterwards."""
    parent = None if tools_root is None else tools_root / "tmp"
    with tempfile.TemporaryDirectory(prefix="boot-display-", dir=parent) as scratch:
        work = Path(scratch)
        tools = Tools(tools_root, run=run)
        if tools_root is None:
            return applied_violations(boot, names, work, tools=tools)
        staged = work / "boot"
        (staged / "overlays").mkdir(parents=True)
        shutil.copy2(boot / DTB_NAME, staged / DTB_NAME)
        for name in names:
            shutil.copy2(boot / "overlays" / f"{name}.dtbo", staged / "overlays" / f"{name}.dtbo")
        version = tools("fdtoverlay", "--version")
        print(f"tools: chroot {tools_root}: {(version.stdout or version.stderr).strip()}")
        return applied_violations(staged, names, work, tools=tools)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("boot", type=Path, help="the bundle's boot/ directory")
    parser.add_argument("--apply", action="store_true",
                        help="apply the overlays to the DTB with fdtoverlay and check the nodes")
    parser.add_argument("--tools-root", type=Path,
                        help="run fdtoverlay/fdtget chrooted into this root (needs root)")
    args = parser.parse_args(argv)
    names, violations = config_violations(args.boot)
    if not violations and args.apply:
        violations = apply_in(args.boot, names, args.tools_root)
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
