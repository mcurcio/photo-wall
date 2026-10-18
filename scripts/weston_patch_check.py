#!/usr/bin/env python3
"""The Weston build's tripwire (docs/module-debian-packaging.md, "Weston"): run by
debian-packaging/build-weston.sh over the patched source tree, before dpkg-buildpackage, it refuses
a libweston DRM backend that visibly still gives two KMS planes one bit of a plane mask.

- Narrow mask: a `*plane_mask` declared as anything but uint64_t.
- Narrow bit: a shift by a `*plane_idx` whose left operand is not a 64-bit one (`UINT64_C(1)`,
  `1ULL`), such as `1 << plane->plane_idx`.
- No guard: drm-internal.h does not define `DRM_PLANE_MASK_BITS` as 64 or does not
  static_assert drm_fb::plane_mask against it, or drm.c (where the hardware planes are indexed)
  or drm-virtual.c (where a virtual output's plane is) compares no plane count or index with it.

It reads text, so it is a tripwire for a Weston bump, not the guarantee: a mask under another
name, a BIT() macro or a guard that compares but skips nothing passes it. The guarantees are the
patch's own: every bit comes from drm_plane_mask_bit(), which asserts the index fits, and a
static_assert holds drm_fb::plane_mask to DRM_PLANE_MASK_BITS. Weston's DRM tests need a KMS
device, so no automated tier runs the guard's branches; the Pi bench check does
(docs/module-debian-packaging.md, "Weston").

Build tooling: stdlib only, so the build container's python3 runs it (`python3 -I -B`).
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Final, Sequence

BACKEND: Final = Path("libweston/backend-drm")
WIDE_ONE: Final = frozenset({"UINT64_C(1)", "1ULL", "1ull", "((uint64_t)1)", "(uint64_t)1"})
MASK_DECLARATION: Final = re.compile(
    r"\b((?:const\s+)?(?:unsigned\s+(?:long\s+)*int|unsigned\s+long|unsigned|int|long|"
    r"u?int(?:8|16|32|64)_t|uint))\s+(\w*plane_mask)\b\s*[;=,)]")
SHIFT: Final = re.compile(r"(\(\(uint64_t\)\s*1\)|\(uint64_t\)\s*1|UINT64_C\(1\)|[\w.]+)\s*<<\s*"
                          r"\(?\s*([\w>.-]*plane_idx)\b")
GUARD: Final = re.compile(r"[<>]=?\s*DRM_PLANE_MASK_BITS\b")
GUARDED: Final = ("drm.c", "drm-virtual.c")
DEFINITION: Final = re.compile(r"^#define\s+DRM_PLANE_MASK_BITS\s+64\s*$", re.MULTILINE)
WIDTH_ASSERT: Final = re.compile(
    r"static_assert\(\s*sizeof\(.*->plane_mask\).*DRM_PLANE_MASK_BITS")


def _sources(tree: Path) -> list[Path]:
    return sorted(path for path in (tree / BACKEND).glob("*") if path.suffix in (".c", ".h"))


def findings(tree: Path) -> list[str]:
    """One line per refusal, `<path>:<line>: <what>`, in path order; empty when the backend's
    plane masks are 64 bits wide and guarded."""
    sources = _sources(tree)
    if not sources:
        return [f"{BACKEND}: no DRM backend sources under {tree}"]
    found: list[str] = []
    for path in sources:
        text = path.read_text(errors="replace")
        relative = path.relative_to(tree)
        for match in MASK_DECLARATION.finditer(text):
            kind = " ".join(match.group(1).split())
            if kind not in ("uint64_t", "const uint64_t"):
                line = text.count("\n", 0, match.start()) + 1
                found.append(f"{relative}:{line}: narrow mask: {kind} {match.group(2)}")
        for match in SHIFT.finditer(text):
            one = re.sub(r"\s+", "", match.group(1))
            if one not in WIDE_ONE:
                line = text.count("\n", 0, match.start()) + 1
                found.append(f"{relative}:{line}: narrow bit: {one} << {match.group(2)}")
    header = tree / BACKEND / "drm-internal.h"
    header_text = header.read_text(errors="replace") if header.is_file() else ""
    if not DEFINITION.search(header_text):
        found.append(f"{BACKEND / 'drm-internal.h'}: no guard: DRM_PLANE_MASK_BITS is not 64")
    if not WIDTH_ASSERT.search(header_text):
        found.append(f"{BACKEND / 'drm-internal.h'}: no guard: drm_fb::plane_mask's width is "
                     "not static_asserted against DRM_PLANE_MASK_BITS")
    for name in GUARDED:
        path = tree / BACKEND / name
        if not path.is_file() or not GUARD.search(path.read_text(errors="replace")):
            found.append(f"{BACKEND / name}: no guard: nothing is compared with "
                         "DRM_PLANE_MASK_BITS")
    return found


def main(argv: Sequence[str] | None = None) -> int:
    """TREE; exit 1 with one line per refusal, 0 with none."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tree", type=Path, help="the unpacked, patched Weston source tree")
    args = parser.parse_args(argv)
    lines = findings(args.tree)
    for line in lines:
        print(line)
    return 1 if lines else 0


if __name__ == "__main__":
    raise SystemExit(main())
