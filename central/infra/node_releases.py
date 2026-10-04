"""PostgreSQL node release records: the ONE deployment writer, the ONE select writer, the
per-tag observations and the wanted deployments (auto-ingest design §6).

Tables: `node_release_catalog` (058, immutable), `node_release_observations` (065),
`node_deployments` / `node_deployment_assets` / `node_base_managers` / `node_environment_catalog`
/ `node_boot_policy` / `node_boot_offers` (054, 065).

* **Deployment writer** (`write_deployment`): used by the release sync's ingest and by the hand
  route `POST /v1/operator/node/deployments`. Takes the asset-roots lock; an existing id with the
  same canonical bytes is a `duplicate`, with other bytes a refusal.
* **Select writer** (`select_deployment`): used by the operator's `PUT boot-policy` and by the
  first-run auto-select (expected revision 0, so it can only create the row). Records the
  previous deployment when the selection changes to a different one.
* **Observations** (`_observe`, from `ingest` and `record_problem`): take the asset-roots lock
  too; a tag's stable flag and manifest decide whether it is in the window.
* **Wanted deployments** (`wanted_deployments`): the window (newest 3 stable tags with a
  deployment), the selected and previous deployments, and the content of every V2 boot offer
  younger than `OFFER_TTL_SECONDS`. The ONE definition of "wanted"; `fleet_desired_assets`
  turns it into keys.

Every writer raises `NodeReleaseRefused` before its first write (or the caller's transaction
rolls back), never a bare database error, for a release's own conflicts.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any, Final
from uuid import UUID

from psycopg.types.json import Jsonb

from central.content_catalog.boot_policy import newest_first, window
from central.content_catalog.deployment import (
    OFFER_TTL_SECONDS,
    NodeDeployment,
    deployment_for_release,
    deployment_id_for,
    encode_node_deployment,
)
from central.content_catalog.ports import NodeReleaseRefused, StableDeployment
from central.infra.asset_records import PgAssetRecords
from central.infra.asset_roots import lock_fleet_assets_in
from central.infra.transactions import PgTransaction, pg_connection
from central.infra.upstream_guard import not_older
from central.kernel.assets import AssetKey, AssetKind, AssetReference, OriginLocator
from central.kernel.job_types import AssetJob, FetchOsImage, FetchSealedEnvironment
from central.kernel.ports import PublishedRelease
from central.kernel.transactions import Transaction
from contracts.node_boot import parse_node_boot_offer
from contracts.node_release import parse_node_release
from contracts.time import Clock

LOG = logging.getLogger("central.infra.node_releases")

POLICY_CONFLICT: Final = "node_boot_policy_conflict"
DEPLOYMENT_UNKNOWN: Final = "node_deployment_unknown"
IDENTITY_CONFLICT: Final = "node_release_identity_conflict"


class _Older(Exception):
    """An observation older than the stored one: its savepoint rolls back, nothing is written."""


def deployment_job(kind: str, identity: str) -> AssetJob:
    """The fetch job of one `node_deployment_assets` row."""
    if kind == AssetKind.OS_IMAGE:
        return FetchOsImage(tarball_sha256=identity)
    if kind == AssetKind.SEALED_ENVIRONMENT:
        return FetchSealedEnvironment(sha256=identity)
    raise ValueError(f"not a node deployment asset kind: {kind!r}")


def write_deployment(conn: Any, deployment: NodeDeployment,
                     base_source: Callable[[], OriginLocator], *, clock: Clock) -> bool:
    """Write one deployment and its asset references; True when it already existed (duplicate).

    Refuses (`NodeReleaseRefused`) an existing id with other bytes, a base whose manager pins
    differ from the ones it was first published with, and an environment digest already
    catalogued with another reference. `base_source` names where the base tarball is
    downloaded; it is asked after those checks and may refuse too (`hand_provenance`)."""
    canonical = encode_node_deployment(deployment)
    lock_fleet_assets_in(conn)
    prior = conn.execute("SELECT document FROM node_deployments WHERE deployment_id=%s",
                         (deployment.deployment_id,)).fetchone()
    if prior:
        if bytes(prior["document"]) != canonical:
            raise NodeReleaseRefused("node_deployment_identity_conflict")
        return True
    base = deployment.base
    manager_pins = {"base_abi": base.base_abi, "graphics_abi": base.graphics_abi,
                    "plugin_abi": base.plugin_abi,
                    "primary": asdict(deployment.manager_primary),
                    "fallback": asdict(deployment.manager_fallback) if deployment.manager_fallback else None}
    pins = conn.execute("SELECT document FROM node_base_managers WHERE base_content_key=%s",
                        (base.content_key,)).fetchone()
    if pins and pins["document"] != manager_pins:
        raise NodeReleaseRefused("node_base_manager_pins_immutable")
    environments = deployment.environments()
    catalogued = {}
    for item in environments:
        old = conn.execute("SELECT reference FROM node_environment_catalog WHERE environment_sha256=%s",
                           (item.environment_sha256,)).fetchone()
        if old and old["reference"] != asdict(item):
            raise NodeReleaseRefused("node_environment_identity_conflict")
        catalogued[item.environment_sha256] = old is not None
    source = base_source()
    if not pins:
        conn.execute("INSERT INTO node_base_managers(base_content_key,document) VALUES(%s,%s)",
                     (base.content_key, Jsonb(manager_pins)))
    conn.execute("INSERT INTO node_deployments(deployment_id,document,published_at) VALUES(%s,%s,%s)",
                 (deployment.deployment_id, canonical, clock.utc()))
    owner = "node-deployment:" + deployment.deployment_id.hex
    references = [(AssetKey(AssetKind.OS_IMAGE, base.content_key),
                   AssetReference(owner, source, base.size_bytes, base.squashfs_sha256))]
    for item in environments:
        if not catalogued[item.environment_sha256]:
            conn.execute("INSERT INTO node_environment_catalog(environment_sha256,reference) VALUES(%s,%s)",
                         (item.environment_sha256, Jsonb(asdict(item))))
        references.append((AssetKey(AssetKind.SEALED_ENVIRONMENT, item.environment_sha256),
            AssetReference(owner, OriginLocator(deployment.environment_sources[item.environment_sha256],
                item.environment_sha256, item.size_bytes), item.size_bytes, item.environment_sha256)))
    records, tx = PgAssetRecords(clock), PgTransaction(conn)
    for key, reference in references:
        records.reference(tx, key, reference)
        conn.execute("INSERT INTO node_deployment_assets(deployment_id,kind,identity,owner) VALUES(%s,%s,%s,%s)",
                     (deployment.deployment_id, key.kind.value, key.identity, owner))
    return False


def hand_provenance(conn: Any, deployment: NodeDeployment) -> OriginLocator:
    """Where a hand-published deployment's base comes from: a catalogued release (any observed
    `node_release_catalog` row) with the same base and managers, whose environments name the
    same sources. Refused when no observed release provides them."""
    base_source = None
    environments = {}
    for row in conn.execute("SELECT document,asset_locators FROM node_release_catalog").fetchall():
        manifest = parse_node_release(bytes(row["document"]))
        locators = row["asset_locators"]
        if (manifest.base == deployment.base and manifest.manager_primary == deployment.manager_primary
                and manifest.manager_fallback == deployment.manager_fallback):
            base_source = OriginLocator(locators["base"]["url"], deployment.base.content_key,
                                        locators["base"]["size"])
        for role, ref in (("app", manifest.app_environment), ("manager-primary", manifest.manager_primary),
                          ("manager-fallback", manifest.manager_fallback)):
            if ref is not None:
                environments[(ref.environment_sha256, locators[role]["url"])] = ref
    if base_source is None:
        raise NodeReleaseRefused("node_base_release_provenance_unavailable")
    for ref in deployment.environments():
        if environments.get((ref.environment_sha256,
                             deployment.environment_sources[ref.environment_sha256])) != ref:
            raise NodeReleaseRefused("node_environment_release_provenance_unavailable")
    return base_source


def select_deployment(conn: Any, deployment_id: UUID, expected_revision: int, *,
                      now: float) -> int:
    """Compare-and-set the boot selection; the new revision. Holds the asset-roots lock and the
    policy row's lock before the revision compare, so expected revision 0 can only create the
    row. The outgoing deployment becomes `previous_deployment_id` when the selection changes to
    a different deployment; re-selecting the same one keeps the previous."""
    lock_fleet_assets_in(conn)
    row = conn.execute("SELECT revision,deployment_id,previous_deployment_id FROM node_boot_policy "
                       "WHERE singleton FOR UPDATE").fetchone()
    current = row["revision"] if row else 0
    if current != expected_revision:
        raise NodeReleaseRefused(POLICY_CONFLICT)
    if not conn.execute("SELECT 1 FROM node_deployments WHERE deployment_id=%s", (deployment_id,)).fetchone():
        raise NodeReleaseRefused(DEPLOYMENT_UNKNOWN, "not_found")
    previous = None
    if row:
        previous = (row["deployment_id"] if row["deployment_id"] != deployment_id
                    else row["previous_deployment_id"])
    conn.execute("INSERT INTO node_boot_policy(singleton,revision,deployment_id,changed_at,previous_deployment_id) "
                 "VALUES(TRUE,%s,%s,%s,%s) ON CONFLICT(singleton) DO UPDATE SET revision=EXCLUDED.revision,"
                 "deployment_id=EXCLUDED.deployment_id,changed_at=EXCLUDED.changed_at,"
                 "previous_deployment_id=EXCLUDED.previous_deployment_id",
                 (current + 1, deployment_id, now, previous))
    return current + 1


@dataclass(frozen=True, slots=True)
class WantedDeployments:
    """What the cache must keep for node boots (the one definition, design §6.2)."""

    window: tuple[tuple[str, UUID], ...]  # (tag, deployment), newest release first
    selected: UUID | None
    previous: UUID | None
    offer_jobs: frozenset[AssetJob]  # the base and environments of every live V2 offer

    @property
    def deployments(self) -> frozenset[UUID]:
        """Every wanted deployment named by id (window, selected, previous)."""
        named = {deployment for _, deployment in self.window}
        named.update(item for item in (self.selected, self.previous) if item is not None)
        return frozenset(named)


def _stable_observed(conn: Any) -> dict[str, str]:
    """tag -> its last good manifest digest, for every stable observed tag that has one."""
    rows = conn.execute("SELECT tag, manifest_sha256 FROM node_release_observations "
                        "WHERE NOT is_prerelease AND manifest_sha256 IS NOT NULL").fetchall()
    return {row["tag"]: row["manifest_sha256"] for row in rows}


def _deployment_ids(conn: Any, manifests: dict[str, str]) -> dict[str, UUID]:
    """tag -> the deployment its manifest became (`deployment_id_for`)."""
    if not manifests:
        return {}
    rows = conn.execute("SELECT manifest_sha256, document FROM node_release_catalog "
                        "WHERE manifest_sha256 = ANY(%s)", (sorted(set(manifests.values())),)).fetchall()
    with_app = {row["manifest_sha256"]: parse_node_release(bytes(row["document"])).app_environment
                is not None for row in rows}
    return {tag: deployment_id_for(sha, with_app[sha]) for tag, sha in manifests.items()}


def wanted_deployments(conn: Any, *, now: float) -> WantedDeployments:
    policy = conn.execute("SELECT deployment_id, previous_deployment_id FROM node_boot_policy "
                          "WHERE singleton").fetchone()
    observed = _stable_observed(conn)
    tags = window(observed)
    ids = _deployment_ids(conn, {tag: observed[tag] for tag in tags})
    offer_jobs: set[AssetJob] = set()
    for row in conn.execute("SELECT offer_payload FROM node_boot_offers WHERE offer_payload IS NOT NULL "
                            "AND created_at > %s", (now - OFFER_TTL_SECONDS,)).fetchall():
        try:
            offer = parse_node_boot_offer(bytes(row["offer_payload"]))
        except ValueError:  # written by `NodeBootService.offer` through the same codec
            LOG.warning("a stored node boot offer does not parse; its files are not kept")
            continue
        offer_jobs.add(FetchOsImage(tarball_sha256=offer.base.content_key))
        offer_jobs.update(FetchSealedEnvironment(sha256=ref.environment_sha256) for ref in (
            offer.app_environment, offer.manager_primary, offer.manager_fallback) if ref is not None)
    return WantedDeployments(tuple((tag, ids[tag]) for tag in tags),
                             policy["deployment_id"] if policy else None,
                             policy["previous_deployment_id"] if policy else None,
                             frozenset(offer_jobs))


def deployment_jobs(conn: Any, deployments) -> dict[UUID, frozenset[AssetJob]]:
    """Each deployment's asset jobs (`node_deployment_assets`)."""
    ids = sorted(set(deployments), key=str)
    if not ids:
        return {}
    found: dict[UUID, set[AssetJob]] = {deployment: set() for deployment in ids}
    for row in conn.execute("SELECT deployment_id, kind, identity FROM node_deployment_assets "
                            "WHERE deployment_id = ANY(%s)", (ids,)).fetchall():
        found[row["deployment_id"]].add(deployment_job(row["kind"], row["identity"]))
    return {deployment: frozenset(jobs) for deployment, jobs in found.items()}


_OBSERVE = ("INSERT INTO node_release_observations(tag,is_prerelease,manifest_sha256,problem,"
            "upstream_changed_at,upstream_asset_id,observed_at) "
            "VALUES(%(tag)s,%(is_prerelease)s,%(manifest)s,%(problem)s,%(changed_at)s,%(asset_id)s,%(now)s) "
            "ON CONFLICT(tag) DO UPDATE SET is_prerelease=EXCLUDED.is_prerelease,"
            "manifest_sha256=COALESCE(EXCLUDED.manifest_sha256, node_release_observations.manifest_sha256),"
            "problem=EXCLUDED.problem,upstream_changed_at=EXCLUDED.upstream_changed_at,"
            "upstream_asset_id=EXCLUDED.upstream_asset_id,observed_at=EXCLUDED.observed_at "
            f"WHERE {not_older('node_release_observations')} RETURNING tag")


def _observe(conn: Any, release: PublishedRelease, *, manifest: str | None, problem: str | None,
             now: float) -> bool:
    """Upsert the tag's observation under the asset-roots lock: `is_prerelease` and
    `manifest_sha256` decide window membership (`_stable_observed`)."""
    lock_fleet_assets_in(conn)
    version = release.node_version
    return conn.execute(_OBSERVE, {
        "tag": release.tag, "is_prerelease": release.is_prerelease, "manifest": manifest,
        "problem": problem, "now": now,
        "changed_at": None if version is None else version.changed_at,
        "asset_id": None if version is None else version.asset_id,
    }).fetchone() is not None


class PgNodeReleaseRecords:
    """Implements `content_catalog.ports.NodeReleaseRecords`."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def ingest(self, tx: Transaction, release: PublishedRelease, *, now: float) -> bool:
        publication = release.node_publication
        if publication is None:
            raise ValueError("no_node_publication")
        conn = pg_connection(tx)
        lock_fleet_assets_in(conn)  # first: the writer below takes it too (lock order)
        manifest = parse_node_release(publication.manifest)
        identity = sha256(publication.manifest).hexdigest()
        try:
            with conn.transaction():  # an older observation rolls back the catalog row too
                conn.execute("INSERT INTO node_release_catalog VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                             (identity, release.tag, manifest.revision, publication.manifest,
                              Jsonb({role: asdict(locator) for role, locator in publication.assets}), now))
                prior = conn.execute("SELECT manifest_sha256, asset_locators FROM node_release_catalog "
                                     "WHERE tag=%s AND revision=%s", (release.tag, manifest.revision)).fetchone()
                if prior is None or prior["manifest_sha256"] != identity:
                    raise NodeReleaseRefused(IDENTITY_CONFLICT)
                if not _observe(conn, release, manifest=identity, problem=None, now=now):
                    raise _Older
                locators = prior["asset_locators"]
                base = locators["base"]
                source = OriginLocator(base["url"], manifest.base.content_key, base["size"])
                write_deployment(conn, deployment_for_release(identity, manifest, locators),
                                 lambda: source, clock=self._clock)
        except _Older:
            return False
        return True

    def record_problem(self, tx: Transaction, release: PublishedRelease, problem: str, *,
                       now: float) -> bool:
        return _observe(pg_connection(tx), release, manifest=None, problem=problem, now=now)

    def selection_exists(self, tx: Transaction) -> bool:
        return pg_connection(tx).execute(
            "SELECT 1 FROM node_boot_policy WHERE singleton").fetchone() is not None

    def stable_deployments(self, tx: Transaction) -> tuple[StableDeployment, ...]:
        conn = pg_connection(tx)
        ids = _deployment_ids(conn, _stable_observed(conn))
        jobs = deployment_jobs(conn, ids.values())
        return tuple(StableDeployment(tag, str(ids[tag]), jobs[ids[tag]])
                     for tag in newest_first(ids))

    def select_first(self, tx: Transaction, deployment_id: str, *, now: float) -> bool:
        try:
            select_deployment(pg_connection(tx), UUID(deployment_id), 0, now=now)
        except NodeReleaseRefused as refused:
            if refused.reason == POLICY_CONFLICT:
                return False
            raise
        return True
