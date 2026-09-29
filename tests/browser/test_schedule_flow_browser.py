"""Behavioral checks for the Schedule flow and the Program cards (flow design §7 J6; bead 4).

Reuses the operator browser harness (real Chromium against the production create_app, the
disposable-schema `registry` fixture and the autouse `page_errors` guard) and the Showrunner
file's seeding. The tests that were about the single Programs form (the windows helper, the
time zone, removal, the states) moved with it to the flow in
test_operator_showrunner_browser.py; these cover what the flow adds: the Scene prefilled
from the Scene flow and the Scene cards, priority's stated default, Continue scoped to its
step, Review's problems routed to their step and Advanced, the draft kept across sections,
and Save leaving the flow for good.
"""

import os
import re

import pytest
from console_tasks import (
    author_scene,
    connect,
    current_hash,
    go,
    schedule_continue,
    schedule_form,
    schedule_program,
    start_schedule,
    visit,
)
from operator_harness import answer_first, operator_server
from playwright.sync_api import expect
from test_operator_showrunner_browser import (
    PROGRAM_ID,
    SOURCE,
    VALID_FRAME,
    WINDOW_END,
    WINDOW_START,
    _console_scene,
    _runtime,
    _seed,
    _seed_source,
)
from test_registry import ADMIN

from central.runtime import Program

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)



def _programs(page):
    return page.get_by_role("region", name="Programs", exact=True)


def _schedule_link(page):
    return page.get_by_role("navigation", name="Sections", exact=True).get_by_role(
        "link", name="Schedule", exact=True)


def _put(page):
    return page.expect_response(
        lambda r: "/v1/operator/programs/" in r.url and r.request.method == "PUT")


def test_schedule_it_prefills_the_scene_from_the_scene_flow_and_a_card(page, registry):
    """§7 J6 step 1: the Scene is prefilled from the Scene just saved ("Schedule it" after
    Save) or picked on a Scene card, and "Schedule it" lands on the Scene step. A draft the
    operator changed is kept, and the Scene step offers the handed-over Scene instead.
    Mutation probe: seed the draft without `recentSceneId`."""
    _seed(registry)
    queue = _seed_source(registry)
    _runtime(registry).command("set_scene", _console_scene("evening"))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        author_scene(page, "saved-scene", SOURCE, (VALID_FRAME,))
        scenes = page.get_by_role("region", name="Scenes", exact=True)
        scenes.get_by_role("group", name="Next for Scene saved-scene").get_by_role(
            "button", name="Schedule it", exact=True).click()
        assert current_hash(page) == "#/schedule/new/scene"
        form = schedule_form(page)
        scene = form.get_by_label("Scene", exact=True)
        expect(scene).to_have_value("saved-scene")

        # A card's "Schedule it" refills the untouched draft.
        go(page, "scenes")
        scenes.get_by_role("button", name="Schedule Scene evening", exact=True).click()
        assert current_hash(page) == "#/schedule/new/scene"
        expect(scene).to_have_value("evening")

        # A changed draft is kept; the hand-over is offered, not forced.
        schedule_continue(page, "When")
        form.get_by_label("Window start", exact=True).fill(WINDOW_START)
        go(page, "scenes")
        scenes.get_by_role("button", name="Schedule Scene saved-scene", exact=True).click()
        assert current_hash(page) == "#/schedule/new/scene"
        expect(scene).to_have_value("evening")
        expect(form).to_contain_text("Your unsaved draft schedules Scene evening.")
        form.get_by_role("button", name="Schedule Scene saved-scene instead", exact=True).click()
        expect(scene).to_have_value("saved-scene")
        schedule_continue(page, "When")
        expect(form.get_by_label("Window start", exact=True)).to_have_value(WINDOW_START)


def test_priority_defaults_to_zero_shown_on_review_and_changes_under_advanced(page, registry):
    """§7 J6 step 3: Priority's stated default, 0, is on Review with the other answers,
    and is changed under Review's Advanced. Mutation probe: default the priority to 1."""
    _seed(registry)
    queue = _seed_source(registry)
    _runtime(registry).command("set_scene", _console_scene("evening"))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        form = schedule_program(page, "first", "evening", WINDOW_START, WINDOW_END, submit=False)
        answers = form.get_by_label("Your answers", exact=True)
        expect(answers).to_contain_text(re.compile(r"Priority0Change"))
        advanced = form.get_by_role("button", name="Advanced", exact=True)
        expect(advanced).to_have_attribute("aria-expanded", "false")
        expect(advanced).to_have_accessible_description(re.compile(r"^Priority: 0 · Id: first$"))
        with _put(page) as info:
            form.get_by_role("button", name="Schedule Program", exact=True).click()
        assert info.value.request.post_data_json["priority"] == 0

        form = schedule_program(page, "second", "evening", WINDOW_START, WINDOW_END, submit=False)
        form.get_by_role("button", name="Change Priority", exact=True).click()
        priority = form.get_by_label("Priority", exact=True)
        expect(priority).to_be_focused()
        expect(advanced).to_have_attribute("aria-expanded", "true")
        priority.fill("7")
        expect(answers).to_contain_text(re.compile(r"Priority7Change"))
        with _put(page) as info:
            form.get_by_role("button", name="Schedule Program", exact=True).click()
        assert info.value.request.post_data_json["priority"] == 7
        expect(_programs(page).get_by_label("Program second", exact=True)).to_contain_text(
            "Priority 7")


def test_future_program_edit_posts_exact_expected_program_and_keeps_its_id(page, registry):
    """Editing opens at Review, disables the multi-window helper and replaces against
    the exact stored Program; the id stays fixed and only future Runs use the change."""
    _seed(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", _console_scene("evening"))
    # These are the later occurrence of 01:30 in the 2026 Los Angeles fallback,
    # then 02:30 PST. The browser suite runs with TZ=America/Los_Angeles.
    baseline = Program(program_id="editable", scene_id="evening", starts_at=1793525400,
                       ends_at=1793529000, priority=2)
    runtime.command("set_program", baseline)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "schedule")
        card = _programs(page).get_by_label("Program editable", exact=True)
        card.get_by_role("button", name="Edit program editable", exact=True).click()
        assert current_hash(page) == "#/schedule/editable/edit/review"
        form = _programs(page).get_by_role("form", name="Edit Program editable", exact=True)
        expect(form.get_by_role("heading", name="Check your Program", exact=True)).to_be_visible()
        expect(form.get_by_role("note")).to_contain_text("Saved start occurrence: 1:30 AM PST (GMT-08:00)")
        expect(form.get_by_label("Create separate windows", exact=True)).to_have_count(0)
        form.get_by_role("button", name="Change Priority", exact=True).click()
        form.get_by_label("Priority", exact=True).fill("8")
        with page.expect_response(lambda r: r.url.endswith("/v1/operator/programs/editable/replace") and r.request.method == "POST") as response:
            form.get_by_role("button", name="Replace Program", exact=True).click()
        request = response.value.request.post_data_json
        assert request == {
            "expected": {"program_id": "editable", "scene_id": "evening", "starts_at": baseline.starts_at,
                         "ends_at": baseline.ends_at, "priority": 2},
            "program": {"program_id": "editable", "scene_id": "evening", "starts_at": baseline.starts_at,
                        "ends_at": baseline.ends_at, "priority": 8},
        }
        expect(_programs(page).get_by_role("status")).to_have_text(
            "Replaced Program editable. Changes apply to future Runs; an active Run keeps its secured assignment.")


def test_continue_checks_only_its_own_step(page, registry):
    """§7 mechanics: Continue validates the current step and shows only its reasons; a
    later step shows none until its own Continue. Mutation probe: check every step."""
    _seed(registry)
    _runtime(registry).command("set_scene", _console_scene("evening"))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        form = start_schedule(page)
        form.get_by_role("button", name="Continue", exact=True).click()
        summary = form.get_by_role("alert")
        expect(summary).to_have_text(re.compile("Choose a Scene."))
        expect(form.get_by_label("Scene", exact=True)).to_be_focused()
        assert current_hash(page) == "#/schedule/new/scene"

        form.get_by_label("Scene", exact=True).select_option("evening")
        schedule_continue(page, "When")
        start = form.get_by_label("Window start", exact=True)
        expect(start).to_have_accessible_description("")
        expect(form.get_by_role("alert")).to_have_count(0)

        form.get_by_role("button", name="Continue", exact=True).click()
        expect(summary).to_contain_text("Enter when the window starts.")
        expect(summary).to_contain_text("Enter when the window ends.")
        expect(summary).not_to_contain_text("Enter a name.")
        expect(start).to_be_focused()
        assert current_hash(page) == "#/schedule/new/when"


def test_review_routes_each_problem_to_its_step_and_its_advanced(page, registry):
    """§7 Review: Save checks everything; a problem on Review focuses its field, opening
    its Advanced (a non-integer priority); a problem on another step's Advanced (the
    number of windows) opens that step and its Advanced from the summary. Nothing is
    sent."""
    _seed(registry)
    _runtime(registry).command("set_scene", _console_scene("evening"))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        puts = []
        page.on("request", lambda r: puts.append(r.url)
                if r.method == "PUT" and "/v1/operator/programs/" in r.url else None)
        form = schedule_program(page, PROGRAM_ID, "evening", WINDOW_START, WINDOW_END,
                                priority="1.5", submit=False)
        advanced = form.get_by_role("button", name="Advanced", exact=True)
        advanced.click()  # closed again: Save must open it
        expect(advanced).to_have_attribute("aria-expanded", "false")
        form.get_by_role("button", name="Schedule Program", exact=True).click()
        expect(form.get_by_role("alert")).to_contain_text("Priority must be a whole number.")
        expect(advanced).to_have_attribute("aria-expanded", "true")
        expect(form.get_by_label("Priority", exact=True)).to_be_focused()
        form.get_by_label("Priority", exact=True).fill("2")

        # A bad number of windows, reached past When by a typed URL.
        form.get_by_role("button", name="Change Number of windows", exact=True).click()
        form.get_by_label("Number of windows", exact=True).fill("2.5")
        visit(page, "#/schedule/new/review")
        form.get_by_role("button", name="Add separate windows", exact=True).click()
        summary = form.get_by_role("alert")
        expect(summary).to_be_focused()
        summary.get_by_role("button", name="Between 1 and 60 windows.", exact=True).click()
        assert current_hash(page) == "#/schedule/new/when"
        count = form.get_by_label("Number of windows", exact=True)
        expect(count).to_be_focused()
        expect(form.get_by_role("button", name="Advanced", exact=True)).to_have_attribute(
            "aria-expanded", "true")
        count.fill("1")
        schedule_continue(page, "Review")  # back to Review: nothing else to fix
        with _put(page) as info:
            form.get_by_role("button", name="Schedule Program", exact=True).click()
        assert info.value.request.post_data_json["priority"] == 2
        assert puts == [info.value.url]


def test_the_draft_survives_a_section_change_and_the_sidebar_says_draft(page, registry):
    """Rule 2: the Schedule flow never unmounts, so a section change keeps its draft; the
    sidebar marks Schedule "Draft" and the page offers "Resume draft (Draft)"."""
    _seed(registry)
    _runtime(registry).command("set_scene", _console_scene("evening"))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        expect(_schedule_link(page)).to_have_accessible_description("")
        form = start_schedule(page)
        form.get_by_label("Scene", exact=True).select_option("evening")
        schedule_continue(page, "When")
        form.get_by_label("Window start", exact=True).fill(WINDOW_START)
        expect(_schedule_link(page)).to_have_accessible_description("Draft")

        go(page, "now")
        go(page, "wall")
        go(page, "schedule")
        resume = _programs(page).get_by_role("button", name="Resume draft (Draft)", exact=True)
        expect(resume).to_be_visible()
        resume.click()
        assert current_hash(page) == "#/schedule/new/when"
        expect(form.get_by_label("Window start", exact=True)).to_have_value(WINDOW_START)
        page.get_by_role("navigation", name="Steps", exact=True).get_by_role(
            "button", name="Scene", exact=True).click()
        expect(form.get_by_label("Scene", exact=True)).to_have_value("evening")


def test_save_lands_on_the_schedule_and_back_never_reenters_the_flow(page, registry):
    """§6 History: Save replaces the flow's entry with #/schedule, which lists the new
    Program's card and says what was scheduled; Back never re-enters the finished flow."""
    _seed(registry)
    _runtime(registry).command("set_scene", _console_scene("evening"))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        schedule_program(page, PROGRAM_ID, "evening", WINDOW_START, WINDOW_END)
        programs = _programs(page)
        expect(programs.get_by_role("status")).to_have_text(f"Scheduled Program {PROGRAM_ID}.")
        assert current_hash(page) == "#/schedule"
        card = programs.get_by_label(f"Program {PROGRAM_ID}", exact=True)
        expect(card).to_contain_text("Scene evening")
        expect(card.get_by_role("button")).to_have_text(["Edit", "Remove"])
        expect(_schedule_link(page)).to_have_accessible_description("")

        page.go_back()  # one Back leaves the section: no second #/schedule entry
        assert not current_hash(page).startswith("#/schedule"), current_hash(page)
        expect(schedule_form(page)).to_have_count(0)
        go(page, "schedule")
        expect(programs.get_by_role("button", name="Schedule a Program", exact=True)
               ).to_be_visible()


def test_a_program_whose_window_ended_while_its_run_finishes_is_not_past(page, registry):
    """A Program's window has ended but its Run is still finishing its cycle (Central asks
    it to finish at the window's end): it is running, so its card stays with the current
    Programs, not under "Past". Mutation probe: file a Program as past by its window's end
    alone."""
    _seed(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", _console_scene("evening", cycle_seconds=600))
    now = registry.clock.utc()
    runtime.command("advance", now)  # Central has ticked: a warm Runtime
    runtime.command("set_program", Program(
        program_id="closing", scene_id="evening", starts_at=now + 10, ends_at=now + 20))
    registry.clock.advance(25)  # the window has ended; the 600 s cycle has not
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "schedule")
        programs = _programs(page)
        card = programs.get_by_label("Program closing", exact=True)
        expect(card).to_be_visible()
        expect(card).to_contain_text("Running since")
        expect(programs.get_by_text(re.compile(r"^Past \("))).to_have_count(0)


def test_a_program_central_did_not_answer_may_have_been_saved_and_again_confirms(
        page, registry):
    """Final review finding 2, the single-window Program: a Schedule Program that got no
    answer (a 5xx after Central stored it) says it may have been saved, is not read as a
    collision with itself while the draft is unchanged, and Schedule Program again (an
    idempotent PUT of the same body) confirms it: one Program. Mutation probe: drop the
    exclusion."""
    _seed(registry)
    _runtime(registry).command("set_scene", _console_scene("evening"))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "schedule")

        def committed_then_500(route):
            route.fetch()
            route.fulfill(status=500, content_type="application/json", body='{"error": "internal"}')

        answer_first(page, "**/v1/operator/programs/**", committed_then_500)
        form = schedule_program(page, PROGRAM_ID, "evening", WINDOW_START, WINDOW_END, submit=False)
        with _put(page):
            form.get_by_role("button", name="Schedule Program", exact=True).click()
        programs = _programs(page)
        expect(programs.get_by_role("status").filter(has_text="may have been saved")).to_have_text(
            f"Program {PROGRAM_ID} may have been saved: Central did not answer. "
            "Schedule Program again to confirm.")
        assert current_hash(page) == "#/schedule/new/review"
        expect(form.get_by_text(re.compile("already exists"))).to_have_count(0)

        with _put(page) as info:
            form.get_by_role("button", name="Schedule Program", exact=True).click()
        assert info.value.status == 200
        expect(programs.get_by_role("status")).to_have_text(f"Scheduled Program {PROGRAM_ID}.")
        assert current_hash(page) == "#/schedule"
        response = page.request.get(origin + "/v1/operator/runtime",
                                    headers={"Authorization": "Bearer " + ADMIN})
        assert sorted(response.json()["programs"]) == [PROGRAM_ID]
