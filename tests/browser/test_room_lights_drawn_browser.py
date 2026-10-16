"""Whether a Pi draws with the house's lights as they are now, said where the Pi and its Frames are:
the Frame's Lighting tab and the Pi's Hardware page read GET /v1/operator/house/lights/drawn on the
production app, where Central judges what the Player last reported it drew with against the
room-lights document now.

Mutation probes: the Lighting tab shows no drawn line -> the Frame case fails; the Hardware page
reads another Pi's report -> it says "Not reported" and the Hardware case fails.
"""

import os

import pytest
from console_tasks import connect, open_frame, open_pi, player_name
from operator_harness import operator_server
from playwright.sync_api import expect
from test_console_frame_page_browser import FRAME, _seed
from test_frame_picture_browser import _off_thread

from central.infra.room_light_store import PgRoomLights
from contracts.room_light import RoomLightsDrawn

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

BEHIND = "Behind: it still draws with an older copy of the house's lights."
OLDER = "0" * 64     # a digest no room-lights document Central holds now has


def test_a_pi_central_judges_behind_says_so_on_its_frames_lighting_tab_and_its_hardware_page(page, registry):
    """Acceptance C3: the Player reports it drew with an older document while following; Central
    judges it behind, and both the Frame's Lighting tab and its Pi's Hardware page say so."""
    player_id = _seed(registry)
    lights = PgRoomLights(registry.db)
    _off_thread(lambda: lights.record_drawn(player_id, RoomLightsDrawn(drawn=OLDER, following=True)))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        open_frame(page, FRAME, "lighting")
        expect(page.get_by_role("region", name="Lighting", exact=True)).to_contain_text(BEHIND)

        hardware = open_pi(page, player_name(registry, player_id))
        expect(hardware.get_by_role("region", name="Light", exact=True)).to_contain_text(BEHIND)
        expect(hardware.get_by_role("region", name="Light", exact=True)).to_contain_text(
            "No light Sensor is wired to this Pi.")
