"""Behavioral checks for the Source flow (flow design §7 J5; bead 3).

Reuses the operator browser harness (real Chromium against the production create_app, the
disposable-schema `registry` fixture and the autouse `page_errors` guard) and the Showrunner
file's seeding. The Source tests that were about the single form moved with it to the flow
in test_operator_showrunner_browser.py; these cover what the flow adds: the connection
rule's three cases, Continue scoped to its step, Review's problem routing, the draft
surviving a section change, Save returning to the cards for good, and the Scene flow's
"New selection from your photo library" running the Source flow inline and returning.
"""

import os

import pytest
from console_tasks import (
    add_source,
    connect,
    go,
    scene_form,
    source_continue,
    source_form,
    start_scene,
    start_source,
    visit,
)
from operator_harness import operator_server, submit_sign_in
from playwright.sync_api import expect
from test_operator_showrunner_browser import (
    SOURCE,
    _seed,
    _seed_source,
)

from central.media_repository import MediaRepository
from media.models import SourceSpec

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

NEW_SOURCE = "spring:1"


def _hash(page):
    return page.evaluate("window.location.hash")


def _sources(page):
    return page.get_by_role("region", name="Sources", exact=True)


def _link(page, label):
    return page.get_by_role("navigation", name="Sections", exact=True).get_by_role(
        "link", name=label, exact=True)


def _connection(page):
    return source_form(page).get_by_label("Connection name", exact=True)


def _advanced(page):
    return source_form(page).get_by_role("button", name="Advanced", exact=True)


def _to_name_step(page):
    start_source(page)
    source_continue(page, "Name")


# --- The connection rule (§7 J5, step 2).


def test_with_no_source_the_connection_is_a_visible_required_field(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        _to_name_step(page)
        connection = _connection(page)
        expect(connection).to_be_visible()
        expect(connection).to_have_value("")
        assert connection.evaluate("(element) => element.tagName") == "INPUT"
        expect(_advanced(page)).to_have_count(0)

        source_form(page).get_by_label("Source name and revision", exact=True).fill(NEW_SOURCE)
        source_continue(page)
        expect(source_form(page).get_by_role("alert")).to_contain_text(
            "Connection name is required.")
        expect(connection).to_be_focused()
        assert _hash(page) == "#/sources/new/name"


def test_one_connection_among_the_sources_is_prefilled_under_advanced(page, registry):
    _seed(registry)
    queue = _seed_source(registry)  # SOURCE, on "fixture-library"
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        _to_name_step(page)
        connection = _connection(page)
        expect(connection).to_be_hidden()
        expect(_advanced(page)).to_have_attribute("aria-expanded", "false")
        expect(_advanced(page)).to_have_accessible_description("Connection: fixture-library")
        _advanced(page).click()
        expect(connection).to_be_visible()
        expect(connection).to_have_value("fixture-library")

        # Saved without typing it: Review lists it, and the body carries it.
        source_form(page).get_by_label("Source name and revision", exact=True).fill(NEW_SOURCE)
        source_continue(page, "Review")
        answers = source_form(page).get_by_role("definition").filter(has_text="fixture-library")
        expect(answers).to_have_count(1)
        with page.expect_response(lambda r: r.request.method == "PUT"
                                  and "/v1/operator/sources/" in r.url) as info:
            source_form(page).get_by_role("button", name="Save source", exact=True).click()
        assert info.value.status == 200
        assert info.value.request.post_data_json["connection_ref"] == "fixture-library"


def test_several_connections_give_a_visible_chooser_with_none_chosen(page, registry):
    _seed(registry)
    queue = _seed_source(registry)  # SOURCE, on "fixture-library"
    MediaRepository(registry.db, registry.clock, queue=queue).configure_source(
        SourceSpec(source_ref="garden:1", connection_ref="second-library"))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        _to_name_step(page)
        chooser = source_form(page).get_by_role("combobox", name="Connection name", exact=True)
        expect(chooser).to_be_visible()
        expect(chooser).to_have_value("")
        expect(chooser.get_by_role("option")).to_have_text(
            ["Choose a connection", "fixture-library", "second-library", "Another connection…"])
        expect(_advanced(page)).to_have_count(0)

        source_form(page).get_by_label("Source name and revision", exact=True).fill(NEW_SOURCE)
        source_continue(page)
        expect(source_form(page).get_by_role("alert")).to_contain_text(
            "Connection name is required.")
        chooser.select_option("second-library")
        source_continue(page, "Review")


# --- Steps, problems and the draft.


def test_continue_checks_only_its_own_step(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        form = start_source(page)
        form.get_by_label("Taken from", exact=True).fill("2024-01-01")
        until = form.get_by_label("Taken until", exact=True)
        until.fill("2023-06-01")
        source_continue(page)
        # Only What to include's reason: the Name step's empty fields say nothing yet.
        summary = form.get_by_role("alert")
        expect(summary.get_by_role("listitem")).to_have_text(
            ["'Taken until' must be after 'Taken from'."])
        expect(until).to_be_focused()
        assert _hash(page) == "#/sources/new/include"

        until.fill("2025-01-01")
        source_continue(page, "Name")
        expect(form.get_by_role("alert")).to_have_count(0)
        ref = form.get_by_label("Source name and revision", exact=True)
        expect(ref).not_to_have_attribute("aria-invalid", "true")
        expect(_connection(page)).not_to_have_attribute("aria-invalid", "true")


def test_a_review_problem_opens_its_step_and_focuses_the_field(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "sources")
        visit(page, "#/sources/new/review")  # a typed URL skips the steps
        form = source_form(page)
        save = form.get_by_role("button", name="Save source", exact=True)
        expect(save).to_be_visible()
        save.click()
        summary = form.get_by_role("alert")
        expect(summary).to_be_focused()
        problem = summary.get_by_role("button").filter(has_text="Name and revision")
        problem.click()
        assert _hash(page) == "#/sources/new/name"
        expect(form.get_by_label("Source name and revision", exact=True)).to_be_focused()

        # A value under Advanced: its Change link opens Advanced on its step first.
        form.get_by_label("Source name and revision", exact=True).fill(NEW_SOURCE)
        source_continue(page, "Review")
        form.get_by_role("button", name="Change Connection name", exact=True).click()
        assert _hash(page) == "#/sources/new/name"
        expect(_advanced(page)).to_have_attribute("aria-expanded", "true")
        expect(_connection(page)).to_be_focused()


def test_the_draft_survives_a_section_change(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        form = start_source(page)
        form.get_by_label("Media type", exact=True).select_option("video")
        source_continue(page, "Name")
        form.get_by_label("Source name and revision", exact=True).fill("kept:1")
        expect(_link(page, "Photo sources")).to_have_accessible_description("Draft")

        go(page, "wall")
        go(page, "sources")
        assert _hash(page) == "#/sources"
        _sources(page).get_by_role("button", name="Resume draft (Draft)", exact=True).click()
        assert _hash(page) == "#/sources/new/name"
        expect(form.get_by_label("Source name and revision", exact=True)).to_have_value("kept:1")
        form.get_by_role("button", name="Back", exact=True).click()
        expect(form.get_by_label("Media type", exact=True)).to_have_value("video")


def test_saving_returns_to_the_cards_and_back_never_reenters(page, registry):
    _seed(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin)
        response = add_source(page, NEW_SOURCE, "fixture-library")
        assert response.status == 200
        assert _hash(page) == "#/sources"
        sources = _sources(page)
        expect(sources.get_by_role("status")).to_have_text(f"Saved Source {NEW_SOURCE}.")
        card = sources.get_by_role("article", name=NEW_SOURCE, exact=True)
        expect(card).to_be_visible()
        expect(card.get_by_role("button", name=f"Refresh {NEW_SOURCE}", exact=True)).to_be_visible()
        expect(_link(page, "Photo sources")).to_have_accessible_description("")

        page.go_back()
        assert not _hash(page).startswith("#/sources/new")
        expect(source_form(page)).to_have_count(0)


# --- Inline from the Scene flow (§7 J4 step 2).

NEW_SELECTION = "New selection from your photo library"
FOR_SCENE = ("This photo source is for your Scene. Saving it takes you back there, "
             "with it chosen.")


def test_a_new_selection_runs_inline_and_returns_with_the_new_source_chosen(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        scene = start_scene(page)
        scene.get_by_role("button", name=NEW_SELECTION, exact=True).click()

        assert _hash(page) == "#/sources/new/include"
        expect(_sources(page).get_by_text(FOR_SCENE, exact=True)).to_be_visible()
        form = source_form(page)
        expect(form.get_by_role("heading", name="What to include", exact=True)).to_be_focused()
        form.get_by_label("Media type", exact=True).select_option("image")
        source_continue(page, "Name")
        form.get_by_label("Source name and revision", exact=True).fill(NEW_SOURCE)
        source_continue(page, "Review")
        with page.expect_response(lambda r: r.request.method == "PUT"
                                  and "/v1/operator/sources/" in r.url) as info:
            form.get_by_role("button", name="Save source", exact=True).click()
        assert info.value.status == 200

        # Back on the Scene's Photos step, the new Source chosen and focused.
        expect(page.get_by_role("heading", level=1, name="Scenes", exact=True)).to_be_visible()
        assert _hash(page) == "#/scenes/new/photos"
        picker = scene_form(page).get_by_label("Source", exact=True)
        expect(picker).to_have_value(NEW_SOURCE)
        expect(picker).to_be_focused()
        expect(_link(page, "Photo sources")).to_have_accessible_description("")
        expect(_link(page, "Scenes")).to_have_accessible_description("Draft")

        # The hand-off ended: the Sources page no longer says whom it is for.
        go(page, "sources")
        expect(_sources(page).get_by_text(FOR_SCENE, exact=True)).to_have_count(0)
        expect(_sources(page).get_by_role("article", name=NEW_SOURCE, exact=True)).to_be_visible()


def test_back_or_discard_in_the_inline_flow_returns_to_the_scene_unchanged(page, registry):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        scene = start_scene(page)
        picker = scene.get_by_label("Source", exact=True)
        picker.select_option(SOURCE)

        # Back on the first step returns, keeping the Scene's choice.
        scene.get_by_role("button", name=NEW_SELECTION, exact=True).click()
        assert _hash(page) == "#/sources/new/include"
        source_form(page).get_by_role("button", name="Back", exact=True).click()
        assert _hash(page) == "#/scenes/new/photos"
        expect(picker).to_have_value(SOURCE)
        expect(picker).to_be_focused()

        # Discard and return: the Source draft is gone, the Scene is as it was.
        scene.get_by_role("button", name=NEW_SELECTION, exact=True).click()
        assert _hash(page) == "#/sources/new/include"
        source_form(page).get_by_label("Media type", exact=True).select_option("video")
        expect(_link(page, "Photo sources")).to_have_accessible_description("Draft")
        _sources(page).get_by_role(
            "button", name="Discard and return to your Scene", exact=True).click()
        dialog = page.get_by_role("dialog", name="Discard your unsaved draft?")
        dialog.get_by_role("button", name="Discard draft", exact=True).click()
        expect(dialog).to_have_count(0)
        expect(page.get_by_role("heading", level=1, name="Scenes", exact=True)).to_be_visible()
        assert _hash(page) == "#/scenes/new/photos"
        expect(picker).to_have_value(SOURCE)
        expect(picker).to_be_focused()
        expect(_link(page, "Photo sources")).to_have_accessible_description("")


def _discard_scene_draft(page):
    """On Scenes: "Discard draft", confirmed."""
    go(page, "scenes")
    page.get_by_role("region", name="Scenes", exact=True).get_by_role(
        "button", name="Discard draft", exact=True).click()
    dialog = page.get_by_role("dialog", name="Discard your unsaved draft?")
    dialog.get_by_role("button", name="Discard draft", exact=True).click()
    expect(dialog).to_have_count(0)


def test_a_hand_off_ends_with_the_scene_draft_it_was_begun_for(page, registry):
    """The hand-off belongs to the draft it was begun for (its identity), not to its key:
    Scene A hands off, is discarded, and a new Scene B (keyed "new" too) is started. The
    Source flow no longer says it is for a Scene, and a Source saved there stays on
    Photo sources and never replaces B's Source. Mutation probe: settle the hand-off only
    on its own write (no settle when its Scene draft closes)."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        scene = start_scene(page)
        scene.get_by_label("Source", exact=True).select_option(SOURCE)  # draft A, dirty
        scene.get_by_role("button", name=NEW_SELECTION, exact=True).click()
        assert _hash(page) == "#/sources/new/include"
        expect(_sources(page).get_by_text(FOR_SCENE, exact=True)).to_be_visible()

        _discard_scene_draft(page)
        scene = start_scene(page)  # draft B
        picker = scene.get_by_label("Source", exact=True)
        picker.select_option(SOURCE)

        go(page, "sources")
        expect(_sources(page).get_by_text(FOR_SCENE, exact=True)).to_have_count(0)
        expect(_sources(page).get_by_role(
            "button", name="Discard and return to your Scene", exact=True)).to_have_count(0)
        response = add_source(page, "stale:1", "fixture-library")
        assert response.status == 200
        assert _hash(page) == "#/sources"
        expect(page.get_by_role("heading", level=1, name="Photo sources", exact=True)
               ).to_be_visible()
        expect(_sources(page).get_by_role("article", name="stale:1", exact=True)).to_be_visible()

        # B is as the operator left it.
        go(page, "scenes")
        page.get_by_role("region", name="Scenes", exact=True).get_by_role(
            "button", name="Resume draft (Draft)", exact=True).click()
        assert _hash(page) == "#/scenes/new/photos"
        expect(picker).to_have_value(SOURCE)


def test_log_out_mid_hand_off_drops_it(page, registry):
    """Log out remounts the shell: the Scene draft, the Source draft and the hand-off
    between them are gone, so after signing in again the Source flow runs on its own."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        scene = start_scene(page)
        scene.get_by_label("Source", exact=True).select_option(SOURCE)
        scene.get_by_role("button", name=NEW_SELECTION, exact=True).click()
        source_form(page).get_by_label("Media type", exact=True).select_option("video")
        expect(_sources(page).get_by_text(FOR_SCENE, exact=True)).to_be_visible()

        page.get_by_role("button", name="Log out", exact=True).click()
        submit_sign_in(page)  # on the screen Log out left, without a reload
        go(page, "sources")
        expect(_sources(page).get_by_text(FOR_SCENE, exact=True)).to_have_count(0)
        expect(_sources(page).get_by_role("button", name="New source", exact=True)).to_be_visible()
        response = add_source(page, NEW_SOURCE, "fixture-library")
        assert response.status == 200
        assert _hash(page) == "#/sources"
        go(page, "scenes")
        expect(page.get_by_role("region", name="Scenes", exact=True).get_by_role(
            "button", name="New Scene", exact=True)).to_be_visible()


def test_with_several_connections_another_one_can_be_typed(page, registry):
    """The chooser (several connections) also offers "Another connection…", which shows a
    field for a connection no Source uses yet, so a Source can still be added on a new
    one. Mutation probe: offer only the served connections."""
    _seed(registry)
    queue = _seed_source(registry)  # SOURCE, on "fixture-library"
    MediaRepository(registry.db, registry.clock, queue=queue).configure_source(
        SourceSpec(source_ref="garden:1", connection_ref="second-library"))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin)
        _to_name_step(page)
        form = source_form(page)
        chooser = form.get_by_role("combobox", name="Connection name", exact=True)
        other = form.get_by_label("New connection name", exact=True)
        expect(other).to_have_count(0)
        chooser.select_option(label="Another connection…")
        expect(other).to_be_visible()
        expect(other).to_have_value("")

        form.get_by_label("Source name and revision", exact=True).fill(NEW_SOURCE)
        source_continue(page)
        expect(form.get_by_role("alert")).to_contain_text("Connection name is required.")
        expect(other).to_be_focused()
        other.fill("third-library")
        source_continue(page, "Review")
        expect(form.get_by_label("Your answers", exact=True)).to_contain_text("third-library")
        # Change goes back to the field it was typed in.
        form.get_by_role("button", name="Change Connection name", exact=True).click()
        expect(other).to_be_focused()
        expect(other).to_have_value("third-library")
        source_continue(page, "Review")
        with page.expect_response(lambda r: r.request.method == "PUT"
                                  and "/v1/operator/sources/" in r.url) as info:
            form.get_by_role("button", name="Save source", exact=True).click()
        assert info.value.status == 200
        assert info.value.request.post_data_json["connection_ref"] == "third-library"
        expect(_sources(page).get_by_role("article", name=NEW_SOURCE, exact=True)).to_contain_text(
            "third-library")
