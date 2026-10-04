"""Frozen cold offers, superseding boot enrollment, and weak legacy adoption."""
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


def claim_for(offer, *, owner="host_core"):
    return NodeSessionClaim(offer.serial, offer.offer_id, offer.kernel_boot_id, owner, uuid4(), uuid4(),
                            uuid4().hex + uuid4().hex)


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
    # Offer age never refuses its own boot: a re-offer replays the frozen offer
    # and a first enrollment after a long Central outage is still admitted.
    assert service.offer(request) == offer
    assert sessions.enroll(claim_for(offer, owner="app_effect_broker")).command_eligible
    # Nor does it refuse the offer's frozen artifacts: identity and generation gate them.
    assert service.asset(offer.offer_id, "base").sha256 == BASE_SHA
    with pytest.raises(NodeControlError, match="offer_unknown"):
        service.asset(uuid4(), "base")
    with registry.db.transaction() as conn:
        conn.execute("UPDATE fleet_device_lifecycle SET generation=generation+1 WHERE device_id=%s",
                     (offer.device_id,))
    with pytest.raises(NodeControlError, match="generation_stale"):
        service.asset(offer.offer_id, "base")


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


def test_new_boot_supersedes_prior_boot_and_revokes_its_sessions(registry):
    service, sessions, _ = cold_setup(registry)
    first = service.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64))
    claim = claim_for(first)
    grant = sessions.enroll(claim)
    gate, _ = _gate(registry)
    generation = gate.open(expected_revision=0).generation
    commands = NodeCommands(sessions, gate)
    request = OperatorReboot(uuid4(), grant.session_id, 1, "fixture:operator", generation)
    commands.request_reboot(DEVICE_ID, request)
    # A second boot of the same serial needs no knowledge of its predecessor.
    second = service.offer(NodeBootRequestV2(SERIAL, uuid4(), "b" * 64))
    second_claim = claim_for(second)
    second_grant = sessions.enroll(second_claim)
    assert second_grant.command_eligible
    assert sessions.enroll(claim_for(second, owner="app_effect_broker")).command_eligible
    with pytest.raises(NodeControlError, match="superseded"):
        commands.poll(grant.session_id, claim.credential)
    with pytest.raises(NodeControlError, match="session_unavailable"):
        commands.request_reboot(DEVICE_ID, replace(request, command_id=uuid4()))
    assert commands.poll(second_grant.session_id, second_claim.credential) == {"commands": []}
    # The old boot's frozen offer still enrolls; it supersedes in turn (duplicate serials flap visibly).
    assert sessions.enroll(claim_for(first)).command_eligible
    with pytest.raises(NodeControlError, match="superseded"):
        commands.poll(second_grant.session_id, second_claim.credential)


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


def test_selected_deployment_environments_are_desired_and_unselected_are_not(registry):
    from central.content_catalog.catalog import ReleaseCatalog
    from central.infra.catalog_records import PgDeviceRecords, PgReleaseRecords
    from central.infra.transactions import PgTransactions
    from central.kernel.job_types import FetchOsImage, FetchSealedEnvironment
    service, _, selected = cold_setup(registry)
    other_app = environment("4")
    unselected = replace(selected, deployment_id=uuid4(), app_environment=other_app,
        environment_sources={**{ref: url for ref, url in selected.environment_sources.items()
                                if ref != selected.app_environment.environment_sha256},
                             other_app.environment_sha256: "https://example.invalid/other"})
    seed_verified_publication(registry, unselected)
    service.publish(unselected)
    catalog = ReleaseCatalog(releases=PgReleaseRecords(), devices=PgDeviceRecords(), stored=None,
                             transactions=PgTransactions(registry.db), publisher=None,
                             clock=registry.clock)

    def desired():
        with PgTransactions(registry.db).begin() as tx:
            return catalog.desired_in(tx)

    def env(ref):
        return FetchSealedEnvironment(sha256=ref.environment_sha256)

    jobs = desired()
    assert {env(selected.app_environment), env(selected.manager_primary),
            FetchOsImage(tarball_sha256=BASE_TARBALL_SHA)} <= jobs
    assert env(other_app) not in jobs
    service.select(unselected.deployment_id, 1)
    jobs = desired()
    assert env(other_app) in jobs and env(selected.manager_primary) in jobs
    assert env(selected.app_environment) not in jobs


class SelectingPublisher:
    """Records each job with the selection its transaction sees; `fail` refuses the publish."""

    def __init__(self, *, fail=False):
        self.published, self.fail = [], fail

    def publish(self, job, *, within, retry_terminal=False):
        from central.infra.transactions import pg_connection
        if self.fail:
            raise RuntimeError("queue down")
        row = pg_connection(within).execute(
            "SELECT deployment_id FROM node_boot_policy WHERE singleton").fetchone()
        self.published.append((job, row["deployment_id"]))


def test_selecting_a_deployment_warms_the_cache_in_the_same_transaction(registry):
    from central.kernel.job_types import Prefetch
    service, sessions, selected = cold_setup(registry)
    other = replace(selected, deployment_id=uuid4())
    seed_verified_publication(registry, other)
    service.publish(other)
    publisher = SelectingPublisher()
    boots = NodeBootService(sessions, publisher=publisher)
    # A conflicting selection writes nothing and warms nothing.
    with pytest.raises(NodeControlError, match="node_boot_policy_conflict"):
        boots.select(other.deployment_id, 0)
    assert publisher.published == []
    boots.select(other.deployment_id, 1)
    # Published inside the selecting transaction: it already sees the new selection.
    assert publisher.published == [(Prefetch(), other.deployment_id)]
    # A publish that fails rolls the selection back with it.
    with pytest.raises(RuntimeError, match="queue down"):
        NodeBootService(sessions, publisher=SelectingPublisher(fail=True)).select(
            selected.deployment_id, 2)
    with registry.db.transaction() as conn:
        row = conn.execute("SELECT revision, deployment_id FROM node_boot_policy").fetchone()
    assert (row["revision"], row["deployment_id"]) == (2, other.deployment_id)
