"""Fleet › Releases, its pure parts: a version per Player (Q2 A, 2026-10-16). The words of
domain/releases/model.ts (each release with its readiness and the Players that boot it next, each
Player with its version, "newest ready" when Central keeps none for it, its previous, an install's
progress and what its host reported running), Update the wall's one send function
(releases.js `sendWallVersion`, judged on the newest read) and Install's request
(domain/releases/api.ts `installOnPlayer`, one POST for one Player), bundled by the console's own
esbuild and run under Node with `fetch` recorded. Source scans hold that no console code names
the boot selection's route, that Update the wall's PUT has one caller, and that Install is sent
only from the Releases page, one Player at a time. The browser half is
tests/browser/test_releases_browser.py.

Mutation probes: `playerRows` drops "newest ready" for a Player with no version of its own -> the
player case fails; `sendWallVersion` skips its `wallRefusal` -> the unlisted case sends a PUT;
Update the wall calls `installOnPlayer` -> the one-caller scan fails.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest
from test_console_flow import _require_node
from test_console_routes_r4 import ESBUILD, _require_esbuild

SRC = Path(__file__).parents[1] / "central/console/src"

ENTRY = """
export * as releases from "{releases}";
export * as model from "{model}";
export * as api from "{api}";
export * as outcome from "{outcome}";
export * as fleet from "{fleet}";
"""

SCRIPT = r"""
const { releases, model, api, outcome, fleet } = await import(process.argv[1]);
const out = {};
const APP = "9f8e7d".padEnd(64, "0");
const release = (version, extra = {}) => ({ version, stable: true, problem: null, base_tag: version,
  app_environment_sha256: APP, in_window: true, readiness: "ready", readiness_reason: null, missing_bytes: 0,
  discovered_at: 700, ...extra });
const player = (serial, name, extra = {}) => ({ serial, player_id: `p-${serial}`, name, version: "v2.0.0", chosen: true,
  previous: "v1.0.0", install: null, running: "v2.0.0", ...extra });
const view = (extra = {}) => ({ read_at: 1000, default: "v2.0.0",
  releases: [release("v3.0.0", { stable: false, readiness: "not_downloaded", missing_bytes: 1200000000 }),
    release("v2.0.0"), release("v1.5.0", { readiness: "downloading", missing_bytes: 2500000000 }),
    release("v1.2.0", { readiness: "failed", readiness_reason: "digest_mismatch" }),
    release("v1.1.0", { problem: "node_release_invalid", base_tag: null, app_environment_sha256: null, readiness: null }),
    release("v1.0.0", { app_environment_sha256: null, readiness: null })],
  players: [player("S1", "Hall"), player("S2", "Lobby", { version: null, chosen: false, previous: null, running: null }),
    player("S3", "Stairs", { version: "v1.5.0", install: "downloading", previous: "v2.0.0" })],
  ...extra });

out.releases = model.releaseRows(view()).map(({ release: _, ...row }) => row);
out.players = model.playerRows(view());
out.noneReady = model.playerRows(view({ default: null }))[1].version;
out.restarting = model.playerRows(view({ players: [player("S9", "Den", { install: "restarting" })] }))[0].install;
const allOn = view({ players: [player("S1", "Hall"), player("S2", "Lobby")] });
out.wallOn = [model.wallOn(allOn, "v2.0.0"), model.wallOn(view(), "v2.0.0"), model.wallOn(view({ players: [] }), "v2.0.0"),
  model.wallOn(view({ players: [player("S2", "Lobby", { chosen: false })] }), "v2.0.0")];
out.previous = [model.wallPrevious(allOn), model.wallPrevious(view())];
out.target = [model.updateTarget(view()), model.updateTarget(view({ default: null })), model.updateTarget(null)];
out.lines = ["v2.0.0", "v1.5.0", "v3.0.0", "v1.2.0", "v1.0.0"].map((version) =>
  model.readinessLine(view().releases.find((row) => row.version === version)));
out.sizes = [model.downloadSize(1200000000), model.downloadSize(5000)];
out.refusals = ["unknown_player", "unknown_version", "not_deployable", "unreachable", "http_422"].map(model.refusalWords);

let answer = () => new Response(JSON.stringify(view()), { status: 200 });
const sent = [];
globalThis.fetch = async (url, init) => {
  sent.push({ url, method: init.method, body: init.body === undefined ? null : JSON.parse(init.body) });
  return answer();
};
const hook = (r) => ({ latest: () => ({ read: r, readAt: r?.read_at ?? null, error: null, seq: 1 }) });
const wall = async (version, r) => {
  const before = sent.length;
  const result = await releases.sendWallVersion(version, hook(r));
  return { ...result, puts: sent.length - before };
};
out.wall = {
  unread: await wall("v2.0.0", null),
  unlisted: await wall("v9.9.9", view()),
  rejected: await wall("v1.1.0", view()),
  current: await wall("v2.0.0", view()),
};
answer = () => new Response(JSON.stringify({ error: "not_deployable" }), { status: 409 });
out.wall.refused = await wall("v2.0.0", view());
answer = () => new Response("bad gateway", { status: 502 });
out.wall.gateway = await wall("v2.0.0", view());
answer = () => { throw new TypeError("network"); };
out.wall.lost = await wall("v2.0.0", view());
out.wallBodies = sent.map((entry) => ({ url: entry.url, method: entry.method, body: entry.body }));
out.settled = [releases.wallSettled("v2.0.0", allOn).outcome, releases.wallSettled("v2.0.0", view()).outcome,
  releases.wallSettled("v2.0.0", null).outcome];

const installBefore = sent.length;
answer = () => new Response(JSON.stringify({ kind: "started", serial: "S1", version: "v2.0.0", install: "downloading" }),
  { status: 200 });
out.install = { started: await api.installOnPlayer("S1", { version: "v2.0.0" }) };
answer = () => new Response(JSON.stringify({ error: "unknown_player" }), { status: 404 });
out.install.refused = await api.installOnPlayer("S/2", { version: "v2.0.0" });
out.install.sent = sent.slice(installBefore);

// --- Check GitHub releases now: one POST while unanswered, never "a release arrived".
let checking = false;
const heldCheck = { get: () => checking, set: (value) => { checking = value; } };
const checkBefore = sent.length;
let finishCheck;
answer = () => new Promise((resolve) => { finishCheck = resolve; });
const firstCheck = releases.sendCatalogCheck(heldCheck);
out.check = { whileUnanswered: await releases.sendCatalogCheck(heldCheck), heldWhile: checking };
finishCheck(new Response(JSON.stringify({ status: "polling" }), { status: 202 }));
out.check.first = await firstCheck;
out.check.heldAfter = checking;
answer = () => { throw new TypeError("network"); };
out.check.lost = await releases.sendCatalogCheck(heldCheck);
answer = () => new Response(JSON.stringify({ error: "content_unavailable" }), { status: 503 });
out.check.refused = await releases.sendCatalogCheck(heldCheck);
out.check.posts = sent.slice(checkBefore).map((entry) => ({ url: entry.url, method: entry.method, body: entry.body }));

// --- One rule says when an answer leaves the outcome unknown.
const gateway = { ok: false, status: 502, error: null, data: null };
const coded = (error) => ({ ok: false, status: 503, error, data: null });
out.unknown = {
  none: outcome.answerUnknown(null), gateway: outcome.answerUnknown(gateway),
  ok: outcome.answerUnknown({ ok: true, status: 200 }), refused: outcome.answerUnknown({ ok: false, status: 409, error: "x" }),
  reboot: [fleet.rebootResult(coded("node_reboot_outstanding")).outcome, fleet.rebootResult(coded("other")).outcome,
           fleet.rebootResult(gateway).outcome],
  release: [outcome.answerOutcome(coded("release_unavailable"), () => "", {}).outcome,
            outcome.answerOutcome(gateway, () => "", {}).outcome, outcome.answerOutcome(null, () => "", {}).outcome],
  shared: [fleet.rebootResult(gateway).message === outcome.UNKNOWN_MESSAGE,
           outcome.answerOutcome(gateway, () => "", {}).message === outcome.UNKNOWN_MESSAGE],
};
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    _require_node()
    _require_esbuild()
    folder = tmp_path_factory.mktemp("releases")
    entry = folder / "entry.js"
    entry.write_text(ENTRY.format(releases=SRC / "releases.js", model=SRC / "domain/releases/model.ts",
                                  api=SRC / "domain/releases/api.ts", outcome=SRC / "sendOutcome.js",
                                  fleet=SRC / "fleetCommands.js"))
    bundle = folder / "entry.mjs"
    subprocess.run([str(ESBUILD), str(entry), "--bundle", "--format=esm", "--platform=node", f"--outfile={bundle}",
                    "--log-level=error"], check=True, timeout=60)
    result = subprocess.run(["node", "--input-type=module", "-e", SCRIPT, "--", bundle.as_uri()],
                            capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def test_each_release_lists_its_readiness_and_the_players_that_boot_it_next(out):
    rows = {row["version"]: row for row in out["releases"]}
    assert list(rows) == ["v3.0.0", "v2.0.0", "v1.5.0", "v1.2.0", "v1.1.0", "v1.0.0"]
    assert rows["v2.0.0"] == {"version": "v2.0.0", "readiness": "Ready", "contents": "Base v2.0.0 · app 9f8e7d…",
                              "prerelease": False, "deployable": True, "bootedBy": ["Hall", "Lobby"], "isDefault": True}
    assert rows["v3.0.0"]["readiness"] == "Not downloaded" and rows["v3.0.0"]["prerelease"] is True
    assert rows["v1.5.0"]["readiness"] == "Downloading · 2.5 GB left" and rows["v1.5.0"]["bootedBy"] == ["Stairs"]
    assert rows["v1.2.0"]["readiness"] == "Failed: digest mismatch"
    assert rows["v1.1.0"] == {"version": "v1.1.0", "readiness": "Rejected: node release invalid", "contents": None,
                              "prerelease": False, "deployable": False, "bootedBy": [], "isDefault": False}
    assert rows["v1.0.0"]["readiness"] is None and rows["v1.0.0"]["contents"] == "Base v1.0.0 · no app"


def test_each_player_says_its_version_newest_ready_when_none_is_chosen_its_install_and_what_it_runs(out):
    hall, lobby, stairs = out["players"]
    assert hall == {"serial": "S1", "name": "Hall", "playerId": "p-S1", "version": "v2.0.0", "previous": "v1.0.0",
                    "install": None, "running": "Base v2.0.0"}
    assert lobby["version"] == "v2.0.0 · newest ready"
    assert lobby["previous"] is None and lobby["running"] == "Not reported on this boot"
    assert stairs["version"] == "v1.5.0" and stairs["install"] == "Installing v1.5.0: Central is downloading it"
    assert out["restarting"] == "Installing v2.0.0: the Player is restarting onto it"
    assert out["noneReady"] == "No release is ready to boot"


def test_the_wall_is_on_a_release_only_when_every_player_chose_it(out):
    assert out["wallOn"] == [True, False, False, False]
    assert out["previous"] == ["v1.0.0", None]
    assert out["target"] == ["v2.0.0", "v2.0.0", None]


def test_the_readiness_line_says_what_a_booting_player_meets(out):
    assert out["lines"] == [
        "Ready: Central has every file of v2.0.0.",
        "Downloading: Central still has 2.5 GB of v1.5.0 to download; a Player that boots it before then waits for "
        "the download.",
        "Not downloaded: a Player that boots v3.0.0 waits while Central downloads it.",
        "Failed: Central could not download v1.2.0 (digest mismatch); a Player that boots it waits while Central "
        "tries again.",
        "Central did not serve whether v1.0.0 is downloaded.",
    ]
    assert out["sizes"] == ["1.2 GB", "less than 0.1 GB"]


def test_every_install_refusal_has_its_own_sentence_and_a_lost_answer_is_unknown(out):
    assert out["refusals"] == [
        "Central does not know this Player.",
        "Central's list of releases has no such release.",
        "Central cannot put this release on a Player: its upload was refused.",
        "Central did not answer: the outcome is unknown until the next read.",
        "Central refused it (http_422).",
    ]


def test_update_the_wall_judges_the_newest_read_and_sends_one_put_of_the_version(out):
    wall = out["wall"]
    for case in ("unread", "unlisted", "rejected"):
        assert wall[case]["outcome"] == "changed" and wall[case]["puts"] == 0, case
    assert wall["current"] == {"outcome": "done", "message": "Every Player boots v2.0.0 from its next boot on.",
                               "code": None, "puts": 1}
    assert wall["refused"] == {"outcome": "refused", "code": "not_deployable", "puts": 1,
                               "message": "Central cannot put this release on a Player: its upload was refused."}
    assert wall["gateway"]["outcome"] == wall["lost"]["outcome"] == "unknown"
    assert out["wallBodies"] == [{"url": "/v1/operator/releases/wall", "method": "PUT", "body": {"version": "v2.0.0"}}] * 4
    assert out["settled"] == ["done", "changed", "changed"]


def test_install_sends_one_post_for_one_player_and_answers_its_refusal_code(out):
    install = out["install"]
    assert install["started"] == {"ok": True, "started": {"kind": "started", "serial": "S1", "version": "v2.0.0",
                                                          "install": "downloading"}}
    assert install["refused"] == {"ok": False, "error": "unknown_player"}
    assert [(each["url"], each["method"], each["body"]) for each in install["sent"]] == [
        ("/v1/operator/players/S1/install", "POST", {"version": "v2.0.0"}),
        ("/v1/operator/players/S%2F2/install", "POST", {"version": "v2.0.0"}),
    ]


def _sources():
    return {module.relative_to(SRC).as_posix(): module.read_text()
            for suffix in ("*.js", "*.jsx", "*.ts", "*.tsx") for module in SRC.rglob(suffix)
            if "generated" not in module.parts}


def test_no_console_code_names_the_boot_selection_or_publishes_a_deployment():
    """Q2 A removed the stored wall-wide selection: no console source names its route or its
    shape, and none publishes a deployment or reads the old release route: Stage names the
    deployment the Releases read gives each release."""
    for name, source in _sources().items():
        assert "/boot-policy" not in source, name
        assert re.search(r"read\??\.selection\b|previous_deployment_id|sendSelection|[Bb]oot selection", source) is None, name
        assert re.search(r"/releases/[^\s`'\"]*/deployments|/deployments[`'\"]", source) is None, name
    assert [name for name, source in _sources().items() if "/v1/operator/node/releases" in source] == []


def test_update_the_wall_has_one_send_function_and_install_is_sent_only_from_the_releases_page():
    sources = _sources()
    assert [name for name, source in sources.items() if "/releases/wall" in source] == ["domain/releases/api.ts"]
    callers = {name: len(re.findall(r"\bsetWallVersion\(", source)) for name, source in sources.items()
               if name != "domain/releases/api.ts" and "setWallVersion(" in source}
    assert callers == {"releases.js": 1}
    source = sources["releases.js"]
    start = source.index("export async function sendWallVersion(")
    assert "wallRefusal(releases.latest().read, version)" in source[start:source.index("setWallVersion(", start)]
    installers = {name: len(re.findall(r"\binstallOnPlayer\(", source)) for name, source in sources.items()
                  if name != "domain/releases/api.ts" and "installOnPlayer(" in source}
    assert installers == {"ReleasesPage.jsx": 1}


def test_check_github_releases_now_sends_one_post_while_unanswered_and_never_claims_a_release(out):
    check = out["check"]
    assert check["whileUnanswered"]["outcome"] == "changed" and check["heldWhile"] is True
    assert check["first"] == {"outcome": "done", "message": (
        "Central queued a check of GitHub releases; new releases appear here when the media worker records them."),
        "code": None}
    assert check["heldAfter"] is False
    assert check["lost"]["outcome"] == "unknown"
    assert check["refused"] == {"outcome": "refused", "message": "Central refused: content_unavailable.",
                                "code": "content_unavailable"}
    assert check["posts"] == [{"url": "/v1/operator/app/releases/refresh", "method": "POST", "body": None}] * 3


def test_one_rule_says_when_an_answer_leaves_the_outcome_unknown(out):
    """sendOutcome.js `answerUnknown` is the one 5xx rule; each verb names only which served
    codes are its owner's own refusals: `node_`/`rollout_` for Reboot, any code for the release
    and node verbs (`answerOutcome`). Mutation probe: make `answerUnknown` ignore
    `centralRefusal` and the Reboot and release rows fail."""
    unknown = out["unknown"]
    assert (unknown["none"], unknown["gateway"], unknown["ok"], unknown["refused"]) == (True, True, False, False)
    assert unknown["reboot"] == ["changed", "unknown", "unknown"]
    assert unknown["release"] == ["refused", "unknown", "unknown"]
    assert unknown["shared"] == [True, True]
