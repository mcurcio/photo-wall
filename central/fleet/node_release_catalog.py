"""The node release read: every observed release, what it became, whether it is downloaded, and every
enrolled Pi's version (the console's Releases page, by version per Pi, Q2 A, 2026-10-16;
central/content_catalog/views.py ReleasesView).

Releases are observed and ingested by the release sync (`central/content_catalog/sync.py`): each
valid one is already a deployment. Readiness is derived on read by the one readiness check
(`AssetReadiness`): nothing here stores "cached". Each Pi's version is the one read
(central/infra/player_versions.py `pi_versions_in`) the boot offer and Central's hub answer from.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from central.content_catalog.boot_policy import newest_first
from central.content_catalog.deployment import deployment_id_for
from central.content_catalog.views import PlayerVersionView, ReleasesView, ReleaseView
from central.fleet.node_observations import NodeObservations, host_facts_of
from central.infra.node_releases import deployment_jobs, wanted_deployments
from central.infra.player_versions import pi_versions_in
from central.infra.transactions import PgTransaction
from central.kernel.ports import AssetReadiness
from contracts.node_release import parse_node_release

MAX_RELEASES = 100


class NodeReleaseCatalog:
    def __init__(self, sessions, *, readiness: AssetReadiness) -> None:
        self.sessions = sessions
        self.readiness = readiness

    def view(self) -> ReleasesView:
        """The Releases page under ONE read-only snapshot (no lock taken): one row per observed tag
        (newest release first, at most MAX_RELEASES) with its deployment's base and app, whether it is
        in the window and its readiness; every enrolled Pi's version (`pi_versions_in`) with the base
        tag its host last reported (`running`, the console's host read); and `default`, the release a
        Pi with no version boots."""
        with self.sessions.db.read_snapshot() as conn:
            read_at = self.sessions.clock.utc()
            observed = conn.execute(
                "SELECT o.tag,o.is_prerelease,o.problem,o.manifest_sha256,c.document,c.discovered_at "
                "FROM node_release_observations o LEFT JOIN node_release_catalog c USING(manifest_sha256)").fetchall()
            # Every observed tag is semver (the origin skips any other).
            observed = newest_first(observed, lambda row: row["tag"])[:MAX_RELEASES]
            wanted = wanted_deployments(conn)
            window = {deployment for _, deployment in wanted.window}
            rows = [(row, _deployment(row)) for row in observed]
            jobs = deployment_jobs(conn, [deployment for _, deployment in rows if deployment is not None])
            tx = PgTransaction(conn)
            releases = []
            for row, deployment in rows:
                readiness = None if deployment is None else self.readiness.readiness(
                    tx, jobs.get(deployment, frozenset()), wanted=deployment in wanted.deployments)
                releases.append(_release(row, deployment, deployment in window, readiness))
            versions = pi_versions_in(conn, self.sessions.clock, self.readiness)
            players = {row["device_id"]: row["id"] for row in conn.execute(
                "SELECT id, device_id FROM players WHERE retired_at IS NULL").fetchall()}
            facts = host_facts_of(NodeObservations(self.sessions).fleet_hosts_in(conn, read_at))
        running = {device_id: each.get("base_tag") for device_id, each in facts.items()}
        return ReleasesView(
            read_at=read_at, releases=tuple(releases),
            players=tuple(PlayerVersionView(
                serial=pi.serial, player_id=players.get(pi.device_id), name=pi.serial, version=pi.version,
                chosen=pi.chosen, previous=pi.previous, install=pi.install, running=running.get(pi.device_id))
                for pi in versions.pis),
            default=None if versions.default is None else versions.default.tag)


def _deployment(row: Any):
    """The deployment an observed tag's last good manifest became (None: none)."""
    if row["manifest_sha256"] is None:
        return None
    release = parse_node_release(bytes(row["document"]))
    return deployment_id_for(row["manifest_sha256"], release.app_environment is not None)


def _release(row: Any, deployment: UUID | None, in_window: bool, readiness) -> ReleaseView:
    base_tag = app = None
    if row["manifest_sha256"] is not None:
        release = parse_node_release(bytes(row["document"]))
        base_tag = release.base.tag
        app = release.app_environment.environment_sha256 if release.app_environment else None
    return ReleaseView(
        version=row["tag"], stable=not row["is_prerelease"], problem=row["problem"],
        deployment_id=None if deployment is None else str(deployment),
        base_tag=base_tag,
        app_environment_sha256=app, in_window=in_window,
        readiness=None if readiness is None else readiness.state,
        readiness_reason=None if readiness is None else readiness.reason,
        missing_bytes=0 if readiness is None else readiness.missing_bytes,
        discovered_at=row["discovered_at"])
