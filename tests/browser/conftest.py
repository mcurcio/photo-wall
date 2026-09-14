"""Bounded public evidence for the opt-in, disposable operator browser checks."""

import importlib.metadata
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_registry import enroll

# The Bead 17 cutover retired the legacy flat-page tests (test_operator_browser.py,
# test_operator_content_browser.py) in favour of the re-hosted /console browser
# suite. CHECKS is repointed at those live console tests so the acceptance-evidence
# artifact keeps tracking real coverage. Each acceptance item is attached to the
# console test that asserts the equivalent behavior. Three legacy items are RETIRED
# because the redesigned console has no equivalent by architecture: the console is
# REST with per-request bearer auth (test_operator_binding_browser.py:143-145), so
# there is no persistent browser-held token (volatile_browser_token) and no
# operator-websocket fencing (same_token_reconnect, delayed_rejection_fence).

# The two console tests whose green result qualifies the authored-media and program
# control surfaces, respectively (see `qualification` in pytest_sessionfinish).
AUTHORED_MEDIA_CHECK = "test_author_authored_scene_saves_per_frame_choices_in_one_request"
PROGRAM_CONTROL_CHECK = "test_program_schedules_single_window_and_lists"

CHECKS = {
    # Registry / equipment / authentication walkthrough (binding + wall).
    "test_connect_with_a_rejected_token_shows_not_accepted_and_returns_to_login": (
        "authentication_rejection", "authentication_success", "authentication_recovery",
    ),
    "test_pending_player_appears_in_the_pending_rail": ("two_players_three_outputs",),
    "test_retiring_a_pending_player_moves_it_to_retired_and_drops_its_output": (
        "equipment_replacement_retirement",
    ),
    "test_drag_create_posts_frame_with_scaled_placement": ("frame_creation_binding",),
    # Commissioning / calibration lease + conflict handling.
    "test_manual_revert_clears_preview_and_returns_draft_to_committed": (
        "calibration_preview_revert_commit",
    ),
    "test_calibration_stale_commit_conflicts_on_revision": ("stale_calibration_conflict",),
    "test_calibration_overtaken_detected_by_inventory_poll": ("inventory_refresh_recovery",),
    "test_calibration_lease_expiry_reverts_to_committed_no_auto_renew": ("preview_expiry",),
    "test_commissioning_provenance_frame_facts_vs_live_readback": ("fresh_server_persistence",),
    # Content walkthrough: sources, scenes, programs, runs (showrunner).
    "test_sources_render_name_rev_with_refresh": ("current_generation_content_refresh",),
    "test_source_configuration_creates_source_awaiting_refresh": ("source_configuration_refresh",),
    "test_author_live_source_scene_saves_and_appears_by_id": (
        "live_scene_explicit_frames", "fresh_server_content_persistence",
    ),
    "test_authored_chooser_hard_filters_incompatible_candidate": ("compatible_per_frame_choices",),
    AUTHORED_MEDIA_CHECK: ("authored_scene_single_request_save",),
    PROGRAM_CONTROL_CHECK: ("program_local_time_authoring_removal",),
    "test_activation_shows_synchronous_outcome_truthfully": ("immediate_activation_ignore_queue",),
    "test_runs_region_shows_only_synchronous_outcomes_no_missed_window": ("natural_cycle_completion",),
    "test_cancel_removes_live_run": ("run_cancellation",),
    "test_finish_live_run_posts": ("controlled_program_admission_completion",),
}
RESULTS = pytest.StashKey[dict]()
BROWSERS = pytest.StashKey[set]()
ERRORS = pytest.StashKey[list]()


@pytest.fixture
def installation(registry):
    # Only simulated equipment setup bypasses the operator UI.
    first, _, _ = enroll(registry, count=2)
    second, _, _ = enroll(registry, count=1)
    return registry, first["player_id"], second["player_id"]


@pytest.fixture(autouse=True)
def page_errors(context, request):
    """Every page, including additional tabs, must remain free of JS exceptions."""
    errors = []
    request.node.stash[ERRORS] = errors

    def watch(page):
        page.on("pageerror", lambda error: errors.append(type(error).__name__))

    for page in context.pages:
        watch(page)
    context.on("page", watch)
    browser = context.browser
    request.config.stash.setdefault(BROWSERS, set()).add((
        browser.browser_type.name, browser.version,
    ))
    yield
    assert not errors, f"unexpected JavaScript page errors: {len(errors)}"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    name = getattr(item, "originalname", item.name)
    if name not in CHECKS:
        return
    phase = outcome.get_result()
    result = item.config.stash.setdefault(RESULTS, {}).setdefault(name, {"phases": {}})
    result["phases"][phase.when] = phase.outcome
    result["page_errors"] = len(item.stash.get(ERRORS, []))


def source_identity(root):
    """Allowlist provenance; never export environment values or Git status paths."""
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=5,
    ).strip()
    assert re.fullmatch(r"[a-f0-9]{40}", revision)
    dirty = bool(subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=root, timeout=5,
    ).strip())
    event = os.environ.get("GITHUB_EVENT_NAME", "local")
    if event not in {"local", "push", "pull_request", "workflow_dispatch"}:
        event = "other"
    declared = os.environ.get("GITHUB_SHA", "")
    declared = declared if re.fullmatch(r"[a-f0-9]{40}", declared) else None
    kind = {"pull_request": "synthetic_merge", "workflow_dispatch": "dispatched_ref",
            "push": "pushed_commit"}.get(event, "local_working_tree")
    return {"revision": revision, "dirty": dirty, "event": event,
            "github_sha": declared, "checkout_kind": kind,
            "declared_sha_matches": declared == revision if declared else None}


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("PHOTO_WALL_BROWSER_TESTS") != "1":
        return
    results = session.config.stash.get(RESULTS, {})
    checks = []
    for name, assertions in CHECKS.items():
        result = results.get(name, {})
        phases = result.get("phases", {})
        passed = all(phases.get(phase) == "passed" for phase in ("setup", "call", "teardown"))
        status = "passed" if passed else "failed" if "failed" in phases.values() else "not_completed"
        checks.append({"test": name, "status": status, "assertions": list(assertions),
                       "page_errors": result.get("page_errors", 0)})
    completed = all(check["status"] == "passed" for check in checks)
    passed_checks = {check["test"] for check in checks if check["status"] == "passed"}
    document = {
        "schema": 2, "scope": "operator_browser",
        "status": "passed" if completed and exitstatus == 0 else "failed" if exitstatus else "incomplete",
        "recorded_at": datetime.now(UTC).isoformat(),
        "source": source_identity(session.config.rootpath),
        "playwright": importlib.metadata.version("playwright"),
        "browsers": [{"name": name[:24], "version": version[:80]}
                     for name, version in sorted(session.config.stash.get(BROWSERS, set()))],
        "environment": {"database": "real_postgresql_disposable_schema", "transport": "loopback_http",
                        "equipment": "simulated", "players": 2, "outputs": 3,
                        "scheduler": False, "worker": False, "preview_clock": "controlled",
                        "runtime_advance": "controlled_production_owner",
                        "media": "generated_public_jpeg_controlled_acquisition_production_publication",
                        "preparation": "synthetic_recipe_and_build"},
        "checks": checks,
        "qualification": {"authored_media_controls": AUTHORED_MEDIA_CHECK in passed_checks,
                          "program_controls": PROGRAM_CONTROL_CHECK in passed_checks,
                          "scheduled_playback": False,
                          "native_rendering": False, "pxe": False, "physical_outputs": False},
    }
    data = (json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    assert len(data) <= 32768, "browser evidence limit"
    output = Path(session.config.getoption("output"))
    output.mkdir(parents=True, exist_ok=True)
    (output / "operator-browser.json").write_bytes(data)
