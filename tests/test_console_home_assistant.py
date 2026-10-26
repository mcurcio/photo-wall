"""Settings › Home Assistant's version and update lines, from the console's pure model
(central/console/src/domain/home-assistant/model.ts), bundled by the console's own esbuild and run
under Node: Galleria's version says when Central heard it; the update line tells apart a Galleria
never heard from (install it), an old one that sends no version (update it) and one that cannot
send colour sensors (update it); nothing while Central judges no update.

Mutation probes: versionWords without its heard time -> the version case fails; updateWords saying
"update" when nothing was heard -> the not-heard case fails.
"""

import json
import subprocess
from pathlib import Path

import pytest
from test_console_flow import _require_node
from test_console_routes_r4 import ESBUILD, _require_esbuild

MODEL = Path(__file__).parents[1] / "central/console/src/domain/home-assistant/model.ts"
HEARD = 1_791_000_000
GALLERIA = {"version": "0.40.0", "kinds": ["light", "illuminance", "colour-temperature"], "heard_at": HEARD,
            "central_version": "0.40.0", "update": False}
NO_BIRTH = {"version": None, "kinds": [], "heard_at": None, "central_version": "0.40.0", "update": True}


def _view(galleria: dict, heard_at: int | None = HEARD) -> dict:
    return {"heard_at": heard_at, "entities": [], "refused": [], "galleria": galleria}


VIEWS = {
    "current": _view(GALLERIA),
    "cannot-send-colour": _view({**GALLERIA, "version": "0.39.0", "kinds": ["light", "illuminance"], "update": True}),
    "old": _view(NO_BIRTH),
    "never-heard": _view(NO_BIRTH, heard_at=None),
}

SCRIPT = r"""
const words = await import(process.argv[1]);
const views = JSON.parse(process.argv[2]);
const since = (at) => `Tue 14:20 (${at})`;
process.stdout.write(JSON.stringify(Object.fromEntries(Object.entries(views).map(([name, view]) =>
  [name, { version: words.versionWords(view.galleria, since), update: words.updateWords(view) }]))));
"""


@pytest.fixture(scope="module")
def said(tmp_path_factory):
    _require_node()
    _require_esbuild()
    bundle = tmp_path_factory.mktemp("home-assistant") / "model.mjs"
    subprocess.run([str(ESBUILD), str(MODEL), "--bundle", "--format=esm", "--platform=node", f"--outfile={bundle}",
                    "--log-level=error"], check=True, timeout=60)
    result = subprocess.run(["node", "--input-type=module", "-e", SCRIPT, bundle.as_uri(), json.dumps(VIEWS)],
                            capture_output=True, text=True, check=True, timeout=30)
    return json.loads(result.stdout)


def test_galleria_s_version_says_when_central_heard_it(said):
    assert said["current"]["version"] == f"Galleria 0.40.0, heard Tue 14:20 ({HEARD}) · Central 0.40.0"
    assert said["old"]["version"] == "Central 0.40.0"


def test_the_update_line_tells_never_heard_old_and_cannot_send_colour_apart(said):
    assert said["current"]["update"] is None
    assert said["cannot-send-colour"]["update"] == "Update Galleria in HACS: this version cannot send colour sensors yet"
    assert said["old"]["update"] == "Galleria has not said its version, so it is older than this release: update it in HACS"
    assert said["never-heard"]["update"] == (
        "Galleria has not been heard from: install it in HACS and add it in Home Assistant (runbook: Connect Home Assistant)")
