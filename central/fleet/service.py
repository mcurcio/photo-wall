"""Central fleet state for the T0 OS check-ins: observational intake and the operator read.

Nothing here can issue a command. The node boot path reuses the T0 quotas and the device claim.
"""

from __future__ import annotations

from central.content_catalog.catalog import device_id_for_serial, sanitize_serial
from central.db import Database
from central.fleet.check_in_status import (
    app_control_status,
    app_observation_status,
    boot_claim_status,
)
from central.fleet.management_status import management_status_in
from central.fleet.models import CheckIn, CheckInV2, FleetError
from central.fleet.node_bus_presence import bus_links_in
from contracts.time import Clock

_OBS_LOCK_CLASS = 734118329
_DAY_SECONDS = 86400
_OBS_TTL_SECONDS = 30 * _DAY_SECONDS
_OFFER_DEVICE_DAILY = 128
_OFFER_GLOBAL_DAILY = 32768  # 128 Players x 128 offers, plus equivalent spoof headroom
_OBS_DEVICE_DAILY = 10000  # 10-second heartbeat with room for phase changes
_OBS_GLOBAL_DAILY = 2000000  # exceeds 128 x per-device budget with headroom
_NEW_CLAIMS_GLOBAL_DAILY = 1024  # bounds distinct fake serial rows, above 128-Player ceiling


class FleetService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db, self.clock = db, clock

    @staticmethod
    def _claim_quota(conn, *, device_id: str, kind: str, now: float) -> None:
        day = int(now // _DAY_SECONDS)
        limits = {
            "offer": (_OFFER_GLOBAL_DAILY, _OFFER_DEVICE_DAILY),
            "observation": (_OBS_GLOBAL_DAILY, _OBS_DEVICE_DAILY),
        }[kind]
        for scope, limit in zip(("global", device_id), limits, strict=True):
            row = conn.execute(
                "INSERT INTO fleet_t0_daily_quotas(scope,kind,day,used) "
                "VALUES(%s,%s,%s,1) ON CONFLICT(scope,kind,day) DO UPDATE SET "
                "used=fleet_t0_daily_quotas.used+1 WHERE fleet_t0_daily_quotas.used<%s "
                "RETURNING used", (scope, kind, day, limit),
            ).fetchone()
            if row is None:
                raise FleetError("t0_rate_limited", 429)
        # A bounded day index keeps fake-serial quota rows from accumulating forever.
        conn.execute("DELETE FROM fleet_t0_daily_quotas WHERE day<%s", (day - 30,))

    @staticmethod
    def _claim_new_device(conn, *, device_id: str, serial: str, now: float) -> None:
        if conn.execute("SELECT 1 FROM devices WHERE device_id=%s", (device_id,)).fetchone():
            return
        day = int(now // _DAY_SECONDS)
        row = conn.execute(
            "INSERT INTO fleet_t0_daily_quotas(scope,kind,day,used) "
            "VALUES('global','new_device',%s,1) ON CONFLICT(scope,kind,day) DO UPDATE SET "
            "used=fleet_t0_daily_quotas.used+1 "
            "WHERE fleet_t0_daily_quotas.used<%s RETURNING used",
            (day, _NEW_CLAIMS_GLOBAL_DAILY),
        ).fetchone()
        if row is None:
            raise FleetError("t0_claim_limit", 429)
        conn.execute("INSERT INTO devices(device_id,serial,first_seen,last_seen) "
                     "VALUES(%s,%s,%s,%s) ON CONFLICT(device_id) DO NOTHING",
                     (device_id, serial, now, now))

    def record_check_in(self, request: CheckIn | CheckInV2) -> dict:
        serial = sanitize_serial(request.serial)
        if serial is None:
            raise FleetError("invalid_serial", 422)
        device_id = device_id_for_serial(serial)
        assert device_id is not None
        # T0 is unauthenticated. Charge every syntactically valid check-in before
        # any idempotency lookup or per-boot lock, so cheap duplicate traffic
        # cannot bypass the daily DB admission cap.
        now = self.clock.utc()
        with self.db.transaction() as quota_conn:
            self._claim_quota(quota_conn, device_id=device_id, kind="observation", now=now)
        if request.offer_id is not None:
            raise FleetError("boot_offer_mismatch")  # Central keeps no V1 boot offer
        with self.db.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s,hashtext(%s))",
                         (_OBS_LOCK_CLASS, device_id + str(request.kernel_boot_id)))
            last = conn.execute("SELECT max(observation_sequence) AS n FROM fleet_os_observations "
                                "WHERE device_id=%s AND kernel_boot_id=%s",
                                (device_id, request.kernel_boot_id)).fetchone()["n"]
            if last is not None and request.observation_sequence <= last:
                return {"accepted": False, "reason": "stale_or_duplicate",
                        "next_sequence": last + 1 if last < 2147483647 else None}
            self._claim_new_device(conn, device_id=device_id, serial=serial, now=now)
            evidence = request.app_evidence if isinstance(request, CheckInV2) else None
            running = evidence.running if evidence is not None else None
            conn.execute("INSERT INTO fleet_os_observations(device_id,kernel_boot_id,"
                         "agent_incarnation,observation_sequence,offer_id,base_digest,phase,"
                         "fault_code,attempted_app_sha256,sampled_boottime_ms,received_at,"
                         "observation_schema,app_installed_sha256,app_running_sha256,"
                         "app_running_pid,app_running_start_ticks,app_running_invocation_id,"
                         "app_installed_reason,app_running_reason) "
                         "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (device_id, request.kernel_boot_id, request.agent_incarnation,
                          request.observation_sequence, request.offer_id, request.base_digest,
                          request.phase, request.fault_code, request.attempted_app_sha256,
                          request.sampled_boottime_ms, now, request.schema_version,
                          evidence.installed_sha256 if evidence is not None else None,
                          running.sha256 if running is not None else None,
                          running.pid if running is not None else None,
                          running.start_ticks if running is not None else None,
                          running.invocation_id if running is not None else None,
                          evidence.installed_reason if evidence is not None else None,
                          evidence.running_reason if evidence is not None else None))
            # Bounded per-boot history; the latest sequence and its fault always survive.
            conn.execute("DELETE FROM fleet_os_observations WHERE device_id=%s "
                         "AND kernel_boot_id=%s AND observation_sequence < %s",
                         (device_id, request.kernel_boot_id,
                          max(0, request.observation_sequence - 63)))
            # Opportunistic global TTL in bounded batches.
            conn.execute("DELETE FROM fleet_os_observations WHERE ctid IN "
                         "(SELECT ctid FROM fleet_os_observations WHERE received_at<%s "
                         "ORDER BY received_at LIMIT 1000)", (now - _OBS_TTL_SECONDS,))
            return {"accepted": True}

    def status(self) -> dict:
        read_at = self.clock.utc()
        with self.db.transaction() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            devices = conn.execute("SELECT device.device_id,device.serial,"
                                   "lifecycle.generation AS device_generation FROM devices AS device "
                                   "JOIN fleet_device_lifecycle AS lifecycle "
                                   "ON lifecycle.device_id=device.device_id "
                                   "WHERE device.retired_at IS NULL "
                                   "AND lifecycle.revoked_at IS NULL "
                                   "ORDER BY device.device_id LIMIT 5000").fetchall()
            device_ids = [row["device_id"] for row in devices]
            management = management_status_in(conn, device_ids, read_at=read_at)
            bus_links = bus_links_in(conn, device_ids)
            observations: dict[str, list[dict]] = {}
            for observation in conn.execute(
                "SELECT DISTINCT ON(device_id,kernel_boot_id) * FROM fleet_os_observations "
                "WHERE received_at>=%s ORDER BY device_id,kernel_boot_id,"
                "observation_sequence DESC,received_at DESC",
                (read_at - _OBS_TTL_SECONDS,),
            ).fetchall():
                observations.setdefault(observation["device_id"], []).append(observation)
            players = {r["device_id"]: r for r in conn.execute(
                "SELECT DISTINCT ON(device_id) device_id,id,authority_epoch,last_seen,health "
                "FROM players WHERE retired_at IS NULL ORDER BY device_id,last_seen DESC").fetchall()}
            control = {r["player_id"]: r for r in conn.execute(
                "SELECT player_id,authority_epoch,status,schema_version,applied_at,"
                "applied_delivery_id,applied_digest,last_delivery_id,last_result_sequence,"
                "last_result_digest,last_result,last_result_at "
                "FROM player_control_sessions").fetchall()}
            accepted = {r["device_id"]: r for r in conn.execute(
                "SELECT DISTINCT ON(accepted.device_id) accepted.* "
                "FROM fleet_generation_acceptances AS accepted "
                "JOIN fleet_device_lifecycle AS lifecycle "
                "ON lifecycle.device_id=accepted.device_id "
                "JOIN devices AS device ON device.device_id=accepted.device_id "
                "WHERE accepted.kind='app' "
                "AND accepted.device_generation=lifecycle.generation "
                "AND lifecycle.revoked_at IS NULL AND device.retired_at IS NULL "
                "ORDER BY accepted.device_id,accepted.accepted_at DESC,"
                "accepted.sha256 DESC").fetchall()}
            result = []
            for device in devices:
                device_id = device["device_id"]
                obs = observations.get(device_id, [])
                player = players.get(device_id)
                session = control.get(player["id"]) if player else None
                installed, running = app_observation_status(observations=obs, read_at=read_at)
                accepted_app = accepted.get(device_id)
                result.append({
                    "device_id": device_id, "serial": device["serial"],
                    "device_generation": device["device_generation"],
                    "management": management[device_id],
                    "installed": installed, "running": running,
                    "accepted_fallback": None if accepted_app is None else {
                        "sha256": accepted_app["sha256"], "size": accepted_app["size"],
                        "at": accepted_app["accepted_at"], "assurance": "accepted_record"},
                    "base": boot_claim_status(observations=obs, read_at=read_at),
                    "app": app_control_status(player=player, session=session, read_at=read_at),
                    "output": {"state": "not_currently_qualified", "source": "none",
                               "age_seconds": None},
                    "update_now": {"available": False, "reason": "command_trust_unapproved"},
                    "bus_link": bus_links[device_id],
                })
            return {"read_at": read_at, "devices": result,
                    "assurance": "t0_observational", "commands_available": False}
