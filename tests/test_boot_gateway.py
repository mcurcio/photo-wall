"""The boot fixture serves only its verified signed public bundle."""

import hashlib

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

from contracts.release import Release, configuration_digest
from scripts.boot_gateway import BootBundle


@pytest.fixture
def signing_key():
    return Ed25519PrivateKey.generate()


@pytest.fixture
def bundle(tmp_path, signing_key):
    private = signing_key
    public, root = tmp_path / "public", tmp_path / "bundle"
    public.mkdir()
    root.mkdir()
    files = {"public.json": b"public Player fixture", "bootstrap.json": b"public bootstrap fixture",
             "ca.pem": b"public fixture CA", "release.pub.pem": private.public_key().public_bytes(
                 serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)}
    for name, data in files.items():
        (public / name).write_bytes(data)
    payload = b"signed synthetic rootfs bytes"
    release = Release(revision="a"*40, boot_abi="b"*64,
                      configuration_sha256=configuration_digest(files),
                      rootfs_sha256=hashlib.sha256(payload).hexdigest(), rootfs_size=len(payload))
    (root / "release.json").write_bytes(release.encode())
    (root / "release.sig").write_bytes(private.sign(release.encode()))
    (root / release.rootfs_name).write_bytes(payload)
    return root, public, release, payload


def test_only_three_signed_public_files_are_served(bundle):
    root, public, release, payload = bundle
    loaded = BootBundle.load(root, public)
    app = FastAPI()

    @app.get("/appliance/{filename}")
    def artifact(filename: str):
        return loaded.response(filename)

    (root / "private.txt").write_bytes(b"must stay unserved")
    with TestClient(app) as client:
        for name in ("release.json", "release.sig", release.rootfs_name):
            response = client.get("/appliance/" + name)
            assert response.status_code == 200
            assert response.content == (root / name).read_bytes()
            assert response.headers["cache-control"] == "no-store"
            assert int(response.headers["content-length"]) == len(response.content)
        for name in ("private.txt", "public.json", "rootfs-" + "0"*64 + ".squashfs", "%2e%2e"):
            assert client.get("/appliance/" + name).status_code == 404


@pytest.mark.parametrize("failure", ["signature", "configuration", "corrupt", "truncated",
                                      "manifest_symlink", "rootfs_symlink", "directory_symlink"])
def test_unverified_or_symlinked_bundle_is_never_served(bundle, failure):
    root, public, release, payload = bundle
    if failure == "signature":
        (root / "release.sig").write_bytes(b"x"*64)
    elif failure == "configuration":
        (public / "public.json").write_bytes(b"changed public configuration")
    elif failure in ("corrupt", "truncated"):
        (root / release.rootfs_name).write_bytes(b"x"*len(payload) if failure == "corrupt" else payload[:-1])
    elif failure == "directory_symlink":
        link = root.with_name("link")
        link.symlink_to(root, target_is_directory=True)
        root = link
    else:
        path = root / ("release.json" if failure == "manifest_symlink" else release.rootfs_name)
        saved = path.with_name("saved")
        path.rename(saved)
        path.symlink_to(saved)
    with pytest.raises((OSError, ValueError, InvalidSignature)):
        BootBundle.load(root, public)


def test_persistent_media_fault_denies_bodies_but_preserves_control(tmp_path):
    from scripts.boot_gateway import install_media_denial

    control = tmp_path / "control"
    control.mkdir()
    delivered = []
    def app():
        result = FastAPI()
        @result.get("/v1/media/{digest}")
        def media(digest):
            delivered.append(digest)
            return {"bytes": "photo"}
        @result.get("/v1/player/state")
        def state():
            return {"control": "available"}
        install_media_denial(result, control)
        return result
    with TestClient(app()) as client:
        assert client.get("/v1/media/" + "a" * 64).status_code == 200
    (control / "media-blocked").write_bytes(b"photo-wall-ci-media-blocked-v1\n")
    # Recreating the app models the central restart: denial is persistent.
    for _ in range(2):
        with TestClient(app()) as client:
            for method in ("GET", "HEAD"):
                response = client.request(method, "/v1/media/" + "a" * 64)
                assert response.status_code == 503
                assert response.headers["x-photo-wall-fixture"] == "media-blocked"
                if method == "GET":
                    assert response.json() == {"error": "fixture_media_blocked"}
            assert client.get("/v1/player/state").json() == {"control": "available"}
    assert len(delivered) == 1


def test_invalid_or_unreadable_fault_marker_keeps_delivery_denied(tmp_path, monkeypatch):
    from scripts import boot_gateway

    app = FastAPI()
    boot_gateway.install_media_denial(app, tmp_path)
    marker = tmp_path / "media-blocked"
    marker.symlink_to(tmp_path / "missing")
    with TestClient(app) as client:
        assert client.get("/v1/media/" + "a" * 64).status_code == 503
        original = boot_gateway.os.lstat
        def denied(path, *args, **kwargs):
            if path == marker:
                raise PermissionError("private-path")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(boot_gateway.os, "lstat", denied)
        response = client.get("/v1/media/" + "a" * 64)
        assert response.status_code == 503 and "private-path" not in response.text


def test_real_central_fixture_selects_registered_signed_candidate_and_falls_back(
        registry, bundle, signing_key, monkeypatch):
    import base64
    import uuid

    from central.app import create_app as central_app
    from scripts import boot_gateway
    from scripts.vm_release_probe import evidence

    root, public, accepted, _ = bundle
    candidate_bytes = b'failed candidate root'
    candidate = Release('e'*40, accepted.boot_abi, accepted.configuration_sha256,
                        hashlib.sha256(candidate_bytes).hexdigest(), len(candidate_bytes))
    (root / candidate.rootfs_name).write_bytes(candidate_bytes)
    monkeypatch.setenv('PHOTO_WALL_APPLIANCE_BUNDLE', str(root))
    monkeypatch.setenv('PHOTO_WALL_BOOT_PUBLIC_CONFIG', str(public))
    for name in ('PHOTO_WALL_RELEASE_PUBLIC_KEY', 'PHOTO_WALL_RELEASE_BOOT_ABI',
                 'PHOTO_WALL_RELEASE_CONFIGURATION_SHA256', 'PHOTO_WALL_RELEASE_ROOT',
                 'PHOTO_WALL_INITIAL_RELEASE_MANIFEST', 'PHOTO_WALL_INITIAL_RELEASE_SIGNATURE'):
        monkeypatch.setenv(name, '')  # Record restoration before gateway configures its public inputs.
    monkeypatch.setattr(boot_gateway, 'create_central_app', lambda: central_app(
        db=registry.db, clock=registry.clock, admin_token='fixture-admin-token-'+'x'*32, run_scheduler=False))
    with registry.db.transaction() as conn:
        conn.execute('DELETE FROM appliance_release_policy')
    app = boot_gateway.create_app()
    with TestClient(app) as client:
        request = dict(device_id='device-'+'d'*64, boot_id=str(uuid.uuid4()), request_id='f'*48)
        first = client.post('/v1/bootstrap/boot', json=request)
        assert first.status_code == 200
        assert first.json()['release_id'] == accepted.release_id
        assert first.json()['trial'] is False
        assert client.get('/appliance/'+candidate.rootfs_name).status_code == 404
        admin = {'Authorization': 'Bearer fixture-admin-token-'+'x'*32}
        registered = client.post('/v1/operator/releases', headers=admin, json=dict(
            manifest=candidate.encode().decode(), signature=base64.b64encode(signing_key.sign(candidate.encode())).decode()))
        assert registered.status_code == 201
        assert client.get('/appliance/'+candidate.rootfs_name).content == candidate_bytes
        assert client.put(f'/v1/operator/equipment/{request["device_id"]}/candidate/{candidate.release_id}',
                          headers=admin).json() == {'staged': True}
        trial_request = request | dict(boot_id=str(uuid.uuid4()), request_id='e'*48)
        trial = client.post('/v1/bootstrap/boot', json=trial_request).json()
        assert trial['trial'] and trial['release_id'] == candidate.release_id
        assert client.post('/v1/bootstrap/boot', json=trial_request).json() == trial
        following = client.post('/v1/bootstrap/boot', json=request | dict(
            boot_id=str(uuid.uuid4()), request_id='d'*48)).json()
        assert not following['trial'] and following['release_id'] == accepted.release_id
        with registry.db.transaction() as conn:
            proof = evidence(conn, request['device_id'], trial_request['boot_id'])
        assert proof['status'] == 'failed' and not proof['current']
        assert proof['ticket_sha256'] == hashlib.sha256(trial['ticket_id'].encode()).hexdigest()
        assert 'ticket_id' not in proof


def test_delivery_evidence_names_current_epoch_without_exposing_bearer(capsys):
    from types import SimpleNamespace

    from scripts.boot_gateway import install_delivery_observer

    app = FastAPI()
    token = 'private-fixture-bearer-'+'x'*32
    authenticated = []
    def authenticate(value):
        authenticated.append(value)
        return dict(id='p-fixture', authority_epoch=2)
    app.state.registry = SimpleNamespace(authenticate=authenticate)
    @app.get('/v1/media/{digest}')
    def image(digest):
        return {'fixture': 'synthetic media'}
    install_delivery_observer(app)
    with TestClient(app) as client:
        assert client.get('/v1/media/'+'a'*64, headers={'Authorization':'Bearer '+token}).status_code == 200
    import json
    output = capsys.readouterr().out
    assert json.loads(output) == dict(event='photo-wall-fixture-media-attempt', sha256='a'*64,
                                      authenticated=True, status_class='2xx',
                                      player_id='p-fixture', authority_epoch=2)
    assert token not in output and authenticated == [token]


def test_media_attempt_records_unauthenticated_failure_without_identity(capsys):
    import json
    from types import SimpleNamespace

    from scripts.boot_gateway import install_delivery_observer

    app = FastAPI()
    app.state.registry = SimpleNamespace(authenticate=lambda _: (_ for _ in ()).throw(ValueError()))
    @app.get('/v1/media/{digest}')
    def image(digest):
        return Response(status_code=503)
    install_delivery_observer(app)
    with TestClient(app) as client:
        assert client.get('/v1/media/'+'b'*64, headers={'Authorization':'Bearer stale'}).status_code == 503
    event = json.loads(capsys.readouterr().out)
    assert event == dict(event='photo-wall-fixture-media-attempt', sha256='b'*64,
                         authenticated=False, status_class='5xx')
