"""One node token, pinned everywhere it is frozen by hand.

The release cmdline template carries `CMDLINE_NODE_TOKEN` (contracts/release.py). A new cmdline
over an older selected base still takes the node path only while every site that spells the token
by hand spells this one: the node units' conditions, the V1-off drop-in, the initramfs's branch
and the base-image content check. Device code does not import the constant (the initramfs and the
node packages stay unchanged), so this test is the binding.
"""
from __future__ import annotations

import re
from pathlib import Path

from contracts.release import CMDLINE_NODE_TOKEN

REPO = Path(__file__).resolve().parents[1]
KEY, VALUE = CMDLINE_NODE_TOKEN.split("=", 1)
CONDITION = re.compile(r"^ConditionKernelCommandLine=(.*)$", re.MULTILINE)


def _node_conditions() -> dict[str, list[str]]:
    """Each systemd unit's ConditionKernelCommandLine values that name the node key."""
    found = {}
    for path in sorted((REPO / "appliance/systemd").iterdir()):
        if path.is_file():
            values = [value for value in CONDITION.findall(path.read_text())
                      if value.lstrip("!").startswith(KEY + "=")]
            if values:
                found[path.name] = values
    return found


def test_every_node_unit_condition_spells_the_template_token():
    found = _node_conditions()
    assert found
    assert {name: values for name, values in found.items() if values != [CMDLINE_NODE_TOKEN]} == {}


def test_the_v1_off_drop_in_negates_the_template_token():
    text = (REPO / "scripts/build_node_base_deb.py").read_text()
    values = CONDITION.findall(text.replace("\\n", "\n"))
    assert values == [f"!{CMDLINE_NODE_TOKEN}"]


def test_the_initramfs_node_branch_compares_the_template_key_and_value():
    text = (REPO / "appliance/netboot_init.py").read_text()
    compared = re.findall(r'\.get\("(photowall\.node)"\) == "([^"]+)"', text)
    assert compared == [(KEY, VALUE)]


def test_the_base_image_content_check_names_the_template_token():
    assert CMDLINE_NODE_TOKEN in (REPO / ".github/workflows/base-image.yml").read_text()
