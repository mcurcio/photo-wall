"""Real stage producer/API/PostgreSQL regression; ASGI replaces TLS transport only."""
from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from central.app import create_app
from central.releases import ReleaseAuthority
from contracts.release import Release
from scripts import vm_release_probe
from scripts.vm_release_contract import ReleaseStageResult, decode_release_result
from tests.conftest import BOOT_ABI, CONFIGURATION

ADMIN = 'release-stage-integration-' + 'x' * 32
DEVICE = 'device-' + 'e' * 64


@pytest.fixture
def stage_boundary(registry, monkeypatch):
    key = Ed25519PrivateKey.generate()
    authority = ReleaseAuthority(registry.db, registry.clock, key.public_key(),
        BOOT_ABI, CONFIGURATION)
    accepted = Release('1' * 40, BOOT_ABI, CONFIGURATION, '2' * 64, 1024)
    authority.register(accepted.encode(), key.sign(accepted.encode()))
    authority.set_default(accepted.release_id)
    candidate = Release('3' * 40, BOOT_ABI, CONFIGURATION, '4' * 64, 2048)
    manifest = base64.b64encode(candidate.encode()).decode()
    signature = base64.b64encode(key.sign(candidate.encode())).decode()
    app = create_app(db=registry.db, clock=registry.clock, release_authority=authority,
        admin_token=ADMIN, run_scheduler=False)
    state = SimpleNamespace(candidate=candidate, manifest=manifest, signature=signature,
        accepted_manifest=base64.b64encode(accepted.encode()).decode(),
        accepted_signature=base64.b64encode(key.sign(accepted.encode())).decode(),
        calls=[], mutation=None, registry=registry)
    tls = object()

    def context(*, cafile):
        assert cafile == '/public/ca.pem'
        return tls

    with TestClient(app, base_url='https://photo-wall.test') as central:
        selected = central.post('/v1/bootstrap/boot', json=dict(device_id=DEVICE,
            boot_id=str(uuid.uuid4()), request_id='f' * 48))
        assert selected.status_code == 200
        assert selected.json()['release_id'] == accepted.release_id

        class Transport:
            def __init__(self, **options):
                assert options == dict(base_url='https://photo-wall.test', trust_env=False,
                    follow_redirects=False, verify=tls, timeout=15,
                    headers={'Authorization': 'Bearer ' + vm_release_probe.os.environ['PHOTO_WALL_ADMIN_TOKEN']})
                self.headers = options['headers']

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def request(self, method, path, **kwargs):
                response = central.request(method, path, headers=self.headers, **kwargs)
                state.calls.append((method, path, response.status_code, response.json()))
                if state.mutation is not None:
                    response = state.mutation(method, response)
                return response

            def post(self, path, **kwargs):
                return self.request('POST', path, **kwargs)

            def put(self, path, **kwargs):
                return self.request('PUT', path, **kwargs)

        monkeypatch.setenv('PHOTO_WALL_ADMIN_TOKEN', ADMIN)
        monkeypatch.setattr(vm_release_probe, 'ssl', SimpleNamespace(create_default_context=context))
        monkeypatch.setattr(vm_release_probe, 'httpx', SimpleNamespace(Client=Transport))
        yield state


def stored(boundary):
    with boundary.registry.db.transaction() as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        release = conn.execute('SELECT manifest,signature FROM appliance_releases WHERE release_id=%s',
            (boundary.candidate.release_id,)).fetchone()
        device = conn.execute('SELECT candidate_release_id FROM appliance_devices WHERE device_id=%s',
            (DEVICE,)).fetchone()
    return release, device['candidate_release_id']


def changed_response(response, body):
    # The actual authenticated handler has run; corrupt only its returned boundary body.
    return httpx.Response(response.status_code, json=body, request=response.request)


def test_stage_helper_registers_canonical_signed_release_and_returns_strict_receipt(stage_boundary):
    b = stage_boundary
    assert base64.b64encode(base64.b64decode(b.manifest, validate=True)).decode() == b.manifest
    assert base64.b64encode(base64.b64decode(b.signature, validate=True)).decode() == b.signature
    result = vm_release_probe.stage(DEVICE, b.manifest, b.signature)
    assert isinstance(result, ReleaseStageResult)
    assert result.model_dump() == dict(schema_version=1, kind='release-staged', staged=True,
        release_id=b.candidate.release_id)
    assert decode_release_result('stage', result.model_dump_json().encode()) == result
    assert b.calls == [('POST', '/v1/operator/releases', 201, {'release_id': b.candidate.release_id}),
        ('PUT', f'/v1/operator/equipment/{DEVICE}/candidate/{b.candidate.release_id}', 200, {'staged': True})]
    release, candidate = stored(b)
    assert bytes(release['manifest']) == base64.b64decode(b.manifest, validate=True)
    assert bytes(release['signature']) == base64.b64decode(b.signature, validate=True)
    assert candidate == b.candidate.release_id


@pytest.mark.parametrize('fault', ['operator_auth', 'wrong_signature', 'unknown_device'])
def test_stage_helper_rejects_real_operator_api_errors(stage_boundary, monkeypatch, fault):
    b = stage_boundary
    device, signature = DEVICE, b.signature
    if fault == 'operator_auth':
        monkeypatch.setenv('PHOTO_WALL_ADMIN_TOKEN', 'wrong-integration-operator-' + 'z' * 32)
    elif fault == 'wrong_signature':
        signature = base64.b64encode(Ed25519PrivateKey.generate().sign(b.candidate.encode())).decode()
    else:
        device = 'device-' + 'd' * 64
    expected = 'release_stage_failed' if fault == 'unknown_device' else 'release_registration_failed'
    with pytest.raises(ValueError, match='^' + expected + '$'):
        vm_release_probe.stage(device, b.manifest, signature)
    assert b.calls[-1][2] == {'operator_auth': 401, 'wrong_signature': 422, 'unknown_device': 404}[fault]
    release, candidate = stored(b)
    assert candidate is None
    assert (release is not None) == (fault == 'unknown_device')


@pytest.mark.parametrize('fault', ['missing', 'null', 'integer', 'malformed', 'changed', 'extra'])
def test_stage_helper_rejects_registration_receipt_drift_before_staging(stage_boundary, fault):
    b = stage_boundary
    bodies = {'missing': {}, 'null': {'release_id': None}, 'integer': {'release_id': 123},
        'malformed': {'release_id': 'not-a-digest'}, 'changed': {'release_id': '9' * 64},
        'extra': {'release_id': b.candidate.release_id, 'unexpected': True}}

    def mutate(method, response):
        if method == 'POST':
            return changed_response(response, bodies[fault])
        return response

    b.mutation = mutate
    with pytest.raises(ValueError, match='^release_registration_failed$'):
        vm_release_probe.stage(DEVICE, b.manifest, b.signature)
    assert len(b.calls) == 1 and stored(b)[1] is None


@pytest.mark.parametrize('body', [{}, {'staged': None}, {'staged': 1}, {'staged': 1.0},
    {'staged': 'true'}, {'staged': False}, {'staged': True, 'unexpected': True}, {'error': 'stage_failed'}])
def test_stage_helper_rejects_malformed_or_unsuccessful_staging_receipts(stage_boundary, body):
    b = stage_boundary
    b.mutation = lambda method, response: changed_response(response, body) if method == 'PUT' else response
    with pytest.raises(ValueError, match='^release_stage_failed$'):
        vm_release_probe.stage(DEVICE, b.manifest, b.signature)
    assert len(b.calls) == 2


def test_accepted_release_retains_canonical_false_api_receipt_and_is_not_a_staged_candidate(stage_boundary):
    b = stage_boundary
    with pytest.raises(ValueError, match='^release_stage_failed$'):
        vm_release_probe.stage(DEVICE, b.accepted_manifest, b.accepted_signature)
    assert b.calls[-1][2:] == (200, {'staged': False})
    assert stored(b)[1] is None
