"""The everyday actions in the console's words (decision 0036, slice 3): domain/everyday-words.ts
and the menu's order in domain/frame-status.ts, run under Node as they are (pure TypeScript, as
tests/test_console_interruption.py runs frame-status.ts). Central decides who moved, who stayed
and why; these say it: each answer as its toast says it, a refusal by its code's words and never
the code, a late action as its card's sentence, and only what Central's `can` holds in a Frame's
menu, in the menu's order. Without Node it skips on a developer machine, but FAILS where the
checks are meant to run in full (`CI` or `PHOTO_WALL_BROWSER_TESTS` set).
"""

import json
import subprocess
from pathlib import Path

from test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"

SCRIPT = r"""
const words = await import(process.argv[1]);
const status = await import(process.argv[2]);
const time = await import(process.argv[3]);
time.setHouseClock({ zone: "Europe/London", clock: "24h" });
const at = (hour, minute) => Date.UTC(2026, 10, 22, hour, minute) / 1000;
const names = { left: "Living room left", hall: "Hall", door: "By the door" };
const out = {};
out.skip = [
  words.skipSaid({ request_id: "r", kind: "next", at: 1, moved: [{ frame_id: "left" }, { frame_id: "door" }],
    stayed: [{ frame_id: "hall", reason: "hand-placed" }] }, names),
  words.skipSaid({ request_id: "r", kind: "previous", at: 1, moved: [{ frame_id: "hall" }] }, names),
  words.skipSaid({ request_id: "r", kind: "next", at: 1, moved: [{ frame_id: "unnamed" }] }, names),
];
out.nothingMoved = words.nothingMovedSaid([{ frame_id: "hall", reason: "not-answering" },
  { frame_id: "door", reason: "paused" }], names);
out.neverShow = [
  words.neverShowSaid({ asset_id: "a" }, names),
  words.neverShowSaid({ asset_id: "a", skipped_on: ["left"], last_showings: [{ frame_id: "hall", start: 1, until: 2 }],
    stayed: [{ frame_id: "door", reason: "hand-placed" }] }, names),
  words.neverShowSaid({ asset_id: "a", last_showings: [{ frame_id: "left", start: 1, until: 2 },
    { frame_id: "hall", start: 3, until: 4 }, { frame_id: "hall", start: 5, until: 6 }] }, names),
];
out.pause = [
  words.pauseSaid({ pause_id: "p", ends_at: at(22, 4), paused: [{ frame_id: "hall" }] }, names, at(21, 4)),
  words.pauseSaid({ pause_id: "p", ends_at: at(22, 4), paused: [{ frame_id: "hall" }, { frame_id: "left" }],
    stayed: [{ frame_id: "door", reason: "video" }] }, names, at(21, 4)),
];
out.nothingPaused = words.nothingPausedSaid([{ frame_id: "door", reason: "video" }], names);
out.notTaken = [
  words.notTakenSaid("changed", "Not hidden"),
  words.notTakenSaid("no-night-off"),
  words.notTakenSaid("unreachable"),
  words.notTakenSaid("lock_busy"),
  words.notTakenSaid("http_500"),
];
out.late = words.lateWords("skip", "Hall");
out.who = ["person:op_abcdefghijklmnop", "console", "scheduler", "Matt", "", null].map(status.whoWords);
out.listed = [
  words.listedWords({ by: "console", listed_at: at(21, 4) }, at(21, 10)),
  words.listedWords({ by: "Matt", listed_at: at(21, 4) }, at(21, 10)),
];
const can = (list) => ({ frame_id: "f", word: "showing", setup: null, since: 0, because: { row: "showing" }, can: list });
out.offered = [
  status.offeredActions(can(["reboot-pi", "never-show", "skip", "restart-app", "pause", "previous"])),
  status.offeredActions(can(["reboot-pi", "never-show"])),
  status.offeredActions(can([])),
  status.offeredActions(null),
];
out.pi = ["restart-app", "reboot-pi", "skip", "pause", "never-show"].map(status.isPiAction);
out.pauseLabel = status.ACTION_WORDS.pause;
out.override = status.overrideView({ kind: "pause", id: "p", frame_ids: ["hall"], by: "person:op_abcdefghijklmnop",
  until: at(22, 4) }, names, at(21, 10));
console.log(JSON.stringify(out));
"""


def _run():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--", (SRC / "domain/everyday-words.ts").as_uri(),
         (SRC / "domain/frame-status.ts").as_uri(), (SRC / "timeWords.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def test_a_skip_says_who_moved_and_each_frame_that_stayed_and_why():
    """Mutation probe: drop the stay clauses -> the first case fails."""
    out = _run()
    assert out["skip"] == [
        "Skipped on 2 Frames. Hall stayed: its photo is hand-placed.",
        "Stepped back on Hall.",
        "Skipped on unnamed.",
    ]
    assert out["nothingMoved"] == ("No Frame could move to another photo. Hall stayed: its Pi isn't answering. "
                                   "By the door stayed: it's paused.")


def test_never_show_says_where_it_shows_once_more_and_who_keeps_it():
    out = _run()
    assert out["neverShow"][0] == "Won't show again."
    assert out["neverShow"][1] == (
        "Won't show again. It shows once more on Hall, where it's already downloaded. "
        "It stays on By the door to the end of its turn: its photo is hand-placed.")
    # A Frame with several of its turns named is named once (found in a browser, 2026-11-25).
    assert out["neverShow"][2] == "Won't show again. It shows once more on Living room left, Hall, where it's already downloaded."


def test_a_pause_says_until_when_and_each_frame_not_paused():
    out = _run()
    assert out["pause"] == [
        "Hall paused until 22:04.",
        "2 Frames paused until 22:04. By the door wasn't paused: it's playing a clip, not a still photo.",
    ]
    assert out["nothingPaused"] == "No Frame could be paused. By the door wasn't paused: it's playing a clip, not a still photo."


def test_a_refusal_is_said_in_words_never_by_its_code():
    """Mutation probe: answer an unknown code with the code itself -> the last case fails."""
    out = _run()
    assert out["notTaken"] == [
        "Not hidden: A new photo came up before you pressed, so nothing was hidden.",
        "Not changed: There is no Night off tonight to end it at.",
        "Not changed: Central did not answer. Press it again.",
        "Not changed: Central was busy. Press it again.",
        "Not changed: Central did not take it. Read the page again, then press it again.",
    ]
    assert out["late"] == "Hall didn't change. Its Pi hasn't reported the new photo."


def test_who_did_it_is_never_an_audit_value():
    """central/actor.py's values are Central's to name; the console shows none of them as it is."""
    out = _run()
    assert out["who"] == [None, None, None, "Matt", None, None]
    assert out["listed"] == ["21:04", "by Matt · 21:04"]
    assert out["override"]["title"] == "Paused until 22:04"
    assert out["override"]["back"] == "Carry on"


def test_a_frames_menu_offers_only_what_central_can_in_its_order():
    """Mutation probe: list every action whatever `can` says -> the second case fails."""
    out = _run()
    assert out["offered"] == [
        ["skip", "previous", "pause", "never-show", "restart-app", "reboot-pi"],
        ["never-show", "reboot-pi"],
        [],
        [],
    ]
    assert out["pi"] == [True, True, False, False, False]
    assert out["pauseLabel"] == "Pause for 1 hour"
