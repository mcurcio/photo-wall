"""Pass 4 Show (console DDD §34, §35, bead S1): the `planned` truth kind, a Run's origin and
zoned clock times, run under Node as tests/test_console_flow.py runs the flow kit, plus two
source scans over the console. Without Node the model test skips on a developer machine, but
FAILS where the checks are meant to run in full (`CI` or `PHOTO_WALL_BROWSER_TESTS` set).

  * Every §35 row from stubbed runtimes: a Program's Run, a Show now, a child Run, an unbound
    Frame, a removed Program, nothing on top, a participant with no visible Intent, and a top
    Intent whose root Run is missing (Unknown).
  * `timeWords.js` is the only module that formats an instant for display: a scan fails when
    any other console module calls a display formatter.
  * The Why heading names a child's Run as the `planned` fact does, and the "Why nothing new"
    chain's Intended? step states the top Run through the `planned` fact (C5).
  * No console string says "Scheduled:", "Intended scene", "Now showing", "Central's plan" (the
    class, not only "Central's plan for") or "Why each frame shows what it does".
"""

import json
import os
import re
import subprocess
from pathlib import Path

from tests.test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"

SCRIPT = r"""
const facts = await import(process.argv[1]);
const join = await import(process.argv[2]);
const show = await import(process.argv[3]);
const time = await import(process.argv[4]);
const media = await import(process.argv[5]);
const out = {};

const intent = (frame, scene, run, root, phase = "body") => ({
  target: `frame:${frame}`, scene_id: scene, run_id: run, root_id: root, phase,
  priority: 5, root_order: 1, admission_order: 1 });
const run = (run_id, scene_id, extra = {}) => ({
  run_id, scene_id, root_id: run_id, parent_id: null, program_id: null, phase: "body",
  participants: [], children: [], priority: 5, started_at: 0, ended_at: null,
  finish_requested_at: null, ...extra });

const runtime = {
  programs: { "dec-evenings": { program_id: "dec-evenings" } },
  current: {
    now: 1000,
    runs: [
      run("r-prog", "xmas", { program_id: "dec-evenings", participants: ["frame:a", "frame:c"],
        children: ["r-child"] }),
      run("r-child", "intro", { parent_id: "r-prog", root_id: "r-prog", participants: ["frame:c"] }),
      run("r-direct", "lobby", { participants: ["frame:b"] }),
      run("r-gone", "old", { program_id: "removed-one", participants: ["frame:d"] }),
      run("r-quiet", "quiet", { participants: ["frame:e"] }),
    ],
    visible: [
      intent("a", "xmas", "r-prog", "r-prog"),
      intent("b", "lobby", "r-direct", "r-direct", "outro"),
      intent("c", "intro", "r-child", "r-prog"),
      intent("d", "old", "r-gone", "r-gone"),
      intent("f", "orphan", "r-orphan", "r-missing"),
    ],
  },
};

const planned = (frame, bound = true) => {
  const value = join.plannedFor(runtime, frame, bound);
  return { text: facts.factText(value.fact), kind: value.fact.kind, sceneId: value.sceneId,
           phase: value.phase };
};
out.rows = {
  program: planned("a"),
  direct: planned("b"),
  child: planned("c"),
  unbound: planned("a", false),
  removed: planned("d"),
  nothing: planned("z"),
  participantNoIntent: planned("e"),
  missingRoot: planned("f"),
};
out.nullRuntime = facts.factText(join.plannedFor(null, "a", true).fact);

// The fact's own construction: origin and basis are required labels; "nothing" has its own
// builder, so a Scene named "nothing" is still a Scene.
out.construction = {
  noOrigin: facts.factText(facts.fact({ kind: "planned", value: "xmas", basis: "media not checked" })),
  noBasis: facts.factText(facts.fact({ kind: "planned", value: "xmas", origin: "Program p" })),
  noValue: facts.factText(facts.fact({ kind: "planned", origin: "Program p", basis: "b" })),
  sceneNamedNothing: facts.factText(facts.fact({ kind: "planned", value: "nothing", origin: "Program p",
    basis: "media not checked" })),
  noAge: facts.fact({ kind: "planned", value: "xmas", origin: "o", basis: "b", receivedAt: 1, readAt: 9 }).age,
};

const byId = (id) => runtime.current.runs.find((candidate) => candidate.run_id === id);
out.origins = Object.fromEntries(["r-prog", "r-child", "r-direct", "r-gone"].map(
  (id) => [id, show.runOrigin(runtime, byId(id))]));
out.orphanChild = show.runOrigin(runtime, run("x", "y", { parent_id: "p", root_id: "p" })).words;
const rows = show.runRows({ runtime });
out.rowOrigins = Object.fromEntries(rows.live.map((row) => [row.run.run_id, row.origin]));
out.childRowOrigin = rows.live.find((row) => row.run.run_id === "r-prog").children[0].origin;

out.heading = join.explainPrecedence(
  { programs: runtime.programs, current: { runs: runtime.current.runs, contributions: [intent("a", "xmas", "r-prog", "r-prog")] } },
  "a").heading;
out.directHeading = join.explainPrecedence(
  { programs: runtime.programs, current: { runs: runtime.current.runs, contributions: [intent("b", "lobby", "r-direct", "r-direct")] } },
  "b").heading;
out.childHeading = join.explainPrecedence(
  { programs: runtime.programs, current: { runs: runtime.current.runs, contributions: [intent("c", "intro", "r-child", "r-prog")] } },
  "c").heading;
const why = (frames) => media.whyNothingNew({
  runtime: { programs: runtime.programs, current: { runs: runtime.current.runs,
    contributions: [{ ...intent("a", "xmas", "r-prog", "r-prog"), kind: "media", asset_refs: ["x"] }] } },
  inventory: { frames }, media: {} }, "a")[0];
out.intended = why([{ id: "a", player_id: "p", output_id: "o" }]);
out.intendedUnbound = why([{ id: "a", player_id: null, output_id: null }]);

// Zoned clock times (TZ=America/Los_Angeles, en-US): every clock time names its zone.
const winter = Date.UTC(2027, 0, 12, 2, 0, 0) / 1000;  // 18:00 PST on 11 Jan
out.time = {
  clock: time.clockTime(winter),
  seconds: time.clockTime(winter + 5),
  factsClock: facts.clock(winter),
  window: time.windowLabel({ starts_at: winter, ends_at: winter + 7200 }),
  acrossDays: time.windowLabel({ starts_at: winter, ends_at: winter + 8 * 3600 }),
  zone: time.zoneName(),
  note: time.zoneNote(),
  occurrence: time.occurrenceTime(winter),
};

console.log(JSON.stringify(out));
"""


def test_the_planned_fact_origins_and_zoned_times():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--",
         (SRC / "facts.js").as_uri(), (SRC / "join.js").as_uri(), (SRC / "showState.js").as_uri(),
         (SRC / "timeWords.js").as_uri(), (SRC / "mediaHealth.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True,
        env={**os.environ, "TZ": "America/Los_Angeles", "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"})
    out = json.loads(result.stdout)
    rows = out["rows"]
    tail = "(Central's Runs; media not checked; the Panel is not observed)"

    assert rows["program"] == {
        "text": f"On top: xmas · Program dec-evenings {tail}",
        "kind": "planned", "sceneId": "xmas", "phase": "body"}
    assert rows["direct"]["text"] == f"On top: lobby · started directly, by Show now or the API {tail}"
    assert rows["direct"]["phase"] == "outro"
    # A child's origin is read from its ROOT Run (a child's program_id is null).
    assert rows["child"]["text"] == f"On top: intro · part of xmas's Run, Program dec-evenings {tail}"
    assert rows["unbound"]["text"] == (
        "On top: xmas · Program dec-evenings (Central's Runs; this Frame is unbound, so Central "
        "sends it no layers; the Panel is not observed)")
    assert rows["removed"]["text"] == f"On top: old · Program removed-one, since removed {tail}"
    nothing = ("On top: nothing · no Run puts a layer on this Frame now (Central's Runs; the Panel "
               "is not observed)")
    assert rows["nothing"] == {"text": nothing, "kind": "planned", "sceneId": None, "phase": None}
    # A Run that targets the Frame but puts nothing visible on it now.
    assert rows["participantNoIntent"]["text"] == nothing
    assert rows["missingRoot"] == {"text": "Unknown: who started orphan is not served", "kind": "unknown",
                                   "sceneId": "orphan", "phase": "body"}
    assert out["nullRuntime"] == nothing

    construction = out["construction"]
    assert construction["noOrigin"] == "Unknown: who started xmas is not served"
    assert construction["noBasis"] == "Unknown: what Central did not check for xmas is not named"
    assert construction["noValue"] == "Unknown: the top Run's Scene is not served"
    assert construction["sceneNamedNothing"] == f"On top: nothing · Program p {tail}"
    assert construction["noAge"] is None

    assert out["origins"] == {
        "r-prog": {"kind": "program", "words": "Program dec-evenings", "programListed": True},
        "r-child": {"kind": "child", "words": "part of xmas's Run", "programListed": False},
        "r-direct": {"kind": "direct", "words": "started directly (Show now or the API)",
                     "programListed": False},
        "r-gone": {"kind": "program", "words": "Program removed-one, since removed", "programListed": False},
    }
    assert out["orphanChild"] == "part of another Run"
    assert out["rowOrigins"]["r-prog"] == "Program dec-evenings"
    assert out["rowOrigins"]["r-direct"] == "started directly (Show now or the API)"
    assert out["childRowOrigin"] == "part of xmas's Run"

    assert out["heading"] == "Central's Runs on a: xmas (priority 5, Program dec-evenings) on top."
    assert out["directHeading"] == (
        "Central's Runs on b: lobby (priority 5, started directly, by Show now or the API) on top.")

    # A child on top: the heading names its root's Run, as the `planned` fact does (§35).
    assert out["childHeading"] == (
        "Central's Runs on c: intro (priority 5, part of xmas's Run, Program dec-evenings) on top.")
    # The Intended? step says the top Run as the `planned` fact, never Central's plan as the output.
    assert out["intended"] == {"title": "Intended?", "state": "ok",
                               "text": f"On top: xmas · Program dec-evenings {tail}; priority 5."}
    assert "this Frame is unbound" in out["intendedUnbound"]["text"]

    time = out["time"]
    assert time["clock"] == "18:00 PST"
    assert time["seconds"] == "18:00:05 PST"
    assert time["factsClock"] == "18:00 PST"
    assert time["window"] == "Mon, Jan 11 18:00–20:00 PST"
    assert time["acrossDays"] == "Mon, Jan 11 18:00 PST – Tue, Jan 12 02:00 PST"
    assert time["zone"] == "America/Los_Angeles"
    assert time["note"] == "Times in America/Los_Angeles (this browser's time zone)"
    assert time["occurrence"] == "6:00 PM PST (GMT-08:00)"


DISPLAY_FORMATTERS = re.compile(r"toLocaleTimeString|toLocaleString|toLocaleDateString|Intl\.DateTimeFormat")
RETIRED_WORDS = ("Scheduled:", "Intended scene", "Now showing", "Central's plan",
                 "Why each frame shows what it does", "meant to show", "No Scene is intended",
                 "No contributions target",
                 # Liveness names its layer, the Player app, in its one `reported` wording
                 # (health.js `livenessFact`): never "Player silent" or "last heard".
                 "Player silent", "last heard", "Last heard")


def _console_modules():
    return sorted(path for path in [*SRC.rglob("*.js"), *SRC.rglob("*.jsx")]
                  if "node_modules" not in path.parts)


def test_only_time_words_formats_an_instant_for_display():
    modules = _console_modules()
    assert SRC / "timeWords.js" in modules and len(modules) > 50
    offenders = sorted(str(path.relative_to(SRC)) for path in modules
                       if path.name != "timeWords.js" and DISPLAY_FORMATTERS.search(path.read_text()))
    assert offenders == [], f"{offenders} format clock times themselves; use timeWords.js"
    assert DISPLAY_FORMATTERS.search((SRC / "timeWords.js").read_text())


# Modules whose Date.now() compares only with another Date.now() of the same browser.
_OWN_CLOCK_ONLY = {"useSnapshot.js",  # the snapshot's arrival age: both ends are this browser's
                   "authoring.js"}    # a request id's entropy, never compared with anything


def test_the_browser_clock_is_never_compared_with_a_served_time():
    """R10: clocks compare only to themselves. A served absolute time (Central's, a node's)
    is aged only against a served read time on the same clock; a browser counts a served
    duration on its own monotonic clock (useCalibration.js `lease_seconds`). Date.now() is
    allowed only where both ends are the browser's own. Mutation probe: restore
    `expiresAt - Date.now() / 1000` in useCalibration.js and this fails."""
    offenders = sorted(str(path.relative_to(SRC)) for path in _console_modules()
                       if path.name not in _OWN_CLOCK_ONLY and "Date.now()" in path.read_text())
    assert offenders == [], f"{offenders} read the browser's clock; see R10"


def test_no_console_string_uses_the_retired_show_words():
    offenders = sorted(f"{path.relative_to(SRC)}: {word}" for path in _console_modules()
                       for word in RETIRED_WORDS if word in path.read_text())
    assert offenders == []
