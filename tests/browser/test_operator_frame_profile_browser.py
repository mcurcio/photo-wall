"""Frame profile editing on the Frame page's Hardware tab."""

import os

import pytest
from console_tasks import connect, open_frame, visit
from operator_harness import operator_server
from playwright.sync_api import expect
from test_registry import enroll

from central.displays.model import Readiness
from central.registry import FrameCreate
from contracts.models import FrameProfile

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)


def _frame(registry, frame_id="profile-frame"):
    registry.create_frame(FrameCreate(
        id=frame_id, surface_id="wall", x_mm=100, y_mm=100,
        width_mm=400, height_mm=300,
        profile=FrameProfile(width_px=1920, height_px=1080, diagonal_inches=24),
    ))


def _editor(inspector):
    inspector.get_by_role("region", name="Frame profile", exact=True).get_by_role(
        "button", name="Edit Frame profile", exact=True).click()
    return inspector.get_by_role("form", name="Edit Frame profile", exact=True)


def test_profile_save_uses_generation_zero_and_invalidates_calibration(page, registry):
    _frame(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        inspector = open_frame(page, "profile-frame", "hardware")
        form = _editor(inspector)
        expect(form.get_by_label("Pixel width", exact=True)).to_have_value("1920")
        expect(form.get_by_label("Pixel height", exact=True)).to_have_value("1080")
        form.get_by_label("Pixel width", exact=True).fill("2560")
        form.get_by_label("Pixel height", exact=True).fill("1440")
        with page.expect_request(
            lambda request: request.url.endswith("/v1/operator/frames/profile-frame/profile")
            and request.method == "PUT"
        ) as captured:
            form.get_by_role("button", name="Save profile", exact=True).click()
        payload = captured.value.post_data_json
        assert payload["expected_generation"] == 0
        assert payload["profile"] == {
            "width_px": 2560, "height_px": 1440,
            "diagonal_inches": 24, "video": True,
        }
        expect(inspector.get_by_role("region", name="Frame profile").get_by_role("status")).to_contain_text(
            "Set this Frame's position again")
        expect(inspector.get_by_role("button", name="Edit Frame profile", exact=True)).to_be_focused()
        saved = registry.inventory().frames[0]
        assert saved.profile.width_px == 2560
        assert saved.generation == 1
        assert saved.readiness is Readiness.UNBOUND


def test_saving_identical_profile_does_not_claim_calibration_was_invalidated(page, registry):
    _frame(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        inspector = open_frame(page, "profile-frame", "hardware")
        form = _editor(inspector)
        form.get_by_role("button", name="Save profile", exact=True).click()
        expect(inspector.get_by_role("region", name="Frame profile").get_by_role("status")).to_have_text(
            "Frame profile already matches; its position was not changed.")
        frame = registry.inventory().frames[0]
        assert frame.generation == 0
        assert frame.readiness is Readiness.UNBOUND


def test_switching_frames_discards_the_previous_frame_profile_editor(page, registry):
    _frame(registry, "first-profile")
    registry.create_frame(FrameCreate(
        id="second-profile", surface_id="wall", x_mm=700, y_mm=100,
        width_mm=400, height_mm=300,
        profile=FrameProfile(width_px=1280, height_px=720, diagonal_inches=20),
    ))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        first = open_frame(page, "first-profile", "hardware")
        first_form = _editor(first)
        first_form.get_by_label("Pixel width", exact=True).fill("2560")

        # Change the route to another Frame's page at the same tab.
        visit(page, "#/wall/frames/second-profile/hardware")
        expect(page.get_by_role("heading", level=1, name="Frame second-profile", exact=True)).to_be_visible()
        second = page.locator("main > section:not([hidden])")
        expect(second.get_by_role("form", name="Edit Frame profile", exact=True)).to_have_count(0)
        second_form = _editor(second)
        expect(second_form.get_by_label("Pixel width", exact=True)).to_have_value("1280")
        expect(second_form.get_by_label("Pixel height", exact=True)).to_have_value("720")


def test_profile_orientation_validation_sends_no_write(page, registry):
    _frame(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        form = _editor(open_frame(page, "profile-frame", "hardware"))
        form.get_by_label("Pixel width", exact=True).fill("1080")
        form.get_by_label("Pixel height", exact=True).fill("1920")
        requests = []
        page.on("request", lambda request: requests.append(request)
                if request.url.endswith("/v1/operator/frames/profile-frame/profile") else None)
        form.get_by_role("button", name="Save profile", exact=True).click()
        expect(form.get_by_role("alert")).to_contain_text(
            "Frame profile must match the frame's orientation.")
        assert not requests


def test_bound_refusal_keeps_profile_draft_and_explains_unbind(page, registry):
    _frame(registry)
    # Establish a real Output binding; generation advances from its default zero.
    identity, _, _ = enroll(registry)
    registry.bind("profile-frame", identity["player_id"], "HDMI-A-1", expected_generation=0)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        inspector = open_frame(page, "profile-frame", "hardware")
        form = _editor(inspector)
        form.get_by_label("Pixel width", exact=True).fill("2560")
        form.get_by_label("Pixel height", exact=True).fill("1440")
        form.get_by_role("button", name="Save profile", exact=True).click()
        expect(form.get_by_role("alert")).to_contain_text("Disconnect this Frame from its Pi")
        expect(form.get_by_label("Pixel width", exact=True)).to_have_value("2560")
        expect(form.get_by_label("Pixel height", exact=True)).to_have_value("1440")
        assert registry.inventory().frames[0].profile.width_px == 1920


def test_stale_generation_refusal_keeps_draft_and_guides_reload(page, registry):
    _frame(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        inspector = open_frame(page, "profile-frame", "hardware")
        form = _editor(inspector)
        form.get_by_label("Pixel width", exact=True).fill("2560")
        form.get_by_label("Pixel height", exact=True).fill("1440")

        # A different operator binds after this editor captured generation zero.
        identity, _, _ = enroll(registry)
        registry.bind("profile-frame", identity["player_id"], "HDMI-A-1", expected_generation=0)

        form.get_by_role("button", name="Save profile", exact=True).click()
        expect(form.get_by_role("alert")).to_contain_text("equipment changed")
        expect(form.get_by_role("alert")).to_contain_text("Reload its facts")
        expect(form.get_by_label("Pixel width", exact=True)).to_have_value("2560")
        expect(form.get_by_label("Pixel height", exact=True)).to_have_value("1440")
