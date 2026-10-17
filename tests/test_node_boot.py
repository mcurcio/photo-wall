"""Frozen cold offers, superseding boot enrollment, and weak legacy adoption. Each Pi boots its own
version (central/infra/player_versions.py).

The boot's test helpers, importable from tests/ (the e2e tracers use them too): `enroll` (a Pi's device
row, as its first boot offer claims it), `keep_version` (a Pi's kept version, as Install keeps it),
`player_versions` and `boot_service` (Central's PgPlayerVersions and the boot offer over it),
`NOTHING_READY` / `ALL_READY` (a cache with no release, or every release, ready; with releases
ingested by the real sync pass its `world.stored`: tests/test_node_release_ingest.py
`make_sync_world`, `Upstream`, `node_upload`), `FixedVersion` (a schema before migration 100),
`cold_setup` and `publish_deployment`."""
from collections.abc import Collection
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
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

from central.content_catalog.catalog import device_id_for_serial
from central.content_catalog.ports import NodeReleaseRefused
from central.db import Database
from central.fleet.node_boot import NodeBootService, NodeDeployment, claim_new_device
from central.fleet.node_commands import NodeCommands, OperatorReboot
from central.fleet.node_sessions import NodeControlConfig, NodeControlError, NodeSessions
from central.infra.player_versions import PgPlayerVersions
from central.kernel.ports import AssetReadiness, Readiness
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


def publish_deployment(db, deployment, clock) -> bool:
    """Write `deployment` through the one deployment writer (`write_deployment`, which the
    release sync's ingest calls; the ingest has its own tests). True when it already existed."""
    from central.infra.node_releases import write_deployment
    from central.kernel.assets import OriginLocator
    source = OriginLocator("https://example.invalid/base.tar", deployment.base.content_key, 256)
    with db.transaction() as conn:
        return write_deployment(conn, deployment, source, clock=clock)


class _Readiness:
    """An AssetReadiness whose every set of assets is `state`."""

    def __init__(self, state: str) -> None:
        self._state = state

    def readiness(self, tx, jobs: Collection, *, wanted: bool) -> Readiness:
        if self._state == "ready":
            return Readiness("ready", None, 0)
        return Readiness("downloading" if wanted else "not_downloaded", None, 1)

    def missing(self, tx, job) -> bool:
        return self._state != "ready"


NOTHING_READY: AssetReadiness = _Readiness("not_downloaded")  # type: ignore[assignment]
ALL_READY: AssetReadiness = _Readiness("ready")  # type: ignore[assignment]


def enroll(db: Database, serial: str, now: float) -> str:
    """The Pi `serial`'s device id, its row claimed as a first boot offer claims it."""
    device_id = device_id_for_serial(serial)
    assert device_id is not None
    with db.transaction() as conn:
        claim_new_device(conn, device_id=device_id, serial=serial, now=now)
    return device_id


def keep_version(db: Database, serial: str, deployment_id: UUID, now: float) -> str:
    """Enroll `serial` and keep `deployment_id` as its version; its device id."""
    device_id = enroll(db, serial, now)
    with db.transaction() as conn:
        conn.execute("INSERT INTO player_versions(device_id, deployment_id, previous_deployment_id, install, changed_at) "
                     "VALUES (%s, %s, NULL, NULL, to_timestamp(%s)) ON CONFLICT (device_id) DO UPDATE SET "
                     "previous_deployment_id=CASE WHEN player_versions.deployment_id <> EXCLUDED.deployment_id "
                     "THEN player_versions.deployment_id ELSE player_versions.previous_deployment_id END, "
                     "deployment_id=EXCLUDED.deployment_id", (device_id, deployment_id, now))
    return device_id


def player_versions(sessions: NodeSessions, *, readiness: AssetReadiness = NOTHING_READY,
                    publisher=None) -> PgPlayerVersions:
    return PgPlayerVersions(sessions.db, sessions.clock, readiness, publisher, NodeCommands(sessions))


def boot_service(sessions: NodeSessions, *, readiness: AssetReadiness = NOTHING_READY) -> NodeBootService:
    return NodeBootService(sessions, versions=player_versions(sessions, readiness=readiness))


class FixedVersion:
    """The boot offer's version source for a migration test's schema before migration 100 (no
    player_versions table): every Pi boots `deployment_id`, as the wall's one selection did then."""

    def __init__(self, deployment_id: UUID) -> None:
        self._deployment_id = deployment_id

    def boot_deployment_in(self, tx, serial: str) -> str | None:
        return str(self._deployment_id)


def cold_setup(registry, *, app=True):
    _seed(registry)
    sessions = NodeSessions(registry.db, registry.clock, NodeControlConfig("node-test"))
    manager = environment("2", "photo-wall-app-manager")
    selected = environment() if app else None
    refs = [manager] + ([selected] if selected else [])
    deployment = NodeDeployment(uuid4(), NodeBaseRefV2(BASE_TAG, BASE_TARBALL_SHA, BASE_SHA, 1024,
        BASE_ABI, "graphics-v1", "plugin-v1"), selected, manager, None,
        {ref.environment_sha256: "https://example.invalid/" + ref.environment_sha256 for ref in refs})
    publish_deployment(registry.db, deployment, registry.clock)
    with registry.db.transaction() as conn:
        versioned = conn.execute("SELECT to_regclass('player_versions') AS t").fetchone()["t"] is not None
    if versioned:   # SERIAL's version, as Install keeps it
        keep_version(registry.db, SERIAL, deployment.deployment_id, registry.clock.utc())
        return boot_service(sessions), sessions, deployment
    # A migration test's schema before 100: the wall's selection, as the Central of that schema kept
    # it (migration 100 turns it into every enrolled Pi's version).
    with registry.db.transaction() as conn:
        conn.execute("INSERT INTO node_boot_policy(singleton,revision,deployment_id,changed_at) "
                     "VALUES(TRUE,1,%s,%s)", (deployment.deployment_id, registry.clock.utc()))
    return NodeBootService(sessions, versions=FixedVersion(deployment.deployment_id)), sessions, deployment


def claim_for(offer, *, owner="host_core"):
    return NodeSessionClaim(offer.serial, offer.offer_id, offer.kernel_boot_id, owner, uuid4(), uuid4(),
                            uuid4().hex + uuid4().hex)


def test_frozen_offer_selection_exact_retry_and_no_app(registry):
    service, sessions, deployment = cold_setup(registry, app=False)
    request = NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64)
    offer = service.offer(request)
    assert parse_node_boot_offer(encode_node_boot_offer(offer)) == offer
    assert offer.app_status == "unconfigured" and offer.manager_fallback is None
    assert publish_deployment(registry.db, deployment, registry.clock)  # a duplicate
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
    changed = environment("3", "photo-wall-app-manager")
    sources = {**deployment.environment_sources, changed.environment_sha256: "https://example.invalid/new"}
    del sources[deployment.manager_primary.environment_sha256]
    with pytest.raises(NodeReleaseRefused, match="manager_pins_immutable"):
        publish_deployment(registry.db, replace(deployment, deployment_id=uuid4(),
                           manager_primary=changed, environment_sources=sources), registry.clock)
    with pytest.raises(ValueError, match="role_invalid"):  # a fallback must be another root
        replace(deployment, manager_fallback=deployment.manager_primary).offer(offer_id=uuid4(), audience="a",
            request=NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64), device_id=DEVICE_ID,
            generation=1, revision=1, now=1)


def test_new_boot_supersedes_prior_boot_and_revokes_its_sessions(registry):
    service, sessions, _ = cold_setup(registry)
    first = service.offer(NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64))
    claim = claim_for(first)
    grant = sessions.enroll(claim)
    commands = NodeCommands(sessions)
    request = OperatorReboot(uuid4(), grant.session_id, 1, "fixture:operator")
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
    service = boot_service(sessions)   # no version kept and no release ready
    request = NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64)
    for _ in range(2):
        with pytest.raises(NodeControlError, match="node_boot_policy_unconfigured"):
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


def _desired(registry):
    from central.content_catalog.catalog import ReleaseCatalog
    from central.infra.catalog_records import PgReleaseRecords
    from central.infra.transactions import PgTransactions
    catalog = ReleaseCatalog(releases=PgReleaseRecords(),
                             transactions=PgTransactions(registry.db), publisher=None,
                             clock=registry.clock)
    with PgTransactions(registry.db).begin() as tx:
        return catalog.desired_in(tx)


def _other(selected, digest):
    app = environment(digest)
    return replace(selected, deployment_id=uuid4(), app_environment=app,
        environment_sources={**{ref: url for ref, url in selected.environment_sources.items()
                                if ref != selected.app_environment.environment_sha256},
                             app.environment_sha256: "https://example.invalid/" + digest})


def _env(ref):
    from central.kernel.job_types import FetchSealedEnvironment
    return FetchSealedEnvironment(sha256=ref.environment_sha256)


def test_every_pis_version_and_previous_are_desired_and_older_ones_are_not(registry):
    """The keep set holds every Pi's version and every Pi's previous one (boot_policy.kept), so
    Undo on any Pi needs no download. Mutation probe: kept() drops `previous` -> this fails."""
    from central.kernel.job_types import FetchOsImage
    _, _, first = cold_setup(registry)
    second, third, fourth = _other(first, "4"), _other(first, "5"), _other(first, "6")
    for deployment in (second, third, fourth):
        publish_deployment(registry.db, deployment, registry.clock)
    jobs = _desired(registry)
    assert {_env(first.app_environment), _env(first.manager_primary),
            FetchOsImage(tarball_sha256=BASE_TARBALL_SHA)} <= jobs
    assert _env(second.app_environment) not in jobs  # published, no Pi's version: not wanted
    now = registry.clock.utc()
    keep_version(registry.db, SERIAL, second.deployment_id, now)
    keep_version(registry.db, "pi-other", fourth.deployment_id, now)
    jobs = _desired(registry)
    # SERIAL's version and previous, and the other Pi's version.
    assert {_env(second.app_environment), _env(first.app_environment), _env(fourth.app_environment)} <= jobs
    keep_version(registry.db, SERIAL, third.deployment_id, now)
    jobs = _desired(registry)
    assert {_env(third.app_environment), _env(second.app_environment), _env(fourth.app_environment)} <= jobs
    assert _env(first.app_environment) not in jobs  # no Pi's version nor previous any more
