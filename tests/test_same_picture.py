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
    FakeVideo,
    NoLuts,
    framed,
    named,
)

from contracts.models import Continuation, Variant
from contracts.photo_crop import PhotoCrop
from player.plane_renderer import PlaneRenderer
from player.planes import Screen


def jpeg(data: bytes) -> Variant:
    """An opaque still: a JPEG copy of `data`."""
    return Variant(asset_id="asset-" + hashlib.sha256(data).hexdigest()[:16], recipe_id="e" * 64,
                   sha256=hashlib.sha256(data).hexdigest(), size=len(data), media_type="image/jpeg",
                   width=1920, height=1080)


class Screens(FakeScreens):
    """tests/test_plane_renderer.py's screens, one per Output asked for."""

    def screen(self, output_id):
        return Screen(output_id, self.screen_now.size, 0)


class Wall:
    """The executor with the plane renderer, on one clock: each refresh the executor ticks, then the
    renderer presents its held composition again and finishes the GPU work owed."""

    def __init__(self, directory: Path, bindings=None) -> None:
        self.plan = PlanRig(directory, bindings=bindings or (framed(),))
        clock = self.plan.clock
        self.screens = Screens(clock.monotonic)
        self.device, self.fitter = FakeDevice(), FakeFitter()
        self.stills: list[Path] = []
        self.videos: list[FakeVideo] = []
        self.tags: list[str] = []
        self.renderer = PlaneRenderer(self.screens, self.device, self.fitter, FakePatterns(), NoLuts(),
                                      stills=self._still, videos=self._video, clock=clock.monotonic)
        self.plan.renderer = self.plan.executor.renderer = self.renderer

    def _still(self, path, region):
        self.stills.append(path)
        return FakeStill(region.size)

    def _video(self, path, on_frame):
        self.videos.append(FakeVideo(on_frame))
        self.videos[-1].push(1)
        return self.videos[-1]

    def run(self, until: float) -> list[tuple[tuple, ...]]:
        """Refresh until plan time `until`: what each new commit shows, as (picture handle,
        strength) per plane bottom to top, with the plan time it was committed at first."""
        shown = []
        serial = 1
        while self.plan.clock.utc() < until:
            commits = len(self.screens.commits)
            serial += 1
            for video in self.videos:                   # each decode makes a new frame every refresh
                video.push(serial)
                video.on_frame()
            self.plan.executor.prepare_imminent()
            self.plan.executor.tick()
            self.renderer.frame()
            self.renderer.finish()
            for composition, pictures, tag, _change in self.screens.commits[commits:]:
                shown.append((round(self.plan.clock.utc(), 3), *(
                    (pictures[named(plane)].handle, round(plane.strength, 6)) for plane in composition.planes)))
                self.tags.append((round(self.plan.clock.utc(), 3), tag))
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
    # The Frame witness still names what is drawn: past the plan's layer, each of three slots is a
    # commit of the one picture whose tag names the new slot.
    assert len({tag for at, tag in wall.tags if at >= 101}) == 3


@pytest.mark.parametrize(("seconds", "fade"), [(10, 1), (30, 12), (28, 14)])
def test_a_slot_that_fades_out_and_in_to_the_same_photo_keeps_the_picture(tmp_path, seconds, fade):
    """Fade out and in (no lead): the plan's photo would fade out over its last `fade` seconds, then
    each slot fade in and out; the next picture is the same, so none of it is drawn: no dip to
    black. Fade-outs longer than the prepare lead (12 s) included: the next slot is held before it
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


def pair(directory: Path, kind: str = "jpeg", *, first: dict | None = None, second: dict | None = None) -> Wall:
    """Two planned layers on hdmi1 of one copy (`kind`: a JPEG, a PNG or a video), the second
    starting at 108 and dissolving in over 2 s over the first, which plays to 110 (each with the
    fields of `first` and `second` instead)."""
    wall = Wall(directory)
    data = b"a"
    copy = {"variant": jpeg(data)} if kind == "jpeg" else {}
    wall.plan.offer(
        layer("a", data=data, video=kind == "video", **{
            "start": 100, "end": 110, "awaits_grant": False, **copy, **(first or {})}),
        layer("b", data=data, video=kind == "video", **{
            "start": 108, "end": 130, "media_origin": 100, "fade_in": 2, "awaits_grant": False, **copy,
            **(second or {})}),
        valid_until=130)
    wall.plan.secure("a", data)
    wall.plan.secure("b", data)
    return wall


def dissolves(shown) -> bool:
    """Whether some commit while the second layer dissolves in (108 to 110) shows two planes."""
    return any(len(commit) == 3 for commit in shown if 108 <= commit[0] < 110)


@pytest.mark.parametrize(("kind", "differs"), [
    ("jpeg", {"crop": PhotoCrop(offset=0.2)}),      # another window of the photo
    ("video", {"media_origin": 108}),               # the video from its start again, not where it plays
], ids=["crop", "video-origin"])
def test_the_same_copy_drawn_otherwise_is_a_change_and_drawn_alike_is_not(tmp_path, kind, differs):
    """player/planes.py `drawn_identity`, field by field: the second layer of one copy that differs
    in exactly one drawn field (its crop window; a video's media origin) is another picture, so it
    dissolves in as planned and is decoded on its own; the same layer drawn alike (a video going on
    from where it plays) keeps the picture: one plane at full, one decode."""
    decodes = (lambda wall: wall.videos) if kind == "video" else (lambda wall: wall.stills)
    changed = pair(tmp_path / "changed", kind, second=differs)
    shown = changed.run(112)
    assert dissolves(shown)
    assert len(decodes(changed)) == 2
    alike = pair(tmp_path / "alike", kind)
    shown = alike.run(112)
    assert not dissolves(shown)
    assert all(len(commit) == 2 and commit[1][1] == 1.0 for commit in shown if commit[0] >= 101)
    assert len(decodes(alike)) == 1


def test_a_half_strength_layer_fades_out_though_the_same_photo_follows_at_full(tmp_path):
    """Opacity is drawn: the first layer at half strength fades out over its last 2 s although the
    next layer is the same photo at full (another picture), never held at half to a cut."""
    wall = pair(tmp_path, first={"opacity": 0.5, "fade_out": 2}, second={"start": 109, "fade_in": 1})
    shown = wall.run(109)
    before, fading = [commit for commit in shown if commit[0] < 108][-1], shown[-1]
    assert fading[0] >= 108.9 and len(fading) == 2 and fading[1][1] < before[1][1] - 0.1


def test_two_frames_showing_one_photo_each_draw_their_own_picture(tmp_path):
    """The Output and Frame are drawn: one photo on two Frames at once is two pictures, each
    finished for its own Frame (a picture shared across Frames would be finished for one of them)."""
    wall = Wall(tmp_path, bindings=(framed(), framed(output="hdmi2", frame="frame2")))
    wall.plan.offer(layer("a", data=b"a", variant=jpeg(b"a"), start=100, end=120, awaits_grant=False),
                    layer("b", output="hdmi2", frame="frame2", data=b"a", variant=jpeg(b"a"), start=100, end=120,
                          awaits_grant=False), valid_until=120)
    wall.plan.secure("a", b"a")
    wall.plan.secure("b", b"a")
    wall.run(102)
    shown = {screen.output_id: pictures for screen, pictures in (
        (composition.screen, pictures) for composition, pictures, _tag, _change in wall.screens.commits)}
    assert shown["hdmi1"]["a"].handle != shown["hdmi2"]["b"].handle
    assert len(wall.fitter.draws) == 2


def test_a_png_of_the_same_photo_still_cross_fades(tmp_path):
    """Only an opaque picture is merged: a PNG may carry transparency, so the next layer of the same
    PNG dissolves in as planned and the one before it fades out."""
    wall = pair(tmp_path, "png", first={"fade_out": 2})
    shown = wall.run(112)
    assert dissolves(shown)


def test_a_kept_photo_fades_out_while_another_layer_plays_on_beneath(tmp_path):
    """The kept photo is what follows only when nothing else plays: a Run beneath plays on past the
    kept photo's end, so the kept photo fades out over its last 2 s to show it, never cut."""
    wall = Wall(tmp_path)
    wall.plan.offer(
        layer("kept", data=b"a", variant=jpeg(b"a"), start=100, end=110, fade_out=2, priority=1,
              after_end="keep_this_photo", awaits_grant=False),
        layer("beneath", data=b"b", variant=jpeg(b"b"), start=100, end=130, awaits_grant=False),
        valid_until=130)
    wall.plan.secure("kept", b"a")
    wall.plan.secure("beneath", b"b")
    shown = wall.run(111)
    fading = [commit for commit in shown if 108.2 <= commit[0] < 109.8]
    assert fading and all(len(commit) == 3 and 0 < commit[2][1] < 1 for commit in fading)


def test_a_rejoin_to_a_new_plans_layer_of_the_same_photo_keeps_the_picture(tmp_path):
    """Central replans to a layer of the photo on screen starting later (decision 0032, rejoining):
    the superseded layer is held, still, until the new one starts and would dissolve in over 2 s;
    it shows the same picture, so the screen keeps it throughout."""
    wall = Wall(tmp_path)
    wall.plan.offer(layer("a", data=b"a", variant=jpeg(b"a"), start=100, end=120, awaits_grant=False),
                    valid_until=200)
    wall.plan.secure("a", b"a")
    wall.run(105)
    wall.plan.offer(layer("b", data=b"a", variant=jpeg(b"a"), start=110, end=200, media_origin=110, fade_in=2,
                          awaits_grant=False), revision=2, valid_until=200)
    shown = wall.run(108)
    wall.plan.secure("b", b"a")
    shown += wall.run(125)
    (picture,) = held(shown, 105)
    assert len(picture) == 1 and picture[0][1] == 1.0
    assert len(wall.stills) == 1


@pytest.mark.parametrize("why", ["not secured", "no grant"])
def test_a_next_layer_of_the_same_photo_that_cannot_draw_leaves_the_fade_out(tmp_path, why):
    """What a Frame draws next is a layer that can draw (player/executor.py `_following`): the next
    layer of the same photo that is not secured, or awaits a grant it does not have, never draws, so the photo before it fades out over its last 2 s as planned
    instead of holding at full to a cut."""
    wall = Wall(tmp_path)
    nxt = layer("b", data=b"a", variant=jpeg(b"a"), start=110, end=130, media_origin=110,
                awaits_grant=why == "no grant")
    wall.plan.offer(layer("a", data=b"a", variant=jpeg(b"a"), start=100, end=110, fade_out=2, awaits_grant=False),
                    nxt, valid_until=130)
    wall.plan.secure("a", b"a")
    if why != "not secured":
        wall.plan.secure("b", b"a")
    shown = wall.run(109.5)
    strengths = [commit[1][1] for commit in shown if 108.1 <= commit[0] < 109.5]
    assert strengths and strengths == sorted(strengths, reverse=True) and strengths[-1] < 0.8


def test_what_the_executor_reads_as_seen_is_what_the_renderer_draws(tmp_path):
    """One look (player/look.py): every composition the executor presents carries its layers at the
    alphas the renderer draws it with at that instant, so the layers the executor reads as seen
    (player/visibility.py `visible_now`: what it prepares, starts, witnesses and fails) are the ones
    drawn, also where the seam rule leaves out a fade (the next layer of the photo at full from its
    start)."""
    from player.look import look
    from player.visibility import visible_now

    wall = Wall(tmp_path)
    wall.plan.offer(layer("a", data=b"a", variant=jpeg(b"a"), start=100, end=110, awaits_grant=False),
                    layer("b", data=b"a", variant=jpeg(b"a"), start=107, end=130, media_origin=107, fade_in=3,
                          awaits_grant=False), valid_until=130)
    wall.plan.secure("a", b"a")
    wall.plan.secure("b", b"a")
    handed = []
    present = wall.renderer.present

    def recorded(composition):
        handed.append((composition, wall.plan.clock.monotonic()))
        return present(composition)

    wall.renderer.present = recorded
    wall.run(108)
    seen = [([local.layer.assignment_id for local in visible_now(composition.layers)],
             [local.layer.assignment_id for local in visible_now(look(composition, at).layers)])
            for composition, at in handed if composition.timebase is not None]
    assert seen and all(executor == renderer for executor, renderer in seen)
    assert (["b"], ["b"]) in seen
