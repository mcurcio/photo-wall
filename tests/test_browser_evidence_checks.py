"""Every operator-browser evidence check names a browser test that exists.

The browser tier's evidence (tests/browser/conftest.py CHECKS) marks a check `not_completed`
when no test by that name ran, and CI's "Require passed operator browser evidence" step then
fails with every test green. A test renamed or moved without its CHECKS key is caught here, in
the unit tier, with the stale name.
"""
from __future__ import annotations

import ast
from pathlib import Path

BROWSER = Path(__file__).resolve().parent / "browser"


def _check_names() -> set[str]:
    tree = ast.parse((BROWSER / "conftest.py").read_text())
    constants = {
        target.id: node.value.value
        for node in tree.body if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant)
    }
    checks = next(
        node.value for node in tree.body if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "CHECKS" for t in node.targets))
    return {key.value if isinstance(key, ast.Constant) else constants[key.id] for key in checks.keys}


def _browser_tests() -> set[str]:
    return {
        node.name
        for path in BROWSER.glob("test_*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }


def test_every_evidence_check_names_an_existing_browser_test():
    assert sorted(_check_names() - _browser_tests()) == []
