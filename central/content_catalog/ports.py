"""The catalog's seams: the release listing's records and the node half of a release sync.

`ReleaseRecords` covers the release listing's ETag (`app_release_poll`) and the wanted node
deployments' files. It takes the kernel's opaque `Transaction`, so the domain never sees psycopg
(`central/infra/catalog_records.py` implements it).
`NodeReleaseRecords` is the node half of a release sync (`central/infra/node_releases.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from central.kernel.job_types import AssetJob
from central.kernel.ports import PublishedRelease
from central.kernel.transactions import Transaction
from central.kernel.types import require_reason
from contracts.central_api import InstallRefusal, InstallStarted, Versions


@dataclass(frozen=True, slots=True)
class FleetDesiredAssets:
    """Exact content keys of the selected and previous node deployments; plus `window`, the
    fetch jobs of the window's node deployments (the newest stable releases), newest release
    first, which are desired too but fetched in the background (a file may be in both)."""

    base_tarballs: frozenset[str] = frozenset()
    sealed_environments: frozenset[str] = frozenset()
    window: tuple[AssetJob, ...] = ()


class NodeReleaseRefused(Exception):
    """A node release (or a boot selection) refused before any write: the release's
    own problem (`reason`), never the sync's. `kind` maps to the HTTP status class."""

    def __init__(self, reason: str, kind: Literal["conflict", "not_found"] = "conflict") -> None:
        require_reason(reason)
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


@dataclass(frozen=True, slots=True)
class StableDeployment:
    """One stable observed tag's ingested deployment, with the asset jobs it needs."""

    tag: str
    deployment_id: str
    jobs: frozenset[AssetJob]


class NodeReleaseRecords(Protocol):
    """The node half of a release sync (`node_release_observations`, the release catalog, the
    deployment writer and the select writer). Every method runs in the caller's transaction."""

    def ingest(self, tx: Transaction, release: PublishedRelease, *, now: float) -> bool:
        """Turn `release.node_publication` into its catalog row, deployment and asset references,
        and point the tag's observation at it, unless the observation is older than the stored
        one (False: nothing written). Raises `NodeReleaseRefused`
        for the release's own refusals (identity, pins, environment conflicts); the caller then
        rolls the transaction back and records the problem in another."""
        ...

    def record_problem(self, tx: Transaction, release: PublishedRelease, problem: str, *,
                       now: float) -> bool:
        """Record the tag's latest refusal under the same not-older guard; the last good
        manifest (and so its deployment) stays. False when the observation is older."""
        ...

    def selection_exists(self, tx: Transaction) -> bool:
        """Whether a boot selection was ever made (the row is never deleted)."""
        ...

    def stable_deployments(self, tx: Transaction) -> tuple[StableDeployment, ...]:
        """Every stable observed tag with a deployment, newest release first."""
        ...

    def select_first(self, tx: Transaction, deployment_id: str, *, now: float) -> bool:
        """Create the boot selection through the one select writer with expected revision 0;
        False (nothing written) when any selection exists."""
        ...


@dataclass(frozen=True, slots=True)
class StoredEtag:
    """The release listing's stored ETag (None: list in full) and when it was stored."""

    etag: str | None
    stored_at: float


class ReleaseRecords(Protocol):
    def fleet_desired_assets(self, tx: Transaction) -> FleetDesiredAssets:
        """Digest roots of the wanted node deployments (`central/infra/node_releases.py`
        `wanted_deployments`: selected, previous, and the window as `window`)."""
        ...

    def load_etag(self, tx: Transaction) -> StoredEtag | None:
        """None when no listing was ever stored."""
        ...

    def store_etag(self, tx: Transaction, etag: str | None, *, now: float) -> None: ...


class VersionRefused(Exception):
    """A version Central would not set: why (contracts/central_api.py InstallRefusal)."""

    def __init__(self, refusal: InstallRefusal) -> None:
        super().__init__(refusal.value)
        self.refusal = refusal


class PlayerVersions(Protocol):
    """A version per Pi (Q2 A, 2026-10-16: "No stored wall-wide release; Central keeps only a version
    per Pi"). The one writer and reader of each Pi's version; the boot offer, the console's Releases
    routes and Central's hub method all call it. `central/infra/player_versions.py` implements it.

    A Pi's version is a release tag naming one deployment. A Pi with none boots the newest ready
    stable release (`boot_policy.newest_ready_stable`); once set, a version changes only by these
    writers ("upgrades are always your choice"). Central keeps downloaded the newest WINDOW_SIZE
    stable releases plus every Pi's version and previous version (`boot_policy.kept`)."""

    def versions(self) -> Versions:
        """What each enrolled Pi is to boot now, whether Central keeps one for it, and an install's
        progress."""
        ...

    def install(self, serial: str, version: str) -> InstallStarted:
        """Set the Pi `serial`'s version to `version` (its old one becomes its previous), download the
        release if it is not ready (a Prefetch in the same transaction), then restart that Pi onto it
        once it is ready (a reboot command to its current host session); answers once the version is
        set. VersionRefused(UNKNOWN_PLAYER | UNKNOWN_VERSION | NOT_DEPLOYABLE) before any write."""
        ...

    def set_wall_version(self, version: str) -> Versions:
        """Update the wall: set every enrolled Pi's version to `version` in one transaction (each old
        one becomes its previous) and download it; restarts no Pi (the console's rollout does).
        VersionRefused(UNKNOWN_VERSION | NOT_DEPLOYABLE) before any write."""
        ...

    def boot_deployment_in(self, tx: Transaction, serial: str) -> str | None:
        """Inside the boot offer's transaction: the deployment id the Pi `serial` boots now (its
        version's, else the newest ready stable release's); None when no release is ready."""
        ...
