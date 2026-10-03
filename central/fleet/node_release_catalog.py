"""Observed V2 releases become publishable only after exact artifact verification."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from shutil import disk_usage
from tempfile import TemporaryDirectory
from uuid import UUID

from psycopg.types.json import Jsonb

from central.fleet.node_boot import NodeBootService, NodeDeployment, parse_node_deployment
from central.fleet.node_sessions import NodeControlError
from central.kernel.assets import OriginLocator
from central.origins.github import GitHubReleaseOrigin
from contracts.node_protocol import digest, identifier, token
from contracts.node_release import parse_node_release


def _deployment_row(row) -> dict:
    deployment = parse_node_deployment(bytes(row["document"]))
    app = deployment.app_environment
    return {"deployment_id": str(row["deployment_id"]), "published_at": row["published_at"],
            "base_tag": deployment.base.tag, "app_environment_sha256": app.environment_sha256 if app else None}


def _release_row(row) -> dict:
    release = parse_node_release(bytes(row["document"]))
    app = release.app_environment
    # `download_bytes`: what a publish downloads and hash-checks, every asset, app or not.
    return {"manifest_sha256": row["manifest_sha256"], "tag": row["tag"], "revision": row["revision"],
            "discovered_at": row["discovered_at"], "verified_at": row["verified_at"],
            "base_tag": release.base.tag, "app_environment_sha256": app.environment_sha256 if app else None,
            "download_bytes": sum(asset.size_bytes for asset in release.artifacts)}


class NodeReleaseCatalog:
    def __init__(self, sessions, origin=None):
        self.sessions = sessions
        self.origin = origin or GitHubReleaseOrigin.from_env()

    def list(self):
        """The release read (console DDD Part E G1, R18): the boot selection, the deployments and
        the release catalog under ONE read-only snapshot, so a selection's revision, the
        deployments it can name and the releases they came from agree. No lock is taken.

        `selection` is always present: revision 0 with no policy row, the value Central's own
        compare-and-set compares against then (`NodeBootService.select`). `deployments` are the
        50 newest by `published_at` plus the selected one, wherever it falls. Every base and app
        is read from the stored canonical documents through their one parser."""
        self.sessions.require_enabled()
        with self.sessions.db.transaction() as conn:
            # Must precede the first data query (operator_snapshot.py does the same).
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            read_at = self.sessions.clock.utc()
            policy = conn.execute("SELECT revision,deployment_id,changed_at FROM node_boot_policy "
                                  "WHERE singleton").fetchone()
            selected = policy["deployment_id"] if policy else None
            deployments = conn.execute(
                "SELECT deployment_id,document,published_at FROM node_deployments WHERE deployment_id IN ("
                "SELECT deployment_id FROM node_deployments ORDER BY published_at DESC, deployment_id LIMIT 50) "
                "OR deployment_id=%s ORDER BY published_at DESC, deployment_id", (selected,)).fetchall()
            releases = conn.execute(
                "SELECT c.manifest_sha256,c.tag,c.revision,c.document,c.discovered_at,v.verified_at "
                "FROM node_release_catalog c LEFT JOIN node_release_verifications v USING(manifest_sha256) "
                "ORDER BY c.discovered_at DESC LIMIT 100").fetchall()
        return {
            "read_at": read_at,
            "selection": {"revision": policy["revision"] if policy else 0,
                          "deployment_id": str(selected) if selected else None,
                          "changed_at": policy["changed_at"] if policy else None},
            "deployments": [_deployment_row(row) for row in deployments],
            "releases": [_release_row(row) for row in releases],
        }

    def _load(self, manifest_sha256):
        digest(manifest_sha256)
        with self.sessions.db.transaction() as conn:
            row = conn.execute("SELECT * FROM node_release_catalog WHERE manifest_sha256=%s",
                               (manifest_sha256,)).fetchone()
            if row is None:
                raise NodeControlError("node_release_unknown", 404)
            return row

    async def publish(self, manifest_sha256: str, deployment_id: UUID, select_app: bool,
                      operator_audit_ref: str):
        self.sessions.require_enabled()
        identifier(deployment_id)
        token(operator_audit_ref, 256)
        if type(select_app) is not bool:
            raise NodeControlError("node_release_app_selection_invalid", 422)
        row = await asyncio.to_thread(self._load, manifest_sha256)
        release = parse_node_release(bytes(row["document"]))
        if select_app and release.app_environment is None:
            raise NodeControlError("node_release_app_unconfigured")
        # This uses the existing bounded, hash-checking GitHub transport. These
        # private temporary files are verification scratch, never a cache writer.
        # Actual serving still rehydrates through the worker and stream leases.
        with TemporaryDirectory(prefix="photo-wall-node-release-") as directory:
            for asset in release.artifacts:
                locator = OriginLocator(**row["asset_locators"][asset.role])
                if (locator.sha256, locator.size) != (asset.sha256, asset.size_bytes):
                    raise NodeControlError("node_release_locator_mismatch")
                if disk_usage(directory).free < asset.size_bytes + 64 * 1024**2:
                    raise NodeControlError("node_release_verification_storage_unavailable", 503)
                target = Path(directory) / asset.filename
                await self.origin.download(locator, target, max_bytes=asset.size_bytes)
                await asyncio.to_thread(target.unlink)
        await asyncio.to_thread(self._record, manifest_sha256, operator_audit_ref, release)
        references = [release.manager_primary, release.manager_fallback,
                      release.app_environment if select_app else None]
        roles = {release.manager_primary.environment_sha256: "manager-primary"}
        if release.manager_fallback:
            roles[release.manager_fallback.environment_sha256] = "manager-fallback"
        if select_app and release.app_environment:
            roles[release.app_environment.environment_sha256] = "app"
        deployment = NodeDeployment(deployment_id, release.base, release.app_environment if select_app else None,
            release.manager_primary, release.manager_fallback,
            {ref.environment_sha256: row["asset_locators"][roles[ref.environment_sha256]]["url"]
             for ref in references if ref is not None})
        return await asyncio.to_thread(NodeBootService(self.sessions).publish, deployment)

    def _record(self, manifest_sha256, operator_audit_ref, release):
        with self.sessions.db.transaction() as conn:
            conn.execute("INSERT INTO node_release_verifications VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                         (manifest_sha256, self.sessions.clock.utc(), operator_audit_ref,
                          Jsonb({a.role: asdict(a) for a in release.artifacts})))
