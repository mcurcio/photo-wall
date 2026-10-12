"""The console's words, read from the files the console renders them from (one home each).

`central/console/src/showWords.json` holds the Show's copy (the Schedule, the console's word for
Central's Program, and a Scene's actions, Show now and Schedule it); `powerWords.json` holds display
power's (Night off, All off and All on, holds, Resting, the power line). The console fills their
templates in `showWords.js` and `powerWords.js`; the tests fill the same templates here, as
`routeSamples.json` is shared with the route tables. A copy change is one JSON edit: no test types
the console's copy again (tests/test_console_copy_home.py holds every test module to it).
"""

import json
import re
from pathlib import Path

_SRC = Path(__file__).parents[2] / "central/console/src"
# Every catalog's namespaces, by file: {"schedule": {...}, "scene": {...}, "power": {...}}.
WORDS = {namespace: templates
         for name in ("showWords.json", "powerWords.json")
         for namespace, templates in json.loads((_SRC / name).read_text(encoding="utf-8")).items()}

# The Schedule's templates (showWords.json `schedule`).
SCHEDULE = WORDS["schedule"]
# A Scene's actions (showWords.json `scene`).
SCENE = WORDS["scene"]
# Display power's (powerWords.json `power`).
POWER = WORDS["power"]

_HOLE = re.compile(r"\{(\w+)\}")


def fill(template, **values):
    """`template` with each `{key}` replaced by `values[key]`, as showWords.js `fill` does: a
    hole with no value stays as it is."""
    return _HOLE.sub(lambda hole: str(values[hole[1]]) if hole[1] in values else hole[0], template)


def schedule(key, **values):
    """One Schedule template filled: `schedule("card", name="Evenings")` -> "Schedule Evenings"."""
    return fill(SCHEDULE[key], **values)


def scene_words(key, **values):
    """One Scene template filled: `scene_words("scheduleCard", id="evening")` -> "Schedule Scene evening"."""
    return fill(SCENE[key], **values)


def power(key, **values):
    """One power template filled: `power("band", what=POWER["nightOffBand"], when="…")`."""
    return fill(POWER[key], **values)


def pattern(template, **values):
    """`template` as a whole-text pattern: the given holes filled, every other hole any text."""
    filled = fill(template, **values)
    return re.compile("^" + _HOLE.sub(".+", re.escape(filled).replace(r"\{", "{").replace(r"\}", "}")) + "$")


def schedule_pattern(key, **values):
    """Schedule template `key` as a whole-text pattern: the given holes filled, every other hole
    any text (one the console fills from elsewhere, such as flow/useFlowWrite.js NOT_CONFIRMED)."""
    return pattern(SCHEDULE[key], **values)
