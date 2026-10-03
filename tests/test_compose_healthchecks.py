"""Compose healthchecks run first-party code only through a module entry point that resolves.

An inline `python -c` snippet that imports first-party code is invisible to lint, type checks
and tests, so a signature change breaks it only at `docker compose up --wait` (the media
worker's `MediaRepository(...)` snippet did exactly that). A stdlib-only probe may stay inline.

PyYAML is not a dependency, so `_healthcheck_tests` is a small structural scan of the YAML: it
finds each `test:` under a `healthcheck:` mapping in flow-list, block-list or string (CMD-SHELL)
form, and the probe is analysed as an argv, following `sh -c` / CMD-SHELL strings into the shell
words. CI workflow YAML is not scanned: .github/workflows/base-image.yml runs first-party
`python3 -c` snippets today (errata HC-3).
"""

import importlib.util
import re
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = (ROOT / "compose.yaml", *sorted((ROOT / "tests/integration").glob("*.yml")))
FIRST_PARTY = r"(central|media|contracts|player|appliance|uplink|scripts)"
FIRST_PARTY_IMPORT = re.compile(rf"\b(from\s+{FIRST_PARTY}\b|import\s+{FIRST_PARTY}\b)")
PYTHON = re.compile(r"^python(\d+(\.\d+)?)?$")
SHELLS = {"sh", "bash", "dash", "ash"}
FLOW_ITEM = re.compile(r"""\s*("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^,]*?)\s*(?:,|$)""")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _scalar(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1].encode().decode("unicode_escape")
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    return text


def _flow_list(text: str) -> list[str]:
    inner = text.strip()[1:-1]
    items, position = [], 0
    while position < len(inner):
        match = FLOW_ITEM.match(inner, position)
        if match.end() == position:
            break
        if match[1].strip():
            items.append(_scalar(match[1]))
        position = match.end()
    return items


def _test_value(lines: list[str], index: int, key_indent: int, inline: str) -> list[str] | str:
    """A `test:` value as an argv (list forms) or a shell string (string form)."""
    inline = inline.split(" #")[0].strip() if not inline.strip().startswith(("'", '"')) else inline.strip()
    if inline.startswith("["):
        return _flow_list(inline)
    if inline and inline[0] not in "|>":
        return _scalar(inline)
    body = []
    for line in lines[index + 1:]:
        continues = (not line.strip() or _indent(line) > key_indent
                     or (_indent(line) == key_indent and line.lstrip().startswith("- ")))
        if not continues:
            break
        body.append(line)
    if inline:  # block scalar: a shell string
        return " ".join(line.strip() for line in body if line.strip())
    return [_scalar(line.strip()[2:]) for line in body if line.strip().startswith("- ")]


def _healthcheck_tests(text: str) -> list[tuple[int, list[str] | str]]:
    found, lines, healthcheck_indent = [], text.splitlines(), None
    for index, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if healthcheck_indent is not None and _indent(line) <= healthcheck_indent:
            healthcheck_indent = None
        match = re.match(r"(\s*)healthcheck:\s*(.*)$", line)
        if match:
            rest = match[2].strip()
            if rest.startswith("{"):  # flow mapping, e.g. `{disable: false}` or `{test: [...]}`
                test = re.search(r"\btest:\s*(\[[^\]]*\]|\"[^\"]*\"|'[^']*')", rest)
                if test:
                    found.append((index + 1, _test_value(lines, index, len(match[1]), test[1])))
            else:
                healthcheck_indent = len(match[1])
            continue
        match = re.match(r"(\s*)test:\s*(.*)$", line)
        if healthcheck_indent is not None and match:
            found.append((index + 1, _test_value(lines, index, len(match[1]), match[2])))
    return found


def _probe_violations(value: list[str] | str) -> list[str]:
    """Inline first-party `-c` snippets and unresolvable `-m` modules in one probe."""
    if isinstance(value, str):
        argv = ["CMD-SHELL", value]
    else:
        argv = list(value)
    if argv and argv[0] == "CMD-SHELL":
        return _argv_violations(_shell_words(" ".join(argv[1:])))
    if argv and argv[0] in ("CMD", "NONE"):
        argv = argv[1:]
    return _argv_violations(argv)


def _shell_words(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _argv_violations(argv: list[str]) -> list[str]:
    violations = []
    for position, word in enumerate(argv):
        name = word.rsplit("/", 1)[-1]
        following = argv[position + 1:position + 3]
        if len(following) < 2:
            continue
        flag, payload = following
        if PYTHON.match(name) and flag == "-c" and FIRST_PARTY_IMPORT.search(payload):
            violations.append(f"inline first-party snippet: {payload!r}")
        elif PYTHON.match(name) and flag == "-m" and importlib.util.find_spec(payload) is None:
            violations.append(f"module does not resolve: {payload!r}")
        elif name in SHELLS and flag == "-c":
            violations.extend(_argv_violations(_shell_words(payload)))
    return violations


def test_the_compose_files_declare_healthchecks():
    assert _healthcheck_tests((ROOT / "compose.yaml").read_text())


def test_no_compose_healthcheck_runs_first_party_code_except_through_a_resolving_module():
    for path in COMPOSE_FILES:
        for number, value in _healthcheck_tests(path.read_text()):
            assert not _probe_violations(value), (
                f"{path.relative_to(ROOT)}:{number}: use `python -m <module>` naming a real module, "
                f"not an inline snippet: {_probe_violations(value)}")


def test_the_worker_healthcheck_is_the_media_healthcheck_module():
    tests = [value for _, value in _healthcheck_tests((ROOT / "compose.yaml").read_text())
             if "media.healthcheck" in str(value)]
    assert tests == [["CMD", "python", "-m", "media.healthcheck"]]
    assert importlib.util.find_spec("media.healthcheck") is not None


@pytest.mark.parametrize("probe", [
    '    healthcheck:\n      test: ["CMD", "python3", "-c", "from media.worker import x"]\n',
    '    healthcheck:\n      test: ["CMD-SHELL", "python3 -c \'import central.db\'"]\n',
    '    healthcheck:\n      test: "/app/.venv/bin/python -c \'from contracts import models\'"\n',
    '    healthcheck:\n      test:\n        - CMD\n        - python\n        - -c\n'
    '        - from media.repository import MediaRepository\n',
    '    healthcheck:\n      test:\n        - CMD-SHELL\n        - sh -c "python -c \'import scripts.x\'"\n',
    '    healthcheck:\n      interval: 2s\n      test: >\n        python -c\n        "import uplink.locate"\n',
    '    healthcheck: {test: ["CMD", "python", "-c", "import player.service"]}\n',
    '    healthcheck:\n      test: ["CMD", "python", "-m", "media.no_such_healthcheck"]\n',
    '    healthcheck:\n      test: [CMD, python, -m, central.missing]\n',
])
def test_the_scan_refuses_every_hidden_first_party_form(probe):
    found = _healthcheck_tests(probe)
    assert len(found) == 1, found
    assert _probe_violations(found[0][1]), found


@pytest.mark.parametrize("probe", [
    '    healthcheck:\n      test: ["CMD-SHELL", "pg_isready -U photo_wall"]\n',
    '    healthcheck:\n      test: [CMD, redis-cli, ping]\n',
    '    healthcheck:\n      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen(\'x\')"]\n',
    '    healthcheck:\n      test:\n        - CMD\n        - python\n        - -m\n        - media.healthcheck\n',
    '    healthcheck: {disable: false}\n',
])
def test_the_scan_admits_stdlib_and_resolving_probes(probe):
    assert not any(_probe_violations(value) for _, value in _healthcheck_tests(probe))
