"""The computed first-party closure on synthetic package trees: imports at module level and
inside functions are both followed; an undeclared third-party import, a missing first-party
module or a forbidden package is refused with the importer named; a declared third-party import
is recorded; the stdlib's own imports are not judged; the search path never includes
site-packages."""

import sys
from pathlib import Path
from types import MappingProxyType

import pytest

from scripts import module_closure
from scripts.module_closure import (
    ClosureError,
    compute_closure,
    first_party_files,
    first_party_packages,
    search_path,
)

FIRST_PARTY = ("pkg_a", "pkg_b", "pkg_c", "pkg_d")


def tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


BASE = {
    "pkg_a/__init__.py": "",
    "pkg_a/main.py": "import json\nimport pkg_b.helper\n\n"
                     "def later():\n    from pkg_c import deferred\n    return deferred\n",
    "pkg_b/__init__.py": "",
    "pkg_b/helper.py": "import hashlib\n",
    "pkg_c/__init__.py": "",
    "pkg_c/deferred.py": "VALUE = 1\n",
    "pkg_d/__init__.py": "",
    "pkg_d/unused.py": "import pydantic\n",
}


def closure_of(root: Path, extra: dict[str, str] | None = None, **kwargs):
    tree(root, {**BASE, **(extra or {})})
    return compute_closure(["pkg_a.main"], repo=root, first_party=FIRST_PARTY, **kwargs)


def test_module_level_and_function_body_imports_are_both_in_the_closure(tmp_path):
    closure = closure_of(tmp_path)
    assert closure.modules == ("pkg_a", "pkg_a.main", "pkg_b", "pkg_b.helper", "pkg_c",
                               "pkg_c.deferred")
    assert closure.files == tuple(Path(name) for name in (
        "pkg_a/__init__.py", "pkg_a/main.py", "pkg_b/__init__.py", "pkg_b/helper.py",
        "pkg_c/__init__.py", "pkg_c/deferred.py"))
    assert len(closure.digest) == 64


def test_the_digest_follows_the_content(tmp_path):
    before = closure_of(tmp_path / "one").digest
    after = closure_of(tmp_path / "two", {"pkg_c/deferred.py": "VALUE = 2\n"}).digest
    assert before != after


@pytest.mark.parametrize("statement", ["import pydantic", "from httpx import Client"])
def test_a_third_party_import_is_refused_naming_the_importer(tmp_path, statement):
    extra = {"pkg_b/helper.py": f"def lazily():\n    {statement}\n"}
    with pytest.raises(ClosureError) as caught:
        closure_of(tmp_path, extra)
    assert "pkg_b.helper imports" in str(caught.value)
    assert statement.split()[1] in str(caught.value)


def test_a_missing_first_party_module_is_refused(tmp_path):
    with pytest.raises(ClosureError, match="pkg_c.absent, which does not exist"):
        closure_of(tmp_path, {"pkg_b/helper.py": "import pkg_c.absent\n"})


def test_a_forbidden_first_party_package_is_refused(tmp_path):
    with pytest.raises(ClosureError, match="pkg_b.helper imports pkg_c.*forbidden"):
        closure_of(tmp_path, {"pkg_b/helper.py": "import pkg_c.deferred\n"},
                   forbidden=("pkg_c",))


def test_a_forbidden_package_not_reached_is_fine(tmp_path):
    assert closure_of(tmp_path, forbidden=("pkg_d",)).modules[0] == "pkg_a"


def test_a_dotted_forbidden_entry_catches_a_two_hop_chain(tmp_path):
    with pytest.raises(ClosureError,
                       match="pkg_b.helper imports pkg_d.target: pkg_d.target is forbidden here"):
        closure_of(tmp_path, {"pkg_b/helper.py": "import pkg_d.target\n", "pkg_d/target.py": ""},
                   forbidden=("pkg_d.target",))


def test_a_dotted_forbidden_entry_does_not_match_a_sibling_sharing_its_prefix(tmp_path):
    closure = closure_of(tmp_path, {"pkg_b/help.py": ""}, forbidden=("pkg_b.help",))
    assert "pkg_b.helper" in closure.modules


def test_a_dotted_forbidden_entry_naming_no_module_is_refused(tmp_path):
    with pytest.raises(ClosureError, match="forbidden entry pkg_c.absent names no module under"):
        closure_of(tmp_path, forbidden=("pkg_c.absent",))


def fake_stdlib(tmp_path: Path, monkeypatch) -> None:
    """A directory searched before the real stdlib: `colorsys` (a real stdlib name) whose own
    code imports `_pw_not_stdlib`, a module that is neither first-party nor stdlib -- the shape
    of CPython's `multiprocessing.util` importing `test.support` -> `_testcapi` on an
    interpreter that ships its test suite."""
    stdlib = tree(tmp_path / "fake-stdlib", {
        "colorsys.py": "def f():\n    import _pw_not_stdlib\n",
        "_pw_not_stdlib.py": "",
    })
    real = module_closure.search_path()
    monkeypatch.setattr(module_closure, "search_path", lambda: [str(stdlib), *real])


def test_the_stdlibs_own_imports_are_not_judged(tmp_path, monkeypatch):
    fake_stdlib(tmp_path, monkeypatch)
    repo = tree(tmp_path / "repo", {"pkg_a/__init__.py": "import colorsys\n"})
    closure = compute_closure(["pkg_a"], repo=repo, first_party=FIRST_PARTY)
    assert closure.modules == ("pkg_a",)


def test_a_found_non_stdlib_module_imported_by_first_party_code_is_refused(tmp_path,
                                                                          monkeypatch):
    fake_stdlib(tmp_path, monkeypatch)
    repo = tree(tmp_path / "repo", {"pkg_a/__init__.py": "def f():\n    import _pw_not_stdlib\n"})
    with pytest.raises(ClosureError,
                       match="pkg_a imports _pw_not_stdlib, which is neither first-party"):
        compute_closure(["pkg_a"], repo=repo, first_party=FIRST_PARTY)


def test_the_search_path_is_the_stdlib_only(tmp_path):
    path = search_path()
    assert path and all("site-packages" not in entry and "dist-packages" not in entry
                        for entry in path)
    if sys.prefix != sys.base_prefix:
        assert not any(entry.startswith(sys.prefix) for entry in path)


def test_first_party_files_are_the_closures_files_unjudged(tmp_path):
    """pkg_d.unused's pydantic import is not judged; a missing first-party module is."""
    closure = closure_of(tmp_path / "repo")
    assert first_party_files(["pkg_a.main"], repo=tmp_path / "repo") == closure.files
    assert Path("pkg_d/unused.py") in first_party_files(["pkg_d.unused"], repo=tmp_path / "repo")
    tree(tmp_path / "repo", {"pkg_b/helper.py": "import pkg_b.absent\n"})
    with pytest.raises(ClosureError, match="pkg_b.absent, which does not exist"):
        first_party_files(["pkg_a.main"], repo=tmp_path / "repo")


def test_first_party_packages_are_the_top_level_packages(tmp_path):
    tree(tmp_path, {**BASE, "scripts/tool.py": "", "notes/readme.txt": ""})
    assert first_party_packages(tmp_path) == FIRST_PARTY


# --- the third-party table -------------------------------------------------------------------


DECLARED = MappingProxyType({"pydantic": "python3-pydantic", "gi": "python3-gi"})


@pytest.mark.parametrize("statement", ["import pydantic", "from gi.repository import GLib"])
def test_a_declared_third_party_import_is_allowed_and_recorded(tmp_path, statement):
    extra = {"pkg_b/helper.py": f"def lazily():\n    {statement}\n"}
    closure = closure_of(tmp_path, extra, third_party=DECLARED)
    assert closure.third_party == (statement.split()[1].partition(".")[0],)
    assert closure.modules[:2] == ("pkg_a", "pkg_a.main")


def test_an_undeclared_third_party_import_is_refused_naming_the_importer(tmp_path):
    extra = {"pkg_b/helper.py": "import pydantic\nimport requests\n"}
    with pytest.raises(ClosureError, match="pkg_b.helper imports requests, which is neither"):
        closure_of(tmp_path, extra, third_party=DECLARED)


def test_a_forbidden_name_is_refused_even_when_declared(tmp_path):
    with pytest.raises(ClosureError, match="pydantic is forbidden here"):
        closure_of(tmp_path, {"pkg_b/helper.py": "import pydantic\n"}, third_party=DECLARED,
                   forbidden=("pydantic",))


def test_the_finder_records_each_direct_import_with_its_ancestors_found_or_not(tmp_path):
    tree(tmp_path, {
        **BASE,
        "pkg_a/main.py": "import json\nimport pkg_b.helper\nfrom pkg_c import deferred, VALUE\n"
                         "from . import sibling\nfrom .sibling import thing\n\n"
                         "def later():\n    import _pw_absent_lib.sub\n",
        "pkg_a/sibling.py": "thing = 1\n",
    })
    finder = module_closure.Finder([str(tmp_path), *search_path()], frozenset(FIRST_PARTY))
    finder.import_hook("pkg_a.main")
    assert {edge for edge in finder.edges if edge[0] == "pkg_a.main"} == {
        ("pkg_a.main", "json"), ("pkg_a.main", "pkg_b"), ("pkg_a.main", "pkg_b.helper"),
        ("pkg_a.main", "pkg_c"), ("pkg_a.main", "pkg_c.deferred"), ("pkg_a.main", "pkg_a"),
        ("pkg_a.main", "pkg_a.sibling"), ("pkg_a.main", "_pw_absent_lib"),
        ("pkg_a.main", "_pw_absent_lib.sub")}
    # Direct, never transitive: the helper's import is the helper's edge; the stdlib is not
    # scanned, so every importer is first-party.
    assert ("pkg_b.helper", "hashlib") in finder.edges
    assert {importer.partition(".")[0] for importer, _ in finder.edges} <= set(FIRST_PARTY)
