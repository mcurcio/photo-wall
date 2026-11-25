"""Show now, Run cards and the Why disclosures (flow design §7 J7 and "See what is showing
and why"; bead 5), in a real browser against the production app.

The Show-now flow is #/now/show/<step>: Scene → Review → "Activate now". Its priority
defaults to showState.js `coveringPriority` (the highest priority among the live root Runs
covering the Scene's frames, 0 when none does), and its activation key lives in the draft:
kept across steps, sections, a Wall visit and the sign-in overlay, reused after an unknown
outcome, and new whenever the form changes (slice 3B §11).
"""

import os
import re

import pytest
from console_tasks import (
    connect,
    current_hash,
    go,
    show_advanced,
    show_form,
    show_now,
    start_show_now,
    visible_page,
    visit,
)
from operator_harness import (
    SNAPSHOT,
    RequestGate,
    answer_first,
    drive_poll,
    operator_server,
    report_presenting,
    submit_sign_in,
)
from photo_seed import connected, filled, library_of, saved_source
from playwright.sync_api import expect
from support.console_words import scene_words
from test_operator_showrunner_browser import (
    INVALID_FRAME,
    SCENE_ID,
    SOURCE,
    VALID_FRAME,
    _runs_of,
    _runtime,
    _scene,
    _seed,
    _seed_source,
)
from test_skip_never_show import Wall

from central.everyday.model import END_LEAD
from central.everyday.policy import showing_at
from central.photo_library.model import SourceQuery
from central.photo_library.ports import CheckFailed
from central.runtime import Contribution, Scene, Transition

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

ACTIVATIONS = "**/v1/operator/activations"
UNKNOWN = ("Outcome unknown. Try again; it will not start twice. "
           "Changing the form makes this a new activation.")


def _runs(page):
    return page.get_by_role("region", name="Runs", exact=True)


def _outcome(page):
    return _runs(page).get_by_label("Activation outcome", exact=True)


def _keys(page):
    """Every activation id the console sends, in order."""
    sent = []
    page.on("request", lambda request: sent.append(request.post_data_json["activation_id"])
            if request.url.endswith("/v1/operator/activations")
            and request.method == "POST" else None)
    return sent



def _covered(registry):
    """A Run of "evening" at priority 5 on VALID_FRAME; SCENE_ID on the same frame and
    "elsewhere" on INVALID_FRAME, which no Run covers."""
    runtime = _runtime(registry)
    runtime.command("set_scene", _scene("evening"), by="console")
    runtime.command("set_scene", _scene(SCENE_ID), by="console")
    runtime.command("set_scene", _scene("elsewhere", frame=INVALID_FRAME), by="console")
    runtime.command("activate", "evening", "evening-act", registry.clock.utc(), priority=5, by="console")
    return runtime


def _garden(registry):
    """A saved Photo source "garden" (roadmap 4) and a Scene on VALID_FRAME playing it."""
    library = library_of(registry)
    ref = connected(library)
    garden = saved_source(registry, library, "garden", SourceQuery(connection_ref=ref))
    return library, garden


def _on(scene_id, source_ref, frame=VALID_FRAME):
    return Scene(scene_id=scene_id, loop=True, contributions=(
        Contribution(target=f"frame:{frame}", source_refs=(source_ref,)),))


def test_show_now_names_the_scenes_sources_in_one_line_with_no_check_now(page, registry):
    """K4, after the designer review (follow-ups D1): Show now names the chosen Scene's Sources
    with their state in one line ("Photos from garden (Not checked yet)"), with no boilerplate
    and no Check now (the Sources page checks a Source), and none of the words Run, Runs or
    "Scene to activate" while the quick action shows.

    Mutation probe: render the old Saved Sources section -> the boilerplate assertion fails."""
    _seed(registry)
    _library, garden = _garden(registry)
    _runtime(registry).command("set_scene", _on(SCENE_ID, garden), by="console")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "now")
        form = start_show_now(page, SCENE_ID)
        line = form.get_by_role("definition").filter(has_text="garden")
        expect(line).to_have_text("garden (Not checked yet)")
        expect(form).not_to_contain_text("A Source keeps playing what it has found")
        expect(form.get_by_role("button", name=re.compile("Check"))).to_have_count(0)
        expect(form.get_by_label("Scene", exact=True)).to_have_value(SCENE_ID)
        shown = page.get_by_role("region", name="Runs", exact=True).inner_text()   # visible text only
        assert not re.search(r"\bRuns\b|Scene to activate", shown), shown
        form.get_by_role("button", name="Continue", exact=True).click()
        expect(form.get_by_role("definition").filter(has_text="garden")).to_have_text("garden (Not checked yet)")
        expect(form.get_by_role("button", name="Activate now", exact=True)).to_be_enabled()


def test_show_now_shows_a_failed_check_as_its_problem_naming_immich(page, registry):
    """A Source whose check failed reads as the error template under the one line, naming who
    failed (Immich) with its one fix; the line says only "Check failed" (owner, 2026-10-09).
    Mutation probe: drop SourceCheckProblem from ShowNowFlow.jsx's SceneSources."""
    _seed(registry)
    library, garden = _garden(registry)
    with registry.db.transaction() as conn:
        key = conn.execute("SELECT pool_key FROM media_sources WHERE source_ref=%s",
                           (garden,)).fetchone()["pool_key"]
    filled(library, key, [], CheckFailed(problem="connection"))
    _runtime(registry).command("set_scene", _on(SCENE_ID, garden), by="console")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "now")
        form = start_show_now(page, SCENE_ID)
        expect(form.get_by_role("definition").filter(has_text="garden")).to_have_text("garden (Check failed)")
        problem = form.get_by_role("group", name="Check failed")
        expect(problem).to_contain_text("garden: Check failed.")
        expect(problem).to_contain_text("Immich could not be read: see Photo library. Photos keep playing.")
        expect(problem.get_by_role("link", name="Open Photo library")).to_be_visible()


def test_show_now_handles_historical_and_authored_scenes(page, registry):
    _seed(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", Scene(scene_id="historical", contributions=(
        Contribution(target=f"frame:{VALID_FRAME}", source_refs=("gone:1",)),)), by="console")
    runtime.command("set_scene", Scene(scene_id="fixed", contributions=(
        Contribution(target=f"frame:{VALID_FRAME}", asset_refs=("fixed-item",)),)), by="console")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "now")
        form = start_show_now(page, "historical")
        sources = form.get_by_label("Photos from", exact=True)
        expect(sources).to_contain_text("gone (its state is not served)")
        expect(sources).not_to_contain_text("gone:1")
        form.get_by_label("Scene", exact=True).select_option("fixed")
        expect(form.get_by_label("Photos from", exact=True)).to_contain_text("Hand-picked photos")


def test_the_default_priority_is_the_covering_runs_so_the_new_run_shows_on_top(page, registry):
    """§7 J7: the default is the highest priority among the live Runs covering the Scene's
    frames — max, not max + 1: at equal priority the later admission wins
    (central/runtime.py `Intent.precedence`, `_view`, `_admit`), so the new Run is on top.
    A Scene on frames no Run covers defaults to 0. Mutation probes: `max + 1`, or 0."""
    _seed(registry)
    queue = _seed_source(registry)
    _covered(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        form = show_now(page, SCENE_ID, submit=False)
        review = form.get_by_role("term").filter(has_text="Priority")
        expect(review).to_have_count(1)
        expect(form).to_contain_text(
            "5 (the default: the highest Run on its frames has priority 5; "
            "at equal priority the newer Run shows on top)")
        # Advanced is closed at the default; it holds the same value.
        toggle = form.get_by_role("button", name="Advanced", exact=True)
        expect(toggle).to_have_attribute("aria-expanded", "false")
        show_advanced(form)
        expect(form.get_by_label("Activation priority", exact=True)).to_have_value("5")
        expect(form.get_by_text(re.compile("stays underneath"))).to_have_count(0)

        with page.expect_response(lambda r: r.url.endswith("/v1/operator/activations")) as info:
            form.get_by_role("button", name="Activate now", exact=True).click()
        assert info.value.request.post_data_json["priority"] == 5
        expect(_outcome(page)).to_have_text(f"Started: Central admitted a Run of {SCENE_ID}.")
        assert current_hash(page) == "#/now"

        # Central's Runs put the new Run on top of the frame.
        why = _runs(page).get_by_role("group", name="Why", exact=True)
        why.get_by_role("button", name=f"Why? {VALID_FRAME}", exact=True).click()
        expect(why).to_contain_text(
            f"Central's Runs on {VALID_FRAME}: {SCENE_ID} (priority 5, started directly, by Show now or the API) on top.")

        # A Scene on frames no Run covers: 0.
        form = show_now(page, "elsewhere", submit=False)
        expect(form).to_contain_text("0 (the default: no Run covers its frames)")
        show_advanced(form)
        expect(form.get_by_label("Activation priority", exact=True)).to_have_value("0")


def test_unhealthy_scene_frame_opens_its_recovery_tab_and_keeps_show_draft(page, registry):
    """Show-now frame health offers the existing Wall recovery route, while an
    informational healthy chip stays plain text and the mounted activation draft/key
    survives a Wall visit."""
    players = _seed(registry)
    _seed_source(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", _scene(SCENE_ID, frame=INVALID_FRAME), by="console")
    runtime.command("set_scene", _scene("healthy", frame=VALID_FRAME), by="console")
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "now", paused_at=registry.clock.utc())
        form = show_now(page, SCENE_ID, submit=False)
        # Bound, its Position not checked: setup's step, the Frame page header's words
        # (domain/frame-status.ts), with the tab that step is on.
        expect(form).to_contain_text(f"{INVALID_FRAME}: Check its Position")
        expect(form.get_by_role("link", name=f"Open Frame {INVALID_FRAME}", exact=True)).to_have_attribute(
            "href", f"#/wall/frames/{INVALID_FRAME}/position")
        # A chip is status only (design fix round 4): its link sits on the line under the chips.
        expect(form.get_by_text(f"{INVALID_FRAME}: Check its Position", exact=True).get_by_role("link")).to_have_count(0)

        # Its Pi heard and presenting changes nothing for it: setup comes first (decision 0036, row 1).
        for player_id in players:
            report_presenting(registry, player_id)
        drive_poll(page)
        expect(form).to_contain_text(f"{INVALID_FRAME}: Check its Position")
        recovery = form.get_by_role("link", name=f"Open Frame {INVALID_FRAME}", exact=True)
        expect(recovery).to_have_attribute(
            "href", f"#/wall/frames/{INVALID_FRAME}/position")
        expect(form.get_by_role("link", name=f"Open Frame {VALID_FRAME}", exact=True)).to_have_count(0)

        recovery.click()
        inspector = page.locator("main > section:not([hidden])")
        expect(inspector.get_by_role("tab", name="Position", exact=True)).to_have_attribute(
            "aria-selected", "true")

        # Ordinary route navigation away and back leaves Show now's values and
        # activation identity intact because its flow remains mounted in the shell.
        go(page, "now")
        visit(page, "#/now/show/review")
        form = show_form(page)
        expect(form.get_by_text(SCENE_ID, exact=True)).to_be_visible()
        expect(form).to_contain_text(f"{INVALID_FRAME}: Check its Position")
        show_advanced(form)
        expect(form.get_by_label("Activation priority", exact=True)).to_have_value("0")

        # Selecting a healthy Frame leaves its chip informational.
        page.get_by_role("navigation", name="Steps", exact=True).get_by_role(
            "button", name="Scene", exact=True).click()
        form.get_by_label("Scene", exact=True).select_option("healthy")
        expect(form).to_contain_text(f"{VALID_FRAME}: Showing")
        expect(form.get_by_role("link", name=f"Open Frame {VALID_FRAME}", exact=True)).to_have_count(0)


def test_a_priority_below_the_default_opens_advanced_and_says_where_it_stays_underneath(
        page, registry):
    """§7 J7: lowering the priority below the covering Run's holds Advanced open with "At
    priority P this stays underneath …"; a Run that starts higher while Review shows opens
    it by itself."""
    _seed(registry)
    queue = _seed_source(registry)
    runtime = _covered(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now", paused_at=registry.clock.utc())
        form = show_now(page, SCENE_ID, submit=False)
        toggle = form.get_by_role("button", name="Advanced", exact=True)
        show_advanced(form)
        priority = form.get_by_label("Activation priority", exact=True)
        priority.fill("3")
        underneath = form.get_by_role("status").filter(has_text="stays underneath")
        expect(underneath).to_have_text(
            f"At priority 3 this stays underneath the Run of evening (priority 5) on {VALID_FRAME}.")
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(toggle).to_be_disabled()

        # At the covering priority it is on top again: the operator may close Advanced.
        priority.fill("5")
        expect(underneath).to_have_count(0)
        expect(toggle).to_be_enabled()
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(form).to_contain_text("Priority 5")

        # Another Run starts above it: Advanced opens by itself and says so.
        runtime.command("set_scene", _scene("late"), by="console")
        runtime.command("activate", "late", "late-act", registry.clock.utc(), priority=7, by="console")
        drive_poll(page)
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(underneath).to_have_text(
            f"At priority 5 this stays underneath the Run of late (priority 7) on {VALID_FRAME}.")


def test_the_activation_key_outlives_steps_sections_the_wall_and_the_overlay(page, registry):
    """Slice 3B §11 with the key in the Show-now draft: after an unknown outcome the
    operator changes step, visits Scenes and the Wall, and the session ends and is signed
    in again; the retry still sends the SAME key, so Central answers the Admission it
    stored and there is one Run. Mutation probe: a new key on a section change."""
    _seed(registry)
    queue = _seed_source(registry)
    _runtime(registry).command("set_scene", _scene(SCENE_ID), by="console")
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now", paused_at=registry.clock.utc())
        sent = _keys(page)

        def committed_then_500(route):
            route.fetch()  # the write reaches Central and commits
            route.fulfill(status=500, content_type="application/json", body='{"error": "internal"}')
        answer_first(page, ACTIVATIONS, committed_then_500)

        form = show_now(page, SCENE_ID, submit=False)
        form.get_by_role("button", name="Activate now", exact=True).click()
        expect(_outcome(page)).to_have_text(UNKNOWN)
        assert current_hash(page) == "#/now/show/review"

        # A step change and back: the form is unchanged.
        steps = page.get_by_role("navigation", name="Steps", exact=True)
        steps.get_by_role("button", name="Scene", exact=True).click()
        form.get_by_role("button", name="Continue", exact=True).click()
        # Other sections and the Wall.
        go(page, "scenes")
        go(page, "wall")
        # The session ends: the overlay keeps the shell, and the draft, mounted.
        page.route(SNAPSHOT, lambda route: route.fulfill(
            status=401, content_type="application/json", body='{"error": "unauthorized"}'))
        with page.expect_response(SNAPSHOT):
            page.clock.run_for(5000)
        expect(page.get_by_role("heading", name="Sign in to Photo Wall")).to_be_visible()
        page.unroute(SNAPSHOT)
        submit_sign_in(page)

        go(page, "now")
        _runs(page).get_by_role("button", name=re.compile(r"^Resume draft")).click()
        expect(_outcome(page)).to_have_text(UNKNOWN)
        with page.expect_response(lambda r: r.url.endswith("/v1/operator/activations")):
            show_form(page).get_by_role("button", name="Activate now", exact=True).click()
        expect(_outcome(page)).to_have_text(f"Started: Central admitted a Run of {SCENE_ID}.")
        assert len(sent) == 2 and len(set(sent)) == 1, sent
        assert len(_runs_of(page, origin, SCENE_ID)) == 1
        assert current_hash(page) == "#/now"


def test_changing_the_form_makes_a_new_activation(page, registry):
    """Slice 3B §11: after an unknown outcome, a changed form is a new activation (a new
    key); choosing the value it already has changes nothing. Mutation probe: keep the key
    after a form change."""
    _seed(registry)
    queue = _seed_source(registry)
    _runtime(registry).command("set_scene", _scene(SCENE_ID), by="console")
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        sent = _keys(page)
        answer_first(page, ACTIVATIONS, lambda route: route.fulfill(
            status=500, content_type="application/json", body='{"error": "internal"}'))

        form = show_now(page, SCENE_ID, submit=False)
        form.get_by_role("button", name="Activate now", exact=True).click()
        expect(_outcome(page)).to_have_text(UNKNOWN)

        show_advanced(form)
        form.get_by_label("Leave it running", exact=True).check()  # already chosen
        form.get_by_label("Activation priority", exact=True).fill("2")
        with page.expect_response(lambda r: r.url.endswith("/v1/operator/activations")) as info:
            form.get_by_role("button", name="Activate now", exact=True).click()
        assert info.value.request.post_data_json["priority"] == 2
        expect(_outcome(page)).to_have_text(f"Started: Central admitted a Run of {SCENE_ID}.")
        assert len(sent) == 2 and sent[0] != sent[1], sent


def test_an_activation_refused_as_signed_out_keeps_the_draft(page, registry):
    """A 401 on the write is refused before the Runtime: nothing started, and the draft
    stays on Review for after sign-in instead of ending the flow."""
    _seed(registry)
    queue = _seed_source(registry)
    _runtime(registry).command("set_scene", _scene(SCENE_ID), by="console")
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        answer_first(page, ACTIVATIONS, lambda route: route.fulfill(
            status=401, content_type="application/json", body='{"error": "unauthorized"}'))
        form = show_now(page, SCENE_ID, submit=False)
        form.get_by_role("button", name="Activate now", exact=True).click()
        expect(_outcome(page)).to_have_text(
            "Not started: the session ended. Sign in again, then activate.")
        assert current_hash(page) == "#/now/show/review"
        with page.expect_response(lambda r: r.url.endswith("/v1/operator/activations")):
            form.get_by_role("button", name="Activate now", exact=True).click()
        expect(_outcome(page)).to_have_text(f"Started: Central admitted a Run of {SCENE_ID}.")


def test_a_scene_cards_show_now_opens_the_flow_on_that_scene(page, registry):
    """The Scene cards' "Show now" lands on the Show-now flow's Scene step, prefilled from
    the Scene picked (the shell's `recentSceneId`); a clean draft follows a newer pick."""
    _seed(registry)
    queue = _seed_source(registry)
    _covered(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        scenes = page.get_by_role("region", name="Scenes", exact=True)
        scenes.get_by_role("button", name=scene_words("showCard", id=SCENE_ID), exact=True).click()
        expect(page.get_by_role("heading", level=1, name="Now", exact=True)).to_be_visible()
        assert current_hash(page) == "#/now/show/scene"
        expect(show_form(page).get_by_label("Scene", exact=True)).to_have_value(SCENE_ID)

        go(page, "scenes")
        scenes.get_by_role("button", name=scene_words("showCard", id="elsewhere"), exact=True).click()
        expect(show_form(page).get_by_label("Scene", exact=True)).to_have_value(
            "elsewhere")


def test_run_cards_and_the_why_disclosures(page, registry):
    """"See what is showing and why": each Run is a card (state chip, lines, Finish and
    Cancel); per frame, "Why?" opens the precedence list and "Why nothing new?" the media
    chain, each a disclosure. The page is Central's plan and never says "live" (R2)."""
    _seed(registry)
    queue = _seed_source(registry)
    runtime = _covered(registry)
    runtime.command("set_scene", _scene("earlier"), by="console")
    earlier = runtime.command("activate", "earlier", "earlier-act", registry.clock.utc(), by="console").run_id
    runtime.command("cancel", earlier, registry.clock.utc(), by="console")
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        runs = _runs(page)
        card = runs.get_by_role("list", name="Running Runs", exact=True).get_by_role(
            "article", name="Scene evening", exact=True)
        expect(card).to_contain_text("Running")
        expect(card).to_contain_text("priority 5")
        expect(card).to_contain_text("started directly (Show now or the API)")
        expect(card.get_by_role("button", name=re.compile(r"^Finish run "))).to_be_enabled()
        expect(card.get_by_role("button", name=re.compile(r"^Cancel run "))).to_be_visible()
        runs.get_by_text("Recently ended (1)", exact=True).click()
        expect(runs.get_by_label("Cancelled Runs", exact=True).get_by_role(
            "article", name="Scene earlier", exact=True)).to_contain_text("Cancelled at")
        expect(runs.get_by_role("article", name="Scene earlier", exact=True).get_by_role(
            "button")).to_have_count(0)

        why = runs.get_by_role("group", name="Why", exact=True)
        why_button = why.get_by_role("button", name=f"Why? {VALID_FRAME}", exact=True)
        nothing_button = why.get_by_role("button", name=f"Why nothing new? {VALID_FRAME}", exact=True)
        precedence = why.get_by_role("list", name="Contribution precedence")
        chain = why.get_by_role("group", name=f"Why nothing new on {VALID_FRAME}?", exact=True)
        expect(why_button).to_have_attribute("aria-expanded", "false")
        expect(precedence).to_have_count(0)
        expect(chain).to_have_count(0)

        why_button.click()
        expect(why_button).to_have_attribute("aria-expanded", "true")
        expect(precedence.get_by_role("listitem")).to_have_count(1)
        nothing_button.click()
        expect(nothing_button).to_have_attribute("aria-expanded", "true")
        expect(chain).to_be_visible()
        # R2: intent, never "live" playback. The one "live" allowed is a Scene's kind,
        # "live from <Source>" (the media chain's "Authored? No: live from holiday:1.").
        copy = visible_page(page).inner_text()
        assert "live from" in copy
        assert not re.search(r"\blive\b(?! from)", copy, re.IGNORECASE), copy

        why_button.click()
        expect(precedence).to_have_count(0)
        expect(chain).to_be_visible()


# --- Review's words, prefill hand-overs and answers that keep the draft (review fixes).


def test_a_priority_zero_run_covering_the_frames_is_named_as_the_default(page, registry):
    """A covering Run of priority 0 still covers the frames: Review says the default comes
    from it, never "no Run covers its frames". Mutation probe: branch on the covering
    priority's value (0) instead of on whether a Run covers the frames."""
    _seed(registry)
    queue = _seed_source(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", _scene("evening"), by="console")
    runtime.command("set_scene", _scene(SCENE_ID), by="console")
    runtime.command("activate", "evening", "evening-act", registry.clock.utc(), priority=0, by="console")
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        form = show_now(page, SCENE_ID, submit=False)
        expect(form).to_contain_text(
            "0 (the default: the highest Run on its frames has priority 0; "
            "at equal priority the newer Run shows on top)")
        expect(form).not_to_contain_text("no Run covers its frames")


def test_a_protecting_scene_below_a_covering_run_says_central_will_refuse_it(page, registry):
    """A Scene that protects its frames is refused below a higher Run covering them
    (central/runtime.py `_protected_conflict`, `protection_not_visible`), so Review says
    so instead of "stays underneath", and Central's answer agrees."""
    _seed(registry)
    queue = _seed_source(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", _scene("evening"), by="console")
    runtime.command("set_scene", _scene(SCENE_ID, protect_frames=True), by="console")
    runtime.command("activate", "evening", "evening-act", registry.clock.utc(), priority=5, by="console")
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        form = show_now(page, SCENE_ID, priority=3, submit=False)
        expect(form.get_by_role("status").filter(has_text="refuse")).to_have_text(
            f"At priority 3 Central will refuse this: it protects {VALID_FRAME}, which the "
            "Run of evening (priority 5) covers. Use priority at least 5.")
        expect(form.get_by_text(re.compile("stays underneath"))).to_have_count(0)
        form.get_by_role("button", name="Activate now", exact=True).click()
        expect(_outcome(page)).to_have_text(
            "Not started: this Scene protects frames that evening's Run (priority 5) covers; "
            "use priority at least 5.")


def test_a_run_protecting_the_frames_is_said_to_refuse_it_at_any_priority(page, registry):
    """Final review: a live Run whose Scene protects one of the frames refuses the
    activation at ANY priority (central/runtime.py `_protected_conflict`,
    `protected_frames`), so Review never promises "on top" there: it says Central will
    refuse it, holds Advanced open, and Central's answer agrees."""
    _seed(registry)
    queue = _seed_source(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", _scene("evening", protect_frames=True), by="console")
    runtime.command("set_scene", _scene(SCENE_ID), by="console")
    runtime.command("activate", "evening", "evening-act", registry.clock.utc(), priority=5, by="console")
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        form = show_now(page, SCENE_ID, submit=False)
        expect(form).to_contain_text(
            "5 (the default: the highest Run on its frames has priority 5)")
        expect(form.get_by_text(re.compile("shows on top"))).to_have_count(0)
        expect(form.get_by_role("status").filter(has_text="refuse")).to_have_text(
            f"Central will refuse this at any priority: {VALID_FRAME} is protected by the Run "
            "of evening.")
        expect(form.get_by_role("button", name="Advanced", exact=True)).to_be_disabled()
        form.get_by_label("Activation priority", exact=True).fill("50")
        expect(form.get_by_role("status").filter(has_text="refuse")).to_contain_text(
            "at any priority")
        form.get_by_role("button", name="Activate now", exact=True).click()
        expect(_outcome(page)).to_have_text(
            f"Not started: {VALID_FRAME} is protected by the Run of evening.")


def _show_scene_card(page, scene_id):
    """A Scene card's "Show now", from the Scenes page."""
    go(page, "scenes")
    page.get_by_role("region", name="Scenes", exact=True).get_by_role(
        "button", name=scene_words("showCard", id=scene_id), exact=True).click()
    expect(page.get_by_role("heading", level=1, name="Now", exact=True)).to_be_visible()


def test_a_card_show_now_offers_its_scene_to_a_changed_draft(page, registry):
    """A card's "Show now" for another Scene keeps a changed draft and offers the Scene
    instead, as the Schedule flow does (one shared hand-over rule)."""
    _seed(registry)
    queue = _seed_source(registry)
    _covered(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        form = show_now(page, SCENE_ID, priority=2, submit=False)
        _show_scene_card(page, "elsewhere")
        assert current_hash(page) == "#/now/show/scene"
        scene = form.get_by_label("Scene", exact=True)
        expect(scene).to_have_value(SCENE_ID)
        expect(form).to_contain_text(f"Your unsaved draft shows Scene {SCENE_ID}.")
        form.get_by_role("button", name=scene_words("showInstead", id="elsewhere"), exact=True).click()
        expect(scene).to_have_value("elsewhere")
        expect(form.get_by_role("button", name=scene_words("showInstead", id="elsewhere"), exact=True)
               ).to_have_count(0)


def test_the_same_scene_handed_over_again_is_offered_again(page, registry):
    """A hand-over is an event (the shell's `recentScene` carries a `seq`): the operator who
    picked a card's Scene, changed the draft to another, and picks the same card again is
    offered it again. Mutation probe: react to a change of the Scene id only."""
    _seed(registry)
    queue = _seed_source(registry)
    _covered(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        _show_scene_card(page, "elsewhere")
        form = show_form(page)
        scene = form.get_by_label("Scene", exact=True)
        expect(scene).to_have_value("elsewhere")
        scene.select_option(SCENE_ID)  # a changed draft
        _show_scene_card(page, "elsewhere")
        expect(scene).to_have_value(SCENE_ID)
        form.get_by_role("button", name=scene_words("showInstead", id="elsewhere"), exact=True).click()
        expect(scene).to_have_value("elsewhere")


def test_a_card_show_now_keeps_a_draft_whose_outcome_is_unknown(page, registry):
    """A clean draft whose last activation's outcome is unknown keeps its Scene and its key
    when a card's "Show now" names another Scene (the retry must not start twice); the
    Scene is offered instead. Mutation probe: follow the card while the outcome is
    unknown."""
    _seed(registry)
    queue = _seed_source(registry)
    _covered(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        sent = _keys(page)
        answer_first(page, ACTIVATIONS, lambda route: route.fulfill(
            status=500, content_type="application/json", body='{"error": "internal"}'))
        # A card's "Show now" seeds the draft with its Scene: the operator changes nothing,
        # so the draft is clean (the sidebar does not say "Draft").
        _show_scene_card(page, SCENE_ID)
        form = show_form(page)
        expect(form.get_by_label("Scene", exact=True)).to_have_value(SCENE_ID)
        form.get_by_role("button", name="Continue", exact=True).click()
        form.get_by_role("button", name="Activate now", exact=True).click()
        expect(_outcome(page)).to_have_text(UNKNOWN)
        now_link = page.get_by_role("navigation", name="Sections", exact=True).get_by_role(
            "link", name="Now", exact=True)
        expect(now_link).to_have_accessible_description("")

        _show_scene_card(page, "elsewhere")
        assert current_hash(page) == "#/now/show/scene"
        expect(form.get_by_label("Scene", exact=True)).to_have_value(SCENE_ID)
        expect(form).to_contain_text(f"Your unsaved draft shows Scene {SCENE_ID}.")
        expect(form.get_by_role("button", name=scene_words("showInstead", id="elsewhere"), exact=True)
               ).to_be_visible()
        form.get_by_role("button", name="Continue", exact=True).click()
        with page.expect_response(lambda r: r.url.endswith("/v1/operator/activations")):
            form.get_by_role("button", name="Activate now", exact=True).click()
        expect(_outcome(page)).to_have_text(f"Started: Central admitted a Run of {SCENE_ID}.")
        assert len(sent) == 2 and len(set(sent)) == 1, sent


def test_an_activation_refused_for_its_origin_keeps_the_draft_and_its_key(page, registry):
    """A 403 origin refusal is answered before the Runtime, as a 401 is: nothing started,
    so the draft and its key stay on Review for the retry. Mutation probe: read a 403 as
    a known outcome (the flow would end and the draft be discarded)."""
    _seed(registry)
    queue = _seed_source(registry)
    _runtime(registry).command("set_scene", _scene(SCENE_ID), by="console")
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        sent = _keys(page)
        answer_first(page, ACTIVATIONS, lambda route: route.fulfill(
            status=403, content_type="application/json", body='{"error": "origin_mismatch"}'))
        form = show_now(page, SCENE_ID, submit=False)
        form.get_by_role("button", name="Activate now", exact=True).click()
        expect(_outcome(page)).to_have_text(
            "Not started: Central refused the request from this page. Reload the console "
            "from the address you signed in at, then activate.")
        assert current_hash(page) == "#/now/show/review"
        with page.expect_response(lambda r: r.url.endswith("/v1/operator/activations")):
            form.get_by_role("button", name="Activate now", exact=True).click()
        expect(_outcome(page)).to_have_text(f"Started: Central admitted a Run of {SCENE_ID}.")
        assert len(sent) == 2 and len(set(sent)) == 1, sent


def test_a_card_show_now_during_an_activation_in_flight_keeps_its_draft_and_key(
        page, registry):
    """§6 History, final review finding 1: while an activation is in flight its draft is
    held (flow/useFlowWrite.js), so a card's "Show now" for another Scene cannot reseed it
    (the draft is clean, and no outcome is unknown yet); the Scene is offered instead. The
    answer is lost (a 500 after Central committed it), so the retry must send the SAME key:
    Central returns the stored Admission and there is one Run. Mutation probe: reseed while
    busy (a new key: "already running, left as is")."""
    _seed(registry)
    queue = _seed_source(registry)
    _covered(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        sent = _keys(page)
        gate = RequestGate(page, ACTIVATIONS)
        gate.holding = True
        _show_scene_card(page, SCENE_ID)  # a clean draft on SCENE_ID
        form = show_form(page)
        scene = form.get_by_label("Scene", exact=True)
        form.get_by_role("button", name="Continue", exact=True).click()
        form.get_by_role("button", name="Activate now", exact=True).click()
        gate.wait_held()

        # In flight, the operator picks another Scene's "Show now": the draft is kept.
        _show_scene_card(page, "elsewhere")
        assert current_hash(page) == "#/now/show/scene"
        expect(scene).to_have_value(SCENE_ID)
        # The committed write's answer is lost.
        route = gate.held.pop(0)
        route.fetch()
        route.fulfill(status=500, content_type="application/json", body='{"error": "internal"}')
        expect(_outcome(page)).to_have_text(UNKNOWN)
        expect(scene).to_have_value(SCENE_ID)
        expect(form.get_by_role("button", name=scene_words("showInstead", id="elsewhere"), exact=True)
               ).to_be_visible()

        # The operator retries the same Scene, as the notice invites: the same key.
        gate.holding = False
        _show_scene_card(page, SCENE_ID)
        expect(scene).to_have_value(SCENE_ID)
        form.get_by_role("button", name="Continue", exact=True).click()
        with page.expect_response(lambda r: r.url.endswith("/v1/operator/activations")):
            form.get_by_role("button", name="Activate now", exact=True).click()
        expect(_outcome(page)).to_have_text(f"Started: Central admitted a Run of {SCENE_ID}.")
        assert len(sent) == 2 and len(set(sent)) == 1, sent
        assert len(_runs_of(page, origin, SCENE_ID)) == 1


def _on_both(scene_id):
    """A live-source Scene on both seeded frames."""
    return Scene(scene_id=scene_id, loop=True, contributions=tuple(
        Contribution(target=f"frame:{frame}", source_refs=(SOURCE,)) for frame in (VALID_FRAME, INVALID_FRAME)))


def test_show_now_on_chosen_frames_with_an_end(page, registry):
    """Decision 0036: the sheet ticks every Frame of the Scene explicitly; one left out is not sent,
    and gets no command (its Run plays on the chosen Frame alone); the end chosen is sent.
    Mutation probes: send every Scene Frame whatever is ticked; send no `ends`."""
    _seed(registry)
    queue = _seed_source(registry)
    _runtime(registry).command("set_scene", _on_both(SCENE_ID), by="console")
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        form = start_show_now(page, SCENE_ID)
        frames = form.get_by_role("group", name="Frames", exact=True)
        for frame in (VALID_FRAME, INVALID_FRAME):
            expect(frames.get_by_role("checkbox", name=frame, exact=True)).to_be_checked()
        frames.get_by_role("checkbox", name=INVALID_FRAME, exact=True).uncheck()
        ends = form.get_by_role("group", name="Ends", exact=True)
        expect(ends.get_by_role("radio", name="In 1 hour", exact=True)).to_be_checked()
        ends.get_by_role("radio", name="Until I stop it", exact=True).check()
        form.get_by_role("button", name="Continue", exact=True).click()
        with page.expect_response(lambda r: r.url.endswith("/v1/operator/activations")) as info:
            form.get_by_role("button", name="Activate now", exact=True).click()
        sent = info.value.request.post_data_json
        assert sent["frame_ids"] == [VALID_FRAME] and sent["ends"] == {"kind": "stopped"}
        expect(_outcome(page)).to_have_text(f"Started: Central admitted a Run of {SCENE_ID}.")
        (run,) = [run for run in _runs_of(page, origin, SCENE_ID) if run["phase"] == "body"]
        assert run["frames"] == [VALID_FRAME]
        assert f"frame:{INVALID_FRAME}" not in run["participants"]


def test_back_to_normal_on_home_ends_the_takeover_at_once(page, registry):
    """Decision 0036, the owner's S3: Home's takeover banner says what is shown and until when, and
    Back to normal ends it now (downward cancellation at an instant: its Run reads cancelled once
    its cut has passed). Mutation probe: draw no takeover banner."""
    _seed(registry)
    queue = _seed_source(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", _scene(SCENE_ID), by="console")
    runtime.command("activate", SCENE_ID, "takeover-1", registry.clock.utc(), by="console",
                    ends_at=registry.clock.utc() + 3600)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "home")
        banner = page.locator("section[data-kind=takeover]")     # the takeover banner (OverrideBanner)
        expect(banner).to_contain_text(SCENE_ID)
        expect(banner).to_contain_text(re.compile(r"until \d\d:\d\d"))
        with page.expect_response(lambda r: r.url.endswith("/end") and r.request.method == "POST") as info:
            banner.get_by_role("button", name="Back to normal", exact=True).click()
        assert info.value.status == 200, info.value.text()
        expect(page.locator("[role=status].fixed")).to_contain_text("Back to normal")
        answer = info.value.json()
        registry.clock.advance(answer["until"] - registry.clock.utc() + 1)
        (run,) = _runs_of(page, origin, SCENE_ID)
        assert run["phase"] == "cancelled"


def test_back_to_normal_reveals_the_schedules_current_state_at_once(page, registry):
    """The owner's D2/S3 end to end, on a wall the real Coordinator plans: Show now on the hall
    alone (the desk left out gets no command), with an end; minutes later, Home's banner's Back to
    normal ends it at once, mid-photo: the cut is `END_LEAD` and the Scene's fade after the press,
    not the takeover's end nor its photo's, and what shows from the cut is the schedule's turn of
    that moment (it kept advancing beneath), not the one the takeover covered. Downward
    cancellation: the Run reads cancelled. Mutation probe: route Back to normal to `finish`."""
    fade = 2
    with operator_server(registry.db, registry.clock) as origin:
        wall = Wall(registry, {"hall": "wall", "desk": "wall"}, cycle=60)
        for frame_id in wall.players:
            registry.rename_frame(frame_id, frame_id.capitalize(), by="console")
        wall.coordinator.runtime.command("set_scene", Scene(
            scene_id="holidays", cycle_seconds=600, loop=True, transition=Transition(seconds=fade),
            contributions=tuple(Contribution(target=f"frame:{frame}", source_refs=("library:1",))
                                for frame in ("hall", "desk"))), by="console")
        schedule = showing_at(wall.layers("hall"), registry.clock.utc()).run_id
        connect(page, origin)
        form = start_show_now(page, "holidays")
        frames = form.get_by_role("group", name="Frames", exact=True)
        frames.get_by_role("checkbox", name="desk", exact=True).uncheck()
        form.get_by_role("button", name="Continue", exact=True).click()
        with page.expect_response(lambda r: r.url.endswith("/v1/operator/activations")) as info:
            form.get_by_role("button", name="Activate now", exact=True).click()
        assert info.value.status == 200, info.value.text()
        sent = info.value.request.post_data_json
        assert sent["frame_ids"] == ["hall"] and sent["ends"] == {"kind": "in", "seconds": 3600}
        takeover = info.value.json()["run_id"]
        wall.tick(5)
        covered = showing_at(wall.layers("hall"), registry.clock.utc())
        assert covered.run_id == takeover
        assert showing_at(wall.layers("desk"), registry.clock.utc()).run_id == schedule
        beneath_then = next(layer for layer in wall.layers("hall") if layer.run_id == schedule
                            and layer.start <= registry.clock.utc() < layer.end)
        for _ in range(5):                       # minutes pass; the schedule advances beneath
            wall.tick(30)

        go(page, "home")
        banner = page.locator("section[data-kind=takeover]").filter(has_text="holidays")
        expect(banner).to_be_visible(timeout=15_000)
        pressed = registry.clock.utc()
        with page.expect_response(lambda r: r.url.endswith("/end") and r.request.method == "POST") as ended:
            banner.get_by_role("button", name="Back to normal", exact=True).click()
        assert ended.value.status == 200, ended.value.text()
        cut = ended.value.json()["until"]
        assert cut <= pressed + END_LEAD + fade + 1, (pressed, cut)       # at once, mid-photo
        assert cut < covered.end
        expect(page.locator("[role=status].fixed")).to_contain_text("Back to normal")
        wall.tick()
        wall.tick(cut + 1 - registry.clock.utc())
        revealed = showing_at(wall.layers("hall"), registry.clock.utc())
        assert revealed.run_id == schedule, revealed
        assert revealed.start < cut and revealed.assignment_id != beneath_then.assignment_id
        assert showing_at(wall.layers("desk"), registry.clock.utc()).run_id == schedule
        (run,) = [run for run in _runs_of(page, origin, "holidays")]
        assert run["phase"] == "cancelled"
        expect(banner).to_have_count(0, timeout=15_000)
