"""The node release read: every observed release, what it became, and whether it is downloaded.

Releases are observed and ingested by the release sync (`central/content_catalog/sync.py`): each
valid one is already a deployment, so the console's one verb per row ("Put vX on the wall") is
built from the row's own `deployment_id`, never from the capped deployments list. Readiness is
derived on read by the one readiness check (`AssetReadiness`): nothing here stores "cached".
"""
from __future__ import annotations

from typing import Any

from central.content_catalog.boot_policy import first_run_choice, newest_first
from central.content_catalog.deployment import deployment_id_for, parse_node_deployment
from central.infra.node_releases import deployment_jobs, wanted_deployments
from central.infra.transactions import PgTransaction
from central.kernel.ports import AssetReadiness, Readiness
from contracts.node_release import parse_node_release

MAX_RELEASES = 100
MAX_DEPLOYMENTS = 50


def _deployment_row(row) -> dict:
    deployment = parse_node_deployment(bytes(row["document"]))
    app = deployment.app_environment
    return {"deployment_id": str(row["deployment_id"]), "published_at": row["published_at"],
            "base_tag": deployment.base.tag, "app_environment_sha256": app.environment_sha256 if app else None}


def _readiness(readiness: Readiness | None) -> dict[str, Any]:
    if readiness is None:  # no cache mounted in this process: unknown, never guessed
        return {"readiness": None, "readiness_reason": None, "missing_bytes": None}
    return {"readiness": readiness.state, "readiness_reason": readiness.reason,
            "missing_bytes": readiness.missing_bytes}


class NodeReleaseCatalog:
    def __init__(self, sessions, *, readiness: AssetReadiness | None = None):
        self.sessions = sessions
        self.readiness = readiness

    def list(self):
        """The release read (console DDD Part E G1, R18): the boot selection, the deployments,
        and one row per observed tag (newest release first, at most `MAX_RELEASES`) under ONE
        read-only snapshot, so a selection's revision, the deployments it can name and the
        releases they came from agree. No lock is taken.

        `selection` is always present: revision 0 with no policy row, the value Central's own
        compare-and-set compares against then (`NodeBootService.select`), and `auto` (with no
        selection ever made) names the release Central will select by itself and its readiness.
        `deployments` are the 50 newest by `published_at` plus the selected and the previous
        one, wherever they fall. Each release row carries its `deployment_id` (None while the
        tag has no good manifest), its `problem`, `stable`, `in_window` and its readiness."""
        self.sessions.require_enabled()
        with self.sessions.db.transaction() as conn:
            # Must precede the first data query (operator_snapshot.py does the same).
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            read_at = self.sessions.clock.utc()
            policy = conn.execute("SELECT revision,deployment_id,previous_deployment_id,changed_at "
                                  "FROM node_boot_policy WHERE singleton").fetchone()
            selected = policy["deployment_id"] if policy else None
            previous = policy["previous_deployment_id"] if policy else None
            deployments = conn.execute(
                "SELECT deployment_id,document,published_at FROM node_deployments WHERE deployment_id IN ("
                "SELECT deployment_id FROM node_deployments ORDER BY published_at DESC, deployment_id "
                f"LIMIT {MAX_DEPLOYMENTS}) OR deployment_id=%s OR deployment_id=%s "
                "ORDER BY published_at DESC, deployment_id", (selected, previous)).fetchall()
            observed = conn.execute(
                "SELECT o.tag,o.is_prerelease,o.problem,o.manifest_sha256,c.revision,c.document,"
                "c.discovered_at FROM node_release_observations o "
                "LEFT JOIN node_release_catalog c USING(manifest_sha256)").fetchall()
            # Every observed tag is semver (the origin skips any other).
            observed = newest_first(observed, lambda row: row["tag"])[:MAX_RELEASES]
            wanted = wanted_deployments(conn, now=read_at)
            window = {deployment for _, deployment in wanted.window}
            releases = [self._release_row(row) for row in observed]
            ids = [row["deployment_id"] for row in releases if row["deployment_id"] is not None]
            jobs = deployment_jobs(conn, ids)
            tx = PgTransaction(conn)
            for row in releases:
                deployment = row["deployment_id"]
                state = None
                if deployment is not None and self.readiness is not None:
                    state = self.readiness.readiness(tx, jobs.get(deployment, frozenset()),
                                                     wanted=deployment in wanted.deployments)
                row["in_window"] = deployment in window
                row.update(_readiness(state))
                row["deployment_id"] = None if deployment is None else str(deployment)
            auto = None
            if policy is None:
                choice = first_run_choice(
                    (row, Readiness(row["readiness"], row["readiness_reason"], row["missing_bytes"]))
                    for row in releases if row["stable"] and row["deployment_id"] is not None
                    and row["readiness"] is not None)
                if choice is not None:
                    auto = {key: choice[0][key] for key in (
                        "tag", "deployment_id", "readiness", "readiness_reason", "missing_bytes")}
        return {
            "read_at": read_at,
            "selection": {"revision": policy["revision"] if policy else 0,
                          "deployment_id": str(selected) if selected else None,
                          "previous_deployment_id": str(previous) if previous else None,
                          "changed_at": policy["changed_at"] if policy else None,
                          "auto": auto},
            "deployments": [_deployment_row(row) for row in deployments],
            "releases": releases,
        }

    @staticmethod
    def _release_row(row) -> dict:
        """One observed tag; `deployment_id` stays a UUID until the caller has its readiness."""
        common = {"tag": row["tag"], "stable": not row["is_prerelease"], "problem": row["problem"],
                  "manifest_sha256": row["manifest_sha256"]}
        if row["manifest_sha256"] is None:  # rejected, no good upload yet
            return {**common, "deployment_id": None, "revision": None, "discovered_at": None,
                    "base_tag": None, "app_environment_sha256": None}
        release = parse_node_release(bytes(row["document"]))
        app = release.app_environment
        return {**common, "deployment_id": deployment_id_for(row["manifest_sha256"], app is not None),
                "revision": row["revision"], "discovered_at": row["discovered_at"],
                "base_tag": release.base.tag,
                "app_environment_sha256": app.environment_sha256 if app else None}
