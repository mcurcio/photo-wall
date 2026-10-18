"""The Player's screens' pure rules (player/plane_screen.py; render redesign, run ledger
.claude/runs/render-planes/s1-page.md, bead B3): the strength's wire value, and a present past the
controller's planes refused before the frame client library is asked anything; the trial overlay's
corners, the overlay sent with a present, the Position trial and the Output's mode as built (run ledger
.claude/runs/render-planes/s3-page.md, bead B3), over a fake library. The screens themselves are proved under a real Weston by tests/native_screen_harness.py."""
from __future__ import annotations

import json
import math
from uuid import uuid4

import pytest

import player.plane_screen as plane_screen
from contracts.frame_fit import Placement
from contracts.frame_lighting import FrameLighting, Paper
from contracts.models import Calibration, CoverFit, FrameProfile, Layer, OutputBinding, RectangleFit
from contracts.node_calibration import (
    MAX_TRIAL_BYTES,
    TrialCandidate,
    candidate_hash,
    canonical,
    encode_trial,
)
from contracts.node_display import Surface
from contracts.node_protocol import NodeProcessIdentity, OutputKey
from player.plane_screen import FrameGrant
from player.planes import Composition, Plane, Screen
from player.rendering import LocalLayer, OutputComposition


@pytest.mark.parametrize(("strength", "wire"), [
    (1.0, 0xFFFFFFFF),
    (0.5, 0x80000000),              # round(2147483647.5): half to even
    (1 / 255, 0x01010101),
])
def test_multiplier_is_the_strength_on_the_alpha_modifiers_scale(strength, wire):
    assert plane_screen.multiplier(strength) == wire


@pytest.mark.parametrize("strength", [0.0, -0.1, 1.0000001, math.nan, math.inf, -math.inf])
def test_multiplier_refuses_a_strength_outside_zero_to_one(strength):
    with pytest.raises(ValueError, match="plane_strength"):
        plane_screen.multiplier(strength)


class _Library:
    """A frame client library that records every call; connecting succeeds."""

    def __init__(self, _path: str) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        library = self

        class Function:
            argtypes = restype = None

            def __call__(self, *_args):
                library.calls.append(name)
                return 1

        function = Function()
        setattr(self, name, function)
        return function


def _plane(index: int) -> Plane:
    layer = Layer(assignment_id=f"black-{index}", run_id="run", output_id="HDMI-A-1", frame_id="frame",
                  binding_generation=1, start=0, end=10**9, media_origin=0, presentation="black")
    return Plane("frame", LocalLayer(layer, None, 0.0, 1.0), Placement(0, 0, 64, 48), 1.0)


def test_present_refuses_past_the_screens_planes_before_asking_the_library(monkeypatch):
    monkeypatch.setattr(plane_screen.ctypes, "CDLL", _Library)
    screens = plane_screen.PlaneScreens("libfake.so")
    library = screens._lib
    before = list(library.calls)
    composition = Composition(Screen("HDMI-A-1", (640, 480), 0),
                              tuple(_plane(index) for index in range(plane_screen.SCREEN_PLANES + 1)))
    grant = FrameGrant(uuid4(), 1, 1, "frame", True)
    with pytest.raises(ValueError, match="present_planes"):
        screens.present(composition, {}, grant, "tag", None)
    assert library.calls == before


def test_present_refuses_a_ground_not_laid_out_for_the_whole_screen_before_asking_the_library(monkeypatch):
    """The screen's ground is the whole screen's picture as it is laid out (turned with the
    screen): any other size, or a picture the screens never imported, is refused untouched."""
    monkeypatch.setattr(plane_screen.ctypes, "CDLL", _Library)
    screens = plane_screen.PlaneScreens("libfake.so")
    library = screens._lib
    screens._pictures.add(7)
    grant = FrameGrant(uuid4(), 1, 1, "frame", True)
    turned = Composition(Screen("HDMI-A-1", (480, 640), 90), ())
    before = list(library.calls)
    for ground in (plane_screen.ScreenPicture(7, (480, 640)), plane_screen.ScreenPicture(8, (640, 480))):
        assert screens.present(turned, {}, grant, "tag", None, ground) == "picture_refused"
    assert library.calls == before


# --- render redesign S3, bead B3: the trial and the overlay on the screens' own connection, the
# Output's mode as built ------------------------------------------------------------------------

def test_overlay_corners_are_the_drawn_placement_as_fractions_of_the_screen():
    corners = plane_screen.overlay_corners(Placement(0, 1313, 2160, 1215), (2160, 3840))
    wanted = ((0, 1313 / 3840), (1, 1313 / 3840), (1, 2528 / 3840), (0, 2528 / 3840))
    assert corners == wanted
    assert corners[0][1] == pytest.approx(0.3419270833) and corners[2][1] == pytest.approx(0.6583333333)


@pytest.mark.parametrize(("placement", "screen", "error"), [
    (Placement(0, 0, 10, 10), (0, 100), "overlay_screen"),
    (Placement(0, 0, 10, 10), (100, 0), "overlay_screen"),
    (Placement(-1, 0, 10, 10), (100, 100), "overlay_outside"),
    (Placement(0, -1, 10, 10), (100, 100), "overlay_outside"),
    (Placement(91, 0, 10, 10), (100, 100), "overlay_outside"),
    (Placement(0, 91, 10, 10), (100, 100), "overlay_outside"),
    (Placement(0, 0, 0, 10), (100, 100), "overlay_outside"),
])
def test_overlay_corners_refuse_a_placement_not_wholly_on_a_real_screen(placement, screen, error):
    with pytest.raises(ValueError, match=error):
        plane_screen.overlay_corners(placement, screen)


class _Scripted:
    """A frame client library whose calls are recorded with their arguments; each function answers
    `answers[name]` (default 1), or calls it with the call's arguments when it must write into them."""

    def __init__(self, _path: str) -> None:
        self.calls: list[tuple] = []
        self.answers: dict = {}

    def __getattr__(self, name: str):
        library = self

        class Function:
            argtypes = restype = None

            def __call__(self, *args):
                library.calls.append((name, *args))
                answer = library.answers.get(name, 1)
                return answer(*args) if callable(answer) else answer

        function = Function()
        setattr(self, name, function)
        return function

    def named(self, name: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == name]


def _screens(monkeypatch) -> tuple[plane_screen.PlaneScreens, _Scripted]:
    monkeypatch.setattr(plane_screen.ctypes, "CDLL", _Scripted)
    screens = plane_screen.PlaneScreens("libfake.so")
    return screens, screens._lib


def _output(width: int, height: int, transform: int):
    def answer(_client, _name, w, h, t):
        w.value, h.value, t.value = width, height, transform
        return 1
    return answer


def test_mode_is_the_output_as_built_while_screen_is_it_as_it_hangs(monkeypatch):
    screens, library = _screens(monkeypatch)
    library.answers["pw_frame_client_output"] = _output(3840, 2160, 1)
    assert screens.mode("HDMI-A-1") == (3840, 2160)
    assert screens.screen("HDMI-A-1").size == (2160, 3840)
    library.answers["pw_frame_client_output"] = 0
    assert screens.mode("HDMI-A-1") is None
    library.answers["pw_frame_client_output"] = -1
    with pytest.raises(RuntimeError, match="display_frame_protocol_lost"):
        screens.mode("HDMI-A-1")


TRIAL_TAG = "trial-" + "b" * 64


def _present_once(monkeypatch, overlay=None, overlay_answer=0):
    """A present of one plane on a configured 640 x 480 screen, `overlay` sent with it."""
    screens, library = _screens(monkeypatch)
    library.answers["pw_frame_client_output"] = _output(640, 480, 0)
    library.answers["pw_screen_create"] = 0x5C
    library.answers["pw_screen_overlay"] = overlay_answer
    library.answers["pw_screen_present"] = 7
    screens._pictures.add(9)
    plane = _plane(0)
    grant = FrameGrant(uuid4(), 1, 1, "frame", True)
    composition = Composition(Screen("HDMI-A-1", (640, 480), 0), (plane,))
    pictures = {"black-0": plane_screen.ScreenPicture(9, (64, 48))}

    def present():
        return screens.present(composition, pictures, grant, TRIAL_TAG, None, overlay=overlay)

    return present, library, grant


def test_a_present_with_an_overlay_sends_it_first_under_the_presents_grant_and_tag(monkeypatch):
    corners = plane_screen.overlay_corners(Placement(0, 1313, 2160, 1215), (2160, 3840))
    present, library, grant = _present_once(monkeypatch, overlay=corners)
    assert present() == 7
    names = [call[0] for call in library.calls]
    assert names.index("pw_screen_overlay") < names.index("pw_screen_present")
    (overlay,) = library.named("pw_screen_overlay")
    assert overlay[1:] == (0x5C, str(grant.grant_id).encode(), TRIAL_TAG.encode(),
                           json.dumps(corners, separators=(",", ":"), allow_nan=False).encode())
    assert json.loads(overlay[4]) == [[0, 1313 / 3840], [1, 1313 / 3840], [1, 2528 / 3840], [0, 2528 / 3840]]
    assert library.named("pw_screen_present")[0][6:8] == (str(grant.grant_id).encode(), TRIAL_TAG.encode())


def test_a_present_without_an_overlay_sends_none(monkeypatch):
    present, library, _grant = _present_once(monkeypatch)
    assert present() == 7
    assert library.named("pw_screen_overlay") == [] and len(library.named("pw_screen_present")) == 1


def test_a_refused_overlay_raises_and_commits_nothing(monkeypatch):
    corners = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    present, library, _grant = _present_once(monkeypatch, overlay=corners, overlay_answer=-1)
    with pytest.raises(RuntimeError, match="display_trial_overlay_failed"):
        present()
    assert library.named("pw_screen_present") == []


def test_an_overlay_on_a_screen_not_configured_yet_answers_output_gone_unsent(monkeypatch):
    """As a present without one would: the screen shows nothing before its first configure."""
    corners = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    present, library, _grant = _present_once(monkeypatch, overlay=corners)
    library.answers["pw_screen_size"] = 0
    assert present() == "output_gone"
    assert library.named("pw_screen_overlay") == [] and library.named("pw_screen_present") == []


LIGHTING = FrameLighting(mat_depth_mm=0, lights=(), paper=Paper(whiteness=0.9, ink_density=2))
BINDING = OutputBinding(output_id="HDMI-A-1", frame_id="frame", generation=3, configuration_revision=2,
                        profile=FrameProfile(width_px=1920, height_px=1080, diagonal_inches=27), lighting=LIGHTING)
BASELINE = Surface(OutputKey(uuid4(), uuid4(), "HDMI-A-1", 1, 1), NodeProcessIdentity(100, 5, uuid4()),
                   1, BINDING.generation, BINDING.configuration_revision, "frame")


def _envelope(fit) -> str:
    trial_id = uuid4()
    calibration = canonical(Calibration(fit=fit).model_dump(mode="json"))
    return encode_trial(TrialCandidate(
        trial_id=trial_id, generation=1, sequence=1, baseline=BASELINE, calibration_json=calibration,
        candidate_sha256=candidate_hash(trial_id, 1, 1, BASELINE, calibration, LIGHTING),
        expires_boottime_ms=5000, hard_expires_boottime_ms=9000, lighting=LIGHTING))


def _trial_answer(raw: str | None, result: int):
    def answer(_client, _output_id, _grant, buffer, capacity):
        assert capacity > MAX_TRIAL_BYTES
        if raw is not None:
            buffer.value = raw.encode()
        return result
    return answer


@pytest.mark.parametrize("fit", [RectangleFit(centre_x=0.4, centre_y=0.5, width=0.5), CoverFit(rotation=7)],
                         ids=["rectangle", "cover"])
def test_trial_hands_the_librarys_envelope_to_the_outputs_trial_state(monkeypatch, fit):
    screens, library = _screens(monkeypatch)
    monkeypatch.setattr(plane_screen, "_boottime_ms", lambda: 1000)
    raw = _envelope(fit)
    library.answers["pw_frame_client_trial"] = _trial_answer(raw, 1)
    grant = FrameGrant(uuid4(), 3, 2, "frame", True)
    applied = screens.trial(OutputComposition(BINDING), grant)
    assert applied.calibration.fit == fit and applied.lighting == LIGHTING
    assert applied.candidate.frame_tag == "trial-" + json.loads(raw)["candidate_sha256"]
    (call,) = library.named("pw_frame_client_trial")
    assert call[2:4] == (b"HDMI-A-1", str(grant.grant_id).encode())


def test_trial_keeps_one_trial_state_per_output_on_the_boottime_clock(monkeypatch):
    """TrialState's fences hold across reads: past the trial's expiry the same envelope read again
    is not drawn (a delayed envelope cannot revive it)."""
    screens, library = _screens(monkeypatch)
    now = [1000]
    monkeypatch.setattr(plane_screen, "_boottime_ms", lambda: now[0])
    library.answers["pw_frame_client_trial"] = _trial_answer(_envelope(RectangleFit()), 1)
    grant = FrameGrant(uuid4(), 3, 2, "frame", True)
    assert screens.trial(OutputComposition(BINDING), grant) is not None
    now[0] = 6000
    assert screens.trial(OutputComposition(BINDING), grant) is None


def test_trial_is_none_when_none_is_drawn_and_raises_on_transport_failure(monkeypatch):
    screens, library = _screens(monkeypatch)
    monkeypatch.setattr(plane_screen, "_boottime_ms", lambda: 1000)
    grant = FrameGrant(uuid4(), 3, 2, "frame", True)
    library.answers["pw_frame_client_trial"] = _trial_answer(None, 0)
    assert screens.trial(OutputComposition(BINDING), grant) is None
    library.answers["pw_frame_client_trial"] = _trial_answer(None, -1)
    with pytest.raises(RuntimeError, match="display_trial_protocol_lost"):
        screens.trial(OutputComposition(BINDING), grant)
