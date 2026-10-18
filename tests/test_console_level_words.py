"""A display's own level as the console and the base page show it (decision 0022; owner,
2026-10-12, picked "Percent, raw count second", e.g. "46 %  (117 of 255)"): the console's
central/console/src/levelWords.js against contracts/node_picture.py `HardwareRange.percent`, the
rule the Pi's base page line uses, so a level reads the same percent on both.

Runs the module under Node (it is pure), as tests/test_console_placement.py does. Without Node it
skips on a developer machine, but FAILS where the checks are meant to run in full (`CI` or
`PHOTO_WALL_BROWSER_TESTS` set).

Mutation probes: Math.floor(value * 100 / maximum) in levelWords.js -> 117 of 255 reads 45 -> RED;
Python's round(value * 100 / maximum) in `percent` -> 1 of 8 is 12 -> RED; the count first in
`levelWords` -> the words case fails.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from contracts.node_picture import HardwareRange, RangeKind

ROOT = Path(__file__).parents[1]
MODULE = ROOT / "central/console/src/levelWords.js"
# Every value of the common maxima, and the 16-bit top: the half-way cases (1 of 8, 1 of 200) included.
PAIRS = [(value, maximum) for maximum in (1, 8, 100, 200, 255) for value in range(maximum + 1)]
PAIRS += [(0, 65535), (327, 65535), (32767, 65535), (65535, 65535)]

SCRIPT = r"""
const level = await import(process.argv[1]);
const pairs = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify({
  percents: pairs.map(([value, maximum]) => level.levelPercent(value, maximum)),
  words: level.levelWords(117, 255),
}));
"""


def _require_node():
    if shutil.which("node") is not None:
        return
    if os.environ.get("CI") or os.environ.get("PHOTO_WALL_BROWSER_TESTS"):
        pytest.fail("Node (the console build's) is absent where the console checks must run")
    pytest.skip("Node (the console build's) is absent")


def _percent(value, maximum):
    return HardwareRange(RangeKind.LEVEL, maximum, ()).percent(value)


def test_a_level_reads_as_a_whole_percent_of_its_range_rounded_half_up():
    assert _percent(117, 255) == 46                      # the owner's example
    assert (_percent(1, 8), _percent(1, 200), _percent(0, 100), _percent(255, 255)) == (13, 1, 0, 100)
    for bad in (HardwareRange(RangeKind.LEVEL, 100, ()), HardwareRange(RangeKind.READ_ONLY_CHOICE, None, ())):
        with pytest.raises(ValueError, match="level_value"):
            bad.percent(101)


def test_the_console_shows_the_percent_first_and_the_displays_count_second_by_the_same_rule():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--", MODULE.as_uri(), json.dumps(PAIRS)],
        capture_output=True, text=True, check=True, timeout=60)
    console = json.loads(result.stdout)
    assert console["percents"] == [_percent(value, maximum) for value, maximum in PAIRS]
    assert console["words"] == "46 % (117 of 255)"
