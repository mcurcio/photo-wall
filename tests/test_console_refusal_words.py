"""Every refusal Central can answer the console with has its own words: each context's closed
vocabulary in central/console/src/generated/contract.ts (`REFUSALS`, written from
central.console_contract `CONSOLE_REFUSALS`) is matched by the words tables the console shows it
through, bundled by the console's own esbuild and run under Node (they are pure). A table is a
`Record` over its generated union, so the typecheck already refuses a missing code; this test holds
the other half: every context the contract carries has a table (a new context without words fails
here), every table covers exactly its context's codes, and no code's words are empty or the general
"refused" sentence a code outside the contract falls back to.

Mutation probes: drop a code's words from a table (with a `@ts-expect-error`) -> the coverage case
fails; add a refusal context to CONSOLE_REFUSALS and regenerate -> the context case fails; answer a
code with the fallback sentence -> the fallback case fails.
"""

import json
import subprocess

import pytest
from test_console_flow import _require_node
from test_console_routes_r4 import ESBUILD, SRC, _require_esbuild

# Each context's words tables: (module under src/, exported table). A context may be shown in more
# than one place, each with its own words (a Pi's reading refused on its Hardware page, a Home
# Assistant message refused on that page).
TABLES = {
    "room_light": [("domain/room-lights/model.ts", "REFUSAL_WORDS")],
    "frame_lighting": [("domain/frame-lighting/model.ts", "LIGHTING_REFUSAL_WORDS")],
    "paper_profiles": [("domain/paper-profiles/model.ts", "PAPER_PROFILE_REFUSALS")],
    "reports": [("domain/room-lights/model.ts", "REFUSED_READING_WORDS"),
                ("domain/home-assistant/model.ts", "REFUSED_WORDS")],
    "trial": [("domain/trial-refusals.ts", "TRIAL_REFUSAL_WORDS")],
    "position_restore": [("domain/trial-refusals.ts", "POSITION_RESTORE_WORDS")],
}
# Contexts the console never receives: the drawn report is the Player's PUT, answered to its bearer.
NOT_SHOWN = {"drawn_report"}
# The general sentences a code outside the contract is said with (liveAdjustment.js, the pages).
FALLBACKS = ("Photo Wall refused the change.", "Central refused it")


@pytest.fixture(scope="module")
def said(tmp_path_factory):
    _require_node()
    _require_esbuild()
    folder = tmp_path_factory.mktemp("refusal-words")
    modules = sorted({module for tables in TABLES.values() for module, _ in tables})
    lines = [f'import {{ REFUSALS }} from {json.dumps(str(SRC / "generated/contract.ts"))};']
    lines += [f"import * as m{index} from {json.dumps(str(SRC / module))};" for index, module in enumerate(modules)]
    tables = {context: [f'm{modules.index(module)}["{name}"]' for module, name in entries]
              for context, entries in TABLES.items()}
    lines.append("process.stdout.write(JSON.stringify({ refusals: REFUSALS, tables: {"
                 + ", ".join(f"{json.dumps(context)}: [{', '.join(each)}]" for context, each in tables.items())
                 + "} }));")
    entry = folder / "entry.ts"
    entry.write_text("\n".join(lines) + "\n")
    bundle = folder / "entry.mjs"
    subprocess.run([str(ESBUILD), str(entry), "--bundle", "--format=esm", "--platform=node", f"--outfile={bundle}",
                    "--log-level=error"], check=True, timeout=60)
    result = subprocess.run(["node", str(bundle)], capture_output=True, text=True, check=True, timeout=30)
    return json.loads(result.stdout)


def test_every_refusal_context_the_console_receives_has_words(said):
    assert set(said["refusals"]) == set(TABLES) | NOT_SHOWN


def test_each_table_covers_exactly_its_contexts_codes(said):
    for context, tables in said["tables"].items():
        for table in tables:
            assert table is not None, context
            assert sorted(table) == sorted(said["refusals"][context]), context


def test_no_code_is_said_with_empty_or_fallback_words(said):
    for context, tables in said["tables"].items():
        for table in tables:
            for code, words in table.items():
                assert words.strip(), (context, code)
                assert not words.startswith(FALLBACKS), (context, code, words)
                assert code not in words, (context, code, words)    # its own words, never the code itself
