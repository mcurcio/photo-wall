"""Authenticated content controls over real HTTP and disposable PostgreSQL.

Equipment, source refresh, acquisition requests, a synthetic recipe/publication,
and elapsed time are fixture inputs.
Every authored operator mutation uses Chromium controls. No upstream, worker
process, background scheduler, Player, or renderer runs in this walkthrough.
"""

import os
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pytest
from media_queue import RecordingMediaQueue
from playwright.sync_api import expect
from test_operator_browser import bind, command, connect, create_frame, operator_server
from test_registry import ADMIN

from central.catalog import CatalogSnapshot
from central.db import Database
from central.media_ports import SourceConfigurationReceipt
from central.media_repository import MediaRepository
from central.media_store import MediaStore
from central.planner import AcquisitionRequest
from central.runtime import Admission, Program, RuntimeView, Scene
from central.runtime_store import RuntimeStore
from media.models import RefreshResult
from tests.public_media import public_photo, publish_photo

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1",
    reason="set PHOTO_WALL_BROWSER_TESTS=1 and install Playwright Chromium",
)
SOURCE = "browser-photos:1"
PORTRAIT, LANDSCAPE, OMITTED = "browser-portrait", "browser-landscape", "browser-omitted"
ZONE = "America/Los_Angeles"


@pytest.fixture
def browser_context_args(browser_context_args):
    return {**browser_context_args, "timezone_id": ZONE}


def runtime(page, origin):
    """Validate persisted definitions and observable Runs through public reads."""
    response = page.request.get(origin + "/v1/operator/runtime", headers={
        "Authorization": "Bearer " + ADMIN,
    })
    assert response.status == 200
    data = response.json()
    return SimpleNamespace(
        scenes={key: Scene.model_validate(value) for key, value in data["definitions"].items()},
        programs={key: Program.model_validate(value) for key, value in data["programs"].items()},
        current=RuntimeView.model_validate(data["current"]),
    )


def refresh_content(page):
    page.get_by_role("button", name="Refresh content and health", exact=True).click()
    expect(page.locator("#message")).to_have_text("Content and health refreshed.")


@pytest.fixture
def content(page, installation, tmp_path):
    registry, first, second = installation
    registry.clock.advance(1893456000 - registry.clock.utc())
    queue = RecordingMediaQueue()
    repository = MediaRepository(registry.db, registry.clock, queue=queue)
    storage = MediaStore(repository, tmp_path / "public-media")
    photos = (
        public_photo(number=1, captured_at=registry.clock.utc()),
        public_photo(number=2, width=192, height=108, captured_at=registry.clock.utc()),
    )
    with operator_server(registry.db, registry.clock, media_root=storage.root,
                         media_queue=queue) as origin:
        connect(page, origin)
        create_frame(page, PORTRAIT)
        bind(page, first, PORTRAIT)
        page.locator("#cal-frame").select_option(PORTRAIT)
        command(page, "Commit", f"/v1/operator/frames/{PORTRAIT}/calibration")
        page.get_by_label("Aperture width (mm)").fill("500")
        page.get_by_label("Aperture height (mm)").fill("300")
        page.get_by_label("Display width (px)").fill("1920")
        page.get_by_label("Display height (px)").fill("1080")
        create_frame(page, LANDSCAPE)
        bind(page, second, LANDSCAPE)
        page.locator("#cal-frame").select_option(LANDSCAPE)
        command(page, "Commit", f"/v1/operator/frames/{LANDSCAPE}/calibration")
        create_frame(page, OMITTED)

        page.get_by_label("Source name and revision").fill(SOURCE)
        page.get_by_label("Connection name").fill("fixture-library")
        page.locator("#source-type").select_option("image")
        receipt = SourceConfigurationReceipt.model_validate(command(
            page, "Save source", "/v1/operator/sources/" + quote(SOURCE, safe=""),
        ))
        assert receipt.source_ref == SOURCE and receipt.created
        expect(page.locator("#sources")).to_contain_text(SOURCE)
        expect(page.locator("#sources")).to_contain_text("Awaiting refresh")
        expect(page.locator("#scene-authored")).to_be_disabled()

        # Supply only the external media result. Source configuration and every
        # Scene/Program/Run command remain production browser/API operations.
        lease = repository.begin_scheduled_refresh()
        assert lease is not None and lease.source.source_ref == SOURCE
        assert repository.publish_refresh(lease, RefreshResult(snapshot=CatalogSnapshot(
            source_ref=SOURCE, refreshed_at=registry.clock.utc(),
            candidates=tuple(photo.asset.candidate for photo in photos),
        ), assets=tuple(photo.asset for photo in photos)))
        repository.set_recipe("e" * 64)
        for photo in photos:
            assert repository.request_acquisitions((AcquisitionRequest(
                asset_id=photo.asset.asset_id, assignment_ids=("browser-publication",),
                earliest_start=registry.clock.utc(),
            ),)) == 1
            publish_photo(storage, photo)
        refresh_content(page)
        expect(page.locator("#sources tbody tr")).to_have_count(1)
        expect(page.locator("#sources tbody tr td").nth(1)).to_have_text("ok")
        expect(page.locator("#scene-authored")).to_be_enabled()
        yield SimpleNamespace(registry=registry, origin=origin, photos=photos,
                              repository=repository, storage=storage)


def save_live_scene(page, name="browser-live"):
    page.get_by_label("Scene name", exact=True).fill(name)
    page.locator("#scene-source").select_option(SOURCE)
    page.locator("#scene-frames").select_option([PORTRAIT, LANDSCAPE])
    page.get_by_label("Seconds per cycle").fill("10")
    command(page, "Save Scene", f"/v1/operator/scenes/{name}")
    expect(page.locator("#scenes")).to_contain_text(name)


def test_browser_sources_live_and_authored_scenes(page, content):
    save_live_scene(page)
    live = runtime(page, content.origin).scenes["browser-live"]
    assert {item.target for item in live.contributions} == {
        "frame:" + PORTRAIT, "frame:" + LANDSCAPE,
    }
    assert all(item.source_refs == (SOURCE,) and not item.asset_refs for item in live.contributions)
    assert live.loop and live.cycle_seconds == 10

    page.get_by_label("Scene name", exact=True).fill("browser-authored")
    page.get_by_label("Choose a photo or video for each Frame").check()
    portrait = page.locator(f'select[data-frame="{PORTRAIT}"]')
    landscape = page.locator(f'select[data-frame="{LANDSCAPE}"]')
    expect(portrait.locator("option")).to_have_count(2)
    expect(landscape.locator("option")).to_have_count(3)
    portrait_id, landscape_id = (photo.asset.asset_id for photo in content.photos)
    expect(portrait.locator(f'option[value="{portrait_id}"]')).to_contain_text("Prepared")
    expect(portrait.locator(f'option[value="{landscape_id}"]')).to_have_count(0)
    portrait.select_option(portrait_id)
    landscape.select_option(landscape_id)
    expect(page.locator("#authored-status")).to_have_text(
        "Selections are compatible. Playback requires prepared media.",
    )
    landscape.select_option("")
    expect(page.locator("#authored-status")).to_have_text(
        "Choose one compatible photo or video for each Frame.",
    )
    landscape.select_option(landscape_id)
    expect(page.locator("#authored-status")).to_have_text(
        "Selections are compatible. Playback requires prepared media.",
    )
    result = command(page, "Save Scene", "/v1/operator/scenes/browser-authored/authored")
    assert result["created"] == 2 and set(result["asset_refs"]) == {portrait_id, landscape_id}
    authored = runtime(page, content.origin).scenes["browser-authored"]
    assert {item.target: item.asset_refs for item in authored.contributions} == {
        "frame:" + PORTRAIT: (portrait_id,), "frame:" + LANDSCAPE: (landscape_id,),
    }
    assert all(not item.source_refs for item in authored.contributions)
    assert "frame:" + OMITTED not in {item.target for item in authored.contributions}
    expect(page.locator("#scenes tbody tr")).to_have_count(2)


def local_minute(instant):
    return datetime.fromtimestamp(instant, ZoneInfo(ZONE)).strftime("%Y-%m-%dT%H:%M")


def schedule_program(page, name, start, finish):
    page.get_by_label("Program name").fill(name)
    page.locator("#program-scene").select_option("browser-live")
    page.locator("#program-start").fill(local_minute(start))
    page.locator("#program-end").fill(local_minute(finish))
    page.locator("#program-priority").fill("3")
    command(page, "Schedule Program", f"/v1/operator/programs/{name}")


def activate(page, *, repeat="ignore", expires=None):
    page.locator("#activate-scene").select_option("browser-live")
    page.locator("#activate-repeat").select_option(repeat)
    if expires is not None:
        page.locator("#activate-expires").fill(local_minute(expires))
    return Admission.model_validate(command(page, "Start Scene", "/v1/operator/activations"))


def test_browser_programs_run_controls_and_content_persistence(page, content):
    save_live_scene(page)
    clock = content.registry.clock
    now = clock.utc()
    expect(page.locator("#timezone")).to_have_text("Dates use " + ZONE + ".")
    schedule_program(page, "browser-remove", now + 60, now + 120)
    page.locator("#remove-program").select_option("browser-remove")
    command(page, "Remove Program", "/v1/operator/programs/browser-remove")
    assert not runtime(page, content.origin).programs
    expect(page.locator("#programs")).to_contain_text("None configured")
    schedule_program(page, "browser-scheduled", now + 120, now + 180)
    program = runtime(page, content.origin).programs["browser-scheduled"]
    assert program == Program(program_id="browser-scheduled", scene_id="browser-live",
                              starts_at=now + 120, ends_at=now + 180, priority=3)

    admitted = activate(page)
    assert admitted.status == "admitted" and admitted.run_id
    expect(page.locator("#message")).to_have_text("Scene started.")
    ignored = activate(page)
    assert ignored.status == "ignored" and ignored.run_id == admitted.run_id
    expect(page.locator("#message")).to_have_text("Scene already active; request ignored.")
    queued = activate(page, repeat="queue", expires=now + 60)
    assert queued.status == "queued" and queued.run_id is None
    expect(page.locator("#message")).to_have_text("Scene queued.")
    assert len(runtime(page, content.origin).current.runs) == 1

    page.locator("#control-run").select_option(admitted.run_id)
    command(page, "Finish naturally", f"/v1/operator/runs/{admitted.run_id}/finish")
    view = runtime(page, content.origin).current
    finished = next(run for run in view.runs if run.run_id == admitted.run_id)
    assert finished.finish_requested_at == now and finished.phase == "body"
    clock.advance(10)
    RuntimeStore(content.registry.db, clock).command("advance", clock.utc())
    refresh_content(page)
    view = runtime(page, content.origin).current
    finished = next(run for run in view.runs if run.run_id == admitted.run_id)
    assert finished.phase == "completed" and finished.ended_at == now + 10
    next_run = next(run for run in view.runs if run.phase == "body")
    assert next_run.run_id != admitted.run_id
    page.locator("#control-run").select_option(next_run.run_id)
    command(page, "Cancel Run and children", f"/v1/operator/runs/{next_run.run_id}/cancel")
    view = runtime(page, content.origin).current
    assert next(run for run in view.runs if run.run_id == next_run.run_id).phase == "cancelled"
    expect(page.locator("#control-run option")).to_have_count(0)

    # Explicit elapsed-time input through the production Runtime owner. The
    # background scheduler is disabled, so this cannot qualify scheduled playback.
    clock.advance(program.starts_at - clock.utc())
    RuntimeStore(content.registry.db, clock).command("advance", clock.utc())
    refresh_content(page)
    scheduled = next(run for run in runtime(page, content.origin).current.runs if run.phase == "body")
    assert scheduled.started_at == program.starts_at
    expect(page.locator(f'#control-run option[value="{scheduled.run_id}"]')).to_have_count(1)
    clock.advance(60)
    RuntimeStore(content.registry.db, clock).command("advance", clock.utc())
    refresh_content(page)
    saved = runtime(page, content.origin)
    assert next(run for run in saved.current.runs if run.run_id == scheduled.run_id).phase == "completed"
    expect(page.locator("#control-run option")).to_have_count(0)

    with closing(Database(content.registry.db.dsn)) as fresh:
        with operator_server(fresh, clock, media_root=content.storage.root,
                             media_queue=RecordingMediaQueue()) as restarted:
            connect(page, restarted)
            restored = runtime(page, restarted)
            assert restored.scenes == saved.scenes and restored.programs == saved.programs
            assert restored.current == saved.current
            expect(page.locator("#sources")).to_contain_text(SOURCE)
            expect(page.locator("#scenes")).to_contain_text("browser-live")
            expect(page.locator("#programs")).to_contain_text("browser-scheduled")
            expect(page.locator("#runs tbody tr")).to_have_count(3)
