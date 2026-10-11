"""A Photo source in words and Check now's receipt (roadmap 4, K2; run ledger .claude/runs/photo-4.md),
run under Node as tests/test_console_flow.py runs the flow kit, plus the retired-words scan. Without
Node the model test skips on a developer machine, but FAILS where the checks are meant to run in
full (`CI` or `PHOTO_WALL_BROWSER_TESTS` set). The browser half is
tests/browser/test_source_flow_browser.py.

  * `sourceState` (mediaHealth.js) reads a Source's `check` alone: each check state's words, a
    failed check `notice` with "Photos keep playing" (C7), never an alarm.
  * `queryWords` (sourceWords.js): what a query includes, named when the names are known.
  * `sourceCheckResult` / `sourceCheckMessage` (useSourceCheck.js): the receipt lasts until the
    served check differs from the one it was asked from, never by the browser's clock.
  * The scan: no console string says a retired preview word.
"""

import json
import re
import subprocess
from pathlib import Path

from test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"

SCRIPT = r"""
const media = await import(process.argv[1]);
const words = await import(process.argv[2]);
const out = {};
const NOW = 1791147840;
const state = (check) => media.sourceState({ source_ref: "anna:1", check }, NOW);
out.states = [
  state({ state: "never_checked" }),
  state({ state: "checking", seen_so_far: 12400, started_at: NOW - 60, last_complete_at: null }),
  state({ state: "complete", at: NOW - 60 }),
  state({ state: "incomplete", reason: "over_1000_at_one_instant", last_complete_at: null }),
  state({ state: "failed", problem: "connection", last_complete_at: NOW - 3600 }),
  state({ state: "failed", problem: "chosen_item_gone", last_complete_at: null }),
  state(undefined),
].map(({ state, severity, label }) => ({ state, severity, label: label.replace(/\d\d:\d\d( [A-Z+0-9:-]+)?/, "HH:MM") }));
const query = {
  connection_ref: "home", albums: [], people: ["p-anna", "p-ben"], place: null,
  taken: { kind: "last", years: 3, months: 0 }, favourites_only: false, tags: [],
  media_types: ["live_photo", "photo", "video"], partners: false,
  exclude: { albums: [], people: [], tags: [], screenshots: true },
};
out.named = words.queryWords(query, { "p-anna": "Anna", "p-ben": "Ben" });
out.counted = words.queryWords(query);
out.every = words.queryWords({ ...query, people: [], taken: null, exclude: { ...query.exclude, screenshots: false } });
out.photosOnly = words.queryWords({ ...query, people: [], taken: { kind: "last", years: 0, months: 1 },
  media_types: ["photo"], favourites_only: true });
out.preFour = words.queryWords(null);
console.log(JSON.stringify(out));
"""

CHECK_SCRIPT = r"""
const check = await import(process.argv[1]);
const before = { state: "complete", at: 100 };
const requested = check.sourceCheckResult({ ok: true, status: 202, error: null }, before);
const out = {
  kinds: [
    requested.kind,
    check.sourceCheckResult({ ok: false, status: 0, error: null }, before).kind,
    check.sourceCheckResult({ ok: false, status: 503, error: null }, before).kind,
    check.sourceCheckResult({ ok: false, status: 404, error: "source_unknown" }, before).message,
    check.sourceCheckResult(null, before).kind,
  ],
  same: check.sourceCheckMessage(requested, { state: "complete", at: 100 }),
  started: check.sourceCheckMessage(requested, { state: "checking", seen_so_far: 0, started_at: 120,
                                                 last_complete_at: 100 }),
  later: check.sourceCheckMessage(requested, { state: "complete", at: 140 }),
  none: check.sourceCheckMessage(undefined, before),
  fromNothing: check.sourceCheckMessage(check.sourceCheckResult({ ok: true, status: 202 }, null), null),
};
console.log(JSON.stringify(out));
"""


def _run(script, *modules):
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, "--", *[(SRC / m).as_uri() for m in modules]],
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def test_a_sources_state_is_its_checks_and_a_failure_keeps_photos_playing():
    out = _run(SCRIPT, "mediaHealth.js", "sourceWords.js")
    assert out["states"] == [
        {"state": "never-checked", "severity": "todo", "label": "Not checked yet"},
        {"state": "checking", "severity": "notice", "label": "Checking: 12,400 photos so far"},
        {"state": "complete", "severity": "ok", "label": "Checked HH:MM"},
        {"state": "incomplete", "severity": "notice",
         "label": "Checked part of it: more than 1,000 photos share one moment"},
        {"state": "failed", "severity": "notice",
         "label": "Immich could not be read: see Photo library. Photos keep playing."},
        {"state": "failed", "severity": "notice",
         "label": "An album, person or tag in this Source was deleted in Immich. Photos keep playing."},
        {"state": "unknown", "severity": "unknown", "label": "Its check was not served"},
    ]
    assert out["named"] == "Anna and Ben · last 3 years · no screenshots"
    assert out["counted"] == "2 people · last 3 years · no screenshots"
    assert out["every"] == "Every photo"
    assert out["photosOnly"] == "last month · favourites only · photos only · no screenshots"
    assert out["preFour"] == "Made before Photo Wall read Immich directly"


def test_check_nows_receipt_lasts_until_the_served_check_differs():
    out = _run(CHECK_SCRIPT, "useSourceCheck.js")
    assert out["kinds"] == ["requested", "unknown", "unknown", "Check request failed: source unknown.",
                            "unknown"]
    requested = "Check requested. Photos keep playing while Photo Wall reads Immich."
    assert out["same"] == requested           # the same check: the receipt stays
    assert out["started"] is None             # a new check started: the state's words take over
    assert out["later"] is None
    assert out["none"] is None
    assert out["fromNothing"] == requested


# --- Retired words: the source scan. Roadmap 4 names Immich, its albums and the Photo source
# (console design #8-#33, design-language row A3), so only the retired preview words remain.

RETIRED = re.compile(r"match preview|· taken|taken (?:from|before|until|\$\{|\d)", re.IGNORECASE)
COMMENT = re.compile(r"/\*.*?\*/|(?<![:\w])//[^\n]*", re.DOTALL)


def _console_strings_of(text):
    """Source text without its comments: its strings, JSX text and code."""
    return COMMENT.sub("", text)


def test_no_console_string_says_a_retired_preview_word():
    modules = sorted(path for path in [*SRC.rglob("*.js"), *SRC.rglob("*.jsx"), *SRC.rglob("*.tsx")]
                     if "node_modules" not in path.parts)
    assert len(modules) > 50
    offenders = sorted(f"{path.relative_to(SRC)}: {match.group(0)}" for path in modules
                       for match in RETIRED.finditer(_console_strings_of(path.read_text())))
    assert offenders == [], f"{offenders}: retired preview words"
    # The scan sees strings: a comment-free probe line is caught.
    assert RETIRED.search(_console_strings_of("const label = `${kind} · taken ${day}`;"))
    assert RETIRED.search(_console_strings_of('filters.push(`taken ${year}`);'))


# --- The retiring Source fields (roadmap 4, K4; moved here from the integrate slice): a Source's
# state is its check, so no console module reads what the refresh served for it.

RETIRED_SOURCE_FIELDS = re.compile(
    r"next_refresh|last_success|refresh_requested_revision|refresh_completed_revision|connection_ids"
    r"|source-previews|sources/[^\"'`]*/refresh\b|sourceRefreshPath"
    r"|\bsource\w*\??\.(?:spec|status|diagnostics|counts|limited)\b"
    r"|\bspec\??\.(?:connection_ref|tags|media_types|favorites|captured_from|captured_until)\b")


def test_no_console_module_reads_a_retired_source_field():
    modules = sorted(path for path in [*SRC.rglob("*.js"), *SRC.rglob("*.jsx"), *SRC.rglob("*.ts"),
                                       *SRC.rglob("*.tsx")] if "node_modules" not in path.parts)
    offenders = sorted(f"{path.relative_to(SRC)}: {match.group(0)}" for path in modules
                       for match in RETIRED_SOURCE_FIELDS.finditer(_console_strings_of(path.read_text())))
    assert offenders == [], offenders
    # The scan sees each kind of read; the release list's own refresh route is not a Source's.
    for probe in ("source.next_refresh", "row.refresh_completed_revision", "source.status",
                  "source?.spec", "spec.tags", "`/v1/operator/sources/${ref}/refresh`",
                  '"/v1/operator/source-previews"'):
        assert RETIRED_SOURCE_FIELDS.search(probe), probe
    assert not RETIRED_SOURCE_FIELDS.search('apiWrite("/v1/operator/app/releases/refresh")')
