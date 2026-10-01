"""Frozen cold offers, explicit ambiguity, and weak legacy adoption."""
from dataclasses import asdict, replace
from hashlib import sha256
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from test_fleet_attempts import (
    BASE_ABI,
    BASE_SHA,
    BASE_TAG,
    BASE_TARBALL_SHA,
    BOOT_ID,
    DEVICE_ID,
    SERIAL,
    _seed,
)
from test_fleet_rollout_gate import _gate

from central.fleet.node_boot import NodeBootService, NodeDeployment
from central.fleet.node_boot_claims import NodeBootClaims
from central.fleet.node_commands import NodeCommands, OperatorReboot
from central.fleet.node_sessions import NodeControlConfig, NodeControlError, NodeSessions
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_boot import (
    NodeBaseRefV2,
    NodeBootRequestV2,
    encode_node_boot_offer,
    parse_node_boot_offer,
)
from contracts.node_commands import NodeSessionClaim


def environment(digest="d", name="photo-wall-player"):
    return AppEnvironmentRefV2(digest * 64, 128, "e" * 64, name, "1.0.0", "arm64", "f" * 64,
                              "1" * 64, "/usr/bin/app", BASE_ABI, "graphics-v1", "plugin-v1")


def seed_verified_publication(registry, deployment):
    """Explicit verified-publication fixture; real HTTP verification has separate tests."""
    from contracts.node_release import NodeReleaseAssetV2, NodeReleaseV2, encode_node_release
    roles = [("manager-primary", deployment.manager_primary), ("manager-fallback", deployment.manager_fallback),
             ("app", deployment.app_environment)]
    assets = [NodeReleaseAssetV2("base", "node-base.tar.gz", deployment.base.content_key, 256)]
    sources = {"base": {"url": "https://example.invalid/base.tar", "sha256": deployment.base.content_key, "size": 256}}
    for role in ("boot", "node-base-deb", "node-display-deb", "build-provenance"):
        assets.append(NodeReleaseAssetV2(role, role+".bin", "8"*64, 32))
    for role, ref in roles:
        if ref is not None:
            assets.extend([NodeReleaseAssetV2(role, role+".tar", ref.environment_sha256, ref.size_bytes),
                           NodeReleaseAssetV2(role+"-deb", role+".deb", ref.deb_sha256, 16)])
            sources[role] = {"url": deployment.environment_sources[ref.environment_sha256],
                             "sha256": ref.environment_sha256, "size": ref.size_bytes}
    for asset in assets:
        sources.setdefault(asset.role, {"url": "https://example.invalid/"+asset.filename,
                                        "sha256": asset.sha256, "size": asset.size_bytes})
    revision = sha256(str(asdict(deployment)).encode()).hexdigest()[:40]
    raw = encode_node_release(NodeReleaseV2(revision, deployment.base, deployment.app_environment,
        deployment.manager_primary, deployment.manager_fallback, tuple(assets)))
    identity = sha256(raw).hexdigest()
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO node_release_catalog VALUES(%s,%s,%s,%s,%s,1000) ON CONFLICT DO NOTHING",
                     (identity, deployment.base.tag, revision, raw, Jsonb(sources)))
        conn.execute("INSERT INTO node_release_verifications VALUES(%s,1000,'test:verified-publication',%s) ON CONFLICT DO NOTHING",
                     (identity, Jsonb({"fixture": True})))


def cold_setup(registry, *, app=True):
    _seed(registry)
    with registry.db.transaction() as conn:
        conn.execute("UPDATE app_releases SET base_tarball_url=%s,base_tarball_size=256 WHERE tag=%s",
                     ("https://example.invalid/base.tar", BASE_TAG))
    sessions = NodeSessions(registry.db, registry.clock, NodeControlConfig("node-test"))
    manager = environment("2", "photo-wall-node-manager")
    selected = environment() if app else None
    refs = [manager] + ([selected] if selected else [])
    deployment = NodeDeployment(uuid4(), NodeBaseRefV2(BASE_TAG, BASE_TARBALL_SHA, BASE_SHA, 1024,
        BASE_ABI, "graphics-v1", "plugin-v1"), selected, manager, None,
        {ref.environment_sha256: "https://example.invalid/" + ref.environment_sha256 for ref in refs})
    service = NodeBootService(sessions)
    seed_verified_publication(registry, deployment)
    service.publish(deployment)
    service.select(deployment.deployment_id, 0)
    return service, sessions, deployment


def claim_for(offer, *, owner="host_core", expected_boot_id=None):
    return NodeSessionClaim(offer.serial, offer.offer_id, offer.kernel_boot_id, owner, uuid4(), uuid4(),
                            uuid4().hex + uuid4().hex, 1000, expected_boot_id=expected_boot_id)


def test_frozen_offer_selection_exact_retry_and_no_app(registry):
    service, sessions, deployment = cold_setup(registry, app=False)
    request = NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64)
    offer = service.offer(request)
    assert parse_node_boot_offer(encode_node_boot_offer(offer)) == offer
    assert offer.app_status == "unconfigured" and offer.manager_fallback is None
    assert service.publish(deployment)["duplicate"]
    service.select(deployment.deployment_id, 1)
    assert service.offer(request) == offer
    assert service.asset(offer.offer_id, "base").sha256 == BASE_SHA
    assert service.asset(offer.offer_id, "manager-primary").format == "sealed-environment-v2"
    with pytest.raises(NodeControlError, match="artifact_unconfigured"):
        service.asset(offer.offer_id, "app")
    with pytest.raises(NodeControlError, match="nonce_conflict"):
        service.offer(replace(request, boot_nonce="b" * 64))
    assert sessions.enroll(claim_for(offer)).command_eligible
    registry.clock.advance(3601)
    with pytest.raises(NodeControlError, match="offer_expired"):
        service.offer(request)


def test_manager_pins_and_environment_identity_are_immutable(registry):
    service, _, deployment = cold_setup(registry)
    changed = environment("3", "photo-wall-node-manager")
    sources = {**deployment.environment_sources, changed.environment_sha256: "https://example.invalid/new"}
    del sources[deployment.manager_primary.environment_sha256]
    with pytest.raises(NodeControlError, match="manager_pins_immutable"):
        service.publish(replace(deployment, deployment_id=uuid4(), manager_primary=changed,
                                environment_sources=sources))
    with pytest.raises(ValueError, match="role_invalid"):
        replace(deployment, manager_primary=environment()).offer(offer_id=uuid4(), audience="a",
            request=NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64), device_id=DEVICE_ID,
            generation=1, revision=1, now=1)


def test_overlapping_boots_block_both_scopes_until_operator_cas(registry):
    service, sessions, _ = cold_setup(registry)
    first = service.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64))
    claim = claim_for(first)
    grant = sessions.enroll(claim)
    gate, _ = _gate(registry)
    generation = gate.open(expected_revision=0).generation
    commands = NodeCommands(sessions, gate)
    request = OperatorReboot(uuid4(), grant.session_id, 1, "fixture:operator", generation, 31000)
    commands.request_reboot(DEVICE_ID, request)
    second = service.offer(NodeBootRequestV2(SERIAL, uuid4(), "b" * 64))
    assert not sessions.enroll(claim).command_eligible
    assert sessions.enroll(claim).command_reason == "boot_claim_conflict"
    for action in (lambda: commands.poll(grant.session_id, claim.credential),
                   lambda: commands.request_reboot(DEVICE_ID, replace(request, command_id=uuid4()))):
        with pytest.raises(NodeControlError, match="boot_claim_conflict"):
            action()
    second_claim = claim_for(second, expected_boot_id=BOOT_ID)
    second_grant = sessions.enroll(second_claim)
    assert not second_grant.command_eligible
    broker_claim = claim_for(second, owner="app_effect_broker")
    assert not sessions.enroll(broker_claim).command_eligible
    with pytest.raises(NodeControlError, match="boot_claim_conflict"):
        commands.poll(second_grant.session_id, second_claim.credential)
    with registry.db.transaction() as conn:
        revision = conn.execute("SELECT revision FROM node_boot_claim_conflicts").fetchone()["revision"]
    registry.clock.advance(1)
    NodeBootClaims(sessions).select(DEVICE_ID, generation=1, expected_revision=revision,
                                   boot_id=second.kernel_boot_id, operator_audit_ref="operator:selection")
    assert service.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64)) == first
    assert sessions.enroll(second_claim).command_eligible
    assert commands.poll(second_grant.session_id, second_claim.credential) == {"commands": []}
    with pytest.raises(NodeControlError, match="selection_conflict"):
        NodeBootClaims(sessions).select(DEVICE_ID, generation=1, expected_revision=revision,
            boot_id=BOOT_ID, operator_audit_ref="operator:stale")


def test_refusal_is_frozen_and_never_retargeted(registry):
    _seed(registry)
    sessions = NodeSessions(registry.db, registry.clock, NodeControlConfig("node-test"))
    service = NodeBootService(sessions)
    request = NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64)
    for _ in range(2):
        with pytest.raises(NodeControlError, match="policy_unconfigured"):
            service.offer(request)
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM node_boot_offers").fetchone()["n"] == 1


def test_sealed_environment_worker_and_exact_reader_hold_inode_lease(registry, tmp_path):
    import asyncio
    import os

    from test_assets_handlers import World, facts, sha

    from central.assets.handlers import FetchSealedEnvironmentHandler
    from central.assets.reader import Opened
    from central.fleet.bytes import OfferByteReader
    from central.fleet.models import OfferAsset
    from central.kernel.assets import AssetKey, AssetKind, OriginLocator
    from central.kernel.job_types import FetchSealedEnvironment
    data = b"sealed closure bytes"
    digest = sha(data)
    key = AssetKey(AssetKind.SEALED_ENVIRONMENT, digest)
    world = World(registry, tmp_path, {"https://example.invalid/environment": data})
    world.reference(key, "exact-root", OriginLocator("https://example.invalid/environment", digest, len(data)), facts(data))
    job = FetchSealedEnvironment(sha256=digest)
    handler = FetchSealedEnvironmentHandler(production=world.production, origin=world.origin)
    assert asyncio.run(handler.handle(job)) == facts(data)
    path = world.store.layout.path(key)
    class Reader:
        async def read(self, candidates):
            assert candidates.jobs == (job,)
            return Opened(job, os.open(path, os.O_RDONLY), len(data), digest)
    opened = asyncio.run(OfferByteReader(Reader()).open_exact(
        OfferAsset("environment", "v1", digest, digest, len(data), "sealed-environment-v2")))
    try:
        path.unlink()
        assert os.read(opened.fd, len(data)) == data
    finally:
        os.close(opened.fd)
