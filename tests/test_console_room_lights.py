"""Room lights in the console's words (decision 0028): central/console/src/roomLightWords.js, the one
home of a room light's state line, its reporter, its colour, a Sensor's reading, the id a name
gives and Settings › Lights' refusals, run under Node (it is pure), as tests/test_console_level_words.py
runs levelWords.js. The ids it derives are checked against contracts/room_light.py's own rule.

Mutation probes: "Not reported yet" for an unavailable state -> the state case fails; the id keeps
upper case or a trailing hyphen -> the contract rejects it; a refusal code without its words -> the
refusal case fails.
"""

import json
import re
import subprocess
from pathlib import Path

from test_console_flow import _require_node

from contracts.room_light import ROOM_LIGHT_ID_PATTERN

MODULE = Path(__file__).parents[1] / "central/console/src/roomLightWords.js"
NAMES = ["Bookshelf spot", "  Overheads (RGB) ", "Café lamp", "Daylight — window", "a" * 40, "Spot-", "!!!"]
REFUSALS = ["named-by-a-frame", "sensor-reports-a-light", "reported-colour-needs-home-assistant",
            "entity-reports-another", "unknown-sensor", "unknown-room-light", "too-many"]

SCRIPT = r"""
const words = await import(process.argv[1]);
const input = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify({
  states: [
    words.stateWords({ kind: "dimmer", on: true, brightness: 153 / 255, rgb: null }),
    words.stateWords({ kind: "dimmer", on: false, brightness: 0, rgb: null }),
    words.stateWords({ kind: "measured", lux: 1234.4 }),
    words.stateWords({ kind: "measured", lux: 2.46 }),
    words.stateWords({ kind: "unavailable" }),
    words.stateWords({ kind: "not-reported" }),
  ],
  readings: [words.readingWords(320), words.readingWords("unavailable"), words.readingWords(null)],
  reporters: [
    words.reporterWords({ kind: "home-assistant-light", entity: "light.bookshelf" }),
    words.reporterWords({ kind: "sensor", sensor: "window-meter" }, { "window-meter": "Window meter" }),
    words.reporterWords({ kind: "fixed" }),
  ],
  colours: [
    words.colourWords({ kind: "fixed", kelvin: 2700 }),
    words.colourWords({ kind: "reported", channel_shares: [0.5, 0.3, 0.2] }),
  ],
  ids: input.names.map((name) => words.idFromName(name)),
  refusals: input.refusals.map((code) => words.refusalWords("Not removed", code)),
  unknown: words.refusalWords("Not saved", "something-new"),
}));
"""


def _run():
    _require_node()
    result = subprocess.run(["node", "--input-type=module", "-e", SCRIPT, str(MODULE),
                             json.dumps({"names": NAMES, "refusals": REFUSALS})],
                            capture_output=True, text=True, check=True, timeout=30)
    return json.loads(result.stdout)


def test_a_state_reads_from_the_state_alone():
    out = _run()
    assert out["states"] == ["On, 60 %", "Off", "1,234 lx", "2.5 lx", "Unavailable", "Not reported yet"]
    assert out["readings"] == ["320 lx", "Unavailable", "Not reported yet"]
    assert out["reporters"] == ["Home Assistant: light.bookshelf", "Sensor: Window meter", "Always on"]
    assert out["colours"] == ["2700 K", "Reported by the bulb (red 50 %, green 30 %, blue 20 %)"]


def test_a_name_gives_an_id_the_contract_takes():
    ids = _run()["ids"]
    assert ids[:4] == ["bookshelf-spot", "overheads-rgb", "cafe-lamp", "daylight-window"]
    assert ids[5] == "spot" and ids[6] == ""
    assert all(re.fullmatch(ROOM_LIGHT_ID_PATTERN, each) for each in ids[:6]), ids
    assert len(ids[4]) == 32


def test_every_refusal_is_said_in_words():
    out = _run()
    assert all(said.startswith("Not removed: ") and "(" not in said for said in out["refusals"]), out["refusals"]
    assert len(set(out["refusals"])) == len(REFUSALS)
    assert out["unknown"] == "Not saved: Central refused it (something-new)."
