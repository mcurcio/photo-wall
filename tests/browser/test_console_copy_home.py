"""The console's Schedule copy has one home: no browser test re-types it.

Every label, button name and sentence that names a Schedule is a template in
central/console/src/showWords.json; the tests reach it through console_words.py and
schedule_page.py. This check fails when a browser test's string literal is one of those
templates typed again (a plain string equal to one, or an f-string with the same text around its
holes), so a copy change stays one JSON edit. It reads the source; it needs no browser.

What it leaves uncovered: a hand-filled template ("Schedule Evenings" typed whole) cannot be told
from other copy that shares the word ("Schedule it" is the Scene card's button).
"""

import ast
import re
from pathlib import Path

from console_words import SCHEDULE

HERE = Path(__file__).parent
_HOLE = re.compile(r"\{\w+\}")


def _shape(template):
    """A template's text with every hole as "{}"."""
    return _HOLE.sub("{}", template)


def _literal(node):
    """A string literal's text, an f-string's with "{}" for each value; None for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)
    return None


def _docstrings(tree):
    """The ids of the module's, classes' and functions' docstring nodes (prose, not copy)."""
    owners = [tree, *(node for node in ast.walk(tree)
                      if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef))]
    return {id(owner.body[0].value) for owner in owners
            if owner.body and isinstance(owner.body[0], ast.Expr)
            and isinstance(owner.body[0].value, ast.Constant)}


def retyped_copy(path, templates):
    """(line, text) of each literal in `path` that is one of `templates` typed again."""
    shapes = {_shape(template) for template in templates}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstrings(tree) | {id(part) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)
                                for part in node.values}  # an f-string is read whole
    found = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        text = _literal(node)
        if text is not None and text in shapes:
            found.append((node.lineno, text))
    return found


def test_no_browser_test_retypes_the_schedule_copy():
    # The bare noun ("one") is left out: "Schedule" is also the page's own name (its sidebar link
    # and heading, console_tasks.LABELS), which is not the Schedule's copy.
    templates = [value for key, value in SCHEDULE.items() if key != "one"]
    offenders = {path.name: found for path in sorted(HERE.glob("test_*.py"))
                 if (found := retyped_copy(path, templates))}
    assert offenders == {}, (
        "Browser tests re-type the console's Schedule copy; locate it through schedule_page.py or "
        f"read it from console_words.py (showWords.json): {offenders}")
