"""Room lights in the console's words: central/console/src/domain/room-lights/model.ts, the one home
of a room light's state line, who reports it in light terms, its colour, a Sensor's reading and the
id a name gives, bundled by the console's own esbuild and run under Node (it is pure). The ids it
derives are checked against contracts/room_light.py's own rule. Every refusal code having words is
the typecheck's (a `Record` over the generated refusal union).

Mutation probes: "Not reported yet" for an unavailable state -> the state case fails; the id keeps
upper case or a trailing hyphen -> the contract rejects it.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest
from test_console_flow import _require_node
from test_console_routes_r4 import ESBUILD, _require_esbuild

from contracts.room_light import ROOM_LIGHT_ID_PATTERN

MODEL = Path(__file__).parents[1] / "central/console/src/domain/room-lights/model.ts"
NAMES = ["Bookshelf spot", "  Overheads (RGB) ", "Café lamp", "Daylight — window", "a" * 40, "Spot-", "!!!"]
FIXED = {"kind": "fixed", "kelvin": 2700}

SCRIPT = r"""
const words = await import(process.argv[1]);
const input = JSON.parse(process.argv[2]);
const white = (state) => words.entryWords({ kind: "white", colour: input.fixed, state });
const sensors = [{ id: "window-meter", sensor: { name: "Window meter", at: { source: "x", address: "y" } },
                   reading: null, heard_at: null, reports: null }];
process.stdout.write(JSON.stringify({
  states: [
    white({ kind: "dimmer", on: true, brightness: 153 / 255 }),
    white({ kind: "dimmer", on: false, brightness: 0 }),
    words.entryWords({ kind: "measured", colour: input.fixed, state: { kind: "measured", lux: 1234.4 } }),
    words.entryWords({ kind: "measured", colour: input.fixed, state: { kind: "measured", lux: 2.46 } }),
    white({ kind: "unavailable" }),
    white({ kind: "not-reported" }),
    words.entryWords({ kind: "fixed", colour: input.fixed }),
  ],
  readings: [words.readingWords({ kind: "measured", lux: 320 }), words.readingWords({ kind: "unavailable" }),
             words.readingWords(null)],
  reporters: [
    words.reporterWords({ kind: "reported", name: "Spot", reporter: { source: "x", address: "y" }, colour: input.fixed }, sensors),
    words.reporterWords({ kind: "measured", name: "Daylight", sensor: "window-meter", colour: input.fixed }, sensors),
    words.reporterWords({ kind: "fixed", name: "Lamp", colour: input.fixed }, sensors),
  ],
  colours: [words.colourWords(input.fixed), words.colourWords({ kind: "reported", channel_shares: [0.5, 0.3, 0.2] })],
  ids: input.names.map((name) => words.idFromName(name)),
}));
"""


@pytest.fixture(scope="module")
def said(tmp_path_factory):
    _require_node()
    _require_esbuild()
    bundle = tmp_path_factory.mktemp("room-lights") / "model.mjs"
    subprocess.run([str(ESBUILD), str(MODEL), "--bundle", "--format=esm", "--platform=node", f"--outfile={bundle}",
                    "--log-level=error"], check=True, timeout=60)
    result = subprocess.run(["node", "--input-type=module", "-e", SCRIPT, bundle.as_uri(),
                             json.dumps({"names": NAMES, "fixed": FIXED})],
                            capture_output=True, text=True, check=True, timeout=30)
    return json.loads(result.stdout)


def test_a_state_reads_from_the_entry_alone(said):
    assert said["states"] == ["On, 60 %", "Off", "1,234 lx", "2.5 lx", "Unavailable", "Not reported yet", "On, 100 %"]
    assert said["readings"] == ["320 lx", "Unavailable", "Not reported yet"]


def test_who_reports_a_light_is_said_in_light_terms(said):
    assert said["reporters"] == ["Reports itself", "Measured by Window meter", "Always on"]
    assert said["colours"] == ["2700 K", "Its own colour (red 50 %, green 30 %, blue 20 %)"]


def test_a_name_gives_an_id_the_contract_takes(said):
    ids = said["ids"]
    assert ids[:4] == ["bookshelf-spot", "overheads-rgb", "cafe-lamp", "daylight-window"]
    assert ids[5] == "spot" and ids[6] == ""
    assert all(re.fullmatch(ROOM_LIGHT_ID_PATTERN, each) for each in ids[:6]), ids
    assert ids[4] == "a" * len(ids[4]) and re.fullmatch(ROOM_LIGHT_ID_PATTERN, ids[4] + "a") is None   # as long as an id goes
