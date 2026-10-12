"""Behavioral checks for the Schedule flow and the Program cards (flow design §7 J6; Power and
the week, A10).

Reuses the operator browser harness (real Chromium against the production create_app, the
disposable-schema `registry` fixture and the autouse `page_errors` guard) and the Showrunner
file's seeding (its house is UTC, the test clock 1 Jan 1970). A Program is a Scene every week
on its days, from a start to an end on the house clock, at a priority: one POST, with Central
naming it; an edit sends the Program it loaded and the draft; the card shows Central's words
for its next window, whatever the browser's time zone.
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
from support.house_time import daily, window_program
from test_operator_showrunner_browser import (
    SOURCE,
    VALID_FRAME,
    _console_scene,
    _runtime,
    _seed,
    _seed_source,
)

from central.runtime import ProgramDraft

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")


def _programs(page):
    return page.get_by_role("region", name="Programs", exact=True)


def _schedule_link(page):
    return page.get_by_role("navigation", name="Sections", exact=True).get_by_role(
        "link", name="Schedule", exact=True)


def _post(page):
    return page.expect_response(
        lambda r: r.url.endswith("/v1/operator/programs") and r.request.method == "POST")


@pytest.mark.browser_context_args(timezone_id="Pacific/Auckland")
def test_a_weekday_schedule_is_one_post_and_its_card_shows_the_house_clock(page, registry):
    """A10: "Evenings, Mon–Fri 17:00–23:00" made in the flow is one POST with a When; the card
    shows "Mon–Fri, 17:00–23:00" and its next window on the house clock (UTC here) while the
    browser runs in Auckland; Edit sends {expected, next}; no count or id field exists.
    Mutation probe (P-browser-zone): format the card's window in the browser's zone -> the
    house-clock text fails."""
    _seed(registry)
    _runtime(registry).command("set_scene", _console_scene("evening"))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        form = schedule_program(page, "Evenings", "evening", "17:00", "23:00", days=WEEKDAYS, submit=False)
        expect(form.get_by_label("Your answers", exact=True)).to_contain_text("Mon–Fri, 17:00–23:00")
        expect(form.get_by_label("Number of windows", exact=True)).to_have_count(0)
        expect(form.get_by_label("Id", exact=True)).to_have_count(0)
        with _post(page) as info:
            form.get_by_role("button", name="Schedule Program", exact=True).click()
        assert info.value.status == 201
        assert info.value.request.post_data_json == {
            "name": "Evenings", "scene_id": "evening", "priority": "normal", "when": {
                "days": ["mon", "tue", "wed", "thu", "fri"],
                "start": {"at": "clock", "time": "17:00"}, "end": {"at": "clock", "time": "23:00"}}}
        stored = info.value.json()

        card = _programs(page).get_by_label("Program Evenings", exact=True)
        expect(card).to_contain_text("Mon–Fri, 17:00–23:00")
        expect(card).to_contain_text("Thu 1 Jan 17:00–23:00")     # Thursday 1 Jan 1970, UTC house
        expect(card).to_contain_text("Normal")

        card.get_by_role("button", name="Edit program Evenings", exact=True).click()
        assert current_hash(page) == f"#/schedule/{stored['program_id']}/edit/review"
        edit = _programs(page).get_by_role("form", name="Edit Program Evenings", exact=True)
        edit.get_by_role("button", name="Change When", exact=True).click()
        edit.get_by_label("Ends", exact=True).fill("22:00")
        schedule_continue_edit(page, edit)
        with page.expect_response(lambda r: r.request.method == "PUT"
                                  and r.url.endswith(f"/v1/operator/programs/{stored['program_id']}")) as saved:
            edit.get_by_role("button", name="Save Program", exact=True).click()
        assert saved.value.status == 200
        request = saved.value.request.post_data_json
        assert request["expected"] == stored
        assert request["next"]["when"]["end"] == {"at": "clock", "time": "22:00"}
        assert saved.value.json()["revision"] == 2
        expect(_programs(page).get_by_role("status")).to_have_text(
            "Saved Program Evenings. It applies from its next window; a window playing now keeps its end.")
        expect(card).to_contain_text("Mon–Fri, 17:00–22:00")


def schedule_continue_edit(page, form):
    """Continue on an edit's step until Review shows."""
    form.get_by_role("button", name="Continue", exact=True).click()
    expect(page.get_by_role("navigation", name="Steps", exact=True).locator(
        "[aria-current=step]")).to_contain_text("Review")


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

        go(page, "scenes")
        scenes.get_by_role("button", name="Schedule Scene evening", exact=True).click()
        assert current_hash(page) == "#/schedule/new/scene"
        expect(scene).to_have_value("evening")

        schedule_continue(page, "When")
        form.get_by_label("Starts", exact=True).fill("18:00")
        go(page, "scenes")
        scenes.get_by_role("button", name="Schedule Scene saved-scene", exact=True).click()
        assert current_hash(page) == "#/schedule/new/scene"
        expect(scene).to_have_value("evening")
        expect(form).to_contain_text("Your unsaved draft schedules Scene evening.")
        form.get_by_role("button", name="Schedule Scene saved-scene instead", exact=True).click()
        expect(scene).to_have_value("saved-scene")
        schedule_continue(page, "When")
        expect(form.get_by_label("Starts", exact=True)).to_have_value("18:00")


def test_priority_defaults_to_normal_shown_on_review_and_changes_under_advanced(page, registry):
    """§7 J6 step 3: Priority's stated default, Normal, is on Review with the other answers,
    and is changed under Review's Advanced. Mutation probe: default the priority to another."""
    _seed(registry)
    _runtime(registry).command("set_scene", _console_scene("evening"))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        form = schedule_program(page, "first", "evening", "09:00", "11:00", submit=False)
        answers = form.get_by_label("Your answers", exact=True)
        expect(answers).to_contain_text(re.compile(r"PriorityNormalChange"))
        advanced = form.get_by_role("button", name="Advanced", exact=True)
        expect(advanced).to_have_attribute("aria-expanded", "false")
        with _post(page) as info:
            form.get_by_role("button", name="Schedule Program", exact=True).click()
        assert info.value.request.post_data_json["priority"] == "normal"

        form = schedule_program(page, "second", "evening", "09:00", "11:00", submit=False)
        form.get_by_role("button", name="Change Priority", exact=True).click()
        priority = form.get_by_label("Priority", exact=True)
        expect(priority).to_be_focused()
        expect(advanced).to_have_attribute("aria-expanded", "true")
        priority.select_option(label="Always wins")
        expect(answers).to_contain_text(re.compile(r"PriorityAlways winsChange"))
        with _post(page) as info:
            form.get_by_role("button", name="Schedule Program", exact=True).click()
        assert info.value.request.post_data_json["priority"] == "always-wins"
        expect(_programs(page).get_by_label("Program second", exact=True)).to_contain_text("Always wins")


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
        start = form.get_by_label("Starts", exact=True)
        expect(start).to_have_accessible_description("")
        expect(form.get_by_role("alert")).to_have_count(0)

        form.get_by_role("button", name="Continue", exact=True).click()
        expect(summary).to_contain_text("Enter when it starts.")
        expect(summary).to_contain_text("Enter when it ends.")
        expect(summary).not_to_contain_text("Enter a name.")
        expect(start).to_be_focused()
        assert current_hash(page) == "#/schedule/new/when"


def test_review_routes_each_problem_to_its_step(page, registry):
    """§7 Review: Save checks everything; a problem on another step (no day ticked) opens that
    step from the summary. Nothing is sent until it is fixed."""
    _seed(registry)
    _runtime(registry).command("set_scene", _console_scene("evening"))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        posts = []
        page.on("request", lambda r: posts.append(r.url)
                if r.method == "POST" and r.url.endswith("/v1/operator/programs") else None)
        form = schedule_program(page, "Show", "evening", "09:00", "11:00", submit=False)
        form.get_by_role("button", name="Change When", exact=True).click()
        for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
            form.get_by_label(day, exact=True).uncheck()
        visit(page, "#/schedule/new/review")      # past When by a typed URL
        form.get_by_role("button", name="Schedule Program", exact=True).click()
        summary = form.get_by_role("alert")
        expect(summary).to_be_focused()
        summary.get_by_role("button", name="Tick at least one day.", exact=True).click()
        assert current_hash(page) == "#/schedule/new/when"
        form.get_by_label("Monday", exact=True).check()
        schedule_continue(page, "Review")
        with _post(page) as info:
            form.get_by_role("button", name="Schedule Program", exact=True).click()
        assert info.value.request.post_data_json["when"]["days"] == ["mon"]
        assert posts == [info.value.url]


def test_a_refusal_central_routes_to_a_field_returns_to_its_step(page, registry):
    """Central's 422 "unknown-scene" (the Scene was deleted meanwhile) returns to the Scene step
    with its reason; nothing is stored."""
    _seed(registry)
    _runtime(registry).command("set_scene", _console_scene("evening"))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        answer_first(page, "**/v1/operator/programs", lambda route: route.fulfill(
            status=422, content_type="application/json", body='{"error": "unknown-scene"}'))
        form = schedule_program(page, "Show", "evening", "09:00", "11:00", submit=False)
        with _post(page):
            form.get_by_role("button", name="Schedule Program", exact=True).click()
        expect(page).to_have_url(re.compile(r"#/schedule/new/scene$"))
        expect(form.get_by_label("Scene", exact=True)).to_have_accessible_description(
            re.compile("That Scene no longer exists"))


def test_an_edit_that_meets_a_newer_save_offers_reload(page, registry):
    """A save whose expected Program is no longer the stored one is refused 409 "changed" with
    the stored Program; the editor says so and Reload reseeds from it, after which Save works."""
    _seed(registry)
    runtime = _runtime(registry)
    runtime.command("set_scene", _console_scene("evening"))
    created = runtime.command_current("create_program", window_program("late-show", "evening", 3600, 7200))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "schedule")
        card = _programs(page).get_by_label("Program late-show", exact=True)
        card.get_by_role("button", name="Edit program late-show", exact=True).click()
        edit = _programs(page).get_by_role("form", name="Edit Program late-show", exact=True)
        # Someone else saves first.
        runtime.command_current("save_program", created, ProgramDraft(
            name="late-show", scene_id="evening", when=daily("01:00", "03:00")))
        with page.expect_response(lambda r: r.request.method == "PUT") as refused:
            edit.get_by_role("button", name="Save Program", exact=True).click()
        assert refused.value.status == 409
        expect(edit).to_contain_text("Program late-show changed since you opened it.")
        edit.get_by_role("button", name="Reload", exact=True).click()
        expect(edit.get_by_label("Your answers", exact=True)).to_contain_text("Every day, 01:00–03:00")
        with page.expect_response(lambda r: r.request.method == "PUT") as saved:
            edit.get_by_role("button", name="Save Program", exact=True).click()
        assert saved.value.status == 200
        assert saved.value.request.post_data_json["expected"]["revision"] == 2


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
        form.get_by_label("Starts", exact=True).fill("18:00")
        expect(_schedule_link(page)).to_have_accessible_description("Draft")

        go(page, "now")
        go(page, "wall")
        go(page, "schedule")
        resume = _programs(page).get_by_role("button", name="Resume draft (Draft)", exact=True)
        expect(resume).to_be_visible()
        resume.click()
        assert current_hash(page) == "#/schedule/new/when"
        expect(form.get_by_label("Starts", exact=True)).to_have_value("18:00")
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
        schedule_program(page, "Morning", "evening", "09:00", "11:00")
        programs = _programs(page)
        expect(programs.get_by_role("status")).to_have_text("Scheduled Program Morning.")
        assert current_hash(page) == "#/schedule"
        card = programs.get_by_label("Program Morning", exact=True)
        expect(card).to_contain_text("Scene evening")
        expect(card.get_by_role("button")).to_have_text(["Edit", "Remove"])
        expect(_schedule_link(page)).to_have_accessible_description("")

        page.go_back()  # one Back leaves the section: no second #/schedule entry
        assert not current_hash(page).startswith("#/schedule"), current_hash(page)
        expect(schedule_form(page)).to_have_count(0)
        go(page, "schedule")
        expect(programs.get_by_role("button", name="Schedule a Program", exact=True)).to_be_visible()


def test_a_program_central_did_not_answer_says_it_may_have_been_saved(page, registry):
    """A Schedule Program that got no answer (a 5xx after Central stored it) says it may have
    been saved and stays on Review; the stored Program lists once the snapshot refreshes."""
    _seed(registry)
    _runtime(registry).command("set_scene", _console_scene("evening"))
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "schedule")

        def committed_then_500(route):
            route.fetch()
            route.fulfill(status=500, content_type="application/json", body='{"error": "internal"}')

        answer_first(page, "**/v1/operator/programs", committed_then_500)
        form = schedule_program(page, "Morning", "evening", "09:00", "11:00", submit=False)
        with _post(page):
            form.get_by_role("button", name="Schedule Program", exact=True).click()
        expect(_programs(page).get_by_role("status").filter(has_text="may have been saved")).to_have_text(
            "Program Morning may have been saved: Central did not answer. "
            "Check the Programs before scheduling it again.")
        assert current_hash(page) == "#/schedule/new/review"
        visit(page, "#/schedule")
        expect(_programs(page).get_by_label("Program Morning", exact=True)).to_be_visible()
