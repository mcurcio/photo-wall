"""Qualification signature, report integrity and durable live publication checks.

Payloads are fixtures; these tests do not qualify any actual container image.
"""
import json
from copy import deepcopy
from uuid import uuid4

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from central.fleet.node_ci_evidence import CiEvidencePublisher, report_result, sign, verify
from contracts.node_rollout import (
    CI_LIVE_DOMAIN,
    REPORT_CATEGORIES,
    REVOCATION_DOMAIN,
    canonical,
    document_hash,
    validate_qualification,
)


def fixture_bundle(tmp_path):
    raw = b'<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"><testcase classname="fixture" name="positive"/></testsuite></testsuites>'
    report = report_result(raw)
    (tmp_path / (report['sha256']+'.xml')).write_bytes(raw)
    imports = {name: '/app/'+name.replace('.', '/')+'.py' for name in (
        'central.app', 'central.coordination', 'central.fleet.node_lifecycle', 'contracts.node_protocol')}
    payload = {'schema': 1, 'kind': 'image_qualification', 'revision': 'a'*40,
        'suite_sha256': 'b'*64, 'created_at': 900, 'expires_at': 2000,
        'images': [{'reference': 'registry.invalid/central@sha256:'+'c'*64,
            'image_config_id': 'sha256:'+'d'*64, 'platform': 'linux/amd64',
            'interpreter': '/app/.venv/bin/python', 'imports': imports,
            'reports': {category: report for category in REPORT_CATEGORIES}}]}
    key = Ed25519PrivateKey.generate()
    return payload, key, sign(payload, key, tmp_path)


def revocations(key, *, generation=1, revoked=(), start=990, expiry=1100):
    payload = {'schema': 1, 'kind': 'qualification_revocations', 'audience': 'test',
        'record_uid': 'feed-uid', 'generation': generation, 'issued_at': start, 'expires_at': expiry,
        'revoked_qualifications': sorted(revoked)}
    return canonical({'payload': payload, 'signature': key.sign(REVOCATION_DOMAIN+canonical(payload)).hex()})


def test_qualification_requires_signed_exact_reports_and_installed_imports(tmp_path):
    payload, key, raw = fixture_bundle(tmp_path)
    assert verify(raw, key.public_key(), tmp_path, now=1000) == payload
    with pytest.raises(ValueError, match='signature_shape'):
        verify(canonical({'state': 'unsigned_unqualified'}), key.public_key(), tmp_path, now=1000)
    with pytest.raises(InvalidSignature):
        verify(raw, Ed25519PrivateKey.generate().public_key(), tmp_path, now=1000)
    with pytest.raises(ValueError, match='expired'):
        verify(raw, key.public_key(), tmp_path, now=2000)
    changed = deepcopy(payload)
    changed['images'][0]['imports']['central.app'] = '/qualification/central/app.py'
    with pytest.raises(ValueError, match='source_shadowed'):
        validate_qualification(changed)
    report = payload['images'][0]['reports']['fence_contract']
    (tmp_path / (report['sha256']+'.xml')).write_bytes(b'<testsuites><testsuite><testcase><skipped/></testcase></testsuite></testsuites>')
    with pytest.raises(ValueError, match='not_all_passed'):
        verify(raw, key.public_key(), tmp_path, now=1000)


def test_live_publisher_exact_retry_no_extension_scope_and_revocation_floor(registry, tmp_path):
    payload, key, raw = fixture_bundle(tmp_path)
    service = CiEvidencePublisher(registry.db, audience='test', record_name='ci', record_uid='ci-uid',
        key=key, expected_suite_sha256=payload['suite_sha256'], clock=registry.clock.utc)
    feed = revocations(key)
    args = dict(request_id=uuid4(), signed_qualification=raw, reports=tmp_path,
                signed_revocations=feed, image_digests=('sha256:'+'c'*64,))
    result = service.prepare(**args)
    live = json.loads(result)
    key.public_key().verify(bytes.fromhex(live['signature']), CI_LIVE_DOMAIN+canonical(live['payload']))
    assert live['payload']['expires_at'] == 1100
    registry.clock.advance(5)
    assert service.prepare(**args) == result
    with pytest.raises(ValueError, match='scope_image_mismatch'):
        service.prepare(**{**args, 'image_digests': ('sha256:'+'d'*64,)})
    revoked = revocations(key, generation=2, revoked=(document_hash(payload),))
    with pytest.raises(ValueError, match='qualification_revoked'):
        service.prepare(**{**args, 'request_id': uuid4(), 'signed_revocations': revoked})
    with pytest.raises(ValueError, match='revocation_replay'):
        service.prepare(**args)
    # A newer feed cannot un-revoke a previously observed exact artifact.
    with pytest.raises(ValueError, match='qualification_revoked'):
        service.prepare(**{**args, 'request_id': uuid4(), 'signed_revocations': revocations(key, generation=3)})


def test_live_publisher_stale_feed_and_record_replacement_fail_closed(registry, tmp_path):
    payload, key, raw = fixture_bundle(tmp_path)
    args = dict(request_id=uuid4(), signed_qualification=raw, reports=tmp_path,
                signed_revocations=revocations(key), image_digests=('sha256:'+'c'*64,))
    config = dict(audience='test', record_name='ci', key=key,
                  expected_suite_sha256=payload['suite_sha256'], clock=registry.clock.utc)
    service = CiEvidencePublisher(registry.db, record_uid='original', **config)
    service.prepare(**args)
    with pytest.raises(ValueError, match='reprovision_required'):
        CiEvidencePublisher(registry.db, record_uid='replacement', **config).prepare(**args)
    registry.clock.advance(100)
    with pytest.raises(ValueError, match='feed_stale'):
        service.prepare(**{**args, 'request_id': uuid4()})
