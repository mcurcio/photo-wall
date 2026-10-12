"""The console's copy has one home: no test types it again.

Every label, button name and sentence a test looks for in the Show's and display power's screens
is a template in central/console/src/showWords.json or powerWords.json; the console fills them
(showWords.js, powerWords.js) and the tests reach them through support/console_words.py and the
page objects (schedule_page.py, power_page.py). This check reads every Python module under tests/
(helpers and node-script tests included, not only browser tests) and fails when a string there
is one of those templates typed again: a plain string equal to one, or an f-string or a `+` join
with the same text around its holes. So a copy change stays one JSON edit. It reads source only.

What it leaves uncovered: a template typed whole with its holes already filled ("Schedule
Evenings") cannot be told from other copy that shares the words ("Schedule it" is a Scene's
button); and copy the catalogs do not hold yet (the Scene list, Sources, Runs) is not checked.
"""

import ast
import re
from pathlib import Path

from support.console_words import WORDS

TESTS = Path(__file__).parent
_HOLE = re.compile(r"\{\w+\}")
# Words that are also a page's own name (its sidebar link and heading, console_tasks.LABELS, from
# the route table), not the catalog's copy.
PAGE_NAMES = {("schedule", "one")}


def _shape(template):
    """A template's text with every hole as "{}"."""
    return _HOLE.sub("{}", template)


def _literal(node):
    """A string's text with "{}" for each value it joins in: a plain string, an f-string, or a
    `+` join of strings and values; None for anything else (or a join with no string in it)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant) else "{}" for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left), _literal(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else "{}") + (right if right is not None else "{}")
    return None


def _inner(tree):
    """The ids of nodes read as part of a larger string (an f-string's parts, a join's operands),
    and of docstrings (prose, not copy)."""
    owners = [tree, *(node for node in ast.walk(tree)
                      if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef))]
    skip = {id(owner.body[0].value) for owner in owners
            if owner.body and isinstance(owner.body[0], ast.Expr)
            and isinstance(owner.body[0].value, ast.Constant)}
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            skip.update(id(part) for part in node.values)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            skip.update((id(node.left), id(node.right)))
    return skip


def retyped_copy(source, templates):
    """(line, text) of each string in Python `source` that is one of `templates` typed again."""
    shapes = {_shape(template) for template in templates}
    tree = ast.parse(source)
    skip = _inner(tree)
    found = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        text = _literal(node)
        if text is not None and text in shapes:
            found.append((node.lineno, text))
    return sorted(found)


def _templates():
    return [template for namespace, templates in WORDS.items() for key, template in templates.items()
            if (namespace, key) not in PAGE_NAMES]


def test_the_check_finds_a_template_typed_again_in_every_shape():
    """The check can fail: a plain string, an f-string and a `+` join that re-type a template are
    each found; a docstring and other copy sharing its words are not."""
    source = '''
def test_x(name):
    """Schedule {name} removed."""
    a = "Night off"
    b = f"Schedule {name} removed."
    c = "Schedule " + name + " removed."
    d = "Night off rules"
    e = "Schedule " + name
'''
    assert retyped_copy(source, ["Night off", "Schedule {name} removed."]) == [
        (4, "Night off"), (5, "Schedule {} removed."), (6, "Schedule {} removed.")]


def test_no_test_types_the_consoles_copy_again():
    templates = _templates()
    here = Path(__file__).resolve()
    offenders = {str(path.relative_to(TESTS)): found for path in sorted(TESTS.rglob("*.py"))
                 if path.resolve() != here and (found := retyped_copy(path.read_text(encoding="utf-8"), templates))}
    assert offenders == {}, (
        "Tests type the console's copy again; locate it through the page objects (schedule_page.py, "
        f"power_page.py) or read it from support/console_words.py: {offenders}")
