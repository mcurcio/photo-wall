"""Behavioral checks for the Scene flow and the flow kit (flow design §6, §7 J4; bead 2).

Reuses the operator browser harness (real Chromium against the production create_app, the
disposable-schema `registry` fixture and the autouse `page_errors` guard) and the Showrunner
file's seeding. The tests that were about the single Scene form moved with it to the flow in
test_operator_showrunner_browser.py; these cover what the flow adds: steps as routes that
replace their history entry, one draft per flow, problem routing, Edit's revision guard,
the vanished-frame prune on every step, and the narrow layout.
"""

import os
import re

import pytest
from console_tasks import (
    HAND_PICKED,
    LIVE,
    author_scene,
    connect,
    go,
    scene_continue,
    scene_form,
    start_scene,
    visit,
)
from operator_harness import operator_server
from playwright.sync_api import expect
from test_operator_showrunner_browser import (
    LOBBY_FRAME,
    SOURCE,
    VALID_FRAME,
    _add_lobby_frame,
    _console_scene,
    _runtime,
    _seed,
    _seed_source,
)
from test_registry import ADMIN

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

NARROW = {"width": 390, "height": 844}


def _hash(page):
    return page.evaluate("window.location.hash")


def _scenes(page):
    return page.get_by_role("region", name="Scenes", exact=True)


def _steps(page):
    return page.get_by_role("navigation", name="Steps", exact=True)


def _scenes_link(page):
    return page.get_by_role("navigation", name="Sections", exact=True).get_by_role(
        "link", name="Scenes", exact=True)


def _puts(page):
    """Every PUT the page sends from now on, by URL."""
    sent = []
    page.on("request", lambda request: sent.append(request.url)
            if request.method == "PUT" else None)
    return sent


def test_kind_is_the_first_question_and_decides_the_steps(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        _scenes(page).get_by_role("button", name="New Scene", exact=True).click()
        assert _hash(page) == "#/scenes/new/kind"
        form = scene_form(page)
        # Live is the stated default; the stepper lists five steps for it.
        expect(form.get_by_label(LIVE, exact=True)).to_be_checked()
        items = _steps(page).get_by_role("listitem")
        expect(items).to_have_text(["1Kind", "2Photos", "3Frames", "4Playback", "5Review"])
        # Hand-picked adds "Media per frame" after Frames.
        form.get_by_label(HAND_PICKED, exact=True).check()
        expect(items).to_have_text(
            ["1Kind", "2Photos", "3Frames", "4Media per frame", "5Playback", "6Review"])

        # Continue checks the current step only: Photos asks for the Source, and the
        # frames, asked later, are not listed yet.
        scene_continue(page, "Photos")
        form.get_by_role("button", name="Continue", exact=True).click()
        summary = form.get_by_role("alert")
        expect(summary).to_contain_text("Choose a Source.")
        expect(summary).not_to_contain_text("Choose at least one frame.")
        expect(form.get_by_label("Source", exact=True)).to_be_focused()
        assert _hash(page) == "#/scenes/new/photos"


def test_steps_replace_their_entry_and_back_leaves_the_flow_keeping_the_draft(page, registry):
    """Question 2: steps move with `replace` (the flow is one history entry); browser Back
    leaves the flow, the draft is kept, the page offers "Resume draft (Draft)" and the
    sidebar marks Scenes with the word "Draft". Mutation probe: push the steps."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        before = page.evaluate("history.length")
        expect(_scenes_link(page)).to_have_accessible_description("")
        form = start_scene(page)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        assert _hash(page) == "#/scenes/new/frames"
        assert page.evaluate("history.length") == before + 1
        expect(_scenes_link(page)).to_have_accessible_description("Draft")
        expect(_scenes_link(page)).to_contain_text("Draft")

        page.go_back()
        expect(_scenes(page).get_by_role("button", name="Resume draft (Draft)", exact=True)
               ).to_be_visible()
        assert _hash(page) == "#/scenes"
        expect(_scenes_link(page)).to_have_accessible_description("Draft")

        _scenes(page).get_by_role("button", name="Resume draft (Draft)", exact=True).click()
        assert _hash(page) == "#/scenes/new/frames"
        expect(form.get_by_label(f"Target frame {VALID_FRAME}", exact=True)).to_be_checked()


def test_enter_submits_the_steps_continue(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = author_scene(page, "enter-scene", SOURCE, (VALID_FRAME,), submit=False)
        page.get_by_role("navigation", name="Steps", exact=True).get_by_role(
            "button", name="Playback", exact=True).click()
        seconds = form.get_by_label("Seconds per cycle", exact=True)
        seconds.fill("12")
        seconds.press("Enter")
        expect(form.get_by_label("Scene name", exact=True)).to_be_visible()
        assert _hash(page) == "#/scenes/new/review"
        # Enter in the name submits Review's Save Scene.
        with page.expect_response(
            lambda r: r.url.endswith("/v1/operator/scenes/enter-scene") and r.request.method == "PUT"
        ) as info:
            form.get_by_label("Scene name", exact=True).press("Enter")
        assert info.value.request.post_data_json["cycle_seconds"] == 12


def test_a_review_problem_opens_its_step_and_focuses_the_field(page, registry):
    """§7: Review checks everything; each summary entry routes through FIELD_STEP to the
    owning step and focuses the field there, and a field under Advanced opens it first.
    After a routed problem, Continue returns toward Review. Mutation probe: map a field to
    the wrong step."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        puts = _puts(page)
        visit(page, "#/scenes/new/review")  # a typed URL skips the earlier steps
        form = scene_form(page)
        form.get_by_label("Scene name", exact=True).fill("Routed")
        form.get_by_role("button", name="Save Scene", exact=True).click()
        summary = form.get_by_role("alert")
        expect(summary.get_by_role("button")).to_have_text(
            ["Choose a Source.", "Choose at least one frame."])

        summary.get_by_role("button", name="Choose a Source.", exact=True).click()
        assert _hash(page) == "#/scenes/new/photos"
        source = form.get_by_label("Source", exact=True)
        expect(source).to_be_focused()
        source.select_option(SOURCE)
        # Continue goes to the next step that still has a problem: Frames.
        scene_continue(page, "Frames")
        frame = form.get_by_label(f"Target frame {VALID_FRAME}", exact=True)
        frame.check()
        # ...then, nothing left before it, back to Review (Playback is skipped).
        scene_continue(page, "Review")

        # An invalid id under Advanced: Save opens Advanced and focuses the Id.
        advanced = form.get_by_role("button", name="Advanced", exact=True)
        advanced.click()
        form.get_by_label("Id", exact=True).fill("-bad")
        advanced.click()
        expect(advanced).to_have_attribute("aria-expanded", "false")
        form.get_by_role("button", name="Save Scene", exact=True).click()
        expect(form.get_by_role("alert")).to_contain_text("An id starts with a letter or digit")
        expect(advanced).to_have_attribute("aria-expanded", "true")
        expect(form.get_by_label("Id", exact=True)).to_be_focused()
        assert puts == []


def test_a_stale_edit_offers_reload_and_never_sends_a_replace(page, registry):
    """§7 Edit: a poll that shows a newer stored revision withholds Replace and offers
    Reload, which reseeds the draft, resets its base revision and names what changed; no
    PUT is sent while stale, so there is no 409 loop. Mutation probe: reseed keeps the old
    base revision."""
    _seed(registry)
    queue = _seed_source(registry)
    _runtime(registry).command("set_scene", _console_scene("evening"))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes", paused_at=registry.clock.utc())
        _scenes(page).get_by_role("button", name="Edit Scene evening", exact=True).click()
        form = scene_form(page)
        expect(form).to_contain_text("Editing evening · revision 1.")
        replace = form.get_by_role("button", name="Replace Scene", exact=True)
        expect(replace).to_be_enabled()

        # Another operator saves revision 2; the next poll shows it.
        other = _console_scene("evening", revision=2, cycle_seconds=20).model_dump(mode="json")
        response = page.request.put(origin + "/v1/operator/scenes/evening",
                                    headers={"Authorization": "Bearer " + ADMIN}, data=other)
        assert response.status == 200, response.text()
        puts = _puts(page)
        page.clock.run_for(5000)
        expect(_scenes(page)).to_contain_text(
            "This Scene was changed (revision 2) since you opened it.")
        expect(replace).to_be_disabled()
        form.evaluate("(element) => element.requestSubmit()")  # Enter, in effect
        page.wait_for_timeout(200)
        assert puts == []

        _scenes(page).get_by_role("button", name="Reload", exact=True).click()
        expect(_scenes(page).get_by_role("status").filter(has_text="Reloaded")).to_have_text(
            "Reloaded revision 2. Changed: Seconds per cycle.")
        expect(_scenes(page).get_by_text(re.compile("was changed"))).to_have_count(0)
        expect(form).to_contain_text("Editing evening · revision 2.")
        expect(replace).to_be_enabled()
        replace.click()
        with page.expect_response(
            lambda r: r.url.endswith("/v1/operator/scenes/evening") and r.request.method == "PUT"
        ) as info:
            page.get_by_role("dialog").get_by_role("button", name="Confirm replace").click()
        assert info.value.status == 200
        body = info.value.request.post_data_json
        assert (body["revision"], body["cycle_seconds"]) == (3, 20)


def test_a_frame_deleted_mid_draft_is_announced_on_the_current_step_and_on_review(
        page, registry):
    """§7: the vanished-frame prune is the flow container's, so it runs whichever step
    shows (here Playback, which never shows the frames) and its notice follows the draft to
    Review. Mutation probe: move the prune into the Frames step."""
    _seed(registry)
    _add_lobby_frame(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes", paused_at=registry.clock.utc())
        form = start_scene(page)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        form.get_by_label(f"Target frame {LOBBY_FRAME}", exact=True).check()
        scene_continue(page, "Playback")

        registry.delete_frame(LOBBY_FRAME)
        page.clock.run_for(5000)
        notice = f"{LOBBY_FRAME} was deleted and removed from this Scene."
        expect(form.get_by_role("status")).to_have_text(notice)
        scene_continue(page, "Review")
        expect(form.get_by_role("status")).to_have_text(notice)
        answers = form.get_by_label("Your answers", exact=True)
        expect(answers).to_contain_text(VALID_FRAME)
        expect(answers).not_to_contain_text(LOBBY_FRAME)
        form.get_by_label("Scene name", exact=True).fill("pruned")
        with page.expect_response(
            lambda r: r.url.endswith("/v1/operator/scenes/pruned") and r.request.method == "PUT"
        ) as info:
            form.get_by_role("button", name="Save Scene", exact=True).click()
        targets = [c["target"] for c in info.value.request.post_data_json["contributions"]]
        assert targets == [f"frame:{VALID_FRAME}"]


def test_at_phone_width_the_stepper_collapses_and_the_footer_sticks(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    page.set_viewport_size(NARROW)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = start_scene(page)
        steps = _steps(page)
        expect(steps.get_by_text("Step 2 of 5 · Photos", exact=True)).to_be_visible()
        expect(steps.get_by_role("list")).to_be_hidden()
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page)
        expect(steps.get_by_text("Step 3 of 5 · Frames", exact=True)).to_be_visible()
        footer = form.locator(".flow__footer")
        assert footer.evaluate("(el) => getComputedStyle(el).position") == "sticky"
        expect(form.get_by_role("button", name="Continue", exact=True)).to_be_in_viewport()
        expect(form.get_by_role("button", name="Back", exact=True)).to_be_in_viewport()
        fits = page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
        assert fits, "the Scene flow overflows at 390 px"


def test_another_instance_never_replaces_a_dirty_draft(page, registry):
    """§6: a typed URL naming another instance shows "Unsaved draft for X: Resume or
    Discard"; an in-app Edit asks through the confirmation dialog; a stale edit route says
    the Scene no longer exists."""
    _seed(registry)
    queue = _seed_source(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", _console_scene("evening"))
    runtime.command("set_scene", _console_scene("morning"))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = start_scene(page)
        form.get_by_label("Source", exact=True).select_option(SOURCE)

        # A typed URL: the draft is kept until the operator decides.
        visit(page, "#/scenes/evening/edit/review")
        blocked = _scenes(page).get_by_text(
            "Unsaved draft for a new Scene: Resume or Discard", exact=True)
        expect(blocked).to_be_visible()
        expect(scene_form(page)).to_have_count(0)
        _scenes(page).get_by_role("button", name="Resume", exact=True).click()
        assert _hash(page) == "#/scenes/new/photos"
        expect(form.get_by_label("Source", exact=True)).to_have_value(SOURCE)
        visit(page, "#/scenes/evening/edit/review")
        _scenes(page).get_by_role("button", name="Discard", exact=True).click()
        expect(form).to_contain_text("Editing evening · revision 1.")

        # In the app: Edit on another card asks first; Cancel keeps the draft.
        form.get_by_role("button", name="Change Seconds per cycle", exact=True).click()
        form.get_by_label("Seconds per cycle", exact=True).fill("50")
        go(page, "scenes")
        _scenes(page).get_by_role("button", name="Edit Scene morning", exact=True).click()
        dialog = page.get_by_role("dialog", name="Discard your unsaved draft?")
        expect(dialog).to_contain_text("Your unsaved changes to Scene evening will be lost.")
        dialog.get_by_role("button", name="Cancel", exact=True).click()
        expect(_scenes(page).get_by_role("button", name="Resume draft (Draft)", exact=True)
               ).to_be_visible()
        _scenes(page).get_by_role("button", name="Edit Scene morning", exact=True).click()
        dialog.get_by_role("button", name="Discard draft", exact=True).click()
        expect(form).to_contain_text("Editing morning · revision 1.")
        assert _hash(page) == "#/scenes/morning/edit/review"

        visit(page, "#/scenes/ghost/edit/review")
        expect(_scenes(page).get_by_text("Scene ghost: This Scene no longer exists.")
               ).to_be_visible()


def test_saving_returns_to_the_cards_and_offers_show_now_and_schedule_it(page, registry):
    """§6 history: Save replaces the flow entry with #/scenes, so Back never re-enters the
    finished flow; the next actions are Show now and Schedule it."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        author_scene(page, "saved-scene", SOURCE, (VALID_FRAME,))
        assert _hash(page) == "#/scenes"
        scenes = _scenes(page)
        expect(scenes.get_by_role("status")).to_have_text("Saved Scene saved-scene.")
        next_actions = scenes.get_by_role("group", name="Next for Scene saved-scene")
        expect(next_actions.get_by_role("button")).to_have_text(["Show now", "Schedule it"])
        card = scenes.get_by_label("Scene saved-scene", exact=True)
        expect(card.get_by_role("button")).to_have_text(["Edit", "Show now", "Schedule it"])

        page.go_back()
        assert not _hash(page).startswith("#/scenes/new")
        go(page, "scenes")
        next_actions.get_by_role("button", name="Schedule it", exact=True).click()
        expect(page.get_by_role("heading", level=1, name="Schedule", exact=True)).to_be_visible()
        assert _hash(page) == "#/schedule"
