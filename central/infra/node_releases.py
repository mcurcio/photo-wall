"""PostgreSQL node release records: the ONE deployment writer, each tag's release row and the
wanted deployments (auto-ingest design §6; a version per Pi, Q2 A, 2026-10-16).

Table: `node_releases` (121b), one row per tag: its upstream observation, its catalogue entry (the
manifest, NULL until a good one arrives) and its deployment (NULL until written); each Pi's
version is on its `devices` row (121, central/infra/player_versions.py). A deployment's files are
its `asset_references` rows, owner `node-deployment:<hex>`.

* **Deployment writer** (`write_deployment`): used by the release sync's ingest. An existing id
  with the same canonical bytes is a `duplicate`, with other bytes a refusal. A base's manager pins
  and an environment's reference are read from the other stored deployment documents: never a
  second copy.
* **Observations** (`_OBSERVED`, from `ingest` and `record_problem`): a tag's stable flag and
  deployment decide whether it is in the window. A tag once catalogued keeps its manifest: any
  other one is `node_release_identity_conflict`.
* **Wanted deployments** (`wanted_deployments`): the window (newest 3 stable tags with a
  deployment), every Pi's version and every Pi's previous one (`boot_policy.kept`). The ONE
  definition of "wanted"; `fleet_desired_assets` turns it into keys.

No writer here takes a lock for the cache cleaner: the cleaner re-reads the desired set right
before each unlink (mark and sweep, `central/assets/maintenance.py`), and a file removed anyway
is restored by read-through. Every writer raises `NodeReleaseRefused` before its first write (or
the caller's transaction rolls back), never a bare database error, for a release's own conflicts.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any, Final
from uuid import UUID

from psycopg.types.json import Jsonb

from central.content_catalog.boot_policy import kept, newest_first, window
from central.content_catalog.deployment import (
    NodeDeployment,
    deployment_for_release,
    deployment_id_for,
    encode_node_deployment,
    parse_node_deployment,
)
from central.content_catalog.ports import NodeReleaseRefused, StableDeployment
from central.infra.asset_records import PgAssetRecords
from central.infra.transactions import PgTransaction, pg_connection
from central.infra.upstream_guard import not_older
from central.kernel.assets import AssetKey, AssetKind, AssetReference, OriginLocator
from central.kernel.job_types import AssetJob, FetchOsImage, FetchSealedEnvironment
from central.kernel.ports import NODE_RELEASE_INVALID, PublishedRelease
from central.kernel.transactions import Transaction
from contracts.app_environment import AppEnvironmentRefV2
from contracts.node_release import NodeReleaseV2, parse_node_release
from contracts.time import Clock

IDENTITY_CONFLICT: Final = "node_release_identity_conflict"
# Serializes deployment writers with each other only: taken alone, so a release sync never waits on
# a device's requests (the device fence, `central/fleet/device_fence.py`) and none waits on it.
RELEASE_WRITER_LOCK: Final = 734118330
_OWNER: Final = "node-deployment:"


class _Older(Exception):
    """An observation older than the stored one: its savepoint rolls back, nothing is written."""


@contextmanager
def _release_data() -> Iterator[None]:
    """Where a release's own data error starts: the pure parse / build / encode of its manifest.
    A `ValueError`, `KeyError` or `TypeError` there is that release's `NODE_RELEASE_INVALID`;
    a database error is never raised inside it, so it stays the sync's (the tick fails)."""
    try:
        yield
    except (ValueError, KeyError, TypeError) as error:
        raise NodeReleaseRefused(NODE_RELEASE_INVALID) from error


def _built_deployment(identity: str, manifest: NodeReleaseV2,
                      locators: Any) -> tuple[NodeDeployment, OriginLocator]:
    """The release's deployment and its base source, encoded once so an unencodable deployment
    is refused here, before any write (`write_deployment` encodes the same value again)."""
    with _release_data():
        base = locators["base"]
        source = OriginLocator(base["url"], manifest.base.content_key, base["size"])
        deployment = deployment_for_release(identity, manifest, locators)
        encode_node_deployment(deployment)
    return deployment, source


def deployment_job(kind: str, identity: str) -> AssetJob:
    """The fetch job of one of a deployment's asset references."""
    if kind == AssetKind.OS_IMAGE:
        return FetchOsImage(tarball_sha256=identity)
    if kind == AssetKind.SEALED_ENVIRONMENT:
        return FetchSealedEnvironment(sha256=identity)
    raise ValueError(f"not a node deployment asset kind: {kind!r}")


def _owner(deployment_id: UUID) -> str:
    """The `asset_references` owner of a deployment's files."""
    return _OWNER + deployment_id.hex


def _manager_pins(deployment: NodeDeployment) -> dict[str, Any]:
    """What a base's managers are pinned to: the same for every deployment of that base."""
    base = deployment.base
    return {"base_abi": base.base_abi, "graphics_abi": base.graphics_abi,
            "plugin_abi": base.plugin_abi,
            "primary": asdict(deployment.manager_primary),
            "fallback": asdict(deployment.manager_fallback) if deployment.manager_fallback else None}


def _stored_deployments(conn: Any) -> list[NodeDeployment]:
    """Every stored deployment, parsed from its release row's document."""
    return [parse_node_deployment(bytes(row["deployment_document"])) for row in conn.execute(
        "SELECT deployment_document FROM node_releases WHERE deployment_document IS NOT NULL").fetchall()]


def environment_reference_in(conn: Any, environment_sha256: str) -> AppEnvironmentRefV2 | None:
    """The reference any stored deployment names for this digest (`write_deployment` holds every
    deployment to one reference per digest); None when none names it."""
    for deployment in _stored_deployments(conn):
        for item in deployment.environments():
            if item.environment_sha256 == environment_sha256:
                return item
    return None


def _release_of(conn: Any, deployment: NodeDeployment) -> str | None:
    """The tag whose catalogued manifest builds `deployment` (`deployment_id_for`), None: none."""
    with_app = deployment.app_environment is not None
    for row in conn.execute("SELECT tag, manifest_sha256 FROM node_releases WHERE left(manifest_sha256, 12)=%s",
                            (deployment.deployment_id.hex[:12],)).fetchall():
        if deployment_id_for(row["manifest_sha256"], with_app) == deployment.deployment_id:
            return row["tag"]
    return None


def write_deployment(conn: Any, deployment: NodeDeployment, base_source: OriginLocator, *,
                     clock: Clock) -> bool:
    """Write one deployment onto the release row whose manifest built it, and its asset
    references; True when it already existed (duplicate, nothing written).

    Refuses (`NodeReleaseRefused`) an existing id with other bytes, a base whose manager pins
    differ from another stored deployment of the same base, an environment digest another stored
    deployment names with another reference, and a deployment no catalogued release builds.
    `base_source` names where the base tarball is downloaded. Holds the release writer's lock
    (`RELEASE_WRITER_LOCK`) before its first read, so two writers serialize and the second sees
    the first's deployment."""
    canonical = encode_node_deployment(deployment)
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (RELEASE_WRITER_LOCK,))
    prior = conn.execute("SELECT deployment_document FROM node_releases WHERE deployment_id=%s",
                         (deployment.deployment_id,)).fetchone()
    if prior:
        if bytes(prior["deployment_document"]) != canonical:
            raise NodeReleaseRefused("node_deployment_identity_conflict")
        return True
    base, others = deployment.base, _stored_deployments(conn)
    pins = _manager_pins(deployment)
    if any(other.base.content_key == base.content_key and _manager_pins(other) != pins for other in others):
        raise NodeReleaseRefused("node_base_manager_pins_immutable")
    known = {item.environment_sha256: item for other in others for item in other.environments()}
    environments = deployment.environments()
    if any(known.get(item.environment_sha256, item) != item for item in environments):
        raise NodeReleaseRefused("node_environment_identity_conflict")
    tag = _release_of(conn, deployment)
    if tag is None:
        raise NodeReleaseRefused("node_deployment_unreleased")
    now = clock.utc()
    conn.execute("UPDATE node_releases SET deployment_id=%s, deployment_document=%s, published_at=%s, "
                 "changed_at=%s, changed_by='central' WHERE tag=%s",
                 (deployment.deployment_id, canonical, now, now, tag))
    owner = _owner(deployment.deployment_id)
    references = [(AssetKey(AssetKind.OS_IMAGE, base.content_key),
                   AssetReference(owner, base_source, base.size_bytes, base.squashfs_sha256))]
    for item in environments:
        references.append((AssetKey(AssetKind.SEALED_ENVIRONMENT, item.environment_sha256),
            AssetReference(owner, OriginLocator(deployment.environment_sources[item.environment_sha256],
                item.environment_sha256, item.size_bytes), item.size_bytes, item.environment_sha256)))
    records, tx = PgAssetRecords(clock), PgTransaction(conn)
    for key, reference in references:
        records.reference(tx, key, reference)
    return False


@dataclass(frozen=True, slots=True)
class WantedDeployments:
    """What the cache must keep for node boots (the one definition, design §6.2; a version per Pi,
    Q2 A, 2026-10-16)."""

    window: tuple[tuple[str, UUID], ...]  # (tag, deployment), newest release first
    versions: frozenset[UUID]             # every Pi's version
    previous: frozenset[UUID]             # every Pi's previous version

    @property
    def deployments(self) -> frozenset[UUID]:
        """Every wanted deployment (`boot_policy.kept`: the window, every Pi's version and previous)."""
        return frozenset(UUID(each) for each in kept((str(deployment) for _, deployment in self.window),
                                                      map(str, self.versions), map(str, self.previous)))


def _stable_deployments(conn: Any) -> dict[str, UUID]:
    """tag -> its deployment, for every stable tag that has one."""
    return {row["tag"]: row["deployment_id"] for row in conn.execute(
        "SELECT tag, deployment_id FROM node_releases "
        "WHERE NOT is_prerelease AND deployment_id IS NOT NULL").fetchall()}


def wanted_deployments(conn: Any) -> WantedDeployments:
    """The window, every Pi's version and every Pi's previous version (`devices`). A Pi still
    preparing a deployment that is none of them (its version changed twice since its offer) is
    covered by `SERVE_GRACE` while it reads and by read-through after: nothing else roots it."""
    rows = conn.execute("SELECT deployment_id, previous_deployment_id FROM devices "
                        "WHERE deployment_id IS NOT NULL").fetchall()
    stable = _stable_deployments(conn)
    return WantedDeployments(tuple((tag, stable[tag]) for tag in window(stable)),
                             frozenset(row["deployment_id"] for row in rows),
                             frozenset(row["previous_deployment_id"] for row in rows
                                       if row["previous_deployment_id"] is not None))


def tag_deployments_in(conn: Any) -> dict[str, UUID]:
    """tag -> its deployment, for every tag that has one (stable and pre-release)."""
    return {row["tag"]: row["deployment_id"] for row in conn.execute(
        "SELECT tag, deployment_id FROM node_releases WHERE deployment_id IS NOT NULL").fetchall()}


def deployment_jobs(conn: Any, deployments) -> dict[UUID, frozenset[AssetJob]]:
    """Each deployment's asset jobs (its `asset_references`, owner `node-deployment:<hex>`)."""
    ids = sorted(set(deployments), key=str)
    if not ids:
        return {}
    owners = {_owner(deployment): deployment for deployment in ids}
    found: dict[UUID, set[AssetJob]] = {deployment: set() for deployment in ids}
    for row in conn.execute("SELECT owner, kind, identity FROM asset_references WHERE owner = ANY(%s)",
                            (sorted(owners),)).fetchall():
        found[owners[row["owner"]]].add(deployment_job(row["kind"], row["identity"]))
    return {deployment: frozenset(jobs) for deployment, jobs in found.items()}


# A tag's observation: inserted with its catalogue entry (ingest) or its problem (record_problem);
# on a known tag only the observation moves, under the not-older guard, and a manifest once
# catalogued is kept (`coalesce`): the caller compares the returned manifest with its own.
_OBSERVED = ("INSERT INTO node_releases(tag,is_prerelease,problem,upstream_changed_at,upstream_asset_id,"
             "observed_at,manifest_sha256,revision,document,asset_locators,discovered_at,"
             "created_at,created_by,changed_at,changed_by) "
             "VALUES(%(tag)s,%(is_prerelease)s,%(problem)s,%(changed_at)s,%(asset_id)s,%(now)s,"
             "%(manifest)s,%(revision)s,%(document)s,%(locators)s,%(discovered)s,%(now)s,'central',%(now)s,"
             "'central') "
             "ON CONFLICT(tag) DO UPDATE SET is_prerelease=EXCLUDED.is_prerelease,problem=EXCLUDED.problem,"
             "upstream_changed_at=EXCLUDED.upstream_changed_at,upstream_asset_id=EXCLUDED.upstream_asset_id,"
             "observed_at=EXCLUDED.observed_at,"
             + ",".join(f"{column}=COALESCE(node_releases.{column},EXCLUDED.{column})"
                        for column in ("manifest_sha256", "revision", "document", "asset_locators",
                                       "discovered_at"))
             + ",changed_at=EXCLUDED.changed_at,changed_by=EXCLUDED.changed_by "
             f"WHERE {not_older('node_releases')} RETURNING manifest_sha256, asset_locators")


def _observe(conn: Any, release: PublishedRelease, *, problem: str | None, now: float,
             catalogued: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Upsert the tag's observation (and, on a new tag, `catalogued`'s entry); the row's manifest
    and locators after it, None when the observation is older than the stored one."""
    version = release.node_version
    entry = catalogued or {}
    return conn.execute(_OBSERVED, {
        "tag": release.tag, "is_prerelease": release.is_prerelease, "problem": problem, "now": now,
        "changed_at": None if version is None else version.changed_at,
        "asset_id": None if version is None else version.asset_id,
        "manifest": entry.get("manifest"), "revision": entry.get("revision"),
        "document": entry.get("document"), "locators": entry.get("locators"),
        "discovered": None if catalogued is None else now,
    }).fetchone()


class PgNodeReleaseRecords:
    """Implements `content_catalog.ports.NodeReleaseRecords`."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def ingest(self, tx: Transaction, release: PublishedRelease, *, now: float) -> bool:
        publication = release.node_publication
        if publication is None:
            raise ValueError("no_node_publication")
        conn = pg_connection(tx)
        with _release_data():
            manifest = parse_node_release(publication.manifest)
        identity = sha256(publication.manifest).hexdigest()
        entry = {"manifest": identity, "revision": manifest.revision, "document": publication.manifest,
                 "locators": Jsonb({role: asdict(locator) for role, locator in publication.assets})}
        try:
            with conn.transaction():  # an older observation or a refusal writes nothing
                stored = _observe(conn, release, problem=None, now=now, catalogued=entry)
                if stored is None:
                    raise _Older
                if stored["manifest_sha256"] != identity:
                    raise NodeReleaseRefused(IDENTITY_CONFLICT)
                deployment, source = _built_deployment(identity, manifest, stored["asset_locators"])
                write_deployment(conn, deployment, source, clock=self._clock)
        except _Older:
            return False
        return True

    def record_problem(self, tx: Transaction, release: PublishedRelease, problem: str, *,
                       now: float) -> bool:
        return _observe(pg_connection(tx), release, problem=problem, now=now) is not None

    def stable_deployments(self, tx: Transaction) -> tuple[StableDeployment, ...]:
        conn = pg_connection(tx)
        ids = _stable_deployments(conn)
        jobs = deployment_jobs(conn, ids.values())
        return tuple(StableDeployment(tag, str(ids[tag]), jobs[ids[tag]])
                     for tag in newest_first(ids))
