"""The console's routes (flow design §6, bead 1b): R4 by import reachability, the route
tables' sample paths, and the hash routes' round trip.

R4: Display controls are reachable only from the Wall side. The shell mounts Show sections
always (hidden when not current) and Wall sections only while current, so R4 holds as long as
nothing the Show and neutral route tables import can reach the Commissioning facet or the
Inspector that hosts it, and the shell's own modules reach them only through `wallRoutes.jsx`.
This walks the ES module imports from each table and from the shell, transitively; the browser
half (tests/browser/test_console_shell_browser.py) visits every sample path.

The import scan FAILS CLOSED: it lexes each module (comments, strings, template and regular
expression literals), and every `import`/`export` it finds in code must be a form it reads
(`import … from "x"`, `import "x"`, `export … from "x"`, `import("x")`, `` import(`x`) ``)
naming a module it resolves to a file (relative, or root-relative "/src/…" as Vite reads it)
or a package; anything else (`import.meta.glob`, a computed `import()`) is an error, not a
skip. The self-tests below give it small synthetic sources.

The round trip runs routes.js itself under Node (a pure module, no React), which the console
build already requires.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CONSOLE = Path(__file__).parents[1] / "central/console"
SRC = CONSOLE / "src"
SAMPLES = json.loads((SRC / "routeSamples.json").read_text())
TABLES = {"show": "showRoutes.jsx", "wall": "wallRoutes.jsx", "neutral": "neutralRoutes.jsx"}
DISPLAY_CONTROLS = {"Commissioning.jsx", "Inspector.jsx"}


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


def reachable(entry, *, root=CONSOLE, stop=frozenset()):
    """Every module reachable from `entry` (a path under SRC, or a Path) by imports, `entry`
    included. Modules named in `stop` are reached but not followed."""
    start = entry if isinstance(entry, Path) else SRC / entry
    seen, stack = set(), [start]
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        if module.suffix in {".js", ".jsx"} and module.name not in stop:
            stack.extend(_imports(module, root) - seen)
    return {module.name for module in seen}


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


def test_the_import_scan_reads_every_console_module():
    # Fail closed over the real sources too: every module lexes and resolves.
    for module in [*SRC.rglob("*.js"), *SRC.rglob("*.jsx")]:
        _imports(module)


# --- R4.


@pytest.mark.parametrize("table", ["show", "neutral"])
def test_show_and_neutral_routes_never_reach_display_controls(table):
    modules = reachable(TABLES[table])
    assert not modules & DISPLAY_CONTROLS, sorted(modules & DISPLAY_CONTROLS)
    assert "health.js" in modules  # the walk reached past the table itself


def test_the_shell_reaches_display_controls_only_through_the_wall_routes():
    # From the entry point, with the Wall table reached but not followed: the shell's own
    # modules (App, Shell, the Wall's memory) never import the Inspector or Commissioning.
    modules = reachable("main.jsx", stop={TABLES["wall"]})
    assert {"Shell.jsx", TABLES["wall"]} <= modules
    assert not modules & DISPLAY_CONTROLS, sorted(modules & DISPLAY_CONTROLS)


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
