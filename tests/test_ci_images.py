"""Behavioral routing and registry failure boundaries for persistent CI images."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import ci_images, os_base

REPOSITORY = Path(__file__).parents[1]
CONFIG = "sha256:" + "a" * 64
DIGEST = "sha256:" + "b" * 64
TAG = "ghcr.io/example/wall/appliance-base:definition-" + "c" * 64
REF = TAG.split(":")[0] + "@" + DIGEST
BUILDER = "ghcr.io/example/wall/appliance-builder@" + DIGEST


def git(repository, *args):
    return subprocess.check_output(["git", "-C", str(repository), *args]).decode().strip()


@pytest.fixture
def definition_tree(tmp_path):
    for name in {*os_base.DEFINITION_FILES, *ci_images.BUILDER_FILES, "appliance/build.py"}:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY / name, target)
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "--quiet", "-m", "initial definition")
    return tmp_path


def test_application_and_lock_changes_do_not_prepare_os_or_builder(definition_tree):
    before = git(definition_tree, "rev-parse", "HEAD")
    (definition_tree / "player").mkdir()
    (definition_tree / "player/main.py").write_text("new application code\n")
    (definition_tree / "uv.lock").write_text("new application dependency lock\n")
    result = ci_images.plan(definition_tree, before)
    assert result["prepare_builder"] == result["prepare_base"] == "false"


def test_native_definition_changes_prepare_only_base(definition_tree):
    before = git(definition_tree, "rev-parse", "HEAD")
    config = definition_tree / "appliance/os_definition.json"
    config.write_text(config.read_text() + "\n")
    result = ci_images.plan(definition_tree, before)
    assert result["prepare_builder"] == "false"
    assert result["prepare_base"] == "true"


def test_builder_changes_prepare_both_definitions(definition_tree):
    before = git(definition_tree, "rev-parse", "HEAD")
    tools = definition_tree / "appliance/build-tools.txt"
    tools.write_text(tools.read_text() + "# Updated tool definition\n")
    result = ci_images.plan(definition_tree, before)
    assert result["prepare_builder"] == result["prepare_base"] == "true"


def test_explicit_recovery_and_initial_definition_allow_preparation(definition_tree):
    before = git(definition_tree, "rev-parse", "HEAD")
    assert ci_images.plan(definition_tree, before, force=True)["prepare_base"] == "true"
    git(definition_tree, "rm", "scripts/os_base.py")
    git(definition_tree, "commit", "--quiet", "-m", "old pipeline")
    old = git(definition_tree, "rev-parse", "HEAD")
    shutil.copyfile(REPOSITORY / "scripts/os_base.py", definition_tree / "scripts/os_base.py")
    assert ci_images.plan(definition_tree, old)["prepare_base"] == "true"


def test_invalid_or_unavailable_comparison_does_not_authorize_build(definition_tree):
    with pytest.raises(ci_images.ImageError):
        ci_images.plan(definition_tree, "main")
    with pytest.raises(ci_images.CommandError):
        ci_images.plan(definition_tree, "f" * 40)


def test_registry_hit_resolves_manifest_digest(monkeypatch):
    monkeypatch.setattr(ci_images, "run", lambda *a, **kw:
                        f"Name: {TAG}\nMediaType: application/vnd.oci.image.manifest.v1+json\n"
                        f"Digest: {DIGEST}\n".encode())
    assert ci_images.resolve(TAG) == REF


@pytest.mark.parametrize("failure", [
    "ERROR: manifest unknown", "ERROR: manifest_unknown", "ERROR: name_unknown",
    f"ERROR: {TAG}: not found",
])
def test_only_explicit_absence_is_a_missing_artifact(monkeypatch, failure):
    def fail(*args, **kwargs):
        raise ci_images.CommandError(failure)
    monkeypatch.setattr(ci_images, "run", fail)
    assert ci_images.resolve(TAG) is None


@pytest.mark.parametrize("failure", [
    "503 Service Unavailable", "502 Bad Gateway", "429 Too Many Requests",
    "unauthorized: authentication required", "denied: permission_denied",
    "dial tcp: no such host", "tool_timeout", "tool_output_limit", "unexpected EOF",
])
def test_registry_errors_never_authorize_a_new_build(monkeypatch, failure):
    def fail(*args, **kwargs):
        raise ci_images.CommandError(failure)
    monkeypatch.setattr(ci_images, "run", fail)
    with pytest.raises(ci_images.CommandError, match=failure):
        ci_images.select(TAG, allow_build=True)


def test_application_only_missing_artifact_fails_with_recovery_instruction(monkeypatch):
    monkeypatch.setattr(ci_images, "resolve", lambda ref: None)
    with pytest.raises(ci_images.ImageError, match="prepare_base=true"):
        ci_images.select(TAG, allow_build=False)
    assert ci_images.select(TAG, allow_build=True) is None


def test_existing_builder_uses_only_resolved_digest(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ci_images, "resolve", lambda ref: REF)
    monkeypatch.setattr(ci_images, "pull", lambda ref: calls.append(ref) or CONFIG)
    monkeypatch.setattr(ci_images, "run", lambda *a, **k: pytest.fail("must not rebuild"))
    result = ci_images.prepare_builder(tmp_path, TAG, allow_build=True, publish=True)
    assert calls == [REF]
    assert result == {"imageid": CONFIG, "image": REF, "built": "false"}


def test_fork_candidate_builder_does_not_write_registry(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ci_images, "resolve", lambda ref: None)
    monkeypatch.setattr(ci_images, "image_id", lambda ref: CONFIG)
    monkeypatch.setattr(ci_images, "run", lambda cmd, **kw: calls.append(cmd) or b"")
    result = ci_images.prepare_builder(tmp_path, TAG, allow_build=True, publish=False)
    assert [cmd[:6] for cmd in calls] == [
        ["docker", "buildx", "build", "--builder", "default", "--load"]
    ]
    assert result["image"] == CONFIG


def test_base_hit_never_runs_os_preparation_and_checks_builder(monkeypatch, tmp_path):
    output = tmp_path / "base"
    calls = []
    monkeypatch.setattr(ci_images, "resolve", lambda ref: REF)
    monkeypatch.setattr(ci_images, "restore_base", lambda ref, out: calls.append((ref, out)))
    monkeypatch.setattr(ci_images, "run", lambda *a, **k: pytest.fail("no OS preparation"))
    monkeypatch.setattr(os_base, "verify", lambda *args: {"builder_image": BUILDER})
    result = ci_images.prepare_base(REPOSITORY, TAG, output, BUILDER, CONFIG, allow_build=False)
    assert calls == [(REF, output)]
    assert result == {"image": REF, "built": "false"}
    monkeypatch.setattr(os_base, "verify", lambda *args: {"builder_image": CONFIG})
    with pytest.raises(ci_images.ImageError, match="different immutable builder"):
        ci_images.prepare_base(REPOSITORY, TAG, output, BUILDER, CONFIG, allow_build=False)


def test_new_base_uses_networked_preparation_only_when_authorized(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ci_images, "resolve", lambda ref: None)
    monkeypatch.setattr(ci_images, "run", lambda command, **kw: calls.append(command) or b"")
    monkeypatch.setattr(os_base, "verify", lambda *args: {"builder_image": BUILDER})
    result = ci_images.prepare_base(REPOSITORY, TAG, tmp_path / "base", BUILDER, CONFIG,
                                    allow_build=True)
    assert result == {"image": "", "built": "true"}
    assert len(calls) == 1
    assert "scripts.os_base" in calls[0]
    assert calls[0][-2:] == ["--builder-image", BUILDER]
    assert "scripts.build_ci_image" not in calls[0]


def test_corrupt_pulled_base_does_not_fall_back_to_rebuild(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_images, "resolve", lambda ref: REF)
    monkeypatch.setattr(ci_images, "restore_base", lambda *args: None)
    monkeypatch.setattr(ci_images, "run", lambda *a, **k: pytest.fail("no OS preparation"))
    def corrupt(*args):
        raise os_base.BaseError("os_base_archive_hash")
    monkeypatch.setattr(os_base, "verify", corrupt)
    with pytest.raises(os_base.BaseError, match="archive_hash"):
        ci_images.prepare_base(REPOSITORY, TAG, tmp_path / "base", BUILDER, CONFIG,
                               allow_build=True)


def test_existing_definition_cannot_be_republished(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_images, "resolve", lambda ref: REF)
    monkeypatch.setattr(ci_images, "run", lambda *a, **k: pytest.fail("must not overwrite"))
    with pytest.raises(ci_images.ImageError, match="refusing to overwrite"):
        ci_images.publish_base(REPOSITORY, TAG, tmp_path)


def test_changed_definition_parser_does_not_execute_new_contract_on_old_tree(definition_tree):
    recipe = definition_tree / "scripts/os_base.py"
    recipe.write_text("# Old implementation without newly declared input files\n")
    (definition_tree / "appliance/os_definition.json").unlink()
    git(definition_tree, "add", ".")
    git(definition_tree, "commit", "--quiet", "-m", "older schema")
    before = git(definition_tree, "rev-parse", "HEAD")
    for name in ("scripts/os_base.py", "appliance/os_definition.json"):
        shutil.copyfile(REPOSITORY / name, definition_tree / name)
    result = ci_images.plan(definition_tree, before)
    assert result["prepare_base"] == "true"
    assert result["prepare_builder"] == "false"


def test_carrier_restores_exact_digest_and_releases_transport_storage(monkeypatch, tmp_path):
    calls = []
    container = "d" * 64
    monkeypatch.setattr(ci_images, "pull", lambda ref: calls.append(["pull", ref]) or CONFIG)
    def run(command, **kwargs):
        calls.append(command)
        return container.encode() if command[:2] == ["docker", "create"] else b""
    monkeypatch.setattr(ci_images, "run", run)
    ci_images.restore_base(REF, tmp_path / "bundle")
    assert calls == [
        ["pull", REF], ["docker", "create", REF, "/unused"],
        ["docker", "cp", f"{container}:/os-base", str(tmp_path / "bundle")],
        ["docker", "rm", container], ["docker", "image", "rm", REF],
    ]


@pytest.mark.parametrize("name, expected", [
    ("README.md", False), ("AGENTS.md", False), ("CONTRIBUTING.md", False),
    ("docs/runbook.md", False), ("docs/evidence/current.md", False),
    ("docs/diagram.png", True), ("unknown.md", True), ("player/new.py", True),
])
def test_only_committed_documentation_changes_skip_qualification(definition_tree, name, expected):
    before = git(definition_tree, "rev-parse", "HEAD")
    changed = definition_tree / name
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("Changed content\n")
    # A dirty tree is not evidence of which committed changes triggered CI.
    assert ci_images.qualification_required(definition_tree, before) is True
    git(definition_tree, "add", ".")
    git(definition_tree, "commit", "--quiet", "-m", "change")
    assert ci_images.qualification_required(definition_tree, before) is expected
    assert ci_images.plan(definition_tree, before)["qualify"] == str(expected).lower()
    assert ci_images.plan(definition_tree, before, force=True)["qualify"] == "true"


def test_no_diff_and_bootstrap_require_qualification(definition_tree):
    current = git(definition_tree, "rev-parse", "HEAD")
    assert ci_images.qualification_required(definition_tree, current) is True
    assert ci_images.qualification_required(definition_tree, "0" * 40) is True
