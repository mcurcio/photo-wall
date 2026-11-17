"""116 (decision 0034, slice S1): `singletons` exists; Settings › House moves into it whole, its
writer "unknown" and its audit times the row's `updated_at`; the two quota tables and the hub's
look stamp are gone.

The migration's SQL runs on a test database at head after dropping `singletons` and recreating the
four tables 116 drops, `house_settings` as 089 left it (with `zone_confirmed`).

Mutation probes: (C2) drop the two quota `DROP TABLE` lines from 116 -> the quota tables remain;
(S2) drop `zone_confirmed` from 116's `jsonb_build_object` -> the document no longer validates.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from central.house_time.model import HouseTime, Place
from central.infra.house_store import _HouseDocument

pytestmark = pytest.mark.db

MIGRATION = Path(__file__).parents[1] / "central/migrations/116_singletons_and_dropped_counters.sql"
DROPPED = ("house_settings", "node_intake_quotas", "fleet_t0_daily_quotas", "node_bus_hub_looks")
AT_115 = """
CREATE TABLE house_settings (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
  zone TEXT NOT NULL,
  latitude DOUBLE PRECISION CHECK (latitude BETWEEN -90 AND 90),
  longitude DOUBLE PRECISION CHECK (longitude BETWEEN -180 AND 180),
  units TEXT NOT NULL CHECK (units IN ('cm', 'in')),
  clock_format TEXT NOT NULL CHECK (clock_format IN ('12h', '24h')),
  updated_at DOUBLE PRECISION NOT NULL,
  zone_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
  CHECK ((latitude IS NULL) = (longitude IS NULL)));
CREATE TABLE node_intake_quotas (device_id TEXT, day BIGINT, kind TEXT, used INTEGER);
CREATE TABLE fleet_t0_daily_quotas (scope TEXT, kind TEXT, day BIGINT, used INTEGER);
CREATE TABLE node_bus_hub_looks (singleton BOOLEAN PRIMARY KEY, looked_at DOUBLE PRECISION);
"""


def _migrate(registry, house: tuple | None) -> dict | None:
    with registry.db.transaction() as conn:
        conn.execute("DROP TABLE singletons")
        conn.execute(AT_115)
        if house is not None:
            conn.execute("INSERT INTO house_settings (zone, latitude, longitude, units, clock_format,"
                         " updated_at, zone_confirmed) VALUES (%s, %s, %s, %s, %s, %s, %s)", house)
        conn.execute("INSERT INTO node_intake_quotas VALUES ('d', 1, 'observation', 3)")
        conn.execute("INSERT INTO fleet_t0_daily_quotas VALUES ('global', 'offer', 1, 3)")
        conn.execute("INSERT INTO node_bus_hub_looks VALUES (TRUE, 99)")
        conn.execute(MIGRATION.read_text())
        for name in DROPPED:
            assert conn.execute("SELECT to_regclass(%s) AS t", (name,)).fetchone()["t"] is None, name
        return conn.execute("SELECT * FROM singletons WHERE name='house'").fetchone()


def test_116_moves_a_house_with_a_place_in_whole(registry):
    row = _migrate(registry, ("Europe/London", 51.5, -0.12, "in", "12h", 1234.5, True))
    assert _HouseDocument.model_validate(row["document"]) == _HouseDocument(
        time=HouseTime(zone="Europe/London", place=Place(latitude=51.5, longitude=-0.12)),
        units="in", clock="12h", zone_confirmed=True)
    assert (row["revision"], row["created_at"], row["created_by"], row["changed_at"],
            row["changed_by"]) == (1, 1234.5, "unknown", 1234.5, "unknown")


def test_116_moves_a_house_without_a_place_and_its_keys_are_the_documents(registry):
    row = _migrate(registry, ("UTC", None, None, "cm", "24h", 7.0, False))
    document = _HouseDocument(time=HouseTime(zone="UTC"), units="cm", clock="24h",
                              zone_confirmed=False)
    assert row["document"] == document.model_dump(mode="json")


def test_116_with_no_house_row_stores_none(registry):
    assert _migrate(registry, None) is None
