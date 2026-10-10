"""Qualified fallback, its pure parts (console DDD Part E §25, §27-§28, bead NS2): qualification.js
(`qualificationView` over the G4 block with no usability verdict, `beginOffer`/`beginRequest`, the
ONE Begin send function `sendBegin`, `sampleAnswer`'s §27 classes with unlisted codes terminal, and
the sampler's stop rule `samplerStep`: accepted, terminal, or 2 minutes without progress), run under
Node as tests/test_console_stage.py runs stage.js. The browser half is in
tests/browser/test_player_page_browser.py.
"""

import json
import re
import subprocess
from pathlib import Path

from test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"
LINKED = "9f8e7d" + "0" * 58

SCRIPT = r"""
const q = await import(process.argv[1]);
const LINKED = process.argv[2];
const out = {};

const bound = { inventory: { players: [{ id: "p-1" }], outputs: [{ player_id: "p-1", output_id: "HDMI-A-1" }],
  frames: [{ id: "f-1", player_id: "p-1", output_id: "HDMI-A-1" }] } };
const unbound = { inventory: { players: [{ id: "p-1" }], outputs: [{ player_id: "p-1", output_id: "HDMI-A-1" }],
  frames: [] } };
const ops = (qualification) => ({ read_at: 1759363200, operations: [], qualification });
const served = ops({ linked_app: { environment_sha256: LINKED, admitted_at: 1759363080 },
  acceptances: [
    { environment_sha256: "aa".repeat(32), base_content_key: "k".repeat(64), base_tag: "v0.15.0", accepted_at: 1759190400 },
    { environment_sha256: "bb".repeat(32), base_content_key: "j".repeat(64), base_tag: null, accepted_at: 1759100000 }] });

// --- qualificationView: facts, no verdict.
const { factText } = await import(new URL("./facts.js", process.argv[1]).href);
const view = (o) => { const v = q.qualificationView(o); return { linked: factText(v.linkedApp), kind: v.linkedApp.kind,
  acceptances: v.acceptances.map((entry) => ({ kind: entry.kind, text: factText(entry) })) }; };
out.view = {
  served: view(served),
  unlinked: view(ops({ linked_app: null, acceptances: [] })),
  notServed: view(ops(undefined)),
  notRead: view(null),
};

// --- beginOffer / beginRequest.
out.offers = {
  ok: q.beginOffer(served, bound, "p-1", false),
  sampling: q.beginOffer(served, bound, "p-1", true),
  unbound: q.beginOffer(served, unbound, "p-1", false),
  unlinked: q.beginOffer(ops({ linked_app: null, acceptances: [] }), bound, "p-1", false),
  notServed: q.beginOffer(ops(undefined), bound, "p-1", false),
};
const request = q.beginRequest(served, bound, "p-1", "q-1");
out.request = request;
out.frozen = Object.isFrozen(request) && Object.isFrozen(request.body);
out.refusedRequest = q.beginRequest(served, unbound, "p-1", "q-1");

// --- sendBegin: judged on node.latest() at the moment of sending.
const posts = [];
let answer = () => new Response(JSON.stringify({ qualification_id: "q-1", status: "awaiting_representative_media" }),
  { status: 200 });
globalThis.fetch = async (url, init) => { posts.push({ url, body: init.body ? JSON.parse(init.body) : null }); return answer(); };
const send = async (latest, snapshot = bound, sampling = false) => {
  const before = posts.length;
  const result = await q.sendBegin("d-1", request, { node: { latest: () => ({ operations: latest }) }, snapshot,
    playerId: "p-1" }, sampling);
  return { ...result, posts: posts.length - before };
};
const relinked = ops({ linked_app: { environment_sha256: "cc".repeat(32), admitted_at: 1 }, acceptances: [] });
out.send = {
  ok: await send(served),
  sampling: await send(served, bound, true),
  unbound: await send(served, unbound),
  relinked: await send(relinked),
};
answer = () => new Response(JSON.stringify({ error: "node_environment_unknown" }), { status: 404 });
out.send.unknownEnvironment = await send(served);
answer = () => new Response(JSON.stringify({ error: "node_control_disabled" }), { status: 503 });
out.send.disabled = await send(served);
answer = () => { throw new TypeError("network"); };
out.send.lost = await send(served);
out.sentUrl = posts[0].url;
out.sentBodies = posts.map((post) => JSON.stringify(post.body));

// --- sampleAnswer: §27's classes.
const ok = (data) => ({ ok: true, status: 200, error: null, data });
const no = (status, error) => ({ ok: false, status, error, data: error ? { error } : null });
out.answers = {
  observing: q.sampleAnswer(ok({ status: "observing", sustained_seconds: 12.5, accepted: false })),
  awaiting: q.sampleAnswer(ok({ status: "awaiting_new_witnesses", accepted: false })),
  accepted: q.sampleAnswer(ok({ status: "accepted", acceptance_id: "a" })),
  oddStatus: q.sampleAnswer(ok({ status: "mystery" })),
  lost: q.sampleAnswer(null),
  gateway: q.sampleAnswer(no(502, null)),
  noCode4xx: q.sampleAnswer(no(400, null)),
};
for (const code of ["node_qualification_control_stale", "node_qualification_control_unlinked",
  "node_qualification_readiness_stale", "node_qualification_readiness_unavailable", "node_qualification_plan_changed",
  "node_qualification_handoff_unavailable", "node_qualification_preview_active", "node_qualification_base_unavailable",
  "node_output_cohort_unavailable", "node_representative_media_evidence_required",
  "node_representative_single_media_required", "node_representative_frame_witness_mismatch",
  "node_qualification_unknown", "node_qualification_generation_changed", "node_qualification_base_abi_mismatch",
  "node_qualification_process_changed", "node_player_unavailable", "node_device_unavailable",
  "node_control_disabled", "node_something_new"]) {
  out.answers[code] = q.sampleAnswer(no(409, code));
}

// --- samplerStep: the stop rule on the page's own monotonic clock.
const run = (answers, stepMs = 2000) => {
  let state = q.samplerStart("q-1", 0);
  let at = 0;
  const trail = [];
  for (const a of answers) {
    if (state.phase !== "sampling") break;
    at += typeof a.gap === "number" ? a.gap : stepMs;
    state = q.samplerStep(state, a, at);
    trail.push(state.phase);
  }
  return { phase: state.phase, stopped: state.stopped, samples: state.samples, quietMs: state.quietMs, trail };
};
const waiting = out.answers.awaiting;
const progress = (seconds) => ({ kind: "progress", seconds, text: `Observing representative media: ${seconds} s of 30 sustained` });
out.steps = {
  accepted: run([progress(0), progress(5), out.answers.accepted, waiting]),
  terminal: run([waiting, out.answers.node_qualification_process_changed, waiting]),
  unlisted: run([out.answers.node_something_new, waiting]),
  disabled: run([out.answers.node_control_disabled, waiting]),
  // 60 waiting answers 2 s apart: 120 s without progress, stopped on the 60th.
  quiet: run(Array(70).fill(waiting)),
  quiet59: run(Array(59).fill(waiting)),
  // Observing that never grows (a slideshow restarting the window) is no progress.
  flat: run(Array(70).fill(progress(0))),
  // Growth resets the quiet time.
  grows: run([...Array(50).fill(waiting), progress(0), progress(5), ...Array(50).fill(waiting)]),
  // A hidden tab's long pause counts at most 5 s.
  hidden: run([waiting, { ...waiting, gap: 600000 }, waiting]),
};
console.log(JSON.stringify(out));
"""


def _run():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--", (SRC / "qualification.js").as_uri(), LINKED],
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


STEADY = "Begin again when the Player app and its Outputs are steady."


def test_the_view_states_the_linked_app_and_acceptances_without_a_usability_verdict():
    view = _run()["view"]
    assert view["served"]["kind"] == "reported"
    assert view["served"]["linked"] == (
        "App Effect Broker reported the linked Player app 9f8e7d… · first received 2 min ago")
    assert view["served"]["acceptances"] == [
        {"kind": "set", "text": "Qualified on this Player: app aaaaaa… on base v0.15.0 · physical pixels unknown · "
                                "Stage checks it against the current Outputs and base when you send · recorded 2 d ago"},
        {"kind": "set", "text": "Qualified on this Player: app bbbbbb… on a base other than the one this boot runs · "
                                "physical pixels unknown · Stage checks it against the current Outputs and base when "
                                "you send · recorded 3 d ago"}]
    assert view["unlinked"]["linked"] == "Unknown: the Player app has not linked its current process on this boot"
    assert view["notServed"]["linked"] == "Unknown: Central does not serve the qualification read"
    assert view["notRead"]["linked"] == "Unknown: the app operations read has not answered"
    assert view["notRead"]["acceptances"] == []


def test_begin_is_offered_only_for_a_bound_player_with_a_linked_app_and_names_it():
    out = _run()
    assert out["offers"]["ok"] == {"offer": "begin", "environmentSha256": LINKED}
    assert out["offers"]["sampling"] == {"offer": "sampling"}
    assert out["offers"]["unbound"] == {"offer": "blocked", "reason": "qualification needs this Player bound to a Frame"}
    assert out["offers"]["unlinked"]["reason"] == "the Player app has not linked its current process on this boot"
    assert out["offers"]["notServed"]["reason"] == "Central does not serve the qualification read"
    assert out["request"] == {"body": {"qualification_id": "q-1", "environment_sha256": LINKED,
                                       "operator_audit_ref": "console/2025-10-02"}}
    assert out["frozen"] is True
    assert out["refusedRequest"] == {"refused": "qualification needs this Player bound to a Frame"}


def test_send_begin_judges_the_newest_read_and_refuses_without_a_post():
    out = _run()
    send = out["send"]
    assert send["ok"] == {"outcome": "done", "message": "Qualification begun.", "posts": 1, "code": None}
    assert send["sampling"] == {"outcome": "changed", "message": "This page is already sampling.", "posts": 0}
    assert send["unbound"]["posts"] == 0 and send["unbound"]["outcome"] == "changed"
    assert send["relinked"] == {"outcome": "changed", "posts": 0,
                                "message": "The Player app's linked process changed; begin again."}
    assert send["unknownEnvironment"] == {"outcome": "refused", "posts": 1, "code": "node_environment_unknown",
                                          "message": "Central does not know this app environment."}
    assert send["disabled"] == {"outcome": "refused", "posts": 1, "message": "Central refused: node_control_disabled.",
                                "code": "node_control_disabled"}
    assert send["lost"]["outcome"] == "unknown" and send["lost"]["posts"] == 1
    assert out["sentUrl"] == "/v1/operator/node/devices/d-1/app-qualifications"
    # Every Begin sent carries the same frozen body, so a resend after a lost answer is identical.
    assert len(out["sentBodies"]) == 4 and len(set(out["sentBodies"])) == 1


def test_sample_answers_are_classed_and_any_unlisted_code_is_terminal():
    answers = _run()["answers"]
    assert answers["observing"] == {"kind": "progress", "seconds": 12.5,
                                    "text": "Observing representative media: 12 s of 30 sustained"}
    assert answers["awaiting"] == {"kind": "waiting",
                                   "text": "Waiting for new reports from the Player app and Display Host"}
    assert answers["accepted"] == {"kind": "done", "text": "Qualified · physical pixels unknown"}
    assert answers["lost"] == answers["gateway"] == {"kind": "waiting",
                                                     "text": "Waiting: Central did not answer this sample"}
    waiting = [code for code, value in answers.items() if code.startswith("node_") and value["kind"] == "waiting"]
    assert len(waiting) == 12 and all(answers[code]["text"].startswith("Waiting: ") for code in waiting)
    assert answers["node_representative_single_media_required"]["text"] == (
        "Waiting: every bound Output must keep showing the same photo or video at full opacity for 30 s")
    terminal = [code for code, value in answers.items() if value["kind"] == "terminal"]
    assert set(terminal) == {"node_qualification_unknown", "node_qualification_generation_changed",
                             "node_qualification_base_abi_mismatch", "node_qualification_process_changed",
                             "node_player_unavailable", "node_device_unavailable", "node_control_disabled",
                             "node_something_new", "oddStatus", "noCode4xx"}
    assert answers["node_qualification_process_changed"]["text"] == (
        f"Stopped: the Player app's linked process changed or unlinked. {STEADY}")
    assert answers["node_control_disabled"]["text"] == f"Stopped: Central refused: node_control_disabled. {STEADY}"
    assert answers["node_something_new"]["text"] == f"Stopped: Central refused: node_something_new. {STEADY}"


def test_the_sampler_stops_on_accepted_terminal_and_two_minutes_without_progress():
    steps = _run()["steps"]
    assert steps["accepted"]["phase"] == "accepted" and steps["accepted"]["samples"] == 3
    for name in ("terminal", "unlisted", "disabled"):
        assert steps[name]["phase"] == "stopped", name
        assert steps[name]["trail"][-1] == "stopped" and steps[name]["stopped"].endswith(STEADY)
    assert steps["terminal"]["samples"] == 2 and steps["unlisted"]["samples"] == 1
    assert steps["quiet"]["phase"] == "stopped" and steps["quiet"]["samples"] == 60
    assert steps["quiet"]["stopped"] == ("Stopped: no progress for 2 minutes. Last answer: Waiting for new reports "
                                         "from the Player app and Display Host.")
    assert steps["quiet59"]["phase"] == "sampling" and steps["quiet59"]["samples"] == 59
    assert steps["flat"]["phase"] == "stopped" and steps["flat"]["samples"] == 60
    assert steps["grows"]["phase"] == "sampling" and steps["grows"]["samples"] == 102
    assert steps["hidden"]["phase"] == "sampling" and steps["hidden"]["quietMs"] == 9000


def _only_caller(pattern, module, function):
    route = re.compile(pattern)
    naming = {path.name: path.read_text() for path in [*SRC.rglob("*.js"), *SRC.rglob("*.jsx")]
              if route.search(path.read_text())}
    assert list(naming) == [module]
    source = naming[module]
    assert len(route.findall(source)) == 1
    start = source.index(function)
    assert start < route.search(source).start() < source.index("\n}\n", start)
    return source[start:route.search(source).start()]


def test_send_begin_is_the_only_caller_of_the_app_qualifications_route():
    before = _only_caller(r"/app-qualifications[`'\"]", "qualification.js", "export async function sendBegin(")
    assert "beginOffer(node.latest().operations, snapshot, playerId, sampling)" in before


def test_the_sampler_is_the_only_caller_of_the_sample_route_and_refuses_once_settled():
    before = _only_caller(r"/sample[`'\"]", "qualification.js", "export function useQualificationSampler(")
    assert 'if (qualificationId == null || state.phase !== "sampling") return state;' in before
