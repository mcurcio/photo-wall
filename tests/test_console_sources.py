"""Pass 5 Sources (console DDD §37, §39, §40, bead L3): what a draft selects, the selection
summary, the tag rules and the tile words, run under Node as tests/test_console_flow.py runs
the flow kit, plus the one-noun source scan. Without Node the model test skips on a developer
machine, but FAILS where the checks are meant to run in full (`CI` or
`PHOTO_WALL_BROWSER_TESTS` set). The browser half is tests/browser/test_source_flow_browser.py.

  * `previewFacts`: a complete answer is the library's `reported` fact; a failure never reads
    as "nothing matches" (Unknown with no earlier answer; the earlier answer and "can't reach"
    with one); the connection is named only when more than one is announced.
  * `selectionWords`: the §39 summary, with a gone tag said as gone and a tag not looked up
    never said as gone; `sourceState`'s filters (mediaHealth.js) say the same pieces (C5).
  * `chooseTag`: a nested tag replaces its ancestor; an ancestor of a chosen tag is refused
    with its reason; at most four.
  * The source scan: no console string says "photo source", "Photo sources", "match preview",
    "album" or the library's vendor, and none says a library date is "taken".
"""

import json
import os
import re
import subprocess
from pathlib import Path

from central.infra.runtime import COMPLETION_NOT_RECORDED, WORKER_EXITED
from tests.test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"

SCRIPT = r"""
const preview = await import(process.argv[1]);
const facts = await import(process.argv[2]);
const model = await import(process.argv[3]);
const tags = await import(process.argv[4]);
const media = await import(process.argv[5]);
const out = {};
const said = (state, connections = ["home"]) => {
  const { facts: list, notes } = preview.previewFacts(state, connections);
  return [...list.map((fact) => facts.factText(fact)), ...notes];
};
const at = { observed_at: 100, read_at: 112 };
const answer = (extra) => ({ ...at, count: 0, image_count: 0, video_count: 0, shown: [], ...extra });
const good = answer({ count: 132, image_count: 128, video_count: 4 });

out.complete = said({ phase: "complete", answer: good, previous: null, connection: "home" });
out.nothing = said({ phase: "complete", answer: answer({}), previous: null });
out.limited = said({ phase: "complete", previous: null, answer: answer({ count: 1000, image_count: 1000,
  limited: true, shown: Array.from({ length: 24 }, (_, i) => ({ asset_id: `a${i}`, kind: "image", captured_at: 0 })) }) });
out.many = said({ phase: "complete", previous: null, answer: answer({ count: 30, image_count: 30,
  shown: Array.from({ length: 24 }, (_, i) => ({ asset_id: `a${i}`, kind: "image", captured_at: 0 })) }) });
out.unreachableNone = said({ phase: "unreachable", answer: null, previous: null });
out.unreachableEarlier = said({ phase: "unreachable", answer: null, previous: good });
out.looking = said({ phase: "looking", answer: null, previous: null });
out.updating = said({ phase: "looking", answer: null, previous: good });
out.stillLooking = said({ phase: "looking", answer: null, previous: null, stillLooking: true });
out.key = said({ phase: "key", answer: null, previous: null });
out.failed = said({ phase: "failed", answer: null, previous: null, code: "connection_unknown" });
out.named = said({ phase: "complete", answer: good, previous: null, connection: "home" }, ["home", "work"]);
out.noTime = said({ phase: "complete", answer: { count: 2, image_count: 0, video_count: 2, read_at: 1 }, previous: null });
out.kinds = [preview.failurePhase("upstream_unavailable"), preview.failurePhase("upstream_permission"),
             preview.failurePhase("source_limit"), preview.failurePhase("preview_expired"),
             preview.failurePhase("owner_mismatch")];
out.owner = said({ phase: "failed", answer: null, previous: null, code: "owner_mismatch" });
out.delays = [0, 1, 2, 3, 4, 9].map(preview.pollDelay);
const day = new Date(2024, 11, 12, 12).getTime() / 1000;
out.tiles = [preview.tileWords({ kind: "image", captured_at: day }),
             preview.tileWords({ kind: "video", captured_at: day, duration_seconds: 32 }),
             preview.tileWords({ kind: "video", captured_at: day, duration_seconds: 3723 }),
             preview.tileWords({ kind: "video", captured_at: day, duration_seconds: null })];
out.paths = [preview.thumbnailPath("asset-1"), preview.thumbnailPath("a/b", 1)];

const from = new Date(2025, 2, 3).getTime() / 1000;
const until = new Date(2025, 2, 6).getTime() / 1000;
out.selection = [
  model.selectionWords({ tags: ["t1"], favorites: true, media_types: ["image", "video"] }, { t1: "Family/Christmas" }),
  model.selectionWords({ media_types: ["image"], favorites: false, captured_from: from, captured_until: until }),
  model.selectionWords({ tags: ["t1", "t2"], media_types: ["video"] }, { t1: "Family", t2: null }),
  model.selectionWords({ tags: ["t3"] }, {}),
];
const year = [new Date(2024, 0, 1).getTime() / 1000, new Date(2025, 0, 1).getTime() / 1000];
const specs = [
  { tags: ["t1"], favorites: true, media_types: ["image"], captured_from: year[0], captured_until: year[1] },
  { tags: ["t1", "t2"], favorites: false, media_types: ["video"], captured_from: from },
  { media_types: ["image", "video"], captured_until: until },
  {},
];
out.agree = specs.map((spec) => ({ filters: media.sourceFilters(spec), selection: model.selectionWords(spec),
  named: media.sourceFilters(spec, { t1: "Family", t2: "Pets" }) }));
out.overLimit = media.sourceState({ status: "incompatible", next_refresh: 1000, last_success: null,
  diagnostics: [{ code: "source_limit" }], spec: {} }, 1000).label;
out.tagMissing = media.sourceState({ status: "incompatible", next_refresh: 1000, last_success: 900,
  diagnostics: [{ code: "tag_missing" }], spec: {} }, 1000).label;
const failing = (diagnostics) => media.sourceState({ status: "incompatible", next_refresh: 1000,
  last_success: 940, diagnostics, spec: {} }, 1000, false).label;
out.refusals = [failing([{ code: "spec_unsupported" }]), failing([{ code: "connection_mismatch" }]),
                failing([{ code: "upstream_unavailable" }]), failing([{ code: "brand_new_code" }]),
                failing([])];
out.tagMissingPreview = said({ phase: "failed", answer: null, previous: null, code: "tag_missing" });
out.state = media.sourceState({ status: "ok", next_refresh: 1000, last_success: 990, counts: { valid: 3 },
  spec: specs[0] }, 1000).label;
// The unannounced sentence: "isn't set up yet" only once the worker reported a list without it.
const saved = [{ spec: { connection_ref: "home" } }];
out.unannounced = [model.unannouncedWords(model.connectionRule(null, saved, "typed")),
                   model.unannouncedWords(model.connectionRule(["work"], saved, "home"))];

const family = { tag_ref: "f", path: "Family", name: "Family" };
const xmas = { tag_ref: "x", path: "Family/Christmas", name: "Christmas", parent_ref: "f" };
const pets = { tag_ref: "p", path: "Pets", name: "Pets" };
const known = { f: "Family", x: "Family/Christmas", p: "Pets" };
out.nested = tags.chooseTag(["f", "p"], xmas, known);
out.ancestor = tags.chooseTag(["x"], family, known);
out.again = tags.chooseTag(["x"], xmas, known);
out.max = tags.chooseTag(["a", "b", "c", "d"], pets, { ...known, a: "A", b: "B", c: "C", d: "D" });
out.fullReplace = tags.chooseTag(["a", "b", "c", "f"], xmas, { ...known, a: "A", b: "B", c: "C" });
const list = (extra) => ({ status: "ok", observed_at: 100, read_at: 200, total_matches: 2,
  tags: [family, pets], connection_ref: "home", ...extra });
out.learnWhole = tags.learnPaths({}, list({}));
out.learnIds = tags.learnPaths({}, list({ tags: [pets], absent: ["gone"] }));
out.learnUnnamed = tags.learnPaths({}, list({ tags: [{ tag_ref: "u", path: "", name: "" }] }));
out.unnamedWords = [media.sourceFilters({ tags: ["u"] }, out.learnUnnamed),
                    model.selectionWords({ tags: ["u"] }, out.learnUnnamed)];
out.said = [tags.pickerAnnouncement(list({}), null),
            tags.pickerAnnouncement(list({ total_matches: 0, tags: [] }), null),
            tags.pickerAnnouncement(list({ total_matches: 40 }), null),
            tags.pickerAnnouncement(list({ status: "pending", observed_at: null, total_matches: 0, tags: [] }), null),
            tags.pickerAnnouncement(list({ status: "unavailable", observed_at: null, error: "upstream_timeout",
                                           total_matches: 0, tags: [] }), null),
            tags.pickerAnnouncement(list({ total_matches: 0, tags: [] }), "unreachable")];
out.age = [tags.tagListFact(list({})), tags.tagListFact(list({ read_at: 100 + 7 * 60 })),
           tags.tagListFact(list({ status: "unavailable", observed_at: null, error: "upstream_timeout" }))]
  .map((fact) => (fact === null ? null : facts.factText(fact)));
console.log(JSON.stringify(out));
"""


def test_what_a_draft_selects_the_summary_and_the_tag_rules():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--",
         (SRC / "sourcePreview.js").as_uri(), (SRC / "facts.js").as_uri(),
         (SRC / "sourceFlowModel.js").as_uri(), (SRC / "libraryTags.js").as_uri(),
         (SRC / "mediaHealth.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True,
        env={**os.environ, "TZ": "Europe/London", "LC_ALL": "en_GB.UTF-8", "LANG": "en_GB.UTF-8"})
    out = json.loads(result.stdout)

    reported = "Your photo library reported 128 photos and 4 videos · first received 12 s ago"
    assert out["complete"] == [reported]
    assert out["nothing"] == ["Your photo library reported nothing matching yet · first received 12 s ago",
                              "New matches appear automatically once saved."]
    # Over the limit: the worker's current ceiling, never a product rule, then the sample.
    assert out["limited"] == [
        "Your photo library reported more than 1,000 matches · first received 12 s ago",
        "Photo Wall currently refuses a Source with more than 1,000 matches; saved like this it "
        "selects nothing. Narrow it with tags or dates.",
        "Showing the newest 24."]
    assert out["many"][-1] == "Showing the newest 24."
    # A failure is never "nothing matches".
    assert out["unreachableNone"] == ["Unknown: Photo Wall can't reach your photo library right now; retrying"]
    assert out["unreachableEarlier"] == [reported, "Photo Wall can't reach your photo library right now."]
    for failure in ("unreachableNone", "unreachableEarlier", "key", "failed", "looking"):
        assert not any("nothing matching" in line or "reported 0" in line for line in out[failure]), failure
    assert out["looking"] == ["Looking…"]
    assert out["updating"] == [reported, "Updating…"]
    assert out["stillLooking"] == ["Still looking. Photo Wall will keep trying."]
    assert out["key"] == [
        "Your library connection's key isn't allowed to list tags or show previews. Add the "
        "permissions in the setup guide's library key step."]
    assert out["failed"] == ["Unknown: the preview failed (connection unknown)"]
    assert out["named"] == [reported.replace("Your photo library", "Your photo library (connection home)")]
    assert out["noTime"] == ["Unknown: the time of your photo library's answer not served"]
    assert out["kinds"] == ["unreachable", "key", "failed", "unreachable", "failed"]
    # A key that is another user's is not fixed by more permissions: the card's own sentence.
    assert out["owner"] == ["Unknown: the preview failed (owner mismatch)",
                            "The library key belongs to a different user."]
    assert out["delays"] == [2000, 4000, 8000, 16000, 30000, 30000]
    assert out["tiles"] == ["Photo dated 12 Dec 2024", "Video, 0:32, dated 12 Dec 2024",
                            "Video, 1:02:03, dated 12 Dec 2024", "Video dated 12 Dec 2024"]
    assert out["paths"] == ["/v1/operator/library/thumbnails/asset-1",
                            "/v1/operator/library/thumbnails/a%2Fb?attempt=1"]

    assert out["selection"] == [
        "Selects media tagged Family/Christmas (and nested tags) · favourites only · photos and videos",
        "Selects everything on your library's timeline (not archived, hidden or other users' media)"
        " · no favourites · photos only · dated 3 Mar 2025 to 5 Mar 2025",
        "Selects media tagged with all of Family, a tag that no longer exists in your library "
        "(each with its nested tags) · videos only",
        "Selects media tagged a tag Photo Wall has not looked up yet (and nested tags) · photos and videos",
    ]

    # One home for a Source's selection words: the state label's filters are pieces of the
    # selection summary, in its order, "dated" and never "taken"; tags read as a count
    # until their paths are given.
    assert [entry["filters"] for entry in out["agree"]] == [
        ["1 tag", "favourites only", "photos only", "dated 2024"],
        ["2 tags", "no favourites", "videos only", "dated from 3 Mar 2025"],
        ["dated before 6 Mar 2025"],
        [],
    ]
    for entry in out["agree"]:
        parts = entry["selection"].split(" · ")
        untagged = [piece for piece in entry["filters"] if not piece.endswith(("tag", "tags"))]
        assert [part for part in parts if part in untagged] == untagged, entry
    assert out["agree"][1]["named"][0] == "tagged Family and Pets"
    # Over the worker's ceiling: Photo Wall's limit, never "your photo library is unsupported".
    # `source_limit` covers the worker's byte and request bounds too, so not the count alone.
    assert out["overLimit"] == ("Over Photo Wall's current size limits for one Source (at most 1,000 "
                                "matches) · narrow it with tags or dates · never refreshed successfully")
    # Photo Wall's own refusals never read as the library's fault (§39 R21); an unknown code
    # or none reads neutrally. Mutation probe: restore a fall-through keyed on `status`.
    good = " · last good refresh 1 min ago"
    assert out["refusals"] == [
        "This media worker can't read this Source's settings · update the media worker" + good,
        "The media worker's library connection doesn't match this Source · check the worker's "
        "connections" + good,
        "Your photo library is unreachable" + good,
        "Refresh failed (brand new code)" + good,
        "Refresh failed (incompatible)" + good,
    ]
    # A tag deleted in the library: its own cause, never "unsupported" and never "nothing matches".
    assert out["tagMissing"] == ("Your photo library no longer has a tag this Source uses · edit its tags"
                                 " · last good refresh 1 min ago")
    assert out["tagMissingPreview"] == ["Unknown: the preview failed (tag missing)",
                                        "A tag this Source uses no longer exists in your library."]
    assert out["state"].endswith(" · 1 tag · favourites only · photos only · dated 2024")

    assert out["unannounced"] == [
        "The media worker hasn't reported its library connections yet, so tags and previews "
        "aren't available yet. You can still save.",
        "This connection isn't set up yet, so tags and previews aren't available. You can still "
        "save; the Source starts selecting once the connection is set up."]

    # A nested tag replaces its ancestor; an ancestor of a chosen tag is refused, with why.
    assert out["nested"] == {"chosen": ["p", "x"], "refused": None, "replaced": ["f"]}
    assert out["ancestor"]["chosen"] == ["x"]
    assert out["ancestor"]["refused"] == (
        "Family is not added: Family/Christmas is already chosen and is nested under it, "
        "so adding Family would change nothing.")
    assert out["again"] == {"chosen": ["x"], "refused": None, "replaced": []}
    assert out["max"]["chosen"] == ["a", "b", "c", "d"]
    assert out["max"]["refused"] == "A Source takes at most 4 tags. Remove one to choose another."
    # Replacing frees a place, so a fifth nested tag fits.
    assert out["fullReplace"]["chosen"] == ["a", "b", "c", "x"]

    # No search proves a tag gone, even the unfiltered one: it hides unnamed tags (B5-FC1).
    assert out["learnWhole"] == {"f": "Family", "p": "Pets"}
    assert out["learnUnnamed"] == {"u": ""}
    assert out["unnamedWords"] == [["1 tag"], "Selects media tagged a tag with no visible name in "
                                   "your library (and nested tags) · photos and videos"]
    # The picker never announces "No tags match" for an unlisted library or a failed read.
    assert out["said"] == [
        "2 tags", "No tags match", "2 of 40 tags; keep typing",
        "Your photo library has not reported its tags yet.",
        "Your photo library has not reported its tags (upstream timeout).",
        "Photo Wall could not read your library's tags; it asks again as you type."]
    # A lookup by id names its tags and the ids Central says an ok list lacks (C5).
    assert out["learnIds"] == {"p": "Pets", "gone": None}
    assert out["age"] == [None, "Your photo library last reported 7 min ago",
                          "Unknown: your photo library has not reported its tags (upstream timeout)"]


# --- One noun (§37): the source scan.

RETIRED = re.compile(r"photo source|match preview|album|immich|· taken|taken (?:from|before|until|\$\{|\d)",
                     re.IGNORECASE)
COMMENT = re.compile(r"/\*.*?\*/|(?<![:\w])//[^\n]*", re.DOTALL)


def _console_strings_of(text):
    """Source text without its comments: its strings, JSX text and code."""
    return COMMENT.sub("", text)


# Every code a refresh can record (central/media_repository.py, media/worker.py and the
# adapter media/immich.py), harvested from the code that raises them.
_RAISED = re.compile(r'(?:MediaError\((?:"[a-z_]+" if \w+ else )?|Diagnostic\(code=|missing=|return |\bcode = )"([a-z_]+)"')


def _served_refusal_codes() -> set[str]:
    root = SRC.parents[2]
    codes = set()
    for path in ("media/immich.py", "media/worker.py", "central/media_repository.py"):
        codes |= set(_RAISED.findall((root / path).read_text()))
    return codes | {WORKER_EXITED, COMPLETION_NOT_RECORDED}


def test_every_served_refusal_code_has_one_owner():
    """One closed table names the owner of every refusal code (sourceWords.js SOURCE_REFUSALS):
    the library only for what it reported, and Photo Wall's own refusals never worded as the
    library's (§39 R21). A new code with no row fails here."""
    _require_node()
    script = r"""
const words = await import(process.argv[1]);
console.log(JSON.stringify({ table: words.SOURCE_REFUSALS, owners: [words.LIBRARY, words.PHOTO_WALL] }));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, "--", (SRC / "sourceWords.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True)
    out = json.loads(result.stdout)
    served = _served_refusal_codes()
    assert {"spec_unsupported", "connection_mismatch", "source_limit", "upstream_timeout",
            "worker_internal", "tag_missing"} <= served
    assert sorted(served - set(out["table"])) == []
    library, photo_wall = out["owners"]
    for code, entry in out["table"].items():
        assert entry["owner"] in (library, photo_wall), code
        if entry["owner"] == photo_wall:
            assert not entry["state"].startswith("Your photo library"), code
    for code in ("spec_unsupported", "connection_mismatch", "connection_unknown", "source_limit",
                 "owner_mismatch", "worker_timeout"):
        assert out["table"][code]["owner"] == photo_wall, code


def test_no_console_string_says_photo_source_album_or_the_librarys_vendor():
    modules = sorted(path for path in [*SRC.rglob("*.js"), *SRC.rglob("*.jsx")]
                     if "node_modules" not in path.parts)
    assert len(modules) > 50
    offenders = sorted(f"{path.relative_to(SRC)}: {match.group(0)}" for path in modules
                       for match in RETIRED.finditer(_console_strings_of(path.read_text())))
    assert offenders == [], f"{offenders}: the console's noun is Source, and it names no vendor"
    # The scan sees strings: a comment-free probe line is caught.
    assert RETIRED.search(_console_strings_of('const label = "Photo sources";'))
    assert RETIRED.search(_console_strings_of("const label = `${kind} · taken ${day}`;"))
    assert RETIRED.search(_console_strings_of('filters.push(`taken ${year}`);'))
