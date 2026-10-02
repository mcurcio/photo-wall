"""Read-only HTTP inventories and signed artifacts, never a real cluster qualification."""
import json
from dataclasses import replace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from central.fleet.kubernetes_verifier import (
    KubernetesIdentity,
    KubernetesReader,
    KubernetesRolloutVerifier,
    KubernetesTopology,
    PostgresEvidenceWatermarks,
    SignedRolloutEvidence,
    _canonical,
    _hash,
)
from central.fleet.rollout_gate import RolloutEffectGate, RolloutGateError

IMAGE = 'sha256:'+'a'*64
ROLLBACK = 'sha256:'+'b'*64


def fixture_api():
    def owner(kind, uid):
        return [{'kind': kind, 'uid': uid, 'controller': True}]
    pod = {'metadata': {'name': 'central-1', 'namespace': 'photos', 'uid': 'pod-1',
                       'labels': {'app': 'photo-wall'}, 'ownerReferences': owner('ReplicaSet','rs-1')},
           'spec': {'containers': [{'name':'central','image':'mutable:tag'}]},
           'status': {'podIPs': [{'ip':'10.1.1.1'}], 'containerStatuses': [{'name':'central','ready':True,
                      'state':{'running':{}},'imageID':'ghcr.io/test/central@'+IMAGE}]}}
    service = {'metadata': {'name':'central','namespace':'photos','uid':'service-1'},
               'spec': {'selector':{'app':'photo-wall'},'type':'LoadBalancer',
                        'ports':[{'port':8000,'nodePort':30204}]}}
    endpoint = {'metadata':{'namespace':'photos','name':'central-abc','uid':'slice-1',
                           'labels':{'kubernetes.io/service-name':'central'}},
                'endpoints':[{'conditions':{'ready':True},'targetRef':{'kind':'Pod','uid':'pod-1'},
                              'addresses':['10.1.1.1']}]}
    listings = {
        '/apis/apps/v1/namespaces/photos/replicasets':[{'metadata':{'name':'central-rs','uid':'rs-1',
                                                     'ownerReferences':owner('Deployment','dep-1')}}],
        '/api/v1/namespaces/photos/pods':[pod], '/api/v1/services':[service],
        '/apis/discovery.k8s.io/v1/endpointslices':[endpoint],
        '/apis/networking.k8s.io/v1/ingresses':[], '/apis/networking.k8s.io/v1/networkpolicies':[]}
    docs = {k:{'items':v,'metadata':{}} for k,v in listings.items()}
    docs['/apis/apps/v1/namespaces/photos/deployments/central'] = {'metadata':{'uid':'dep-1','generation':1}}
    docs['/apis'] = {'groups':[{'name':'apps'}]}
    return docs


def reader(docs):
    def handle(request):
        assert request.method == 'GET'
        value = docs.get(request.url.path)
        return httpx.Response(404 if value is None else 200, json=value or {})
    return KubernetesReader(httpx.Client(base_url='https://kubernetes.test', transport=httpx.MockTransport(handle)))


def topology(docs):
    return KubernetesTopology(reader(docs), KubernetesIdentity('photos','central-1','pod-1','central','central','dep-1'))


def signed_setup(registry):
    docs = fixture_api()
    observed = topology(docs).observe()
    ci_key, guard_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    ci = {'audience':'node-test','record_uid':'ci-uid','generation':1,'state':'active','issued_at':999,'expires_at':1200,
          'image_digests':[IMAGE,ROLLBACK], 'compatibility_matrix_sha256':'1'*64,
          'fence_contract_sha256':'2'*64,'readiness_contract_sha256':'3'*64}
    guard = {'audience':'node-test','record_uid':'guard-uid','generation':1,'state':'active',
             'issued_at':999,'expires_at':1200,'mutation_not_before':1200,
             'deployment_uid':'dep-1','deployment_generation':1,'serving_image_digests':[IMAGE],
             'rollback_image_digests':[ROLLBACK],'endpoint_slice_sha256':observed.endpoints_sha256,
             'route_inventory_sha256':observed.routes_sha256,'ci_payload_sha256':_hash(ci),
             'post_expiry_policy':'retain_fenced_images_until_all_effects_reconciled'}
    def publish(role, payload):
        key = ci_key if role=='ci' else guard_key
        domain = ('photo-wall-rollout-'+role+'-v1\0').encode()
        envelope = {'payload':payload,'signature':key.sign(domain+_canonical(payload)).hex()}
        docs['/api/v1/namespaces/photos/configmaps/'+role] = {'metadata':{'uid':role+'-uid'},
            'data':{'evidence.json':json.dumps(envelope)}}
    publish('ci',ci)
    publish('guard',guard)
    evidence = SignedRolloutEvidence(reader(docs),namespace='photos',ci_record='ci',guard_record='guard',
        ci_public_key=ci_key.public_key().public_bytes_raw(),guard_public_key=guard_key.public_key().public_bytes_raw(),
        audience='node-test',watermarks=PostgresEvidenceWatermarks(registry.db),clock=registry.clock.utc)
    return docs, observed, evidence, ci, guard, publish


def test_observation_uses_actual_running_content_identity_and_direct_service():
    docs = fixture_api()
    actual = topology(docs).observe()
    assert actual.serving_images == (IMAGE,)
    docs['/api/v1/services']['items'][0]['spec']['ports'][0]['nodePort'] += 1
    assert topology(docs).observe().routes_sha256 != actual.routes_sha256
    docs['/api/v1/namespaces/photos/pods']['items'][0]['status']['containerStatuses'][0]['imageID'] = 'containerd://'+IMAGE
    with pytest.raises(RolloutGateError, match='content_identity_unknown'):
        topology(docs).observe()


@pytest.mark.parametrize('mutation', ['endpoint', 'custom_route', 'forbidden', 'page_loop','pod_replaced'])
def test_unknown_or_incomplete_inventory_refuses(mutation):
    docs = fixture_api()
    if mutation == 'endpoint':
        docs['/apis/discovery.k8s.io/v1/endpointslices']['items'][0]['endpoints'][0]['targetRef']['uid']='foreign'
    elif mutation == 'custom_route':
        docs['/apis']['groups'].append({'name':'unknown.mesh.io'})
    elif mutation == 'forbidden':
        del docs['/apis/networking.k8s.io/v1/ingresses']
    elif mutation == 'pod_replaced':
        docs['/api/v1/namespaces/photos/pods']['items'][0]['metadata']['uid']='replacement'
    else:
        docs['/api/v1/services']['metadata']['continue']='repeated-cursor'
    with pytest.raises(RolloutGateError):
        topology(docs).observe()


def test_signed_exact_image_scope_and_revocation_survive_restart(registry):
    docs, observed, evidence, ci, guard, publish = signed_setup(registry)
    assert evidence.verify(observed).serving_image_digests == (IMAGE,)
    with pytest.raises(RolloutGateError, match='topology_changed'):
        evidence.verify(replace(observed, routes_sha256='f'*64))
    revoked = {**guard,'generation':2,'state':'revoked'}
    publish('guard',revoked)
    with pytest.raises(RolloutGateError, match='stale_or_revoked'):
        evidence.verify(observed)
    publish('guard',guard)
    with pytest.raises(RolloutGateError, match='replayed'):
        evidence.verify(observed)
    replacement = docs['/api/v1/namespaces/photos/configmaps/guard']
    replacement['metadata']['uid']='new-uid'
    with pytest.raises(RolloutGateError, match='identity_invalid'):
        evidence.verify(observed)


def test_tampered_signature_wrong_image_and_expiry_refuse(registry):
    docs, observed, evidence, ci, guard, publish = signed_setup(registry)
    publish('ci',{**ci,'image_digests':[ROLLBACK],'generation':2})
    with pytest.raises(RolloutGateError, match='guard_contract_invalid'):
        evidence.verify(observed)
    registry.clock.advance(201)
    with pytest.raises(RolloutGateError, match='stale_or_revoked'):
        evidence.verify(observed)


def test_signed_scope_measurement_cannot_reuse_gate_after_same_image_route_change(registry):
    # Gate clock is actual DB time, signed fixture time is controllable; inject
    # the measured scope into existing durable gate fixture to isolate admission.
    from test_fleet_rollout_gate import _gate
    gate, _ = _gate(registry)
    opened = gate.open(expected_revision=0)
    docs, _, evidence, *_ = signed_setup(registry)
    measured = KubernetesRolloutVerifier(topology(docs), evidence).measure()
    from central.fleet.rollout_gate import MeasuredServingImage
    class Local:
        def measure(self):
            return MeasuredServingImage('photo-wall-installation-one', IMAGE, measured.measurement_ref,
                                        measured.rollout_scope_sha256)
    actual = RolloutEffectGate(registry.db, serving_verifier=Local())
    with registry.db.transaction() as conn, pytest.raises(RolloutGateError, match='measured_topology_changed'):
        actual.require_open_in(conn, expected_generation=opened.generation)


def test_measured_guard_expiry_is_rechecked_after_downstream_wait(registry):
    import time

    from test_fleet_rollout_gate import _gate, _LocalImageVerifier
    gate,_=_gate(registry)
    opened=gate.open(expected_revision=0)
    class ShortMeasurement:
        def measure(self):
            return replace(_LocalImageVerifier().measure(),verified_until=time.time()+0.1)
    actual=RolloutEffectGate(registry.db,serving_verifier=ShortMeasurement())
    with registry.db.transaction() as conn:
        admitted=actual.require_open_in(conn,expected_generation=opened.generation)
        conn.execute('SELECT pg_sleep(0.15)')
        with pytest.raises(RolloutGateError,match='gate_closed'):
            admitted.ensure_current_in(conn)
