"""New unbound stop transitions require a durable permit at transaction commit."""

import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.errors import CheckViolation
from psycopg.types.json import Jsonb
from test_fleet_attempts import _principal
from test_fleet_command_lifecycle import _ready, _setup
from test_node_upgrade_history import seed_historical_committed_stop
from test_registry import enroll

from central.coordination import Coordinator
from central.db import Database
from central.equipment_drain import EquipmentDrain
from central.registry import Registry, RegistryError
from contracts.time import ManualClock


def test_direct_unbound_insert_without_permit_rolls_back(registry) -> None:
    player, _, request = enroll(registry)
    drain = EquipmentDrain(Coordinator(registry.db, registry.clock))
    drain.prepare_unbound(
        player["player_id"], "legacy-attempt", request.boot_id,
        player["authority_epoch"], authorization_expires_at=1010,
    )
    with registry.db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM equipment_drains WHERE player_id=%s", (player["player_id"],),
        ).fetchone()
    with pytest.raises(CheckViolation, match="same-transaction fleet permit"):
        with registry.db.transaction() as conn:
            conn.execute("DELETE FROM equipment_drains WHERE player_id=%s", (player["player_id"],))
            conn.execute(
                "INSERT INTO equipment_drains(player_id,attempt_id,boot_id,authority_epoch,"
                "phase,prepared_at,authorization_expires_at,stop_committed_at,snapshot) "
                "VALUES(%s,%s,%s,%s,'stop_committed',%s,%s,%s,%s)",
                (row["player_id"], row["attempt_id"], row["boot_id"], row["authority_epoch"],
                 row["prepared_at"], row["authorization_expires_at"], 1000.0,
                 Jsonb(row["snapshot"])),
            )
    with registry.db.transaction() as conn:
        assert conn.execute(
            "SELECT phase FROM equipment_drains WHERE player_id=%s", (player["player_id"],),
        ).fetchone()["phase"] == "prepared"


def test_scope_removal_in_stop_transaction_is_blocked(registry) -> None:
    player, _, request = enroll(registry)
    drain = EquipmentDrain(Coordinator(registry.db, registry.clock))
    drain.prepare_unbound(
        player["player_id"], "legacy-attempt", request.boot_id,
        player["authority_epoch"], authorization_expires_at=1010,
    )
    with pytest.raises(CheckViolation, match="committed drain is immutable"):
        with registry.db.transaction() as conn:
            conn.execute(
                "UPDATE equipment_drains SET phase='stop_committed',stop_committed_at=1000 "
                "WHERE player_id=%s", (player["player_id"],),
            )
            conn.execute(
                "UPDATE equipment_drains SET snapshot=snapshot-'admission_scope' "
                "WHERE player_id=%s", (player["player_id"],),
            )
    with registry.db.transaction() as conn:
        row = conn.execute(
            "SELECT phase,snapshot->>'admission_scope' AS scope FROM equipment_drains "
            "WHERE player_id=%s", (player["player_id"],),
        ).fetchone()
    assert row == {"phase": "prepared", "scope": "unbound_canary"}


def test_prepared_scope_cannot_be_disguised_before_later_stop(registry) -> None:
    player, _, request = enroll(registry)
    drain = EquipmentDrain(Coordinator(registry.db, registry.clock))
    drain.prepare_unbound(
        player["player_id"], "legacy-attempt", request.boot_id,
        player["authority_epoch"], authorization_expires_at=1010,
    )
    with pytest.raises(CheckViolation, match="prepared unbound drain snapshot"):
        with registry.db.transaction() as conn:
            conn.execute(
                "UPDATE equipment_drains SET snapshot=snapshot-'admission_scope' "
                "WHERE player_id=%s", (player["player_id"],),
            )
    with pytest.raises(RegistryError, match="unbound_drain_requires_safe_commit"):
        drain.commit_stop(
            player["player_id"], "legacy-attempt", request.boot_id,
            player["authority_epoch"],
        )
    with pytest.raises(CheckViolation, match="same-transaction fleet permit"):
        drain.commit_stop_unbound(
            player["player_id"], "legacy-attempt", request.boot_id,
            player["authority_epoch"],
        )


def test_committed_drain_identity_and_snapshot_are_immutable(registry) -> None:
    lifecycle, request_id, player, generation = _setup(registry)
    command = lifecycle.dispatch_unbound(
        _principal(), request_id=request_id, player_id=player["player_id"],
        expected_gate_generation=generation,
    )
    lifecycle.authorize_stop_unbound(
        _principal(), _ready(command), expected_gate_generation=generation,
    )
    for statement in (
        "UPDATE equipment_drains SET snapshot=snapshot-'admission_scope' WHERE player_id=%s",
        "UPDATE equipment_drains SET attempt_id='tampered' WHERE player_id=%s",
        "UPDATE equipment_drains SET phase='prepared',stop_committed_at=NULL WHERE player_id=%s",
    ):
        with pytest.raises(CheckViolation, match="committed drain is immutable"):
            with registry.db.transaction() as conn:
                conn.execute(statement, (player["player_id"],))
    with registry.db.transaction() as conn:
        row = conn.execute(
            "SELECT phase,attempt_id,snapshot->>'admission_scope' AS scope "
            "FROM equipment_drains WHERE player_id=%s", (player["player_id"],),
        ).fetchone()
    assert row == {
        "phase": "stop_committed", "attempt_id": str(command.attempt_id),
        "scope": "unbound_canary",
    }


def test_migration_preserves_older_unbound_stop_without_permit() -> None:
    dsn = os.environ.get("PHOTO_WALL_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("set PHOTO_WALL_TEST_DATABASE_URL for real PostgreSQL integration")
    schema = "pw_old_stop_" + uuid4().hex
    migration_dir = Path(__file__).parents[1] / "central" / "migrations"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    db = Database(make_conninfo(dsn, options=f"-c search_path={schema}"))
    try:
        with db.transaction() as conn:
            for path in sorted(migration_dir.glob("*.sql")):
                if path.name >= "052_":
                    break
                conn.execute(path.read_text())
        old_registry = Registry(db, ManualClock(1000))
        player, _, request = enroll(old_registry)
        seed_historical_committed_stop(old_registry, player, request)
        with db.transaction() as conn:
            conn.execute((migration_dir / "052_unbound_stop_permit_guard.sql").read_text())
            assert conn.execute(
                "SELECT phase,fleet_drain_id FROM equipment_drains WHERE player_id=%s",
                (player["player_id"],),
            ).fetchone() == {"phase": "stop_committed", "fleet_drain_id": None}
    finally:
        db.close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
