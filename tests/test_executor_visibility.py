"""The Pi spends picture slots only on photos you can see (owner, 2026-10-16).

Through the real executor and the simulator's decoder slots: a photo wholly covered by an opaque
one above it holds no slot, is still ready for its reveal, and is prepared within the prepare lead
before it shows; when slots still run short, a Frame gives up its bottom-most layer, not the screen.
"""

from __future__ import annotations

import hashlib

from test_executor import Rig, binding, layer

from contracts.models import Layer, Variant


def jpeg(data: bytes) -> Variant:
    return Variant(asset_id="asset-" + hashlib.sha256(data).hexdigest()[:16], recipe_id="e" * 64,
                   sha256=hashlib.sha256(data).hexdigest(), size=len(data),
                   media_type="image/jpeg", width=1920, height=1080)


def photo(key: str, *, output: str = "hdmi1", frame: str = "frame1", **fields) -> Layer:
    return layer(key, output=output, frame=frame, data=key.encode(), variant=jpeg(key.encode()),
                 **fields)


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


class Wall:
    """Drives the executor as the Player's main loop and Central do: a prepare pass, a readiness
    report, a grant for each newly prepared assignment (never a second one), a draw."""

    def __init__(self, rig: Rig, *layers: Layer):
        self.rig, self.granted = rig, set()
        rig.offer(*layers)
        for item in layers:
            rig.secure(item.assignment_id, item.assignment_id.encode())

    def at(self, instant: float):
        self.rig.advance(instant - self.rig.clock.utc())
        self.rig.executor.prepare_imminent()
        fresh = [key for key in self.rig.executor.readiness().prepared if key not in self.granted]
        if fresh:
            self.rig.commit(*fresh)
            self.granted.update(fresh)
        return self.rig.executor.tick()

    def drawn(self, output: str = "hdmi1") -> list[str]:
        composition = self.rig.renderer.outputs[output]
        assert not composition.fallback
        return [local.layer.assignment_id for local in composition.layers]

    @property
    def resident(self) -> set[str]:
        return set(self.rig.renderer.resident)


def test_two_frames_each_with_a_show_now_over_a_scene_both_draw_and_covered_photos_hold_no_slot(
        tmp_path):
    # Before the rule both screens failed `capacity` from 113 s: three photos a screen, six on a Pi
    # of four slots. Probe: player/visibility.py `hidden` answering False (no layer ever counted
    # covered) fails this test: the covered Scene photos keep their slots.
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
            if instant >= 110:
                # Under the Show now's opaque photos the Scene's photo holds no slot, yet it is
                # still reported ready, so its grant stands for when it is revealed.
                assert f"{tag}-scene" not in wall.resident, instant
                assert f"{tag}-scene" not in wall.drawn(output), instant
                assert f"{tag}-scene" in readiness.prepared, instant
            assert wall.drawn(output)[-1] == (f"{tag}-show2" if instant >= 117 else
                                             f"{tag}-show1" if instant >= 102 else f"{tag}-scene")


def test_during_a_cross_fade_both_photos_are_resident_and_drawn(tmp_path):
    # Probe: player/visibility.py `opaque_span` starting at the layer's start (its fade-in counted
    # opaque) fails this test: the photo dissolving out is treated as covered and dropped.
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
    # fails this test (the Scene's photo is not resident before the Show now ends); so does
    # dropping the pending carry in `prepare_imminent` (a decode in flight unreports it, and
    # Central would take back its grant).
    rig = Rig(tmp_path, capacity=4, bindings=(binding(),))
    scene = photo("scene", start=100, end=200, media_origin=100)
    show = photo("show", start=102, end=130, media_origin=102, fade_in=2, priority=10)
    wall = Wall(rig, scene, show)
    wall.at(100)
    wall.at(102)
    wall.at(110)
    assert "scene" not in wall.resident
    assert "scene" in rig.executor.readiness().prepared
    # Five seconds before the reveal its decoder is getting ready; a decode still in flight does
    # not cost the grant it holds.
    rig.renderer.pending.add("scene")
    wall.at(125)
    assert "scene" in wall.resident
    readiness = rig.executor.readiness()
    assert "scene" in readiness.prepared
    assert readiness.lost_grants == ()
    rig.renderer.pending.clear()
    wall.at(126)
    assert "scene" in wall.resident
    observations = wall.at(130)
    # Shown at its logical position: its photo kept playing while covered.
    assert [(o.assignment_id, o.position) for o in observations if o.status == "presented"] == [
        ("scene", 30)]
    assert wall.drawn() == ["scene"]
    assert wall.granted == {"scene", "show"}


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
    assert [o.assignment_id for o in observations if o.status == "presented"] == ["incoming"]
