"""Observed V2 releases become publishable only after exact artifact verification."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from shutil import disk_usage
from tempfile import TemporaryDirectory
from uuid import UUID

from psycopg.types.json import Jsonb

from central.fleet.node_boot import NodeBootService, NodeDeployment
from central.fleet.node_sessions import NodeControlError
from central.kernel.assets import OriginLocator
from central.origins.github import GitHubReleaseOrigin
from contracts.node_protocol import digest, identifier, token
from contracts.node_release import parse_node_release


class NodeReleaseCatalog:
    def __init__(self, sessions, origin=None):
        self.sessions = sessions
        self.origin = origin or GitHubReleaseOrigin.from_env()

    def list(self):
        self.sessions.require_enabled()
        with self.sessions.db.transaction() as conn:
            return {"releases": [dict(row) for row in conn.execute(
                "SELECT c.manifest_sha256,c.tag,c.revision,c.discovered_at,v.verified_at "
                "FROM node_release_catalog c LEFT JOIN node_release_verifications v USING(manifest_sha256) "
                "ORDER BY c.discovered_at DESC LIMIT 100").fetchall()]}

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
