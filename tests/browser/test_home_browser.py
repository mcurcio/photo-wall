"""Home in a real browser (decision 0036, slice 2; roadmap 3b-i's acceptance, moment of truth 9).

Real Chromium against the production create_app (the operator browser harness). Every status on
the page is Central's: the house is furnished through the stores each fact's owner records it with
(home_house.py), Central composes the words (central/frame_status/), and the console only words
the codes it is served. Nothing here stubs Home's read, except where a test says it alters one
field of the real answer.

- Each of the six words, and setup, from what Central holds; the house word and its counts.
- A 390-pixel phone: no sideways scroll, the bottom tabs, the tile's menu as a sheet.
- Every fixture fault's problem names its failing part in plain words, with since when and one fix.
- Central's hub unreachable: one house problem replaces the per-Frame cards.
- A tile's menu holds an action only when Central's `can` does.
- Restart app, step by step, against a recording broker (the fake Pi); a restart that fails or is
  refused says so with Retry and never as "requested"; one left for another page is still in
  progress on coming back; an older restart of the Pi is never read as this request's.
- Reboot the Pi from a tile's menu: nothing is asked or claimed before the dialog is confirmed,
  then Requested, Accepted by the Pi, Showing again; a Pi that did not take it reads Not back.
- Central not answering Home's read: the house as last read, Can't tell everywhere, no action offered.
- A fault while the display is off on purpose says when it will matter (finding F1), and each
  wall says what plays now.
- The console lands on Home after sign-in, with a token and through a provider; no Refresh button.

Central's clock is the wall clock here (home_house.py), so no test asserts a time of day, only
that one is said. The page's timers are real: Home's read rides the snapshot's 5 s cadence
(finding F2), so a change made while the page is open is awaited for up to `SOON` ms.

Mutation probes (each run and restored): word `app-absent` without "photo app" in
domain/frame-status.ts -> the six-words and the fault tests fail; draw every action whatever `can`
says in domain/home-board.tsx -> the menu test fails; drop `explains` from Central's hub problem
(central/frame_status/policy.py) -> the hub test fails with a card per Frame. One build with eight
more, then restored: read any restart Central serves as this request's, never draw "Not
restarted", draw a failed Home read as current (page and top bar), drop "Won't show", drop the
now/next strip, offer actions from a stale document, say "requested" whatever the request's
state -> the eight tests of restart failures, leaving and coming back, reboot, the failed read,
intended darkness and now/next fail (8 failed, 9 passed).
"""

import os
import re

import pytest
from console_tasks import current_hash, visit
from home_house import home_server
from operator_harness import (
    assert_fits_width,
    provider_server,
    sign_in,
    sign_in_with_provider,
)
from playwright.sync_api import expect
from support.console_words import POWER, SCENE
from test_registry import ADMIN

from central.home_routes import APP_RESTARTS, HOME
from contracts.node_apps import AppRestartStep
from contracts.node_output import Power, PowerResult

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

WALL = "Wall 1"                     # Central's name for the one wall a new house has
SOON = 15_000                       # ms: Home is read every 5 s; a change shows within a few reads
TIME = r"\d\d:\d\d(:\d\d)?"         # a time of day as the console says it
MENU_LINKS = ["Why is this playing?", "Open Frame"]
# The words Home never says outside Details (0036, Copy).
INTERNAL = re.compile(r"\b(Player|Output|verdict|Host Management)\b")


# --- The page's parts -----------------------------------------------------------------------------

def _main(page):
    return page.get_by_role("main")


def _house_word(page, word):
    """The house word in the top bar, on every page: a link to Home named "House: <word>"."""
    return page.get_by_role("banner").get_by_role("link", name=f"House: {word}", exact=True)


def _tile(page, name):
    """A Frame's tile on its wall: a group named by the Frame."""
    return page.get_by_role("region", name=WALL, exact=True).get_by_role("group", name=name, exact=True)


def _chip(scope):
    """The one status chip in `scope`: its severity is `data-severity`, its text the severity's mark
    and the word."""
    return scope.locator("[data-severity]")


def _says(word):
    """A chip's text for `word`: the word after its severity's mark (a shape, no part of its name)."""
    return re.compile(rf"^\W?{re.escape(word)}$")


def _words(chips):
    """The words of `chips`, in order, without their marks."""
    return [re.sub(r"^\W+", "", chip.inner_text()).strip() for chip in chips.all()]


def _expect_tile(page, name, word, severity, line=None):
    """The tile says `word` on `severity`'s chip and, under it, `line` (a string or a pattern)."""
    tile = _tile(page, name)
    expect(_chip(tile)).to_have_text(_says(word))
    expect(_chip(tile)).to_have_attribute("data-severity", severity)
    if line is not None:
        expect(tile).to_contain_text(line)


def _problems(page):
    return _main(page).get_by_role("group", name="Problems", exact=True)


def _cards(page):
    """Every problem card: a group named by its word, inside Problems."""
    return _problems(page).get_by_role("group")


def _card(page, subject):
    """The one card about `subject` (a Frame's name, or "The house")."""
    card = _cards(page).filter(has_text=re.compile(rf"{re.escape(subject)}: "))
    expect(card).to_have_count(1)
    return card


def _fixes(card):
    """What a card offers to do: its links and buttons but Details, as (role, name)."""
    found = [("link", link.inner_text().strip()) for link in card.get_by_role("link").all()]
    found += [("button", button.inner_text().strip())
              for button in card.get_by_role("button").filter(has_not_text="Details").all()]
    return found


def _more(page, name):
    return _tile(page, name).get_by_role("button", name=f"More actions for {name}", exact=True)


def _menu_items(page, name):
    """Open the tile's menu on a wide screen, read its entries in order, and close it."""
    _more(page, name).click()
    menu = page.get_by_role("menu")
    expect(menu).to_be_visible()
    items = [item.inner_text().strip() for item in menu.get_by_role("menuitem").all()]
    page.keyboard.press("Escape")
    expect(menu).to_have_count(0)
    return items


def _served(page, origin):
    """Home's document as Central serves it now: each Frame's status by its id."""
    response = page.request.get(origin + HOME, headers={"Authorization": "Bearer " + ADMIN})
    assert response.status == 200, response.text()
    return {place["frame_id"]: place["status"] for wall in response.json()["walls"] for place in wall["frames"]}


def _open_home(page, origin):
    sign_in(page, origin)
    expect(page.get_by_role("heading", level=1, name="Home", exact=True)).to_be_visible()


# --- Houses ---------------------------------------------------------------------------------------

def _six_words(house, registry):
    """One house saying every word: Kitchen and Porch showing; Hall's photo app not running; Stairs'
    Pi silent; Study held off and off; Landing's Pi throttled; Attic's Pi rebooting; Spare unbound."""
    house.hub()
    kitchen = house.pi("10000000aaaa0001", ("kitchen", "Kitchen"), ("hall", "Hall"))
    stairs = house.pi("10000000bbbb0002", ("stairs", "Stairs"))
    study = house.pi("10000000cccc0003", ("study", "Study"), ("porch", "Porch"))
    landing = house.pi("10000000dddd0004", ("landing", "Landing"))
    attic = house.pi("10000000eeee0005", ("attic", "Attic"))
    house.frame("spare", "Spare")
    for pi in (kitchen, stairs, study, landing, attic):
        house.shows(pi)
    house.verdict(kitchen, hall="app_absent")
    house.silent(stairs)
    house.all_off()
    house.resume("kitchen", "hall", "stairs", "porch", "landing", "attic", "spare")
    house.display_answers(study, "study", PowerResult.CONFIRMED, Power.OFF)
    house.host_reports(landing, throttled=True)
    house.reboot_asked(attic)
    registry.clock.advance(60)          # past the silence limit for the Pi whose socket closed
    return {"kitchen": kitchen, "stairs": stairs, "study": study, "landing": landing, "attic": attic}


# --- The six words --------------------------------------------------------------------------------

def test_home_says_each_of_the_six_words_from_what_central_holds(page, registry):
    with home_server(registry) as (origin, house):
        pis = _six_words(house, registry)
        _open_home(page, origin)

        _expect_tile(page, "Kitchen", "Showing", "ok")
        _expect_tile(page, "Porch", "Showing", "ok")
        _expect_tile(page, "Hall", "Not showing", "alarm", "The photo app is not running on the Pi.")
        _expect_tile(page, "Stairs", "Can't tell", "unknown",
                     re.compile(rf"Can't reach {pis['stairs'].handle} since {TIME}: it stopped reporting\."))
        _expect_tile(page, "Study", POWER["resting"], "ok", re.compile(rf"Held off until .*{TIME}"))
        _expect_tile(page, "Landing", "Needs a look", "notice", "The Pi is slowed down right now")
        _expect_tile(page, "Attic", "Getting ready", "notice", "Its Pi is restarting")
        # Setup is not status: a `todo` chip with its step, and nothing to ask of it.
        _expect_tile(page, "Spare", "Needs a Pi", "todo", "Choose which Pi and HDMI port feeds this Frame")
        expect(_tile(page, "Spare").get_by_role("button")).to_have_count(0)
        # What Central serves is what the tiles say: nothing is worked out in the browser.
        served = _served(page, origin)
        assert {frame_id: status["word"] for frame_id, status in served.items()} == {
            "kitchen": "showing", "porch": "showing", "hall": "not-showing", "stairs": "cant-tell",
            "study": "resting", "landing": "needs-a-look", "attic": "getting-ready", "spare": None}
        assert served["spare"]["setup"] == "needs-a-pi"

        # The house: the worst Frame word, in the top bar and on Home with its counts.
        expect(_house_word(page, "Not showing")).to_have_attribute("href", "#/home")
        house_group = _main(page).get_by_role("group", name="House", exact=True)
        expect(_chip(house_group)).to_have_text(_says("Not showing"))
        expect(house_group).to_contain_text(
            "1 Frame not showing, 1 can't tell, 1 needs a look, 1 getting ready, 2 showing, 1 resting, 1 to set up")

        # What is wrong, worst first; Showing, Resting, Getting ready and setup raise no card.
        expect(_cards(page)).to_have_count(3)
        assert _words(_chip(_cards(page))) == ["Not showing", "Can't tell", "Needs a look"]
        for quiet in ("Kitchen", "Porch", "Study", "Attic", "Spare"):
            expect(_cards(page).filter(has_text=f"{quiet}: ")).to_have_count(0)
        # The hold a person put over the schedule, with its way back.
        holds = _main(page).get_by_role("region", name="Holds", exact=True)
        expect(holds.get_by_role("heading", name=re.compile(rf"^Off · until .*{TIME}$"))).to_be_visible()
        expect(holds).to_contain_text("Study")
        expect(holds.get_by_role("button", name=POWER["resume"], exact=True)).to_be_visible()
        assert not INTERNAL.search(_main(page).inner_text())


def test_a_frames_tile_opens_its_page_and_resume_schedule_ends_the_hold(page, registry):
    with home_server(registry) as (origin, house):
        house.hub()
        study = house.pi("10000000cccc0003", ("study", "Study"))
        house.shows(study)
        house.all_off()
        house.display_answers(study, "study", PowerResult.CONFIRMED, Power.OFF)
        _open_home(page, origin)
        _expect_tile(page, "Study", POWER["resting"], "ok", "Held off until")
        expect(_house_word(page, "All good")).to_be_visible()      # Resting is not a problem
        expect(_problems(page)).to_have_count(0)

        _main(page).get_by_role("region", name="Holds", exact=True).get_by_role(
            "button", name=POWER["resume"], exact=True).click()
        expect(_main(page).get_by_role("region", name="Holds", exact=True)).to_have_count(0)
        expect(_main(page).get_by_role("group", name="Quick actions", exact=True)).to_contain_text("Schedule resumed.")
        # The display is now due on and the Pi still carries out "off": no longer Resting.
        expect(_chip(_tile(page, "Study"))).not_to_have_text(_says(POWER["resting"]))

        _tile(page, "Study").get_by_role("link", name="Study", exact=True).click()
        expect(page).to_have_url(re.compile(r"#/wall/frames/study/"))
        expect(page.get_by_role("heading", level=1, name="Study", exact=True)).to_be_visible()
        # The house word stays in the top bar on every page, and leads back.
        page.get_by_role("banner").get_by_role("link", name=re.compile(r"^House: ")).click()
        expect(page.get_by_role("heading", level=1, name="Home", exact=True)).to_be_visible()


# --- The phone ------------------------------------------------------------------------------------

def test_a_390_pixel_phone_has_no_sideways_scroll_and_the_bottom_tabs(page, registry):
    page.set_viewport_size({"width": 390, "height": 844})
    with home_server(registry) as (origin, house):
        _six_words(house, registry)
        _open_home(page, origin)
        expect(_chip(_tile(page, "Hall"))).to_have_text(_says("Not showing"))
        expect(_cards(page)).to_have_count(3)
        assert_fits_width(page, "Home at 390 px")

        # The bottom tabs are the phone's navigation: no rail, no Menu button in the header.
        tabs = page.get_by_role("navigation", name="Main", exact=True)
        expect(tabs).to_be_visible()
        expect(tabs.get_by_role("link")).to_have_text(["Home", SCENE["showNow"], "Schedule"])
        expect(tabs.get_by_role("link", name="Home", exact=True)).to_have_attribute("aria-current", "page")
        more = tabs.get_by_role("button", name="More", exact=True)
        expect(more).to_be_visible()
        expect(page.get_by_role("banner").get_by_role("button", name="Menu", exact=True)).to_have_count(0)
        expect(page.get_by_role("navigation", name="Sections", exact=True)).to_be_hidden()
        box = tabs.bounding_box()
        assert round(box["y"] + box["height"]) == 844 and round(box["width"]) == 390, box   # pinned to the bottom
        for tab in (*tabs.get_by_role("link").all(), more):
            assert tab.bounding_box()["height"] >= 44, tab.inner_text()                  # a thumb's target
        # The house word stays in the top bar.
        expect(_house_word(page, "Not showing")).to_be_visible()
        # Show now is a tab here, so it is not a quick action too; All off and All on stay.
        quick = _main(page).get_by_role("group", name="Quick actions", exact=True)
        expect(quick.get_by_role("button")).to_have_text([POWER["allOff"], POWER["allOn"]])

        # A tile's menu opens as a sheet, with what Central's `can` offers and the two links.
        _more(page, "Hall").click()
        sheet = page.get_by_role("dialog", name="More actions for Hall", exact=True)
        expect(sheet).to_be_visible()
        expect(sheet.get_by_role("button", name="Restart app", exact=True)).to_be_visible()
        expect(sheet.get_by_role("button", name="Reboot the Pi", exact=True)).to_be_visible()
        expect(sheet.get_by_role("link")).to_have_text(MENU_LINKS)
        assert_fits_width(page, "Home with a tile's sheet open")
        page.keyboard.press("Escape")
        expect(sheet).to_be_hidden()

        # More opens the rest of the menu; Escape gives focus back to More.
        more.click()
        drawer = page.get_by_role("dialog", name="Menu", exact=True)
        expect(drawer.get_by_role("navigation", name="Sections", exact=True)).to_be_visible()
        assert_fits_width(page, "Home with the menu open")
        page.keyboard.press("Escape")
        expect(drawer).to_be_hidden()
        expect(more).to_be_focused()

        # The tabs go where they say, and stay.
        tabs.get_by_role("link", name="Schedule", exact=True).click()
        expect(page.get_by_role("heading", level=1, name="Schedule", exact=True)).to_be_visible()
        expect(tabs.get_by_role("link", name="Schedule", exact=True)).to_have_attribute("aria-current", "page")
        tabs.get_by_role("link", name=SCENE["showNow"], exact=True).click()
        expect(page).to_have_url(re.compile(r"#/now/show/scene$"))
        tabs.get_by_role("link", name="Home", exact=True).click()
        expect(page.get_by_role("heading", level=1, name="Home", exact=True)).to_be_visible()
        assert_fits_width(page, "Home at 390 px, come back to")


# --- Moment of truth 9: every fault names its part ------------------------------------------------

def test_every_fixture_faults_problem_names_its_part_with_since_and_one_fix(page, registry):
    """One fault per part: the display (it did not answer when turned off), the Pi (silent), the
    network (the Pi's link dropped: Central says Pi, and that its network may be down), the photo
    app (not running), Central (the Pi was never told what to show) and Immich (no library answers)."""
    with home_server(registry) as (origin, house):
        house.hub()
        first = house.pi("10000000aaaa0001", ("kitchen", "Kitchen"), ("hall", "Hall"))
        silent = house.pi("10000000bbbb0002", ("stairs", "Stairs"))
        dropped = house.pi("10000000cccc0003", ("study", "Study"))
        last = house.pi("10000000dddd0004", ("landing", "Landing"), ("porch", "Porch"))
        for pi in (first, silent, dropped, last):
            house.shows(pi)
        house.all_off()
        house.resume("hall", "stairs", "study", "landing", "porch")
        house.display_answers(first, "kitchen", PowerResult.DID_NOT_ANSWER, Power.OFF)
        house.verdict(first, hall="app_absent")
        house.silent(silent)
        house.unlinked(dropped)
        house.verdict(last, landing="screen_not_told")
        house.library_unreachable(house.scene_reading("evenings", "library", "porch"))
        registry.clock.advance(60)
        _open_home(page, origin)

        faults = [
            # subject, word, the sentence naming the part, Details' failing part, the one fix
            ("Hall", "Not showing", "The photo app is not running on the Pi.", "the photo app",
             ("button", "Restart app")),
            ("Landing", "Not showing", "Central has not told the Pi what to show on this display.", "Central",
             ("link", "Open the Frame")),
            ("Stairs", "Can't tell", re.compile(rf"Can't reach {silent.handle} since {TIME}: it stopped reporting\."),
             "the Pi", ("link", "Open the Pi")),
            ("Study", "Can't tell",
             re.compile(rf"Can't reach {dropped.handle} since {TIME}\. It may be off, or its network may be down\."),
             "the Pi", ("link", "Open the Pi")),
            ("Kitchen", "Needs a look", "The display did not answer when it was asked to turn off.", "the display",
             ("link", "Open the Frame")),
            ("The house", "Needs a look", "No photo library (Immich) is answering.", "Immich",
             ("link", "Open Settings › Photo library")),
        ]
        connection = "fixture (Immich)"     # the one library connection, by its name
        for subject, word, sentence, part, fix in faults:
            card = _card(page, subject)
            expect(_chip(card)).to_have_text(_says(word))
            expect(card).to_contain_text(re.compile(rf"{re.escape(subject)}: {re.escape(word)} since {TIME}\."))
            expect(card.get_by_role("paragraph")).to_contain_text(sentence)
            assert _fixes(card) == [fix], (subject, _fixes(card))
            card.get_by_role("button", name="Details", exact=True).click()
            expect(card).to_contain_text(f"Failing part: {part}")
        # One card per Frame fault, worst first: the house's Immich problem stands for Porch's.
        # The connection's own card (Settings › Photo library's, read beside Home's document) says
        # which connection and why, with its own fix.
        expect(_cards(page)).to_have_count(len(faults) + 1)
        own = _card(page, connection)
        expect(own).to_contain_text(re.compile(rf"{re.escape(connection)}: Needs a look since .*{TIME}\."))
        expect(own).to_contain_text("Photo Wall cannot reach Immich at")
        assert _fixes(own) == [("button", "Check again")]
        of_frames = _cards(page).filter(has_not_text="The house: ").filter(has_not_text=connection)
        assert _words(_chip(of_frames)) == [word for subject, word, *_ in faults if subject != "The house"]
        _expect_tile(page, "Porch", "Needs a look", "notice",
                     "The photo library (Immich) this Frame's Scene reads needs a look.")
        # A link fix leads to the page that owns the setting.
        expect(_card(page, "Stairs").get_by_role("link", name="Open the Pi", exact=True)).to_have_attribute(
            "href", f"#/hardware/{silent.device_id}")
        expect(_card(page, "Kitchen").get_by_role("link", name="Open the Frame", exact=True)).to_have_attribute(
            "href", re.compile(r"^#/wall/frames/kitchen/"))
        expect(_card(page, "The house").get_by_role("link")).to_have_attribute("href", "#/photo-library")
        assert_fits_width(page, "Home with six problems")


# --- The house problem ----------------------------------------------------------------------------

def test_an_unreachable_hub_is_one_house_problem_in_place_of_a_card_per_frame(page, registry):
    with home_server(registry) as (origin, house):
        house.hub()
        first = house.pi("10000000aaaa0001", ("kitchen", "Kitchen"), ("hall", "Hall"))
        second = house.pi("10000000bbbb0002", ("stairs", "Stairs"))
        house.shows(first)
        house.shows(second, stairs=False)
        house.verdict(first, hall="app_absent")
        _open_home(page, origin)
        expect(_card(page, "Hall")).to_contain_text("The photo app is not running on the Pi.")
        expect(_card(page, "Stairs")).to_contain_text("The photo app reports it is not presenting this Frame's picture.")
        expect(_house_word(page, "Not showing")).to_be_visible()

        house.hub(reachable=False)      # FleetHub's look no longer reaches Central's hub
        expect(_house_word(page, "Can't tell")).to_be_visible(timeout=SOON)
        card = _card(page, "The house")
        expect(card).to_contain_text(re.compile(rf"The house: Can't tell since {TIME}\."))
        expect(card.get_by_role("paragraph")).to_contain_text(
            "Central can't hear any Pi: the part of Central that talks to the Pis is not answering.")
        expect(card.get_by_role("paragraph")).to_contain_text("Photo Wall keeps trying to reach it.")
        card.get_by_role("button", name="Details", exact=True).click()
        expect(card).to_contain_text("Failing part: Central")
        # One problem, not one per Frame: what each Frame's Pi said before explains nothing now.
        expect(_cards(page)).to_have_count(1)
        for name in ("Kitchen", "Hall", "Stairs"):
            _expect_tile(page, name, "Can't tell", "unknown")
        expect(_main(page).get_by_role("group", name="House", exact=True)).to_contain_text("3 Frames can't tell")
        # Nothing can be asked of a Pi Central can't reach: the menus hold only their links.
        assert _served(page, origin)["hall"]["can"] == []
        assert _menu_items(page, "Hall") == MENU_LINKS

        house.hub()                     # the hub answers again: the Frames' own problems are back
        expect(_card(page, "Hall")).to_be_visible(timeout=SOON)
        expect(_cards(page).filter(has_text="The house: ")).to_have_count(0)


# --- The action menu and Central's `can` ----------------------------------------------------------

def test_a_tiles_menu_holds_an_action_only_when_centrals_can_does(page, registry):
    with home_server(registry) as (origin, house):
        house.hub()
        linked = house.pi("10000000aaaa0001", ("kitchen", "Kitchen"), ("hall", "Hall"))
        dropped = house.pi("10000000bbbb0002", ("stairs", "Stairs"))
        house.frame("spare", "Spare")
        house.shows(linked)
        house.shows(dropped)
        house.unlinked(dropped)
        _open_home(page, origin)
        served = _served(page, origin)
        assert {frame_id: status["can"] for frame_id, status in served.items()} == {
            "kitchen": ["reboot-pi", "restart-app"], "hall": ["reboot-pi", "restart-app"], "stairs": [], "spare": []}
        # Offered: both actions, the lighter first, then the links. Not offered: absent, never disabled.
        assert _menu_items(page, "Kitchen") == ["Restart app", "Reboot the Pi", *MENU_LINKS]
        assert _menu_items(page, "Stairs") == MENU_LINKS
        expect(_tile(page, "Spare").get_by_role("button")).to_have_count(0)     # in setup: no menu at all

        # The real answer with one field altered: Central's `can` for Kitchen without restart-app.
        # The menu follows it entry by entry, and Hall's, on the same Pi, is untouched.
        def without_restart(route):
            response = route.fetch()
            if response.status != 200:
                route.fulfill(response=response)
                return
            document = response.json()
            for wall in document["walls"]:
                for place in wall["frames"]:
                    if place["frame_id"] == "kitchen":
                        place["status"]["can"] = ["reboot-pi"]
            route.fulfill(response=response, json=document)

        page.route("**" + HOME, without_restart)
        page.reload()
        expect(_chip(_tile(page, "Kitchen"))).to_have_text(_says("Showing"))
        assert _menu_items(page, "Kitchen") == ["Reboot the Pi", *MENU_LINKS]
        assert _menu_items(page, "Hall") == ["Restart app", "Reboot the Pi", *MENU_LINKS]


# --- Restart app ----------------------------------------------------------------------------------

def _step(steps, label):
    return steps.get_by_role("listitem").filter(has_text=label)


def test_restart_app_shows_each_step_as_the_pi_reports_it(page, registry):
    """The card's one fix asks Central once; the worker's job asks the Pi's broker once (a recording
    broker here); each step shows when Central records the broker's statement, never before."""
    posts = []
    page.on("request", lambda request: posts.append(request.post_data_json)
            if request.method == "POST" and request.url.endswith("/app-restarts") else None)
    with home_server(registry) as (origin, house):
        house.hub()
        pi = house.pi("10000000aaaa0001", ("kitchen", "Kitchen"), ("hall", "Hall"))
        house.shows(pi, hall=False)
        house.verdict(pi, hall="app_absent")
        _open_home(page, origin)
        card = _card(page, "Hall")
        assert _fixes(card) == [("button", "Restart app")]
        card.get_by_role("button", name="Restart app", exact=True).click()

        steps = _main(page).get_by_role("list", name=f"Restarting the photo app on {pi.handle}", exact=True)
        expect(steps.get_by_role("listitem")).to_have_text(
            [re.compile(rf"Requested\s*{TIME}$"), re.compile(r"App stopped\s*Now$"),
             re.compile(r"App started\s*Not yet$"), re.compile(r"Showing\s*Not yet$")], timeout=SOON)
        expect(_step(steps, "App stopped")).to_have_attribute("aria-current", "step")
        progress = _main(page).get_by_role("group", name=f"Restarting the photo app on {pi.handle}", exact=True)
        expect(progress.get_by_role("status")).to_have_text("Waiting for “App stopped”.")
        expect(progress.get_by_role("button", name="Close", exact=True)).to_have_count(0)   # not ended yet
        # Asked and not yet done: both of the Pi's Frames say so, and nothing more can be asked of it.
        for name in ("Kitchen", "Hall"):
            expect(_tile(page, name)).to_contain_text("Restart requested — waiting for the Pi")
            assert _menu_items(page, name) == MENU_LINKS
        expect(_card(page, "Hall").get_by_role("button", name="Restart app", exact=True)).to_have_count(0)

        request_id = house.run_restart(pi)             # the worker's job: the broker is asked once
        assert posts == [{"request_id": request_id}]
        assert house.broker.asked == [(pi.device_id, request_id)]
        expect(_step(steps, "App stopped")).to_have_text(re.compile(r"Now$"))    # delivered is not stopped

        house.broker_states(pi, request_id, AppRestartStep.APP_STOPPED)
        expect(_step(steps, "App stopped")).to_have_text(re.compile(rf"{TIME}$"), timeout=SOON)
        expect(progress.get_by_role("status")).to_have_text("Waiting for “App started”.")
        house.broker_states(pi, request_id, AppRestartStep.APP_STARTED)
        expect(_step(steps, "App started")).to_have_text(re.compile(rf"{TIME}$"), timeout=SOON)
        expect(progress.get_by_role("status")).to_have_text("Waiting for “Showing”.")
        expect(_step(steps, "Showing")).to_have_text(re.compile(r"Now$"))        # started is not showing

        house.verdict(pi)                              # the new run: nothing dark, both Frames presented
        house.shows(pi)
        expect(_step(steps, "Showing")).to_have_text(re.compile(rf"{TIME}$"), timeout=SOON)
        expect(progress.get_by_role("status")).to_have_text("Finished.")
        _expect_tile(page, "Hall", "Showing", "ok")
        expect(_house_word(page, "All good")).to_be_visible(timeout=SOON)
        expect(_problems(page)).to_have_count(0)
        progress.get_by_role("button", name="Close", exact=True).click()
        expect(steps).to_have_count(0)
        expect(_tile(page, "Hall")).not_to_contain_text("Restart requested")
        assert _menu_items(page, "Hall") == ["Restart app", "Reboot the Pi", *MENU_LINKS]
        # One press was one request to Central and one ask of the Pi; nothing repeated it.
        assert posts == [{"request_id": request_id}]
        assert house.broker.asked == [(pi.device_id, request_id)]


def _restart_progress(page, pi):
    title = f"Restarting the photo app on {pi.handle}"
    return (_main(page).get_by_role("group", name=title, exact=True),
            _main(page).get_by_role("list", name=title, exact=True))


def _broken_hall(house):
    """One Pi feeding Kitchen and Hall, Hall's photo app not running: its card's fix is Restart app."""
    house.hub()
    pi = house.pi("10000000aaaa0001", ("kitchen", "Kitchen"), ("hall", "Hall"))
    house.shows(pi, hall=False)
    house.verdict(pi, hall="app_absent")
    return pi


def test_a_restart_that_fails_says_not_restarted_with_retry_and_stops_saying_requested(page, registry):
    """The broker could not stop the app: the step it stopped at, the plain reason, Retry as a new
    request a person makes; the tiles stop saying "requested" the moment it ended, before Close."""
    posts = []
    page.on("request", lambda request: posts.append(request.post_data_json["request_id"])
            if request.method == "POST" and request.url.endswith("/app-restarts") else None)
    with home_server(registry) as (origin, house):
        pi = _broken_hall(house)
        _open_home(page, origin)
        _card(page, "Hall").get_by_role("button", name="Restart app", exact=True).click()
        progress, steps = _restart_progress(page, pi)
        expect(_tile(page, "Hall")).to_contain_text("Restart requested — waiting for the Pi", timeout=SOON)
        request_id = house.run_restart(pi)
        house.broker_states(pi, request_id, AppRestartStep.NOT_STOPPED)

        failed = progress.get_by_role("alert")
        expect(failed).to_contain_text(f"{pi.handle}: Not restarted.", timeout=SOON)
        expect(failed).to_contain_text("The Pi could not stop the photo app, so nothing was started.")
        expect(failed).to_contain_text("Photo Wall does not try again by itself.")
        expect(_step(steps, "App stopped")).to_have_text(re.compile(r"Did not happen$"))
        expect(progress.get_by_role("status")).to_contain_text("Stopped at “App stopped”")
        expect(page.get_by_role("main")).not_to_contain_text(re.compile(r"not_stopped|node_app_restart"))
        # Ended, and not yet closed: nothing is waiting for the Pi, so no tile says so and the
        # menus offer what Central's `can` holds again.
        for name in ("Kitchen", "Hall"):
            expect(_tile(page, name)).not_to_contain_text("requested")
        assert _menu_items(page, "Hall") == ["Restart app", "Reboot the Pi", *MENU_LINKS]
        assert house.broker.asked == [(pi.device_id, request_id)]      # asked once; nothing retried by itself

        # Another restart of this Pi follows (another tab's): Central's read answers the newest
        # only, and its steps never become this request's, which stays as it ended.
        other = page.request.post(origin + APP_RESTARTS.format(device_id=pi.device_id),
                                  headers={"Authorization": "Bearer " + ADMIN},
                                  data={"request_id": "11111111-2222-4333-8444-555555555555"})
        assert other.status == 200, other.text()
        house.broker_states(pi, house.run_restart(pi), AppRestartStep.APP_STOPPED)
        page.wait_for_timeout(5000)                     # past two reads of the progress
        expect(failed).to_contain_text("The Pi could not stop the photo app, so nothing was started.")
        expect(_step(steps, "App stopped")).to_have_text(re.compile(r"Did not happen$"))
        house.broker_states(pi, "11111111-2222-4333-8444-555555555555", AppRestartStep.NOT_STARTED)   # and it ends

        failed.get_by_role("button", name="Retry", exact=True).click()
        expect(_tile(page, "Hall")).to_contain_text("Restart requested — waiting for the Pi", timeout=SOON)
        assert len(posts) == 2 and posts[0] == request_id and posts[1] != request_id     # a new request, sent once
        expect(progress.get_by_role("alert")).to_have_count(0)


def test_a_restart_central_refuses_stops_at_requested_and_says_why_in_words(page, registry):
    """Another restart of the Pi is already queued (another tab's): Central answers 409. The
    progress says the request was not taken, in words and never by its code, and nothing is "waiting"."""
    with home_server(registry) as (origin, house):
        pi = _broken_hall(house)
        _open_home(page, origin)
        other = page.request.post(origin + APP_RESTARTS.format(device_id=pi.device_id),
                                  headers={"Authorization": "Bearer " + ADMIN},
                                  data={"request_id": "11111111-2222-4333-8444-555555555555"})
        assert other.status == 200, other.text()
        _card(page, "Hall").get_by_role("button", name="Restart app", exact=True).click()
        progress, steps = _restart_progress(page, pi)
        refused = progress.get_by_role("alert")
        expect(refused).to_contain_text(f"{pi.handle}: Not restarted.", timeout=SOON)
        expect(refused).to_contain_text("Another restart of this Pi's photo app is already on its way.")
        expect(refused.get_by_role("button", name="Retry", exact=True)).to_be_visible()
        # The list agrees with the card: it stopped at its first step, and nothing is awaited.
        expect(steps.get_by_role("listitem")).to_have_text(
            [re.compile(r"Requested\s*Did not happen$"), re.compile(r"App stopped\s*Not yet$"),
             re.compile(r"App started\s*Not yet$"), re.compile(r"Showing\s*Not yet$")])
        expect(progress.get_by_role("status")).to_contain_text("Stopped at “Requested”")
        expect(progress).not_to_contain_text("Waiting for")
        expect(page.get_by_role("main")).not_to_contain_text("node_app_restart_outstanding")
        for name in ("Kitchen", "Hall"):
            expect(_tile(page, name)).not_to_contain_text("requested")


def test_a_restart_left_for_another_page_is_still_in_progress_on_coming_back(page, registry):
    """Design language §5, Feedback timing: the person may leave and come back. The steps, the
    tiles' "requested" and the withheld action are all still there; nothing was asked again."""
    posts = []
    page.on("request", lambda request: posts.append(request.post_data_json["request_id"])
            if request.method == "POST" and request.url.endswith("/app-restarts") else None)
    with home_server(registry) as (origin, house):
        pi = _broken_hall(house)
        _open_home(page, origin)
        _card(page, "Hall").get_by_role("button", name="Restart app", exact=True).click()
        progress, steps = _restart_progress(page, pi)
        expect(_tile(page, "Hall")).to_contain_text("Restart requested — waiting for the Pi", timeout=SOON)
        request_id = house.run_restart(pi)

        visit(page, "#/schedule")
        expect(page.get_by_role("heading", level=1, name="Home", exact=True)).to_have_count(0)
        house.broker_states(pi, request_id, AppRestartStep.APP_STOPPED)      # while Home is not shown
        visit(page, "#/home")
        expect(_step(steps, "Requested")).to_have_text(re.compile(rf"{TIME}$"), timeout=SOON)
        expect(_step(steps, "App stopped")).to_have_text(re.compile(rf"{TIME}$"), timeout=SOON)
        expect(progress.get_by_role("status")).to_have_text("Waiting for “App started”.")
        for name in ("Kitchen", "Hall"):
            expect(_tile(page, name)).to_contain_text("Restart requested — waiting for the Pi")
            assert _menu_items(page, name) == MENU_LINKS
        expect(_card(page, "Hall").get_by_role("button", name="Restart app", exact=True)).to_have_count(0)
        assert posts == [request_id] and house.broker.asked == [(pi.device_id, request_id)]


# --- Reboot the Pi --------------------------------------------------------------------------------

def _ask_reboot(page, pi, name):
    """Reboot the Pi from `name`'s menu: the confirm dialog, not yet answered, with the Pi's state
    read (the request is frozen from the Pi's newest node read, as the Pi page's is)."""
    _more(page, name).click()
    node_read = lambda response: (response.request.method == "GET"      # noqa: E731
                                  and response.url.endswith(f"/node/devices/{pi.device_id}"))
    with page.expect_response(node_read, timeout=SOON):
        page.get_by_role("menu").get_by_role("menuitem", name="Reboot the Pi", exact=True).click()
    dialog = page.get_by_role("dialog", name=f"Reboot {pi.handle}?", exact=True)
    expect(dialog).to_be_visible()
    return dialog


def _confirm_reboot(page, dialog):
    """Press the dialog's Reboot the Pi. The request is frozen from the Pi's node read; a press
    before that read has landed says so and sends nothing, so it is pressed again."""
    for _ in range(10):
        dialog.get_by_role("button", name="Reboot the Pi", exact=True).click()
        try:
            expect(dialog).to_have_count(0, timeout=1500)
            return
        except AssertionError:
            expect(dialog.get_by_role("status")).to_contain_text("Still reading this Pi's state from Central.")
    raise AssertionError("the reboot dialog never took the request")


def _reboot_progress(page, pi):
    title = f"Rebooting {pi.handle}"
    return (_main(page).get_by_role("group", name=title, exact=True),
            _main(page).get_by_role("list", name=title, exact=True))


def test_reboot_from_a_tile_asks_nothing_before_it_is_confirmed_then_shows_each_step(page, registry):
    writes = []
    page.on("request", lambda request: writes.append(request.url.rsplit("/", 1)[-1])
            if request.method == "POST" and "/node/devices/" in request.url else None)
    with home_server(registry) as (origin, house):
        house.hub()
        pi = house.pi("10000000aaaa0001", ("kitchen", "Kitchen"), ("hall", "Hall"))
        house.host_starts(pi)
        house.shows(pi)
        _open_home(page, origin)
        dialog = _ask_reboot(page, pi, "Kitchen")
        # It cannot be taken back and blacks both Frames: the dialog names them. Nothing is sent
        # and nothing is claimed while it is open.
        for name in ("Kitchen", "Hall"):
            expect(dialog.get_by_role("paragraph")).to_contain_text(name)
        expect(dialog).to_contain_text("It cannot be taken back.")
        expect(page.get_by_text("requested", exact=False)).to_have_count(0)     # the page is inert behind the dialog
        assert writes == []
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        expect(dialog).to_have_count(0)
        assert writes == [] and _menu_items(page, "Kitchen") == ["Restart app", "Reboot the Pi", *MENU_LINKS]

        _confirm_reboot(page, _ask_reboot(page, pi, "Kitchen"))
        progress, steps = _reboot_progress(page, pi)
        expect(steps.get_by_role("listitem")).to_have_text(
            [re.compile(rf"Requested\s*{TIME}$"), re.compile(r"Accepted by the Pi\s*Now$"),
             re.compile(r"Showing again\s*Not yet$")], timeout=SOON)
        assert writes == ["reboots"]
        for name in ("Kitchen", "Hall"):
            expect(_tile(page, name)).to_contain_text("Reboot requested — waiting for the Pi", timeout=SOON)
            _expect_tile(page, name, "Getting ready", "notice", "Its Pi is restarting")
            assert _menu_items(page, name) == MENU_LINKS

        house.run_reboot(pi)                               # the worker's job: the Pi's host accepts
        expect(_step(steps, "Accepted by the Pi")).to_have_text(re.compile(rf"{TIME}$"), timeout=SOON)
        expect(_step(steps, "Showing again")).to_have_text(re.compile(r"Now$"))     # accepted is not back
        expect(progress.get_by_role("status")).to_have_text("Waiting for “Showing again”.")
        expect(progress.get_by_role("button", name="Close", exact=True)).to_have_count(0)

        house.host_starts(pi)                              # the Pi is back: its host started, its app reports
        house.shows(pi)
        expect(_step(steps, "Showing again")).to_have_text(re.compile(rf"{TIME}$"), timeout=SOON)
        expect(progress.get_by_role("status")).to_have_text("Finished.")
        for name in ("Kitchen", "Hall"):
            _expect_tile(page, name, "Showing", "ok")
            expect(_tile(page, name)).not_to_contain_text("requested")
        progress.get_by_role("button", name="Close", exact=True).click()
        expect(steps).to_have_count(0)
        assert writes == ["reboots"]                       # one press, one request; nothing repeated


def test_a_reboot_the_pi_did_not_take_reads_not_back_with_a_link_to_the_pi(page, registry):
    with home_server(registry) as (origin, house):
        house.hub()
        pi = house.pi("10000000aaaa0001", ("kitchen", "Kitchen"))
        house.host_starts(pi)
        house.shows(pi)
        _open_home(page, origin)
        _confirm_reboot(page, _ask_reboot(page, pi, "Kitchen"))
        progress, steps = _reboot_progress(page, pi)
        expect(_step(steps, "Requested")).to_have_text(re.compile(rf"{TIME}$"), timeout=SOON)
        house.run_reboot(pi, delivered=False)              # no link, or the host did not answer
        failed = progress.get_by_role("alert")
        expect(failed).to_contain_text(f"{pi.handle}: Not back.", timeout=SOON)
        expect(failed).to_contain_text("The reboot stopped at “Accepted by the Pi”")
        expect(failed.get_by_role("link", name="Open the Pi", exact=True)).to_have_attribute(
            "href", f"#/hardware/{pi.device_id}")
        expect(_step(steps, "Showing again")).to_have_text(re.compile(r"Not yet$"))
        expect(_tile(page, "Kitchen")).not_to_contain_text("requested")
        _expect_tile(page, "Kitchen", "Showing", "ok")     # nothing happened on the Pi


# --- Central not answering ------------------------------------------------------------------------

def test_a_failed_home_read_shows_the_house_as_last_read_and_offers_no_action(page, registry):
    """Central stops answering Home's read while the rest of the console still reads: the page
    says so once, every tile says Can't tell since the last read, the house word is Can't tell on
    every page, and what could be asked of a Pi before is not offered from a document that is not
    current. When Central answers again the page is Central's again."""
    with home_server(registry) as (origin, house):
        _broken_hall(house)
        _open_home(page, origin)
        expect(_house_word(page, "Not showing")).to_be_visible()
        assert _menu_items(page, "Kitchen") == ["Restart app", "Reboot the Pi", *MENU_LINKS]

        page.route("**" + HOME, lambda route: route.abort())
        expect(_house_word(page, "Can't tell")).to_be_visible(timeout=SOON)
        banner = _cards(page).filter(has_text="Central did not answer")
        expect(banner).to_have_count(1)
        expect(banner).to_contain_text(re.compile(rf"This is the house as last read at {TIME}\."))
        expect(banner).to_contain_text("Photo Wall keeps trying.")
        for name in ("Kitchen", "Hall"):
            expect(_tile(page, name)).to_contain_text(re.compile(rf"Can't tell since {TIME}"))
            assert _menu_items(page, name) == MENU_LINKS
        # Hall's problem is still listed as last read, without its fix: Restart app is asked of
        # what Central says now.
        expect(_card(page, "Hall")).to_contain_text("The photo app is not running on the Pi.")
        expect(_card(page, "Hall").get_by_role("button", name="Restart app", exact=True)).to_have_count(0)
        visit(page, "#/schedule")
        expect(_house_word(page, "Can't tell")).to_be_visible()

        page.unroute("**" + HOME)
        visit(page, "#/home")
        expect(_house_word(page, "Not showing")).to_be_visible(timeout=SOON)
        expect(_cards(page).filter(has_text="Central did not answer")).to_have_count(0)
        expect(_tile(page, "Hall")).not_to_contain_text("Can't tell since")
        assert _fixes(_card(page, "Hall")) == [("button", "Restart app")]


# --- Intended darkness, and now and next ----------------------------------------------------------

def test_a_fault_while_the_display_is_off_on_purpose_says_when_it_will_matter(page, registry):
    """Finding F1: Hall's photo app is not running while every display is held off and off. That
    is not "Not showing" (nothing should be showing): Needs a look, the fault and its fix at once,
    and that it won't show when its display is next due on. Kitchen, on the same Pi, is Resting."""
    with home_server(registry) as (origin, house):
        house.hub()
        pi = house.pi("10000000aaaa0001", ("kitchen", "Kitchen"), ("hall", "Hall"))
        house.shows(pi, hall=False)
        house.all_off()
        for frame_id in ("kitchen", "hall"):
            house.display_answers(pi, frame_id, PowerResult.CONFIRMED, Power.OFF)
        house.verdict(pi, hall="app_absent")
        _open_home(page, origin)
        wont_show = re.compile(r"Won't show (at \d\d:\d\d|when its display is next due on)")
        _expect_tile(page, "Hall", "Needs a look", "notice", wont_show)
        _expect_tile(page, "Kitchen", POWER["resting"], "ok", "Held off")
        card = _card(page, "Hall")
        expect(card).to_contain_text(re.compile(rf"Hall: Needs a look since {TIME}\."))
        expect(card.get_by_role("paragraph")).to_contain_text(
            re.compile(r"The photo app is not running on the Pi\. Won't show (at \d\d:\d\d|when its display is next due on)\."))
        assert _fixes(card) == [("button", "Restart app")]
        expect(_house_word(page, "Needs a look")).to_be_visible()


def test_each_wall_says_what_plays_now_and_a_frame_nothing_plays_on_is_resting(page, registry):
    """The now/next strip under a wall, from Central's read of the Runtime; and a bound Frame no
    Scene targets is dark on purpose: Resting, "Nothing to show right now", never a photo app fault."""
    with home_server(registry) as (origin, house):
        house.hub()
        pi = house.pi("10000000aaaa0001", ("kitchen", "Kitchen"))
        idle = house.pi("10000000bbbb0002")                 # a Pi with no Frame yet
        house.frame("porch", "Porch")
        registry.bind("porch", idle.player["player_id"], "HDMI-A-1", expected_generation=0, by="console")
        house.position("porch")
        idle = house.feeds(idle, "porch")
        house.shows(pi)
        house.shows(idle, porch=False)                      # black, and nothing is to play on it
        _open_home(page, origin)
        wall = page.get_by_role("region", name=WALL, exact=True)
        strip = wall.locator("dl")
        expect(strip.locator("dt")).to_have_text(["Now"])
        expect(strip.locator("dd")).to_have_text(["Show now: daily-aa0001"])
        _expect_tile(page, "Kitchen", "Showing", "ok", "Show now: daily-aa0001")
        _expect_tile(page, "Porch", POWER["resting"], "ok", "Nothing to show right now")
        expect(_house_word(page, "All good")).to_be_visible()
        expect(_problems(page)).to_have_count(0)
        # Once a Scene is to play there, the same black Frame is the photo app's fault.
        house.plays("porch-scene", "porch")
        _expect_tile(page, "Porch", "Not showing", "alarm")
        expect(_card(page, "Porch")).to_contain_text("The photo app reports it is not presenting this Frame's picture.",
                                                     timeout=SOON)


# --- Landing --------------------------------------------------------------------------------------

def _expect_landed_on_home(page):
    expect(page.get_by_role("heading", level=1, name="Home", exact=True)).to_be_visible()
    assert current_hash(page) == "#/home"
    expect(page).to_have_title("Home · Photo Wall")
    nav = page.get_by_role("navigation", name="Sections", exact=True)
    expect(nav.get_by_role("link", name="Home", exact=True)).to_have_attribute("aria-current", "page")
    # The page updates by itself (0036 F2; console design §9): there is no Refresh button on it.
    expect(page.get_by_role("button", name="Refresh", exact=True)).to_have_count(0)


def test_the_console_lands_on_home_after_sign_in_with_a_token(page, registry):
    unchanged = []
    page.on("response", lambda response: unchanged.append(response.request.headers.get("if-none-match"))
            if response.url.endswith(HOME) and response.status == 304 else None)
    with home_server(registry) as (origin, house):
        sign_in(page, origin)
        _expect_landed_on_home(page)
        # A house with no Frame yet: nothing to be wrong, and where to start.
        expect(_house_word(page, "All good")).to_be_visible()
        expect(_main(page)).to_contain_text("No Frame yet. Each Frame you add shows here with its status.")
        expect(_main(page).get_by_role("link", name="Add your first Frame on the Wall", exact=True)).to_have_attribute(
            "href", "#/wall")
        # An unchanged house costs no body: the read names the document it holds, and Central says 304.
        with page.expect_response(lambda response: response.url.endswith(HOME) and response.status == 304, timeout=SOON):
            pass
        assert unchanged and all(tag for tag in unchanged), unchanged

        # A Frame added while Home is open shows by itself, with its status.
        house.hub()
        house.shows(house.pi("10000000aaaa0001", ("kitchen", "Kitchen")))
        expect(_chip(_tile(page, "Kitchen"))).to_have_text(_says("Showing"), timeout=SOON)
        expect(_main(page).get_by_role("group", name="House", exact=True)).to_contain_text("1 Frame showing")
        # A route the console does not know falls back to Home; a reload stays on it.
        visit(page, "#/nowhere")
        expect(page).to_have_url(re.compile(r"#/home$"))
        page.reload()
        _expect_landed_on_home(page)
        expect(_chip(_tile(page, "Kitchen"))).to_have_text(_says("Showing"))


def test_the_console_lands_on_home_after_sign_in_through_a_provider(page, registry):
    with provider_server(registry.db, registry.clock) as (origin, _fake):
        sign_in_with_provider(page, origin)
        _expect_landed_on_home(page)
        expect(_house_word(page, "All good")).to_be_visible()
        expect(page.get_by_role("dialog", name="Sign in to Photo Wall", exact=True)).to_be_hidden()
