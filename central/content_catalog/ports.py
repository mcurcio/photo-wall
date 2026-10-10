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
