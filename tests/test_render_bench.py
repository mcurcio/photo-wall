"""The tracer bench's pure parts (scripts/render_bench): the plan and what each moment must show,
the kernel's state dump, the glitch bits and the verdict."""
from __future__ import annotations

from pathlib import Path

import pytest

from player.geometry import encode_srgb
from scripts.render_bench import timeline
from scripts.render_bench.evidence import Moment, verdict
from scripts.render_bench.kms_state import KmsPlane, kms_planes
from scripts.render_bench.registers import STICKY_GLITCH, RegisterSample, glitched

FIXTURE = Path(__file__).parent / "fixtures" / "render_bench" / "kms-state.txt"
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


def test_the_overload_is_cut_to_the_budget(plan):
    overload = plan.steps[-1]
    strengths = timeline.strengths_at(plan, overload.start + 1, SCREEN)
    assert list(strengths) == ["overload-2", "overload-3", "overload-4"]


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


def test_text_that_is_not_a_state_dump_is_refused():
    with pytest.raises(ValueError, match="^kms_state$"):
        kms_planes("hello\n", {})


def test_only_a_newly_set_glitch_bit_counts():
    clean, set_before = RegisterSample(0.0, 0x420), RegisterSample(0.0, 0x420 | 0x200)
    assert not glitched(clean, RegisterSample(1.0, 0x4FF))             # other bits move freely
    assert glitched(clean, RegisterSample(1.0, 0x420 | 0x800))
    assert not glitched(set_before, RegisterSample(1.0, 0x420 | 0x200))
    assert glitched(set_before, RegisterSample(1.0, 0x420 | STICKY_GLITCH))


PRIMARY = KmsPlane(83, 94, 686, (3840, 2160), (0, 0), 0, 1, 0xFFFF, "Pre-multiplied")
FIRST = RegisterSample(0.0, 0x420)


def picture(alpha: float, zpos: int = 1, blend: str = "Pre-multiplied") -> KmsPlane:
    return KmsPlane(100 + zpos, 94, 700 + zpos, (3840, 2160), (0, 0), zpos, 1, round(alpha * 0xFFFF), blend)


def moment(*planes: KmsPlane, expected=None, step: str = "dissolve", status: int = 0x420, drawing: bool = True,
           gpu: float = 0.5) -> Moment:
    expected = {"out": 1.0, "in": 0.3} if expected is None else expected
    return Moment(10.0, step, expected, (PRIMARY, *planes), RegisterSample(1.0, status), drawing, gpu)


GOOD = moment(picture(1.0, 1), picture(0.3, 2))
OVERLOAD = moment(picture(1.0, 1), step="overload", expected={"top": 1.0})


def test_a_run_that_shows_every_rule_passes():
    assert verdict(FIRST, [GOOD, OVERLOAD, moment(picture(1.0), expected={"one": 1.0}, drawing=False, gpu=0.01)
                           ]).passed


@pytest.mark.parametrize(("bad", "check"), [
    (moment(picture(1.0, 1), picture(0.3 + 2 / 255, 2)), "alpha"),
    (moment(picture(1.0, 1)), "plane"),
    (moment(picture(1.0, 1), picture(0.3, 2, "Coverage")), "blend"),
    (moment(picture(1.0, 1), picture(0.3, 2), status=0x420 | 0x800), "glitch"),
    (moment(picture(1.0, 1), picture(0.3, 2), drawing=False, gpu=0.2), "gpu"),
])
def test_each_broken_rule_fails_the_run_and_names_its_first_moment(bad, check):
    result = verdict(FIRST, [GOOD, bad, OVERLOAD])
    assert not result.passed and result.failures == (f"{check} at 10.000",)


def test_one_level_off_still_passes():
    assert verdict(FIRST, [moment(picture(1.0, 1), picture(0.3 + 1 / 255, 2)), OVERLOAD]).passed


def test_a_run_that_could_not_see_a_failure_fails():
    assert verdict(FIRST, []).failures == ("no_moments",)
    assert verdict(FIRST, [GOOD]).failures == ("overload_unseen",)
    assert verdict(RegisterSample(0.0, 0x420 | STICKY_GLITCH), [GOOD, OVERLOAD]).failures == ("glitch_blind",)
