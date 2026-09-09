"""Media native definitions are independent of application inputs and fail closed."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts import ci_images, service_base

REPOSITORY = Path(__file__).parents[1]
CONFIG = "sha256:" + "a" * 64
REF = "ghcr.io/example/wall/appliance-media-system@sha256:" + "b" * 64


def git(repository, *args):
    return subprocess.check_output(["git", "-C", str(repository), *args]).decode().strip()


@pytest.fixture
def repository(tmp_path):
    shutil.copyfile(REPOSITORY / "Dockerfile", tmp_path / "Dockerfile")
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "--quiet", "-m", "native definition")
    return tmp_path


def test_application_and_python_dependency_changes_keep_native_definition(repository):
    before = service_base.id(repository, "amd64")
    for name in ("uv.lock", "pyproject.toml", "media.py"):
        (repository / name).write_text("changed application dependency\n")
    dockerfile = repository / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text().replace("uv sync --frozen", "uv sync --locked"))
    assert service_base.id(repository, "amd64") == before
    assert service_base.id(repository, "arm64") != before


def test_native_snapshot_change_invalidates_base(repository):
    before = service_base.id(repository, "arm64")
    dockerfile = repository / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text().replace("20260905", "20260906"))
    assert service_base.id(repository, "arm64") != before


def test_standalone_native_stage_has_no_application_ancestors(repository):
    prefix = service_base._prefix((repository / "Dockerfile").read_bytes()).decode()
    assert prefix.count("\nFROM ") == 2
    assert "FROM python-system AS media-os\n" in prefix
    recipe = (repository / "Dockerfile").read_text()
    assert recipe.count("python:3.12.11-slim-trixie@sha256:") == 1
    assert "FROM python-system AS deps\n" in recipe
    assert " AS media-os\n" in prefix
    assert "COPY " not in prefix and "uv.lock" not in prefix and "pyproject.toml" not in prefix
    assert "apt-get" not in (repository / "Dockerfile").read_text().split(
        service_base.MARKER.decode(), 1)[1]


def test_previous_recipe_is_read_as_data(repository):
    before = git(repository, "rev-parse", "HEAD")
    assert service_base.previous_id(repository, before, "amd64") == service_base.id(
        repository, "amd64")
    (repository / "scripts").mkdir()
    (repository / "scripts/service_base.py").write_text("raise RuntimeError('must not run')\n")
    git(repository, "add", ".")
    git(repository, "commit", "--quiet", "-m", "historical code must not run")
    assert service_base.previous_id(repository, git(repository, "rev-parse", "HEAD"), "amd64")


def test_first_introduction_has_no_previous_identity(repository):
    (repository / "Dockerfile").write_text("FROM scratch\n")
    git(repository, "add", ".")
    git(repository, "commit", "--quiet", "-m", "legacy native installation")
    assert service_base.previous_id(repository, git(repository, "rev-parse", "HEAD"), "arm64") is None
    assert service_base.previous_id(repository, "0" * 40, "arm64") is None


@pytest.mark.parametrize("revision", ["main", "abc", "f" * 40])
def test_invalid_or_missing_comparison_fails(repository, revision):
    with pytest.raises(ci_images.ImageError):
        service_base.previous_id(repository, revision, "amd64")


@pytest.mark.parametrize("recipe", [b"FROM scratch\n", service_base.MARKER * 2])
def test_invalid_current_boundary_is_rejected(repository, recipe):
    (repository / "Dockerfile").write_bytes(recipe)
    with pytest.raises(ci_images.ImageError, match="boundary"):
        service_base.id(repository, "amd64")


def test_unsupported_architecture_is_rejected(repository):
    with pytest.raises(ci_images.ImageError, match="architecture"):
        service_base.id(repository, "x86")


def test_registry_hit_uses_digest_and_exact_architecture_without_build(repository, monkeypatch):
    pulls = []
    monkeypatch.setattr(ci_images, "resolve", lambda ref: REF)
    monkeypatch.setattr(ci_images, "pull", lambda ref, *, platform:
                        pulls.append((ref, platform)) or CONFIG)
    monkeypatch.setattr(service_base, "previous_id", lambda *args: None)
    monkeypatch.setattr(ci_images, "run", lambda *a, **k: pytest.fail("no preparation on hit"))
    result = service_base.prepare(repository, "example/wall", "amd64", "0" * 40,
                                  force=True, publish=True)
    assert result["image"] == REF and result["built"] == "false"
    assert pulls == [(REF, "linux/amd64")]


def test_application_only_missing_base_never_runs_apt(repository, monkeypatch):
    monkeypatch.setattr(ci_images, "resolve", lambda ref: None)
    before = git(repository, "rev-parse", "HEAD")
    with pytest.raises(ci_images.ImageError, match="Application-only"):
        service_base.prepare(repository, "example/wall", "arm64", before, publish=True)


def test_unpublishable_candidate_is_rejected_before_build(repository, monkeypatch):
    monkeypatch.setattr(ci_images, "resolve", lambda ref: None)
    monkeypatch.setattr(ci_images, "run", lambda *a, **k: pytest.fail("no unusable candidate"))
    with pytest.raises(ci_images.ImageError, match="needs a published base"):
        service_base.prepare(repository, "example/wall", "arm64", "0" * 40,
                             require_published=True)


def test_new_definition_smokes_offline_before_publishing(repository, monkeypatch):
    commands = []
    resolutions = iter([None, None, REF])
    monkeypatch.setattr(ci_images, "resolve", lambda ref: next(resolutions))
    monkeypatch.setattr(ci_images, "run", lambda command, **kw: commands.append(command) or b"")
    monkeypatch.setattr(ci_images, "image_id", lambda ref: CONFIG)
    pulls = []
    monkeypatch.setattr(ci_images, "pull", lambda ref, *, platform:
                        pulls.append((ref, platform)) or CONFIG)
    result = service_base.prepare(repository, "example/wall", "amd64", "0" * 40,
                                  publish=True, require_published=True)
    build, smoke, push = commands
    assert build[:6] == ["docker", "buildx", "build", "--builder", "default", "--load"]
    assert build[build.index("--target") + 1] == "media-os"
    assert build[build.index("--platform") + 1] == "linux/amd64"
    assert smoke[:3] == ["docker", "run", "--rm"]
    assert smoke[smoke.index("--network") + 1] == "none"
    assert smoke[smoke.index("--user") + 1] == "10001:10001"
    assert "ffmpeg -version" in smoke[-1]
    assert push[:2] == ["docker", "push"]
    assert result["image"] == REF and result["built"] == "true"
    assert pulls == [(REF, "linux/amd64")]


def test_failed_native_smoke_cannot_publish(repository, monkeypatch):
    monkeypatch.setattr(ci_images, "resolve", lambda ref: None)
    monkeypatch.setattr(ci_images, "image_id", lambda ref: CONFIG)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[:2] == ["docker", "run"]:
            raise ci_images.CommandError("FFmpeg cannot start")
        return b""

    monkeypatch.setattr(ci_images, "run", run)
    with pytest.raises(ci_images.CommandError, match="FFmpeg"):
        service_base.prepare(repository, "example/wall", "arm64", "0" * 40, publish=True)
    assert not any(command[:2] == ["docker", "push"] for command in commands)


def test_registry_outage_is_not_permission_to_build(repository, monkeypatch):
    def outage(ref):
        raise ci_images.CommandError("503 Service Unavailable")

    monkeypatch.setattr(ci_images, "resolve", outage)
    monkeypatch.setattr(ci_images, "run", lambda *a, **k: pytest.fail("no outage fallback"))
    with pytest.raises(ci_images.CommandError, match="503"):
        service_base.prepare(repository, "example/wall", "amd64", "0" * 40, force=True)


def test_concurrent_publication_cannot_replace_existing_base(repository, monkeypatch):
    resolutions = iter([None, REF])
    monkeypatch.setattr(ci_images, "resolve", lambda ref: next(resolutions))
    monkeypatch.setattr(ci_images, "image_id", lambda ref: CONFIG)
    commands = []
    monkeypatch.setattr(ci_images, "run", lambda command, **kw: commands.append(command) or b"")
    with pytest.raises(ci_images.ImageError, match="overwrite"):
        service_base.prepare(repository, "example/wall", "arm64", "0" * 40, publish=True)
    assert not any(command[:2] == ["docker", "push"] for command in commands)


def test_local_candidate_is_available_by_tag_without_registry_write(repository, monkeypatch):
    monkeypatch.setattr(ci_images, "resolve", lambda ref: None)
    monkeypatch.setattr(ci_images, "image_id", lambda ref: CONFIG)
    commands = []
    monkeypatch.setattr(ci_images, "run", lambda command, **kw: commands.append(command) or b"")
    result = service_base.prepare(repository, "example/wall", "arm64", "0" * 40)
    assert result["image"].startswith("ghcr.io/example/wall/appliance-media-system:definition-")
    assert result["imageid"] == CONFIG
    assert not any(command[:2] == ["docker", "push"] for command in commands)
