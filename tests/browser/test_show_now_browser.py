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
from urllib.parse import quote

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
    report_readiness,
    submit_sign_in,
)
from playwright.sync_api import expect
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

from central.runtime import Contribution, Scene

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
    runtime.command("set_scene", _scene("evening"))
    runtime.command("set_scene", _scene(SCENE_ID))
    runtime.command("set_scene", _scene("elsewhere", frame=INVALID_FRAME))
    runtime.command("activate", "evening", "evening-act", registry.clock.utc(), priority=5)
    return runtime


def test_show_now_reports_saved_source_freshness_and_refresh_receipt(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    _runtime(registry).command("set_scene", _scene(SCENE_ID))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        form = start_show_now(page, SCENE_ID)
        freshness = form.get_by_role("region", name="Saved Source freshness", exact=True)
        expect(freshness).to_contain_text("holiday: Awaiting refresh")
        expect(freshness).to_contain_text("does not confirm prepared media or visible playback")
        expect(freshness).not_to_contain_text(SOURCE)

        with page.expect_response(lambda response: response.url.endswith(
                f"/v1/operator/sources/{quote(SOURCE, safe='')}/refresh") and
                response.request.method == "POST") as info:
            freshness.get_by_role("button", name="Refresh holiday", exact=True).click()
        assert info.value.status == 202
        expect(freshness).to_contain_text(
            "Refresh requested. The status will update when the media worker finishes.")
        form.get_by_role("button", name="Continue", exact=True).click()
        freshness = form.get_by_role("region", name="Saved Source freshness", exact=True)
        expect(freshness).to_contain_text("holiday: Awaiting refresh")
        expect(freshness).to_contain_text("Refresh requested.")
        expect(form.get_by_role("button", name="Activate now", exact=True)).to_be_enabled()


def test_show_now_handles_historical_and_authored_scenes_without_refresh(page, registry):
    _seed(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", Scene(scene_id="historical", contributions=(
        Contribution(target=f"frame:{VALID_FRAME}", source_refs=("gone:1",)),)))
    runtime.command("set_scene", Scene(scene_id="fixed", contributions=(
        Contribution(target=f"frame:{VALID_FRAME}", asset_refs=("fixed-item",)),)))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "now")
        form = start_show_now(page, "historical")
        freshness = form.get_by_role("region", name="Saved Source freshness", exact=True)
        expect(freshness).to_contain_text("gone: Current Source status is unavailable.")
        expect(freshness).not_to_contain_text("gone:1")
        expect(freshness.get_by_role("button", name=re.compile("Refresh"))).to_have_count(0)
        form.get_by_role("button", name="Continue", exact=True).click()
        expect(form.get_by_role("region", name="Saved Source freshness", exact=True)).to_contain_text(
            "Current Source status is unavailable.")
        page.get_by_role("navigation", name="Steps", exact=True).get_by_role(
            "button", name="Scene", exact=True).click()
        form.get_by_label("Scene to activate", exact=True).select_option("fixed")
        freshness = form.get_by_role("region", name="Saved Source freshness", exact=True)
        expect(freshness).to_contain_text(
            "This Scene uses hand-picked media; refreshing a Source does not change its chosen items.")
        expect(freshness.get_by_role("button", name=re.compile("Refresh"))).to_have_count(0)


def test_show_now_ignores_refresh_receipt_after_scene_change(page, registry):
    _seed(registry)
    _seed_source(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", _scene("first"))
    runtime.command("set_scene", _scene("second"))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "now")
        form = start_show_now(page, "first")
        freshness = form.get_by_role("region", name="Saved Source freshness", exact=True)
        gate = RequestGate(page, "**/v1/operator/sources/*/refresh")
        gate.holding = True
        freshness.get_by_role("button", name="Refresh holiday", exact=True).click()
        gate.wait_held()
        expect(freshness.get_by_role("button", name="Refresh holiday", exact=True)).to_be_disabled()
        form.get_by_label("Scene to activate", exact=True).select_option("second")
        gate.release(status=202, content_type="application/json", body='{"requested_revision": 1}')
        freshness = form.get_by_role("region", name="Saved Source freshness", exact=True)
        expect(freshness).to_contain_text("holiday: Awaiting refresh")
        expect(freshness).not_to_contain_text("Refresh requested.")
        expect(freshness.get_by_role("button", name="Refresh holiday", exact=True)).to_be_enabled()


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

        # Central's plan puts the new Run on top of the frame.
        why = _runs(page).get_by_role("group", name="Why", exact=True)
        why.get_by_role("button", name=f"Why? {VALID_FRAME}", exact=True).click()
        expect(why).to_contain_text(
            f"Central's plan for {VALID_FRAME}: {SCENE_ID} (priority 5, activated directly) on top.")

        # A Scene on frames no Run covers: 0.
        form = show_now(page, "elsewhere", submit=False)
        expect(form).to_contain_text("0 (the default: no Run covers its frames)")
        show_advanced(form)
        expect(form.get_by_label("Activation priority", exact=True)).to_have_value("0")


def test_unhealthy_scene_frame_opens_its_recovery_facet_and_keeps_show_draft(page, registry):
    """Show-now frame health offers the existing Wall recovery route, while an
    informational healthy chip stays plain text and the mounted activation draft/key
    survives a Wall visit."""
    players = _seed(registry)
    _seed_source(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", _scene(SCENE_ID, frame=INVALID_FRAME))
    runtime.command("set_scene", _scene("healthy", frame=VALID_FRAME))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "now", paused_at=registry.clock.utc())
        form = show_now(page, SCENE_ID, submit=False)
        expect(form).to_contain_text(f"{INVALID_FRAME}: No report yet")
        expect(form.get_by_role("link", name=f"Open Frame {INVALID_FRAME}", exact=True)).to_have_attribute(
            "href", f"#/wall/frames/{INVALID_FRAME}/binding")

        for player_id in players:
            report_readiness(registry, player_id)
        drive_poll(page)
        expect(form).to_contain_text(f"{INVALID_FRAME}: Needs commissioning")
        recovery = form.get_by_role("link", name=f"Open Frame {INVALID_FRAME}", exact=True)
        expect(recovery).to_have_attribute(
            "href", f"#/wall/frames/{INVALID_FRAME}/commissioning")
        expect(form.get_by_role("link", name=f"Open Frame {VALID_FRAME}", exact=True)).to_have_count(0)

        recovery.click()
        inspector = page.get_by_role("region", name=f"Frame {INVALID_FRAME} inspector", exact=True)
        expect(inspector.get_by_role("tab", name="Commissioning", exact=True)).to_have_attribute(
            "aria-selected", "true")

        # Ordinary route navigation away and back leaves Show now's values and
        # activation identity intact because its flow remains mounted in the shell.
        go(page, "now")
        visit(page, "#/now/show/review")
        form = show_form(page)
        expect(form.get_by_text(SCENE_ID, exact=True)).to_be_visible()
        expect(form).to_contain_text(f"{INVALID_FRAME}: Needs commissioning")
        show_advanced(form)
        expect(form.get_by_label("Activation priority", exact=True)).to_have_value("0")

        # Selecting a healthy Frame leaves its chip informational.
        page.get_by_role("navigation", name="Steps", exact=True).get_by_role(
            "button", name="Scene", exact=True).click()
        form.get_by_label("Scene to activate", exact=True).select_option("healthy")
        expect(form).to_contain_text(f"{VALID_FRAME}: Heard recently")
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
        runtime.command("set_scene", _scene("late"))
        runtime.command("activate", "late", "late-act", registry.clock.utc(), priority=7)
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
    _runtime(registry).command("set_scene", _scene(SCENE_ID))
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
    _runtime(registry).command("set_scene", _scene(SCENE_ID))
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
    _runtime(registry).command("set_scene", _scene(SCENE_ID))
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
        scenes.get_by_role("button", name=f"Show Scene {SCENE_ID} now", exact=True).click()
        expect(page.get_by_role("heading", level=1, name="Now showing", exact=True)).to_be_visible()
        assert current_hash(page) == "#/now/show/scene"
        expect(show_form(page).get_by_label("Scene to activate", exact=True)).to_have_value(SCENE_ID)

        go(page, "scenes")
        scenes.get_by_role("button", name="Show Scene elsewhere now", exact=True).click()
        expect(show_form(page).get_by_label("Scene to activate", exact=True)).to_have_value(
            "elsewhere")


def test_run_cards_and_the_why_disclosures(page, registry):
    """"See what is showing and why": each Run is a card (state chip, lines, Finish and
    Cancel); per frame, "Why?" opens the precedence list and "Why nothing new?" the media
    chain, each a disclosure. The page is Central's plan and never says "live" (R2)."""
    _seed(registry)
    queue = _seed_source(registry)
    runtime = _covered(registry)
    runtime.command("set_scene", _scene("earlier"))
    earlier = runtime.command("activate", "earlier", "earlier-act", registry.clock.utc()).run_id
    runtime.command("cancel", earlier, registry.clock.utc())
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        runs = _runs(page)
        card = runs.get_by_role("list", name="Running Runs", exact=True).get_by_role(
            "article", name="Scene evening", exact=True)
        expect(card).to_contain_text("Running")
        expect(card).to_contain_text("priority 5")
        expect(card).to_contain_text("activated directly")
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
    runtime.command("set_scene", _scene("evening"))
    runtime.command("set_scene", _scene(SCENE_ID))
    runtime.command("activate", "evening", "evening-act", registry.clock.utc(), priority=0)
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
    runtime.command("set_scene", _scene("evening"))
    runtime.command("set_scene", _scene(SCENE_ID, protect_frames=True))
    runtime.command("activate", "evening", "evening-act", registry.clock.utc(), priority=5)
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
    runtime.command("set_scene", _scene("evening", protect_frames=True))
    runtime.command("set_scene", _scene(SCENE_ID))
    runtime.command("activate", "evening", "evening-act", registry.clock.utc(), priority=5)
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
        "button", name=f"Show Scene {scene_id} now", exact=True).click()
    expect(page.get_by_role("heading", level=1, name="Now showing", exact=True)).to_be_visible()


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
        scene = form.get_by_label("Scene to activate", exact=True)
        expect(scene).to_have_value(SCENE_ID)
        expect(form).to_contain_text(f"Your unsaved draft shows Scene {SCENE_ID}.")
        form.get_by_role("button", name="Show Scene elsewhere instead", exact=True).click()
        expect(scene).to_have_value("elsewhere")
        expect(form.get_by_role("button", name="Show Scene elsewhere instead", exact=True)
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
        scene = form.get_by_label("Scene to activate", exact=True)
        expect(scene).to_have_value("elsewhere")
        scene.select_option(SCENE_ID)  # a changed draft
        _show_scene_card(page, "elsewhere")
        expect(scene).to_have_value(SCENE_ID)
        form.get_by_role("button", name="Show Scene elsewhere instead", exact=True).click()
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
        expect(form.get_by_label("Scene to activate", exact=True)).to_have_value(SCENE_ID)
        form.get_by_role("button", name="Continue", exact=True).click()
        form.get_by_role("button", name="Activate now", exact=True).click()
        expect(_outcome(page)).to_have_text(UNKNOWN)
        now_link = page.get_by_role("navigation", name="Sections", exact=True).get_by_role(
            "link", name="Now showing", exact=True)
        expect(now_link).to_have_accessible_description("")

        _show_scene_card(page, "elsewhere")
        assert current_hash(page) == "#/now/show/scene"
        expect(form.get_by_label("Scene to activate", exact=True)).to_have_value(SCENE_ID)
        expect(form).to_contain_text(f"Your unsaved draft shows Scene {SCENE_ID}.")
        expect(form.get_by_role("button", name="Show Scene elsewhere instead", exact=True)
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
    _runtime(registry).command("set_scene", _scene(SCENE_ID))
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
        scene = form.get_by_label("Scene to activate", exact=True)
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
        expect(form.get_by_role("button", name="Show Scene elsewhere instead", exact=True)
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
