"""A version per Pi over PostgreSQL (Q2 A, 2026-10-16: "No stored wall-wide release; Central keeps only
a version per Pi"): `PgPlayerVersions` implements central/content_catalog/ports.py PlayerVersions,
the one writer and reader of each Pi's version (its `devices` row's version columns, migration 121).

A Pi's version is a release tag naming one deployment (its `node_releases` row's). A Pi with no version yet is offered the newest ready stable release
(`boot_policy.newest_ready_stable`) at its first boot, and that offer records it as the Pi's version
(decided once, where the first offer is made): after that a Pi's version changes only through Install
or Update the wall, never because a newer release became ready (0010: no auto-advance), and the keep
set holds the release it runs. Install sets one Pi's version (its old one becomes its previous), queues the download (a Prefetch in the same
transaction: the version is now part of the keep set, `boot_policy.kept`), and restarts that Pi once
the release is ready: at once when it already is, else at the worker's next `advance_installs` after
it is. A restart is a `RebootPi` job published in the transaction that sets `install = 'restarting'`
(central/fleet/pi_reboot.py: the worker asks the Pi's host over its fleet-pipe link), so an Install
that rolls back queues no reboot; a Pi with no link then stays `restarting` until it boots, and its
next boot offer carries the version anyway. The install ends when the Pi's host line states the
version's base tag (`node_states`, the host line's `base`; a release's base carries the release's own
tag): `InstallEnds` wakes the look that ends it when a host's base is recorded. Update the wall sets
every enrolled Pi's version in one transaction and restarts none (the console's rollout does).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from central.actor import Actor
from central.content_catalog.boot_policy import newest_first, newest_ready_stable
from central.content_catalog.catalog import device_id_for_serial
from central.content_catalog.deployment import parse_node_deployment
from central.content_catalog.ports import StableDeployment, VersionRefused
from central.db import Database
from central.fleet.node_bus_hub import enrolled_devices_in
from central.infra.node_link_latest import latest_values_in
from central.infra.node_releases import (
    PgNodeReleaseRecords,
    deployment_jobs,
    notify_releases_in,
    tag_deployments_in,
)
from central.infra.transactions import PgTransaction, pg_connection
from central.kernel.job_types import Prefetch, RebootPi
from central.kernel.ports import AssetReadiness
from central.kernel.publishing import Publisher
from contracts.central_api import (
    InstallProgress,
    InstallRefusal,
    InstallStarted,
    PiVersion,
    Versions,
)
from contracts.node_state import HOST_BASE_KEY, HOST_LINE, decode_host_base
from contracts.time import Clock
from nodeapi.buffers import KV_OPERATION_HEADER, state_prefix, state_stream
from nodeapi.pull import Read


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
        "SELECT device_id, deployment_id, previous_deployment_id, install FROM devices "
        "WHERE deployment_id IS NOT NULL").fetchall()}
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
    row = conn.execute("SELECT deployment_id FROM node_releases WHERE tag=%s", (version,)).fetchone()
    if row is None:
        raise VersionRefused(InstallRefusal.UNKNOWN_VERSION)
    if row["deployment_id"] is None:
        raise VersionRefused(InstallRefusal.NOT_DEPLOYABLE)
    return row["deployment_id"]


class PgPlayerVersions:
    """PlayerVersions over one Database."""

    def __init__(self, db: Database, clock: Clock, readiness: AssetReadiness, publisher: Publisher | None) -> None:
        self._db = db
        self._clock = clock
        self._readiness = readiness
        self._publisher = publisher

    def versions(self) -> Versions:
        with self._db.read_snapshot() as conn:
            found = pi_versions_in(conn, self._clock, self._readiness)
        return Versions(pis={pi.serial: PiVersion(version=pi.version, chosen=pi.chosen, install=pi.install)
                             for pi in found.pis})

    def install(self, serial: str, version: str, *, by: Actor) -> InstallStarted:
        device_id = device_id_for_serial(serial)
        with self._db.transaction() as conn:
            if device_id is None or not _enrolled_in(conn, device_id):
                raise VersionRefused(InstallRefusal.UNKNOWN_PLAYER)
            deployment = _deployment_of(conn, version)
            install: InstallProgress = "restarting" if self._ready(conn, deployment) else "downloading"
            self._set_in(conn, device_id, deployment, install, by)
            if install == "restarting":
                self._restart(conn, device_id, version, by)
        return InstallStarted(serial=serial, version=version, install=install)

    def set_wall_version(self, version: str, *, by: Actor) -> Versions:
        with self._db.transaction() as conn:
            deployment = _deployment_of(conn, version)
            for device_id in enrolled_devices_in(conn).values():
                self._set_in(conn, device_id, deployment, None, by)
        return self.versions()

    def boot_deployment_in(self, tx: Any, serial: str) -> str | None:
        """The deployment `serial`'s boot is offered. A Pi with no version yet gets the newest ready
        stable release, recorded here as its version in the offer's own transaction."""
        conn = pg_connection(tx)
        device_id = device_id_for_serial(serial)
        if device_id is None:
            return None
        row = conn.execute("SELECT deployment_id FROM devices WHERE device_id=%s", (device_id,)).fetchone()
        if row is None:
            return None
        if row["deployment_id"] is not None:
            return str(row["deployment_id"])
        default = default_deployment_in(conn, self._clock, self._readiness)
        if default is None:
            return None
        now = self._clock.utc()
        conn.execute("UPDATE devices SET deployment_id=%s, version_changed_at=%s, changed_at=%s, changed_by='node' "
                     "WHERE device_id=%s", (UUID(default.deployment_id), now, now, device_id))
        notify_releases_in(conn)
        return default.deployment_id

    def advance_installs(self) -> None:
        with self._db.transaction() as conn:
            rows = conn.execute("SELECT v.device_id, v.deployment_id, v.install, r.deployment_document FROM devices v "
                                "JOIN node_releases r USING(deployment_id) WHERE v.install IS NOT NULL "
                                "FOR UPDATE OF v").fetchall()
            if not rows:
                return
            now = self._clock.utc()
            running = self._running(conn)
            tags = _tags_by_deployment(tag_deployments_in(conn))
            for row in rows:
                if row["install"] == "downloading" and self._ready(conn, row["deployment_id"]):
                    conn.execute("UPDATE devices SET install='restarting', changed_at=%s, changed_by='central' "
                                 "WHERE device_id=%s", (now, row["device_id"]))
                    self._restart(conn, row["device_id"], tags.get(row["deployment_id"]), "central")
                elif (row["install"] == "restarting"
                      and running.get(row["device_id"]) == parse_node_deployment(bytes(row["deployment_document"])).base.tag):
                    conn.execute("UPDATE devices SET install=NULL, changed_at=%s, changed_by='central' "
                                 "WHERE device_id=%s", (now, row["device_id"]))
                else:
                    continue
                notify_releases_in(conn)

    def _set_in(self, conn: Any, device_id: str, deployment: UUID, install: InstallProgress | None,
                by: Actor) -> None:
        """Set one Pi's version; its old one becomes its previous when it differs (a Pi that never
        booted has none: its first offer records its version); the download is queued in the same
        transaction."""
        conn.execute(
            "UPDATE devices SET previous_deployment_id=CASE WHEN deployment_id <> %(deployment)s "
            "THEN deployment_id ELSE previous_deployment_id END, deployment_id=%(deployment)s, "
            "install=%(install)s, version_changed_at=%(now)s, changed_at=%(now)s, changed_by=%(by)s "
            "WHERE device_id=%(device)s",
            {"device": device_id, "deployment": deployment, "install": install, "now": self._clock.utc(),
             "by": by})
        notify_releases_in(conn)
        if self._publisher is not None:
            self._publisher.publish(Prefetch(), within=PgTransaction(conn))

    def _ready(self, conn: Any, deployment: UUID) -> bool:
        jobs = deployment_jobs(conn, [deployment]).get(deployment, frozenset())
        return self._readiness.readiness(PgTransaction(conn), jobs, wanted=True).state == "ready"

    def _running(self, conn: Any) -> dict[str, str | None]:
        """device id -> the base tag its host line states (its `base`, the newest Central recorded)."""
        return {value.device_id: None if (base := value.decoded(decode_host_base, "host base")) is None
                else base.base_tag for value in latest_values_in(conn, HOST_LINE, HOST_BASE_KEY, of_players=False)}

    def _restart(self, conn: Any, device_id: str, version: str | None, by: Actor) -> None:
        """Queue the Pi's reboot onto its version in the caller's transaction (none without a
        publisher: tests); one pending per Pi, so a second ask while one is queued is not queued."""
        if self._publisher is not None:
            self._publisher.publish(RebootPi(request_id=str(uuid4()), device_id=device_id,
                                             reason=f"install:{version or 'unknown'}", by=by),
                                    within=PgTransaction(conn))


_HOST_STATE_STREAM = state_stream(HOST_LINE)
_HOST_BASE_SUBJECT = state_prefix(HOST_LINE) + HOST_BASE_KEY


class InstallEnds:
    """A RecordJudge for the host line's state bucket: when a Pi whose install is `restarting` has a
    new `base` recorded, the commit that records it sends `notify_releases_in`, so the worker's next
    FleetHub look (its `advance_installs`) ends the install as soon as the Pi states its base, not at
    the look's ceiling."""

    def judge_in(self, conn: Any, device_id: str, stream: str, items: tuple[Read, ...]) -> None:
        if stream != _HOST_STATE_STREAM or not any(
                item.subject == _HOST_BASE_SUBJECT and not (item.headers or {}).get(KV_OPERATION_HEADER)
                for item in items):
            return
        if conn.execute("SELECT 1 FROM devices WHERE device_id=%s AND install='restarting'",
                        (device_id,)).fetchone() is not None:
            notify_releases_in(conn)


__all__ = ["InstallEnds", "PgPlayerVersions", "PiVersionRow", "PiVersions", "default_deployment_in", "pi_versions_in"]
