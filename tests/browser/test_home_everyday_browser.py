"""Home's everyday actions in a real browser (decision 0036, slice 3; roadmap 3b-i's acceptance,
moment of truth 8).

Real Chromium against the production create_app (the operator browser harness), on a wall of
Frames each on its own Pi Central hears, playing one Scene of prepared photos, planned by the real
Coordinator (tests/test_skip_never_show.py's `Wall`). Everything the page offers is Central's `can`,
and everything it says after a press is Central's answer in the console's words: nothing here
claims what a screen showed.

- A wall's Skip moves every Frame of it that can move and names each that stayed, and why.
- A Frame's menu offers its everyday actions only as Central's `can` holds them, and Skips that
  Frame alone; the tile then says "requested" from Central's own `asked`.
- Never show in two taps, with Undo (moment 8); Scenes › Never show list with Show again.
- Pause shows its banner with Carry on, and the tile says "requested" until the Pi holds the photo.

Mutation probes (each run and restored): offer the wall's buttons whatever `WallView.can` says ->
the wall test fails; drop the stay clauses from `skipSaid` -> the wall test fails; draw no
takeover or pause banner -> the pause test fails; leave everyday actions out of a tile's menu ->
the menu, Never show and pause tests fail.
"""

import os
import re

import pytest
from console_tasks import go
from operator_harness import operator_server, sign_in
from playwright.sync_api import expect
from test_skip_never_show import Wall

from central.everyday.policy import photo_of

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="browser tests are opt-in (PHOTO_WALL_BROWSER_TESTS=1)",
)

SOON = 15_000                       # ms: Home is read every 5 s; a change shows within a few reads


def _open_home(page, origin):
    sign_in(page, origin)
    expect(page.get_by_role("heading", level=1, name="Home", exact=True)).to_be_visible()


def _tile(page, name):
    return page.get_by_role("main").get_by_role("group", name=name, exact=True)


def _more(page, name):
    return _tile(page, name).get_by_role("button", name=f"More actions for {name}", exact=True)


def _menu(page, name):
    """Open the tile's menu on a wide screen: its entries, in order."""
    _more(page, name).click()
    menu = page.get_by_role("menu")
    expect(menu).to_be_visible()
    return menu


def _menu_items(page, name):
    menu = _menu(page, name)
    items = [item.inner_text().strip() for item in menu.get_by_role("menuitem").all()]
    page.keyboard.press("Escape")
    expect(menu).to_have_count(0)
    return items


def _toast(page):
    """The toast at the bottom of the view: what the last press came to."""
    return page.locator("[role=status].fixed")


def _plans(wall):
    """Each Frame named as its id with a capital ("Hall"), and each Pi fetches its plan, as a
    Player does: Central's newest offer is then what it shows."""
    for frame_id in wall.players:
        wall.registry.rename_frame(frame_id, frame_id.capitalize(), by="console")
        wall.plan(frame_id)


def test_a_walls_skip_moves_the_wall_and_names_who_stayed(page, registry):
    with operator_server(registry.db, registry.clock) as origin:
        wall = Wall(registry, {"hall": "wall", "desk": "wall"}, hand_placed=("desk",))
        _plans(wall)
        _open_home(page, origin)
        skip = page.get_by_role("button", name=re.compile(r"^Skip every Frame on "))
        expect(skip).to_have_count(1)
        expect(page.get_by_role("button", name=re.compile(r"^Previous on every Frame on "))).to_have_count(0)
        skip.click()
        toast = _toast(page)
        expect(toast).to_contain_text("Skipped on Hall.")
        expect(toast).to_contain_text("Desk stayed: its photo is hand-placed.")
        expect(toast.get_by_role("button", name="Undo")).to_have_count(0)
        # Central's `asked`: the hall waits for its Pi to show the new photo; the desk asked nothing.
        expect(_tile(page, "Hall")).to_contain_text("Skip requested", timeout=SOON)
        expect(_tile(page, "Desk")).not_to_contain_text("requested")


def test_a_frames_menu_offers_only_what_central_can_and_skips_that_frame(page, registry):
    with operator_server(registry.db, registry.clock) as origin:
        wall = Wall(registry, {"hall": "wall", "desk": "wall"}, hand_placed=("desk",))
        _plans(wall)
        _open_home(page, origin)
        assert _menu_items(page, "Hall")[:3] == ["Skip this Frame", "Pause for 1 hour", "Never show this photo"]
        desk = _menu_items(page, "Desk")
        assert "Skip this Frame" not in desk and "Never show this photo" in desk
        _menu(page, "Hall").get_by_role("menuitem", name="Skip this Frame").click()
        expect(_toast(page)).to_contain_text("Skipped on Hall.")
        expect(_tile(page, "Hall")).to_contain_text("Skip requested", timeout=SOON)


def test_never_show_in_two_taps_with_undo_and_the_list_with_show_again(page, registry):
    with operator_server(registry.db, registry.clock) as origin:
        wall = Wall(registry, {"hall": "wall"})
        _plans(wall)
        listed = photo_of(wall.showing("hall"))
        _open_home(page, origin)
        # Two taps: the tile's menu, then the item (moment of truth 8).
        _menu(page, "Hall").get_by_role("menuitem", name="Never show this photo").click()
        toast = _toast(page)
        expect(toast).to_contain_text("Won't show again.")
        expect(toast.get_by_role("button", name="Undo")).to_be_visible()
        # Undo takes it off the list.
        toast.get_by_role("button", name="Undo").click()
        expect(_toast(page)).to_contain_text("It shows again.")
        assert listed not in {entry.asset_id for entry in wall.coordinator.never_show_entries().photos}

        # Listed again, it is on Scenes › Never show list, where Show again takes it off.
        _menu(page, "Hall").get_by_role("menuitem", name="Never show this photo").click()
        expect(_toast(page)).to_contain_text("Won't show again.")
        go(page, "never-show")
        list_ = page.get_by_role("main").locator("section:not([hidden])")
        again = list_.get_by_role("button", name=re.compile(r"^Show again: "))
        expect(again).to_have_count(1, timeout=SOON)
        again.click()
        expect(list_.locator("[role=status].fixed")).to_contain_text("It shows again.")
        expect(again).to_have_count(0, timeout=SOON)
        assert listed not in {entry.asset_id for entry in wall.coordinator.never_show_entries().photos}


def test_pause_shows_its_banner_and_carry_on(page, registry):
    with operator_server(registry.db, registry.clock) as origin:
        wall = Wall(registry, {"hall": "wall"})
        _plans(wall)
        _open_home(page, origin)
        _menu(page, "Hall").get_by_role("menuitem", name="Pause for 1 hour").click()
        expect(_toast(page)).to_contain_text(re.compile(r"Hall paused until \d\d:\d\d\."))
        banner = page.get_by_role("region", name="Paused", exact=True)
        expect(banner).to_be_visible(timeout=SOON)
        # Central's `asked`: requested until the Pi reports holding the photo (this Pi sends no Frame
        # report, so its word is Can't tell, never Showing: "Paused until" rides a Showing tile).
        expect(_tile(page, "Hall")).to_contain_text("Pause requested")
        banner.get_by_role("button", name="Carry on").click()
        expect(_toast(page)).to_contain_text("Carrying on with the schedule.")
        expect(banner).to_have_count(0, timeout=SOON)
