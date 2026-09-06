"""Cheap guards for the CI image orchestration boundary."""

import json
import ssl
import stat
import subprocess
from pathlib import Path

import pytest

from appliance.build import BuildError
from player.service import PlayerConfig
from scripts.build_ci_image import _fixture_deployment, _new_directory, build


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
