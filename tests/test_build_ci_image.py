"""Cheap guards for the CI image orchestration boundary."""

import json
import shutil
import ssl
import stat
import subprocess
from pathlib import Path

import pytest

from appliance.build import BuildError
from appliance.updates import verify_release
from contracts.release import Release, configuration_digest
from player.service import PlayerConfig
from scripts.build_ci_image import (
    _fixture_deployment,
    _new_directory,
    _prepare_and_sign_rollback_candidate,
    _sign_release,
    build,
)


def test_ci_output_and_deployment_paths_must_be_new_and_outside_git(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(BuildError, match="output_exists"):
        build(Path.cwd(), "a" * 40, existing)
    with pytest.raises(BuildError, match="deployment_exists"):
        _new_directory(existing, label="deployment")


def test_ci_rejects_non_commit_shaped_revision_before_creating_outputs(tmp_path):
    output = tmp_path / "artifact"
    with pytest.raises(BuildError, match="revision_invalid"):
        build(Path.cwd(), "not-a-commit", output)
    assert not output.exists()


def test_disposable_deployment_has_valid_player_config_and_matching_tls(tmp_path):
    version = subprocess.run(["openssl", "version"], capture_output=True, text=True, check=True).stdout
    if not version.startswith("OpenSSL 3."):
        pytest.skip("CI fixture signing requires OpenSSL 3")
    deployment, key = _fixture_deployment(tmp_path / "deployment")
    config = PlayerConfig.model_validate_json((deployment / "public/public.json").read_bytes())
    assert config.state_dir == "/var/lib/photo-wall/player"
    assert config.ca_file == "/etc/photo-wall/ca.pem"
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(deployment / "private/server.pem", deployment / "private/server.key.pem")
    subprocess.run(["openssl", "verify", "-CAfile", str(deployment / "public/ca.pem"),
                    "-verify_hostname", "photo-wall.test", str(deployment / "private/server.pem")],
                   capture_output=True, check=True)
    assert key.is_file() and stat.S_IMODE(key.stat().st_mode) == 0o600
    assert not (deployment / "private/ca.key.pem").exists()
    assert not (deployment / "private/server.csr").exists()
    assert {p.name for p in (deployment / "public").iterdir()} == {
        "public.json", "bootstrap.json", "ca.pem", "release.pub.pem"}
    assert json.loads((deployment / "public/bootstrap.json").read_bytes())["time_server"] == "photo-wall.test"


def test_shared_release_signer_produces_verifiable_ed25519_signature(tmp_path, monkeypatch):
    version = subprocess.run(["openssl", "version"], capture_output=True, text=True, check=True).stdout
    if not version.startswith("OpenSSL 3."):
        pytest.skip("CI fixture signing requires OpenSSL 3")
    monkeypatch.setattr("appliance.updates.OPENSSL", shutil.which("openssl"))
    deployment, key = _fixture_deployment(tmp_path / "deployment")
    public = deployment / "public"
    config = configuration_digest({name: (public / name).read_bytes()
                                   for name in ("public.json", "bootstrap.json", "ca.pem", "release.pub.pem")})
    manifest = tmp_path / "release.json"
    release = Release(revision="a" * 40, boot_abi="b" * 64,
                      configuration_sha256=config,
                      rootfs_sha256="c" * 64, rootfs_size=1)
    manifest.write_bytes(release.encode())
    signature = tmp_path / "release.sig"
    record = _sign_release(key, manifest, signature)
    assert record["size"] == 64
    assert verify_release(manifest.read_bytes(), signature.read_bytes(), public / "release.pub.pem",
                          release.boot_abi, config) == release


def test_rollback_candidate_orchestration_keeps_private_path_and_signs_after_prepare(tmp_path, monkeypatch):
    from scripts import build_ci_image as ci

    root, bundle, deployment = (tmp_path / name for name in ("root", "bundle", "deployment"))
    root.mkdir()
    bundle.mkdir()
    deployment.mkdir()
    signing_key = tmp_path / "release-signing.key"
    signing_key.write_bytes(b"fixture")
    calls = []

    def prepare(*args):
        calls.append(("prepare", args))
        destination = args[-1]
        destination.mkdir()
        (destination / "release.json").write_bytes(b"manifest")
        return {"schema": 1, "kind": "ci-rollback-candidate"}

    def sign(*args):
        calls.append(("sign", args))
        args[-1].write_bytes(b"s" * 64)
        return {"sha256": "s" * 64, "size": 64}

    monkeypatch.setattr(ci.build_rollback_candidate, "prepare", prepare)
    monkeypatch.setattr(ci, "_sign_release", sign)
    path, metadata, signature = _prepare_and_sign_rollback_candidate(root, bundle, deployment, signing_key)

    assert path == deployment / "rollback-candidate"
    assert metadata["kind"] == "ci-rollback-candidate"
    assert signature["size"] == 64
    assert calls[0][0] == "prepare" and calls[1][0] == "sign"
    assert calls[1][1][1] == path / "release.json"
    assert (path / "release.sig").read_bytes() == b"s" * 64


def test_fixture_failure_removes_created_private_material(tmp_path, monkeypatch):
    from scripts import build_ci_image as ci

    calls = 0

    def failing_openssl(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            Path(args[-1]).write_bytes(b"disposable signing key")
        else:
            raise BuildError("fixture_tool_failed")

    monkeypatch.setattr(ci, "_openssl", failing_openssl)
    deployment = tmp_path / "deployment"
    with pytest.raises(BuildError, match="fixture_tool_failed"):
        ci._fixture_deployment(deployment)
    assert not deployment.exists()


@pytest.mark.parametrize("failure", ["workspace", "fetch"])
def test_build_failure_removes_owned_fixture_and_workspace(tmp_path, monkeypatch, failure):
    from scripts import build_ci_image as ci

    deployment = tmp_path / "deployment"

    def fixture(destination):
        destination.mkdir()
        private = destination / "private"
        private.mkdir()
        key = private / "release-signing.key"
        key.write_bytes(b"disposable signing key")
        (private / "server.key.pem").write_bytes(b"disposable TLS key")
        return destination, key

    def fail(*args, **kwargs):
        raise BuildError("injected_failure")

    monkeypatch.setattr(ci, "_fixture_deployment", fixture)
    monkeypatch.setattr(ci, "_preflight_space", lambda path: ci.MIN_FREE_BYTES)
    monkeypatch.setattr(ci.tempfile if failure == "workspace" else ci.appliance,
                        "mkdtemp" if failure == "workspace" else "run", fail)
    with pytest.raises(BuildError, match="injected_failure"):
        ci.build(Path.cwd(), "a" * 40, tmp_path / "output", deployment=deployment)
    assert not deployment.exists()
    assert not list(tmp_path.glob(".photo-wall-ci-work-*"))


def test_build_preserves_preexisting_deployment_on_refusal(tmp_path, monkeypatch):
    from scripts import build_ci_image as ci

    deployment = tmp_path / "deployment"
    deployment.mkdir()
    sentinel = deployment / "existing"
    sentinel.write_bytes(b"preexisting content")
    monkeypatch.setattr(ci, "_preflight_space", lambda path: ci.MIN_FREE_BYTES)
    with pytest.raises(BuildError, match="deployment_exists"):
        ci.build(Path.cwd(), "a" * 40, tmp_path / "output", deployment=deployment)
    assert sentinel.read_bytes() == b"preexisting content"
