"""The V2-only console (console DDD Part E §24-§28, bead NV1; R20).

The console assumes node control and shows no V1-lane surface. That posture is held by ONE
source-scan test over every console module, with ONE shared list (`V2_POSTURE`, below: the
bead's acceptance list, verbatim), and mutation probes proving the scan refuses what it should.
The pure parts of node control (nodeControl.js) and the deprecated-path line (bootFacts.js)
run under Node, as tests/test_console_players.py runs the fleet model.
"""

import json
import re
import subprocess
from pathlib import Path

from test_console_flow import _require_node

SRC = Path(__file__).parents[1] / "central/console/src"

# The V2 posture (Part E §28), the one list the scan and NV1's acceptance cite:
#   routes  no console module names a V1-lane route (the `/v1/operator/` prefix itself is allowed);
#   words   no string literal or JSX text carries one of these (comments are not scanned), with
#           exactly one named exemption;
#   reader  only this module reads `transport_enabled`.
V2_POSTURE = {
    "routes": ("/v1/operator/fleet", "maintenance-requests", "app-override", "app-policy",
               "base-baseline", "/v1/netboot/"),
    "words": ("V1", "netboot base", "release frontier", "maintenance request",
              "node management is off"),
    "exemptions": {"nodeControl.js": ("node management is off",)},
    "reader": ("transport_enabled", "nodeControl.js"),
}

# A `/` after one of these (or at the start) opens a regular expression literal, not a division.
_REGEX_AFTER = set("(,=:[!&|?{};+-*%<>~^") | {""}


def strip_comments(source):
    """`source` without its `//` and `/* */` comments: string, template and regular expression
    literals are kept whole, so a `//` inside one is not taken for a comment."""
    out, i, n, last = [], 0, len(source), ""
    while i < n:
        char, pair = source[i], source[i:i + 2]
        if pair == "//":
            while i < n and source[i] != "\n":
                i += 1
            continue
        if pair == "/*":
            end = source.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
            continue
        if char in "'\"`" or (char == "/" and last in _REGEX_AFTER):
            start, i, in_class = i, i + 1, False
            while i < n:
                if source[i] == "\\":
                    i += 2
                    continue
                if char == "/" and source[i] == "[":
                    in_class = True
                elif char == "/" and source[i] == "]":
                    in_class = False
                elif source[i] == char and not in_class:
                    break
                i += 1
            out.append(source[start:i + 1])
            i, last = i + 1, "x"
            continue
        out.append(char)
        if not char.isspace():
            last = char
        i += 1
    return "".join(out)


def _phrase(word):
    # "V1" is a whole token in its case (never `v1` in a path); the phrases match in any case.
    return re.compile(r"\bV1\b") if word == "V1" else re.compile(re.escape(word), re.IGNORECASE)


def posture_offenders(sources):
    """Every (module, what) breaking the V2 posture in `sources` ({module name: source})."""
    offenders = []
    reader, owner = V2_POSTURE["reader"]
    for name, source in sorted(sources.items()):
        code = strip_comments(source)
        offenders += [(name, route) for route in V2_POSTURE["routes"] if route in code]
        exempt = V2_POSTURE["exemptions"].get(name, ())
        offenders += [(name, word) for word in V2_POSTURE["words"]
                      if word not in exempt and _phrase(word).search(code)]
        if reader in code and name != owner:
            offenders.append((name, reader))
    return offenders


def _console_sources():
    return {str(path.relative_to(SRC)): path.read_text()
            for path in sorted([*SRC.rglob("*.js"), *SRC.rglob("*.jsx"), *SRC.rglob("*.ts"),
                                *SRC.rglob("*.tsx")]) if "node_modules" not in path.parts}


def test_the_console_holds_the_v2_posture():
    sources = _console_sources()
    assert len(sources) > 50 and "nodeControl.js" in sources
    assert posture_offenders(sources) == []
    for deleted in ("V1Offers.jsx", "ManagementFacts.jsx", "fleetApi.js"):
        assert not (SRC / deleted).exists()


def test_mutation_probe_a_re_added_v1_fleet_call_fails_the_scan():
    sources = _console_sources()
    sources["PlayerPage.jsx"] += '\nconst fleet = () => apiWrite("/v1/operator/fleet", { method: "GET" });\n'
    assert posture_offenders(sources) == [("PlayerPage.jsx", "/v1/operator/fleet")]


def test_mutation_probe_a_re_added_maintenance_request_string_fails_the_scan():
    sources = _console_sources()
    sources["pages/hardware-page.tsx"] += '\nconst NOTE = "Cancel maintenance request";\n'
    assert posture_offenders(sources) == [("pages/hardware-page.tsx", "maintenance request")]


def test_the_scan_reads_jsx_text_and_literals_but_not_comments():
    probe = {
        "a.jsx": "export const A = () => <h2>V1 boot offers</h2>;\n",
        "b.js": "// The V1 lane and its netboot base are gone (a comment, not a string).\n"
                "/* Release frontier, maintenance request: comments too. */\n"
                "const url = `/v1/operator/node/status`; const re = /\\/\\//;\n",
        "c.js": "const s = 'The Release Frontier moved';\n",
        "nodeControl.js": 'const t = "Node management is off on this Central."; '
                          "const on = data.transport_enabled;\n",
        "d.js": 'const t = "Node management is off"; const on = x.transport_enabled;\n',
    }
    assert posture_offenders(probe) == [
        ("a.jsx", "V1"), ("c.js", "release frontier"),
        ("d.js", "node management is off"), ("d.js", "transport_enabled")]


# --- The pure parts, under Node.

SCRIPT = r"""
const control = await import(process.argv[1]);
const boot = await import(process.argv[2]);
const { factText } = await import(process.argv[3]);
const out = {};
const status = (data, ok = true) => control.controlFromStatus({ ok, data });
const gate = { state: "closed", effective_state: "closed", reason: "never_certified", generation: 1 };
out.states = [
  status({ transport_enabled: true, effect_gate: gate }),
  status({ transport_enabled: false, effect_gate: gate }),
  status({ error: "rollout_gate_missing" }, false),
  control.controlFromStatus(null),
  status({}),
];
out.allowed = [
  { state: "on", failed: false }, { state: "off", failed: false },
  { state: "unread", failed: false }, { state: "unread", failed: true },
].map(control.nodeReadsAllowed);
out.gates = [
  null,
  gate,
  { ...gate, reason: "deployment_changed" },
  { ...gate, reason: "operator_paused" },
  { ...gate, state: "open", reason: null },
  { state: "open", effective_state: "open", generation: 4, reason: null },
].map((value) => factText(control.effectGateFact(value)));
out.recorded = factText(control.effectGateFact({ ...gate, changed_at: 1000 }));
out.deprecated = [
  boot.deprecatedBootFact({ path: "offer", recorded_at: 1700 }, 2000),
  boot.deprecatedBootFact({ path: "base_without_offer", recorded_at: 1990 }, 2000),
].map(factText).concat([boot.deprecatedBootFact(null, 2000)]);
console.log(JSON.stringify(out));
"""


def _run():
    _require_node()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT, "--", (SRC / "nodeControl.js").as_uri(),
         (SRC / "bootFacts.js").as_uri(), (SRC / "facts.js").as_uri()],
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def test_node_control_is_on_off_or_unread_and_never_inferred():
    out = _run()
    gate = {"state": "closed", "effective_state": "closed", "reason": "never_certified", "generation": 1}
    assert out["states"] == [
        {"state": "on", "gate": gate, "failed": False},
        {"state": "off", "gate": gate, "failed": False},
        # A failed read (a missing gate row answers 503) is no claim either way.
        {"state": "unread", "gate": None, "failed": True},
        {"state": "unread", "gate": None, "failed": True},
        {"state": "unread", "gate": None, "failed": True},
    ]
    # Node reads go out with node control on, or after the status read failed; never while it
    # is off, and not before its first answer.
    assert out["allowed"] == [True, False, False, True]


def test_the_effect_gate_has_one_wording_with_centrals_reason_and_no_age():
    out = _run()
    assert out["gates"] == [
        "Unknown: Central's effect gate is not readable",
        "Effect gate closed · Central's reason: no deployment certification has opened it",
        "Effect gate closed · Central's reason: the deployment changed",
        "Effect gate closed · Central's reason: operator paused",
        "Effect gate closed · Central's reason: its certification expired",
        "Effect gate open · certified by the deployment · generation 4 · Central re-checks its "
        "serving evidence on every Reboot and Stage",
    ]
    # Central's timestamp is displayed as a time, never subtracted from a browser clock.
    assert out["recorded"].startswith(
        "Effect gate closed · Central's reason: no deployment certification has opened it · recorded ")
    assert " ago" not in out["recorded"]


def test_the_deprecated_path_line_is_centrals_record_with_centrals_age():
    assert _run()["deprecated"] == [
        "Central's newest boot record for this box is a deprecated boot offer · recorded 5 min ago",
        "Central's newest boot record for this box is a base image served without an offer · recorded 10 s ago",
        None,
    ]
