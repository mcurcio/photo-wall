"""The console's routes (flow design §6, bead 1b): R4 by import reachability, the route
tables' sample paths, and the hash routes' round trip.

R4: Display controls are reachable only from the Wall side. The shell mounts Show sections
always (hidden when not current) and Wall sections only while current, so R4 holds as long as
nothing the Show and neutral route tables import can reach the Commissioning facet or the
Inspector that hosts it. This walks the ES module imports from each table, transitively; the
browser half (tests/browser/test_console_shell_browser.py) visits every sample path.

The round trip runs routes.js itself under Node (a pure module, no React), which the console
build already requires.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).parents[1] / "central/console/src"
SAMPLES = json.loads((SRC / "routeSamples.json").read_text())
TABLES = {"show": "showRoutes.jsx", "wall": "wallRoutes.jsx", "neutral": "neutralRoutes.jsx"}
DISPLAY_CONTROLS = {"Commissioning.jsx", "Inspector.jsx"}

# `import … from "./x"`, `export … from "./x"` (the clause may span lines) and
# `import "./x"`, each starting a line; JSDoc lines start with "*" and never match.
_STATIC = re.compile(
    r"""^\s*(?:import|export)\b(?:[^;"']*?\bfrom)?\s*["'](\.{1,2}/[^"']+)["']""", re.M)
_DYNAMIC = re.compile(r"""\bimport\(\s*["'](\.{1,2}/[^"']+)["']\s*\)""")
_COMMENT_LINE = re.compile(r"^\s*(?:\*|//|/\*)")


def _imports(path):
    """The relative modules `path` imports, statically or dynamically, resolved to files."""
    text = path.read_text()
    specifiers = set(_STATIC.findall(text))
    for line in text.splitlines():
        if not _COMMENT_LINE.match(line):
            specifiers.update(_DYNAMIC.findall(line))
    resolved = set()
    for specifier in specifiers:
        base = (path.parent / specifier).resolve()
        candidates = [base, *(base.with_name(base.name + ext) for ext in (".js", ".jsx", ".json")),
                      base / "index.js", base / "index.jsx"]
        found = next((candidate for candidate in candidates if candidate.is_file()), None)
        assert found is not None, f"{path.name}: cannot resolve {specifier!r}"
        resolved.add(found)
    return resolved


def reachable(entry):
    """Every console module reachable from `entry` by imports, `entry` included."""
    seen, stack = set(), [SRC / entry]
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        if module.suffix in {".js", ".jsx"}:
            stack.extend(_imports(module) - seen)
    return {module.name for module in seen}


def test_the_import_scan_sees_static_multiline_and_dynamic_imports(tmp_path):
    (tmp_path / "a.jsx").write_text(
        'import React from "react";\nimport {\n  One,\n  Two,\n} from "./b.jsx";\n'
        'export { Three } from "./c.js";\nimport "./d.css";\n'
        ' * @param {import("./e.jsx").T} not an import\n'
        'const later = () => import("./f.jsx");\n')
    for name in ("b.jsx", "c.js", "d.css", "e.jsx", "f.jsx"):
        (tmp_path / name).write_text("")
    assert {p.name for p in _imports(tmp_path / "a.jsx")} == {
        "b.jsx", "c.js", "d.css", "f.jsx"}


@pytest.mark.parametrize("table", ["show", "neutral"])
def test_show_and_neutral_routes_never_reach_display_controls(table):
    modules = reachable(TABLES[table])
    assert not modules & DISPLAY_CONTROLS, sorted(modules & DISPLAY_CONTROLS)
    assert "health.js" in modules  # the walk reached past the table itself


def test_the_wall_routes_do_reach_display_controls():
    # Positive control: the scan finds Commissioning where it is meant to be.
    assert DISPLAY_CONTROLS <= reachable(TABLES["wall"])


@pytest.mark.parametrize("table", sorted(TABLES))
def test_each_route_table_takes_its_sections_and_samples_from_its_own_group(table):
    source = (SRC / TABLES[table]).read_text()
    declared = re.findall(r'^\s*section: "(\w+)",$', source, re.M)
    referenced = re.findall(r"\bSAMPLES\.(\w+)\.(\w+)\b", source)
    assert declared and sorted(declared) == sorted(SAMPLES[table])
    assert referenced == [(table, section) for section in declared]


def test_every_section_is_in_exactly_one_route_table():
    sections = [section for group in SAMPLES.values() for section in group]
    assert sorted(sections) == sorted(
        ["now", "scenes", "schedule", "sources", "wall", "equipment", "attention"])


ROUND_TRIP = r"""
const { parseRoute, formatRoute, sameRoute, landingRoute, SECTIONS } = await import(process.argv[1]);
const input = JSON.parse(process.argv[2]);
const out = {};
out.sections = SECTIONS;
out.samples = input.samples.map((path) => {
  const route = parseRoute(path);
  return { path, route, formatted: route === null ? null : formatRoute(route) };
});
out.roundTrips = input.routes.map((route) => {
  const hash = formatRoute(route);
  return { hash, same: sameRoute(parseRoute(hash), route) };
});
out.invalidHashes = input.invalidHashes.map((hash) => parseRoute(hash));
out.invalidRoutes = input.invalidRoutes.map((route) => {
  try { formatRoute(route); return "formatted"; } catch { return "refused"; }
});
out.landing = [landingRoute(0), landingRoute(3)];
console.log(JSON.stringify(out));
"""

ROUTES = [
    {"section": section} for section in
    ("now", "scenes", "schedule", "sources", "wall", "equipment", "attention")
] + [
    {"section": "now", "flow": "show", "step": "review"},
    {"section": "scenes", "flow": "new", "step": "kind"},
    {"section": "scenes", "flow": "new", "step": "edit"},
    {"section": "scenes", "id": "new", "flow": "edit", "step": "review"},
    {"section": "scenes", "id": "lobby/evening ç?#%", "flow": "edit", "step": "frames"},
    {"section": "sources", "flow": "new", "step": "name"},
    {"section": "schedule", "flow": "new", "step": "when"},
    {"section": "wall", "id": "reception north", "facet": "commissioning"},
    {"section": "wall", "id": "a/b", "facet": "binding"},
    {"section": "wall", "id": "frames", "facet": "nowshowing"},
]
INVALID_HASHES = [
    "", "#", "#/", "#/nope", "#now", "#/now/", "#//now", "#/wall/frames/x", "#/wall/frames/x/bogus",
    "#/wall/x/binding", "#/equipment/new/x", "#/now/new/x", "#/sources/x/edit/y",
    "#/scenes/new", "#/wall/frames/%E0%A4%A/binding",
]
INVALID_ROUTES = [
    {"section": "nope"}, {"section": "now", "facet": "binding", "id": "x"},
    {"section": "wall", "id": "x"}, {"section": "wall", "id": "x", "facet": "bogus"},
    {"section": "now", "flow": "new", "step": "x"}, {"section": "scenes", "flow": "edit",
                                                    "step": "x"},
    {"section": "scenes", "flow": "new", "step": ""}, {"section": "now", "extra": 1}, None,
]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node (the console build's) is absent")
def test_routes_parse_format_and_round_trip():
    samples = [path for group in SAMPLES.values() for paths in group.values() for path in paths]
    result = subprocess.run(
        ["node", "--input-type=module", "-e", ROUND_TRIP, "--", (SRC / "routes.js").as_uri(),
         json.dumps({"samples": samples, "routes": ROUTES, "invalidHashes": INVALID_HASHES,
                     "invalidRoutes": INVALID_ROUTES})],
        capture_output=True, text=True, timeout=30, check=True)
    out = json.loads(result.stdout)
    assert sorted(out["sections"]) == sorted(
        section for group in SAMPLES.values() for section in group)
    for group in SAMPLES.values():
        for section, paths in group.items():
            for path in paths:
                [sample] = [s for s in out["samples"] if s["path"] == path]
                # Each sample is a canonical route of its own section.
                assert sample["route"] is not None and sample["route"]["section"] == section, path
                assert sample["formatted"] == path
    assert all(trip["same"] for trip in out["roundTrips"]), out["roundTrips"]
    assert out["invalidHashes"] == [None] * len(INVALID_HASHES)
    assert out["invalidRoutes"] == ["refused"] * len(INVALID_ROUTES)
    assert out["landing"] == [{"section": "wall"}, {"section": "now"}]
