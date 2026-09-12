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
from contracts.release import Release
from player.service import PlayerConfig
from scripts.build_ci_image import (
    _apply_persistent_signing_key,
    _fixture_deployment,
    _load_persistent_signing_key,
    _new_directory,
    _prepare_and_sign_rollback_candidate,
    _sign_release,
    build,
)


@pytest.fixture(autouse=True)
def _clear_persistent_signing_key_env(monkeypatch):
    """Keep the persistent-key path opt-in and hermetic across every test here."""
    monkeypatch.delenv("PHOTO_WALL_RELEASE_SIGNING_KEY", raising=False)
    monkeypatch.delenv("PHOTO_WALL_RELEASE_SIGNING_KEY_FILE", raising=False)


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


@pytest.mark.parametrize("inputs", [
    {"prepared_base": Path("/base")},
    {"player_package": Path("/player")},
    {"os_base_builder_image": "ghcr.io/example/builder@sha256:" + "a" * 64},
    {"os_base_image": "ghcr.io/example/base@sha256:" + "a" * 64},
])
def test_offline_assembly_requires_complete_prepared_inputs(tmp_path, inputs):
    with pytest.raises(BuildError, match="prepared_inputs_incomplete"):
        build(Path.cwd(), "a" * 40, tmp_path / "output", **inputs)
    assert not (tmp_path / "output").exists()


def test_offline_assembly_cannot_enable_legacy_apt_fallback(tmp_path):
    with pytest.raises(BuildError, match="prepared_inputs_conflict"):
        build(
            Path.cwd(), "a" * 40, tmp_path / "output", prepared_base=Path("/base"),
            player_package=Path("/package"), os_base_builder_image="builder",
            apt_archive_cache=Path("/cache"),
        )


def test_prepared_input_failure_never_fetches_or_installs_os_packages(tmp_path, monkeypatch):
    from scripts import build_ci_image as ci

    def forbidden(*args, **kwargs):
        pytest.fail("offline assembly attempted dependency acquisition")

    def missing(*args):
        raise ValueError("baseline_missing")

    monkeypatch.setattr(ci.appliance, "install_runtime_packages", forbidden)
    monkeypatch.setattr(ci, "_prepare_base_root", forbidden)
    monkeypatch.setattr(ci.build_player, "build", forbidden)
    monkeypatch.setattr(ci.os_base, "restore", missing)
    monkeypatch.setattr(ci.os_base, "definition", lambda _: {})
    with pytest.raises(ValueError, match="baseline_missing"):
        ci._prepared_inputs(Path.cwd(), "a" * 40, tmp_path, Path("/base"), Path("/package"), "builder")


def test_prepared_base_cannot_change_builder_without_requalification(tmp_path, monkeypatch):
    from scripts import build_ci_image as ci

    monkeypatch.setattr(ci.os_base, "restore", lambda *args: {"builder_image": "other-builder"})
    monkeypatch.setattr(ci.os_base, "definition", lambda _: {})
    with pytest.raises(BuildError, match="os_base_builder_mismatch"):
        ci._prepared_inputs(Path.cwd(), "a" * 40, tmp_path, Path("/base"), Path("/package"), "builder")


def test_disposable_deployment_has_valid_player_config_and_matching_tls(tmp_path):
    version = subprocess.run(
        ["openssl", "version"], capture_output=True, text=True, check=True
    ).stdout
    if not version.startswith("OpenSSL 3."):
        pytest.skip("CI fixture signing requires OpenSSL 3")
    deployment, key = _fixture_deployment(tmp_path / "deployment")
    config = PlayerConfig.model_validate_json((deployment / "public/public.json").read_bytes())
    assert config.cache_dir == "/run/photo-wall/player/cache"
    assert config.boot_context_file == "/run/photo-wall/boot.json"
    assert config.ca_file == "/etc/photo-wall/ca.pem"
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(
        deployment / "private/server.pem", deployment / "private/server.key.pem"
    )
    subprocess.run(
        [
            "openssl",
            "verify",
            "-CAfile",
            str(deployment / "public/ca.pem"),
            "-verify_hostname",
            "photo-wall.test",
            str(deployment / "private/server.pem"),
        ],
        capture_output=True,
        check=True,
    )
    assert key.is_file() and stat.S_IMODE(key.stat().st_mode) == 0o600
    assert not (deployment / "private/ca.key.pem").exists()
    assert not (deployment / "private/server.csr").exists()
    assert {p.name for p in (deployment / "public").iterdir()} == {
        "public.json",
        "bootstrap.json",
        "ca.pem",
        "release.pub.pem",
    }
    assert (
        json.loads((deployment / "public/bootstrap.json").read_bytes())["time_server"]
        == "photo-wall.test"
    )


def test_shared_release_signer_produces_verifiable_ed25519_signature(tmp_path, monkeypatch):
    version = subprocess.run(
        ["openssl", "version"], capture_output=True, text=True, check=True
    ).stdout
    if not version.startswith("OpenSSL 3."):
        pytest.skip("CI fixture signing requires OpenSSL 3")
    monkeypatch.setattr("appliance.updates.OPENSSL", shutil.which("openssl"))
    deployment, key = _fixture_deployment(tmp_path / "deployment")
    public = deployment / "public"
    manifest = tmp_path / "release.json"
    release = Release(
        revision="a" * 40,
        boot_abi="b" * 64,
        rootfs_sha256="c" * 64,
        rootfs_size=1,
    )
    manifest.write_bytes(release.encode())
    signature = tmp_path / "release.sig"
    record = _sign_release(key, manifest, signature)
    assert record["size"] == 64
    assert (
        verify_release(
            manifest.read_bytes(),
            signature.read_bytes(),
            public / "release.pub.pem",
            release.boot_abi,
        )
        == release
    )


def _generate_ed25519_key(path: Path) -> bytes:
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(path)],
        capture_output=True,
        check=True,
    )
    return path.read_bytes()


def _skip_unless_openssl3():
    version = subprocess.run(
        ["openssl", "version"], capture_output=True, text=True, check=True
    ).stdout
    if not version.startswith("OpenSSL 3."):
        pytest.skip("CI fixture signing requires OpenSSL 3")


def test_persistent_signing_key_signs_with_supplied_key_and_derives_matching_public(
    tmp_path, monkeypatch
):
    """The persistent path signs with, and derives its public half from, the same key."""
    _skip_unless_openssl3()
    monkeypatch.setattr("appliance.updates.OPENSSL", shutil.which("openssl"))
    supplied = tmp_path / "supplied.key"
    pem = _generate_ed25519_key(supplied)
    expected_public = tmp_path / "supplied.pub.pem"
    subprocess.run(
        ["openssl", "pkey", "-in", str(supplied), "-pubout", "-out", str(expected_public)],
        capture_output=True,
        check=True,
    )

    deployment, _disposable_key = _fixture_deployment(tmp_path / "deployment")
    _apply_persistent_signing_key(deployment, pem)

    signing_key = deployment / "private" / "release-signing.key"
    release_pub = deployment / "public" / "release.pub.pem"
    assert signing_key.read_bytes() == pem
    assert release_pub.read_bytes() == expected_public.read_bytes()

    manifest = tmp_path / "release.json"
    release = Release(
        revision="a" * 40, boot_abi="b" * 64, rootfs_sha256="c" * 64, rootfs_size=1,
    )
    manifest.write_bytes(release.encode())
    signature = tmp_path / "release.sig"
    _sign_release(signing_key, manifest, signature)
    assert (
        verify_release(
            manifest.read_bytes(), signature.read_bytes(), release_pub, release.boot_abi,
        )
        == release
    )


def test_persistent_signing_key_absent_preserves_disposable_fallback(tmp_path):
    """No secret supplied: `_load_persistent_signing_key` stays out of the build's way."""
    assert _load_persistent_signing_key() is None


def test_persistent_signing_key_read_from_inline_env_var(monkeypatch, tmp_path):
    pem = b"-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----\n"
    monkeypatch.setenv("PHOTO_WALL_RELEASE_SIGNING_KEY", pem.decode())
    assert _load_persistent_signing_key() == pem


def test_persistent_signing_key_read_from_file_env_var(monkeypatch, tmp_path):
    pem = b"-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----\n"
    key_file = tmp_path / "key.pem"
    key_file.write_bytes(pem)
    monkeypatch.setenv("PHOTO_WALL_RELEASE_SIGNING_KEY_FILE", str(key_file))
    assert _load_persistent_signing_key() == pem


def test_persistent_signing_key_rejects_ambiguous_dual_source(monkeypatch, tmp_path):
    key_file = tmp_path / "key.pem"
    key_file.write_bytes(b"fixture")
    monkeypatch.setenv("PHOTO_WALL_RELEASE_SIGNING_KEY", "inline")
    monkeypatch.setenv("PHOTO_WALL_RELEASE_SIGNING_KEY_FILE", str(key_file))
    with pytest.raises(BuildError, match="release_signing_key_source_conflict"):
        _load_persistent_signing_key()


def test_persistent_signing_key_empty_inline_env_var_is_hard_error(monkeypatch):
    """A blank secret must never be treated as 'unset' -> disposable-key fallback.

    GitHub Actions interpolates a missing/unpopulated secret to an empty
    string without failing the workflow, so silently falling back here would
    ship a release signed with a throwaway key and throwaway trust anchor.
    """
    monkeypatch.setenv("PHOTO_WALL_RELEASE_SIGNING_KEY", "")
    with pytest.raises(BuildError, match="release_signing_key_empty"):
        _load_persistent_signing_key()


def test_persistent_signing_key_whitespace_only_inline_env_var_is_hard_error(monkeypatch):
    monkeypatch.setenv("PHOTO_WALL_RELEASE_SIGNING_KEY", "   \n\t  ")
    with pytest.raises(BuildError, match="release_signing_key_empty"):
        _load_persistent_signing_key()


def test_persistent_signing_key_empty_file_env_var_is_hard_error(monkeypatch, tmp_path):
    key_file = tmp_path / "key.pem"
    key_file.write_text("   \n")
    monkeypatch.setenv("PHOTO_WALL_RELEASE_SIGNING_KEY_FILE", str(key_file))
    with pytest.raises(BuildError, match="release_signing_key_empty"):
        _load_persistent_signing_key()


def test_persistent_signing_key_never_appears_in_phase_output_or_other_files(tmp_path, capsys):
    _skip_unless_openssl3()
    from scripts import build_ci_image as ci

    supplied = tmp_path / "supplied.key"
    pem = _generate_ed25519_key(supplied)
    deployment, _disposable_key = _fixture_deployment(tmp_path / "deployment")
    capsys.readouterr()  # discard fixture_deployment's own phase output
    ci.phase("release_signing_key_persistent", _apply_persistent_signing_key, deployment, pem)
    captured = capsys.readouterr()
    assert pem not in captured.out.encode()
    assert pem not in captured.err.encode()
    signing_key = deployment / "private" / "release-signing.key"
    for path in deployment.rglob("*"):
        if path.is_file() and path != signing_key:
            assert pem not in path.read_bytes()


def test_rollback_candidate_orchestration_keeps_private_path_and_signs_after_prepare(
    tmp_path, monkeypatch
):
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
    path, metadata, signature = _prepare_and_sign_rollback_candidate(
        root, bundle, deployment, signing_key
    )

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
    monkeypatch.setattr(
        ci.tempfile if failure == "workspace" else ci.appliance,
        "mkdtemp" if failure == "workspace" else "run",
        fail,
    )
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
