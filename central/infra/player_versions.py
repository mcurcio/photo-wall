"""A version per Pi over PostgreSQL (Q2 A, 2026-10-16: "No stored wall-wide release; Central keeps only
a version per Pi"): `PgPlayerVersions` implements central/content_catalog/ports.py PlayerVersions,
the one writer and reader of each Pi's version (`player_versions`, migration 100).

A Pi's version is a release tag naming one deployment (the tag's last good manifest, through
node_release_observations). A Pi with no row boots the newest ready stable release
(`boot_policy.newest_ready_stable`) and keeps no version until one is chosen for it. Install sets one
Pi's version (its old one becomes its previous), queues the download (a Prefetch in the same
transaction: the version is now part of the keep set, `boot_policy.kept`), and restarts that Pi once
the release is ready: at once when it already is, else at the worker's next `advance_installs` after
it is. A restart is a reboot command on the Pi's current host session (NodeCommands
`reboot_for_install`); a Pi with none stays `restarting` until it boots, and its next boot offer
carries the version anyway. The install ends when the Pi's host reports the version's base tag (a
release's base carries the release's own tag). Update the wall sets every enrolled Pi's version in
one transaction and restarts none (the console's rollout does).
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from central.content_catalog.boot_policy import newest_first, newest_ready_stable
from central.content_catalog.catalog import device_id_for_serial
from central.content_catalog.deployment import parse_node_deployment
from central.content_catalog.ports import StableDeployment, VersionRefused
from central.db import Database
from central.fleet.node_bus_hub import enrolled_devices_in
from central.fleet.node_commands import NodeCommands
from central.fleet.node_observations import NodeObservations, host_facts_of
from central.fleet.node_sessions import NodeControlError
from central.infra.node_releases import PgNodeReleaseRecords, deployment_jobs, tag_deployments_in
from central.infra.transactions import PgTransaction, pg_connection
from central.kernel.job_types import Prefetch
from central.kernel.ports import AssetReadiness
from central.kernel.publishing import Publisher
from contracts.central_api import (
    InstallProgress,
    InstallRefusal,
    InstallStarted,
    PiVersion,
    Versions,
)
from contracts.time import Clock

log = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class PiVersionRow:
    """One enrolled Pi's version as Central keeps it: its serial and device, the release tag it boots
    next (None: no release is ready), whether Central keeps one for it, its previous tag, an install's
    progress, and the deployment its version names (None: none)."""
    serial: str
    device_id: str
    version: str | None
    chosen: bool
    previous: str | None
    install: InstallProgress | None
    deployment_id: UUID | None


@dataclass(frozen=True, slots=True)
class PiVersions:
    """Every enrolled Pi's version, and the release a Pi with no version boots (None: none ready)."""
    pis: tuple[PiVersionRow, ...]
    default: StableDeployment | None


def default_deployment_in(conn: Any, clock: Clock, readiness: AssetReadiness) -> StableDeployment | None:
    """The release a Pi with no version boots: the newest ready stable release
    (`boot_policy.newest_ready_stable`, readiness asked lazily, newest first)."""
    tx = PgTransaction(conn)
    return newest_ready_stable((row, readiness.readiness(tx, row.jobs, wanted=True))
                               for row in PgNodeReleaseRecords(clock).stable_deployments(tx))


def pi_versions_in(conn: Any, clock: Clock, readiness: AssetReadiness) -> PiVersions:
    """Every enrolled Pi's version, read in the caller's transaction (the ONE read: the hub's
    `versions`, the console's Releases page and Update the wall answer from it)."""
    tags = _tags_by_deployment(tag_deployments_in(conn))
    default = default_deployment_in(conn, clock, readiness)
    rows = {row["device_id"]: row for row in conn.execute(
        "SELECT device_id, deployment_id, previous_deployment_id, install FROM player_versions").fetchall()}
    pis = []
    for serial, device_id in sorted(enrolled_devices_in(conn).items()):
        row = rows.get(device_id)
        if row is None:
            pis.append(PiVersionRow(serial, device_id, None if default is None else default.tag, False, None, None,
                                    None if default is None else UUID(default.deployment_id)))
            continue
        previous = row["previous_deployment_id"]
        pis.append(PiVersionRow(serial, device_id, tags.get(row["deployment_id"]), True,
                                None if previous is None else tags.get(previous), row["install"],
                                row["deployment_id"]))
    return PiVersions(tuple(pis), default)


def _tags_by_deployment(deployments: Mapping[str, UUID]) -> dict[UUID, str]:
    """deployment -> the newest tag naming it."""
    found: dict[UUID, str] = {}
    for tag in newest_first(deployments):
        found.setdefault(deployments[tag], tag)
    return found


def _enrolled_in(conn: Any, device_id: str) -> bool:
    return device_id in enrolled_devices_in(conn).values()


def _deployment_of(conn: Any, version: str) -> UUID:
    """The deployment `version` names: VersionRefused(UNKNOWN_VERSION) for a tag Central never
    observed, VersionRefused(NOT_DEPLOYABLE) for one with no good manifest."""
    row = conn.execute("SELECT manifest_sha256 FROM node_release_observations WHERE tag=%s", (version,)).fetchone()
    if row is None:
        raise VersionRefused(InstallRefusal.UNKNOWN_VERSION)
    deployment = tag_deployments_in(conn).get(version)
    if deployment is None or conn.execute("SELECT 1 FROM node_deployments WHERE deployment_id=%s",
                                          (deployment,)).fetchone() is None:
        raise VersionRefused(InstallRefusal.NOT_DEPLOYABLE)
    return deployment


class PgPlayerVersions:
    """PlayerVersions over one Database."""

    def __init__(self, db: Database, clock: Clock, readiness: AssetReadiness, publisher: Publisher | None,
                 commands: NodeCommands) -> None:
        self._db = db
        self._clock = clock
        self._readiness = readiness
        self._publisher = publisher
        self._commands = commands

    def versions(self) -> Versions:
        with self._db.read_snapshot() as conn:
            found = pi_versions_in(conn, self._clock, self._readiness)
        return Versions(pis={pi.serial: PiVersion(version=pi.version, chosen=pi.chosen, install=pi.install)
                             for pi in found.pis})

    def install(self, serial: str, version: str) -> InstallStarted:
        device_id = device_id_for_serial(serial)
        with self._db.transaction() as conn:
            if device_id is None or not _enrolled_in(conn, device_id):
                raise VersionRefused(InstallRefusal.UNKNOWN_PLAYER)
            deployment = _deployment_of(conn, version)
            install: InstallProgress = "restarting" if self._ready(conn, deployment) else "downloading"
            default = default_deployment_in(conn, self._clock, self._readiness)
            self._set_in(conn, device_id, deployment, install,
                         None if default is None else UUID(default.deployment_id))
        if install == "restarting":
            self._restart(device_id, version)
        return InstallStarted(serial=serial, version=version, install=install)

    def set_wall_version(self, version: str) -> Versions:
        with self._db.transaction() as conn:
            deployment = _deployment_of(conn, version)
            default = default_deployment_in(conn, self._clock, self._readiness)
            for device_id in enrolled_devices_in(conn).values():
                self._set_in(conn, device_id, deployment, None,
                             None if default is None else UUID(default.deployment_id))
        return self.versions()

    def boot_deployment_in(self, tx: Any, serial: str) -> str | None:
        conn = pg_connection(tx)
        device_id = device_id_for_serial(serial)
        row = None if device_id is None else conn.execute(
            "SELECT deployment_id FROM player_versions WHERE device_id=%s", (device_id,)).fetchone()
        if row is not None:
            return str(row["deployment_id"])
        default = default_deployment_in(conn, self._clock, self._readiness)
        return None if default is None else default.deployment_id

    def advance_installs(self) -> None:
        restart: list[tuple[str, str | None]] = []
        with self._db.transaction() as conn:
            rows = conn.execute("SELECT v.device_id, v.deployment_id, v.install, d.document FROM player_versions v "
                                "JOIN node_deployments d USING(deployment_id) WHERE v.install IS NOT NULL "
                                "FOR UPDATE OF v").fetchall()
            if not rows:
                return
            running = self._running(conn)
            tags = _tags_by_deployment(tag_deployments_in(conn))
            for row in rows:
                if row["install"] == "downloading" and self._ready(conn, row["deployment_id"]):
                    conn.execute("UPDATE player_versions SET install='restarting' WHERE device_id=%s",
                                 (row["device_id"],))
                    restart.append((row["device_id"], tags.get(row["deployment_id"])))
                elif (row["install"] == "restarting"
                      and running.get(row["device_id"]) == parse_node_deployment(bytes(row["document"])).base.tag):
                    conn.execute("UPDATE player_versions SET install=NULL WHERE device_id=%s", (row["device_id"],))
        for device_id, version in restart:
            self._restart(device_id, version)

    def _set_in(self, conn: Any, device_id: str, deployment: UUID, install: InstallProgress | None,
                default: UUID | None) -> None:
        """Set one Pi's version; its old one (or, with none kept, the release it booted by default)
        becomes its previous when it differs; the download is queued in the same transaction."""
        conn.execute(
            "INSERT INTO player_versions(device_id, deployment_id, previous_deployment_id, install, changed_at) "
            "VALUES (%(device)s, %(deployment)s, CASE WHEN %(default)s::uuid IS DISTINCT FROM %(deployment)s::uuid "
            "THEN %(default)s::uuid END, %(install)s, to_timestamp(%(now)s)) "
            "ON CONFLICT (device_id) DO UPDATE SET deployment_id=EXCLUDED.deployment_id, install=EXCLUDED.install, "
            "changed_at=EXCLUDED.changed_at, previous_deployment_id=CASE WHEN player_versions.deployment_id "
            "<> EXCLUDED.deployment_id THEN player_versions.deployment_id ELSE player_versions.previous_deployment_id END",
            {"device": device_id, "deployment": deployment, "default": default, "install": install,
             "now": self._clock.utc()})
        if self._publisher is not None:
            self._publisher.publish(Prefetch(), within=PgTransaction(conn))

    def _ready(self, conn: Any, deployment: UUID) -> bool:
        jobs = deployment_jobs(conn, [deployment]).get(deployment, frozenset())
        return self._readiness.readiness(PgTransaction(conn), jobs, wanted=True).state == "ready"

    def _running(self, conn: Any) -> dict[str, str | None]:
        """device id -> the base tag its host reports this boot (the console's host read)."""
        hosts = NodeObservations(self._commands.sessions).fleet_hosts_in(conn, self._clock.utc())
        return {device_id: facts.get("base_tag") for device_id, facts in host_facts_of(hosts).items()}

    def _restart(self, device_id: str, version: str | None) -> None:
        try:
            self._commands.reboot_for_install(device_id, audit_ref=f"install:{version or 'unknown'}")
        except NodeControlError as error:   # no current host session: its next boot takes the version
            log.info("install on %s: not restarted now: %s", device_id, error.code)


__all__ = ["PgPlayerVersions", "PiVersionRow", "PiVersions", "default_deployment_in", "pi_versions_in"]
