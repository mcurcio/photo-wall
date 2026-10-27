"""Room lights in the console's words: central/console/src/domain/room-lights/model.ts, the one home
of a room light's state line, who reports it in light terms, its colour line (Central's
ColourLineView in words: typed, following its bulb or a colour sensor, typed until its source says,
"Can't tell since …", "Can't read since …" or "Link closed since …" (Central's doubt) with the colour kept, and "outside what the screen draws" past the screen's
colour table), a Sensor's reading and the id a name gives, bundled by the console's own esbuild and run under Node (it is pure). The ids it
derives are checked against contracts/room_light.py's own rule. Every refusal code having words is
the typecheck's (a `Record` over the generated refusal union).

The House › Lights picker's choices come from Home Assistant's states as Central last heard them: a
light picks only from light entities and a Sensor only from illuminance entities, each shown by its
name with its entity id as the detail, and the light or Sensor it saves names the entity.

Mutation probes: "Off" for a not-reported state -> the state case fails; the id keeps
upper case or a trailing hyphen -> the contract rejects it; `entitiesFor` keeps illuminance entities
for a light -> the picker case fails; `tableEndKelvin` bounded by KELVIN_STATED_MAX instead of
KELVIN_TABLE_MAX -> the 34,000 K line loses "outside what the screen draws (drawn at 10,000 K)" and
the colour line case fails.
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
FIXED = {"kind": "typed", "kelvin": 2700}
KELVIN = {"kind": "kelvin", "kelvin": 2700}
HEARD = [
    {"entity": "light.kitchen", "kind": "light", "name": "Kitchen", "reports": None, "heard_at": 1},
    {"entity": "sensor.porch_illuminance", "kind": "illuminance", "name": "Porch", "reports": None, "heard_at": 1},
    {"entity": "light.hall", "kind": "light", "name": None, "reports": "hall", "heard_at": None},
    {"entity": "sensor.sky_colour", "kind": "colour-temperature", "name": "Sky", "reports": None, "heard_at": 1},
]
# Central's instants: the clock the test hands the model says 18:42 for CANT_TELL.
CANT_TELL = 1_791_000_000
# Central's doubts (DoubtView), each since CANT_TELL.
DOUBT = {reason: {"reason": reason, "since": CANT_TELL} for reason in ("cant-tell", "cant-read", "link-closed")}
SKY = {"sky": "Sky colour"}


def _line(kelvin=None, rgb=None, *, source="typed", sensor=None, colour_from="typed", doubt=None):
    """A ColourLineView, as Central sends it."""
    colour = {"kind": "rgb", "rgb": rgb} if rgb is not None else {"kind": "kelvin", "kelvin": kelvin}
    return {"colour": colour, "source": source, "sensor": sensor, "colour_from": colour_from,
            "stated_at": None if colour_from != "following" else CANT_TELL - 60, "doubt": doubt}


BULB = {"source": "its-bulb"}
SENSOR = {"source": "colour-sensor", "sensor": "sky"}
# Each ColourLineView kind and the line the console says for it.
LINES = [
    (_line(5500), "Typed 5,500 K"),
    (_line(3400, **BULB, colour_from="following"), "Following its bulb 3,400 K"),
    (_line(3400, **SENSOR, colour_from="following"), "Following Sky colour 3,400 K"),
    (_line(rgb=[1, 0.8, 0.6], **BULB, colour_from="following"), "Following its bulb's colour"),
    (_line(5500, **BULB, colour_from="never-stated"), "Typed 5,500 K until its bulb says"),
    (_line(5500, **SENSOR, colour_from="never-stated"), "Typed 5,500 K until Sky colour says"),
    (_line(3400, **SENSOR, colour_from="following", doubt=DOUBT["cant-tell"]), "Can't tell since 18:42 (keeping 3,400 K)"),
    (_line(3400, **BULB, colour_from="following", doubt=DOUBT["cant-tell"]), "Can't tell since 18:42 (keeping 3,400 K)"),
    # Production `room` (a brightness-only bulb, 5,000 K typed) while Home Assistant says unavailable:
    # its typed colour is never said to be kept.
    (_line(5000, **BULB, colour_from="never-stated", doubt=DOUBT["cant-tell"]),
     "Typed 5,000 K until its bulb says · can't tell since 18:42"),
    # Central can't read the bulb's reports (an older Galleria), or its link closed: the colour kept.
    (_line(3400, **BULB, colour_from="following", doubt=DOUBT["cant-read"]), "Can't read since 18:42 (keeping 3,400 K)"),
    (_line(5000, **BULB, colour_from="never-stated", doubt=DOUBT["link-closed"]),
     "Typed 5,000 K until its bulb says · link closed since 18:42"),
    (_line(34000, **BULB, colour_from="following"),
     "Following its bulb 34,000 K · outside what the screen draws (drawn at 10,000 K)"),
    (_line(1500, **SENSOR, colour_from="following"),
     "Following Sky colour 1,500 K · outside what the screen draws (drawn at 2,000 K)"),
    (_line(10000, **BULB, colour_from="following"), "Following its bulb 10,000 K"),
    (_line(2000), "Typed 2,000 K"),
]

SCRIPT = r"""
const words = await import(process.argv[1]);
const input = JSON.parse(process.argv[2]);
const clock = (at) => (at === input.cantTell ? "18:42" : `at ${at}`);
const light = (doubt, state) => ({ entry: { kind: "reported", colour: input.kelvin, state }, doubt });
const white = (state) => words.entryWords({ kind: "reported", colour: input.kelvin, state });
const sensors = [{ id: "window-meter", sensor: { name: "Window meter", at: { source: "x", address: "y" } },
                   reading: null, heard_at: null, reports: null }];
process.stdout.write(JSON.stringify({
  states: [
    white({ kind: "dimmer", on: true, brightness: 153 / 255 }),
    white({ kind: "dimmer", on: false, brightness: 0 }),
    words.entryWords({ kind: "measured", colour: input.kelvin, state: { kind: "measured", lux: 1234.4 } }),
    words.entryWords({ kind: "measured", colour: input.kelvin, state: { kind: "measured", lux: 2.46 } }),
    white({ kind: "not-reported" }),
    words.entryWords({ kind: "fixed", colour: input.kelvin }),
  ],
  readings: [words.readingWords({ kind: "measured", lux: 320 }), words.readingWords({ kind: "unavailable" }),
             words.readingWords(null)],
  reporters: [
    words.reporterWords({ kind: "reported", name: "Spot", reporter: { source: "x", address: "y" }, colour: input.fixed }, sensors),
    words.reporterWords({ kind: "measured", name: "Daylight", sensor: "window-meter", colour: input.fixed }, sensors),
    words.reporterWords({ kind: "fixed", name: "Lamp", colour: input.fixed }, sensors),
  ],
  lines: input.lines.map((line) => words.colourLineWords(line, input.sky, clock)),
  swatches: input.lines.map((line) => words.colourSwatch(line)),
  brightness: [
    words.brightnessWords(light(null, { kind: "dimmer", on: true, brightness: 0.6 }), clock),
    words.brightnessWords(light(input.doubt["cant-tell"], { kind: "dimmer", on: true, brightness: 0.6 }), clock),
    words.brightnessWords(light(input.doubt["cant-tell"], { kind: "dimmer", on: false, brightness: 0 }), clock),
    words.brightnessWords(light(input.doubt["cant-tell"], { kind: "not-reported" }), clock),
    words.brightnessWords(light(input.doubt["cant-read"], { kind: "dimmer", on: false, brightness: 0 }), clock),
    words.brightnessWords(light(input.doubt["link-closed"], { kind: "dimmer", on: true, brightness: 0.6 }), clock),
  ],
  sensorReadings: [
    words.sensorReadingWords({ reading: { kind: "measured", lux: 320 }, doubt: input.doubt["cant-tell"] }, clock),
    words.sensorReadingWords({ reading: null, doubt: input.doubt["cant-tell"] }, clock),
    words.sensorReadingWords({ reading: { kind: "measured", lux: 0 }, doubt: input.doubt["link-closed"] }, clock),
  ],
  colourSensors: [
    words.colourReadingWords({ colour: { kind: "kelvin", kelvin: 3400 }, doubt: null }, clock),
    words.colourReadingWords({ colour: { kind: "kelvin", kelvin: 3400 }, doubt: input.doubt["cant-tell"] }, clock),
    words.colourReadingWords({ colour: null, doubt: null }, clock),
    words.followedWords({ followed_by: ["daylight", "porch"] }, { daylight: "Daylight" }),
    words.followedWords({ followed_by: [] }, {}),
  ],
  ids: input.names.map((name) => words.idFromName(name)),
  picker: {
    lights: words.entitiesFor(input.heard, "light").map((entity) => words.entityLabel(entity)),
    sensors: words.entitiesFor(input.heard, "illuminance").map((entity) => words.entityLabel(entity)),
    colourSensors: words.entitiesFor(input.heard, "colour-temperature").map((entity) => words.entityLabel(entity)),
    colourSensor: words.smartColourSensor("Sky colour", "sensor.sky_colour"),
    light: words.reportedThrough("Kitchen", "light.kitchen", input.fixed),
    sensor: words.smartSensor("Porch", "sensor.porch_illuminance"),
    reporter: [words.reporterOf(words.reportedThrough("Kitchen", "light.kitchen", input.fixed)),
      words.reporterOf({ kind: "fixed", name: "Lamp", colour: input.fixed }), words.reporterOf(null)],
  },
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
                             json.dumps({"names": NAMES, "fixed": FIXED, "kelvin": KELVIN, "heard": HEARD,
                                         "lines": [line for line, _ in LINES], "sky": SKY, "cantTell": CANT_TELL,
                                         "doubt": DOUBT})],
                            capture_output=True, text=True, check=True, timeout=30)
    return json.loads(result.stdout)


def test_a_state_reads_from_the_entry_alone(said):
    assert said["states"] == ["On, 60 %", "Off", "1,234 lx", "2.5 lx", "Not reported yet", "On, 100 %"]
    assert said["readings"] == ["320 lx", "Unavailable", "Not reported yet"]


def test_who_reports_a_light_is_said_in_light_terms(said):
    assert said["reporters"] == ["Reports itself", "Measured by Window meter", "Always on"]


def test_a_colour_line_says_its_colour_and_where_it_comes_from(said):
    """Every ColourLineView kind in words, with "outside what the screen draws" only past the screen's
    colour table (2,000..10,000 K), the drawn kelvin the table's bound."""
    assert said["lines"] == [words for _, words in LINES]


def test_a_bulb_s_rgb_is_drawn_as_its_swatch_and_a_kelvin_is_not(said):
    """A colour mode's rgb (shares of 1) is the swatch's CSS colour; a kelvin's words say it in full."""
    swatches = dict(zip((words for _, words in LINES), said["swatches"], strict=True))
    assert swatches["Following its bulb's colour"] == "rgb(255 204 153)"
    assert {swatch for words, swatch in swatches.items() if words != "Following its bulb's colour"} == {None}


def test_what_central_is_in_doubt_of_is_kept_and_said_with_its_reason(said):
    """Each doubt reason (its source can't tell, Photo Wall can't read it, its link closed) says the
    last word kept, never a new one."""
    assert said["brightness"] == ["On, 60 %", "Can't tell since 18:42 (keeping On, 60 %)",
                                  "Can't tell since 18:42 (keeping Off)", "Can't tell since 18:42",
                                  "Can't read since 18:42 (keeping Off)", "Link closed since 18:42 (keeping On, 60 %)"]
    assert said["sensorReadings"] == ["Can't tell since 18:42 (keeping 320 lx)", "Can't tell since 18:42",
                                      "Link closed since 18:42 (keeping 0 lx)"]
    assert said["colourSensors"] == ["3,400 K", "Can't tell since 18:42 (keeping 3,400 K)", "Not reported yet",
                                     "Daylight, porch", "No light"]


def test_a_name_gives_an_id_the_contract_takes(said):
    ids = said["ids"]
    assert ids[:4] == ["bookshelf-spot", "overheads-rgb", "cafe-lamp", "daylight-window"]
    assert ids[5] == "spot" and ids[6] == ""
    assert all(re.fullmatch(ROOM_LIGHT_ID_PATTERN, each) for each in ids[:6]), ids
    assert ids[4] == "a" * len(ids[4]) and re.fullmatch(ROOM_LIGHT_ID_PATTERN, ids[4] + "a") is None   # as long as an id goes


def test_the_picker_offers_each_kind_of_entity_for_its_own_setup_item(said):
    """A light picks from light entities, a Sensor from illuminance entities, a colour sensor from
    colour-temperature entities."""
    picker = said["picker"]
    assert picker["lights"] == ["Kitchen (light.kitchen)", "light.hall"]
    assert picker["sensors"] == ["Porch (sensor.porch_illuminance)"]
    assert picker["colourSensors"] == ["Sky (sensor.sky_colour)"]
    assert picker["colourSensor"] == {"name": "Sky colour", "at": {"source": "home-assistant", "address": "sensor.sky_colour"}}
    assert picker["light"] == {"kind": "reported", "name": "Kitchen", "colour": FIXED,
                               "reporter": {"source": "home-assistant", "address": "light.kitchen"}}
    assert picker["sensor"] == {"name": "Porch", "at": {"source": "home-assistant", "address": "sensor.porch_illuminance"}}
    assert picker["reporter"] == ["light.kitchen", None, None]
