"""The console's Show words, read from the file the console renders them from.

`central/console/src/showWords.json` is the one home of every label, heading, button name and
outcome sentence that names a Schedule (the console's word for Central's Program); the console
fills its templates in `showWords.js`, and the browser tests fill the same templates here, as
`routeSamples.json` is shared with the route tables. Renaming a word is one JSON edit: no test
re-types the console's copy (tests/browser/test_console_copy_home.py holds them to it).
"""

import json
import re
from pathlib import Path

WORDS = json.loads(
    (Path(__file__).parents[2] / "central/console/src/showWords.json").read_text(encoding="utf-8"))

# The Schedule's templates (showWords.json `schedule`).
SCHEDULE = WORDS["schedule"]

_HOLE = re.compile(r"\{(\w+)\}")


def fill(template, **values):
    """`template` with each `{key}` replaced by `values[key]`, as showWords.js `fill` does: a
    hole with no value stays as it is."""
    return _HOLE.sub(lambda hole: str(values[hole[1]]) if hole[1] in values else hole[0], template)


def schedule(key, **values):
    """One Schedule template filled: `schedule("card", name="Evenings")` -> "Schedule Evenings"."""
    return fill(SCHEDULE[key], **values)


def schedule_pattern(key, **values):
    """Schedule template `key` as a whole-text pattern: the given holes filled, every other hole
    any text (one the console fills from elsewhere, such as flow/useFlowWrite.js NOT_CONFIRMED)."""
    filled = schedule(key, **values)
    return re.compile("^" + _HOLE.sub(".+", re.escape(filled).replace(r"\{", "{").replace(r"\}", "}")) + "$")
