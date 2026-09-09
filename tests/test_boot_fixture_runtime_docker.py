"""Opt-in actual helper-image import and observer sandbox regression."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import pytest
from test_boot_fixture import HEALTH_RUNTIME_CHECK, synthetic_bundle_and_deployment

from scripts.boot_fixture import LABEL, SOURCE_LABEL, BootFixture
from scripts.container_build import daemon_image_build
from scripts.harness_bundle import BundleFile, HarnessBundle
from scripts.runtime_provenance import source_inventory
from scripts.test_appliance_e2e import ApplianceE2E


def test_actual_derived_health_helper_needs_no_application_imports(tmp_path):
    image_id = os.environ.get("PHOTO_WALL_OBSERVER_TEST_IMAGE")
    if image_id is None:
        pytest.skip("set PHOTO_WALL_OBSERVER_TEST_IMAGE to an immutable local Central image ID")
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", image_id)
    bundle, deployment, _ = synthetic_bundle_and_deployment(tmp_path / "inputs")
    fixture = BootFixture.prepare(tmp_path / "state", bundle, deployment, image_id)
    alias, derived = fixture.project + "-base:local", fixture.project + ":local"

    def docker(*args):
        return subprocess.run(["docker", *args], capture_output=True, text=True,
                              check=True, timeout=180)

    tagged = built = False
    try:
        docker("tag", image_id, alias)
        tagged = True
        result = subprocess.run(daemon_image_build(derived, fixture.state / "context", network="none",
            labels=((LABEL, fixture.project), (SOURCE_LABEL, fixture.marker["source_sha256"]))),
            capture_output=True, text=True, check=True, timeout=180)
        assert result.returncode == 0
        built = True
        inspected = json.loads(docker("image", "inspect", derived).stdout)[0]
        assert inspected["Config"]["User"] == "10001:10001"
        assert inspected["Config"]["Labels"][SOURCE_LABEL] == fixture.marker["source_sha256"]
        # Read the actual staged runtime inside the derived image. -I -S blocks
        # project/site imports while the same helper validates the health body.
        container = ["docker", "run", "--rm", "-i", "--network", "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--memory", "192m", "--cpus", "0.5", "--pids-limit", "64",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=32m", "--entrypoint", "python",
            inspected["Id"]]
        result = subprocess.run([*container, "-I", "-S", "-", "/opt/boot-fixture/runtime.py"],
            input=HEALTH_RUNTIME_CHECK, capture_output=True, text=True, check=True, timeout=30)
        assert json.loads(result.stdout) == dict(health="passed", artifact_imports_required=True)
        # A cached base cannot supply a newly added operator receipt contract.
        # Its exact checked overlay must remain readable by the observer's UID.
        receipt_hash = fixture.marker["source_files"]["central/release_models.py"]
        result = subprocess.run([*container, "-c", """
import hashlib, json, os, stat, sys
from pathlib import Path
from central import release_models
path = Path(release_models.__file__)
digest = hashlib.sha256(path.read_bytes()).hexdigest()
assert path == Path('/app/central/release_models.py') and digest == sys.argv[1]
assert os.getuid() == 10001 and stat.S_IMODE(path.stat().st_mode) == 0o644
assert stat.S_IMODE(path.parent.stat().st_mode) == 0o755
assert release_models.ReleaseStagingReceipt(staged=False).staged is False
for invalid in (1, 1.0, 'true', None):
    try: release_models.ReleaseStagingReceipt.model_validate({'staged': invalid}, strict=True)
    except ValueError: pass
    else: raise AssertionError('nonboolean receipt accepted')
assert release_models.ReleaseRegistrationReceipt(release_id='a'*64).release_id == 'a'*64
print(json.dumps({'receipts': 'passed', 'sha256': digest, 'uid': os.getuid()}))
""", receipt_hash], input="", capture_output=True, text=True, check=True, timeout=30)
        assert json.loads(result.stdout) == dict(receipts="passed", sha256=receipt_hash, uid=10001)
    finally:
        if built:
            docker("image", "rm", derived)
        if tagged:
            docker("image", "rm", alias)


def https_fixture(tmp_path, image_id):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from contracts.release import Release, configuration_digest

    bundle, deployment, old = synthetic_bundle_and_deployment(tmp_path / "inputs")
    key = Ed25519PrivateKey.generate()
    public = deployment / "public"
    (public / "ca.pem").write_bytes((deployment / "private/server.pem").read_bytes())
    (public / "release.pub.pem").write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    release = Release(old.revision, old.boot_abi,
        configuration_digest({path.name: path.read_bytes() for path in public.iterdir()}),
        old.rootfs_sha256, old.rootfs_size)
    (bundle / "release.json").write_bytes(release.encode())
    (bundle / "release.sig").write_bytes(key.sign(release.encode()))
    return BootFixture.prepare(tmp_path / "state", bundle, deployment, image_id)


def test_actual_observer_polling_preserves_production_clock_and_central_dwell(tmp_path):
    image_id = os.environ.get("PHOTO_WALL_OBSERVER_TEST_IMAGE")
    if image_id is None:
        pytest.skip("set PHOTO_WALL_OBSERVER_TEST_IMAGE to an immutable local Central image ID")
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", image_id)
    fixture = https_fixture(tmp_path, image_id)
    harness = object.__new__(ApplianceE2E)
    harness.fixture, harness.run, harness.report = fixture, fixture.run, {}
    name, client_image = fixture.project + "-clock-client", fixture.project + "-clock-client:local"
    client_id = built_id = None
    poll_thread, stop, polls, errors = None, threading.Event(), [], []

    def run(*args, timeout=30):
        return fixture.run(["docker", *args], timeout=timeout)

    def owned_client():
        value = json.loads(run("inspect", "--format", "{{json .}}", name))
        assert value["Id"] == client_id and value["Image"] == built_id
        assert value["Config"]["Labels"][LABEL] == fixture.project
        return value

    try:
        fixture.up()  # Includes actual signed bundle, TLS, database, DNS and NTP checks.
        context = tmp_path / "clock-context"
        context.mkdir()
        root = Path(__file__).resolve().parents[1]
        current = {name: digest for name, digest in source_inventory(root).items()
                   if name.split("/", 1)[0] in ("player", "contracts")}
        manifest = HarnessBundle(tuple(BundleFile(name, name) for name in sorted(current))).stage(root, context)
        assert manifest == current
        driver = root / "tests/observer_clock_client.py"
        (context / "observer_clock_client.py").write_bytes(driver.read_bytes())
        (context / "observer_clock_client.py").chmod(0o644)
        manifest["observer_clock_client.py"] = hashlib.sha256(driver.read_bytes()).hexdigest()
        (context / "sources.json").write_text(json.dumps(manifest, sort_keys=True))
        (context / "sources.json").chmod(0o644)
        source_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        (context / "Dockerfile").write_text(f"FROM {fixture.project}:local\n"
            "COPY player/ /clock-app/player/\nCOPY contracts/ /clock-app/contracts/\n"
            "COPY observer_clock_client.py sources.json /clock-app/\n"
            "ENV PYTHONPATH=/clock-app:/app\n")
        fixture.run(daemon_image_build(client_image, context, network="none",
            labels=((LABEL, fixture.project), (SOURCE_LABEL, source_hash))), timeout=180)
        built_id = run("image", "inspect", "--format", "{{.Id}}", client_image).decode().strip()
        run("create", "--name", name, "--label", LABEL + "=" + fixture.project,
            "--network", fixture.project + "-front", "--memory", "384m", "--cpus", "0.5",
            "--pids-limit", "64", "--user", "10001:10001", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=32m",
            "--tmpfs", "/out:rw,nosuid,nodev,noexec,size=1m,mode=1777",
            "--mount", f"type=volume,source={fixture.project}-public,target=/public,readonly,volume-nocopy",
            built_id, "python", "/clock-app/observer_clock_client.py")
        client_id = run("inspect", "--format", "{{.Id}}", name).decode().strip()
        config = owned_client()
        assert not any(item.startswith(("PHOTO_WALL_DATABASE_URL=", "PHOTO_WALL_ADMIN_TOKEN="))
                       for item in config["Config"]["Env"])
        run("start", name)
        for _ in range(120):
            ready = run("exec", name, "python", "-c",
                "from pathlib import Path; p=Path('/out/session.json'); print(p.read_text() if p.exists() else '{}')")
            session = json.loads(ready)
            if session:
                break
            assert owned_client()["State"]["Running"]
            time.sleep(.25)
        else:
            pytest.fail("clock regression enrollment deadline")

        def poll():
            try:
                while not stop.is_set():
                    value = harness.release_probe("evidence", session).evidence
                    assert value.current and value.current_player_id == session["player_id"]
                    assert value.current_authority_epoch == session["authority_epoch"]
                    assert value.release_id == fixture.marker["release_id"]
                    polls.append(value.model_dump(mode="json"))
                    assert len(polls) <= 40
                    stop.wait(3)
            except Exception as error:
                errors.append(error)

        poll_thread = threading.Thread(target=poll)
        poll_thread.start()
        run("exec", name, "touch", "/out/start")
        assert run("wait", name, timeout=105).strip() == b"0"
        stop.set()
        poll_thread.join(timeout=35)
        assert not poll_thread.is_alive() and not errors
        # Only the bounded public result is printed; private boot capabilities
        # and credentials stay in the client process, never in Docker logs.
        owned_client()
        # Docker may split one long JSON line into several log records. Read
        # the complete finite stdout, checking its byte bound before decoding.
        result_bytes = run("logs", name)
        assert len(result_bytes) <= 1024 * 1024 + 1
        result = json.loads(result_bytes)
        (tmp_path / "clock-result.json").write_bytes(result_bytes)
        assert result["constants"] == dict(max_uncertainty=.1, max_step=.25, max_age=30)
        assert not result["task_faults"] and result["release_accepted"] is True
        accepted = [value for value in result["health_responses"] if value["accepted"]]
        assert accepted and accepted[0]["elapsed"] >= 30
        assert len(polls) >= 15 and polls[-1]["status"] == "healthy"
        assert result["source_files"] == manifest
    finally:
        stop.set()
        if poll_thread is not None:
            poll_thread.join(timeout=35)
        if client_id is not None:
            owned_client()
            run("rm", "-f", name)
        if built_id is not None:
            observed = json.loads(run("image", "inspect", client_image))[0]
            assert observed["Id"] == built_id and observed["Config"]["Labels"][LABEL] == fixture.project
            run("image", "rm", client_image)
        fixture.down()
