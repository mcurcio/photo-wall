"""The tracer bench's pure parts (scripts/render_bench): the plan and what each moment must show,
the kernel's state dump, Weston's composites read from its drm-backend stream, the glitch bits and
the verdict."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from contracts.models import PositionBox
from player.geometry import encode_srgb
from player.plane_renderer import PlaneRenderer
from player.planes import Screen
from player.rendering import LocalLayer, OutputComposition
from scripts.render_bench import timeline
from scripts.render_bench.__main__ import _Bench
from scripts.render_bench.controller import BenchController
from scripts.render_bench.evidence import Moment, verdict
from scripts.render_bench.kms_state import KmsPlane, driven_by, kms_planes, shown_planes
from scripts.render_bench.registers import STICKY_GLITCH, RegisterSample, glitched
from scripts.render_bench.weston_log import renderer_framebuffers

FIXTURE = Path(__file__).parent / "fixtures" / "render_bench" / "kms-state.txt"
WESTON_LOG = Path(__file__).parent / "fixtures" / "render_bench" / "weston-drm-backend.txt"
SCREEN = (3840, 2160)


@pytest.fixture
def plan(tmp_path: Path) -> timeline.Tracer:
    photos = []
    for index in range(4):
        path = tmp_path / f"photo-{index}.jpg"
        path.write_bytes(b"jpeg %d" % index)
        photos.append(timeline.Media(path, "photo", SCREEN, None))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"mp4")
    return timeline.tracer("HDMI-A-1", photos, timeline.Media(clip, "video", (1920, 1080), 20.0),
                           origin=1000.0, fade=2.0, hold=4.0, soak=30.0)


def test_the_tracer_steps_in_order_turning_for_the_turn_and_back_for_the_overload(plan):
    assert [(step.name, step.turn) for step in plan.steps] == [
        ("hold", 0), ("dissolve", 0), ("hold", 0), ("dip", 0), ("hold", 0), ("photo_to_video", 0), ("hold", 0),
        ("turn", 90), ("dissolve", 90), ("hold", 90), ("soak", 0), ("overload", 0)]
    assert plan.steps[0].start == 1000.0
    assert all(before.end == after.start for before, after in zip(plan.steps, plan.steps[1:]))


def test_the_outgoing_picture_stays_at_full_through_every_dissolve(plan):
    dissolves = [step for step in plan.steps if step.name in ("dissolve", "photo_to_video")]
    soak = next(step for step in plan.steps if step.name == "soak")
    soak_fades = [(layer.start, layer.start + layer.fade_in) for layer in plan.layers
                  if layer.assignment_id.startswith("soak-")]
    assert soak_fades
    for start, end in [(step.start, step.end) for step in dissolves] + soak_fades:
        for share in (0.25, 0.5, 0.75):
            at = start + share * (end - start)
            size = (2160, 3840) if timeline.step_at(plan, at).turn == 90 else SCREEN
            strengths = list(timeline.strengths_at(plan, at, size).values())
            assert strengths == [pytest.approx(1.0), pytest.approx(share)], (at, strengths)
    assert soak.start <= soak_fades[0][0]


def test_mid_dissolve_is_one_and_the_share_and_mid_dip_is_its_srgb(plan):
    dissolve = plan.steps[1]
    assert timeline.strengths_at(plan, dissolve.start + 0.6, SCREEN) == {
        "photo-1": pytest.approx(1.0), "photo-2": pytest.approx(0.3)}
    dip = plan.steps[3]
    leaving = timeline.strengths_at(plan, dip.start + 0.3, SCREEN)          # photo 2 at 0.7, over black
    assert leaving == {"dip-black": pytest.approx(1.0), "photo-2": pytest.approx(encode_srgb(0.7))}
    coming = timeline.strengths_at(plan, dip.end - 0.4, SCREEN)             # photo 3 at 0.6
    assert coming == {"dip-black": pytest.approx(1.0), "photo-3": pytest.approx(encode_srgb(0.6))}


def test_the_ground_shows_unless_the_bottom_picture_covers_the_screen_at_full(plan):
    dissolve, dip, overload = plan.steps[1], plan.steps[3], plan.steps[-1]
    assert not timeline.ground_seen(plan, dissolve.start + 0.6, SCREEN)        # photo 1 at full beneath
    assert not timeline.ground_seen(plan, dip.start + 0.3, SCREEN)             # the dip's black layer
    assert timeline.ground_seen(plan, overload.start + 1, SCREEN)              # five at half strength
    turned = next(step for step in plan.steps if step.turn == 90)
    assert not timeline.ground_seen(plan, turned.start + 1, (2160, 3840))     # the whole-screen box, turned


def test_the_overload_is_cut_to_the_budget(plan):
    overload = plan.steps[-1]
    strengths = timeline.strengths_at(plan, overload.start + 1, SCREEN)
    assert list(strengths) == ["overload-2", "overload-3", "overload-4"]


def test_what_lies_beneath_a_kept_picture_is_judged_over_the_kept_pictures(plan):
    """The cut sheds the lower pictures (photo 4, overload 1), so the bottom kept picture is over
    the screen's black: the dip rule (its sRGB), the ones above it over a picture (the share).
    Mutation probe: judge beneath over the uncut stack -> overload 2 is 0.5 and this fails."""
    strengths = timeline.strengths_at(plan, plan.steps[-1].start + 1, SCREEN)
    assert strengths == {"overload-2": pytest.approx(encode_srgb(0.5)), "overload-3": 0.5, "overload-4": 0.5}


def test_the_tracers_frame_is_a_centred_full_width_rectangle(plan):
    assert plan.binding.calibration.fit == PositionBox()


class _Screen:
    """PlaneScreens as PlaneRenderer.capacity asks it: the 4K screen, no grant (so no trial)."""

    def screen(self, output_id):
        return Screen(output_id, SCREEN, 0)

    def grant(self, composition):
        return None

    def picture(self, output_id):
        return None


def test_the_overload_step_sheds_to_three_pictures_by_the_renderers_capacity(plan):
    """The bench stands in for the executor: it gives up visibility.shed's victims until the
    renderer's capacity says the composition fits, and the three pictures left are those the
    moments expect (strengths_at)."""
    at = plan.steps[-1].start + 1
    renderer = PlaneRenderer(_Screen(), None, None, None, None, stills=None, videos=None, clock=lambda: at)
    shown = tuple(LocalLayer(layer, None, 0.0, 1.0) for layer in timeline.layers_at(plan, at))
    assert len(shown) == timeline.OVERLOAD_PICTURES
    kept = timeline.shed_overload(OutputComposition(plan.binding, shown, timebase=0.0), renderer.capacity)
    assert [local.layer.assignment_id for local in kept.layers] == list(timeline.strengths_at(plan, at, SCREEN))
    assert len(kept.layers) == 3


def test_a_tracer_needs_four_photos_and_a_video(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"mp4")
    photo = tmp_path / "p.jpg"
    photo.write_bytes(b"jpeg")
    with pytest.raises(ValueError, match="^tracer_media$"):
        timeline.tracer("HDMI-A-1", [timeline.Media(photo, "photo", SCREEN, None)] * 3,
                        timeline.Media(clip, "video", (1920, 1080), 20.0), origin=0.0)


def test_the_recorded_state_dump_parses_merged_with_the_properties():
    planes = kms_planes(FIXTURE.read_text(), {83: (0x8000, "Coverage")})
    assert planes == (KmsPlane(83, 94, 686, (3840, 2160), (0, 0), 0, 1, 0x8000, "Coverage"),)
    assert kms_planes(FIXTURE.read_text(), {})[0].alpha == 0xFFFF


def test_an_overlay_plane_comes_above_the_primary_by_zpos():
    text = FIXTURE.read_text().replace(
        "plane[107]: plane-4\n\tcrtc=(null)\n\tfb=0\n\tcrtc-pos=0x0+0+0\n\tsrc-pos=0.000000x0.000000+0.000000+0.000000",
        "plane[107]: plane-4\n\tcrtc=crtc-2\n\tfb=700\n\tcrtc-pos=2160x1215+0+1313\n"
        "\tsrc-pos=2160.000000x1215.000000+0.000000+0.000000")
    planes = kms_planes(text, {107: (0x4CCC, "Pre-multiplied")})
    assert [(plane.plane_id, plane.position, plane.source_size, plane.zpos) for plane in planes] == [
        (83, (0, 0), (3840, 2160), 0), (107, (0, 1313), (2160, 1215), 1)]


def test_the_planes_shown_are_those_with_a_framebuffer():
    assert shown_planes(FIXTURE.read_text()) == (83,)


def test_westons_composites_are_the_framebuffers_its_renderer_drew_into():
    """A mixed repaint borrows one renderer framebuffer (685) and applies the one it drew (683) to
    the same plane; a render-only repaint applies its own (683); a plane-only repaint shows a
    client's buffer (686), never Weston's."""
    assert renderer_framebuffers(WESTON_LOG.read_text()) == {683, 685}
    cut = WESTON_LOG.read_text().split("[atomic] applying output", 1)[0]
    assert renderer_framebuffers(cut) == {685}             # a stream cut short: what it says so far


def test_text_that_is_not_a_state_dump_is_refused():
    with pytest.raises(ValueError, match="^kms_state$"):
        kms_planes("hello\n", {})


def test_only_a_newly_set_glitch_bit_counts():
    clean, set_before = RegisterSample(0.0, 0x420), RegisterSample(0.0, 0x420 | 0x200)
    assert not glitched(clean, RegisterSample(1.0, 0x4FF))             # other bits move freely
    assert glitched(clean, RegisterSample(1.0, 0x420 | 0x800))
    assert not glitched(set_before, RegisterSample(1.0, 0x420 | 0x200))
    assert glitched(set_before, RegisterSample(1.0, 0x420 | STICKY_GLITCH))


RENDERER = frozenset({686})                 # Weston's composites in these moments
COMPOSITE = KmsPlane(83, 94, 686, (3840, 2160), (0, 0), 0, 1, 0xFFFF, "Pre-multiplied")
GROUND = KmsPlane(83, 94, 690, (3840, 2160), (0, 0), 0, 1, 0xFFFF, "Pre-multiplied")    # the Player's black
FIRST = RegisterSample(0.0, 0x420)


def picture(alpha: float, zpos: int = 1, blend: str = "Pre-multiplied") -> KmsPlane:
    return KmsPlane(100 + zpos, 94, 700 + zpos, (3840, 2160), (0, 0), zpos, 1, round(alpha * 0xFFFF), blend)


def moment(*planes: KmsPlane, expected=None, step: str = "dissolve", status: int = 0x420, drawing: bool = True,
           gpu: float = 0.5, ground: bool = False) -> Moment:
    """A moment showing `planes`; `ground`: the screen's ground must show, and does (GROUND first)."""
    expected = {"out": 1.0, "in": 0.3} if expected is None else expected
    return Moment(10.0, step, expected, ((GROUND,) if ground else ()) + planes, RegisterSample(1.0, status), drawing,
                  gpu, SCREEN, ground)


GOOD = moment(picture(1.0, 0), picture(0.3, 1))         # the outgoing photo shown directly on the primary
OVERLOAD = moment(picture(0.5, 1), step="overload", expected={"top": 0.5}, ground=True)


def test_a_run_that_shows_every_rule_passes():
    assert verdict(FIRST, [GOOD, OVERLOAD, moment(picture(1.0, 0), expected={"one": 1.0}, drawing=False, gpu=0.01)
                           ], RENDERER).passed


def test_a_picture_shown_directly_on_the_primary_is_a_picture_not_the_compositors():
    """Planes only, the outgoing photo on the primary itself (it covers the screen, so no ground):
    two pictures, both checked."""
    assert verdict(FIRST, [GOOD, OVERLOAD], RENDERER).passed
    off = moment(picture(0.5, 0), picture(0.3, 1))
    assert verdict(FIRST, [off, OVERLOAD], RENDERER).failures == ("alpha at 10.000",)


def test_the_ground_shows_on_the_bottom_plane_whenever_the_pictures_leave_the_screen_uncovered():
    """A dip over the ground: the ground, then the picture at its strength. A ground missing (the
    compositor's black) or a picture plane missing beneath a covered ground fails."""
    dip = moment(picture(0.6, 1), expected={"in": 0.6}, ground=True)
    assert verdict(FIRST, [dip, OVERLOAD], RENDERER).passed
    no_ground = Moment(10.0, "dip", {"in": 0.6}, (picture(0.6, 1),), RegisterSample(1.0, 0x420), True, 0.5, SCREEN,
                       True)
    assert verdict(FIRST, [no_ground, OVERLOAD], RENDERER).failures == ("ground at 10.000",)


def test_anything_weston_composited_fails_the_run():
    mixed = moment(COMPOSITE, picture(0.3, 1), expected={"in": 0.3})
    assert verdict(FIRST, [GOOD, mixed, OVERLOAD], RENDERER).failures == ("composited at 10.000",)


def test_without_westons_composites_known_no_plane_can_be_judged():
    assert verdict(FIRST, [GOOD, OVERLOAD]).failures == ("renderer_unknown",)


@pytest.mark.parametrize(("bad", "check"), [
    (moment(picture(1.0, 0), picture(0.3 + 2 / 255, 1)), "alpha"),
    (moment(picture(1.0, 0)), "plane"),
    (moment(picture(1.0, 0), picture(0.3, 1, "Coverage")), "blend"),
    (moment(picture(1.0, 0), picture(0.3, 1), status=0x420 | 0x800), "glitch"),
    (moment(picture(1.0, 0), picture(0.3, 1), drawing=False, gpu=0.2), "gpu"),
])
def test_each_broken_rule_fails_the_run_and_names_its_first_moment(bad, check):
    result = verdict(FIRST, [GOOD, bad, OVERLOAD], RENDERER)
    assert not result.passed and result.failures == (f"{check} at 10.000",)


def test_one_level_off_still_passes():
    assert verdict(FIRST, [moment(picture(1.0, 0), picture(0.3 + 1 / 255, 1)), OVERLOAD], RENDERER).passed


def test_a_dissolve_passes_in_either_plane_order_that_mixes_the_same_screen():
    """The plan's order (outgoing beneath at 1, incoming on top at s) and the Player's (incoming
    beneath at 1, outgoing on top at 1 - s) show the same picture; the bench judges the picture."""
    expected = {"photo-1": 1.0, "photo-2": 0.027}
    plan_order = moment(picture(1.0, 0), picture(0.027, 1), expected=expected)
    kept_planes = moment(picture(1.0, 0), picture(63766 / 0xFFFF, 1), expected=expected)    # recorded on the Pi
    assert verdict(FIRST, [plan_order, kept_planes, OVERLOAD], RENDERER).passed


def test_a_dip_passes_in_either_order_over_the_ground_and_fails_with_every_strength_forced_to_1():
    """A dip: the black layer at 1 beneath the picture at 0.229, or the picture at 1 beneath the
    black at 0.771, mix the same screen. Every strength forced to 1 (the runbook's mutation) shows
    the top layer alone and fails `alpha`."""
    expected = {"dip-black": 1.0, "photo-3": 0.229}
    swapped = moment(picture(1.0, 0), picture(1 - 0.229, 1), expected=expected)
    assert verdict(FIRST, [moment(picture(1.0, 0), picture(0.229, 1), expected=expected), swapped, OVERLOAD],
                   RENDERER).passed
    forced = moment(picture(1.0, 0), picture(1.0, 1), expected=expected, step="dip")
    assert verdict(FIRST, [swapped, forced, OVERLOAD], RENDERER).failures == ("alpha at 10.000",)


@pytest.mark.parametrize("top", [0.5, 1 - 0.027 - 2 / 255, 0.027 + 2 / 255])
def test_a_dissolve_at_a_wrong_strength_fails_in_either_order(top):
    expected = {"photo-1": 1.0, "photo-2": 0.027}
    assert verdict(FIRST, [moment(picture(1.0, 0), picture(top, 1), expected=expected), OVERLOAD],
                   RENDERER).failures == ("alpha at 10.000",)


def test_the_overloads_three_pictures_over_the_ground_are_judged_by_their_mix():
    """Three planes over the ground: a stack mixing the plan's shares passes in another order of
    the same shares only when every share holds; one plane off fails."""
    expected = {"o2": 0.735, "o3": 0.5, "o4": 0.5}
    shown = moment(picture(0.735, 1), picture(0.5, 2), picture(0.5, 3), step="overload", expected=expected,
                   ground=True)
    assert verdict(FIRST, [shown], RENDERER).passed
    off = moment(picture(0.735, 1), picture(0.6, 2), picture(0.5, 3), step="overload", expected=expected,
                 ground=True)
    assert verdict(FIRST, [off], RENDERER).failures == ("alpha at 10.000",)


def test_a_run_that_could_not_see_a_failure_fails():
    assert verdict(FIRST, [], RENDERER).failures == ("no_moments",)
    assert verdict(FIRST, [GOOD], RENDERER).failures == ("overload_unseen",)
    assert verdict(RegisterSample(0.0, 0x420 | STICKY_GLITCH), [GOOD, OVERLOAD], RENDERER).failures == (
        "glitch_blind",)


@pytest.mark.parametrize(("key", "measured"), sorted(timeline.T3_PLANE_ALPHA.items()))
def test_the_expected_strengths_meet_bench_t3s_measured_plane_alphas(key, measured):
    """The bench's own expectation, held to the plane alphas bench T3 measured as matching the
    Player's mix (16-bit KMS values), not to player/planes.py's rule it checks."""
    beneath, alpha = key
    assert abs(round(timeline.expected_strength(alpha, beneath) * 0xFFFF) - measured) <= 1


def test_the_display_controller_is_the_card_its_driver_names():
    assert driven_by({"/dev/dri/card0": "v3d", "/dev/dri/card1": "vc4"}) == "/dev/dri/card1"
    assert driven_by({"/dev/dri/card0": "vc4", "/dev/dri/card1": "v3d"}) == "/dev/dri/card0"
    debugfs = {"/sys/kernel/debug/dri/0": "v3d dev=1002000000.v3d unique=1002000000.v3d\n",
               "/sys/kernel/debug/dri/1": "vc4 dev=axi:gpu master=pid unique=axi:gpu\n",
               "/sys/kernel/debug/dri/128": "v3d dev=1002000000.v3d unique=1002000000.v3d\n"}
    assert driven_by(debugfs) == "/sys/kernel/debug/dri/1"
    assert driven_by({"/dev/dri/card0": "v3d", "/dev/dri/card1": ""}) is None


class _AnnouncingShell:
    """WestonBackend and its host as BenchController uses them: the Output is announced after
    `after` dispatches; each turn asked is recorded."""

    def __init__(self, after: int) -> None:
        self.after, self.dispatched, self.turns = after, 0, []

    def dispatch(self):
        self.dispatched += 1

    def state(self, output_id):
        if self.dispatched < self.after:
            raise KeyError(output_id)
        return SimpleNamespace(key="key-1", connected=True)

    def output_powered(self, output_id):
        return None if self.dispatched < self.after else "on"

    def turn(self, output_id, turn, *, timeout):
        if self.dispatched < self.after:
            return False
        self.turns.append((output_id, turn))
        return True


def bench_controller(shell: _AnnouncingShell) -> BenchController:
    controller = BenchController.__new__(BenchController)
    controller._output_id, controller._backend, controller._host = "HDMI-A-1", shell, shell
    return controller


def test_a_turn_asked_before_the_output_is_announced_waits_for_it():
    shell = _AnnouncingShell(after=3)
    bench_controller(shell).turn(0, timeout=1.0)
    assert shell.turns == [("HDMI-A-1", 0)] and shell.dispatched >= 3


def test_a_turn_for_an_output_never_announced_fails():
    with pytest.raises(RuntimeError, match="^bench_turn$"):
        bench_controller(_AnnouncingShell(after=10**9)).turn(0, timeout=0.05)


def test_a_new_grant_forgets_the_last_presented_answer():
    """After a re-grant the screen shows the candidate's black until the renderer presents under
    the new grant: a moment of the old answer would be a miss."""
    bench = _Bench.__new__(_Bench)
    bench.tracer = SimpleNamespace(binding=SimpleNamespace(frame_id="f", generation=1, configuration_revision=1))
    bench.controller = SimpleNamespace(grant=lambda *args, **kwargs: None)
    bench.last, bench.handed = object(), True
    bench._grant()
    assert bench.last is None and bench.handed is False
