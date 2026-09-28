"""scripts/verify_boot_display.py: the bundle turns the Pi 5's display on in its device tree.
The config.txt checks run everywhere; applying real overlays needs device-tree-compiler (the
base-image job installs it and runs this against the staged Pi DTB), so here the tools are faked,
and one test compiles a tiny tree with the real tools when they are present."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.verify_boot_display import (
    DISPLAY_NODES,
    DISPLAY_OVERLAY,
    DTB_NAME,
    applied_violations,
    config_violations,
    main,
    overlays,
)

REPO = Path(__file__).resolve().parents[1]


def bundle_config() -> str:
    """The config.txt build_netboot_bundle.sh writes (its heredoc, read from the script)."""
    script = (REPO / "scripts/build_netboot_bundle.sh").read_text()
    start = script.index('cat > "$boot_dir/config.txt" <<\'EOF\'\n') + len(
        'cat > "$boot_dir/config.txt" <<\'EOF\'\n')
    return script[start:script.index("\nEOF\n", start)]


def boot_dir(tmp_path: Path, config: str, dtbos=(DISPLAY_OVERLAY,)) -> Path:
    boot = tmp_path / "boot"
    (boot / "overlays").mkdir(parents=True)
    (boot / "config.txt").write_text(config)
    (boot / DTB_NAME).write_bytes(b"dtb")
    for name in dtbos:
        (boot / "overlays" / f"{name}.dtbo").write_bytes(b"dtbo")
    return boot


def test_the_bundles_config_loads_the_pi_5_kms_overlay():
    assert overlays(bundle_config()) == [DISPLAY_OVERLAY]


def test_v0_9_1s_config_loads_no_overlay_and_is_refused(tmp_path):
    """v0.9.1's config.txt, as released."""
    v091 = ("arm_64bit=1\nkernel=kernel_2712.img\ninitramfs initrd.img followkernel\n"
            f"device_tree={DTB_NAME}\ndisable_overscan=1\n")
    names, violations = config_violations(boot_dir(tmp_path, v091))
    assert names == []
    assert violations == [f"config.txt does not load the display overlay "
                          f"(dtoverlay={DISPLAY_OVERLAY}); vc4 and v3d find no device"]


def test_the_bundles_config_with_the_overlay_line_removed_is_refused(tmp_path):
    """Mutation probe: the bundle's own config.txt minus its dtoverlay line."""
    config = "\n".join(line for line in bundle_config().splitlines()
                       if not line.startswith("dtoverlay="))
    assert config_violations(boot_dir(tmp_path, config))[1] != []


def test_an_overlay_the_bundle_does_not_ship_is_refused(tmp_path):
    names, violations = config_violations(boot_dir(tmp_path, bundle_config(), dtbos=()))
    assert violations == [f"config.txt loads {DISPLAY_OVERLAY}, but "
                          f"overlays/{DISPLAY_OVERLAY}.dtbo is not in the bundle"]


def test_the_generic_name_is_not_the_pi_5_overlay(tmp_path):
    """`vc4-kms-v3d` reaches the Pi 5 variant only through overlay_map.dtb, never shipped."""
    boot = boot_dir(tmp_path, "dtoverlay=vc4-kms-v3d\n", dtbos=("vc4-kms-v3d",))
    assert config_violations(boot)[1][0].startswith("config.txt does not load the display")


@pytest.mark.parametrize("config,names", [
    ("dtoverlay=a,param=1\n# dtoverlay=b\ndtoverlay=\ndtparam=audio=on\n", ["a"]),
    ("  dtoverlay = vc4-kms-v3d-pi5  # full KMS\n", ["vc4-kms-v3d-pi5"]),
    ("dtoverlay=a\ndtoverlay=b,x\n", ["a", "b"]),
])
def test_overlay_lines_are_read_in_order(config, names):
    assert overlays(config) == names


def test_a_conditional_section_is_refused_not_guessed(tmp_path):
    names, violations = config_violations(boot_dir(tmp_path, "[pi4]\ndtoverlay=x\n"))
    assert violations == ["config.txt unreadable: conditional section '[pi4]' is not modelled "
                          "by this check"]


class FakeFdt:
    """fdtoverlay and fdtget over a dict of label -> (path, status)."""

    def __init__(self, nodes, *, overlay_fails=False):
        self.nodes, self.overlay_fails, self.calls = nodes, overlay_fails, []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == "fdtoverlay":
            code = 1 if self.overlay_fails else 0
            return subprocess.CompletedProcess(argv, code, "",
                                               "Failed to apply: FDT_ERR_NOTFOUND" if code else "")
        if argv[2] == "/__symbols__":
            node = self.nodes.get(argv[3])
            return subprocess.CompletedProcess(argv, 0 if node else 1,
                                               f"{node[0]}\n" if node else "", "")
        status = next(status for path, status in self.nodes.values() if path == argv[2])
        if status is None:
            return subprocess.CompletedProcess(argv, 1, "", "FDT_ERR_NOTFOUND")
        return subprocess.CompletedProcess(argv, 0, f"{status}\n", "")


def nodes(status="okay"):
    return {label: (f"/soc/{label}", status) for label in DISPLAY_NODES}


def test_every_display_node_okay_passes(tmp_path):
    boot = boot_dir(tmp_path, bundle_config())
    fdt = FakeFdt(nodes())
    assert applied_violations(boot, [DISPLAY_OVERLAY], tmp_path, run=fdt) == []
    assert fdt.calls[0] == ["fdtoverlay", "-i", str(boot / DTB_NAME), "-o",
                            str(tmp_path / "applied.dtb"),
                            str(boot / "overlays" / f"{DISPLAY_OVERLAY}.dtbo")]


def test_a_node_without_a_status_is_enabled(tmp_path):
    assert applied_violations(boot_dir(tmp_path, ""), [], tmp_path,
                              run=FakeFdt(nodes(status=None))) == []


def test_the_bare_dtb_leaves_the_display_disabled(tmp_path):
    violations = applied_violations(boot_dir(tmp_path, ""), [], tmp_path,
                                    run=FakeFdt(nodes(status="disabled")))
    assert violations == [f"{label} (/soc/{label}) is disabled after the overlays"
                          for label in DISPLAY_NODES]


def test_an_overlay_that_does_not_apply_is_named(tmp_path):
    assert applied_violations(boot_dir(tmp_path, ""), ["vc4-kms-v3d"], tmp_path,
                              run=FakeFdt(nodes(), overlay_fails=True)) == [
        f"fdtoverlay could not apply vc4-kms-v3d to {DTB_NAME}: "
        "Failed to apply: FDT_ERR_NOTFOUND"]


def test_a_dtb_without_a_display_label_is_named(tmp_path):
    partial = {label: value for label, value in nodes().items() if label != "v3d"}
    assert applied_violations(boot_dir(tmp_path, ""), [], tmp_path, run=FakeFdt(partial)) == [
        f"{DTB_NAME} has no node labelled v3d"]


def test_main_without_apply_checks_the_config_only(tmp_path, capsys):
    assert main([str(boot_dir(tmp_path, bundle_config()))]) == 0
    assert capsys.readouterr().out == f"OK: config.txt loads {DISPLAY_OVERLAY}\n"


TOOLS = all(shutil.which(tool) for tool in ("dtc", "fdtoverlay", "fdtget"))


@pytest.mark.skipif(not TOOLS, reason="needs device-tree-compiler (dtc, fdtoverlay, fdtget)")
def test_real_tools_turn_a_disabled_tree_on(tmp_path, monkeypatch):
    """A tiny tree with the Pi DTB's shape: labelled nodes, disabled, symbols exported; an
    overlay setting them okay. Applied with the real fdtoverlay, read with the real fdtget."""
    labels = "\n".join(f"\t{label}: {label} {{ status = \"disabled\"; }};"
                       for label in DISPLAY_NODES)
    frags = "\n".join(f"\tfragment@{i} {{ target = <&{label}>; __overlay__ {{ "
                      f"status = \"okay\"; }}; }};" for i, label in enumerate(DISPLAY_NODES))
    boot = boot_dir(tmp_path, bundle_config(), dtbos=())
    base, overlay = tmp_path / "base.dts", tmp_path / "overlay.dts"
    base.write_text(f"/dts-v1/;\n/ {{\n{labels}\n}};\n")
    overlay.write_text(f"/dts-v1/;\n/plugin/;\n/ {{\n{frags}\n}};\n")
    subprocess.run(["dtc", "-@", "-I", "dts", "-O", "dtb", "-o", str(boot / DTB_NAME),
                    str(base)], check=True, capture_output=True)
    subprocess.run(["dtc", "-@", "-I", "dts", "-O", "dtb", "-o",
                    str(boot / "overlays" / f"{DISPLAY_OVERLAY}.dtbo"), str(overlay)],
                   check=True, capture_output=True)
    assert main([str(boot), "--apply"]) == 0
    assert applied_violations(boot, [], tmp_path)[0].endswith("is disabled after the overlays")
