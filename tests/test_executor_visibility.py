"""The Pi spends picture slots only on photos you can see (owner, 2026-10-16).

Through the real executor and the simulator's decoder slots: a covered photo keeps its decoder
while slots are free and gives it up first when they run short (the kept photo, then the
bottom-most covered layer), stays ready for its reveal and is prepared again within the prepare
lead before it shows; only when that is not enough does a Frame give up its bottom-most seen layer,
never the whole screen.
"""

from __future__ import annotations

import hashlib

from test_executor import Rig, binding, layer

from contracts.models import Layer, Variant
from contracts.time import ManualClock
from player.rendering import LocalLayer, PrepareResult, RecordingRenderer
from player.visibility import shown_within


def jpeg(data: bytes) -> Variant:
    return Variant(asset_id="asset-" + hashlib.sha256(data).hexdigest()[:16], recipe_id="e" * 64,
                   sha256=hashlib.sha256(data).hexdigest(), size=len(data),
                   media_type="image/jpeg", width=1920, height=1080)


def photo(key: str, *, output: str = "hdmi1", frame: str = "frame1", media_type: str = "image/jpeg",
          **fields) -> Layer:
    variant = jpeg(key.encode()).model_copy(update={"media_type": media_type})
    return layer(key, output=output, frame=frame, data=key.encode(), variant=variant, **fields)


def scene_and_show_now(tag: str, output: str, frame: str) -> tuple[Layer, ...]:
    """A Scene playing on a Frame and a Show now over it that cross-fades its two photos (3 s): the
    first fades in over the Scene, the second dissolves in over the first before its cut at 120."""
    return (
        photo(f"{tag}-scene", output=output, frame=frame, start=100, end=200, media_origin=100),
        photo(f"{tag}-show1", output=output, frame=frame, start=102, end=120, media_origin=102,
              fade_in=2, priority=10),
        photo(f"{tag}-show2", output=output, frame=frame, start=117, end=140, media_origin=117,
              fade_in=3, priority=10),
    )


class SlowRenderer(RecordingRenderer):
    """Decodes as the native renderer does: a picture it holds no decoder for takes its slot at
    once and is `pending` for `decode` seconds."""

    def __init__(self, clock: ManualClock, decode: float, capacity: int):
        super().__init__(capacity)
        self.clock, self.decode, self.asked = clock, decode, {}

    def prepare(self, layer: LocalLayer, binding) -> PrepareResult:
        key = layer.layer.assignment_id
        asked = self.asked.setdefault(key, self.clock.utc())
        result = super().prepare(layer, binding)
        if (result.status == "prepared" and layer.layer.variant is not None
                and self.clock.utc() - asked < self.decode):
            return PrepareResult("pending")
        return result

    def release(self, assignment_id: str) -> None:
        self.asked.pop(assignment_id, None)
        super().release(assignment_id)


def slow(rig: Rig, decode: float) -> Rig:
    rig.renderer = rig.executor.renderer = SlowRenderer(rig.clock, decode, rig.renderer.limit)
    return rig


class Wall:
    """Drives the executor as the Player's main loop and Central do: a prepare pass, a readiness
    report, a grant for each newly prepared assignment (never a second one), a draw."""

    def __init__(self, rig: Rig, *layers: Layer):
        self.rig, self.granted, self.revision = rig, set(), 1
        rig.offer(*layers)
        for item in layers:
            rig.secure(item.assignment_id, item.assignment_id.encode())

    def revise(self, *layers: Layer) -> None:
        """A revised plan (a Finish, a new arbitration): Central grants its members afresh."""
        self.revision += 1
        self.rig.offer(*layers, revision=self.revision)
        self.granted.clear()

    def at(self, instant: float, *, limit: int | None = None):
        self.rig.advance(instant - self.rig.clock.utc())
        self.rig.executor.prepare_imminent()
        fresh = [key for key in self.rig.executor.readiness().prepared if key not in self.granted]
        if fresh:
            self.rig.commit(*fresh)
            self.granted.update(fresh)
        if limit is not None:
            self.rig.renderer.limit = limit
        return self.rig.executor.tick()

    def drawn(self, output: str = "hdmi1") -> list[str]:
        composition = self.rig.renderer.outputs[output]
        assert not composition.fallback
        return [local.layer.assignment_id for local in composition.layers]

    def fallback(self, output: str = "hdmi1") -> list[str]:
        composition = self.rig.renderer.outputs[output]
        assert composition.fallback
        return [local.layer.assignment_id for local in composition.layers]

    @property
    def resident(self) -> set[str]:
        return set(self.rig.renderer.resident)


def presented(observations) -> list[str]:
    return [o.assignment_id for o in observations if o.status == "presented"]


def test_two_frames_each_with_a_show_now_over_a_scene_both_draw_and_covered_photos_yield_slots(
        tmp_path):
    # Before the rule both screens failed `capacity` from 112 s: three photos a screen, six on a Pi
    # of four slots. Probe: player/visibility.py `hidden` answering False (no layer ever counted
    # covered) fails this test: the Scene photos are given up as seen layers (`capacity`).
    rig = Rig(tmp_path, capacity=4)
    wall = Wall(rig, *scene_and_show_now("a", "hdmi1", "frame1"),
                *scene_and_show_now("b", "hdmi2", "frame2"))
    for instant in (100, 102, 105, 110, 113, 115, 118, 121, 125):
        observations = wall.at(instant)
        readiness = rig.executor.readiness()
        assert readiness.capacity_ok, instant
        assert readiness.failures == (), instant
        assert readiness.lost_grants == (), instant
        assert not [o for o in observations if o.status == "failed"], instant
        assert len(wall.resident) <= 4, instant
        for tag, output in (("a", "hdmi1"), ("b", "hdmi2")):
            # Covered or not, the Scene's photo is reported ready, so its grant stands.
            assert f"{tag}-scene" in readiness.prepared, instant
            if 113 <= instant < 120:
                # Both Show now photos need slots: the covered Scene photo gives its up.
                assert f"{tag}-scene" not in wall.resident, instant
                assert f"{tag}-scene" not in wall.drawn(output), instant
            if instant in (105, 110, 121, 125):
                # Slots free: covered, it keeps its decoder.
                assert f"{tag}-scene" in wall.resident, instant
            assert wall.drawn(output)[-1] == (f"{tag}-show2" if instant >= 117 else
                                             f"{tag}-show1" if instant >= 102 else f"{tag}-scene")


def test_during_a_cross_fade_both_photos_are_resident_and_drawn(tmp_path):
    # Probe: player/visibility.py `opaque_span` starting at the layer's start (its fade-in counted
    # opaque) fails this test: the photo dissolving out is treated as covered and given up.
    rig = Rig(tmp_path, capacity=4)
    wall = Wall(rig, *scene_and_show_now("a", "hdmi1", "frame1"),
                *scene_and_show_now("b", "hdmi2", "frame2"))
    for instant in (100, 102, 110, 115, 118):
        wall.at(instant)
    for tag, output in (("a", "hdmi1"), ("b", "hdmi2")):
        assert {f"{tag}-show1", f"{tag}-show2"} <= wall.resident
        assert wall.drawn(output) == [f"{tag}-show1", f"{tag}-show2"]
        incoming = rig.renderer.outputs[output].layers[-1]
        assert 0 < incoming.alpha < 1


def test_a_covered_photo_is_prepared_within_the_lead_and_shows_when_the_top_run_ends(tmp_path):
    # Probes: the executor's visibility window ending at `now` instead of `now + prepare_lead`
    # fails this test (the Scene's photo, bottom-most of the covered, gives up its slot again
    # instead of the photo covered for good on the other Frame); so does dropping the pending
    # carry in `prepare_imminent` (a decode in flight unreports it, and Central would take back
    # its grant).
    rig = slow(Rig(tmp_path, capacity=3), 1)
    scene = photo("scene", start=99, end=200, media_origin=99)
    show = photo("show", start=102, end=130, media_origin=102, fade_in=2, priority=10)
    beneath = photo("beneath", output="hdmi2", frame="frame2", start=100, end=200)
    above = photo("above", output="hdmi2", frame="frame2", start=100, end=200, priority=10)
    wall = Wall(rig, scene, show, beneath, above)
    for instant in (100, 101, 102, 105, 110, 115, 120):
        wall.at(instant)
        assert rig.executor.readiness().failures == (), instant
    assert "scene" not in wall.resident
    assert "scene" in rig.executor.readiness().prepared
    # Five seconds before the reveal it is seen within the lead: it takes a slot back from the
    # photo still covered, and its decode starts; the decode in flight costs no grant.
    wall.at(125)
    assert "scene" in wall.resident
    assert "beneath" not in wall.resident
    readiness = rig.executor.readiness()
    assert "scene" in readiness.prepared
    assert readiness.lost_grants == ()
    wall.at(126)
    observations = wall.at(130)
    # Shown at once, at its logical position: its photo kept playing while covered.
    assert [(o.assignment_id, o.position) for o in observations if o.status == "presented"] == [
        ("scene", 31), ("above", 30)]
    assert wall.drawn() == ["scene"]


def test_a_kept_photo_gives_up_its_slot_first_and_is_ready_again_before_it_shows(tmp_path):
    # Verifier finding 1 (de07bbb0): the kept photo, released under a Show, was black for its
    # decode on reveal. Probes: always keeping the kept photo resident fails this test (a Show
    # photo is given up for it); never preparing it ahead (only at the fallback) fails it (black).
    rig = slow(Rig(tmp_path, capacity=2, bindings=(binding(),)), 1)
    kept = photo("kept", start=100, end=110, media_origin=100, after_end="keep_this_photo")
    first = photo("show1", start=112, end=122, media_origin=112, priority=10)
    second = photo("show2", start=119, end=130, media_origin=119, fade_in=3, priority=10)
    wall = Wall(rig, kept, first, second)
    for instant in (100, 101, 105, 110, 111, 112, 113, 115, 118, 120):
        wall.at(instant)
        readiness = rig.executor.readiness()
        assert readiness.failures == (), instant
        assert readiness.capacity_ok, instant
    assert "kept" not in wall.resident
    assert wall.drawn() == ["show1", "show2"]
    for instant in (123, 125, 126.5, 128, 129.75):
        wall.at(instant)
    assert "kept" in wall.resident
    wall.at(130)
    assert wall.fallback() == ["kept"]


def test_a_reveal_on_short_notice_keeps_its_grant_and_draws_once_decoded(tmp_path):
    # Verifier finding 2 (de07bbb0): a Finish 0.5 s before the Show's last cut, a 2 s decode: the
    # Scene's photo lost its grant and the screen went to fallback. Probe: a pending decode in
    # tick's reveal ending the grant (`_lose_preparation`) fails this test.
    rig = slow(Rig(tmp_path, capacity=2, bindings=(binding(),)), 2)
    scene = photo("scene", start=100, end=200, media_origin=100)
    first = photo("show1", start=102, end=128, media_origin=102, fade_in=2, priority=10)
    second = photo("show2", start=125, end=150, media_origin=125, fade_in=3, priority=10)
    wall = Wall(rig, scene, first, second)
    for instant in (100, 101, 102, 105, 110, 115, 120, 122, 124):
        wall.at(instant)
    assert "scene" not in wall.resident
    # Finish: the Show's second photo withdrawn, its first fades out to the Scene beneath.
    wall.revise(scene, first.model_copy(update={"fade_out": 3}))
    shown = []
    for instant in (124.5, 125, 125.5, 126, 126.5, 127, 127.5, 128, 129, 130):
        shown += presented(wall.at(instant))
        readiness = rig.executor.readiness()
        assert readiness.lost_grants == (), instant
        assert readiness.failures == (), instant
        assert "scene" in readiness.prepared, instant
        assert not rig.renderer.outputs["hdmi1"].fallback, instant
    assert "scene" in shown
    assert wall.drawn() == ["scene"]


def test_cover_is_judged_on_the_granted_order_not_a_revision_awaiting_its_grant(tmp_path):
    # Verifier finding 3 (de07bbb0): a revision swapping two layers' priority before Central grants
    # it released the photo still drawn on top. Probe: judging cover on `assignment.layer` (the
    # offer) instead of the grant fails this test.
    rig = Rig(tmp_path, capacity=1, bindings=(binding(),))
    top = photo("top", start=100, end=200, media_origin=100, priority=10)
    beneath = photo("beneath", start=100, end=200, media_origin=100)
    wall = Wall(rig, top, beneath)
    wall.at(100)
    wall.at(101)
    assert wall.drawn() == ["top"]
    rig.offer(top.model_copy(update={"priority": 0}), beneath.model_copy(update={"priority": 10}),
              revision=2)
    for instant in (101.5, 102, 103):
        wall.at(instant)
        assert wall.drawn()[-1] == "top", instant
        assert "top" in wall.resident, instant
        assert rig.executor.readiness().failures == (), instant


def test_short_slots_drop_the_frames_bottom_layer_and_its_top_still_draws(tmp_path):
    # Probe: player/visibility.py `shed` answering None (nothing to give up) fails this test: the
    # whole screen fails `capacity`, as before.
    rig = Rig(tmp_path, capacity=1, bindings=(binding(),))
    outgoing = photo("outgoing", start=100, end=120, media_origin=100)
    incoming = photo("incoming", start=117, end=140, media_origin=117, fade_in=3)
    wall = Wall(rig, outgoing, incoming)
    wall.at(100)
    assert wall.drawn() == ["outgoing"]
    observations = wall.at(118)
    readiness = rig.executor.readiness()
    assert readiness.capacity_ok
    assert [(f.assignment_id, f.code) for f in readiness.failures] == [("outgoing", "capacity")]
    assert wall.resident == {"incoming"}
    assert wall.drawn() == ["incoming"]
    assert presented(observations) == ["incoming"]


def test_slots_lost_at_the_draw_drop_the_bottom_layer_not_the_screen(tmp_path):
    # Probe: no giving up in tick's capacity loop (`shed` not consulted there) fails this test.
    rig = Rig(tmp_path, capacity=2, bindings=(binding(),))
    wall = Wall(rig, photo("outgoing", start=100, end=120, media_origin=100),
                photo("incoming", start=117, end=140, media_origin=117, fade_in=3))
    wall.at(100)
    observations = wall.at(118, limit=1)
    assert [(o.assignment_id, o.detail) for o in observations if o.status == "failed"] == [
        ("outgoing", "capacity")]
    assert presented(observations) == ["incoming"]
    assert wall.drawn() == ["incoming"]


def test_a_layer_given_up_at_the_draw_sets_nothing_its_frame_keeps(tmp_path):
    # Probe: applying the after-state of a layer given up in tick (ignoring `given_up`) fails this
    # test: the Frame keeps a photo never drawn.
    rig = Rig(tmp_path, capacity=2, bindings=(binding(),))
    wall = Wall(rig, photo("never", start=100, end=101, media_origin=100, after_end="keep_this_photo"),
                photo("top", start=100, end=110, media_origin=100, priority=10))
    observations = wall.at(100, limit=1)
    assert presented(observations) == ["top"]
    wall.at(111)
    assert wall.fallback() == []


def test_a_full_strength_png_hides_nothing_beneath_it(tmp_path):
    # Probe: player/visibility.py `opaque` counting a full-strength PNG opaque fails this test.
    rig = Rig(tmp_path, capacity=4, bindings=(binding(),))
    wall = Wall(rig, photo("base", start=100, end=150, media_origin=100),
                photo("overlay", media_type="image/png", start=100, end=140, media_origin=100,
                      priority=10))
    assert presented(wall.at(100)) == ["base", "overlay"]
    assert wall.drawn() == ["base", "overlay"]


def test_the_photo_dissolving_out_is_seen_until_its_cut():
    # Probe: player/visibility.py `opaque_span` starting at the layer's start (its fade-in counted
    # opaque) fails this test: the photo dissolving out would count as covered.
    outgoing = photo("outgoing", start=100, end=120)
    incoming = photo("incoming", start=117, end=140, fade_in=3)
    assert shown_within((outgoing,), (incoming,), 117.5, 119.9) == {"outgoing"}
    assert shown_within((outgoing,), (incoming,), 120, 125) == set()
