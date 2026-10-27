"""Start grants on real PostgreSQL, through the Player's real routes.

A row, and a projected Commit, changes only when its meaning changes: a Player reporting the same
readiness every half second gets the same control delivery back, and Central writes no grant or
configuration row for it."""
from fastapi.testclient import TestClient
from test_coordination import report, schedule, setup_players
from test_registry import ADMIN, hello

from central.app import create_app
from central.coordination import CoordinationLimits, Coordinator
from central.runtime import Contribution, Scene
from contracts.models import LostGrant, Readiness


def _rows(registry, player_id):
    """Each grant, configuration and committed group row's own version (`xmin` changes on every
    rewrite)."""
    with registry.db.transaction() as conn:
        grants = conn.execute("SELECT assignment_id,xmin::text AS version FROM execution_commits "
                              "WHERE player_id=%s ORDER BY assignment_id", (player_id,)).fetchall()
        configuration = conn.execute("SELECT xmin::text AS version FROM player_configurations "
                                     "WHERE player_id=%s", (player_id,)).fetchall()
        groups = conn.execute("SELECT id,xmin::text AS version FROM coordination_groups "
                              "WHERE status='committed' ORDER BY id").fetchall()
    return grants, configuration, groups


def test_unchanged_readiness_reports_rewrite_nothing_and_resend_the_applied_delivery(registry):
    player = setup_players(registry, count=1)[0]
    hello(registry, player)
    app = create_app(registry.db, registry.clock, ADMIN)
    headers = {"Authorization": "Bearer " + player["token"]}
    with TestClient(app) as client:
        coordinator = app.state.coordinator
        schedule(coordinator, ["frame-0"])
        registry.clock.advance(6)

        def ready(sequence):
            readiness = report(coordinator, player, sequence=sequence)
            answer = client.post("/v1/player/readiness", headers=headers,
                                 json=readiness.model_dump(mode="json"))
            assert answer.json() == {"accepted": True}, answer.text

        ready(1)
        state = client.get("/v1/player/state", headers=headers).json()
        assert state["commits"], state
        acked = client.post("/v1/player/control-acks", headers=headers, json={
            "authority_epoch": player["authority_epoch"], "delivery_id": state["delivery_id"],
            "result": "applied"})
        assert acked.json()["accepted"] is True
        before = _rows(registry, player["player_id"])
        assert all(before)
        for sequence in range(2, 12):
            ready(sequence)
            again = client.get("/v1/player/state", headers=headers).json()
            assert (again["delivery_id"], again["commits"]) == (state["delivery_id"], state["commits"])
        assert _rows(registry, player["player_id"]) == before


def _readiness(coordinator, player, sequence, *, prepared=None, lost=()):
    """Every layer of the current plan secured; `prepared` (default: the imminent ones)."""
    plan = coordinator.delivery(player["player_id"], player["authority_epoch"])["plan"]
    now = coordinator.clock.utc()
    if prepared is None:
        prepared = tuple(a.assignment_id for a in plan.layers if a.start <= now + 5 and a.end > now)
    return Readiness(plan_id=plan.plan_id, revision=plan.revision,
                     authority_epoch=plan.authority_epoch, sequence=sequence,
                     secured=tuple(a.assignment_id for a in plan.layers if a.end > now),
                     prepared=prepared, clock_uncertainty=.01, capacity_ok=True, observed_at=now,
                     lost_grants=tuple(LostGrant(assignment_id=key, readiness_sequence=seen)
                                       for key, seen in lost))


def _granted(coordinator, player):
    delivery = coordinator.delivery(player["player_id"], player["authority_epoch"])
    return delivery, [(c.readiness_sequence, c.assignment_ids) for c in delivery["commits"]]


def test_a_lost_grant_is_regranted_once_and_its_cohort_is_never_skipped(registry):
    player = setup_players(registry, count=1)[0]
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0"])
    registry.clock.advance(6)
    assert coordinator.readiness(player["player_id"], _readiness(coordinator, player, 1))
    _, granted = _granted(coordinator, player)
    assert [sequence for sequence, _ in granted] == [1]
    key = granted[0][1][0]

    # Dropped on the Player (a clock blip): it says so, and only that grant is issued again.
    assert coordinator.readiness(player["player_id"],
                                 _readiness(coordinator, player, 2, lost=[(key, 1)]))
    delivery, regranted = _granted(coordinator, player)
    assert delivery["revocations"] == ()
    assert (2, (key,)) in regranted and all(key not in ids for s, ids in regranted if s != 2)
    rows = _rows(registry, player["player_id"])

    # Until the Player sees the new grant it repeats the old one: nothing more is written.
    assert coordinator.readiness(player["player_id"],
                                 _readiness(coordinator, player, 3, lost=[(key, 1)]))
    assert _granted(coordinator, player)[1] == regranted
    assert _rows(registry, player["player_id"]) == rows

    # Refused on arrival (an outage longer than its report window): issued again, once.
    assert coordinator.readiness(player["player_id"],
                                 _readiness(coordinator, player, 4, lost=[(key, 2)]))
    delivery, again = _granted(coordinator, player)
    assert delivery["revocations"] == () and (4, (key,)) in again and (2, (key,)) not in again


def test_a_later_grant_at_the_same_readiness_never_changes_an_accepted_commit(registry):
    player = setup_players(registry, count=1)[0]
    # Cues every 3 s; a 12 s horizon takes in no new cue between 1006.5 and 1008.4, so the
    # plan's revision holds while a second cue falls due on the Player's same report.
    coordinator = Coordinator(registry.db, registry.clock, CoordinationLimits(horizon_seconds=12))
    coordinator.runtime.command("set_scene", Scene(scene_id="quick", loop=True, cycle_seconds=3,
        contributions=(Contribution(target="frame:frame-0", kind="black"),)), by="console")
    coordinator.runtime.command("activate", "quick", "now", 1000, by="console")
    registry.clock.advance(6.5)                   # 1006.5: cues to 1009 are due, 1012 is not
    coordinator.advance()
    plan = coordinator.delivery(player["player_id"], 1)["plan"]
    every = tuple(a.assignment_id for a in plan.layers if a.end > registry.clock.utc())
    assert coordinator.readiness(player["player_id"],
                                 _readiness(coordinator, player, 1, prepared=every))
    _, accepted = _granted(coordinator, player)
    assert accepted
    registry.clock.advance(1.9)                   # 1008.4: the 1012 cue is due on that report
    coordinator.advance()
    delivery, later = _granted(coordinator, player)
    assert delivery["plan"].revision == plan.revision
    assert set(accepted) < set(later) and {sequence for sequence, _ in later} == {1}


def test_a_grant_no_longer_prepared_is_invalidated_once(registry):
    player = setup_players(registry, count=1)[0]
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0"])
    registry.clock.advance(6)
    assert coordinator.readiness(player["player_id"], _readiness(coordinator, player, 1))
    assert _granted(coordinator, player)[1]
    assert coordinator.readiness(player["player_id"],
                                 _readiness(coordinator, player, 2, prepared=()))
    assert _granted(coordinator, player)[1] == []
    rows = _rows(registry, player["player_id"])
    assert coordinator.readiness(player["player_id"],
                                 _readiness(coordinator, player, 3, prepared=()))
    assert _rows(registry, player["player_id"]) == rows
