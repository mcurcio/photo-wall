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
- The rest of 0036's acceptance list: a wall's Skip moves every movable Frame at one instant and
  leaves the other wall alone; Previous from the wall, then once more from a Frame's menu; a late
  Skip's card with Retry; a last showing named in Never show's toast; no Pause on a video; the
  everyday actions on a 390-pixel phone.

Mutation probes (each run and restored): offer the wall's buttons whatever `WallView.can` says ->
the wall test fails; drop the stay clauses from `skipSaid` -> the wall test fails; draw no
takeover or pause banner -> the pause test fails; leave everyday actions out of a tile's menu ->
the menu, Never show and pause tests fail. Two builds, each run and restored byte for byte
(2026-11-25): (A) a wall's Skip sends `next` whatever is pressed, word `not-answering` as
resting, draw no late card, offer no Undo after Show again, end a takeover through `finish` ->
the wall's Skip, Previous, late Skip, Show again and both Back to normal tests fail (6 failed);
(B) name a Frame once per turn in a last showing, offer every action whatever `can` says, send
`next` for a Frame's Previous -> the last showing, menu, video and Previous tests fail (4 failed).
"""

import os
import re

import pytest
from console_tasks import go, open_frame
from operator_harness import assert_fits_width, operator_server, sign_in
from playwright.sync_api import expect
from support.photo_pools import photo
from test_registry import ADMIN
from test_skip_never_show import PHOTOS, Wall, covers, shown_twice

from central.everyday.model import ACK_LATE_SECONDS, SkipKind
from central.everyday.policy import photo_of
from central.home_routes import HOME

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


# --- The rest of the acceptance list (0036, Slice 3's acceptance › Browser) -----------------------

def _wall_named(page, origin, surface_id):
    """The name Home gives the wall `surface_id`, from Central's own document."""
    walls = page.request.get(origin + HOME, headers={"Authorization": f"Bearer {ADMIN}"}).json()["walls"]
    return next(each["name"] for each in walls if each["surface_id"] == surface_id)


def _post(page, part):
    """Await the console's POST to an address ending in `part`."""
    return page.expect_response(lambda r: r.url.endswith(part) and r.request.method == "POST")


def _movers(wall):
    """The Frames the next planning pass gives a cover (what an everyday action adds)."""
    wall.tick()
    return {frame_id for frame_id in wall.players if covers(wall, frame_id)}


def test_a_walls_skip_changes_every_movable_frame_and_leaves_other_walls(page, registry):
    """S1 ("if possible"): the wall's Skip moves every Frame of the wall it is pressed on that can
    move, at one instant, and names each that stayed and why (a hand-placed photo, a Pi Central
    does not hear); the other wall gets no cover. Mutation probe: word one stay reason as another
    in `STAY_WORDS`."""
    with operator_server(registry.db, registry.clock) as origin:
        wall = Wall(registry, {"hall": "wall", "den": "wall", "desk": "wall", "attic": "wall", "porch": "garden"},
                    hand_placed=("desk",), silent=("attic",))
        _plans(wall)
        _open_home(page, origin)
        expect(page.get_by_role("button", name=re.compile(r"^Skip every Frame on "))).to_have_count(2)
        name = _wall_named(page, origin, "wall")
        with _post(page, "/walls/wall/skips") as info:
            page.get_by_role("button", name=f"Skip every Frame on {name}", exact=True).click()
        assert info.value.status == 200, info.value.text()
        answer = info.value.json()
        toast = _toast(page)
        expect(toast).to_contain_text("Skipped on 2 Frames.")
        expect(toast).to_contain_text("Desk stayed: its photo is hand-placed.")
        expect(toast).to_contain_text("Attic stayed: its Pi isn't answering.")
        assert _movers(wall) == {"hall", "den"}
        (hall,), (den,) = covers(wall, "hall"), covers(wall, "den")
        assert hall.start == den.start == answer["at"]                 # one instant
        for frame_id in ("Hall", "Den"):
            expect(_tile(page, frame_id)).to_contain_text("Skip requested", timeout=SOON)
        for frame_id in ("Desk", "Porch"):
            expect(_tile(page, frame_id)).not_to_contain_text("requested")


def test_a_frames_menu_skips_and_steps_back_that_frame(page, registry):
    """S2: the wall's Previous steps back every Frame with an earlier photo (the desk, with none,
    stays and says why); the hall's own menu then steps it back one more. Each press is one cover
    of the photo before, the second's photo further back and drawn later; the desk gets none.
    Mutation probe: send `next` for a Frame's Previous."""
    with operator_server(registry.db, registry.clock) as origin:
        wall = Wall(registry, {"hall": "wall", "desk": "wall"}, cycle=30)
        _plans(wall)
        zeroth = photo_of(wall.showing("hall"))
        wall.presented("hall", wall.showing("hall"))
        wall.tick(31)
        first, second = shown_twice(wall, "hall")
        assert len({zeroth, first, second}) == 3
        _open_home(page, origin)
        name = _wall_named(page, origin, "wall")
        with _post(page, "/walls/wall/skips") as info:
            page.get_by_role("button", name=f"Previous on every Frame on {name}", exact=True).click()
        assert info.value.request.post_data_json["kind"] == SkipKind.PREVIOUS.value, info.value.request.post_data
        expect(_toast(page)).to_contain_text("Stepped back on Hall. Desk stayed: it has nothing earlier to show.")
        expect(_tile(page, "Hall")).to_contain_text("Previous requested", timeout=SOON)
        wall.tick()
        wall.tick(1)
        # Again, from the hall's own menu: one more back.
        with _post(page, "/frames/hall/skips") as info:
            _menu(page, "Hall").get_by_role("menuitem", name="Previous on this Frame").click()
        assert info.value.status == 200, info.value.text()
        expect(_toast(page)).to_contain_text("Stepped back on Hall.")
        assert _movers(wall) == {"hall"}
        back = {photo_of(layer): layer.start for layer in covers(wall, "hall")}
        assert set(back) == {first, zeroth} and back[zeroth] > back[first]


def test_a_late_skip_shows_a_card_with_retry(page, registry):
    """A Skip its Pi has not shown by its deadline (`asked` late, Central's clock past it while the
    page reads Home) is a card in Problems saying what did not happen, with Retry: a new request,
    answered as any Skip. Mutation probe: leave late actions out of Problems."""
    with operator_server(registry.db, registry.clock) as origin:
        wall = Wall(registry, {"hall": "wall"})
        _plans(wall)
        _open_home(page, origin)
        with _post(page, "/frames/hall/skips") as info:
            _menu(page, "Hall").get_by_role("menuitem", name="Skip this Frame").click()
        first = info.value.json()
        expect(_tile(page, "Hall")).to_contain_text("Skip requested", timeout=SOON)
        wall.tick(first["at"] + 31 - registry.clock.utc())       # past its deadline, never presented
        problems = page.get_by_role("main").get_by_role("group", name="Problems", exact=True)
        card = problems.get_by_text("Hall didn't change. Its Pi hasn't reported the new photo.")
        expect(card).to_be_visible(timeout=SOON)
        with _post(page, "/frames/hall/skips") as again:
            problems.get_by_role("button", name="Retry", exact=True).click()
        assert again.value.status == 200, again.value.text()
        sent = again.value.request.post_data_json
        assert sent["request_id"] != info.value.request.post_data_json["request_id"]
        expect(_toast(page)).to_contain_text("Skipped on Hall.")
        expect(card).to_have_count(0, timeout=SOON)


def test_a_late_pause_offers_no_retry_central_would_refuse(page, registry):
    """A Pause its Pi has not shown by its deadline: its card says so, and offers no Retry, as
    Central's `can` holds no Pause while the pause stands (a Retry would be refused `paused`); it
    links to the Frame instead. Mutation probe: draw Retry whatever `can` holds."""
    with operator_server(registry.db, registry.clock) as origin:
        wall = Wall(registry, {"hall": "wall"})
        _plans(wall)
        _open_home(page, origin)
        with _post(page, "/pauses") as info:
            _menu(page, "Hall").get_by_role("menuitem", name="Pause for 1 hour").click()
        assert info.value.status == 200, info.value.text()
        wall.tick(ACK_LATE_SECONDS + 5)                     # past its deadline, never presented
        problems = page.get_by_role("main").get_by_role("group", name="Problems", exact=True)
        card = problems.get_by_text("Hall isn't confirmed paused. Its Pi hasn't reported holding its photo.")
        expect(card).to_be_visible(timeout=SOON)
        expect(problems.get_by_role("button", name="Retry", exact=True)).to_have_count(0)
        expect(problems.get_by_role("link", name="Open the Frame", exact=True)).to_be_visible()


def test_a_frame_page_offers_no_action_from_a_home_read_that_failed(page, registry):
    """Decision 0036: an action is never offered from a document that is not current. While
    Central answers Home, the Frame page's header offers the Frame's everyday actions; once Home's
    read fails, the page keeps the last read and offers none, as Home does. Mutation probe: read
    the stale document's `can` (the menu stays)."""
    with operator_server(registry.db, registry.clock) as origin:
        wall = Wall(registry, {"hall": "wall"})
        _plans(wall)
        _open_home(page, origin)
        open_frame(page, "hall", "overview")
        more = page.get_by_role("button", name="More actions for Hall", exact=True)
        expect(more).to_be_visible(timeout=SOON)
        page.route("**" + HOME, lambda route: route.fulfill(status=503, body="{}"))
        expect(more).to_have_count(0, timeout=SOON)


def test_never_show_names_a_last_showing(page, registry):
    """S5: a photo already given to another Frame's later turn (downloaded there) shows there that
    one last time, and the toast says so naming each such Frame once, with Undo. Mutation probes:
    drop the last showings from `neverShowSaid`; name a Frame once per turn."""
    with operator_server(registry.db, registry.clock) as origin:
        wall = Wall(registry, {"hall": "wall", "desk": "wall"}, cycle=30, photos=PHOTOS[:4])
        _plans(wall)
        listed = photo_of(wall.showing("hall"))
        now = registry.clock.utc()
        assert any(photo_of(layer) == listed and layer.start > now for layer in wall.layers("desk")), \
            "the fixture needs the hall's photo due later on the desk"
        _open_home(page, origin)
        with _post(page, "/never-show") as info:
            _menu(page, "Hall").get_by_role("menuitem", name="Never show this photo").click()
        assert info.value.status == 200, info.value.text()
        last = info.value.json()["last_showings"]
        assert "desk" in {each["frame_id"] for each in last}, last
        toast = _toast(page)
        # Each Frame named once, though Central may name several of its turns (here the hall's too).
        names = ", ".join(dict.fromkeys(each["frame_id"].capitalize() for each in last))
        expect(toast).to_contain_text(f"Won't show again. It shows once more on {names}, where it's already downloaded.")
        expect(toast.get_by_role("button", name="Undo")).to_be_visible()
        assert listed in {entry.asset_id for entry in wall.coordinator.never_show_entries().photos}


def test_an_action_not_offered_is_absent(page, registry):
    """S8: no Pause on a video: Central's `can` leaves it out, and so does the tile's menu and the
    Frame page's More; with nothing earlier shown, no Previous either. Mutation probe: list every
    everyday action in a menu whatever `can` holds."""
    film = photo("film", taken_at=900, width=2400, height=3600, kind="video")
    with operator_server(registry.db, registry.clock) as origin:
        wall = Wall(registry, {"hall": "wall"}, photos=(film,))
        _plans(wall)
        _open_home(page, origin)
        items = _menu_items(page, "Hall")
        assert "Pause for 1 hour" not in items and "Previous on this Frame" not in items, items
        expect(page.get_by_role("button", name=re.compile(r"^Previous on every Frame on "))).to_have_count(0)


def test_the_everyday_actions_on_a_390_pixel_phone(page, registry):
    """A phone: the wall's Skip is a thumb's target and the page never scrolls sideways; a tile's
    menu is a sheet holding the Frame's everyday actions; Skip and Pause from it are answered in
    the toast, and the pause's banner carries Carry on, all within the width."""
    page.set_viewport_size({"width": 390, "height": 844})
    with operator_server(registry.db, registry.clock) as origin:
        wall = Wall(registry, {"hall": "wall", "desk": "wall"})
        _plans(wall)
        _open_home(page, origin)
        name = _wall_named(page, origin, "wall")
        skip = page.get_by_role("button", name=f"Skip every Frame on {name}", exact=True)
        expect(skip).to_be_visible()
        assert skip.bounding_box()["height"] >= 44
        assert_fits_width(page, "Home's everyday actions at 390 px")
        _more(page, "Hall").click()
        sheet = page.get_by_role("dialog", name="More actions for Hall", exact=True)
        expect(sheet).to_be_visible()
        for action in ("Skip this Frame", "Pause for 1 hour", "Never show this photo"):
            expect(sheet.get_by_role("button", name=action, exact=True)).to_be_visible()
        assert_fits_width(page, "A tile's sheet with its everyday actions")
        sheet.get_by_role("button", name="Skip this Frame", exact=True).click()
        expect(_toast(page)).to_contain_text("Skipped on Hall.")
        assert_fits_width(page, "Home with Skip's toast")
        assert _movers(wall) == {"hall"}
        _more(page, "Desk").click()
        page.get_by_role("dialog", name="More actions for Desk", exact=True).get_by_role(
            "button", name="Pause for 1 hour", exact=True).click()
        expect(_toast(page)).to_contain_text(re.compile(r"Desk paused until \d\d:\d\d\."))
        banner = page.get_by_role("region", name="Paused", exact=True)
        expect(banner).to_be_visible(timeout=SOON)
        assert_fits_width(page, "Home with a pause's banner")
        banner.get_by_role("button", name="Carry on").click()
        expect(_toast(page)).to_contain_text("Carrying on with the schedule.")
        expect(banner).to_have_count(0, timeout=SOON)

