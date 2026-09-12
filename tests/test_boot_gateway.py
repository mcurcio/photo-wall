"""The boot fixture serves only its verified signed public bundle."""

import hashlib

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

from contracts.release import Release
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


@pytest.mark.parametrize("failure", ["signature", "corrupt", "truncated",
                                      "manifest_symlink", "rootfs_symlink", "directory_symlink"])
def test_unverified_or_symlinked_bundle_is_never_served(bundle, failure):
    root, public, release, payload = bundle
    if failure == "signature":
        (root / "release.sig").write_bytes(b"x"*64)
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


@pytest.mark.parametrize('consumption_fault', [None, 'missing', 'wrong_ticket', 'wrong_release', 'wrong_device'])
def test_real_central_fixture_selects_registered_signed_candidate_and_falls_back(
        registry, bundle, signing_key, monkeypatch, capsys, consumption_fault):
    import base64
    import sys
    import uuid

    from central.app import create_app as central_app
    from central.installation_models import EquipmentSessionObservation
    from contracts.enrollment import OutputReport, enrollment_message
    from scripts import boot_gateway, vm_release_probe
    from scripts.boot_fixture import FixtureError
    from scripts.test_appliance_e2e import ApplianceE2E
    from scripts.vm_release_contract import trusted_release_failure

    root, public, accepted, _ = bundle
    candidate_bytes = b'failed candidate root'
    candidate = Release('e'*40, accepted.boot_abi,
                        hashlib.sha256(candidate_bytes).hexdigest(), len(candidate_bytes))
    (root / candidate.rootfs_name).write_bytes(candidate_bytes)
    monkeypatch.setenv('PHOTO_WALL_APPLIANCE_BUNDLE', str(root))
    monkeypatch.setenv('PHOTO_WALL_BOOT_PUBLIC_CONFIG', str(public))
    for name in ('PHOTO_WALL_RELEASE_PUBLIC_KEY', 'PHOTO_WALL_RELEASE_BOOT_ABI',
                 'PHOTO_WALL_RELEASE_ROOT',
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
        player_key = Ed25519PrivateKey.generate()
        public_key = player_key.public_key().public_bytes_raw().hex()
        nonce = client.post('/v1/enrollment/challenge', json=dict(public_key=public_key)).json()['nonce']
        outputs = (OutputReport(output_id='HDMI-A-1', width_px=1920, height_px=1080),)
        identity = client.post('/v1/enrollment/register', json=dict(
            public_key=public_key, nonce=nonce,
            signature=base64.b64encode(player_key.sign(enrollment_message(nonce, outputs,
                following['device_id'], following['boot_id'], following['ticket_id']))).decode(),
            outputs=[output.model_dump(mode='json') for output in outputs],
            **{key: following[key] for key in ('device_id', 'boot_id', 'ticket_id')}))
        assert identity.status_code == 200
        session = identity.json()
        restored = EquipmentSessionObservation(player_id=session['player_id'],
            authority_epoch=session['authority_epoch'], device_id=following['device_id'], retired=False)

        # Start from actual authority transactions, then corrupt only the trial
        # relation. Existing failed/current attempt facts must not hide this gap.
        if consumption_fault is not None:
            other_device = 'device-' + 'a' * 64
            if consumption_fault == 'wrong_device':
                other = client.post('/v1/bootstrap/boot', json=request | dict(
                    device_id=other_device, boot_id=str(uuid.uuid4()))).json()
                assert other['device_id'] == other_device
            with registry.db.transaction() as conn:
                if consumption_fault == 'missing':
                    conn.execute('DELETE FROM appliance_release_trials WHERE device_id=%s', (request['device_id'],))
                elif consumption_fault == 'wrong_ticket':
                    conn.execute('UPDATE appliance_release_trials SET ticket_id=%s WHERE device_id=%s',
                                 (following['ticket_id'], request['device_id']))
                elif consumption_fault == 'wrong_release':
                    conn.execute('UPDATE appliance_release_trials SET release_id=%s WHERE device_id=%s',
                                 (accepted.release_id, request['device_id']))
                else:
                    conn.execute('UPDATE appliance_release_trials SET device_id=%s WHERE device_id=%s',
                                 (other_device, request['device_id']))

        harness = object.__new__(ApplianceE2E)
        container = 'pw-boot-' + 'a' * 16 + '-observer'
        harness.fixture_observer = lambda: container
        harness.inputs = dict(candidate=candidate, release=accepted)
        harness.report = dict(checks={})
        monkeypatch.setenv('PHOTO_WALL_DATABASE_URL', 'isolated-test-database')
        monkeypatch.setattr(vm_release_probe, 'Database', lambda _: registry.db)
        serialized = []

        def execute_probe(args, **kwargs):
            assert args[:6] == ['docker', 'exec', container, 'python', '-m', 'scripts.vm_release_probe']
            monkeypatch.setattr(sys, 'argv', args[5:])
            capsys.readouterr()
            failed = False
            try:
                vm_release_probe.main()
            except SystemExit as exc:
                assert exc.code == 1
                failed = True
            output = capsys.readouterr().out.encode()
            serialized.append(output)
            # Preserve the real nonzero boundary while feeding exact producer
            # CLI bytes through the same decoder used by the Docker adapter.
            if failed:
                failure = trusted_release_failure(args, output)
                assert failure is not None
                raise FixtureError('release_probe_failed') from failure
            return output

        harness.run = execute_probe
        def boot(ticket):
            return {key: ticket[key] for key in ('device_id', 'boot_id', 'release_id', 'trial')} | dict(
                ticket_sha256=hashlib.sha256(ticket['ticket_id'].encode()).hexdigest())
        if consumption_fault is not None:
            with pytest.raises(FixtureError, match='release_probe_evidence_invalid'):
                harness.verify_central_rollback(boot(trial), boot(following), restored)
            assert not harness.report['checks'].get('central_trial_consumed')
            assert harness.report['release_probe_failures'] == [dict(
                schema_version=1, kind='release-failure', role='observer', action='evidence',
                stage='result', code='release_probe_evidence_invalid')]
        else:
            harness.verify_central_rollback(boot(trial), boot(following), restored)
            proof = harness.report['central_failed_trial']
            assert proof['status'] == 'failed' and not proof['current']
            assert proof['trial_ticket_sha256'] == proof['ticket_sha256'] == boot(trial)['ticket_sha256']
            assert proof['candidate_release_id'] == candidate.release_id
            assert harness.report['central_fallback']['current_player_id'] == session['player_id']
            assert harness.report['central_fallback']['current_authority_epoch'] == session['authority_epoch']
            assert harness.report['checks']['central_trial_consumed'] is True
        assert all(ticket['ticket_id'].encode() not in output
                   for output in serialized for ticket in (trial, following))
        assert all(session['token'].encode() not in output for output in serialized)


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
