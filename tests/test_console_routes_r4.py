"""The console's routes (flow design §6, bead 1b): R4 by import reachability, the route
tables' sample paths, and the hash routes' round trip.

R4: Display controls are reachable only from the Wall side. The shell mounts Show sections
always (hidden when not current) and Wall sections only while current, so R4 holds as long as
nothing the Show, fleet and neutral route tables import can reach a module only the Wall needs
(the WALL-ONLY CLOSURE: everything `wallRoutes.jsx` reaches but the shell's own modules and the
modules declared shared with the other sides, `SHARED_WITH_SHOW`), no Show, fleet or neutral
module names the display's calibration route, and the shell's own modules reach the Wall's only
through `wallRoutes.jsx`. The browser half (tests/browser/test_console_shell_browser.py)
visits every sample path.

THE MODULE GRAPH is the bundler's: esbuild (shipped with Vite in the console's
node_modules) bundles the entry points with a metafile, whose `inputs` list every module and
what each one imports. It is cross-checked against an import scan of each module, and any
disagreement is an error, so neither can miss an import silently: a lexer tricked by JSX text
(`</p>`, an apostrophe, `http://`, `/*`) disagrees with the bundler, and a bundler resolving
something unexpected disagrees with the scan. Without Node or esbuild the tests skip on a
developer machine but FAIL where the checks must run (`CI` or `PHOTO_WALL_BROWSER_TESTS`).

The import scan itself FAILS CLOSED: it lexes each module (comments, strings, template and
regular expression literals), and every `import`/`export` it finds in code must be a form it
reads (`import … from "x"`, `import "x"`, `export … from "x"`, `import("x")`,
`` import(`x`) ``) naming a module it resolves to a file (relative, or root-relative "/src/…"
as Vite reads it) or a package; anything else (`import.meta.glob`, a computed `import()`) is
an error, not a skip. The self-tests below give both small synthetic sources.

The round trip runs routes.js itself under Node (a pure module, no React), which the console
build already requires.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_console_flow import _require_node

CONSOLE = Path(__file__).parents[1] / "central/console"
SRC = CONSOLE / "src"
ESBUILD = CONSOLE / "node_modules/.bin/esbuild"
SAMPLES = json.loads((SRC / "routeSamples.json").read_text())
TABLES = {"show": "showRoutes.jsx", "wall": "wallRoutes.jsx", "fleet": "fleetRoutes.jsx",
          "neutral": "neutralRoutes.jsx"}
DISPLAY_CONTROLS = {"CalibrationFacet.jsx", "Inspector.jsx", "useCalibration.js"}
# The display's calibration route (central/app.py `/v1/operator/frames/{frame_id}/calibration`):
# only the Wall side may name it. Matched with the interpolated Frame id's closing brace, so the
# Calibration facet's own hash (`#/wall/frames/<id>/calibration`, a route sample) is not it.
CALIBRATION_ROUTE = "}/calibration"
# Modules the Wall table reaches that the Show, fleet or neutral sides use too, besides the
# shell's own. The
# rest of the Wall's closure is Wall-only: sharing another module is a design decision, made
# here, and none of these may be a display control or name the calibration route.
SHARED_WITH_SHOW = {
    "ConfirmAction.jsx",  # every confirmation
    "NowShowingFacet.jsx",  # a frame's intent, also shown on Now showing
    "equipmentApi.js",  # UNKNOWN_MESSAGE and the equipment reads
    "FactLine.jsx",  # the one fact renderer: the Binding facet's Panel at enrollment (§19)
    "framesApi.js",
    "players.js",  # a Player page address (Wall links to the box's home); pure, no controls
    "projection.js",
    "routeSamples.json",  # every route table's sample paths
    "ReadinessNotice.jsx",  # shared read-only Player failure explanation
    "readinessRecovery.js",  # plain-language failure mapping; no controls
    "sceneTargets.js",  # pure stored Scene contribution and target reads
    "useMutate.js",  # refresh after a write
}


class ScanError(Exception):
    """An import the scan cannot read or resolve: R4 cannot be shown, so the test fails."""


# A `/` starts a regular expression literal, not a division, after one of these characters
# or keywords (and at the start of the source).
_REGEX_AFTER_CHARS = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_AFTER_WORDS = {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
                      "throw", "case", "do", "else", "yield", "await"}
_WORD = re.compile(r"[\w$]+$")


def _lex(text):
    """`text` with its comments blanked (offsets and newlines kept), and the (start, end)
    spans of its string, template and regular-expression literals.

    A quote left unterminated on its line is not a string (an apostrophe in JSX text), and a
    `/` whose literal would not close on its line is a plain character, as JavaScript
    allows neither literal to span lines.
    """
    code = list(text)
    spans = []
    n = len(text)

    def blank(start, end):
        for k in range(start, end):
            if code[k] != "\n":
                code[k] = " "

    def regex_may_start(i):
        before = "".join(code[:i]).rstrip()
        if not before:
            return True
        word = _WORD.search(before)
        if word is not None:
            return word.group() in _REGEX_AFTER_WORDS
        return before[-1] in _REGEX_AFTER_CHARS

    i = 0
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            blank(i, end)
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise ScanError(f"unterminated comment at offset {i}")
            blank(i, end + 2)
            i = end + 2
        elif c in "'\"":
            j = i + 1
            while j < n and text[j] not in (c, "\n"):
                j += 2 if text[j] == "\\" else 1
            if j < n and text[j] == c:
                spans.append((i, j + 1))
                i = j + 1
            else:
                i += 1
        elif c == "`":
            j, depth = i + 1, 0
            while j < n and not (text[j] == "`" and depth == 0):
                if text[j] == "\\":
                    j += 1
                elif text.startswith("${", j):
                    depth += 1
                    j += 1
                elif text[j] == "}" and depth > 0:
                    depth -= 1
                j += 1
            if j >= n:
                raise ScanError(f"unterminated template literal at offset {i}")
            spans.append((i, j + 1))
            i = j + 1
        elif c == "/" and regex_may_start(i):
            j, in_class = i + 1, False
            while j < n and text[j] != "\n" and (in_class or text[j] != "/"):
                if text[j] == "\\":
                    j += 1
                elif text[j] == "[":
                    in_class = True
                elif text[j] == "]":
                    in_class = False
                j += 1
            if j < n and text[j] == "/":
                spans.append((i, j + 1))
                i = j + 1
            else:
                i += 1
        else:
            i += 1
    return "".join(code), spans


_STR = r"""(?:"(?P<d>[^"\n]*)"|'(?P<s>[^'\n]*)'|`(?P<t>[^`$]*)`)"""
_IMPORT_FROM = re.compile(
    r"import\s*(?:[\w$]+\s*,?\s*)?(?:\*\s*as\s+[\w$]+\s*|\{[^}]*\}\s*)?from\s*" + _STR)
_IMPORT_BARE = re.compile(r"import\s*" + _STR)
_IMPORT_CALL = re.compile(r"import\s*\(\s*" + _STR + r"\s*\)")
_EXPORT_FROM = re.compile(r"export\s*(?:\*(?:\s*as\s+[\w$]+)?|\{[^}]*\})\s*from\s*" + _STR)
_EXPORT_LOCAL = re.compile(
    r"export\s+(?:default|const|let|var|function|class|async)\b|export\s*\{[^}]*\}(?!\s*from\b)")
_KEYWORD = re.compile(r"\b(import|export)\b")
_PACKAGE = re.compile(r"(?:@[\w.-]+/)?[\w.-]+(?:/[\w./-]+)?")


def _specifiers(text, name="<source>"):
    """Every module specifier `text` imports or re-exports, statically or dynamically.

    Raises ScanError for an `import`/`export` in code that is none of the forms read here.
    """
    code, spans = _lex(text)
    found = []
    for keyword in _KEYWORD.finditer(code):
        at = keyword.start()
        if any(start <= at < end for start, end in spans):
            continue  # a word inside a literal
        if code[:at].rstrip().endswith("."):
            continue  # a property, `x.import`
        line = code.count("\n", 0, at) + 1
        if keyword.group() == "import":
            if re.match(r"import\s*\.", code[at:]):
                raise ScanError(f"{name}:{line}: import.meta cannot be followed")
            if re.match(r"import\s*\(", code[at:]):
                match = _IMPORT_CALL.match(code, at)
            else:
                match = _IMPORT_FROM.match(code, at) or _IMPORT_BARE.match(code, at)
        else:
            match = _EXPORT_FROM.match(code, at)
            if match is None and _EXPORT_LOCAL.match(code, at):
                continue
        if match is None:
            raise ScanError(f"{name}:{line}: an {keyword.group()} the scan cannot read")
        found.append(next(group for group in match.group("d", "s", "t") if group is not None))
    return found


def _imports(path, root=CONSOLE):
    """The console modules `path` imports, resolved to files; packages are left out.

    Relative specifiers resolve against `path`, root-relative ones ("/src/…") against the
    Vite `root`, with Vite's extensions and index files. Anything unresolved is a ScanError.
    """
    resolved = set()
    for specifier in _specifiers(path.read_text(), path.name):
        if specifier.startswith(("./", "../")):
            base = (path.parent / specifier).resolve()
        elif specifier.startswith("/"):
            base = (root / specifier.lstrip("/")).resolve()
        elif _PACKAGE.fullmatch(specifier) and not specifier.startswith("."):
            continue  # a package (react, react-dom/client)
        else:
            raise ScanError(f"{path.name}: cannot resolve {specifier!r}")
        candidates = [base, *(base.with_name(base.name + ext) for ext in (".js", ".jsx", ".json")),
                      base / "index.js", base / "index.jsx"]
        found = next((candidate for candidate in candidates if candidate.is_file()), None)
        if found is None:
            raise ScanError(f"{path.name}: cannot resolve {specifier!r}")
        resolved.add(found)
    return resolved


def _require_esbuild():
    _require_node()
    if ESBUILD.is_file():
        return
    if os.environ.get("CI") or os.environ.get("PHOTO_WALL_BROWSER_TESTS"):
        pytest.fail("esbuild (the console build's) is absent where the console checks must run")
    pytest.skip("esbuild (the console build's) is absent: run `npm --prefix central/console ci`")


def bundler_graph(entries, root, workdir):
    """The bundler's module graph from `entries` (paths under `root`): {module: {imported
    modules}}, each a resolved Path; packages are left out. A module esbuild cannot resolve
    or parse fails the build, which is an error here."""
    result = subprocess.run(
        [str(ESBUILD), *(str(Path(entry).relative_to(root)) for entry in entries), "--bundle",
         f"--metafile={workdir / 'meta.json'}", f"--outdir={workdir / 'out'}", "--format=esm",
         "--packages=external", "--loader:.js=jsx", "--loader:.jsx=jsx", "--loader:.css=empty",
         "--loader:.woff2=empty", "--log-level=error"],
        cwd=root, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise ScanError(f"the bundler could not build the graph: {result.stderr.strip()}")
    inputs = json.loads((workdir / "meta.json").read_text())["inputs"]
    return {
        (root / name).resolve(): {(root / imported["path"]).resolve()
                                  for imported in entry["imports"] if not imported.get("external")}
        for name, entry in inputs.items()}


def module_graph(entries, root, workdir):
    """The bundler's graph (`bundler_graph`), cross-checked module by module against the
    import scan (`_imports`): any disagreement is a ScanError."""
    graph = bundler_graph(entries, root, workdir)
    for module, imports in graph.items():
        if module.suffix in {".js", ".jsx"}:
            scanned = _imports(module, root)
            if scanned != imports:
                raise ScanError(
                    f"{module.name}: the scan and the bundler disagree: only the scan sees "
                    f"{sorted(p.name for p in scanned - imports)}, only the bundler sees "
                    f"{sorted(p.name for p in imports - scanned)}")
    return graph


def reachable(graph, entry, *, stop=frozenset()):
    """The names of every module of `graph` reachable from `entry` (a name under SRC, or a
    Path), `entry` included. Modules named in `stop` are reached but not followed."""
    start = (entry if isinstance(entry, Path) else SRC / entry).resolve()
    seen, stack = set(), [start]
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        if module.name not in stop:
            stack.extend(graph.get(module, set()) - seen)
    return {module.name for module in seen}


@pytest.fixture(scope="module")
def graph(tmp_path_factory):
    """The console's module graph from its entry point and route tables."""
    _require_esbuild()
    entries = [SRC / "main.jsx", *(SRC / table for table in TABLES.values())]
    return module_graph(entries, CONSOLE, tmp_path_factory.mktemp("esbuild"))


# --- The scan's self-tests: small synthetic sources.


def _write(directory, files):
    for name, text in files.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def test_the_import_scan_sees_static_multiline_and_dynamic_imports(tmp_path):
    _write(tmp_path, {
        "a.jsx": 'import React from "react";\nimport {\n  One,\n  Two,\n} from "./b.jsx";\n'
                 'export { Three } from "./c.js";\nimport "./d.css";\n'
                 '/**\n * @param {import("./e.jsx").T} not an import\n */\n'
                 'const later = () => import("./f.jsx");\n'
                 'export const x = 1;\nexport { x as y };\n',
        "b.jsx": "", "c.js": "", "d.css": "", "e.jsx": "", "f.jsx": ""})
    assert {p.name for p in _imports(tmp_path / "a.jsx", tmp_path)} == {
        "b.jsx", "c.js", "d.css", "f.jsx"}


def test_the_import_scan_reads_a_multiline_import_with_quotes_in_its_comments(tmp_path):
    _write(tmp_path, {
        "a.jsx": '// don\'t "import" this; it\'s a comment\n'
                 'import {\n  One, // the "first" one\'s\n  /* it\'s "two" */ Two,\n'
                 '} from "./b.jsx";\n'
                 'const text = "import nothing"; const re = /["\']import/;\n'
                 "/* import('./gone.jsx') */\n"
                 '/*\nimport { Gone } from "./gone.jsx";\n*/\n'
                 "// import me later\n",
        "b.jsx": ""})
    assert {p.name for p in _imports(tmp_path / "a.jsx", tmp_path)} == {"b.jsx"}


def test_the_import_scan_resolves_root_relative_imports_from_the_vite_root(tmp_path):
    _write(tmp_path, {
        "src/deep/a.jsx": 'import { B } from "/src/b.jsx";\nimport "/src/c";\n',
        "src/b.jsx": "", "src/c.js": ""})
    assert {p.name for p in _imports(tmp_path / "src/deep/a.jsx", tmp_path)} == {
        "b.jsx", "c.js"}
    _write(tmp_path, {"src/deep/a.jsx": 'import { B } from "/src/missing.jsx";\n'})
    with pytest.raises(ScanError, match="cannot resolve"):
        _imports(tmp_path / "src/deep/a.jsx", tmp_path)


def test_the_import_scan_reads_plain_template_imports_and_refuses_computed_ones(tmp_path):
    _write(tmp_path, {"a.jsx": "const f = () => import(`./f.jsx`);\n", "f.jsx": ""})
    assert {p.name for p in _imports(tmp_path / "a.jsx", tmp_path)} == {"f.jsx"}
    _write(tmp_path, {"a.jsx": "const f = (name) => import(`./${name}.jsx`);\n"})
    with pytest.raises(ScanError, match="cannot read"):
        _imports(tmp_path / "a.jsx", tmp_path)
    _write(tmp_path, {"a.jsx": "const f = (name) => import(name);\n"})
    with pytest.raises(ScanError, match="cannot read"):
        _imports(tmp_path / "a.jsx", tmp_path)


def test_the_import_scan_refuses_import_meta_glob(tmp_path):
    _write(tmp_path, {"a.jsx": 'const pages = import.meta.glob("./*.jsx");\n', "b.jsx": ""})
    with pytest.raises(ScanError, match="import.meta"):
        _imports(tmp_path / "a.jsx", tmp_path)


# JSX text the lexer cannot tell from code, each before an import on its line (or, for
# `/*`, on the next): the scan misses the import, the bundler does not, and the cross-check
# says so.
JSX_TEXT = {
    "closing tag": 'export const A = () => <div><p>Hi</p>{import("./f.jsx") && 1 / 2}</div>;\n',
    "apostrophe": 'export const A = () => <p>It\'s {import("./f.jsx") && "x"} Ann\'s</p>;\n',
    "url": 'export const A = () => <p>See http://example.com {import("./f.jsx") && 1}</p>;\n',
    "comment opener": ('export const A = () => <p>Use /* for all</p>;\n'
                       'export const later = () => import("./f.jsx");\n'
                       'export const B = () => <p>and */ to end</p>;\n'),
}


@pytest.mark.parametrize("case", sorted(JSX_TEXT))
def test_jsx_text_that_hides_an_import_from_the_scan_fails_the_cross_check(tmp_path, case):
    _require_esbuild()
    _write(tmp_path, {"a.jsx": JSX_TEXT[case], "f.jsx": "export default 1;\n"})
    entry = tmp_path / "a.jsx"
    work = tmp_path / "work"
    work.mkdir()
    # The bundler sees the import ...
    assert (tmp_path / "f.jsx").resolve() in bundler_graph([entry], tmp_path, work)[entry.resolve()]
    # ... so the graph never reads f.jsx as unreachable: the scan's miss is an error.
    with pytest.raises(ScanError, match="disagree"):
        module_graph([entry], tmp_path, work)


def test_the_cross_check_agrees_where_the_scan_reads_every_import(tmp_path):
    _require_esbuild()
    _write(tmp_path, {
        "a.jsx": 'import { B } from "./b.jsx";\nexport const A = () => <p>{B}</p>;\n'
                 'export const later = () => import("./f.jsx");\n',
        "b.jsx": "export const B = 1;\n", "f.jsx": "export default 1;\n"})
    graph = module_graph([tmp_path / "a.jsx"], tmp_path, tmp_path)
    assert reachable(graph, tmp_path / "a.jsx") == {"a.jsx", "b.jsx", "f.jsx"}


def test_the_import_scan_reads_every_console_module():
    # Fail closed over the real sources too: every module lexes and resolves.
    for module in [*SRC.rglob("*.js"), *SRC.rglob("*.jsx")]:
        _imports(module)


# --- R4.


def _shell_own(graph):
    """The shell's own modules: those `main.jsx` reaches without entering a route table."""
    return reachable(graph, "main.jsx", stop=set(TABLES.values()))


def _wall_only(graph):
    """The Wall-only closure: what the Wall table reaches, but the shell's own modules and
    those declared shared with the Show side."""
    return reachable(graph, TABLES["wall"]) - _shell_own(graph) - SHARED_WITH_SHOW - {
        TABLES["wall"]}


@pytest.mark.parametrize("table", ["show", "fleet", "neutral"])
def test_show_fleet_and_neutral_routes_never_reach_the_wall_only_closure(graph, table):
    modules = reachable(graph, TABLES[table])
    assert "health.js" in modules  # the walk reached past the table itself
    assert not modules & _wall_only(graph), sorted(modules & _wall_only(graph))
    naming = sorted(module.name for module in graph
                    if module.name in modules and CALIBRATION_ROUTE in module.read_text())
    assert naming == [], f"{naming} name the display's calibration route"


def test_the_shell_reaches_the_wall_only_closure_only_through_the_wall_routes(graph):
    # From the entry point, with the Wall table reached but not followed: the shell's own
    # modules (App, Shell, the Wall's memory) import nothing only the Wall needs.
    modules = reachable(graph, "main.jsx", stop={TABLES["wall"]})
    assert {"Shell.jsx", TABLES["wall"]} <= modules
    assert not modules & _wall_only(graph), sorted(modules & _wall_only(graph))


def test_the_modules_shared_with_the_show_side_are_declared_and_control_nothing(graph):
    # The declared sharing is exactly what the Show and neutral sides reach of the Wall's
    # closure (a module no longer shared is taken off), and none of it is a display control.
    wall = reachable(graph, TABLES["wall"]) - _shell_own(graph)
    shown = (reachable(graph, TABLES["show"]) | reachable(graph, TABLES["fleet"])
             | reachable(graph, TABLES["neutral"]))
    assert wall & shown == SHARED_WITH_SHOW
    assert not SHARED_WITH_SHOW & DISPLAY_CONTROLS


@pytest.mark.parametrize("table", ["show", "fleet", "neutral"])
def test_readiness_guidance_is_shared_without_reaching_display_controls(graph, table):
    modules = reachable(graph, TABLES[table])
    assert {"ReadinessNotice.jsx", "readinessRecovery.js"} <= modules
    assert not modules & DISPLAY_CONTROLS


def test_the_wall_routes_do_reach_display_controls(graph):
    # Positive control: the closure holds the Calibration facet and the calibration write, where
    # they are meant to be.
    assert DISPLAY_CONTROLS <= _wall_only(graph)
    assert any(CALIBRATION_ROUTE in module.read_text() for module in graph
               if module.name in _wall_only(graph))


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
        ["now", "scenes", "schedule", "sources", "wall", "players", "attention"])


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
out.targetRoute = formatRoute({ section: "scenes", flow: "new", step: "kind",
                                initialTarget: "frame_one" });
out.equipment = parseRoute("#/equipment");
out.commissioning = parseRoute("#/wall/frames/x/commissioning");
out.commissioningRoute = (() => { try {
  return formatRoute({ section: "wall", id: "x", facet: "commissioning" });
} catch { return "refused"; } })();
out.badTargetRoute = (() => { try {
  return formatRoute({ section: "scenes", flow: "new", step: "kind", initialTarget: "old:frame" });
} catch { return "refused"; } })();
console.log(JSON.stringify(out));
"""

ROUTES = [
    {"section": section} for section in
    ("now", "scenes", "schedule", "sources", "wall", "players", "attention")
] + [
    {"section": "players", "id": "device-" + "a" * 64},
    {"section": "players", "id": "a/b ç?#%"},
    {"section": "now", "flow": "show", "step": "review"},
    {"section": "scenes", "flow": "new", "step": "kind"},
    {"section": "scenes", "flow": "new", "step": "kind", "initialTarget": "portrait-1"},
    {"section": "scenes", "flow": "new", "step": "edit"},
    {"section": "scenes", "id": "new", "flow": "edit", "step": "review"},
    {"section": "scenes", "id": "lobby/evening ç?#%", "flow": "edit", "step": "frames"},
    {"section": "sources", "flow": "new", "step": "name"},
    {"section": "sources", "id": "all-photos", "flow": "edit", "step": "review"},
    {"section": "schedule", "flow": "new", "step": "when"},
    {"section": "schedule", "id": "evening/program", "flow": "edit", "step": "review"},
    {"section": "wall", "id": "reception north", "facet": "calibration"},
    {"section": "wall", "id": "a/b", "facet": "binding"},
    {"section": "wall", "id": "frames", "facet": "nowshowing"},
]
INVALID_HASHES = [
    "", "#", "#/", "#/nope", "#now", "#/now/", "#//now", "#/wall/frames/x", "#/wall/frames/x/bogus",
    "#/wall/x/binding", "#/equipment/new/x", "#/now/new/x", "#/equipment/x",
    "#/equipment?target=x", "#/players/a/b", "#/players/x?target=y",
    "#/scenes/new", "#/wall/frames/%E0%A4%A/binding",
    "#/scenes/new/kind?target=bad%20id", "#/scenes/new/kind?target=x&target=y",
    "#/scenes/new/kind?other=x", "#/scenes/new/kind?target=legacy%3Aframe",
    "#/sources/new/name?target=frame",
]
INVALID_ROUTES = [
    {"section": "nope"}, {"section": "now", "facet": "binding", "id": "x"},
    {"section": "wall", "id": "x"}, {"section": "wall", "id": "x", "facet": "bogus"},
    {"section": "now", "flow": "new", "step": "x"}, {"section": "scenes", "flow": "edit",
                                                    "step": "x"},
    {"section": "scenes", "flow": "new", "step": ""}, {"section": "now", "extra": 1}, None,
    {"section": "equipment"}, {"section": "scenes", "id": "x"},
]


def test_routes_parse_format_and_round_trip():
    _require_node()
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
    # The retired Equipment page's bookmark lands on the Players list, and is never formatted.
    assert out["equipment"] == {"section": "players"}
    # The renamed facet's old bookmark opens Calibration, and is never formatted (§19).
    assert out["commissioning"] == {"section": "wall", "id": "x", "facet": "calibration"}
    assert out["commissioningRoute"] == "refused"
    assert out["targetRoute"] == "#/scenes/new/kind?target=frame_one"
    assert out["badTargetRoute"] == "refused"
