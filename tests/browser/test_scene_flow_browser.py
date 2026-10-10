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
    current_hash,
    go,
    open_frame,
    scene_continue,
    scene_form,
    start_scene,
    visit,
)
from operator_harness import (
    SNAPSHOT,
    RequestGate,
    answer_first,
    drive_poll,
    operator_server,
    submit_sign_in,
)
from playwright.sync_api import expect
from psycopg.types.json import Jsonb
from test_operator_showrunner_browser import (
    INVALID_FRAME,
    LOBBY_FRAME,
    SOURCE,
    VALID_FRAME,
    _add_lobby_frame,
    _authored_photos,
    _console_scene,
    _runtime,
    _seed,
    _seed_source,
    _set_source,
)
from test_registry import ADMIN

from central.catalog import CatalogSnapshot
from central.db import ProcessTransactionClock
from central.media_repository import MediaRepository
from media.models import RefreshResult

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)

NARROW = {"width": 390, "height": 844}



def _scenes(page):
    return page.get_by_role("region", name="Scenes", exact=True)


def _steps(page):
    return page.get_by_role("navigation", name="Steps", exact=True)


@pytest.mark.parametrize(
    ("state", "setup", "expected", "consequence"),
    [
        ("awaiting", lambda registry, now: None, "Awaiting refresh", "No catalog has been loaded yet"),
        ("failed", lambda registry, now: _set_source(
            registry, SOURCE, status="unavailable", next_refresh=now + 30,
            refresh_completed_revision=0, refresh_requested_revision=1,
            diagnostics=[{"code": "upstream_unavailable"}],
        ), "Your photo library is unreachable", "last refresh failed"),
        ("empty", lambda registry, now: _set_source(
            registry, SOURCE, status="ok", next_refresh=now + 30, last_success=now,
            counts={"valid": 0, "discovered": 0, "pending": 0, "rejected": 0},
        ), "nothing valid in the last refresh", "found no valid items"),
    ],
)
def test_scene_photos_and_review_explain_source_readiness(
    page, registry, state, setup, expected, consequence,
):
    _seed(registry)
    _seed_source(registry)
    now = registry.clock.utc()
    setup(registry, now)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "scenes")
        form = start_scene(page)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        readiness = form.get_by_role("region", name="Source media status")
        expect(readiness).to_contain_text(expected)
        expect(readiness).to_contain_text(consequence)
        scene_continue(page, "Frames")
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        scene_continue(page, "Playback")
        scene_continue(page, "Review")
        readiness = form.get_by_role("region", name="Source media status")
        expect(readiness).to_contain_text(expected)
        expect(readiness).to_contain_text(consequence)


def test_refresh_source_from_scene_photos_preserves_draft_and_reports_request(
    page, registry,
):
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = start_scene(page)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        readiness = form.get_by_role("region", name="Source media status")
        expect(readiness).to_contain_text("Awaiting refresh")
        with page.expect_response(
            lambda response: response.url.endswith("/refresh")
            and response.request.method == "POST"
        ) as info:
            readiness.get_by_role("button", name="Refresh Source", exact=True).click()
        assert info.value.status == 202
        expect(readiness).to_contain_text(
            "Refresh requested. The status will update when the media worker finishes."
        )
        assert info.value.json()["requested_revision"] == 1
        scene_continue(page, "Frames")
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        scene_continue(page, "Playback")
        scene_continue(page, "Review")
        expect(form.get_by_label("Your answers", exact=True)).to_contain_text("holiday")
        form.get_by_label("Scene name", exact=True).fill("source-refresh-keeps-draft")
        expect(form.get_by_label("Scene name", exact=True)).to_have_value(
            "source-refresh-keeps-draft"
        )
        expect(form.get_by_role("region", name="Source media status")).to_contain_text(
            "Refresh requested."
        )
        form.get_by_role("region", name="Source media status").get_by_role(
            "button", name="Manage in Sources", exact=True
        ).click()
        expect(page.get_by_role("heading", level=1, name="Sources", exact=True)).to_be_visible()
        expect(page.get_by_role("region", name="Sources", exact=True)).to_contain_text("holiday")
        go(page, "scenes")
        _scenes(page).get_by_role("button", name="Resume draft (Draft)", exact=True).click()
        assert current_hash(page) == "#/scenes/new/review"
        expect(form.get_by_label("Scene name", exact=True)).to_have_value(
            "source-refresh-keeps-draft"
        )
        answers = form.get_by_label("Your answers", exact=True)
        expect(answers).to_contain_text("holiday")
        expect(answers).to_contain_text(VALID_FRAME)


def test_completed_source_refresh_reloads_authored_candidates_once(page, registry):
    _seed(registry)
    first, second, incompatible = _authored_photos(registry)
    queue = _seed_source(registry, (first,))
    _set_source(registry, SOURCE, next_refresh=registry.clock.utc() - 1000)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        candidate_responses = []
        page.on("response", lambda response: candidate_responses.append(response)
                if "/candidates?" in response.url else None)
        form = start_scene(page, hand_picked=True)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        scene_continue(page, "Media per frame")
        chooser = form.get_by_label(f"Media for frame {VALID_FRAME}", exact=True)
        expect(chooser.locator("option")).to_have_count(2)
        selected_before = chooser.locator("option").nth(1).get_attribute("value")
        chooser.select_option(selected_before)
        assert len(candidate_responses) == 1

        _steps(page).get_by_role("button", name="Photos", exact=True).click()
        readiness = form.get_by_role("region", name="Source media status")
        with page.expect_response(
            lambda response: response.url.endswith("/refresh")
            and response.request.method == "POST"
        ) as response:
            readiness.get_by_role("button", name="Refresh Source", exact=True).click()
        assert response.value.status == 202
        requested_revision = response.value.json()["requested_revision"]

        repository = MediaRepository(registry.db, registry.clock, queue=queue, times=ProcessTransactionClock(registry.clock))
        lease = repository.begin_requested_refresh(SOURCE)
        assert lease is not None and lease.request_revision == requested_revision
        assert repository.publish_refresh(lease, RefreshResult(
            snapshot=CatalogSnapshot(
                source_ref=SOURCE,
                refreshed_at=registry.clock.utc(),
                candidates=tuple(photo.asset.candidate for photo in (first, second, incompatible)),
            ),
            assets=tuple(photo.asset for photo in (first, second, incompatible)),
        ))

        # The source revision is applied by the snapshot poll; useCandidates then
        # schedules its reload in a React effect. Waiting only for the refresh
        # message can observe the new source revision before that request reaches
        # the browser, so synchronize on the candidate response itself.
        with page.expect_response(
            lambda item: "/candidates?" in item.url
            and f"frame_id={VALID_FRAME}" in item.url
        ) as refreshed_candidates:
            drive_poll(page)
        expect(readiness).to_contain_text("Refresh finished.")
        assert refreshed_candidates.value.status == 200
        assert len(candidate_responses) == 2, [
            (item.status, item.url, item.text()) for item in candidate_responses
        ]
        scene_continue(page, "Frames")
        scene_continue(page, "Media per frame")
        expect(chooser.locator("option")).to_have_count(3)
        expect(chooser).to_have_value(selected_before)
        labels = chooser.locator("option").all_inner_texts()
        assert any("Photo 120×200" in label for label in labels), labels
        assert not any("Photo 192×108" in label for label in labels), labels
        assert len(candidate_responses) == 2

        drive_poll(page)
        assert len(candidate_responses) == 2


def test_commissioned_frame_opens_a_scene_with_an_explicit_editable_target(page, registry):
    _seed(registry)
    _seed_source(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "wall")
        inspector = open_frame(page, VALID_FRAME, "calibration")
        inspector.get_by_role("link", name="Choose content for this Frame", exact=True).click()
        assert current_hash(page) == f"#/scenes/new/kind?target={VALID_FRAME}"
        form = scene_form(page)
        expect(form.get_by_role("heading", name="What kind of Scene?", exact=True)).to_be_visible()
        scene_continue(page, "Photos")
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")
        chosen = form.get_by_label(f"Target frame {VALID_FRAME}", exact=True)
        other = form.get_by_label(f"Target frame {INVALID_FRAME}", exact=True)
        expect(chosen).to_be_checked()
        expect(other).not_to_be_checked()
        chosen.uncheck()
        expect(chosen).not_to_be_checked()
        chosen.check()
        other.check()
        other.uncheck()
        scene_continue(page, "Playback")
        scene_continue(page, "Review")
        answers = form.get_by_label("Your answers", exact=True)
        expect(answers).to_contain_text(VALID_FRAME)
        expect(answers).not_to_contain_text(INVALID_FRAME)
        form.get_by_label("Scene name", exact=True).fill("commissioned-content")
        with page.expect_response(lambda response: response.request.method == "PUT"
                                  and response.url.endswith("/v1/operator/scenes/commissioned-content")):
            form.get_by_role("button", name="Save Scene", exact=True).click()
        next_actions = _scenes(page).get_by_role("group", name="Next for Scene commissioned-content")
        expect(next_actions).to_be_visible()
        next_actions.get_by_role("button", name="Show now", exact=True).click()
        assert current_hash(page) == "#/now/show/scene"


def test_commissioning_link_keeps_an_existing_dirty_scene_draft(page, registry):
    _seed(registry)
    _seed_source(registry)
    with operator_server(registry.db, registry.clock) as origin:
        connect(page, origin, "scenes")
        form = start_scene(page)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        go(page, "wall")
        inspector = open_frame(page, VALID_FRAME, "calibration")
        inspector.get_by_role("link", name="Choose content for this Frame", exact=True).click()
        expect(form.get_by_role("status")).to_contain_text(
            f"Your open Scene draft was kept. Frame {VALID_FRAME} was not added.")
        form.get_by_role("button", name="Choose Frames", exact=True).click()
        expect(form.get_by_label(f"Target frame {VALID_FRAME}", exact=True)).not_to_be_checked()
        _steps(page).get_by_role("button", name="Photos", exact=True).click()
        expect(form.get_by_label("Source", exact=True)).to_have_value(SOURCE)


def _scenes_link(page):
    return page.get_by_role("navigation", name="Sections", exact=True).get_by_role(
        "link", name="Scenes", exact=True)




def _put_scene(page, origin, scene):
    """Another operator saves `scene` through the public route."""
    response = page.request.put(origin + f"/v1/operator/scenes/{scene.scene_id}",
                                headers={"Authorization": "Bearer " + ADMIN},
                                data=scene.model_dump(mode="json"))
    assert response.status == 200, response.text()


def _restore_scene(registry, scene):
    """Central's stored Scene replaced outright, revision and all, as restoring an older
    backup would (nothing the console or the API can do: a save only moves forward)."""
    with registry.db.transaction() as conn:
        row = conn.execute("SELECT snapshot FROM runtime_state WHERE singleton").fetchone()
        snapshot = row["snapshot"]
        snapshot["scenes"][scene.scene_id] = scene.model_dump(mode="json")
        conn.execute("UPDATE runtime_state SET snapshot=%s, revision=revision+1 WHERE singleton",
                     (Jsonb(snapshot),))


def _drop_scene(registry, scene_id):
    """Central's stored Scene gone, as restoring a backup without it would."""
    with registry.db.transaction() as conn:
        row = conn.execute("SELECT snapshot FROM runtime_state WHERE singleton").fetchone()
        snapshot = row["snapshot"]
        del snapshot["scenes"][scene_id]
        conn.execute("UPDATE runtime_state SET snapshot=%s, revision=revision+1 WHERE singleton",
                     (Jsonb(snapshot),))


def _focused_heading(page):
    """The text of the focused element when it is a flow step heading, else None."""
    return page.evaluate(
        "() => document.activeElement?.matches('[data-flow-step-heading]')"
        " ? document.activeElement.textContent : null")


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
        assert current_hash(page) == "#/scenes/new/kind"
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
        assert current_hash(page) == "#/scenes/new/photos"


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
        assert current_hash(page) == "#/scenes/new/frames"
        assert page.evaluate("history.length") == before + 1
        expect(_scenes_link(page)).to_have_accessible_description("Draft")
        expect(_scenes_link(page)).to_contain_text("Draft")

        page.go_back()
        expect(_scenes(page).get_by_role("button", name="Resume draft (Draft)", exact=True)
               ).to_be_visible()
        assert current_hash(page) == "#/scenes"
        expect(_scenes_link(page)).to_have_accessible_description("Draft")

        _scenes(page).get_by_role("button", name="Resume draft (Draft)", exact=True).click()
        assert current_hash(page) == "#/scenes/new/frames"
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
        assert current_hash(page) == "#/scenes/new/review"
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
        # ...so none of them is ticked as answered: each keeps its number.
        expect(_steps(page).get_by_role("listitem")).to_have_text(
            ["1Kind", "2Photos", "3Frames", "4Playback", "5Review"])
        form.get_by_label("Scene name", exact=True).fill("Routed")
        form.get_by_role("button", name="Save Scene", exact=True).click()
        summary = form.get_by_role("alert")
        expect(summary.get_by_role("button")).to_have_text(
            ["Choose a Source.", "Choose at least one frame."])

        summary.get_by_role("button", name="Choose a Source.", exact=True).click()
        assert current_hash(page) == "#/scenes/new/photos"
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
        expect(form).to_contain_text("Editing evening. Its name stays the same.")
        # The stored Scene answers every step, so each is ticked.
        expect(_steps(page).get_by_role("listitem")).to_have_text(
            ["Kind", "Photos", "Frames", "Playback", "5Review"])
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
            "This Scene changed since you opened it.")
        expect(replace).to_be_disabled()
        form.evaluate("(element) => element.requestSubmit()")  # Enter, in effect
        page.wait_for_timeout(200)
        # Neither sent nor asked: no Replace confirmation opens while stale.
        expect(page.get_by_role("dialog")).to_have_count(0)
        assert puts == []

        _scenes(page).get_by_role("button", name="Reload", exact=True).click()
        expect(_scenes(page).get_by_role("status").filter(has_text="Reloaded")).to_have_text(
            "Reloaded the latest saved Scene. Changed: Seconds per cycle.")
        expect(_scenes(page).get_by_text(re.compile("was changed"))).to_have_count(0)
        expect(form).to_contain_text("Editing evening. Its name stays the same.")
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

        response = page.request.delete(
            f"{origin}/v1/operator/frames/{LOBBY_FRAME}",
            headers={"Authorization": f"Bearer {ADMIN}"},
        )
        assert response.status == 200
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
        assert current_hash(page) == "#/scenes/new/photos"
        expect(form.get_by_label("Source", exact=True)).to_have_value(SOURCE)
        visit(page, "#/scenes/evening/edit/review")
        _scenes(page).get_by_role("button", name="Discard", exact=True).click()
        expect(form).to_contain_text("Editing evening. Its name stays the same.")

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
        expect(form).to_contain_text("Editing morning. Its name stays the same.")
        assert current_hash(page) == "#/scenes/morning/edit/review"
        # The discard is said, and opening the next instance does not silence it.
        expect(_scenes(page).get_by_role("status").filter(has_text="Discarded")).to_have_text(
            "Discarded the draft for Scene evening.")
        expect(form.get_by_role("heading", name="Check your Scene", exact=True)).to_be_focused()

        visit(page, "#/scenes/ghost/edit/review")
        expect(_scenes(page).get_by_text("Scene ghost: This Scene no longer exists.")
               ).to_be_visible()
        # A typed id that is no id at all is never echoed (routes.js `routeIdName`).
        visit(page, "#/scenes/Call%20555%20now!/edit/review")
        expect(_scenes(page).get_by_text("An unknown Scene: This Scene no longer exists.")
               ).to_be_visible()
        expect(_scenes(page).get_by_text(re.compile("Call 555"))).to_have_count(0)


def test_saving_returns_to_the_cards_and_offers_show_now_and_schedule_it(page, registry):
    """§6 history: Save returns the flow's entry to #/scenes, so Back never re-enters the
    finished flow, and one Back leaves the section (no second #/scenes entry); the next
    actions are Show now and Schedule it. Mutation probe: replace the flow's entry with
    the section again (Back then shows #/scenes twice)."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        author_scene(page, "saved-scene", SOURCE, (VALID_FRAME,))
        assert current_hash(page) == "#/scenes"
        scenes = _scenes(page)
        expect(scenes.get_by_role("status")).to_have_text("Saved Scene saved-scene.")
        next_actions = scenes.get_by_role("group", name="Next for Scene saved-scene")
        expect(next_actions.get_by_role("button")).to_have_text(["Show now", "Schedule it"])
        card = scenes.get_by_label("Scene saved-scene", exact=True)
        expect(card.get_by_role("button")).to_have_text(["Edit", "Show now", "Schedule it", "Delete"])

        page.go_back()
        assert not current_hash(page).startswith("#/scenes"), current_hash(page)
        go(page, "scenes")
        next_actions.get_by_role("button", name="Schedule it", exact=True).click()
        expect(page.get_by_role("heading", level=1, name="Schedule", exact=True)).to_be_visible()
        # Bead 4: "Schedule it" opens the Schedule flow at its Scene step, prefilled.
        assert current_hash(page) == "#/schedule/new/scene"
        expect(page.get_by_role("region", name="Programs", exact=True).get_by_label(
            "Scene", exact=True)).to_have_value("saved-scene")



# A loaded main thread (CI run 36954340001): the flow's `history.back()` returns and the
# page stays busy, so the traversal moves the location before React renders the
# finished flow, and that traversal's `hashchange` is queued behind the render.
_BUSY_AFTER_HISTORY_BACK = """
(() => {
  const back = history.back.bind(history);
  history.back = () => {
    back();
    const until = performance.now() + 300;
    while (performance.now() < until) {}
  };
})();
"""


def test_a_save_whose_return_lands_before_its_render_still_offers_the_next_actions(
        page, registry):
    """§6 history under load: the rendered route follows the location's `popstate`, so when
    Save's return to #/scenes lands before the finished flow renders, no render sees the
    flow's route with its draft closed and opens a fresh draft (which would clear the
    just-saved Scene and its next actions). Mutation probe: listen to `hashchange` only in
    useRoute.js."""
    page.add_init_script(_BUSY_AFTER_HISTORY_BACK)
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        author_scene(page, "saved-scene", SOURCE, (VALID_FRAME,))
        scenes = _scenes(page)
        expect(scenes.get_by_role("group", name="Next for Scene saved-scene").get_by_role(
            "button")).to_have_text(["Show now", "Schedule it"])
        assert current_hash(page) == "#/scenes"
        expect(_scenes_link(page)).to_have_accessible_description("")


# --- Review fixes (bead 2 review).


def test_a_save_that_lands_after_the_operator_left_keeps_them_where_they_went(page, registry):
    """§6 History, review finding 1: Save replaces the flow's entry with #/scenes only if
    the location still names the flow when the write lands. The operator who went to Now
    showing meanwhile stays there, and Back onto the finished flow's entry shows the cards,
    not a fresh draft. While the write is in flight the step is read-only. Mutation probe:
    navigate in finish without reading the location."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = author_scene(page, "inflight", SOURCE, (VALID_FRAME,), submit=False)
        gate = RequestGate(page, "**/v1/operator/scenes/inflight")
        gate.holding = True
        form.get_by_role("button", name="Save Scene", exact=True).click()
        gate.wait_held()
        # Read-only in flight: the fields, Back, Change and the stepper take no edit.
        expect(form.get_by_label("Scene name", exact=True)).to_be_disabled()
        expect(form.get_by_role("button", name="Back", exact=True)).to_be_disabled()
        expect(form.get_by_role("button", name="Change Photos", exact=True)).to_be_disabled()
        expect(_steps(page).get_by_role("button")).to_have_count(0)

        go(page, "now")
        assert current_hash(page) == "#/now"
        with page.expect_response(lambda r: r.url.endswith("/v1/operator/scenes/inflight")
                                  and r.request.method == "PUT") as info:
            gate.release()
        assert info.value.status == 200
        # The draft ended: the write's `finish` has run, and left the location alone.
        expect(_scenes_link(page)).to_have_accessible_description("")
        assert current_hash(page) == "#/now"
        expect(page.get_by_role("heading", level=1, name="Now", exact=True)).to_be_visible()

        page.go_back()
        expect(page.get_by_role("heading", level=1, name="Scenes", exact=True)).to_be_visible()
        expect(_scenes(page).get_by_role("button", name="New Scene", exact=True)).to_be_visible()
        assert current_hash(page) == "#/scenes"
        expect(scene_form(page)).to_have_count(0)
        expect(_scenes(page).get_by_label("Scene inflight", exact=True)).to_be_visible()


def test_continue_shows_only_its_own_steps_reasons(page, registry):
    """Review finding 2: a failed Continue shows its own step's reasons; a later step shows
    none until its own Continue (the name on Review is not invalid on arrival), and only
    Review's Save shows every reason. Mutation probe: make Continue's check global."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = start_scene(page)
        form.get_by_role("button", name="Continue", exact=True).click()
        expect(form.get_by_role("alert")).to_contain_text("Choose a Source.")
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")
        targets = form.get_by_role("group", name="Target frames", exact=True)
        expect(targets).to_be_visible()
        expect(form.get_by_text("Choose at least one frame.")).to_have_count(0)
        expect(targets).to_have_accessible_description("")
        # Its own Continue shows it.
        form.get_by_role("button", name="Continue", exact=True).click()
        expect(targets).to_have_accessible_description("Choose at least one frame.")
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        scene_continue(page, "Playback")
        scene_continue(page, "Review")
        name = form.get_by_label("Scene name", exact=True)
        expect(name).not_to_have_attribute("aria-invalid", "true")
        expect(form.locator(".field__reason")).to_have_count(0)
        form.get_by_role("button", name="Save Scene", exact=True).click()
        expect(name).to_have_attribute("aria-invalid", "true")


def test_browser_back_from_a_discard_confirmation_leaves_the_page_usable(page, registry):
    """Review finding 3: the Discard confirmation on the always-mounted Scenes page is put
    away when browser Back hides the page (pageVisibility.js), so the page shown is not
    inert, and the draft is kept."""
    _seed(registry)
    queue = _seed_source(registry)
    _runtime(registry).command("set_scene", _console_scene("evening"))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "now")
        form = start_scene(page)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        page.go_back()  # the flow's entry -> #/scenes
        page.go_back()  # -> #/now
        assert current_hash(page) == "#/now"
        go(page, "scenes")
        _scenes(page).get_by_role("button", name="Edit Scene evening", exact=True).click()
        expect(page.get_by_role("dialog", name="Discard your unsaved draft?")).to_be_visible()
        page.go_back()
        assert current_hash(page) == "#/now"
        expect(page.get_by_role("dialog")).to_have_count(0)
        go(page, "schedule")  # the sidebar takes the click: nothing is inert
        go(page, "scenes")
        expect(_scenes(page).get_by_role("button", name="Resume draft (Draft)", exact=True)
               ).to_be_visible()


def test_a_replace_in_flight_on_a_hidden_page_neither_blocks_nor_moves_the_operator(
        page, registry):
    """Review finding 3 with finding 1: a Replace in flight when its page is hidden leaves
    no invisible modal; when it lands, the flow ends without taking the operator's route,
    and the Scenes page says what happened."""
    _seed(registry)
    queue = _seed_source(registry)
    _runtime(registry).command("set_scene", _console_scene("evening"))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        _scenes(page).get_by_role("button", name="Edit Scene evening", exact=True).click()
        form = scene_form(page)
        form.get_by_role("button", name="Change Seconds per cycle", exact=True).click()
        form.get_by_label("Seconds per cycle", exact=True).fill("45")
        scene_continue(page, "Review")
        gate = RequestGate(page, "**/v1/operator/scenes/evening")
        gate.holding = True
        form.get_by_role("button", name="Replace Scene", exact=True).click()
        page.get_by_role("dialog").get_by_role("button", name="Confirm replace").click()
        gate.wait_held()

        visit(page, "#/now")  # a typed URL, while the dialog is in flight
        expect(page.get_by_role("dialog")).to_have_count(0)
        go(page, "schedule")
        with page.expect_response(lambda r: r.url.endswith("/v1/operator/scenes/evening")
                                  and r.request.method == "PUT") as info:
            gate.release()
        assert info.value.status == 200
        # The draft ended: the Replace's `finish` has run, and left the location alone.
        expect(_scenes_link(page)).to_have_accessible_description("")
        assert current_hash(page) == "#/schedule"
        expect(page.get_by_role("dialog")).to_have_count(0)

        go(page, "scenes")
        expect(_scenes(page).get_by_role("status")).to_have_text("Scene evening saved.")
        expect(scene_form(page)).to_have_count(0)
        expect(_scenes(page).get_by_label("Scene evening", exact=True)
               .get_by_text("Revision", exact=True)).to_have_count(0)


def test_a_focus_request_dies_with_the_view_it_was_made_for(page, registry):
    """Review finding 4: a routed problem whose field is not there (no Frame to tick) leaves
    a focus request for the Frames step; moving to another step by URL drops it, so when
    the Frames step shows later, with a Frame by then, focus does not jump to it.
    Mutation probe: keep a request until it is spent."""
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes", paused_at=registry.clock.utc())
        visit(page, "#/scenes/new/review")
        form = scene_form(page)
        form.get_by_label("Scene name", exact=True).fill("Stale focus")
        form.get_by_role("button", name="Save Scene", exact=True).click()
        form.get_by_role("alert").get_by_role(
            "button", name="Choose at least one frame.", exact=True).click()
        assert current_hash(page) == "#/scenes/new/frames"
        expect(form.get_by_text("No Frames to target.", exact=True)).to_be_visible()

        visit(page, "#/scenes/new/playback")
        expect(form.get_by_label("Seconds per cycle", exact=True)).to_be_visible()
        _add_lobby_frame(registry)
        drive_poll(page)
        visit(page, "#/scenes/new/frames")
        lobby = form.get_by_label(f"Target frame {LOBBY_FRAME}", exact=True)
        expect(lobby).to_be_visible()
        page.wait_for_timeout(200)
        expect(lobby).not_to_be_focused()


def test_resuming_a_draft_whose_scene_is_gone_asks_for_no_focus(page, registry):
    """Review finding 4: Resume on a draft whose stored Scene is gone shows "no longer
    exists" and leaves no focus request behind, so when the Scene is back (a later poll)
    and its step shows, focus does not jump to the step heading."""
    _seed(registry)
    queue = _seed_source(registry)
    _runtime(registry).command("set_scene", _console_scene("evening"))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes", paused_at=registry.clock.utc())
        _scenes(page).get_by_role("button", name="Edit Scene evening", exact=True).click()
        form = scene_form(page)
        form.get_by_role("button", name="Change Seconds per cycle", exact=True).click()
        form.get_by_label("Seconds per cycle", exact=True).fill("45")
        go(page, "scenes")
        _drop_scene(registry, "evening")
        drive_poll(page)
        resume = _scenes(page).get_by_role("button", name="Resume draft (Draft)", exact=True)
        resume.click()
        expect(_scenes(page).get_by_text("Scene evening: This Scene no longer exists.")
               ).to_be_visible()

        _restore_scene(registry, _console_scene("evening"))
        drive_poll(page)
        visit(page, "#/scenes/evening/edit/playback")
        expect(form.get_by_label("Seconds per cycle", exact=True)).to_have_value("45")
        page.wait_for_timeout(200)
        assert _focused_heading(page) is None


def test_a_scene_restored_at_a_lower_revision_is_stale_too(page, registry):
    """Review finding 5: any other stored revision withholds Replace, including a lower
    one (a Scene deleted and made again, or restored). Mutation probe: stale only when the
    stored revision is greater."""
    _seed(registry)
    queue = _seed_source(registry)
    _runtime(registry).command("set_scene", _console_scene("evening", revision=3))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes", paused_at=registry.clock.utc())
        _scenes(page).get_by_role("button", name="Edit Scene evening", exact=True).click()
        form = scene_form(page)
        expect(form).to_contain_text("Editing evening. Its name stays the same.")
        _restore_scene(registry, _console_scene("evening", cycle_seconds=20))
        drive_poll(page)
        expect(_scenes(page)).to_contain_text(
            "This Scene changed since you opened it.")
        expect(form.get_by_role("button", name="Replace Scene", exact=True)).to_be_disabled()
        _scenes(page).get_by_role("button", name="Reload", exact=True).click()
        expect(form).to_contain_text("Editing evening. Its name stays the same.")
        form.get_by_role("button", name="Replace Scene", exact=True).click()
        with page.expect_response(
            lambda r: r.url.endswith("/v1/operator/scenes/evening") and r.request.method == "PUT"
        ) as info:
            page.get_by_role("dialog").get_by_role("button", name="Confirm replace").click()
        assert info.value.status == 200
        assert info.value.request.post_data_json["revision"] == 2


def test_reload_names_what_storage_changed_and_the_changes_it_replaced(page, registry):
    """Review finding 6: Reload names the values the other save changed (not the
    operator's own edits) and says the operator's unsaved changes were replaced."""
    _seed(registry)
    queue = _seed_source(registry)
    _runtime(registry).command("set_scene", _console_scene("evening"))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes", paused_at=registry.clock.utc())
        _scenes(page).get_by_role("button", name="Edit Scene evening", exact=True).click()
        form = scene_form(page)
        form.get_by_role("button", name="Change Seconds per cycle", exact=True).click()
        form.get_by_label("Seconds per cycle", exact=True).fill("45")
        scene_continue(page, "Review")
        _put_scene(page, origin, _console_scene("evening", revision=2, loop=False))
        drive_poll(page)
        _scenes(page).get_by_role("button", name="Reload", exact=True).click()
        expect(_scenes(page).get_by_role("status").filter(has_text="Reloaded")).to_have_text(
            "Reloaded the latest saved Scene. Changed: Keep playing until the Program ends. "
            "Your unsaved changes to Scene evening were replaced.")
        answers = form.get_by_label("Your answers", exact=True)
        expect(answers).to_contain_text("No, it plays one cycle")
        expect(answers).to_contain_text("30")


def test_discarding_from_the_cards_says_so_and_focuses_new_scene(page, registry):
    """Review finding 7: after the cards' Discard draft, the status says what was discarded
    and focus moves to New Scene (the button that replaced Discard)."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = start_scene(page)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        go(page, "scenes")
        _scenes(page).get_by_role("button", name="Discard draft", exact=True).click()
        page.get_by_role("dialog", name="Discard your unsaved draft?").get_by_role(
            "button", name="Discard draft", exact=True).click()
        expect(_scenes(page).get_by_role("status").filter(has_text="Discarded")).to_have_text(
            "Discarded the draft for a new Scene.")
        expect(_scenes(page).get_by_role("button", name="New Scene", exact=True)).to_be_focused()


def test_a_frame_with_no_compatible_media_is_a_frame_problem(page, registry):
    """Review finding 8: a hand-picked frame whose Source offers it nothing says so on the
    frame choice, and Review's summary routes it to Frames."""
    _seed(registry)
    _, _, landscape = _authored_photos(registry)
    queue = _seed_source(registry, (landscape,))  # nothing fits a portrait frame
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = start_scene(page, hand_picked=True)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        message = (f"No compatible media for {VALID_FRAME} in holiday. "
                   "Choose another frame, or another Source.")
        targets = form.get_by_role("group", name="Target frames", exact=True)
        expect(targets).to_have_accessible_description(message)
        form.get_by_role("button", name="Continue", exact=True).click()
        expect(form.get_by_role("alert")).to_contain_text(message)
        assert current_hash(page) == "#/scenes/new/frames"

        visit(page, "#/scenes/new/review")
        form.get_by_label("Scene name", exact=True).fill("nothing-fits")
        form.get_by_role("button", name="Save Scene", exact=True).click()
        form.get_by_role("alert").get_by_role("button", name=message, exact=True).click()
        assert current_hash(page) == "#/scenes/new/frames"
        expect(targets.locator("input:focus")).to_have_count(1)  # the frame choice


def test_a_failed_candidates_read_offers_retry(page, registry):
    """Review finding 8: a candidates read that fails says so on Media per frame and
    offers Retry, which reads them again."""
    _seed(registry)
    portrait_a, portrait_b, landscape = _authored_photos(registry)
    queue = _seed_source(registry, (portrait_a, portrait_b, landscape))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        failed = answer_first(page, "**/v1/operator/sources/*/candidates*",
                              lambda route: route.fulfill(status=500))
        form = start_scene(page, hand_picked=True)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        scene_continue(page, "Media per frame")
        expect(form.get_by_text("Could not load candidate media for these Frames.")
               ).to_be_visible()
        assert len(failed) == 1
        form.get_by_role("button", name="Retry", exact=True).click()
        chooser = form.get_by_label(f"Media for frame {VALID_FRAME}", exact=True)
        expect(chooser.get_by_role("option")).to_have_count(3)
        expect(form.get_by_text("Could not load candidate media for these Frames.")
               ).to_have_count(0)


# --- Final review: a write belongs to the draft it was sent from.


def test_a_save_in_flight_holds_its_draft(page, registry):
    """Final review finding 1: while a Save is in flight the draft is held (flow/
    useFlowWrite.js). Discard, New and another Scene's Edit are disabled, and a route naming
    another Scene shows "Resume or Discard" with Discard disabled, so no other draft can
    open for the late answer to close. The answer then ends the flow it was sent from.
    Mutation probe: leave Discard enabled while busy."""
    _seed(registry)
    queue = _seed_source(registry)
    _runtime(registry).command("set_scene", _console_scene("evening"))
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = author_scene(page, "inflight", SOURCE, (VALID_FRAME,), submit=False)
        gate = RequestGate(page, "**/v1/operator/scenes/inflight")
        gate.holding = True
        form.get_by_role("button", name="Save Scene", exact=True).click()
        gate.wait_held()

        page.go_back()  # the flow's entry -> #/scenes
        assert current_hash(page) == "#/scenes"
        scenes = _scenes(page)
        expect(scenes.get_by_role("button", name="Resume draft (Draft)", exact=True)).to_be_enabled()
        expect(scenes.get_by_role("button", name="Discard draft", exact=True)).to_be_disabled()
        expect(scenes.get_by_role("button", name="Edit Scene evening", exact=True)).to_be_disabled()

        visit(page, "#/scenes/evening/edit/review")
        expect(scenes).to_contain_text("Unsaved draft for a new Scene: Resume or Discard")
        expect(scenes.get_by_role("button", name="Discard", exact=True)).to_be_disabled()
        scenes.get_by_role("button", name="Resume", exact=True).click()
        assert current_hash(page) == "#/scenes/new/review"

        with page.expect_response(lambda r: r.url.endswith("/v1/operator/scenes/inflight")
                                  and r.request.method == "PUT") as info:
            gate.release()
        assert info.value.status == 200
        expect(scenes.get_by_role("status")).to_have_text("Saved Scene inflight.")
        assert current_hash(page) == "#/scenes"
        expect(scenes.get_by_role("button", name="New Scene", exact=True)).to_be_enabled()


def test_a_late_answer_after_log_out_leaves_the_new_sessions_draft_alone(page, registry):
    """Final review finding 1: the answer to a Save belongs to the draft it was sent from
    (its id, useFlowDraft `isOpen`). Log out ends that draft (it remounts the shell); a
    new Scene begun after signing in again is another draft, so the late answer neither
    ends its flow nor moves the operator. Mutation probe: finish without checking the
    draft's id."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = author_scene(page, "inflight", SOURCE, (VALID_FRAME,), submit=False)
        gate = RequestGate(page, "**/v1/operator/scenes/inflight")
        gate.holding = True
        form.get_by_role("button", name="Save Scene", exact=True).click()
        gate.wait_held()

        page.get_by_role("button", name="Log out", exact=True).click()
        expect(page.get_by_role("heading", name="Sign in to Photo Wall")).to_be_visible()
        submit_sign_in(page)
        go(page, "scenes")
        form = start_scene(page)
        assert current_hash(page) == "#/scenes/new/photos"

        # The answer lands, then its one refresh: the flow would end right after it.
        with page.expect_response(SNAPSHOT):
            with page.expect_response(lambda r: r.url.endswith("/v1/operator/scenes/inflight")
                                      and r.request.method == "PUT") as info:
                gate.release()
        assert info.value.status == 200
        expect(page.get_by_role("group", name="Snapshot status", exact=True)
               ).not_to_have_attribute("aria-busy", "true")
        assert current_hash(page) == "#/scenes/new/photos"
        expect(form.get_by_label("Source", exact=True)).to_be_visible()


def _stored(page, origin, key):
    """The ids of what Central stores under runtime `key` ("definitions", "programs"),
    read through the public contract."""
    response = page.request.get(origin + "/v1/operator/runtime",
                                headers={"Authorization": "Bearer " + ADMIN})
    return sorted(response.json()[key])


def _committed_then_500(route):
    """The write reaches Central (and commits), but its answer is lost."""
    route.fetch()
    route.fulfill(status=500, content_type="application/json", body='{"error": "internal"}')


def test_a_save_central_did_not_answer_may_have_been_saved_and_saving_again_confirms(
        page, registry):
    """Final review finding 2: a new Scene's Save that got no answer (a 5xx after Central
    stored it) is not a collision with itself. The flow remembers the id it sent
    (flow/useFlowWrite.js `attempted`), says it may have been saved, leaves it out of the
    name check while the draft is unchanged, and Save again (an idempotent PUT of the same
    body) confirms it: one Scene, no second id. Mutation probe: drop the exclusion (Review
    says "already exists" and withholds Save)."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        puts = _puts(page)
        answer_first(page, "**/v1/operator/scenes/**", _committed_then_500)
        form = author_scene(page, "late-show", SOURCE, (VALID_FRAME,), submit=False)
        form.get_by_role("button", name="Save Scene", exact=True).click()
        scenes = _scenes(page)
        expect(scenes.get_by_role("status").filter(has_text="may have been saved")).to_have_text(
            "Scene late-show may have been saved: Central did not answer. Save again to confirm.")
        assert current_hash(page) == "#/scenes/new/review"
        # The refresh after the write lists it; it is not read as another Scene's name.
        expect(scenes.get_by_label("Scene late-show", exact=True)).to_have_count(0)
        assert _stored(page, origin, "definitions") == ["late-show"]
        expect(form.get_by_text(re.compile("already exists"))).to_have_count(0)
        expect(form.get_by_label("Scene name", exact=True)).not_to_have_attribute(
            "aria-invalid", "true")

        with page.expect_response(lambda r: r.url.endswith("/v1/operator/scenes/late-show")
                                  and r.request.method == "PUT") as info:
            form.get_by_role("button", name="Save Scene", exact=True).click()
        assert info.value.status == 200
        expect(scenes.get_by_role("status")).to_have_text("Saved Scene late-show.")
        assert current_hash(page) == "#/scenes"
        assert _stored(page, origin, "definitions") == ["late-show"]
        assert [url.rsplit("/", 1)[1] for url in puts] == ["late-show", "late-show"]


def test_a_changed_draft_after_an_unanswered_save_checks_its_name_again(page, registry):
    """The exclusion lasts only while the draft is the one sent: a changed draft is another
    Scene to save, so a stored id is a collision again."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        answer_first(page, "**/v1/operator/scenes/**", _committed_then_500)
        form = author_scene(page, "late-show", SOURCE, (VALID_FRAME,), submit=False)
        form.get_by_role("button", name="Save Scene", exact=True).click()
        expect(_scenes(page).get_by_role("status")).to_contain_text("may have been saved")
        assert _stored(page, origin, "definitions") == ["late-show"]
        form.get_by_role("button", name="Change Seconds per cycle", exact=True).click()
        form.get_by_label("Seconds per cycle", exact=True).fill("45")
        scene_continue(page, "Review")
        form.get_by_role("button", name="Save Scene", exact=True).click()
        expect(form.get_by_label("Scene name", exact=True)).to_have_attribute("aria-invalid", "true")
        expect(form.get_by_role("alert")).to_contain_text("already exists")


# --- Roadmap 1x: the Scene settings Central already plays, set in the console.


def _row(form, label):
    """A setting's row (patterns/setting-row.tsx): its control, help, default and Reset."""
    return form.locator(f'[data-setting="{label}"]')


def _definition(page, origin, scene_id):
    """The Scene Central stores, read through the public contract."""
    response = page.request.get(origin + "/v1/operator/runtime",
                                headers={"Authorization": "Bearer " + ADMIN})
    return response.json()["definitions"][scene_id]


def test_each_built_scene_setting_is_set_saved_and_shown_again(page, registry):
    """Roadmap 1x: Fade between photos (#41), How it ends and its length (#62, #63), Keep
    the last photo up (#64) and Keep these Frames together (#65) each show their catalogue
    default, are set on Playback, are saved into the Scene Central stores, and show again
    when the Scene is opened after a reload. The plan a Player gets from each value is
    tests/test_exposed_settings_roundtrip.py. Mutation probes: drop any one value from
    authoring.js `buildSave` (the stored Scene misses it) or from `decodeScene` (Edit shows
    the default, or is withheld)."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = start_scene(page)
        form.get_by_label("Source", exact=True).select_option(SOURCE)
        scene_continue(page, "Frames")
        form.get_by_label(f"Target frame {VALID_FRAME}", exact=True).check()
        scene_continue(page, "Playback")

        # Each row shows its default; a value at its default offers no Reset.
        fade = _row(form, "Fade between photos")
        expect(fade).to_contain_text("Default: 1.5 s")
        expect(fade.get_by_role("button", name=re.compile("^Reset"))).to_have_count(0)
        expect(_row(form, "How it ends")).to_contain_text("Default: Stops")
        expect(form.get_by_label("Ending length", exact=True)).to_have_count(0)
        fade.get_by_label("Fade between photos", exact=True).fill("3")
        expect(fade.get_by_role("button", name="Reset Fade between photos (Default: 1.5 s)")).to_be_visible()
        _row(form, "How it ends").get_by_label("Fades out", exact=True).check()
        form.get_by_label("Ending length", exact=True).fill("4.5")
        form.get_by_role("button", name="Advanced", exact=True).click()
        expect(_row(form, "Keep the last photo up")).to_contain_text("Default: On")
        expect(_row(form, "Keep these Frames together")).to_contain_text("Default: Off")
        keep_last = form.get_by_role("switch", name="Keep the last photo up", exact=True)
        expect(keep_last).to_be_checked()
        keep_last.click()
        form.get_by_role("switch", name="Keep these Frames together", exact=True).click()

        scene_continue(page, "Review")
        for answer in ("Fade between photos3 s", "How it endsFades out for 4.5 s",
                       "Keep the last photo upOff", "Keep these Frames togetherOn"):
            expect(form).to_contain_text(answer)
        form.get_by_label("Scene name", exact=True).fill("evening-fade")
        with page.expect_response(lambda r: r.url.endswith("/v1/operator/scenes/evening-fade")
                                  and r.request.method == "PUT") as info:
            form.get_by_role("button", name="Save Scene", exact=True).click()
        assert info.value.status == 200
        stored = _definition(page, origin, "evening-fade")
        body = stored["contributions"][0]
        assert (body["fade_in_seconds"], body["fade_out_seconds"], body["retain_on_expiry"]) == (
            1.5, 1.5, False)
        assert (stored["outro_seconds"], stored["protect_frames"]) == (4.5, True)
        assert [(c["kind"], c["fade_out_seconds"]) for c in stored["outro_contributions"]] == [
            ("media", 4.5)]

        # Shown again after a reload: Review answers each value and Playback holds it.
        page.reload()
        go(page, "scenes")
        _scenes(page).get_by_role("button", name="Edit Scene evening-fade", exact=True).click()
        for answer in ("Fade between photos3 s", "How it endsFades out for 4.5 s",
                       "Keep the last photo upOff", "Keep these Frames togetherOn"):
            expect(form).to_contain_text(answer)
        form.get_by_role("button", name="Change How it ends", exact=True).click()
        expect(_row(form, "How it ends").get_by_label("Fades out", exact=True)).to_be_checked()
        expect(form.get_by_label("Ending length", exact=True)).to_have_value("4.5")
        expect(form.get_by_label("Fade between photos", exact=True)).to_have_value("3")
        form.get_by_role("button", name="Advanced", exact=True).click()
        expect(form.get_by_role("switch", name="Keep the last photo up", exact=True)).not_to_be_checked()
        expect(form.get_by_role("switch", name="Keep these Frames together", exact=True)).to_be_checked()


def test_a_fade_longer_than_the_cycle_is_the_fades_problem(page, registry):
    """The planner refuses fades longer than a cycle, so the console says so on the fade."""
    _seed(registry)
    queue = _seed_source(registry)
    with operator_server(registry.db, registry.clock, media_queue=queue) as origin:
        connect(page, origin, "scenes")
        form = author_scene(page, "quick", SOURCE, (VALID_FRAME,), seconds=2, submit=False)
        form.get_by_role("button", name="Change Fade between photos", exact=True).click()
        form.get_by_label("Fade between photos", exact=True).fill("3")
        scene_continue(page)
        expect(_row(form, "Fade between photos").locator("xpath=..")).to_contain_text(
            "Fade between photos must be no longer than Seconds per cycle.")
        expect(form.get_by_label("Fade between photos", exact=True)).to_be_focused()
