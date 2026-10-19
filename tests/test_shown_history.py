"""Shown history over PostgreSQL (roadmap 5a, run ledger .claude/runs/photo-5a.md, slice O1):
`PgShownHistory` through the `ShownHistory` port, then coordination's hooks through `Coordinator`
with the Planner's projection scripted (its selections carry a Scene and a pass, as slice W1's
will)."""

import pytest
from support.frames import frame_create
from support.house_time import UTC_HOUSE, at, set_house
from test_coordination import publish_fixture_catalog, schedule, setup_players

import central.coordination as coordination
from central.coordination import Coordinator
from central.infra.shown_history_store import PgShownHistory
from central.wall_plan.order import KeptStill, PhotoShowing
from central.wall_plan.ports import ShownHistory
from contracts.models import Observation

DAY = 86400.0
NOW = 1_000_000_000.0
WEEK = 7 * DAY


def showing(assignment: str, asset: str, *, start: float, end: float | None = None,
            scene: str = "scene", pass_number: int = 1, frame: str = "frame-1",
            fade_in: float = 0, after_end: str = "leave_as_is") -> PhotoShowing:
    return PhotoShowing(assignment_id=assignment, asset_id=asset, frame_id=frame, scene_id=scene,
                        pass_number=pass_number, start=start,
                        end=start + 30 if end is None else end, fade_in=fade_in,
                        after_end=after_end)


@pytest.fixture
def store(registry) -> ShownHistory:
    for frame_id in ("frame-1", "frame-2"):
        registry.create_frame(frame_create(frame_id))
    return PgShownHistory()


def shown_now(store, conn, *showings: PhotoShowing) -> None:
    """Offer each showing, then report it presented."""
    store.record_selected_in(conn, showings)
    for item in showings:
        assert store.record_presented_in(conn, item.assignment_id, item.start)


def test_recording_a_selection_twice_keeps_the_first_row(registry, store):
    first = showing("a-1", "photo-1", start=NOW + 10)
    with registry.db.transaction() as conn:
        store.record_selected_in(conn, [first])
        store.record_selected_in(conn, [first.model_copy(update={"asset_id": "photo-2"})])
        store.record_selected_in(conn, [])
        history = store.wall_history_in(conn, ["scene"], now=NOW, window_seconds=WEEK)
    assert history.selected == (first,)
    assert history.shown == ()


def test_presented_copies_the_selection_once_and_refuses_an_unknown_layer(registry, store):
    offered = showing("a-1", "photo-1", start=NOW - 30, after_end="keep_this_photo", fade_in=2)
    with registry.db.transaction() as conn:
        store.record_selected_in(conn, [offered])
        assert store.record_presented_in(conn, "a-1", NOW - 29)
        assert store.record_presented_in(conn, "a-1", NOW - 20)
        assert not store.record_presented_in(conn, "never-offered", NOW)
        rows = conn.execute("SELECT assignment_id, presented_at FROM shown_photos").fetchall()
        history = store.wall_history_in(conn, ["scene"], now=NOW, window_seconds=WEEK)
    assert [(row["assignment_id"], row["presented_at"]) for row in rows] == [("a-1", NOW - 29)]
    assert history.shown == (offered,)


def test_a_layer_planned_into_night_off_is_not_shown_and_one_outside_it_is(registry, store):
    """Power and the week (F3): planning carries on through Night off, but a photo planned wholly
    into wanted-off time was shown to a dark room. Night off is 23:00 to 07:00 on frame-1 only.
    Mutation probe (P-filter-gone): remove the filter -> the in-Night-off layer is recorded."""
    set_house(registry.db, UTC_HOUSE)
    with registry.db.transaction() as conn:
        conn.execute("""UPDATE night_off SET settings = jsonb_set(settings, '{frames}',
                        '{"kind": "frames", "frame_ids": ["frame-1"]}')""")
    night = at(UTC_HOUSE, "2001-09-09T23:30")
    dark = showing("dark", "photo-1", start=night)
    day = showing("day", "photo-2", start=at(UTC_HOUSE, "2001-09-09T15:00"))
    elsewhere = showing("elsewhere", "photo-3", start=night, frame="frame-2")
    straddling = showing("dawn", "photo-4", start=at(UTC_HOUSE, "2001-09-10T06:59:50"))
    with registry.db.transaction() as conn:
        store.record_selected_in(conn, [dark, day, elsewhere, straddling])
        assert not store.record_presented_in(conn, "dark", night)
        for layer in (day, elsewhere, straddling):
            assert store.record_presented_in(conn, layer.assignment_id, layer.start)
        shown = conn.execute("SELECT assignment_id FROM shown_photos ORDER BY assignment_id").fetchall()
    assert [row["assignment_id"] for row in shown] == ["dawn", "day", "elsewhere"]


def test_wall_history_holds_the_window_the_current_pass_live_selections_and_kept_stills(
        registry, store):
    old_pass = showing("old-pass", "p-old", start=NOW - 20 * DAY, pass_number=1)
    current_old = showing("current-old", "p-cur-old", start=NOW - 10 * DAY, pass_number=2)
    in_window = showing("in-window", "p-window", start=NOW - DAY, pass_number=2)
    other_scene_old = showing("other-old", "q-old", scene="other", start=NOW - 10 * DAY)
    unnamed_in_window = showing("unnamed-window", "u-window", scene="unnamed", start=NOW - DAY)
    # Frame 1 kept a photo, then a later layer kept nothing: nothing is kept there.
    kept_then_cleared = (
        showing("f1-keep", "f1-photo", frame="frame-1", start=NOW - 3 * DAY,
                after_end="keep_this_photo"),
        showing("f1-clear", "f1-next", frame="frame-1", start=NOW - 2 * DAY,
                after_end="keep_nothing"),
    )
    # Frame 2 keeps its photo; a later "leave_as_is" layer changes nothing.
    kept = (
        showing("f2-keep", "f2-photo", frame="frame-2", start=NOW - 2 * DAY, fade_in=2,
                after_end="keep_this_photo"),
        showing("f2-leave", "f2-next", frame="frame-2", start=NOW - DAY),
    )
    live = showing("live", "p-live", start=NOW - 10, end=NOW + 20, pass_number=3)
    ended = showing("ended", "p-ended", start=NOW - 40, end=NOW - 10, pass_number=2)
    with registry.db.transaction() as conn:
        shown_now(store, conn, old_pass, current_old, in_window, other_scene_old,
                  unnamed_in_window, *kept_then_cleared, *kept)
        store.record_selected_in(conn, [live, ended])
        history = store.wall_history_in(conn, ["scene", "other"], now=NOW, window_seconds=WEEK)
    assert {row.assignment_id for row in history.shown} == {
        "current-old", "in-window", "other-old", "unnamed-window", "f1-keep", "f1-clear",
        "f2-keep", "f2-leave"}
    assert history.selected == (live,)
    assert history.kept == (KeptStill(frame_id="frame-2", asset_id="f2-photo",
                                      since=NOW - 2 * DAY + 2),)


def test_a_covered_layer_that_keeps_its_photo_is_kept_though_never_reported_presented(
        registry, store):
    """A covered layer is drawn, so its "keep this photo" applies, but its Player reports only
    visible layers presented (and a report can be lost): the Frame's kept still comes from what
    was offered as well. A layer not yet started keeps nothing yet. Mutation probe: read kept
    stills from presented reports only."""
    presented = showing("f1-shown", "older", frame="frame-1", start=NOW - 300,
                        after_end="keep_this_photo")
    covered = showing("f1-covered", "covered", frame="frame-1", start=NOW - 60, fade_in=1,
                      after_end="keep_this_photo")
    upcoming = showing("f1-next", "upcoming", frame="frame-1", start=NOW + 10,
                       after_end="keep_this_photo")
    with registry.db.transaction() as conn:
        shown_now(store, conn, presented)
        store.record_selected_in(conn, [covered, upcoming])
        history = store.wall_history_in(conn, ["scene"], now=NOW, window_seconds=WEEK)
    assert history.kept == (KeptStill(frame_id="frame-1", asset_id="covered", since=NOW - 59),)


def test_prune_keeps_a_row_399_days_old_and_removes_one_401_days_old(registry, store):
    young = showing("young", "p-young", start=NOW - 399 * DAY - 30)
    old = showing("old", "p-old", start=NOW - 401 * DAY - 30)
    with registry.db.transaction() as conn:
        shown_now(store, conn, young, old)
        store.prune_in(conn, now=NOW)
        shown = conn.execute("SELECT assignment_id FROM shown_photos").fetchall()
        selected = conn.execute("SELECT assignment_id FROM photo_selections").fetchall()
    assert [row["assignment_id"] for row in shown] == ["young"]
    # A selection is kept a day after its layer ends (its presented report may come late).
    assert selected == []


def test_prune_keeps_a_selection_until_a_day_after_its_layer_ends(registry, store):
    recent = showing("recent", "p", start=NOW - DAY + 60, end=NOW - DAY + 90)
    stale = showing("stale", "q", start=NOW - DAY - 90, end=NOW - DAY - 60)
    with registry.db.transaction() as conn:
        store.record_selected_in(conn, [recent, stale])
        store.prune_in(conn, now=NOW)
        rows = conn.execute("SELECT assignment_id FROM photo_selections").fetchall()
    assert [row["assignment_id"] for row in rows] == ["recent"]


# Through Coordinator: the projection is scripted so its selections carry a Scene and a pass.

@pytest.fixture
def scripted_scene_pass(monkeypatch):
    real = coordination.project

    def project(*args, **kwargs):
        projection = real(*args, **kwargs)
        return projection.model_copy(update={"selections": tuple(
            selection.model_copy(update={"scene_id": "scheduled", "pass_number": 1})
            if selection.asset_id else selection
            for selection in projection.selections)})

    monkeypatch.setattr(coordination, "project", project)


def observe(coordinator, player, plan, layer, status):
    coordinator.observe(player["player_id"], Observation(
        plan_id=plan.plan_id, revision=plan.revision, assignment_id=layer.assignment_id,
        authority_epoch=plan.authority_epoch, observed_at=coordinator.clock.utc(),
        status=status, detail="decode" if status == "failed" else "none"))


def rows(registry, table):
    with registry.db.transaction() as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY starts_at, assignment_id").fetchall()


def test_offers_record_selections_and_only_presented_reports_record_shown(
        registry, scripted_scene_pass):
    player = setup_players(registry, count=1)[0]
    publish_fixture_catalog(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0"], starts=1000, media=True)
    plan = coordinator.delivery(player["player_id"], 1)["plan"]
    media = [layer for layer in plan.layers if layer.presentation == "media"]
    assert media
    selected = rows(registry, "photo_selections")
    assert [(row["assignment_id"], row["frame_id"], row["scene_id"], row["pass_number"],
             row["starts_at"], row["ends_at"]) for row in selected] == [
        (layer.assignment_id, "frame-0", "scheduled", 1, layer.start, layer.end)
        for layer in media]

    first, second = media[0], media[1]
    observe(coordinator, player, plan, first, "presented")
    observe(coordinator, player, plan, first, "presented")
    observe(coordinator, player, plan, second, "failed")
    shown = rows(registry, "shown_photos")
    assert [(row["assignment_id"], row["presented_at"]) for row in shown] == [
        (first.assignment_id, registry.clock.utc())]

    # A re-offer of the locked layers records nothing new, even when the photo it would pick
    # changes (a selection's first write wins).
    registry.clock.advance(31)
    publish_fixture_catalog(registry, digest="b" * 64, asset="original-b")
    coordinator.advance()
    again = {row["assignment_id"]: row for row in rows(registry, "photo_selections")}
    for row in selected:
        assert again[row["assignment_id"]] == row


def test_the_next_advance_reads_the_shown_history_of_the_plannable_scenes(
        registry, scripted_scene_pass, monkeypatch):
    player = setup_players(registry, count=1)[0]
    publish_fixture_catalog(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0"], starts=1000, media=True)
    plan = coordinator.delivery(player["player_id"], 1)["plan"]
    layer = next(layer for layer in plan.layers if layer.presentation == "media")
    observe(coordinator, player, plan, layer, "presented")

    seen = []
    real = coordination.project

    def spy(*args, **kwargs):
        seen.append(kwargs["history"])
        return real(*args, **kwargs)

    monkeypatch.setattr(coordination, "project", spy)
    coordinator.advance()
    assert [row.assignment_id for row in seen[0].shown] == [layer.assignment_id]
    assert layer.assignment_id in {row.assignment_id for row in seen[0].selected}


def test_a_presented_report_racing_the_next_offer_still_counts_as_shown(
        registry, scripted_scene_pass):
    """A new revision is offered every cycle, so a Player's "presented" report often carries the
    revision before the latest: the photo still counts as shown, while the report is still
    refused as stale (the Player's cue to fetch the newer plan). Mutation probe: record nothing
    for a report against an earlier revision."""
    player = setup_players(registry, count=1)[0]
    publish_fixture_catalog(registry)
    coordinator = Coordinator(registry.db, registry.clock)
    schedule(coordinator, ["frame-0"], starts=1000, media=True)
    old = coordinator.delivery(player["player_id"], 1)["plan"]
    first, second = [layer for layer in old.layers if layer.presentation == "media"][:2]
    for _ in range(40):       # until the next cycle's layer is offered
        registry.clock.advance(15)
        coordinator.advance()
        if coordinator.delivery(player["player_id"], 1)["plan"].revision > old.revision:
            break
    assert coordinator.delivery(player["player_id"], 1)["plan"].revision > old.revision
    with pytest.raises(coordination.CoordinationError, match="stale_observation"):
        observe(coordinator, player, old, first, "presented")    # the Player's cue to renew
    assert [row["assignment_id"] for row in rows(registry, "shown_photos")] == [
        first.assignment_id]
    with pytest.raises(coordination.CoordinationError, match="stale_observation"):
        observe(coordinator, player, old, second, "failed")
