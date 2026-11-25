"""Skip, Previous and Never show through the real Coordinator over PostgreSQL (decision 0036,
slice 3; the owner's rulings S1, S2, S4 and S5).

A Wall of Frames, each on its own Pi heard by Central, playing one Scene of prepared portraits.
Each action goes through the command under the Coordination lock, judged by the rule Home's `can`
reads (Frame status's facts), and the next planning pass offers its covers. Nothing here claims
what a screen showed: the Player's drawing of a cover is the executor's tests and the bench's.
"""
from __future__ import annotations

import itertools

import pytest
from support.devices import seed_device
from support.frames import frame_create
from support.measured_photos import measure_all
from support.photo_pools import library_of, photo, seed_pool
from support.position import PORTRAIT_ON_WIDE, save_position
from test_coordination import prepare, report
from test_registry import enroll, hello

from central.content_catalog.catalog import device_id_for_serial
from central.coordination import Coordinator
from central.db import DatabaseTransactionClock
from central.everyday.model import SkipKind, SkipScope, StayReason
from central.everyday.policy import photo_of, showing_at
from central.frame_status.model import Action
from central.infra.fit_analysis_store import PgFitAnalysisReader
from central.infra.frame_status_store import PgFrameStatus
from central.infra.house_store import seed_house
from central.infra.player_contact_store import opened_in
from central.infra.skip_store import KEEP_SECONDS, SkipRetention
from central.registry import RegistryError
from central.runtime import Contribution, Scene
from central.runtime_store import RuntimeStore
from central.wall_plan.fit_analysis import FitSelection
from contracts.models import Observation

pytestmark = pytest.mark.db

PHOTOS = tuple(photo(f"portrait-{n:02}", taken_at=900 + n, width=2400, height=3600) for n in range(12))


def status_of(registry) -> PgFrameStatus:
    """Frame status as central/app.py wires it, with Coordination's layer read."""
    return PgFrameStatus(registry.db, registry.clock, times=DatabaseTransactionClock(),
                         inventory=registry.inventory_in, plans=Coordinator.frame_plans_in,
                         runtime=RuntimeStore.read_in, layers=Coordinator.frame_layers_in)


class Wall:
    """Frames hung on walls, each on its own Pi that Central hears, playing the Scene "evenings"
    from `PHOTOS` (a Frame in `hand_placed` shows one photo by hand)."""

    def __init__(self, registry, frames: dict[str, str], *, cycle: float = 60, photos=PHOTOS,
                 hand_placed=(), silent=()):
        self.registry = registry
        seed_house(registry.db, DatabaseTransactionClock())
        self.players = {}
        for number, (frame_id, surface) in enumerate(sorted(frames.items())):
            serial = f"10000000d4a3e1{number:02}"
            device = device_id_for_serial(serial)
            with registry.db.transaction() as conn:
                seed_device(conn, device, serial)
            player = enroll(registry, device_id=device)[0]
            hello(registry, player)
            registry.create_frame(frame_create(frame_id, surface_id=surface), by="console")
            registry.bind(frame_id, player["player_id"], "HDMI-A-1", expected_generation=0, by="console")
            save_position(registry, frame_id, PORTRAIT_ON_WIDE)
            if frame_id not in silent:
                with registry.db.transaction() as conn:
                    opened_in(conn, player["player_id"], player["authority_epoch"], registry.clock.utc())
            self.players[frame_id] = player
        seed_pool(registry, "library:1", photos)
        measure_all(registry, photos)
        prepare(registry, photos)
        self.coordinator = Coordinator(registry.db, registry.clock)
        self.status = status_of(registry)
        self.coordinator.runtime.command("set_scene", Scene(
            scene_id="evenings", loop=True, cycle_seconds=cycle, contributions=tuple(
                Contribution(target=f"frame:{frame_id}", kind="media",
                             **({"asset_refs": (photos[0].asset_id,)} if frame_id in hand_placed
                                else {"source_refs": ("library:1",)}))
                for frame_id in sorted(frames))), by="console")
        self.coordinator.runtime.command("activate", "evenings", "now", registry.clock.utc(), by="console")
        self.coordinator.advance()
        self.sequence = itertools.count(1)

    def plan(self, frame_id):
        return self.coordinator.delivery(self.players[frame_id]["player_id"], 1)["plan"]

    def layers(self, frame_id):
        return tuple(layer for layer in self.plan(frame_id).layers if layer.frame_id == frame_id)

    def showing(self, frame_id, at=None):
        return showing_at(self.layers(frame_id), self.registry.clock.utc() if at is None else at)

    def skip(self, scope, target, kind=SkipKind.NEXT, request_id="press-1"):
        return self.coordinator.skip(scope, target, kind, request_id, by="person:matt",
                                     facts=self.status.everyday_in)

    def never_show(self, asset_id, frame_id=None, request_id="never-1"):
        return self.coordinator.never_show(request_id, asset_id, frame_id, by="person:matt",
                                           facts=self.status.everyday_in)

    def tick(self, seconds=0):
        self.registry.clock.advance(seconds)
        return self.coordinator.advance()

    def presented(self, frame_id, layer):
        """The Player reports `layer` presented, as it does when it shows it."""
        plan = self.plan(frame_id)
        self.coordinator.observe(self.players[frame_id]["player_id"], Observation(
            plan_id=plan.plan_id, revision=plan.revision, authority_epoch=1, sequence=next(self.sequence),
            assignment_id=layer.assignment_id, observed_at=self.registry.clock.utc(), status="presented"))

    def ready(self, frame_id):
        player = self.players[frame_id]
        self.coordinator.readiness(player["player_id"], report(self.coordinator, player,
                                                               sequence=next(self.sequence)))

    def frame_status(self, frame_id):
        return self.status.frame(frame_id).status


def covers(wall, frame_id):
    return [layer for layer in wall.layers(frame_id) if layer.action]


def test_a_wall_skip_moves_every_movable_frame_at_one_instant(registry):
    """S1: Skip on a Wall moves each of its Frames to its own next photo at one shared instant,
    as one cue (every cover awaits a grant). Mutation probe: no cue for a wall's covers.""" 
    wall = Wall(registry, {"hall": "wall", "desk": "wall"})
    answer = wall.skip(SkipScope.WALL, "wall")
    assert {moved.frame_id for moved in answer.moved} == {"hall", "desk"} and answer.stayed == ()
    projection = wall.tick()
    hall, desk = covers(wall, "hall"), covers(wall, "desk")
    assert len(hall) == len(desk) == 1
    assert hall[0].start == desk[0].start == answer.at
    assert hall[0].awaits_grant and desk[0].awaits_grant
    cue = {cue.assignment_id: cue.root_id for cue in projection.projection.cues}
    assert cue[hall[0].assignment_id] == cue[desk[0].assignment_id]
    # Each shows the photo its own next turn shows.
    for frame_id, cover in (("hall", hall[0]), ("desk", desk[0])):
        following = next(layer for layer in wall.layers(frame_id)
                         if not layer.action and layer.start > answer.at)
        assert photo_of(cover) == photo_of(following) and cover.end == following.start + following.fade_in


def test_a_wall_skip_names_each_frame_that_stayed_and_why(registry):
    """S1, "if possible": a hand-placed photo and a Pi Central does not hear stay, and the answer
    names each with why; the others move."""
    wall = Wall(registry, {"hall": "wall", "desk": "wall", "attic": "wall"}, hand_placed=("desk",),
                silent=("attic",))
    answer = wall.skip(SkipScope.WALL, "wall")
    assert [moved.frame_id for moved in answer.moved] == ["hall"]
    assert {(stay.frame_id, stay.reason) for stay in answer.stayed} == {
        ("desk", StayReason.HAND_PLACED), ("attic", StayReason.NOT_ANSWERING)}


def test_nothing_moved_is_refused_with_who_stayed(registry):
    wall = Wall(registry, {"desk": "wall"}, hand_placed=("desk",))
    with pytest.raises(RegistryError) as refused:
        wall.skip(SkipScope.WALL, "wall")
    assert refused.value.code == "nothing-moved" and refused.value.status == 409
    assert refused.value.details["stayed"] == [{"frame_id": "desk", "reason": "hand-placed"}]


def test_a_wall_skip_leaves_other_walls_alone(registry):
    wall = Wall(registry, {"hall": "wall", "porch": "garden"})
    answer = wall.skip(SkipScope.WALL, "wall")
    assert [moved.frame_id for moved in answer.moved] == ["hall"]
    wall.tick()
    assert covers(wall, "porch") == []


def test_a_frame_skip_moves_that_frame_alone(registry):
    """A Frame's own Skip moves it alone, with no grant (it waits on no other Frame)."""
    wall = Wall(registry, {"hall": "wall", "desk": "wall"})
    answer = wall.skip(SkipScope.FRAME, "desk")
    assert [moved.frame_id for moved in answer.moved] == ["desk"]
    wall.tick()
    (cover,) = covers(wall, "desk")
    assert covers(wall, "hall") == [] and not cover.awaits_grant
    assert wall.showing("desk", answer.at + 1).assignment_id == cover.assignment_id


def test_unknown_wall_and_frame_are_404(registry):
    wall = Wall(registry, {"hall": "wall"})
    for scope, target in ((SkipScope.WALL, "nowhere"), (SkipScope.FRAME, "nothing")):
        with pytest.raises(RegistryError) as refused:
            wall.skip(scope, target)
        assert refused.value.status == 404


def test_skip_leaves_the_covered_layer_unchanged(registry):
    """Central never changes an offered layer for an everyday action: every layer offered before
    the skip is offered again as it was, the one on screen included."""
    wall = Wall(registry, {"hall": "wall"})
    before = {layer.assignment_id: layer for layer in wall.layers("hall")}
    wall.skip(SkipScope.FRAME, "hall")
    wall.tick()
    after = {layer.assignment_id: layer for layer in wall.layers("hall")}
    assert all(after[identity] == layer for identity, layer in before.items() if layer.end > registry.clock.utc())


def test_skip_beyond_the_reach_takes_the_scene_lines_next(registry):
    """A turn longer than the offer reaches (15 minutes): the cover is the Scene line's next
    prepared photo, never the photo left, and ends with the layer it covers."""
    wall = Wall(registry, {"hall": "wall"}, cycle=900)
    left = photo_of(wall.showing("hall"))
    answer = wall.skip(SkipScope.FRAME, "hall")
    wall.tick()
    (cover,) = covers(wall, "hall")
    assert cover.start == answer.at and photo_of(cover) not in (None, left)
    assert cover.end == wall.showing("hall").end


def shown_twice(wall, frame_id):
    """The Frame shows its first turn and then its second, each reported presented; answers the
    two photos, oldest first."""
    first = wall.showing(frame_id)
    wall.presented(frame_id, first)
    wall.tick(first.end - wall.registry.clock.utc() + 1)
    second = wall.showing(frame_id)
    wall.presented(frame_id, second)
    return photo_of(first), photo_of(second)


def test_previous_shows_the_photo_the_frame_last_reported(registry):
    """Previous pins the photo the Frame reported before the one it shows. Mutation probe: step
    back from the top of the history whatever shows."""
    wall = Wall(registry, {"hall": "wall"}, cycle=30)
    first, second = shown_twice(wall, "hall")
    answer = wall.skip(SkipScope.FRAME, "hall", SkipKind.PREVIOUS)
    assert [moved.frame_id for moved in answer.moved] == ["hall"]
    wall.tick()
    (cover,) = covers(wall, "hall")
    assert photo_of(cover) == first != second


def test_previous_again_steps_back_one_more(registry):
    """S2: each further press steps back one more photo."""
    wall = Wall(registry, {"hall": "wall"}, cycle=30)
    zeroth = photo_of(wall.showing("hall"))
    wall.presented("hall", wall.showing("hall"))
    wall.tick(31)
    first, second = shown_twice(wall, "hall")
    assert zeroth != first
    wall.skip(SkipScope.FRAME, "hall", SkipKind.PREVIOUS, request_id="back-1")
    wall.tick()
    wall.tick(1)
    wall.skip(SkipScope.FRAME, "hall", SkipKind.PREVIOUS, request_id="back-2")
    wall.tick()
    back = {photo_of(layer): layer.start for layer in covers(wall, "hall")}
    assert set(back) == {first, zeroth} and back[zeroth] > back[first]


def test_previous_with_nothing_earlier_stays(registry):
    wall = Wall(registry, {"hall": "wall"})
    with pytest.raises(RegistryError) as refused:
        wall.skip(SkipScope.FRAME, "hall", SkipKind.PREVIOUS)
    assert refused.value.details["stayed"] == [{"frame_id": "hall", "reason": "no-previous"}]


def test_skip_may_show_a_photo_another_frame_shows(registry):
    """S4: Previous shows the hall's earlier photo though the desk shows it now: an everyday
    action may cause a brief overlap rather than be refused."""
    wall = Wall(registry, {"hall": "wall", "desk": "wall"}, cycle=30)
    on_desk = wall.showing("desk")
    hall = wall.showing("hall")
    wall.presented("hall", hall)
    with registry.db.transaction() as conn:    # the hall showed the desk's photo before
        conn.execute("INSERT INTO shown_photos(assignment_id,asset_id,frame_id,scene_id,pass_number,starts_at,"
                     "ends_at,fade_in,after_end,presented_at,by) VALUES('earlier',%s,'hall','evenings',1,900,"
                     "930,0,'leave_as_is',901,'node')", (photo_of(on_desk),))
    wall.skip(SkipScope.FRAME, "hall", SkipKind.PREVIOUS)
    wall.tick()
    (cover,) = covers(wall, "hall")
    assert photo_of(cover) == photo_of(on_desk) == photo_of(wall.showing("desk", cover.start + 1))


def test_a_double_tap_is_one_skip(registry):
    wall = Wall(registry, {"hall": "wall"})
    first = wall.skip(SkipScope.FRAME, "hall")
    registry.clock.advance(1)
    again = wall.skip(SkipScope.FRAME, "hall")
    assert again.duplicate and not first.duplicate
    assert (again.at, again.moved) == (first.at, first.moved)
    with registry.db.transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM skips").fetchone()["n"] == 1


def test_skip_reads_requested_until_the_cover_is_presented(registry):
    """FrameStatus.asked: requested once asked, done once the Player reports the cover presented;
    Skip is in `can` while the Frame would move, and the Wall offers it."""
    wall = Wall(registry, {"hall": "wall"})
    status = wall.frame_status("hall")
    assert {Action.SKIP, Action.NEVER_SHOW} <= set(status.can) and Action.PREVIOUS not in status.can
    home = wall.status.home()
    assert Action.SKIP in home.walls[0].can
    answer = wall.skip(SkipScope.FRAME, "hall")
    assert (wall.frame_status("hall").asked.state, wall.frame_status("hall").asked.request_id) == (
        "requested", answer.request_id)
    wall.tick()
    wall.ready("hall")
    registry.clock.advance(3)
    (cover,) = covers(wall, "hall")
    wall.presented("hall", cover)
    assert wall.frame_status("hall").asked.state == "done"


def test_a_skip_is_late_when_not_presented_in_time(registry):
    wall = Wall(registry, {"hall": "wall"})
    answer = wall.skip(SkipScope.FRAME, "hall")
    wall.tick(answer.at + 31 - registry.clock.utc())
    assert wall.frame_status("hall").asked.state == "late"


def test_never_show_skips_it_where_it_shows(registry):
    """Never show from the hall's menu: the photo is listed and skipped on the hall at once."""
    wall = Wall(registry, {"hall": "wall"})
    listed = photo_of(wall.showing("hall"))
    answer = wall.never_show(listed, "hall")
    assert answer.skipped_on == ("hall",) and answer.stayed == ()
    wall.tick()
    (cover,) = covers(wall, "hall")
    assert photo_of(cover) != listed


def test_never_show_of_a_photo_that_turned_over_is_refused(registry):
    wall = Wall(registry, {"hall": "wall"})
    other = next(item.asset_id for item in PHOTOS if item.asset_id != photo_of(wall.showing("hall")))
    with pytest.raises(RegistryError) as refused:
        wall.never_show(other, "hall")
    assert refused.value.code == "changed"


def test_never_show_lets_a_downloaded_showing_elsewhere_play_once_and_names_it(registry):
    """S5: a photo already offered for another Frame's later slot (downloaded or downloading for
    it) shows there that one last time; the answer names it, and its layer stays offered."""
    wall = Wall(registry, {"hall": "wall", "desk": "wall"}, cycle=30)
    later = next(layer for layer in wall.layers("desk") if layer.start > registry.clock.utc() + 30)
    answer = wall.never_show(photo_of(later))
    offered = {("desk", layer.start, layer.end) for layer in wall.layers("desk")
               if photo_of(layer) == photo_of(later)}
    assert ("desk", later.start, later.end) in offered
    assert {(each.frame_id, each.start, each.until) for each in answer.last_showings} == offered
    wall.tick()
    assert later in wall.layers("desk")


def test_a_listed_photo_is_never_assigned_again(registry):
    """No new placement takes a listed photo, across many re-plans; only what was offered before
    it was listed shows it."""
    wall = Wall(registry, {"hall": "wall", "desk": "wall"}, cycle=30)
    listed = photo_of(wall.showing("hall"))
    offered = {layer.assignment_id for frame_id in ("hall", "desk") for layer in wall.layers(frame_id)}
    wall.never_show(listed)
    for _ in range(40):
        wall.tick(30)
        for frame_id in ("hall", "desk"):
            assert not [layer for layer in wall.layers(frame_id)
                        if photo_of(layer) == listed and layer.assignment_id not in offered]


def test_an_empty_pool_does_not_bring_a_listed_photo_back(registry):
    only = PHOTOS[:1]
    wall = Wall(registry, {"hall": "wall"}, cycle=30, photos=only)
    wall.never_show(only[0].asset_id, request_id="only")
    wall.tick(400)
    assert [layer for layer in wall.layers("hall") if photo_of(layer) == only[0].asset_id] == []
    assert wall.plan("hall").unfilled


def test_show_again_returns_it_to_the_line(registry):
    wall = Wall(registry, {"hall": "wall"}, cycle=30, photos=PHOTOS[:2])
    listed = photo_of(wall.showing("hall"))
    wall.never_show(listed)
    assert listed in {entry.asset_id for entry in wall.coordinator.never_show_entries().photos}
    assert wall.coordinator.show_again(listed) and not wall.coordinator.show_again(listed)
    seen = set()
    for _ in range(12):
        wall.tick(30)
        seen |= {photo_of(layer) for layer in wall.layers("hall")}
    assert listed in seen


def test_fit_and_pool_counts_leave_out_listed_photos(registry):
    """S9: the Frame page's fit counts and a Source's pool counts leave out Never show photos."""
    wall = Wall(registry, {"hall": "wall"})
    reader = PgFitAnalysisReader(registry.db, times=DatabaseTransactionClock())
    selection = FitSelection(sources=("library:1",))
    library = library_of(registry)
    before = sum(group.photos for group in reader.selection_sizes(selection).groups)
    pool_before = library.source_pool("library:1", None)
    listed = photo_of(wall.showing("hall"))
    wall.never_show(listed)
    assert sum(group.photos for group in reader.selection_sizes(selection).groups) == before - 1
    pool_after = library.source_pool("library:1", None)
    assert pool_after.in_pool.photos == pool_before.in_pool.photos - 1
    assert listed not in {item.asset_id for item in pool_after.items}


def test_the_skip_retention_deletes_rows_a_day_past_their_last_cover_in_whole_batches(registry):
    """central/infra/batched_retention.py's contract for `skips`: bounded, idempotent, a row kept
    until a day past its last cover's end. Mutation probe: drop the `ends_at` bound."""
    wall = Wall(registry, {"hall": "wall"})
    for number in range(5):
        wall.skip(SkipScope.FRAME, "hall", request_id=f"press-{number}")
    retention = SkipRetention(registry.db.transaction, registry.clock, batch_rows=2, batches=4)
    assert retention.prune() == {"skips": 0}
    registry.clock.advance(KEEP_SECONDS + 3600)
    wall.tick()
    with registry.db.transaction() as conn:      # its Pi is heard again
        opened_in(conn, wall.players["hall"]["player_id"], 1, registry.clock.utc())
    wall.skip(SkipScope.FRAME, "hall", request_id="kept")
    assert retention.prune() == {"skips": 5} and retention.prune() == {"skips": 0}
    with registry.db.transaction() as conn:
        assert [row["request_id"] for row in conn.execute("SELECT request_id FROM skips")] == ["kept"]
