"""Task-level browser helpers: what an operator does, named by the section it happens in.

Bead 0 of the console passes C+D (docs/operator-console-ux-pass2-flow.md §9). The tests
state *what* they do (go to Scenes, author a Scene, schedule it, show it now, open a frame's
facet); these helpers own *how* today's layout does it. When the layout moves (bead 1b: the
sidebar and hash routes; beads 2-5: step flows), the helpers change and the call sites do
not. Labels and accessible names never change when a control moves (§3 rule 3), so the
helpers use the same names the tests always have.

Tests that are *about* a form (its validation, focus, descriptions or chooser contents) keep
their direct locators; these helpers are for tests that only need the task done.

As of bead 1b each section is a page with its own hash route (#/now, #/scenes, #/schedule,
#/sources, #/wall, #/hardware, #/attention), reached from the sidebar (a drawer under
850 px). As of Console by Domain (E1 U1) the boxes live on Hardware: a list with one row per
Pi in groups, and one Hardware page per Pi (#/hardware/<device-id>), opened with `open_pi`;
the Pi's Software and screens page (#/players/<device-id>) is reached from its Pi header,
opened with `open_player`.
Signing in always lands on #/wall (console DDD §48), so a test that needs another page goes
to it. As of console DDD W1 the Wall's daily face is read-only: drawing, moving and deleting
Frames happen in Edit layout (#/wall/layout), opened with `edit_layout`.

As of bead 2 a Scene is made in the Scene flow (#/scenes/new/<step>): "New Scene" on the
Scenes page, then Kind → Photos → Frames → [Media per frame] → Playback → Review, one step
at a time behind Continue. `author_scene` walks it; `start_scene`, `scene_continue` and
`scene_form` are its parts, for tests about the flow's own steps.

A Source is added in the Source flow (#/sources/new/<step>, console DDD §37): "New
Source", then Library → Tags → Narrow → Name → Review (no Library step when exactly one
connection is known). `add_source` walks it; `start_source`, `source_continue`,
`source_step`, `source_form` and `answer_connection` are its parts.

As of bead 4 a Program is scheduled in the Schedule flow (#/schedule/new/<step>): "Schedule
a Program" on the Schedule page, then Scene → When → Review. `schedule_program` walks it;
`start_schedule`, `schedule_continue` and `schedule_form` are its parts.
"""

from collections.abc import Mapping
from urllib.parse import quote, unquote

from operator_harness import pause_page_clock, sign_in
from playwright.sync_api import expect

# The sections of §6, grouped as its route tables group them, and their sidebar labels
# (each page's <h1> reads the same).
SHOW_SECTIONS = frozenset({"now", "scenes", "schedule", "sources"})
WALL_SECTIONS = frozenset({"wall"})
FLEET_SECTIONS = frozenset({"hardware", "releases"})
SECTIONS = SHOW_SECTIONS | WALL_SECTIONS | FLEET_SECTIONS | {"attention"}
LABELS = {
    "now": "Now", "scenes": "Scenes", "schedule": "Schedule", "sources": "Sources",
    "wall": "Wall", "hardware": "Hardware", "releases": "Releases", "attention": "Needs attention",
    # No sidebar link: one Pi's Software and screens page, reached from its Pi header.
    "players": "Software and screens",
}

# The Inspector's facet keys (Inspector.jsx FACETS) and their tab labels.
FACETS = {"status": "Status", "binding": "Binding", "calibration": "Calibration"}


def go(page, section):
    """Show `section`: one of "now", "scenes", "schedule", "sources", "wall", "hardware",
    "releases" or "attention". Name the section the test is about (a Run test goes to "now", a
    Program test to "schedule").

    Clicks the section's sidebar link by its accessible name; on a narrow screen, where
    the sidebar is a drawer, opens the drawer with "Menu" first. Waits for the page's
    heading. The Wall link returns to the Wall as it was last shown (its frame and facet).
    """
    if section not in SECTIONS:
        raise ValueError(f"unknown console section {section!r}; expected one of {sorted(SECTIONS)}")
    sidebar = page.get_by_role("navigation", name="Sections", exact=True)
    menu = page.get_by_role("button", name="Menu", exact=True)
    expect(sidebar.or_(menu)).to_be_visible()  # the shell is shown (signed in)
    if menu.is_visible():
        menu.click()
        sidebar = page.get_by_role("dialog", name="Menu", exact=True).get_by_role(
            "navigation", name="Sections", exact=True)
    sidebar.get_by_role("link", name=LABELS[section], exact=True).click()
    expect(page.get_by_role("heading", level=1, name=LABELS[section], exact=True)).to_be_visible()


def edit_layout(page):
    """Open the Wall's Edit layout mode from its daily face (the Wall must be shown): click
    **Edit layout** and wait for its "Editing layout" bar. Returns that bar (its Done)."""
    page.get_by_role("button", name="Edit layout", exact=True).click()
    bar = page.get_by_role("group", name="Editing layout", exact=True)
    expect(bar).to_be_visible()
    return bar


def current_hash(page):
    """The console's route as the location says it now (`#/…`)."""
    return page.evaluate("window.location.hash")


def visit(page, route):
    """Follow hash `route` ("#/…") in the loaded console, as a typed URL or a bookmark
    does: a new history entry, and no page load."""
    page.evaluate("(route) => { window.location.hash = route; }", route)


def visible_page(page):
    """The page on screen, to scope negative checks to what is on screen.

    Show pages stay mounted and `hidden` when not current (§3 rule 2), and
    `get_by_text(...).to_have_count(0)` counts hidden DOM too (`get_by_role` does not), so a
    negative text check is scoped through this: `main`'s one page without `hidden`.
    """
    return page.locator("main > section:not([hidden])")


def player_name(registry, player_id, serial=None):
    """A Pi's name on the fleet pages (players.js `playerName`): "Player …<last six of
    its serial>" when its box netbooted with `serial`, else "Player <device id>"."""
    if serial:
        return f"Player …{serial[-6:]}"
    device_id = next(p.device_id for p in registry.inventory().players if p.id == player_id)
    return f"Player {device_id}"


def hardware_list(page):
    """The Hardware list on screen: the region "Pis", its groups' tables inside."""
    return page.get_by_role("region", name="Pis", exact=True)


def open_pi(page, name):
    """Go to Hardware and follow the list's link to the Hardware page of the Pi named `name`
    (`player_name`); waits for the page's heading and returns the page on screen."""
    go(page, "hardware")
    hardware_list(page).get_by_role("link", name=name, exact=True).click()
    expect(page.get_by_role("heading", level=2, name=name, exact=True)).to_be_visible()
    expect(page.get_by_role("heading", level=1, name="Hardware", exact=True)).to_be_visible()
    return visible_page(page)


def open_player(page, name):
    """Open the Software and screens page of the Pi named `name`: its Hardware page
    (`open_pi`), then the Pi header's link; waits for the page and returns it."""
    open_pi(page, name)
    page.get_by_role("link", name="Software and screens", exact=True).click()
    expect(page.get_by_role("heading", level=1, name="Software and screens", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", level=2, name=name, exact=True)).to_be_visible()
    return visible_page(page)


def connect(page, origin, section=None, *, paused_at=None):
    """Sign in (operator_harness.sign_in, owned by pass A), then `go` to `section` if given.

    With `paused_at` (Unix seconds), Playwright's clock is installed and paused there first
    (operator_harness.pause_page_clock), so the console's timers run only when the test
    runs the clock.
    """
    if paused_at is not None:
        pause_page_clock(page, paused_at)
    sign_in(page, origin)
    if section is not None:
        go(page, section)


# The Scene flow's kinds (flow design §7 J4, step 1), by their labels.
LIVE = "Live from a Source"
HAND_PICKED = "Hand-picked per frame"


def scene_form(page):
    """The Scene flow's current step: the form "Author a Scene" in the Scenes region. Every
    step renders it, with the step's fields and its Back and Continue (Review: Save Scene or
    Replace Scene)."""
    return page.get_by_role("region", name="Scenes", exact=True).get_by_role(
        "form", name="Author a Scene", exact=True)


def scene_continue(page, step=None):
    """Press the current step's Continue; with `step` (a stepper label such as "Frames"),
    wait until that step shows."""
    scene_form(page).get_by_role("button", name="Continue", exact=True).click()
    if step is not None:
        expect(page.get_by_role("navigation", name="Steps", exact=True).locator(
            "[aria-current=step]")).to_contain_text(step)


def start_scene(page, *, hand_picked=False):
    """Go to Scenes, press "New Scene", answer Kind (live, or hand-picked per frame) and
    Continue; returns the flow's form, now on the Photos step."""
    go(page, "scenes")
    page.get_by_role("region", name="Scenes", exact=True).get_by_role(
        "button", name="New Scene", exact=True).click()
    form = scene_form(page)
    form.get_by_label(HAND_PICKED if hand_picked else LIVE, exact=True).check()
    scene_continue(page, "Photos")
    return form


def author_scene(page, scene_id, source, frames, *, seconds=None, loop=None, submit=True):
    """Author a Scene from Source `source` on `frames` through the Scene flow and,
    with `submit`, save it.

    `frames` is the target frame ids for a Scene live from the source, or a
    {frame_id: asset_id} mapping for one hand-picked per frame with that item chosen on
    each frame. The flow runs Kind → Photos → Frames → [Media per frame] → Playback →
    Review. `seconds` fills "Seconds per cycle" and `loop` sets "Keep playing until the
    Program ends" (under Playback's Advanced); None keeps each default. `scene_id` is typed
    as the Scene name on Review, so it must be an id the name rule keeps as is (lowercase
    words joined by hyphens), unless the test is about the name rule.

    With `submit`, saves, waits for the saved Scene's card and returns the save's PUT
    response. Without it, returns the flow's form on Review, filled and unsaved.
    """
    picks = frames if isinstance(frames, Mapping) else None
    form = start_scene(page, hand_picked=picks is not None)
    form.get_by_label("Source", exact=True).select_option(source)
    scene_continue(page, "Frames")
    for frame_id in frames:
        form.get_by_label(f"Target frame {frame_id}", exact=True).check()
    if picks is not None:
        scene_continue(page, "Media per frame")
        for frame_id, asset_id in picks.items():
            form.get_by_label(f"Media for frame {frame_id}", exact=True).select_option(asset_id)
    scene_continue(page, "Playback")
    if seconds is not None:
        form.get_by_label("Seconds per cycle", exact=True).fill(str(seconds))
    if loop is not None:
        form.get_by_role("button", name="Advanced", exact=True).click()
        form.get_by_label("Keep playing until the Program ends", exact=True).set_checked(loop)
    scene_continue(page, "Review")
    form.get_by_label("Scene name", exact=True).fill(scene_id)
    if not submit:
        return form
    route = f"/v1/operator/scenes/{scene_id}" + ("/authored" if picks is not None else "")
    with page.expect_response(
        lambda r: r.url.endswith(route) and r.request.method == "PUT"
    ) as info:
        form.get_by_role("button", name="Save Scene", exact=True).click()
    # Listed before anything can use it (proves the refresh after the save landed).
    expect(page.get_by_role("region", name="Scenes", exact=True).get_by_label(
        f"Scene {scene_id}", exact=True)).to_be_visible()
    return info.value


def source_form(page):
    """The Source flow's current step: the form "Configure a Source" in the Sources region.
    Every step renders it, with the step's fields and its Back and Continue (Review: Save
    Source)."""
    return page.get_by_role("region", name="Sources", exact=True).get_by_role(
        "form", name="Configure a Source", exact=True)


def source_continue(page, step=None):
    """Press the Source flow's Continue; with `step` (a stepper label such as "Name"), wait
    until that step shows."""
    source_form(page).get_by_role("button", name="Continue", exact=True).click()
    if step is not None:
        expect(page.get_by_role("navigation", name="Steps", exact=True).locator(
            "[aria-current=step]")).to_contain_text(step)


def source_step(page):
    """The Source flow's current step, by its stepper label ("Library", "Tags", …)."""
    current = page.get_by_role("navigation", name="Steps", exact=True).locator("[aria-current=step]")
    expect(current).to_have_count(1)
    return current.locator("span").last.inner_text().strip()


def start_source(page):
    """Go to Sources and press "New Source"; returns the flow's form, on its first step
    (Library, or Tags when exactly one connection is known)."""
    go(page, "sources")
    page.get_by_role("region", name="Sources", exact=True).get_by_role(
        "button", name="New Source", exact=True).click()
    form = source_form(page)
    expect(form).to_be_visible()
    return form


def answer_connection(form, connection):
    """Answer "Connection name" however the connection rule shows it: a text field on the
    Library step (no Source yet), under the Name step's Advanced (one known connection;
    opened here first) or a chooser on the Library step (several; "Another connection…"
    and "New connection name" for one no Source uses yet)."""
    field = form.get_by_label("Connection name", exact=True)
    if not field.is_visible():
        form.get_by_role("button", name="Advanced", exact=True).click()
    if field.evaluate("(element) => element.tagName") != "SELECT":
        field.fill(connection)
    elif connection in field.evaluate("(element) => [...element.options].map((o) => o.value)"):
        field.select_option(connection)
    else:  # a connection no Source uses yet
        field.select_option(label="Another connection…")
        form.get_by_label("New connection name", exact=True).fill(connection)


def add_source(page, source_ref, connection, *, media_type=None, submit=True):
    """Configure a Source named `source_ref` (`name:rev`) on library `connection` through
    the Source flow: [Library] → Tags (none) → Narrow → Name → Review, then Save Source.

    `media_type` picks "Media type" ("image", "video"); None keeps the flow's default.
    With `submit`, saves, waits (on success) for the saved Source's card and returns the PUT
    response; without it, returns the flow's form on Review, filled and unsaved.
    """
    form = start_source(page)
    if source_step(page) == "Library":
        answer_connection(form, connection)
        source_continue(page, "Tags")
    source_continue(page, "Narrow")
    if media_type is not None:
        form.get_by_label("Media type", exact=True).select_option(media_type)
    source_continue(page, "Name")
    form.get_by_label("Source name", exact=True).fill(source_ref)
    if form.get_by_role("button", name="Advanced", exact=True).count():
        answer_connection(form, connection)
    source_continue(page, "Review")
    if not submit:
        return form
    with page.expect_response(
        lambda r: r.url.endswith("/v1/operator/source-names/" + quote(source_ref, safe=""))
        and r.request.method == "PUT"
    ) as info:
        form.get_by_role("button", name="Save Source", exact=True).click()
    if info.value.ok:
        # The flow ended on the cards, which list it (the refresh after the save landed).
        expect(page.get_by_role("region", name="Sources", exact=True).get_by_role(
            "article", name=source_ref, exact=True)).to_be_visible()
    return info.value


def schedule_form(page):
    """The Schedule flow's current step: the form "Schedule a Program" in the Programs region.
    Every step renders it, with the step's fields and its Back and Continue (Review:
    "Schedule Program", or "Add separate windows" for more than one window)."""
    return page.get_by_role("region", name="Programs", exact=True).get_by_role(
        "form", name="Schedule a Program", exact=True)


def schedule_continue(page, step=None):
    """Press the Schedule flow's Continue; with `step` (a stepper label: "When" or
    "Review"), wait until that step shows."""
    schedule_form(page).get_by_role("button", name="Continue", exact=True).click()
    if step is not None:
        expect(page.get_by_role("navigation", name="Steps", exact=True).locator(
            "[aria-current=step]")).to_contain_text(step)


def start_schedule(page):
    """Go to Schedule and press "Schedule a Program"; returns the flow's form, on the Scene
    step (prefilled with the Scene last saved or picked on a Scene card, if any)."""
    go(page, "schedule")
    page.get_by_role("region", name="Programs", exact=True).get_by_role(
        "button", name="Schedule a Program", exact=True).click()
    return schedule_form(page)


def schedule_program(page, program, scene_id, start, end, priority=None, *, windows=None,
                     submit=True):
    """Schedule Program `program` of Scene `scene_id` over one window, at `priority`.

    Walks the Schedule flow (flow design §7 J6): Scene → When → Review. `scene_id` None
    keeps the Scene step's prefill. `start` and `end` are `datetime-local` values in the
    browser's time zone. `windows` fills "Number of windows" under When's Advanced (the
    separate-windows helper); None keeps the default, one window. The Program name is
    typed on Review; `priority` fills "Priority" under Review's Advanced (None keeps the
    default, 0).

    With `submit`, sends it ("Schedule Program", or "Add separate windows" for more than
    one window), waits (on success) for that Program's card and returns the first Program
    PUT response; without it, returns the flow's
    form on Review, filled and unsent.
    """
    form = start_schedule(page)
    if scene_id is not None:
        form.get_by_label("Scene", exact=True).select_option(scene_id)
    schedule_continue(page, "When")
    form.get_by_label("Window start", exact=True).fill(start)
    form.get_by_label("Window end", exact=True).fill(end)
    if windows is not None:
        form.get_by_role("button", name="Advanced", exact=True).click()
        form.get_by_label("Number of windows", exact=True).fill(str(windows))
    schedule_continue(page, "Review")
    form.get_by_label("Program name", exact=True).fill(program)
    if priority is not None:
        form.get_by_role("button", name="Advanced", exact=True).click()
        form.get_by_label("Priority", exact=True).fill(str(priority))
    if not submit:
        return form
    action = "Schedule Program" if windows in (None, 1) else "Add separate windows"
    with page.expect_response(
        lambda r: "/v1/operator/programs/" in r.url and r.request.method == "PUT"
    ) as info:
        form.get_by_role("button", name=action, exact=True).click()
    if info.value.ok:
        # Listed before anything can use it (the refresh after the save landed): the
        # answer arrives before the console's refresh read does. The card is visible only
        # for a window not yet ended: a past Program renders inside the closed "Past (N)"
        # disclosure (ProgramsRegion.jsx), so this wait suits future windows only.
        program_id = unquote(info.value.url.rsplit("/", 1)[-1])
        expect(page.get_by_role("region", name="Programs", exact=True).get_by_label(
            f"Program {program_id}", exact=True)).to_be_visible()
    return info.value


def show_form(page):
    """The Show-now flow's current step: the form "Activate a Scene" in the Runs region.
    Both steps render it (Scene, then Review, whose forward action is "Activate now")."""
    return page.get_by_role("region", name="Runs", exact=True).get_by_role(
        "form", name="Activate a Scene", exact=True)


def start_show_now(page, scene_id=None):
    """Go to Now, press "Show now" and, with `scene_id`, choose it as "Scene to
    activate"; returns the flow's form, on its Scene step."""
    go(page, "now")
    page.get_by_role("region", name="Runs", exact=True).get_by_role(
        "button", name="Show now", exact=True).click()
    form = show_form(page)
    if scene_id is not None:
        form.get_by_label("Scene to activate", exact=True).select_option(scene_id)
    return form


def show_advanced(form):
    """Open Review's Advanced ("Activation priority", "If it is already running") unless
    it is open already (a priority below its default holds it open)."""
    toggle = form.get_by_role("button", name="Advanced", exact=True)
    if toggle.get_attribute("aria-expanded") != "true":
        toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "true")


def show_now(page, scene_id, priority=None, repeat="Leave it running", *, submit=True):
    """Show Scene `scene_id` now and return the activation's POST response.

    Bead 5: the Show-now flow (#/now/show/<step>): "Show now" on Now, Scene →
    Review, then "Activate now". `priority` fills "Activation priority" under Review's
    Advanced; None keeps its default (the highest priority among the live Runs covering
    the Scene's frames, or 0). `repeat` is the label of the "if it is already running"
    choice; its default ("Leave it running") is left as it is. Central answers
    synchronously with an Admission ({status, reason}); the console mints the activation
    id, so the operator never types one. With `submit`, returns once the console says the
    outcome; without it, returns the form on Review.
    """
    form = start_show_now(page, scene_id)
    form.get_by_role("button", name="Continue", exact=True).click()
    expect(form.get_by_role("button", name="Activate now", exact=True)).to_be_visible()
    if priority is not None or repeat != "Leave it running":
        show_advanced(form)
    if priority is not None:
        form.get_by_label("Activation priority", exact=True).fill(str(priority))
    if repeat != "Leave it running":
        form.get_by_label(repeat, exact=True).check()
    if not submit:
        return form
    with page.expect_response(
        lambda r: r.url.endswith("/v1/operator/activations") and r.request.method == "POST"
    ) as info:
        form.get_by_role("button", name="Activate now", exact=True).click()
    # The outcome is said only after the console's refresh read has landed (useMutate),
    # so whatever the test does next sees the state after the activation.
    expect(page.get_by_role("region", name="Runs", exact=True).get_by_label(
        "Activation outcome", exact=True)).to_be_visible()
    return info.value


def open_frame(page, frame_id, facet):
    """Open frame `frame_id` on the Wall at `facet` ("status", "binding" or
    "calibration", the Inspector.jsx keys); returns its Inspector.

    Follows the frame's route, `#/wall/frames/<id>/<facet>`, as a typed URL would: the
    frame is selected on the plan and its Inspector opens at that facet, without moving
    focus.
    """
    if facet not in FACETS:
        raise ValueError(f"unknown Inspector facet {facet!r}; expected one of {sorted(FACETS)}")
    expect(page.get_by_role("banner")).to_be_visible()  # the shell is shown (signed in)
    visit(page, f"#/wall/frames/{quote(frame_id, safe='')}/{facet}")
    inspector = page.get_by_role("region", name=f"Frame {frame_id} inspector", exact=True)
    expect(inspector).to_be_visible()
    expect(inspector.get_by_role("tab", name=FACETS[facet], exact=True)).to_have_attribute(
        "aria-selected", "true")
    return inspector
