"""The construction-time import check (scripts/import_check.py, decision 0019) on tiny source and
staged trees with a fake build root: each refusal kind, the owner's exemption list (an exempt
upward edge and its ancestor package pass; any other upward edge is refused) and a clean pass.
The real build runs it inside dpkg-buildpackage (debian/rules); tests/debs proves the packages."""

from pathlib import Path, PurePosixPath

import pytest

from scripts import import_check
from scripts.import_check import Declared, ImportCheckError, Refusal

REPO = Path(__file__).resolve().parents[1]

# Three packages: lib (a wire library on pydantic), low and top (two appliance contexts). low's
# runner reaches up into top inside a function: the retiring edge the exemption list names.
SOURCE = {
    "appliance/__init__.py": '"""The namespace\'s docstring."""\n',
    "appliance/low/__init__.py": "",
    "appliance/low/core.py": "import json\nfrom lib import wire\n",
    "appliance/low/runner.py": ("from appliance.low import core\n\n"
                                "def retire():\n    from appliance.top import run\n"),
    "appliance/top/__init__.py": "",
    "appliance/top/run.py": "import appliance.low.core\nimport lib.wire\n",
    "appliance/stage1.py": "",
    "lib/__init__.py": "",
    "lib/wire.py": "import pydantic\nimport pydantic.fields\n",
}
INSTALLS = {"photo-wall-lib": ("lib",), "photo-wall-low": ("appliance/low",),
            "photo-wall-top": ("appliance/top",)}
DEPENDS = {
    "photo-wall-lib": "python3, python3-pydantic, ${misc:Depends}",
    "photo-wall-low": "python3,\n photo-wall-lib (= ${pw-version:photo-wall-lib}),\n ${misc:Depends}",
    "photo-wall-top": ("python3, photo-wall-low (= ${pw-version:photo-wall-low}),\n"
                       " photo-wall-lib (= ${pw-version:photo-wall-lib}), systemd, ${misc:Depends}"),
    "photo-wall-units": "systemd, ${misc:Depends}",
}
EXEMPT = '"appliance.low.runner -> appliance.top.run"'
OWNERS = {"pydantic": "python3-pydantic"}
ROOTS = {"python3": frozenset(), "systemd": frozenset(), "python3-pydantic": frozenset({"pydantic"}),
         "python3-nats": frozenset({"nats"})}


def pyproject(ignore: str = EXEMPT, *, tables: int = 1) -> str:
    layers = ('[[tool.importlinter.contracts]]\nname = "Node contexts point down"\n'
              'type = "layers"\ncontainers = ["appliance"]\nlayers = ["top", "low"]\n'
              f"ignore_imports = [{ignore}]\n\n")
    # A forbidden contract's ignore lines exempt nothing.
    forbidden = ('[[tool.importlinter.contracts]]\nname = "Low reaches no top"\n'
                 'type = "forbidden"\nsource_modules = ["appliance.low"]\n'
                 'forbidden_modules = ["appliance.top"]\n'
                 'ignore_imports = ["appliance.low.core -> appliance.top.run"]\n')
    return "[tool.importlinter]\nroot_packages = [\"appliance\", \"lib\"]\n\n" + layers * tables + forbidden


def control(depends: dict[str, str]) -> str:
    stanzas = ["Source: photo-wall\nBuild-Depends: debhelper-compat (= 13)"]
    stanzas += [f"# {package}'s stanza\nPackage: {package}\nArchitecture: all\nDepends: {value}\n"
                f"Description: {package}\n words" for package, value in depends.items()]
    return "\n\n".join(stanzas) + "\n"


def build(tmp_path: Path, *, source: dict[str, str] | None = None,
          depends: dict[str, str] | None = None, ignore: str = EXEMPT, tables: int = 1):
    repo, staged = tmp_path / "repo", tmp_path / "staged"
    for name, text in (SOURCE | (source or {})).items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    for package, roots in INSTALLS.items():
        into = staged / package / import_check.directory(package).relative_to("/")
        for root in roots:
            for path in sorted((repo / root).glob("*.py")):
                target = into / path.relative_to(repo)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
    (staged / "photo-wall-units/lib/systemd/system").mkdir(parents=True)
    (tmp_path / "control").write_text(control(depends or DEPENDS))
    (tmp_path / "pyproject.toml").write_text(pyproject(ignore, tables=tables))
    return repo, staged


def run(tmp_path: Path, **kwargs) -> list[Refusal]:
    repo, staged = build(tmp_path, **kwargs)
    return import_check.check(repo=repo, staged=staged, control=tmp_path / "control",
                              pyproject=tmp_path / "pyproject.toml", owner=OWNERS.get,
                              roots_of=ROOTS.get)


def kinds(refusals: list[Refusal]) -> set[tuple[str, str, str, str]]:
    return {(each.package, each.kind, each.module, each.importer) for each in refusals}


def test_declared_imports_with_the_exempt_upward_edge_pass(tmp_path):
    assert run(tmp_path) == []


def test_the_exemption_covers_the_ancestor_package_its_target_installs(tmp_path):
    """`from appliance.top import run` imports appliance.top too: exempt with its target."""
    without = run(tmp_path, ignore="")
    assert kinds(without) == {
        ("photo-wall-low", "undeclared-sibling", "appliance.top", "appliance.low.runner"),
        ("photo-wall-low", "undeclared-sibling", "appliance.top.run", "appliance.low.runner")}


def test_an_upward_edge_the_list_does_not_name_is_refused(tmp_path):
    """The forbidden contract's ignore line for core -> top.run exempts nothing."""
    refusals = run(tmp_path, source={"appliance/low/core.py": "from lib import wire\n"
                                                              "import appliance.top.run\n"})
    assert kinds(refusals) == {
        ("photo-wall-low", "undeclared-sibling", "appliance.top", "appliance.low.core"),
        ("photo-wall-low", "undeclared-sibling", "appliance.top.run", "appliance.low.core")}


def test_an_import_of_a_sibling_not_in_depends_is_refused(tmp_path):
    depends = DEPENDS | {"photo-wall-top": "python3, photo-wall-low (= ${pw-version:photo-wall-low})"}
    assert kinds(run(tmp_path, depends=depends)) == {
        ("photo-wall-top", "undeclared-sibling", "lib", "appliance.top.run"),
        ("photo-wall-top", "undeclared-sibling", "lib.wire", "appliance.top.run")}


def test_an_import_of_a_first_party_module_no_package_installs_is_refused(tmp_path):
    refusals = run(tmp_path, source={"appliance/top/run.py": "import appliance.low.core\n"
                                                            "import lib.wire\n"
                                                            "import appliance.stage1\n"
                                                            "import appliance.ghost\n"})
    # appliance itself is the namespace the package directories share: no package, no refusal.
    assert kinds(refusals) == {
        ("photo-wall-top", "unowned-module", "appliance.stage1", "appliance.top.run"),
        ("photo-wall-top", "unowned-module", "appliance.ghost", "appliance.top.run")}


def test_a_third_party_import_whose_provider_is_not_in_depends_is_refused(tmp_path):
    depends = DEPENDS | {"photo-wall-lib": "python3, ${misc:Depends}"}
    refusals = run(tmp_path, depends=depends)
    assert kinds(refusals) == {("photo-wall-lib", "undeclared-provider", "pydantic", "lib.wire")}
    assert refusals[0].line() == ("import-check: photo-wall-lib: undeclared-provider: "
                                  "pydantic <- lib.wire")


def test_a_third_party_import_the_build_root_cannot_resolve_is_refused(tmp_path):
    refusals = run(tmp_path, source={"lib/wire.py": "import pydantic\nimport _pw_unpackaged\n"})
    assert kinds(refusals) == {("photo-wall-lib", "unresolved-import", "_pw_unpackaged",
                                "lib.wire")}


def test_a_depends_entry_no_edge_reaches_is_refused_unless_it_owns_no_import_root(tmp_path):
    depends = DEPENDS | {"photo-wall-low": ("python3, python3-nats, systemd, "
                                            "photo-wall-lib (= ${pw-version:photo-wall-lib}), "
                                            "photo-wall-top (= ${pw-version:photo-wall-top})")}
    refusals = run(tmp_path, depends=depends)
    # top is reached only through the exempt edge, which gives no Depends.
    assert kinds(refusals) == {
        ("photo-wall-low", "unused-depends", "python3-nats", "debian/control"),
        ("photo-wall-low", "unused-depends", "photo-wall-top", "debian/control")}


def test_a_depends_entry_the_build_root_does_not_hold_is_refused(tmp_path):
    depends = DEPENDS | {"photo-wall-lib": "python3, python3-pydantic, python3-absent"}
    assert kinds(run(tmp_path, depends=depends)) == {
        ("photo-wall-lib", "unjudgeable-depends", "python3-absent", "debian/control")}


@pytest.mark.parametrize("ignore, tables, detail", [
    ('"appliance.low.* -> appliance.top.run"', 1, "is not `<module> -> <module>`"),
    ('"appliance.low.runner->appliance.top.run"', 1, "is not `<module> -> <module>`"),
    (EXEMPT, 0, "0 import-linter layers contracts"),
    (EXEMPT, 2, "2 import-linter layers contracts"),
])
def test_an_unreadable_exemption_list_is_refused(tmp_path, ignore, tables, detail):
    refusals = run(tmp_path, ignore=ignore, tables=tables)
    assert [each.kind for each in refusals] == ["exemption-invalid"]
    assert detail in refusals[0].module


def test_an_exemption_naming_a_module_no_package_installs_is_refused(tmp_path):
    refusals = run(tmp_path, ignore=EXEMPT + ', "appliance.stage1 -> appliance.top.run"')
    assert kinds(refusals) == {("pyproject.toml", "exemption-invalid",
                                "appliance.stage1 (no package installs it)",
                                "appliance.stage1 -> appliance.top.run")}


def test_the_control_file_reads_pins_and_other_names_and_skips_substvars(tmp_path):
    (tmp_path / "control").write_text(control(DEPENDS))
    assert import_check.declared(tmp_path / "control")["photo-wall-top"] == Declared(
        frozenset({"photo-wall-low", "photo-wall-lib"}), frozenset({"python3", "systemd"}))


def test_the_runtime_directories_follow_depends_and_exempt_targets(tmp_path):
    repo, staged = build(tmp_path)
    installed = import_check.installed_modules(staged, INSTALLS)
    assert installed["appliance.top.run"] == "photo-wall-top"
    directories = import_check.runtime_directories(
        "photo-wall-low", declared=import_check.declared(tmp_path / "control"),
        exempt=import_check.exemptions(tmp_path / "pyproject.toml"), installed=installed)
    assert directories == tuple(PurePosixPath(f"/usr/lib/photo-wall/{name}")
                                for name in ("low", "lib", "top"))


def test_a_module_two_packages_install_is_an_error(tmp_path):
    repo, staged = build(tmp_path)
    twice = staged / "photo-wall-top/usr/lib/photo-wall/top/lib/wire.py"
    twice.parent.mkdir(parents=True)
    twice.write_text("")
    with pytest.raises(ImportCheckError, match="lib.wire is installed by"):
        import_check.installed_modules(staged, INSTALLS)


def test_the_repositorys_exemption_list_is_the_node_layers_ignore_lines():
    exempt = import_check.exemptions(REPO / "pyproject.toml")
    assert exempt and all(importer.startswith("appliance.") and imported.startswith("appliance.node.")
                          for importer, imported in exempt)


# The release roots' packages (decision 0019): the Player, a launcher and a module package on
# lib, beside a module-less sibling it loads by path (the frame client); the AppManager launcher.
PLAYER_DEPENDS = {
    "photo-wall-player": ("python3, photo-wall-lib (= ${pw-version:photo-wall-lib}),\n"
                          " photo-wall-frame-client (= ${pw-version:photo-wall-frame-client})"),
    "photo-wall-frame-client": "${shlibs:Depends}",
    "photo-wall-app-manager": "python3, photo-wall-top (= ${pw-version:photo-wall-top})",
}


def launcher(path: tuple[str, ...], entry: str) -> str:
    return f"import runpy, sys\nPATH: tuple[str, ...] = {path!r}\nENTRY: str = {entry!r}\n"


def roots(tmp_path, *, player_path=("/usr/lib/photo-wall/lib", "/usr/lib/photo-wall/player"),
          player_source="import lib.wire\n",
          manager_path=("/usr/lib/photo-wall/lib", "/usr/lib/photo-wall/low",
                        "/usr/lib/photo-wall/top"), source=None):
    repo, staged = build(tmp_path, source={"player/__init__.py": "",
                                           "player/service.py": player_source} | (source or {}),
                         depends=DEPENDS | PLAYER_DEPENDS)
    player = staged / "photo-wall-player/usr/lib/photo-wall/player"
    (player / "player").mkdir(parents=True)
    for name in ("__init__.py", "service.py"):
        (player / "player" / name).write_bytes((repo / "player" / name).read_bytes())
    (player / "__main__.py").write_text(launcher(player_path, "player.service"))
    (staged / "photo-wall-frame-client/usr/lib/photo-wall/frame-client").mkdir(parents=True)
    manager = staged / "photo-wall-app-manager/usr/lib/photo-wall/app-manager"
    manager.mkdir(parents=True)
    (manager / "__main__.py").write_text(launcher(manager_path, "appliance.top.run"))
    return import_check.check(repo=repo, staged=staged, control=tmp_path / "control",
                              pyproject=tmp_path / "pyproject.toml", owner=OWNERS.get,
                              roots_of=ROOTS.get)


def test_the_release_roots_launchers_pass_with_their_reached_path(tmp_path):
    """A root launcher's __main__.py is no module, its PATH is its entry's reach, and a sibling
    that installs no module (the frame client) is not judged."""
    assert roots(tmp_path) == []
    assert set(import_check.launchers(tmp_path / "staged")) == {"player", "app-manager"}


def test_a_root_launcher_path_that_is_not_its_reach_is_refused(tmp_path):
    refusals = roots(tmp_path, manager_path=("/usr/lib/photo-wall/lib", "/usr/lib/photo-wall/top"))
    assert kinds(refusals) == {("photo-wall-app-manager", "launcher-path",
                                "/usr/lib/photo-wall/low (missing from PATH)", "app-manager")}


def test_the_app_root_holds_no_node_context(tmp_path):
    """The Player reaching an appliance context: its PATH names the context's directory, which
    the app root never holds, and which the Player's Depends do not give it."""
    refusals = roots(tmp_path, player_source="import lib.wire\nimport appliance.low.core\n",
                     player_path=("/usr/lib/photo-wall/lib", "/usr/lib/photo-wall/low",
                                  "/usr/lib/photo-wall/player"))
    assert ("photo-wall-player", "launcher-path",
            "/usr/lib/photo-wall/low (a Node context in the app root)", "player") in kinds(refusals)
    assert ("photo-wall-player", "undeclared-sibling", "appliance.low.core",
            "player.service") in kinds(refusals)


# The shared rule: the package Central shares with the Node (here lib) installs only modules a
# Node program reaches.
def test_the_shared_package_installing_only_reached_modules_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(import_check, "SHARED", "photo-wall-lib")
    assert roots(tmp_path) == []


def test_a_shared_module_no_node_program_reaches_is_refused(tmp_path, monkeypatch):
    """A module only Central imports (here imported by nothing) must not ship to the Node."""
    monkeypatch.setattr(import_check, "SHARED", "photo-wall-lib")
    assert kinds(roots(tmp_path, source={"lib/central_only.py": "import json\n"})) == {
        ("photo-wall-lib", "unreached-module", "lib.central_only", "no Node program")}


# The composition (photo-wall-node): no module of its own, one launcher per directory under
# usr/lib/photo-wall/node/, each on the contexts it pins.
NODE = "/usr/lib/photo-wall/"
NODE_PINS = {name: f"photo-wall-{name} (= ${{pw-version:photo-wall-{name}}})"
             for name in ("lib", "low", "top")}


def composition(tmp_path, *, pins=("low", "top"), entry="appliance.top.run",
                path=(NODE + "lib", NODE + "low", NODE + "top")) -> list[Refusal]:
    node_depends = ", ".join(NODE_PINS[each] for each in pins) + ", systemd"
    repo, staged = build(tmp_path, depends=DEPENDS | {"photo-wall-node": node_depends})
    into = staged / "photo-wall-node/usr/lib/photo-wall/node/top-run"  # no module name
    into.mkdir(parents=True)
    (into / "__main__.py").write_text(launcher(path, entry))
    return import_check.check(repo=repo, staged=staged, control=tmp_path / "control",
                              pyproject=tmp_path / "pyproject.toml", owner=OWNERS.get,
                              roots_of=ROOTS.get)


def test_a_composition_launcher_on_its_pinned_contexts_passes(tmp_path):
    assert composition(tmp_path) == []


def test_a_composition_launcher_reaching_a_context_it_does_not_pin_is_refused(tmp_path):
    """Its PATH is its entry's reach, but low is no runtime directory of a composition pinning
    only lib (low's exempt edge would bring top: a pin on low brings both)."""
    assert kinds(composition(tmp_path, pins=("lib",), entry="appliance.low.core",
                             path=(NODE + "lib", NODE + "low"))) == {
        ("photo-wall-node", "undeclared-launcher-directory", NODE + "low", "top-run")}


def test_a_composition_pin_on_no_launchers_path_is_refused(tmp_path):
    """top installs modules, yet no launcher of the composition runs it."""
    assert kinds(composition(tmp_path, entry="appliance.low.core",
                             path=(NODE + "lib", NODE + "low"))) == {
        ("photo-wall-node", "unused-depends", "photo-wall-top", "debian/control")}


# Stage 1 (decision 0019 P4): appliance.netboot_init reaches lib's package (not its pydantic
# module); its package names the directories it reaches in a path file the initramfs hook copies
# into the initrd.
STAGE1_SOURCE = {"appliance/netboot_init.py": "import lib\n"}
STAGE1_DEPENDS = {"photo-wall-netboot-init": (
    "initramfs-tools, photo-wall-lib (= ${pw-version:photo-wall-lib})")}
STAGE1_PATH = ("/usr/lib/photo-wall/lib", "/usr/lib/photo-wall/netboot-init")


def stage1(tmp_path, *, path: tuple[str, ...] | None = STAGE1_PATH,
           source: dict[str, str] | None = None) -> list[Refusal]:
    repo, staged = build(tmp_path, source=STAGE1_SOURCE | (source or {}),
                         depends=DEPENDS | STAGE1_DEPENDS)
    into = staged / "photo-wall-netboot-init/usr/lib/photo-wall/netboot-init"
    (into / "appliance").mkdir(parents=True)
    (into / "appliance/netboot_init.py").write_bytes(
        (repo / "appliance/netboot_init.py").read_bytes())
    if path is not None:
        import_check.stage1_path_file(staged).write_text("".join(f"{each}\n" for each in path))
    return import_check.check(repo=repo, staged=staged, control=tmp_path / "control",
                              pyproject=tmp_path / "pyproject.toml", owner=OWNERS.get,
                              roots_of=(ROOTS | {"initramfs-tools": frozenset()}).get)


def test_stage_1s_path_file_naming_its_reach_passes(tmp_path):
    assert stage1(tmp_path) == []


def test_a_stage_1_path_file_missing_a_reached_directory_is_refused(tmp_path):
    """The mutation probe's shape: a reached package's directory dropped from the path file."""
    refusals = stage1(tmp_path, path=STAGE1_PATH[1:])
    assert kinds(refusals) == {("photo-wall-netboot-init", "launcher-path",
                                "/usr/lib/photo-wall/lib (missing from PATH)", "path")}


def test_a_stage_1_path_file_naming_an_unreached_or_unsorted_directory_is_refused(tmp_path):
    unreached = stage1(tmp_path / "a", path=(*STAGE1_PATH, "/usr/lib/photo-wall/top"))
    assert ("photo-wall-netboot-init", "launcher-path",
            "/usr/lib/photo-wall/top (not reached)", "path") in kinds(unreached)
    assert kinds(stage1(tmp_path / "b", path=tuple(reversed(STAGE1_PATH)))) == {
        ("photo-wall-netboot-init", "launcher-path", "PATH is not sorted and unique", "path")}


def test_stage_1_without_its_path_file_is_refused(tmp_path):
    assert [(each.kind, each.importer) for each in stage1(tmp_path, path=None)] == [
        ("launcher-path", "path")]


def test_stage_1_reaching_a_third_party_import_is_refused(tmp_path):
    """lib imports pydantic, which lib's package declares: stage 1 reaching it is still refused,
    since the initrd holds no Python provider."""
    refusals = stage1(tmp_path, source={"appliance/netboot_init.py": "import lib.wire\n"})
    assert kinds(refusals) == {("photo-wall-netboot-init", "stage-1-third-party", "pydantic",
                                "lib.wire")}
