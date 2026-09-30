"""The release rule (scripts/release_plan.py): its decisions on scratch git repositories with the
repository's real commitizen configuration, the gate's verdicts, the package manifest against the
real tree, the Dockerfile and the computed closures, and pipeline.yml's wiring against the rule.
The seal, the one job that writes a version, is tests/test_release_seal.py's."""

import ast
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from contracts.release import IMAGES
from scripts import release_plan
from scripts.module_closure import POLICIES, closure_for
from scripts.release_plan import (
    ALWAYS_JOBS,
    DOCKERFILE,
    NOT_SHIPPED,
    PACKAGES,
    PUBLISH_JOBS,
    RELEASE_JOBS,
    SUITES,
    Commitizen,
    DockerfileError,
    Plan,
    PlanError,
    PullRequestRun,
    ReleaseRun,
    Suite,
    changed_paths,
    check_commits,
    claimed_by,
    dockerfile_stages,
    gate,
    image_inputs,
    matches,
    plan_head,
    pyproject_digest,
    refuse_a_taken_tag,
)
from scripts.release_seal import RELEASE_BRANCH

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github/workflows"
ACTION = REPO / ".github/actions/python-uv/action.yml"

# commitizen runs through uvx. CI installs uv (.github/actions/python-uv), so there a missing uvx
# fails these tests instead of skipping them.
needs_uvx = pytest.mark.skipif(shutil.which("uvx") is None and not os.environ.get("CI"),
                               reason="uvx (uv) is not on PATH")

# The squash commit that shipped #28 with no release, verbatim.
PR28_SQUASH = "Netboot: every boot stage reaches the configured Central (0014) (#28)"


@pytest.fixture(autouse=True)
def _hermetic_git(monkeypatch):
    for key, value in {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                       "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
                       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}.items():
        monkeypatch.setenv(key, value)
    for key in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(key, raising=False)


class Scratch:
    """A scratch repository seeded with the real pyproject.toml ([tool.commitizen]) and one
    file in a shipped package, a doc and a test."""

    def __init__(self, root: Path, *, tag: str | None = "v0.8.0",
                 pyproject: str | None = None) -> None:
        self.root = root
        root.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.commit("chore: seed", {
            "pyproject.toml": pyproject or (REPO / "pyproject.toml").read_text(),
            "player/service.py": "VALUE = 0\n", "central/app.py": "APP = 0\n",
            "docs/guide.md": "# Guide\n", "tests/test_x.py": "def test_x(): pass\n"})
        if tag:
            self.git("tag", tag)

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), "-c", "commit.gpgsign=false", *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, message: str, files: dict[str, str] | None = None) -> str:
        for name, text in (files or {}).items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def plan(self):
        return plan_head(self.root, Commitizen(self.root))

    def push(self) -> ReleaseRun:
        return ReleaseRun(self.plan())

    def main(self, *args: str, output: Path) -> dict[str, str]:
        """release_plan's command line on this repository, and the outputs it wrote."""
        assert release_plan.main(["--repo", str(self.root), *args]) == 0
        return dict(line.split("=", 1) for line in output.read_text().splitlines())


@pytest.fixture
def scratch(tmp_path):
    return Scratch(tmp_path / "repo")


# --- the rule on scratch repositories ----------------------------------------------------------

@needs_uvx
def test_a_package_change_with_fix_releases_a_patch(scratch):
    scratch.commit("fix(player): steady frames", {"player/service.py": "VALUE = 1\n"})
    plan = scratch.plan()
    assert (plan.tag, plan.increment, plan.source) == ("v0.8.1", "PATCH", "commitizen")
    assert plan.since == "v0.8.0"
    assert {"player-deb", "bootstrapper-deb", "base-bundle", "central-image"} <= set(plan.packages)
    assert set(RELEASE_JOBS) <= set(ReleaseRun(plan).jobs)


@needs_uvx
def test_feat_releases_a_minor(scratch):
    scratch.commit("feat(central): a new view", {"central/app.py": "APP = 1\n"})
    plan = scratch.plan()
    assert (plan.tag, plan.increment, plan.source) == ("v0.9.0", "MINOR", "commitizen")
    assert plan.packages == ("central-image", "media-worker-image")
    # The composition root mounts the packaged OS-agent's v2 route, so it runs
    # the netboot tracer; a pull request still needs no base-image rebuild.
    assert plan.suites == ("checks", "e2e", "netboot-e2e")
    assert PullRequestRun(plan).jobs == ("checks", "e2e", "netboot-e2e")
    assert "base-image" in ReleaseRun(plan).jobs


@needs_uvx
@pytest.mark.parametrize("path", ("central/db.py", "Dockerfile", "uv.lock"))
def test_packaged_os_agent_gate_runs_for_central_database_and_image_inputs(scratch, path):
    scratch.commit("fix(central): preserve check-in startup", {path: "changed\n"})
    assert "netboot-e2e" in scratch.plan().suites


@needs_uvx
def test_resident_agent_probe_change_runs_the_built_base_gate(scratch):
    scratch.commit("fix(base): keep OS reports through package failure",
                   {"scripts/os_agent_service_probe.py": "PROBE = 1\n"})
    plan = scratch.plan()
    assert "base-bundle" in plan.packages
    assert "base-image" in plan.suites


@needs_uvx
def test_a_breaking_change_is_capped_to_minor_while_major_version_zero_holds(scratch):
    """pyproject's `major_version_zero = true`: a breaking change bumps the minor within 0.x --
    even on a fix, which would otherwise be a patch."""
    scratch.commit("fix(player)!: the handshake changes", {"player/service.py": "VALUE = 2\n"})
    plan = scratch.plan()
    assert (plan.tag, plan.increment) == ("v0.9.0", "MINOR")


@needs_uvx
def test_a_breaking_change_releases_a_major_without_major_version_zero(tmp_path):
    pyproject = (REPO / "pyproject.toml").read_text().replace("major_version_zero = true",
                                                               "major_version_zero = false")
    repo = Scratch(tmp_path / "repo", tag="v1.2.3", pyproject=pyproject)
    repo.commit("feat(player)!: a new wire format", {"player/service.py": "VALUE = 3\n"})
    plan = repo.plan()
    assert (plan.tag, plan.increment, plan.source) == ("v2.0.0", "MAJOR", "commitizen")


@needs_uvx
def test_a_package_change_with_only_non_bumping_commits_releases_an_automated_patch(scratch):
    scratch.commit("chore(deps): relock", {"uv.lock": "version = 1\n"})
    scratch.commit("ci: faster checks", {".github/workflows/checks.yml": "on: {}\n"})
    scratch.commit("test: one more case", {"tests/test_y.py": "def test_y(): pass\n"})
    plan = scratch.plan()
    assert plan.should_release
    assert (plan.tag, plan.increment) == ("v0.8.1", "PATCH")
    assert plan.source.startswith("automated")
    assert "central-image" in plan.packages


@needs_uvx
@pytest.mark.parametrize("files", [{"docs/guide.md": "# More\n"},
                                   {"tests/test_x.py": "def test_x(): assert 1\n"},
                                   {"README.md": "hello\n", ".claude/notes.md": "n\n"}])
def test_docs_or_tests_alone_release_nothing_whatever_the_commit_type(scratch, files):
    scratch.commit("feat: a documented feature", files)
    plan = scratch.plan()
    assert not plan.should_release
    assert plan.packages == () and plan.tag == ""
    assert ReleaseRun(plan).jobs == ("checks", "e2e")


@needs_uvx
def test_the_first_release_starts_from_zero(tmp_path):
    repo = Scratch(tmp_path / "repo", tag=None)
    repo.commit("feat: the first feature", {"player/service.py": "VALUE = 4\n"})
    plan = repo.plan()
    assert plan.since is None and plan.tag == "v0.1.0"


@needs_uvx
def test_the_real_history_releases_0_9_0(scratch):
    """PR #28 landed as a non-conventional squash (d7b19bd) after v0.8.0, and commitizen found
    nothing to bump, so 106 shipped files went out with no release. Under the rule those
    changed packages release on their own (an automated patch); with this branch's feat
    commit the release is v0.9.0."""
    scratch.commit(PR28_SQUASH, {"appliance/netboot_init.py": "STAGE = 1\n",
                                 "uplink/locate.py": "ORIGIN = 1\n"})
    assert scratch.plan().tag == "v0.8.1"
    scratch.commit("ci(release): a non-conventional commit can no longer skip a release "
                   "silently", {".github/workflows/pipeline.yml": "on: {}\n"})
    scratch.commit("feat(netboot): every boot stage reaches the configured Central (0014, #28)")
    plan = scratch.plan()
    assert (plan.tag, plan.increment, plan.source) == ("v0.9.0", "MINOR", "commitizen")
    assert {"base-bundle", "bootstrapper-deb", "player-deb"} <= set(plan.packages)
    assert "release-assets" not in plan.packages          # pipeline.yml is not a release input
    assert {"base-image", "netboot-e2e"} <= set(plan.suites)


@needs_uvx
def test_a_pull_request_refuses_a_non_conventional_commit_and_names_it(scratch):
    base = scratch.git("rev-parse", "HEAD")
    bad = scratch.commit(PR28_SQUASH, {"player/service.py": "VALUE = 5\n"})
    head = scratch.commit("fix(player): a conventional one")
    with pytest.raises(PlanError) as refused:
        check_commits(scratch.root, Commitizen(scratch.root), base, head)
    assert bad in str(refused.value) and PR28_SQUASH in str(refused.value)
    assert "fix(player)" not in str(refused.value)


@needs_uvx
def test_a_pull_request_allows_merge_commits(scratch):
    base = scratch.git("rev-parse", "HEAD")
    scratch.git("checkout", "-q", "-b", "topic")
    scratch.commit("feat(player): a topic", {"player/service.py": "VALUE = 6\n"})
    scratch.git("checkout", "-q", "main")
    scratch.commit("docs: meanwhile", {"docs/guide.md": "# Meanwhile\n"})
    scratch.git("checkout", "-q", "topic")
    scratch.git("merge", "-q", "--no-edit", "main")          # "Merge branch 'main' into topic"
    head = scratch.git("rev-parse", "HEAD")
    check_commits(scratch.root, Commitizen(scratch.root), base, head)


@needs_uvx
def test_pr_mode_plans_the_merge_ref_and_reports_what_merging_releases(scratch, tmp_path,
                                                                       monkeypatch, capsys):
    base = scratch.git("rev-parse", "HEAD")
    scratch.git("checkout", "-q", "-b", "topic")
    head = scratch.commit("feat(player): a topic", {"player/service.py": "VALUE = 7\n"})
    # GitHub's refs/pull/N/merge: base merged with head, checked out detached.
    scratch.git("checkout", "-q", "--detach", "main")
    scratch.git("merge", "-q", "--no-ff", "-m", f"Merge {head} into {base}", "topic")
    output, summary = tmp_path / "output", tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    values = scratch.main("pr", "--base", base, "--head", head, output=output)
    # The report: what merging releases.
    assert ("merging releases v0.9.0 (packages: central-image, media-worker-image, player-deb, "
            "player-payload, bootstrapper-deb, base-bundle, increment: MINOR (commitizen), from commits: 2 "
            "since v0.8.0)") in summary.read_text()
    assert "::notice title=Release plan::merging releases v0.9.0" in capsys.readouterr().out
    # The action: this run tests the merge ref and releases nothing.
    assert values["revision"] == scratch.git("rev-parse", "HEAD")
    assert (values["should_release"], values["tag"], values["version"], values["since"]) == (
        "false", "", "", "")
    assert json.loads(values["jobs"]) == ["base-image", "checks", "e2e", "netboot-e2e"]
    assert "Jobs: base-image, checks, e2e, netboot-e2e\n" in summary.read_text()


@needs_uvx
def test_pr_mode_fails_on_a_non_conventional_commit(scratch, capsys):
    base = scratch.git("rev-parse", "HEAD")
    head = scratch.commit("tidy things up", {"player/service.py": "VALUE = 8\n"})
    assert release_plan.main(["--repo", str(scratch.root), "pr", "--base", base,
                              "--head", head]) == 1
    assert f"::error title=release plan::commit \"{head}\": \"tidy things up\"" \
        in capsys.readouterr().out


@needs_uvx
def test_push_mode_reports_releasing_nothing(scratch, tmp_path, monkeypatch):
    scratch.commit("docs: clarify", {"docs/guide.md": "# Clear\n"})
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert release_plan.main(["--repo", str(scratch.root), "push"]) == 0
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["should_release"] == "false" and values["tag"] == ""
    assert json.loads(values["jobs"]) == ["checks", "e2e"]


# --- only a push to main releases -------------------------------------------------------------

EVERY_SUITE = tuple(sorted(suite.job for suite in SUITES))


def test_a_pull_request_run_lists_no_publishing_job_whatever_its_plan_forecasts():
    """The type, not a flag: a PullRequestRun of a plan that releases (every package changed,
    every suite due, a version computed) still starts only that plan's test suites."""
    plan = Plan("0" * 40, "v0.8.0", tuple(package.name for package in PACKAGES), EVERY_SUITE,
                version="0.9.0", increment="MINOR", source="commitizen")
    assert plan.should_release and plan.tag == "v0.9.0"
    run = PullRequestRun(plan)
    assert run.jobs == EVERY_SUITE and not run.releases
    assert not set(PUBLISH_JOBS) & set(run.jobs)
    assert {key: release_plan.outputs(run)[key] for key in ("tag", "version", "since")} == {
        "tag": "", "version": "", "since": ""}
    assert set(RELEASE_JOBS) <= set(ReleaseRun(plan).jobs)


def test_no_suite_publishes_and_no_plan_names_a_job_that_is_not_a_suite():
    """So a PullRequestRun, which lists its plan's suites, cannot list a publishing job."""
    for job in PUBLISH_JOBS:
        with pytest.raises(ValueError):
            Suite(job, always=True)
        with pytest.raises(ValueError):
            Plan("0" * 40, None, (), ("checks", job))
    assert not set(PUBLISH_JOBS) & set(EVERY_SUITE)
    assert set(RELEASE_JOBS) - set(PUBLISH_JOBS) == {"base-image"}


@needs_uvx
def test_pr_mode_never_lists_a_publishing_job_even_when_merging_releases(scratch, tmp_path,
                                                                         monkeypatch):
    base = scratch.git("rev-parse", "HEAD")
    head = scratch.commit("feat(player): ships", {"player/service.py": "VALUE = 10\n",
                                                  "central/app.py": "APP = 10\n"})
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert scratch.plan().tag == "v0.9.0"                       # merging would release
    values = scratch.main("pr", "--base", base, "--head", head, output=output)
    jobs = json.loads(values["jobs"])
    assert not {"seal", "service-base", "images"} & set(jobs), jobs
    assert values["should_release"] == "false" and values["tag"] == "" and values["since"] == ""


@needs_uvx
def test_push_mode_lists_every_release_job_when_a_release_is_due(scratch, tmp_path, monkeypatch):
    scratch.commit("fix(player): ships", {"player/service.py": "VALUE = 11\n"})
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    values = scratch.main("push", output=output)
    assert {"seal", "service-base", "images", "base-image"} <= set(json.loads(values["jobs"]))
    # `since` is what the seal's compare-and-swap requires to still be the highest published.
    assert (values["should_release"], values["tag"], values["version"], values["since"]) == (
        "true", "v0.8.1", "0.8.1", "v0.8.0")


@needs_uvx
def test_push_refuses_at_once_a_next_version_whose_tag_already_exists(scratch, tmp_path,
                                                                       monkeypatch, capsys):
    """A tag off main's history (here v0.8.1 on a side branch) is invisible to commitizen, which
    numbers from the last tag it can reach: the push would build for an hour and then collide
    at publish, on every push. The plan refuses before any build."""
    scratch.git("checkout", "-q", "-b", "side", "v0.8.0")
    side = scratch.commit("fix: elsewhere", {"central/app.py": "APP = 12\n"})
    scratch.git("tag", "v0.8.1")
    scratch.git("checkout", "-q", "main")
    scratch.commit("fix(player): ships", {"player/service.py": "VALUE = 12\n"})
    plan = scratch.plan()
    assert plan.tag == "v0.8.1"                                  # the collision is real
    with pytest.raises(PlanError, match=f"v0.8.1, but that tag already exists \\(at {side}\\)"):
        refuse_a_taken_tag(scratch.root, plan)
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert release_plan.main(["--repo", str(scratch.root), "push"]) == 1
    assert "::error title=release plan::this push would release v0.8.1" in capsys.readouterr().out
    assert not output.exists()                                   # no job is started
    scratch.commit("feat(player): bumps past it", {"player/service.py": "VALUE = 13\n"})
    assert refuse_a_taken_tag(scratch.root, scratch.plan()).tag == "v0.9.0"


# --- only the build tables of pyproject.toml, and no pipeline.yml, are release inputs ----------

RUFF = "line-length = 100"


@needs_uvx
@pytest.mark.parametrize("edit", [
    lambda text: text.replace(RUFF, "line-length = 101"),
    lambda text: text.replace("[tool.ruff]", "# a comment\n[tool.ruff]  # and another"),
    lambda text: text.replace('addopts = "-ra"', 'addopts = "-ra -q"'),
], ids=["ruff-setting", "comments", "pytest-setting"])
def test_a_tooling_or_comment_edit_to_pyproject_releases_nothing(scratch, edit):
    text = (scratch.root / "pyproject.toml").read_text()
    assert RUFF in text
    scratch.commit("style: tooling only", {"pyproject.toml": edit(text)})
    plan = scratch.plan()
    assert not plan.should_release and plan.packages == ()
    assert changed_paths(scratch.root, plan.since, plan.revision) == []


ON_THE_LOCK = tuple(package.name for package in PACKAGES if package.claims("uv.lock"))


@needs_uvx
@pytest.mark.parametrize("files", [
    lambda text: {"pyproject.toml": text.replace('"httpx==0.28.1"', '"httpx==0.28.2"')},
    lambda text: {"uv.lock": "version = 2\n"},
    lambda text: {"pyproject.toml": text + "\n[tool.uv]\ncompile-bytecode = true\n"},
], ids=["project-dependency", "lock", "tool-uv"])
def test_a_dependency_change_releases_every_package_on_the_lock(scratch, files):
    text = (scratch.root / "pyproject.toml").read_text()
    assert '"httpx==0.28.1"' in text
    scratch.commit("chore(deps): bump", files(text))
    plan = scratch.plan()
    assert plan.packages == ON_THE_LOCK
    assert (plan.tag, plan.source) == ("v0.8.1", "automated: shipped packages changed and no "
                                                 "commit bumps")


def test_the_lock_is_read_by_every_package_but_the_release_packaging():
    assert set(ON_THE_LOCK) == {package.name for package in PACKAGES} - {"release-assets"}
    assert all(package.claims("pyproject.toml") for package in PACKAGES
               if package.name in ON_THE_LOCK)


@needs_uvx
def test_a_pipeline_edit_releases_nothing(scratch):
    scratch.commit("ci: build differently", {".github/workflows/pipeline.yml": "on: {}\n"})
    assert not scratch.plan().should_release


def test_pyproject_counts_only_through_its_build_tables():
    text = (REPO / "pyproject.toml").read_text()
    digest = pyproject_digest(text)
    assert digest and pyproject_digest(None) is None
    for same in (text.replace(RUFF, "line-length = 120"),
                 text.replace("[tool.pytest.ini_options]", "# tests\n[tool.pytest.ini_options]"),
                 text.replace("major_version_zero = true", "major_version_zero = false"),
                 text.replace('name = "Kernel is vocabulary only"', 'name = "Kernel"'),
                 text.replace('"pytest==8.3.5"', '"pytest==8.3.6"')):
        assert same != text and pyproject_digest(same) == digest
    for different in (text.replace('"fastapi==0.115.12"', '"fastapi==0.115.13"'),
                      text.replace('version = "0.1.0"', 'version = "0.1.1"'),
                      text.replace('requires-python = ">=3.12,<3.14"',
                                   'requires-python = ">=3.12"'),
                      text.replace('"hatchling==1.27.0"', '"hatchling==1.28.0"'),
                      text.replace('packages = ["central", "contracts", "media", "player", '
                                   '"uplink"]', 'packages = ["central"]'),
                      text + "\n[tool.uv]\npackage = false\n"):
        assert different != text and pyproject_digest(different) != digest
    with pytest.raises(PlanError, match="not valid TOML"):
        pyproject_digest("[project\n")


def test_how_a_release_is_written_is_unshipped_and_what_it_contains_is_release_assets():
    """pipeline.yml and the seal (how a release is written) cut no version; the declaration of
    what a release contains and the packager that writes it do."""
    for path in (".github/workflows/pipeline.yml", "scripts/release_seal.py",
                 "scripts/release_plan.py"):
        assert claimed_by(path) == () and any(matches(pattern, path) for pattern in NOT_SHIPPED)
    assert _package("release-assets").paths == ("scripts/package_release_artifacts.py",
                                                "contracts/release.py",
                                                "contracts/player_payload.py")
    assert "release-assets" in claimed_by("contracts/release.py")


# --- the gate ----------------------------------------------------------------------------------

def _needs(jobs, **results):
    needs = {"plan": {"result": results.pop("plan", "success"),
                      "outputs": {"jobs": json.dumps(jobs)}}}
    needs.update({job: {"result": result, "outputs": {}} for job, result in results.items()})
    return needs


def test_the_gate_passes_when_every_listed_job_succeeded_and_the_rest_skipped():
    assert gate(_needs(["checks", "e2e"], checks="success", e2e="success", tested="success",
                       **{"base-image": "skipped", "netboot-e2e": "skipped",
                          "seal": "skipped"})) == []


@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled"])
def test_the_gate_fails_when_a_listed_job_did_not_succeed(result):
    assert gate(_needs(["checks", "base-image"], checks="success", tested="success",
                       **{"base-image": result})) == [f"base-image: {result}, but this run "
                                                      "requires it"]


@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_the_gate_fails_when_an_unlisted_job_failed_or_was_cancelled(result):
    assert gate(_needs(["checks"], checks="success", tested="success", seal=result)) == [
        f"seal: {result}"]


def test_the_gate_fails_when_the_plan_or_the_barrier_failed():
    assert gate(_needs([], plan="failure", tested="failure", checks="skipped")) == [
        "plan: failure", "checks: skipped, but this run requires it",
        "tested: failure, but this run requires it"]
    assert gate(_needs(["checks"], checks="success", tested="skipped")) == [
        "tested: skipped, but this run requires it"]


@pytest.mark.parametrize("outputs", [{}, {"jobs": ""}, {"jobs": "[]"}, {"jobs": "checks"},
                                     {"jobs": '{"checks": 1}'}, {"jobs": "[1]"}])
def test_the_gate_fails_when_the_plan_succeeded_without_a_jobs_list(outputs):
    """Else a plan that wrote nothing would require only `tested`, and a run that ran no test
    at all could pass."""
    needs = {"plan": {"result": "success", "outputs": outputs},
             **{job: {"result": "success"} for job in ALWAYS_JOBS}}
    problems = gate(needs)
    assert len(problems) == 1 and problems[0].startswith("plan: succeeded but its `jobs` output")


def test_the_gate_always_requires_the_suites_that_always_run():
    assert {"checks", "e2e", "tested"} == set(ALWAYS_JOBS)
    assert gate(_needs(["base-image"], tested="success", checks="skipped", e2e="success",
                       **{"base-image": "success"})) == [
        "checks: skipped, but this run requires it"]


def test_the_gate_command_reads_needs(monkeypatch, capsys):
    monkeypatch.setenv("NEEDS", json.dumps(_needs(["checks"], checks="failure")))
    assert release_plan.main(["gate"]) == 1
    assert "::error title=gate::checks: failure, but this run requires it" in \
        capsys.readouterr().out


# --- the package manifest against the real tree ------------------------------------------------

def _tracked() -> list[str]:
    """Every file the next commit would hold: tracked or new, not ignored, not deleted."""
    listed = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others",
                             "--exclude-standard"], check=True, capture_output=True,
                            text=True).stdout.strip("\0").split("\0")
    return sorted({path for path in listed if (REPO / path).is_file()})


def _not_shipped(path: str) -> bool:
    return any(matches(pattern, path) for pattern in NOT_SHIPPED)


def test_patterns_anchor_at_the_root_and_star_stays_in_one_directory():
    assert matches("appliance/*.py", "appliance/provision.py")
    assert not matches("appliance/*.py", "appliance/sub/provision.py")
    assert not matches("appliance/*.py", "x/appliance/provision.py")
    assert matches("central/**", "central/console/src/App.tsx")
    assert not matches("central/**", "centralx/app.py")
    assert matches("*.md", "README.md") and not matches("*.md", "docs/README.md")


def test_every_tracked_file_ships_in_a_package_or_is_declared_unshipped():
    """A new top-level directory or script fails here until someone decides whether it ships:
    an unclassified path can never silently change nothing."""
    unclassified = [path for path in _tracked() if not claimed_by(path) and not _not_shipped(path)]
    assert unclassified == [], "classify in scripts/release_plan.py (PACKAGES or NOT_SHIPPED)"


def test_first_party_code_and_every_dockerfile_ship_in_a_package():
    """Test fixtures aside (tests/native/Dockerfile is a local integration fixture)."""
    sources = ("central/", "player/", "uplink/", "contracts/", "appliance/", "media/")
    unclaimed = [path for path in _tracked()
                 if (path.startswith(sources) or Path(path).name.startswith("Dockerfile"))
                 and not path.startswith("tests/") and not claimed_by(path)]
    assert unclaimed == []


def test_no_path_is_both_shipped_and_declared_unshipped():
    assert [path for path in _tracked() if claimed_by(path) and _not_shipped(path)] == []


@pytest.mark.parametrize("policy, packages", [("initrd", ["base-bundle"]),
                                              ("bootstrapper", ["bootstrapper-deb",
                                                                "base-bundle"]),
                                              ("player", ["player-deb"])])
def test_every_computed_closure_file_is_claimed_by_its_package(policy, packages):
    files = [path.as_posix() for path in closure_for(POLICIES[policy]).files]
    assert files
    for name in packages:
        package = next(package for package in PACKAGES if package.name == name)
        assert [path for path in files if not package.claims(path)] == [], name


def _covered(source: str, tracked: list[str]) -> list[str]:
    """The tracked files a COPY/ADD source (a manifest path) brings into an image."""
    return [path for path in tracked if path == source or matches(f"{source}/**", path)
            or matches(source, path)]


def _image_conflicts(dockerfile_text: str, target: str, package) -> list[str]:
    """Each tracked file `target` copies that `package` does not claim or that is unshipped."""
    stages, tracked = dockerfile_stages(dockerfile_text), _tracked()
    seen, todo = set(), [target]
    while todo:
        if (name := todo.pop()) not in seen:
            seen.add(name)
            todo += stages[name].after
    problems = []
    for source in sorted({path for name in seen for path in stages[name].reads}):
        files = _covered(source, tracked)
        problems += [f"{target}: {source} copies nothing tracked"] if not files else []
        problems += [f"{target}: {path} ({'unshipped' if _not_shipped(path) else 'unclaimed'})"
                     for path in files if _not_shipped(path) or not package.claims(path)]
    return problems


def test_every_file_a_service_image_copies_is_claimed_by_it_and_ships():
    """COPY/ADD sources through the target's whole stage closure (stage copies followed, not
    read); the media-test stage, which copies tests/, ships in no image."""
    images = [package for package in PACKAGES if package.target]
    assert {package.target for package in images} == {"central", "media-worker"}
    text = (REPO / DOCKERFILE).read_text()
    for package in images:
        assert _image_conflicts(text, package.target, package) == [], package.name


def test_a_new_copy_of_an_unshipped_file_into_an_image_fails_the_manifest():
    """The probe that once left every test green: the source stage copies a NOT_SHIPPED file.
    The image now claims it by construction, which the manifest tests refuse."""
    text = (REPO / DOCKERFILE).read_text().replace(
        "COPY --link player /app/player\n",
        "COPY --link player /app/player\nCOPY scripts/configure.py /app/\n")
    central = release_plan.Package("central-image", "", image_inputs("central", text), "central")
    assert central.claims("scripts/configure.py")
    assert _image_conflicts(text, "central", central) == [
        "central: scripts/configure.py (unshipped)"]


def test_every_other_dockerfile_copies_only_what_its_package_ships():
    """Dockerfiles beyond the images' (test fixtures aside): whatever tracked file any stage
    copies ships in every package that claims the Dockerfile."""
    tracked = _tracked()
    for dockerfile in tracked:
        if not Path(dockerfile).name.startswith("Dockerfile") or dockerfile == DOCKERFILE \
                or dockerfile.startswith("tests/"):
            continue
        owners = [package for package in PACKAGES if package.claims(dockerfile)]
        assert owners, dockerfile
        stages = dockerfile_stages((REPO / dockerfile).read_text())
        for source in {path for stage in stages.values() for path in stage.reads}:
            for path in _covered(source, tracked):
                assert not _not_shipped(path), (dockerfile, path)
                assert all(package.claims(path) for package in owners), (dockerfile, path)


def test_the_image_inputs_follow_the_target_stage_closure():
    central, worker = image_inputs("central"), image_inputs("media-worker")
    for inputs in (central, worker):
        assert {"Dockerfile", ".dockerignore", "pyproject.toml", "uv.lock", "central/**",
                "central/console/package.json", "contracts/**", "media/**",
                "player/**"} <= set(inputs)
        assert not any(matches(pattern, "tests/test_prepare.py") for pattern in inputs)
    assert "docker-entrypoint.sh" in worker and "docker-entrypoint.sh" not in central


DOCKERFILE_FORMS = """\
# syntax=docker/dockerfile:1
ARG BASE=builder
FROM --platform=linux/amd64 python AS builder
COPY --chmod=0755 tool.sh /tool
FROM node AS unrelated
COPY tests ./tests
FROM ${BASE} AS mid
RUN --mount=type=bind,source=vendor,target=/v --mount=type=cache,target=/c true
FROM scratch AS Other
COPY ["with space/file", "/x"]
FROM mid AS final
COPY --from=other /x /x
COPY --from=ghcr.io/x/y:1 /bin /bin
ADD https://example.invalid/a.tgz /a
COPY ./app/ \\
     conf.toml /app/
"""


def test_the_dockerfile_reader_follows_every_form_the_manifest_depends_on():
    inputs = image_inputs("final", DOCKERFILE_FORMS)
    for path in ("tool.sh", "vendor/lib.py", "with space/file", "app/main.py", "conf.toml"):
        assert any(matches(pattern, path) for pattern in inputs), path
    assert not any(matches(pattern, "tests/x.py") for pattern in inputs)
    assert not any("example.invalid" in pattern or "ghcr.io" in pattern for pattern in inputs)
    assert image_inputs("final", "FROM python AS final\nCOPY . /app\n")[-1] == "**"


@pytest.mark.parametrize("text", [
    "FROM python AS final\nCOPY $SRC /app\n",
    "FROM python AS final\nCOPY src?.py /app\n",
    "FROM python AS final\nRUN <<EOF\nCOPY x /y\nEOF\n",
    "# escape=`\nFROM python AS final\n",
    "FROM python AS final\nCOPY lonely\n",
    "FROM python AS final\nCOPY a \\\n",
    "FROM python AS other\n",
], ids=["variable", "glob-class", "heredoc", "escape", "no-destination", "open-continuation",
        "no-target"])
def test_the_dockerfile_reader_refuses_what_it_cannot_follow(text):
    with pytest.raises(DockerfileError):
        image_inputs("final", text)


def _scripts_named(text: str) -> set[str]:
    """The scripts a workflow's non-comment lines name."""
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    found = set()
    for name, suffix in re.findall(r"scripts[./]([A-Za-z_]+)(\.py|\.sh)?", text):
        path = f"scripts/{name}{suffix or '.py'}"
        if (REPO / path).is_file():
            found.add(path)
    return found


def _with_imports(scripts: set[str]) -> set[str]:
    """`scripts` and every first-party script they import (transitively); a shell script's
    siblings are those it names."""
    seen, todo = set(), list(scripts)
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        text = (REPO / path).read_text()
        if path.endswith(".sh"):
            todo += [f"scripts/{name}" for name in re.findall(r"\b([a-z_]+\.py)\b", text)
                     if (REPO / "scripts" / name).is_file()]
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module == "scripts":
                todo += [f"scripts/{alias.name}.py" for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("scripts."):
                todo.append(f"scripts/{node.module.split('.', 1)[1]}.py")
            elif isinstance(node, ast.Import):
                todo += [f"scripts/{alias.name.split('.', 1)[1]}.py" for alias in node.names
                         if alias.name.startswith("scripts.")]
    return seen


def _package(name: str):
    return next(package for package in PACKAGES if package.name == name)


def _job(workflow: str, job: str) -> str:
    text = (WORKFLOWS / workflow).read_text()
    return re.split(r"^  [\w-]+:\n", text.split(f"\n  {job}:\n", 1)[1], maxsplit=1,
                    flags=re.MULTILINE)[0]


@pytest.mark.parametrize("builder, package", [("scripts/build_player_deb.py", "player-deb"),
                                              ("scripts/build_bootstrapper_deb.py",
                                               "bootstrapper-deb")])
def test_each_deb_builder_and_what_it_imports_is_claimed_by_its_deb(builder, package):
    assert [path for path in _with_imports({builder}) if not _package(package).claims(path)] == []


def test_every_script_a_release_build_runs_is_claimed_by_what_it_builds():
    base = _with_imports(_scripts_named((WORKFLOWS / "base-image.yml").read_text()))
    assert "scripts/build_netboot_bundle.sh" in base and "scripts/eeprom_update.py" in base
    assert [path for path in base if not (_package("base-bundle").claims(path)
                                         or _package("player-payload").claims(path))] == []
    # The seal and the plan it imports decide whether and how a release is written; they shape
    # no artefact byte. The packager does, and ships as release-assets.
    seal = _with_imports(_scripts_named(_job("pipeline.yml", "seal")))
    assert seal == {"scripts/release_seal.py", "scripts/package_release_artifacts.py",
                    "scripts/release_plan.py"}
    assert _package("release-assets").claims("scripts/package_release_artifacts.py")
    service = _with_imports(_scripts_named((WORKFLOWS / "service-base.yml").read_text()))
    assert service and all(_package("media-worker-image").claims(path) for path in service)


def test_base_cache_and_content_check_include_base_owned_player_contract():
    workflow = (WORKFLOWS / "base-image.yml").read_text()
    key = next(line for line in workflow.splitlines() if "key: squashfs-" in line)
    for path in ("appliance/systemd/photo-wall-os-agent.service",
                 "appliance/systemd/player.service",
                 "appliance/systemd/weston.service",
                 "appliance/systemd/weston.ini", "player/output_discovery.py"):
        assert path in key
    for path in ("$bootstrapper_dir/os-agent.py", "$bootstrapper_dir/player-launch.py",
                 "$bootstrapper_dir/base-abi.txt", "$bootstrapper_dir/weston.ini"):
        assert path in workflow
    assert "forbid_substring 'squashfs-root/usr/lib/photo-wall-player/'" in workflow


# --- pipeline.yml wires the rule ---------------------------------------------------------------

def _pipeline_jobs() -> dict[str, str]:
    text = (WORKFLOWS / "pipeline.yml").read_text().split("\njobs:\n", 1)[1]
    names = re.findall(r"^  ([\w-]+):\n", text, flags=re.MULTILINE)
    return {name: _job("pipeline.yml", name) for name in names}


def _needs_of(body: str) -> list[str]:
    needs = re.search(r"^    needs: (.+)$", body, flags=re.MULTILINE)[1]
    return [name.strip() for name in needs.strip("[]").split(",")]


def test_every_conditional_job_is_one_the_plan_lists_and_reads_the_plan():
    jobs = _pipeline_jobs()
    plannable = {suite.job for suite in SUITES} | set(RELEASE_JOBS)
    assert set(jobs) == plannable | {"plan", "tested", "gate"}
    for name in plannable:
        assert f"contains(fromJSON(needs.plan.outputs.jobs), '{name}')" in jobs[name], name


def test_the_barrier_needs_every_test_and_every_release_job_needs_the_barrier():
    jobs = _pipeline_jobs()
    assert set(_needs_of(jobs["tested"])) == {"plan"} | {suite.job for suite in SUITES}
    for name in ("service-base", "seal"):
        assert "tested" in _needs_of(jobs[name])
        assert "!cancelled() && needs.tested.result == 'success'" in jobs[name]
    assert "service-base" in _needs_of(jobs["images"])
    assert "!cancelled() && needs.service-base.result == 'success'" in jobs["images"]
    # The seal runs only after the images are pushed: it promotes their digests.
    assert "images" in _needs_of(jobs["seal"])
    assert "&& needs.images.result == 'success'" in _condition(jobs["seal"])


def test_the_gate_is_the_one_stable_check_over_every_job():
    jobs = _pipeline_jobs()
    assert "    name: gate\n" in jobs["gate"]
    assert "    if: always()\n" in jobs["gate"] and "    if: always()\n" in jobs["tested"]
    assert set(_needs_of(jobs["gate"])) == set(jobs) - {"gate"}
    for name in ("tested", "gate"):
        assert "run: python3 scripts/release_plan.py gate" in jobs[name]
        assert "NEEDS: ${{ toJSON(needs) }}" in jobs[name]


# The event guard every job that writes outside the run carries in its own `if:`, so that
# pipeline.yml on its own keeps a release off a pull request, whatever the plan lists.
RELEASE_EVENT = "(github.event_name == 'push' && github.ref == 'refs/heads/main')"


def _workflow_jobs(workflow: str) -> dict[str, str]:
    text = (WORKFLOWS / workflow).read_text().split("\njobs:\n", 1)[1]
    names = re.findall(r"^  ([\w-]+):\n", text, flags=re.MULTILINE)
    return {name: _job(workflow, name) for name in names}


def _permissions(block: str, indent: str) -> dict[str, str]:
    """The `permissions:` mapping at `indent` in `block` (a job body or a whole workflow)."""
    found = re.search(rf"^{indent}permissions:\n((?:{indent}  .+\n)+)", block, re.MULTILINE)
    return dict(re.findall(r"^\s+([\w-]+): (\w+)$", found[1], re.MULTILINE)) if found else {}


def _condition(body: str) -> str:
    """A job's `if:`, a folded (`>-`) block joined as GitHub joins it."""
    found = re.search(r"^    if: (.*)\n((?:      .*\n)*)", body, re.MULTILINE)
    if found is None:
        return ""
    head = "" if found[1] in (">-", ">") else found[1]
    return " ".join([head, *(line.strip() for line in found[2].splitlines())]).strip()


def _writes(permissions: dict[str, str]) -> set[str]:
    return {scope for scope, level in permissions.items() if level == "write"}


def test_every_job_that_can_publish_runs_only_on_a_push_to_main():
    """Every pipeline job holding a write scope, and every publishing job, requires the event in
    its own `if:`. The one exception is proven, not named: a test job may hold `packages: write`
    only to call a workflow whose every writing job is the service-base.yml media OS cache
    (same-repository pull requests publish that content-addressed dependency by design)."""
    pipeline = (WORKFLOWS / "pipeline.yml").read_text()
    assert _permissions(pipeline, "") == {"contents": "read"}
    jobs = _workflow_jobs("pipeline.yml")
    guarded = {name for name, body in jobs.items() if RELEASE_EVENT in _condition(body)}
    assert guarded == set(PUBLISH_JOBS)
    for name, body in jobs.items():
        writes = _writes(_permissions(body, "    "))
        if name in guarded or not writes:
            continue
        assert writes == {"packages"}, (name, writes)
        called = re.search(r"^    uses: \./\.github/workflows/([\w-]+\.yml)$", body,
                           flags=re.MULTILINE)
        assert called and called[1] != "service-base.yml", name
        assert not _writes(_permissions((WORKFLOWS / called[1]).read_text(), "")), called[1]
        for nested, nested_body in _workflow_jobs(called[1]).items():
            if _writes(_permissions(nested_body, "    ")):
                assert "    uses: ./.github/workflows/service-base.yml\n" in nested_body, (
                    called[1], nested)


def _code(text: str) -> str:
    """A workflow's lines that are not comments."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


# What makes a job a version writer: the plan's version (its tag, or the tag it follows), an
# image tag that is not `:sha-*`, or a GitHub CLI / API call.
VERSION_WRITES = ("needs.plan.outputs.tag", "needs.plan.outputs.since", "env.TAG", "$TAG",
                  "imagetools", "gh release", "gh api", "claim")


def test_only_the_seal_writes_a_version():
    """The seal is the only job, in any workflow, with `contents: write`, and the only one that
    reads the plan's version: no other job can create a tag or a release, or push an image under
    a version tag. The images job pushes each image under `:sha-<revision>` alone."""
    jobs = _pipeline_jobs()
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        scopes = [_permissions(workflow.read_text(), "")] + [
            _permissions(body, "    ") for name, body in _workflow_jobs(workflow.name).items()
            if (workflow.name, name) != ("pipeline.yml", "seal")]
        assert all("contents" not in _writes(scope) for scope in scopes), workflow.name
    assert _writes(_permissions(jobs["seal"], "    ")) == {"contents", "packages"}
    for name, body in jobs.items():
        if name != "seal":
            assert not [word for word in VERSION_WRITES if word in _code(body)], name
    images = _code(jobs["images"])
    tags = re.findall(r"^\s+tags: (.+)$", images, flags=re.MULTILINE)
    assert tags == [f"ghcr.io/${{{{ github.repository }}}}/{name}:sha-"
                    "${{ needs.plan.outputs.revision }}" for name in IMAGES]
    assert images.count("push: true") == len(IMAGES)


def test_the_images_job_hands_the_seal_exactly_the_digests_it_pushed():
    """Each declared image: built from its Dockerfile target, output as `<repository>@<digest>`
    of that very step, and passed to the seal under its name."""
    jobs = _pipeline_jobs()
    images, seal = jobs["images"], _code(jobs["seal"])
    assert {package.target for package in PACKAGES if package.target} == set(IMAGES)
    for name in IMAGES:
        step = images.split(f"        id: {name}\n", 1)[1].split("      - ", 1)[0]
        assert f"          target: {name}\n" in step, name
        repository = f"ghcr.io/${{{{ github.repository }}}}/{name}"
        assert f"          tags: {repository}:sha-" in step, name
        output = f"      {name}: {repository}@${{{{ steps.{name}.outputs.digest }}}}\n"
        assert output in images, name
        variable = name.upper().replace("-", "_")
        assert f"{variable}: ${{{{ needs.images.outputs.{name} }}}}" in seal, name
        assert f'--image "{name}=${variable}"' in seal, name


def test_the_seal_is_one_script_given_the_plan_and_the_images():
    """Every write is scripts/release_seal.py's (claim, package, verify, promote, stage,
    publish): the job itself runs no other command that reaches GitHub or the registry."""
    seal = _code(_pipeline_jobs()["seal"])
    runs = re.findall(r"^        run: (.*)$", seal, flags=re.MULTILINE)
    assert len(runs) == 2 and "docker login ghcr.io" in runs[0] and runs[1] == "|"
    for binding in ("GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}",
                    "TAG: ${{ needs.plan.outputs.tag }}",
                    "SINCE: ${{ needs.plan.outputs.since }}",
                    "REVISION: ${{ needs.plan.outputs.revision }}"):
        assert binding in seal, binding
    assert ('python3 -m scripts.release_seal --tag "$TAG" --revision "$REVISION" '
            '--since "$SINCE"') in seal
    for forbidden in ("gh ", "curl", "DELETE", "imagetools", "--clobber"):
        assert forbidden not in seal, forbidden
    plan = _pipeline_jobs()["plan"]
    assert "      since: ${{ steps.plan.outputs.since }}\n" in plan
    # The branch whose tip wins an unpublished version is the one the release guard names.
    assert f"github.ref == 'refs/heads/{RELEASE_BRANCH}'" in RELEASE_EVENT
    assert "GH_TOKEN" not in plan                                # the plan reads no GitHub API


def test_no_workflow_hands_its_secrets_to_a_called_workflow():
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        assert "secrets: inherit" not in workflow.read_text(), workflow.name


def test_the_pipeline_is_the_one_workflow_that_triggers_on_pull_requests_and_main():
    assert not (WORKFLOWS / "release.yml").exists() and not (WORKFLOWS / "pr-title.yml").exists()
    pipeline = (WORKFLOWS / "pipeline.yml").read_text()
    assert "\non:\n  pull_request:\n  push:\n    branches:\n      - main\n\npermissions:" \
        in pipeline
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        if workflow.name != "pipeline.yml":
            triggers = workflow.read_text().split("\non:\n", 1)[1].split("\n\n", 1)[0]
            assert "pull_request" not in triggers and "push:" not in triggers, workflow.name
            assert "paths:" not in triggers, workflow.name
    for name in ("checks.yml", "software-e2e.yml"):
        assert "\non:\n  workflow_call:\n\n" in (WORKFLOWS / name).read_text(), name


def test_nothing_releases_by_hand():
    """No manual release path: the pipeline has no dispatch trigger and the plan no dispatch mode,
    and no workflow a person can dispatch can write a tag or a release (contents: write) or push
    anything but service-base.yml's content-addressed media OS dependency."""
    pipeline = (WORKFLOWS / "pipeline.yml").read_text()
    code = "\n".join(line for line in pipeline.splitlines() if not line.lstrip().startswith("#"))
    assert "workflow_dispatch" not in code and "inputs." not in code
    with pytest.raises(SystemExit):
        release_plan.main(["dispatch", "--tag", "v0.8.0"])
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        text = workflow.read_text()
        if "\n  workflow_dispatch:" not in text.split("\njobs:\n", 1)[0]:
            continue
        scopes = [_permissions(text, "")] + [_permissions(body, "    ")
                                              for body in _workflow_jobs(workflow.name).values()]
        writes = set().union(*(_writes(scope) for scope in scopes))
        assert "contents" not in writes and "gh release" not in text, workflow.name
        assert writes <= ({"packages"} if workflow.name == "service-base.yml" else set()), (
            workflow.name, writes)


# --- the pins live once ------------------------------------------------------------------------

def _pins() -> dict[str, str]:
    return dict(re.findall(r'echo "(python|uv)=([0-9.]+)"', ACTION.read_text()))


def test_python_and_uv_are_pinned_once_in_the_composite_action():
    pins = _pins()
    assert set(pins) == {"python", "uv"}
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        text = workflow.read_text()
        assert not re.search(r"uv==[0-9]", text), workflow.name
        for repeated in ("python-version:", "uses: actions/setup-python", pins["uv"],
                         pins["python"]):
            assert repeated not in text, (workflow.name, repeated)
    dockerfile = (REPO / "Dockerfile").read_text()
    assert f"ghcr.io/astral-sh/uv:{pins['uv']}@sha256:" in dockerfile
    assert f"FROM python:{pins['python']}-slim" in dockerfile


def test_commitizen_is_pinned_once_and_only_the_plan_calls_it():
    needle = "commitizen" + "=="
    pinned = [path for path in _tracked()
              if path != "uv.lock" and needle in (REPO / path).read_text(errors="ignore")]
    assert pinned == ["scripts/release_plan.py"]
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        text = workflow.read_text()
        assert "uvx" not in text and not re.search(r"\bcz (bump|check|version)\b", text)
    assert "commitizen" not in (REPO / "uv.lock").read_text()
