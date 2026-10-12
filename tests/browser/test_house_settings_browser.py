"""Settings › House (Power and the week, #2-#5; slice S4 K5): the house's timezone, location, units
and clock, read and saved through GET and PUT /v1/operator/house on the production app.

The browser runs in New York and the house is in London: the page offers the browser's zone and
never applies it by itself. Mutation probe (P-browser-default): prefill the Timezone from
`zoneName()` -> the "offers, does not apply" assertion fails.
"""

import os

import pytest
from console_tasks import connect, go
from operator_harness import operator_server
from playwright.sync_api import expect
from support.console_words import POWER, power
from support.house_time import LONDON
from test_power_lane_browser import NOON, seed_house, week

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)


def house(page):
    return page.get_by_role("region", name="House", exact=True)


def save(page):
    with page.expect_response(lambda r: r.url.endswith("/v1/operator/house") and r.request.method == "PUT") as sent:
        page.get_by_role("button", name="Save house settings", exact=True).click()
    return sent.value


@pytest.mark.browser_context_args(timezone_id="America/New_York")
def test_house_offers_the_browsers_zone_lists_zones_by_city_and_saves_zone_and_place(page, registry):
    seed_house(registry)
    assert registry.clock.wall == NOON and LONDON.zone == "Europe/London"
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "schedule")
        expect(week(page)).to_contain_text("The upright line is now: 12:00 on the house clock.")
        go(page, "house")
        zone = house(page).get_by_label("Timezone", exact=True)
        expect(zone).to_have_value("Europe/London")          # the seeded zone, not the browser's
        offer = house(page).get_by_role("button", name="Use this browser's timezone (America/New_York)", exact=True)
        expect(offer).to_be_visible()
        expect(zone).to_have_value("Europe/London")          # offered, not applied

        # A zone is chosen from the browser's list (Central still refuses one it does not know:
        # tests/test_house_settings.py).
        expect(zone.locator("option[value='Europe/London']")).to_have_text("London (Europe/London)")

        offer.click()
        expect(zone).to_have_value("America/New_York")
        house(page).get_by_label("Latitude", exact=True).fill("40.7")
        house(page).get_by_label("Longitude", exact=True).fill("-74")
        answer = save(page)
        assert answer.status == 200
        assert answer.json()["time"] == {"zone": "America/New_York", "place": {"latitude": 40.7, "longitude": -74.0}}
        expect(offer).to_have_count(0)                       # the house's zone is now the browser's

        # The week page's times move with the zone: Central's noon in London is 07:00 in New York.
        go(page, "schedule")
        expect(week(page)).to_contain_text("Times on the house clock (America/New_York).")
        expect(week(page)).to_contain_text("The upright line is now: 07:00 on the house clock.")

        # The 12-hour clock applies to every time the console shows, from the shell's one read.
        go(page, "house")
        house(page).get_by_role("radiogroup", name="Clock", exact=True).get_by_role(
            "radio", name="12-hour", exact=True).click()
        assert save(page).json()["clock"] == "12h"
        go(page, "schedule")
        expect(week(page)).to_contain_text("The upright line is now: 7:00 am on the house clock.")
        expect(week(page).get_by_role("group", name="Wed 14 Oct", exact=True).get_by_role(
            "img", name=power("band", what=POWER["nightOffBand"], when="Wed 14 Oct 11:00 pm – Thu 15 Oct 7:00 am"))
               ).to_have_count(1)

        go(page, "house")
        house(page).get_by_role("button", name="Clear location", exact=True).click()
        assert save(page).json()["time"]["place"] is None
