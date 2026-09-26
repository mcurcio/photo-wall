"""The computed first-party closure on synthetic package trees: imports at module level and
inside functions are both followed; a third-party import, a missing first-party module or a
forbidden package is refused with the importer named; the search path never includes
site-packages."""

import sys
from pathlib import Path

import pytest

from scripts.module_closure import (
    INITRD_FORBIDDEN,
    ClosureError,
    compute_closure,
    first_party_packages,
    main,
    read_manifest,
    search_path,
    stage,
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


def test_the_search_path_is_the_stdlib_only(tmp_path):
    path = search_path()
    assert path and all("site-packages" not in entry and "dist-packages" not in entry
                        for entry in path)
    if sys.prefix != sys.base_prefix:
        assert not any(entry.startswith(sys.prefix) for entry in path)


def test_stage_copies_the_closure_files(tmp_path):
    closure = closure_of(tmp_path / "repo")
    stage(closure, repo=tmp_path / "repo", into=tmp_path / "staged")
    staged = sorted(path.relative_to(tmp_path / "staged")
                    for path in (tmp_path / "staged").rglob("*.py"))
    assert tuple(staged) == closure.files


def test_first_party_packages_are_the_top_level_packages(tmp_path):
    tree(tmp_path, {**BASE, "scripts/tool.py": "", "notes/readme.txt": ""})
    assert first_party_packages(tmp_path) == FIRST_PARTY


def test_main_stages_writes_the_manifest_and_names_the_offender(tmp_path, capsys):
    repo = tree(tmp_path / "repo", {**BASE, "pkg_a/bad.py": "import player\n",
                                    "player/__init__.py": ""})
    manifest = tmp_path / "manifest.json"
    assert main(["--repo", str(repo), "--root", "pkg_a.main", "--stage", str(tmp_path / "out"),
                 "--manifest", str(manifest)]) == 0
    written = read_manifest(manifest)
    assert written.modules[0] == "pkg_a" and written.forbidden == INITRD_FORBIDDEN
    assert (tmp_path / "out" / "pkg_c" / "deferred.py").is_file()
    assert main(["--repo", str(repo), "--root", "pkg_a.bad"]) == 1
    assert "pkg_a.bad imports player" in capsys.readouterr().err
