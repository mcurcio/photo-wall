"""The unbound drain can join one future command issuer transaction."""

from contextlib import contextmanager

import pytest
from test_registry import enroll

from central.coordination import Coordinator
from central.equipment_drain import EquipmentDrain
from central.registry import RegistryError
from central.transaction_locks import acquire_runtime_locks


@contextmanager
def _cut(drain):
    with drain.coordinator._transaction() as conn:
        acquire_runtime_locks(conn)
        yield conn


def test_unbound_prepare_and_commit_roll_back_with_callers_transaction(registry) -> None:
    player, _, request = enroll(registry)
    player_id = player["player_id"]
    epoch = player["authority_epoch"]
    drain = EquipmentDrain(Coordinator(registry.db, registry.clock))

    with pytest.raises(RuntimeError, match="later_preparation_failure"):
        with _cut(drain) as conn:
            prepared = drain.prepare_unbound_in(
                conn, player_id, "attempt-unbound", request.boot_id, epoch,
                authorization_expires_at=1010,
            )
            assert prepared.status == "prepared"
            assert conn.execute(
                "SELECT phase FROM equipment_drains WHERE player_id=%s", (player_id,),
            ).fetchone()["phase"] == "prepared"
            raise RuntimeError("later_preparation_failure")

    with registry.db.transaction() as conn:
        assert conn.execute(
            "SELECT 1 FROM equipment_drains WHERE player_id=%s", (player_id,),
        ).fetchone() is None

    with _cut(drain) as conn:
        assert drain.prepare_unbound_in(
            conn, player_id, "attempt-unbound", request.boot_id, epoch,
            authorization_expires_at=1010,
        ).status == "prepared"

    with pytest.raises(RuntimeError, match="later_permit_failure"):
        with _cut(drain) as conn:
            committed = drain.commit_stop_unbound_in(
                conn, player_id, "attempt-unbound", request.boot_id, epoch,
            )
            assert committed.status == "stop_committed"
            assert conn.execute(
                "SELECT phase FROM equipment_drains WHERE player_id=%s", (player_id,),
            ).fetchone()["phase"] == "stop_committed"
            raise RuntimeError("later_permit_failure")

    with registry.db.transaction() as conn:
        row = conn.execute(
            "SELECT phase,stop_committed_at FROM equipment_drains WHERE player_id=%s",
            (player_id,),
        ).fetchone()
    assert row == {"phase": "prepared", "stop_committed_at": None}

    with _cut(drain) as conn:
        assert drain.commit_stop_unbound_in(
            conn, player_id, "attempt-unbound", request.boot_id, epoch,
        ).status == "stop_committed"
    with registry.db.transaction() as conn:
        assert conn.execute(
            "SELECT phase FROM equipment_drains WHERE player_id=%s", (player_id,),
        ).fetchone()["phase"] == "stop_committed"


def test_unbound_caller_owned_seam_refuses_missing_runtime_locks(registry) -> None:
    player, _, request = enroll(registry)
    drain = EquipmentDrain(Coordinator(registry.db, registry.clock))
    with registry.db.transaction() as conn:
        with pytest.raises(RegistryError, match="runtime_snapshot_required"):
            drain.prepare_unbound_in(
                conn, player["player_id"], "attempt-unbound", request.boot_id,
                player["authority_epoch"], authorization_expires_at=1010,
            )
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM equipment_drains").fetchone()["n"] == 0
    drain.prepare_unbound(
        player["player_id"], "attempt-unbound", request.boot_id,
        player["authority_epoch"], authorization_expires_at=1010,
    )
    with registry.db.transaction() as conn:
        with pytest.raises(RegistryError, match="runtime_snapshot_required"):
            drain.commit_stop_unbound_in(
                conn, player["player_id"], "attempt-unbound", request.boot_id,
                player["authority_epoch"],
            )
    with registry.db.transaction() as conn:
        assert conn.execute(
            "SELECT phase FROM equipment_drains WHERE player_id=%s",
            (player["player_id"],),
        ).fetchone()["phase"] == "prepared"
