"""scripts/weston_patch_check.py, the Weston build's refusal: over small DRM backend trees, it
flags every 32-bit plane mask and plane bit (each form Weston 14.0.2 had), accepts the 64-bit
forms the patch writes, and requires the index guard. The committed patch and its series are
the ones the build applies."""

import re
from pathlib import Path

import pytest

from scripts.weston_patch_check import findings, main

REPO = Path(__file__).resolve().parents[1]
PATCHES = REPO / "debian-packaging/weston/patches"
BACKEND = "libweston/backend-drm"

# The patched forms: the header's guard and helper, the two hardware and virtual guards.
HEADER = """\
struct drm_fb {
\tuint64_t plane_mask;
};
#define DRM_PLANE_MASK_BITS 64
static inline uint64_t
drm_plane_mask_bit(const struct drm_plane *plane)
{
\tassert(plane->plane_idx < DRM_PLANE_MASK_BITS);
\treturn UINT64_C(1) << plane->plane_idx;
}
"""
DRM = "\t\tif (next_plane_idx >= DRM_PLANE_MASK_BITS) {\n"
VIRTUAL = "\tif (plane->plane_idx >= DRM_PLANE_MASK_BITS) {\n"
PROPOSE = """\
\tuint64_t possible_plane_mask = 0;
\tpossible_plane_mask |= drm_plane_mask_bit(plane);
\tpossible_plane_mask &= ~drm_plane_mask_bit(plane);
"""


def tree(root: Path, files: dict[str, str] | None = None) -> Path:
    """A DRM backend tree of the patched files, each file `files` names holding its text."""
    texts = {"drm-internal.h": HEADER, "drm.c": DRM, "drm-virtual.c": VIRTUAL,
             "state-propose.c": PROPOSE, "fb.c": "\tfb->plane_mask |= drm_plane_mask_bit(plane);\n",
             **(files or {})}
    for name, text in texts.items():
        path = root / BACKEND / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_the_patched_forms_pass(tmp_path, capsys):
    assert findings(tree(tmp_path)) == []
    assert main([str(tmp_path)]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("line", [
    "\tuint32_t plane_mask;\n",
    "\tuint32_t possible_plane_mask = 0;\n",
    "\tunsigned int plane_mask;\n",
    "\tunsigned long plane_mask;\n",
])
def test_a_narrow_mask_is_refused(tmp_path, line):
    found = findings(tree(tmp_path, {"state-propose.c": PROPOSE + line}))
    assert len(found) == 1 and "narrow mask" in found[0]
    assert found[0].startswith(f"{BACKEND}/state-propose.c:4: ")


@pytest.mark.parametrize("line", [
    "\tfb->plane_mask |= 1 << (plane->plane_idx);\n",
    "\tpossible_plane_mask = (1 << output->cursor_plane->plane_idx);\n",
    "\tif (!(possible_plane_mask & (1 << plane->plane_idx)))\n",
    "\tpossible_plane_mask &= ~(1 << plane->plane_idx);\n",
    "\tmask |= 1u << plane->plane_idx;\n",
    "\tmask |= 1UL << plane->plane_idx;\n",
])
def test_a_narrow_bit_is_refused(tmp_path, line):
    found = findings(tree(tmp_path, {"fb.c": line}))
    assert len(found) == 1 and "narrow bit" in found[0], found


@pytest.mark.parametrize("line", ["\tmask |= 1ULL << plane->plane_idx;\n",
                                  "\tmask |= UINT64_C(1) << plane->plane_idx;\n",
                                  "\tmask |= (uint64_t)1 << plane->plane_idx;\n"])
def test_a_wide_bit_passes(tmp_path, line):
    assert findings(tree(tmp_path, {"fb.c": line})) == []


@pytest.mark.parametrize(("files", "missing"), [
    ({"drm.c": "\t\tdrm_plane->plane_idx = next_plane_idx++;\n"}, "drm.c"),
    ({"drm-virtual.c": "\tplane->plane_idx = get_drm_plane_index_maximum(device) + 1;\n"},
     "drm-virtual.c"),
    ({"drm-internal.h": HEADER.replace("BITS 64", "BITS 32")}, "drm-internal.h"),
])
def test_a_missing_guard_is_refused(tmp_path, files, missing):
    found = findings(tree(tmp_path, files))
    assert found == [next(line for line in found if line.startswith(f"{BACKEND}/{missing}: no guard"))]


def test_a_tree_without_the_backend_is_refused(tmp_path, capsys):
    assert main([str(tmp_path)]) == 1
    assert "no DRM backend sources" in capsys.readouterr().out


def test_the_series_is_the_committed_patches_each_a_dep3_patch_of_the_drm_backend():
    """Every patch build-weston.sh appends to Debian's series is committed and listed once, in
    order, and says where it stands upstream (DEP-3), touching only the DRM backend."""
    series = (PATCHES / "series").read_text().split()
    assert series == sorted(path.name for path in PATCHES.glob("*.patch"))
    for name in series:
        text = (PATCHES / name).read_text()
        header = text.split("\n---\n", 1)[0]
        for field in ("Subject", "Forwarded", "Bug", "Last-Update"):
            assert re.search(rf"^{field}: \S", header, re.MULTILINE), (name, field)
        touched = re.findall(r"^\+\+\+ b/(\S+)", text, re.MULTILINE)
        assert touched and all(path.startswith(f"{BACKEND}/") for path in touched), name


def test_the_patch_adds_no_narrow_form_and_brings_the_guard():
    """The patch's added lines are what the build check accepts: no 32-bit plane mask or bit,
    and the guard in each of its three homes."""
    text = (PATCHES / "0001-backend-drm-use-64-bit-plane-masks.patch").read_text()
    added: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("+++ b/"):
            current = line.removeprefix("+++ b/").split("/")[-1]
        elif current and line.startswith("+"):
            added.setdefault(current, []).append(line[1:])
    assert "#define DRM_PLANE_MASK_BITS 64" in added["drm-internal.h"]
    assert any(">= DRM_PLANE_MASK_BITS" in line for line in added["drm.c"])
    assert any(">= DRM_PLANE_MASK_BITS" in line for line in added["drm-virtual.c"])
    every = "\n".join(line for lines in added.values() for line in lines)
    assert not re.search(r"uint32_t\s+\w*plane_mask", every)
    assert not re.search(r"\b1u?\s*<<\s*\(?\s*[\w>.-]*plane_idx", every)
