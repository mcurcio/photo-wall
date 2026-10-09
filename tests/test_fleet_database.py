"""Real PostgreSQL gates for the T0 check-in intake and the fleet status read.

The shared registry fixture supplies a private migrated schema and skips without
PHOTO_WALL_TEST_DATABASE_URL; this test must run against local Compose and CI PostgreSQL.
"""

from uuid import UUID

import pytest

from central.fleet.models import CheckIn, FleetError
from central.fleet.service import FleetService

SERIAL = "abcdef1234567890"


def test_check_in_sequence_and_delayed_boot_remain_observational(registry) -> None:
    service = FleetService(registry.db, registry.clock)

    def report(boot: int, sequence: int) -> CheckIn:
        return CheckIn(schema=1, kind="pi", serial=SERIAL, kernel_boot_id=UUID(int=boot),
                       agent_incarnation="agent-1", observation_sequence=sequence,
                       phase="base_ready")

    assert service.record_check_in(report(2, 2)) == {"accepted": True}
    assert service.record_check_in(report(1, 1)) == {"accepted": True}
    assert service.record_check_in(report(2, 1)) == {
        "accepted": False, "reason": "stale_or_duplicate", "next_sequence": 3}
    device = next(d for d in service.status()["devices"] if d["serial"] == SERIAL)
    assert device["base"]["state"] == "ambiguous_boot_claims"
    assert device["base"]["current_physical_boot"] == "unknown"
    assert device["accepted_fallback"] is None
    assert device["update_now"] == {"available": False, "reason": "command_trust_unapproved"}


def test_duplicate_check_ins_consume_quota_before_idempotent_receipt(registry) -> None:
    service = FleetService(registry.db, registry.clock)
    report = CheckIn(schema=1, kind="pi", serial=SERIAL, kernel_boot_id=UUID(int=91),
                     agent_incarnation="agent-1", observation_sequence=1,
                     phase="base_ready")
    assert service.record_check_in(report) == {"accepted": True}
    with registry.db.transaction() as conn:
        device_id = conn.execute("SELECT device_id FROM devices WHERE serial=%s",
                                 (SERIAL,)).fetchone()["device_id"]
        conn.execute("UPDATE fleet_t0_daily_quotas SET used=9999 "
                     "WHERE scope=%s AND kind='observation'", (device_id,))
    assert service.record_check_in(report) == {
        "accepted": False, "reason": "stale_or_duplicate", "next_sequence": 2}
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT used FROM fleet_t0_daily_quotas "
                            "WHERE scope=%s AND kind='observation'", (device_id,)
                            ).fetchone()["used"] == 10000
        assert conn.execute("SELECT count(*) AS n FROM fleet_os_observations "
                            "WHERE device_id=%s", (device_id,)).fetchone()["n"] == 1
    with pytest.raises(FleetError, match="t0_rate_limited"):
        service.record_check_in(report)


def test_mismatched_offer_check_in_pays_quota_before_lookup(registry) -> None:
    service = FleetService(registry.db, registry.clock)
    report = CheckIn(schema=1, kind="pi", serial=SERIAL, kernel_boot_id=UUID(int=92),
                     offer_id=UUID(int=999), agent_incarnation="agent-1",
                     observation_sequence=1, phase="base_ready")
    with pytest.raises(FleetError, match="boot_offer_mismatch"):
        service.record_check_in(report)
    with registry.db.transaction() as conn:
        charged = conn.execute("SELECT scope,used FROM fleet_t0_daily_quotas "
                               "WHERE kind='observation' ORDER BY scope").fetchall()
        assert len(charged) == 2 and all(row["used"] == 1 for row in charged)
        assert conn.execute("SELECT count(*) AS n FROM fleet_os_observations").fetchone()[
            "n"] == 0


def test_a_check_in_naming_a_boot_offer_pays_quota_and_is_refused(registry) -> None:
    """Central keeps no V1 boot offer, so a check-in naming one matches nothing."""
    service = FleetService(registry.db, registry.clock)
    report = CheckIn(schema=1, kind="pi", serial=SERIAL, kernel_boot_id=UUID(int=92),
                     offer_id=UUID(int=999), agent_incarnation="agent-1",
                     observation_sequence=1, phase="base_ready")
    with pytest.raises(FleetError, match="boot_offer_mismatch"):
        service.record_check_in(report)
    with registry.db.transaction() as conn:
        charged = conn.execute("SELECT scope,used FROM fleet_t0_daily_quotas "
                               "WHERE kind='observation' ORDER BY scope").fetchall()
        assert len(charged) == 2 and all(row["used"] == 1 for row in charged)
        assert conn.execute("SELECT count(*) AS n FROM fleet_os_observations").fetchone()[
            "n"] == 0
