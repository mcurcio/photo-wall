"""Frozen cold offers and their artifacts. Each Pi boots its own
version (central/infra/player_versions.py).

The boot's test helpers, importable from tests/ (the e2e tracers use them too): `enroll` (a Pi's device
row, as its first boot offer claims it), `keep_version` (a Pi's kept version, as Install keeps it),
`player_versions` and `boot_service` (Central's PgPlayerVersions and the boot offer over it),
`NOTHING_READY` / `ALL_READY` (a cache with no release, or every release, ready; with releases
ingested by the real sync pass its `world.stored`: tests/test_node_release_ingest.py
`make_sync_world`, `Upstream`, `node_upload`), `cold_setup`, `released` and `publish_deployment` (a hand-built deployment as a
catalogued release's)."""
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
from central.fleet.models import NodeControlError
from central.fleet.node_boot import (
    NODE_AUDIENCE,
    NodeBootConfig,
    NodeBootService,
    NodeDeployment,
    claim_new_device,
)
from central.infra.player_versions import PgPlayerVersions
from central.kernel.ports import AssetReadiness, Readiness
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_boot import (
    NodeBaseRefV2,
    NodeBootRequestV2,
    encode_node_boot_offer,
    parse_node_boot_offer,
)
from contracts.time import Clock


def environment(digest="d", name="photo-wall-player"):
    return AppEnvironmentRefV2(digest * 64, 128, "e" * 64, name, "1.0.0", "arm64", "f" * 64,
                              "1" * 64, "/usr/bin/app", BASE_ABI, "graphics-v1", "plugin-v1")


def _release_of(deployment) -> tuple[bytes, dict]:
    """The release manifest whose deployment carries `deployment`'s base and environments, and
    its asset locators (each environment's url from `environment_sources`)."""
    from hashlib import sha256

    from contracts.node_release import NodeReleaseAssetV2, NodeReleaseV2, encode_node_release
    base = deployment.base
    files = {"base": (base.content_key, base.size_bytes)}
    for role in ("boot", "node-base-deb", "node-display-deb", "build-provenance"):
        files[role] = (sha256(f"{role}:{base.content_key}".encode()).hexdigest(), 1)
    for role, ref in (("manager-primary", deployment.manager_primary), ("app", deployment.app_environment),
                      ("manager-fallback", deployment.manager_fallback)):
        if ref is not None:
            files[role] = (ref.environment_sha256, ref.size_bytes)
            files[role + "-deb"] = (ref.deb_sha256, 1)
    release = NodeReleaseV2("a" * 40, base, deployment.app_environment, deployment.manager_primary,
                            deployment.manager_fallback,
                            tuple(NodeReleaseAssetV2(role, f"{role}.bin", sha, size)
                                  for role, (sha, size) in files.items()))
    locators = {role: {"url": deployment.environment_sources.get(sha, f"https://example.invalid/{role}.bin"),
                       "sha256": sha, "size": size} for role, (sha, size) in files.items()}
    return encode_node_release(release), locators


def released(deployment):
    """`deployment` as the release that carries it builds it: the same contents under the id its
    manifest derives (`deployment_id_for`). Every stored deployment is a release's
    (`write_deployment` refuses any other), so tests build theirs with this."""
    from hashlib import sha256

    from central.content_catalog.deployment import deployment_for_release
    from contracts.node_release import parse_node_release
    manifest, locators = _release_of(deployment)
    return deployment_for_release(sha256(manifest).hexdigest(), parse_node_release(manifest), locators)


def publish_deployment(db, deployment, clock, *, tag: str | None = None, source=None) -> bool:
    """Catalogue the release that builds `deployment` (a `released` one) under `tag` (default:
    its base's tag, or `<base tag>-r<digest>` once another release holds that) and write the deployment through the one deployment writer
    (`write_deployment`, which the release sync's ingest calls; the ingest has its own tests).
    `source` is where the base tarball downloads (default: an invalid address). True when it
    already existed. The release is a pre-release, so it is no Pi's default and
    outside the window: only a Pi's version (`keep_version`) makes it wanted."""
    from hashlib import sha256

    from psycopg.types.json import Jsonb

    from central.infra.node_releases import write_deployment
    from central.kernel.assets import OriginLocator
    assert released(deployment) == deployment, "publish a released() deployment"
    manifest, locators = _release_of(deployment)
    now = clock.utc()
    source = source or OriginLocator("https://example.invalid/base.tar", deployment.base.content_key, 256)
    digest = sha256(manifest).hexdigest()
    with db.transaction() as conn:
        if tag is None:
            taken = conn.execute("SELECT 1 FROM node_releases WHERE tag=%s AND manifest_sha256<>%s",
                                 (deployment.base.tag, digest)).fetchone()
            tag = f"{deployment.base.tag}-r{digest[:12]}" if taken else deployment.base.tag
        conn.execute("INSERT INTO node_releases(tag,is_prerelease,observed_at,manifest_sha256,revision,document,"
                     "asset_locators,discovered_at,created_at,created_by,changed_at,changed_by) "
                     "VALUES(%s,true,%s,%s,%s,%s,%s,%s,%s,'central',%s,'central') "
                     "ON CONFLICT(manifest_sha256) DO NOTHING",
                     (tag, now, digest, "a" * 40, manifest,
                      Jsonb(locators), now, now, now))
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
        conn.execute("UPDATE devices SET previous_deployment_id=CASE WHEN deployment_id <> %(deployment)s "
                     "THEN deployment_id ELSE previous_deployment_id END, deployment_id=%(deployment)s, "
                     "version_changed_at=%(now)s WHERE device_id=%(device)s",
                     {"device": device_id, "deployment": deployment_id, "now": now})
    return device_id


def player_versions(db: Database, clock: Clock, *, readiness: AssetReadiness = NOTHING_READY,
                    publisher=None) -> PgPlayerVersions:
    return PgPlayerVersions(db, clock, readiness, publisher)


def boot_service(db: Database, clock: Clock, *, readiness: AssetReadiness = NOTHING_READY) -> NodeBootService:
    return NodeBootService(db, clock, NodeBootConfig(), versions=player_versions(db, clock, readiness=readiness))


def cold_setup(registry, *, app=True):
    """(the boot offer, the deployment the Pi `SERIAL` keeps as its version)."""
    _seed(registry)
    manager = environment("2", "photo-wall-app-manager")
    selected = environment() if app else None
    refs = [manager] + ([selected] if selected else [])
    deployment = released(NodeDeployment(uuid4(), NodeBaseRefV2(BASE_TAG, BASE_TARBALL_SHA, BASE_SHA, 1024,
        BASE_ABI, "graphics-v1", "plugin-v1"), selected, manager, None,
        {ref.environment_sha256: "https://example.invalid/" + ref.environment_sha256 for ref in refs}))
    publish_deployment(registry.db, deployment, registry.clock)
    keep_version(registry.db, SERIAL, deployment.deployment_id, registry.clock.utc())   # as Install keeps it
    return boot_service(registry.db, registry.clock), deployment


def test_frozen_offer_selection_exact_retry_and_no_app(registry):
    service, deployment = cold_setup(registry, app=False)
    request = NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64)
    offer = service.offer(request)
    assert parse_node_boot_offer(encode_node_boot_offer(offer)) == offer
    assert offer.installation_audience == NODE_AUDIENCE == "photo-wall"
    assert offer.app_status == "unconfigured" and offer.manager_fallback is None
    assert publish_deployment(registry.db, deployment, registry.clock)  # a duplicate
    assert service.offer(request) == offer
    assert service.asset(offer.offer_id, "base").sha256 == BASE_SHA
    assert service.asset(offer.offer_id, "manager-primary").format == "sealed-environment-v2"
    with pytest.raises(NodeControlError, match="artifact_unconfigured"):
        service.asset(offer.offer_id, "app")
    with pytest.raises(NodeControlError, match="nonce_conflict"):
        service.offer(replace(request, boot_nonce="b" * 64))
    registry.clock.advance(3601)
    # Offer age never refuses its own boot: a re-offer replays the frozen offer.
    assert service.offer(request) == offer
    # Nor does it refuse the offer's frozen artifacts: identity and generation gate them.
    assert service.asset(offer.offer_id, "base").sha256 == BASE_SHA
    with pytest.raises(NodeControlError, match="offer_unknown"):
        service.asset(uuid4(), "base")
    with registry.db.transaction() as conn:
        conn.execute("UPDATE devices SET generation=generation+1 WHERE device_id=%s",
                     (offer.device_id,))
    with pytest.raises(NodeControlError, match="generation_stale"):
        service.asset(offer.offer_id, "base")


def test_manager_pins_and_environment_identity_are_immutable(registry):
    service, deployment = cold_setup(registry)
    changed = environment("3", "photo-wall-app-manager")
    sources = {**deployment.environment_sources, changed.environment_sha256: "https://example.invalid/new"}
    del sources[deployment.manager_primary.environment_sha256]
    with pytest.raises(NodeReleaseRefused, match="manager_pins_immutable"):
        publish_deployment(registry.db, released(replace(deployment, manager_primary=changed,
                           environment_sources=sources)), registry.clock, tag="v1.0.1")
    with pytest.raises(ValueError, match="role_invalid"):  # a fallback must be another root
        replace(deployment, manager_fallback=deployment.manager_primary).offer(offer_id=uuid4(), audience="a",
            request=NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64), device_id=DEVICE_ID,
            generation=1, revision=1, now=1)


def test_refusal_is_frozen_and_never_retargeted(registry):
    _seed(registry)
    service = boot_service(registry.db, registry.clock)   # no version kept and no release ready
    request = NodeBootRequestV2(SERIAL, BOOT_ID, "a" * 64)
    for _ in range(2):
        with pytest.raises(NodeControlError, match="node_boot_policy_unconfigured"):
            service.offer(request)
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM node_boot_offers").fetchone()["n"] == 1


def test_a_first_boot_offer_creates_its_device_at_generation_one(registry):
    """No trigger and no default writes a device's generation: the boot offer's own insert does
    (decision 0034, S2e). Probe: leave `generation` out of `claim_new_device`'s insert -> NOT NULL
    refuses the offer."""
    serial = "0123456789abcdef"
    device_id = device_id_for_serial(serial)
    with pytest.raises(NodeControlError, match="node_boot_policy_unconfigured"):  # no version yet
        boot_service(registry.db, registry.clock).offer(NodeBootRequestV2(serial, BOOT_ID, "a" * 64))
    now = registry.clock.utc()
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT serial, generation, retired_at, created_at, created_by, changed_at, "
                            "changed_by FROM devices WHERE device_id=%s", (device_id,)).fetchone() == {
            "serial": serial, "generation": 1, "retired_at": None, "created_at": now,
            "created_by": "node", "changed_at": now, "changed_by": "node"}
        assert conn.execute("SELECT device_generation, created_by FROM node_boot_offers WHERE "
                            "device_id=%s", (device_id,)).fetchone() == {
            "device_generation": 1, "created_by": "node"}


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
    return released(replace(selected, app_environment=app,
        environment_sources={**{ref: url for ref, url in selected.environment_sources.items()
                                if ref != selected.app_environment.environment_sha256},
                             app.environment_sha256: "https://example.invalid/" + digest}))


def _env(ref):
    from central.kernel.job_types import FetchSealedEnvironment
    return FetchSealedEnvironment(sha256=ref.environment_sha256)


def test_every_pis_version_and_previous_are_desired_and_older_ones_are_not(registry):
    """The keep set holds every Pi's version and every Pi's previous one (boot_policy.kept), so
    Undo on any Pi needs no download. Mutation probe: kept() drops `previous` -> this fails."""
    from central.kernel.job_types import FetchOsImage
    _, first = cold_setup(registry)
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


def test_a_pi_is_never_refused_for_volume(registry):
    """Decision 0034 (class C; owner, 2026-10-07: UX over security): 130 boot offers for one
    serial in one UTC day, past the old per-device cap of 128, each answer an offer. Mutation
    probe (C1): reinstate a 128-a-day per-device offer cap in `NodeBootService.offer` -> the 129th
    offer is refused."""
    boots, _ = cold_setup(registry)
    for _ in range(130):
        offer = boots.offer(NodeBootRequestV2(SERIAL, uuid4(), uuid4().hex + uuid4().hex))
        assert offer.serial == SERIAL
        registry.clock.advance(1)
