"""A Frame changes what it draws only when the picture changes (owner, 2026-11-15: "The player
shouldn't be running any translations if the displayed content isn't going to change"): the real
executor drives the real plane renderer (its GPU, compositor and decoders faked, as
tests/test_plane_renderer.py fakes them) frame by frame, and every commit is read for what the
screen shows. When the next slot, layer or plan draws the same photo as the one on screen, the
screen keeps it: one plane, at full, from the one finished picture, with no fade, no dip and no
second decode. A real change still fades as before.

One rule decides it (player/look.py, the seam rule, by player/planes.py `drawn_identity`), so each
path here goes through the same decision: a continuation's slot advance (a cross-fade and a fade out
and in), the step from a plan's last layer to its continuation, two planned layers of one photo,
a new plan's layer, and the photo a Frame keeps when nothing follows."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from test_executor import Rig as PlanRig
from test_executor import layer
from test_plane_renderer import (
    FRAME,
    FakeDevice,
    FakeFitter,
    FakePatterns,
    FakeScreens,
    FakeStill,
    NoLuts,
    framed,
    named,
)

from contracts.models import Continuation, Variant
from player.plane_renderer import PlaneRenderer


def jpeg(data: bytes) -> Variant:
    """An opaque still: a JPEG copy of `data`."""
    return Variant(asset_id="asset-" + hashlib.sha256(data).hexdigest()[:16], recipe_id="e" * 64,
                   sha256=hashlib.sha256(data).hexdigest(), size=len(data), media_type="image/jpeg",
                   width=1920, height=1080)


class Wall:
    """The executor with the plane renderer, on one clock: each refresh the executor ticks, then the
    renderer presents its held composition again and finishes the GPU work owed."""

    def __init__(self, directory: Path) -> None:
        self.plan = PlanRig(directory, bindings=(framed(),))
        clock = self.plan.clock
        self.screens = FakeScreens(clock.monotonic)
        self.device, self.fitter = FakeDevice(), FakeFitter()
        self.stills: list[Path] = []
        self.renderer = PlaneRenderer(self.screens, self.device, self.fitter, FakePatterns(), NoLuts(),
                                      stills=self._still, videos=self._video, clock=clock.monotonic)
        self.plan.renderer = self.plan.executor.renderer = self.renderer

    def _still(self, path, region):
        self.stills.append(path)
        return FakeStill(region.size)

    @staticmethod
    def _video(path, on_frame):
        raise AssertionError("no video here")

    def run(self, until: float) -> list[tuple[tuple, ...]]:
        """Refresh until plan time `until`: what each new commit shows, as (picture handle,
        strength) per plane bottom to top, with the plan time it was committed at first."""
        shown = []
        while self.plan.clock.utc() < until:
            commits = len(self.screens.commits)
            self.plan.executor.prepare_imminent()
            self.plan.executor.tick()
            self.renderer.frame()
            self.renderer.finish()
            for composition, pictures, _tag, _change in self.screens.commits[commits:]:
                shown.append((round(self.plan.clock.utc(), 3), *(
                    (pictures[named(plane)].handle, round(plane.strength, 6)) for plane in composition.planes)))
            self.plan.advance(FRAME)
        return shown


def held(shown, since: float) -> set[tuple]:
    """What the commits from plan time `since` on show (each commit's planes, its time left out)."""
    return {commit[1:] for commit in shown if commit[0] >= since}


def one_photo(directory: Path, **continuation) -> Wall:
    """A plan of one photo on hdmi1 until plan time 120, then a continuation of that photo alone
    in turns of 10 s from 120 (its lead and fades as given)."""
    wall = Wall(directory)
    photo = layer("a", data=b"a", variant=jpeg(b"a"), start=100, end=120, awaits_grant=False)
    wall.plan.offer(photo, valid_until=125, continuations=(Continuation(
        output_id="hdmi1", run_id="run", photos=("a",), start=120, seconds=10, **continuation),))
    wall.plan.secure("a", b"a")
    return wall


def test_a_slot_that_cross_fades_to_the_same_photo_keeps_the_picture(tmp_path):
    """A continuation of one photo, cross-fading over 2 s: past the first draw, every commit
    through three slot changes shows the one picture alone at full, decoded once."""
    wall = one_photo(tmp_path, lead=2, fade_in=2)
    shown = wall.run(142)
    (picture,) = held(shown, 101)
    assert len(picture) == 1 and picture[0][1] == 1.0
    assert len(wall.stills) == 1 and len(wall.fitter.draws) == 1


@pytest.mark.parametrize(("seconds", "fade"), [(10, 1), (30, 12)])
def test_a_slot_that_fades_out_and_in_to_the_same_photo_keeps_the_picture(tmp_path, seconds, fade):
    """Fade out and in (no lead): the plan's photo would fade out over its last `fade` seconds, then
    each slot fade in and out; the next picture is the same, so none of it is drawn: no dip to
    black. A fade-out longer than the prepare lead (12 s) included: the next slot is held before it
    starts."""
    wall = Wall(tmp_path)
    photo = layer("a", data=b"a", variant=jpeg(b"a"), start=100, end=120, fade_out=fade, awaits_grant=False)
    wall.plan.offer(photo, valid_until=125, continuations=(Continuation(
        output_id="hdmi1", run_id="run", photos=("a",), start=120, seconds=seconds, fade_in=fade, fade_out=fade),))
    wall.plan.secure("a", b"a")
    shown = wall.run(120 + 2 * seconds + 2)
    (picture,) = held(shown, 101)
    assert len(picture) == 1 and picture[0][1] == 1.0
    assert len(wall.stills) == 1


def test_two_planned_layers_of_one_photo_keep_the_picture(tmp_path):
    """A scarce pool: the plan's next layer, its own assignment, shows the same photo and would
    dissolve in over 3 s; the screen keeps the picture it shows."""
    wall = Wall(tmp_path)
    first = layer("a", data=b"a", variant=jpeg(b"a"), start=100, end=110, awaits_grant=False)
    second = layer("b", data=b"a", variant=jpeg(b"a"), start=107, end=130, media_origin=107, fade_in=3,
                   awaits_grant=False)
    wall.plan.offer(first, second, valid_until=130)
    wall.plan.secure("a", b"a")
    wall.plan.secure("b", b"a")
    shown = wall.run(115)
    (picture,) = held(shown, 101)
    assert len(picture) == 1 and picture[0][1] == 1.0
    assert len(wall.stills) == 1


def test_a_new_plans_layer_of_the_same_photo_keeps_the_picture(tmp_path):
    """Central replans: the new revision's layer, a new assignment, shows the photo on screen and
    fades in over 2 s; the screen keeps it."""
    wall = Wall(tmp_path)
    wall.plan.offer(layer("a", data=b"a", variant=jpeg(b"a"), start=100, end=120, awaits_grant=False),
                    valid_until=200)
    wall.plan.secure("a", b"a")
    wall.run(105)
    wall.plan.offer(layer("b", data=b"a", variant=jpeg(b"a"), start=106, end=200, media_origin=106, fade_in=2,
                          awaits_grant=False), revision=2, valid_until=200)
    wall.plan.secure("b", b"a")
    shown = wall.run(125)
    assert len(held(shown, 105)) == 1 and next(iter(held(shown, 105)))[0][1] == 1.0
    assert len(wall.stills) == 1


def test_the_last_slot_hands_over_to_the_kept_photo_without_a_dip(tmp_path):
    """A continuation that stops at 140 of a photo the Frame keeps: its last slot would fade out
    and the fallback then show the kept photo at full; the next picture is the same, so the screen
    keeps it through the fallback."""
    wall = Wall(tmp_path)
    photo = layer("a", data=b"a", variant=jpeg(b"a"), start=100, end=120, awaits_grant=False,
                  after_end="keep_this_photo")
    wall.plan.offer(photo, valid_until=125, continuations=(Continuation(
        output_id="hdmi1", run_id="run", photos=("a",), start=120, seconds=10, fade_in=1, fade_out=1,
        until=140),))
    wall.plan.secure("a", b"a")
    shown = wall.run(145)
    (picture,) = held(shown, 101)
    assert len(picture) == 1 and picture[0][1] == 1.0
    assert len(wall.stills) == 1


def test_a_layer_of_another_photo_still_cross_fades(tmp_path):
    """A real change fades exactly as before: the plan's next layer, another photo, dissolves in
    over 2 s (the incoming at full beneath, the shown one above at 1 - its strength, down to
    nothing), each photo decoded once."""
    wall = Wall(tmp_path)
    first = layer("a", data=b"a", variant=jpeg(b"a"), start=100, end=110, awaits_grant=False)
    second = layer("b", data=b"b", variant=jpeg(b"b"), start=108, end=130, media_origin=108, fade_in=2,
                   awaits_grant=False)
    wall.plan.offer(first, second, valid_until=130)
    wall.plan.secure("a", b"a")
    wall.plan.secure("b", b"b")
    shown = wall.run(112)
    dissolve = [commit for commit in shown if 108 <= commit[0] < 110]
    assert len(dissolve) > 100
    assert all(len(commit) == 3 and commit[1][1] == 1.0 for commit in dissolve)
    assert dissolve[0][1][0] != dissolve[0][2][0]
    strengths = [commit[2][1] for commit in dissolve]
    assert strengths == sorted(strengths, reverse=True) and 0 < strengths[-1] < 0.1 and strengths[0] > 0.9
    assert shown[-1][1:] == ((dissolve[0][1][0], 1.0),)             # the incoming photo alone
    assert len(wall.stills) == 2
