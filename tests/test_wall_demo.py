"""Harness correctness and containment; real media execution is separate evidence."""

import ast
import copy
import hashlib
import json
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.container_build import daemon_image_build
from scripts.demo_wall import (
    CORE_IMAGES,
    PLAYER_REVISION,
    PLAYER_RUNNER,
    DemoError,
    DemoHost,
    OperationFailure,
    baseline_checks,
    composition,
    configure_demo_source,
    core_image_mapping,
    delete_secured_original,
    journal_upstream_mutation,
    local_source_inventory,
    main,
    operator_action,
    outage_checks,
    preserved_locks,
    public_failure,
    rejoined_player_ready,
    retryable_operator_error,
    role_operation,
    run_demo,
    selected_secured_presentation,
    setup_operation,
    source_configuration_receipt,
    source_refresh_completed,
    validate_selected_revision,
    write_json,
)
from scripts.harness_bundle import IMMICH_RUNTIME_BUNDLE, WALL_HELPER_BUNDLE


def test_daemon_image_build_explicitly_selects_and_loads_default_builder(tmp_path):
    assert daemon_image_build("wall:local", tmp_path) == [
        "docker", "buildx", "build", "--builder", "default", "--load",
        "--tag", "wall:local", str(tmp_path),
    ]
    assert daemon_image_build("boot:local", tmp_path, network="none",
                              labels=(("fixture", "one"),)) == [
        "docker", "buildx", "build", "--builder", "default", "--load",
        "--tag", "boot:local", "--network", "none", "--label", "fixture=one", str(tmp_path),
    ]


@pytest.mark.parametrize("scenario,count", [("baseline", 1), ("full", 2)])
def test_role_topology_has_no_player_upstream_or_private_material(scenario, count):
    document = composition("pw-wall-demo-123456abcdef", "pw-immich-fixture-123456abcdef", scenario)
    assert not any(service.get("ports") for service in document["services"].values())
    players = {name: service for name, service in document["services"].items() if name.startswith("player-")}
    assert len(players) == count
    for name, service in players.items():
        assert service["networks"] == ["wall"]
        assert service.get("volumes", []) == []
        assert service["tmpfs"] == ["/tmp", "/state:uid=10001,gid=10001,mode=0700"]
        assert set(service["environment"]) == {"PHOTO_WALL_DEMO_DEVICE_ID"}
        assert re.fullmatch(r"device-[a-f0-9]{64}", service["environment"]["PHOTO_WALL_DEMO_DEVICE_ID"])
        assert service["read_only"] and service["cap_drop"] == ["ALL"]
    central = document["services"]["central"]
    assert central["sysctls"]["net.ipv4.ip_forward"] == "0"
    assert set(central["networks"]) == {"wall", "backend"}
    assert set(document["services"]["worker"]["networks"]) == {"backend", "upstream"}
    assert document["networks"]["upstream"]["external"]
    assert document["volumes"]["fixture_setup"]["external"]
    assert document["services"]["upstream-tools"]["volumes"][0]["read_only"]
    assert all(mount["volume"]["nocopy"] for name, service in document["services"].items()
               if name != "database" for mount in service.get("volumes", []))


def test_player_runner_imports_only_stdlib_and_source_neutral_packages():
    tree = ast.parse(PLAYER_RUNNER)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module.split(".")[0])
    import sys
    assert imports <= sys.stdlib_module_names | {"player", "contracts"}
    assert "RecordingRenderer" in PLAYER_RUNNER and "simulated_actuation" in PLAYER_RUNNER


def test_wall_helper_bundle_is_import_closed_without_host_orchestration(tmp_path):
    files = WALL_HELPER_BUNDLE.stage(Path(__file__).parents[1], tmp_path)
    assert set(files) == {
        "demo_wall.py", "scripts/harness_failure.py", "scripts/immich_actions.py",
    }
    assert not (tmp_path / "scripts/immich_fixture.py").exists()
    assert not (tmp_path / "scripts/docker_diagnostics.py").exists()
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); "
         "import demo_wall, scripts.immich_actions; print('closed')", str(tmp_path)],
        check=False, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "closed\n"


def test_immich_runtime_bundle_excludes_host_process_control(tmp_path):
    files = IMMICH_RUNTIME_BUNDLE.stage(Path(__file__).parents[1], tmp_path)
    assert set(files) == {
        "scripts/harness_failure.py", "scripts/immich_actions.py", "scripts/immich_runtime.py",
    }
    assert not (tmp_path / "scripts/immich_fixture.py").exists()
    assert not (tmp_path / "scripts/docker_diagnostics.py").exists()
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import scripts.immich_runtime; "
         "print('closed')", str(tmp_path)],
        check=False, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "closed\n"
    compose = (Path(__file__).parent / "integration/compose.immich.yml").read_text()
    assert ("COPY scripts/harness_failure.py scripts/immich_actions.py "
            "scripts/immich_runtime.py /app/scripts/") in compose
    assert "COPY scripts/immich_fixture.py" not in compose
    assert "COPY scripts/docker_diagnostics.py" not in compose
    assert compose.count("entrypoint: [python, -m, scripts.immich_runtime]") == 2


def test_setup_operation_persists_typed_phase_failure_without_private_text():
    evidence = {"phases": {}}
    saved = []

    def save():
        saved.append(copy.deepcopy(evidence))

    with pytest.raises(OperationFailure) as caught:
        setup_operation(evidence, save, "setup_upstream", "upstream-tools", "initialize",
                        lambda: (_ for _ in ()).throw(RuntimeError("private token abc")))

    assert caught.value.envelope() == {
        "type": "photo_wall_helper_failure", "schema": 1,
        "phase": "setup_upstream", "role": "upstream-tools",
        "action": "initialize", "code": "demo_failed",
    }
    assert saved[0]["phases"]["setup_upstream"]["status"] == "running"
    assert saved[-1]["phases"]["setup_upstream"]["status"] == "failed"
    assert saved[-1]["phases"]["setup_upstream"]["code"] == "demo_failed"
    assert "private" not in json.dumps(saved[-1])


def test_setup_operation_preserves_bounded_domain_code_and_success_identity():
    evidence = {"phases": {}}
    with pytest.raises(OperationFailure) as caught:
        setup_operation(evidence, lambda: None, "setup_refresh", "operator",
                        "request_refresh",
                        lambda: (_ for _ in ()).throw(DemoError("operator_http_503")))
    assert caught.value.code == "operator_http_503"

    result = setup_operation(evidence, lambda: None, "setup_source", "operator",
                             "configure_source", lambda: {"source_ref": "demo:1"})
    assert result == {"source_ref": "demo:1"}
    assert evidence["phases"]["setup_source"]["status"] == "passed"


def test_source_configuration_uses_shared_api_receipt_and_phase_postcondition():
    class Host:
        def role(self, role, action):
            assert (role, action) == ("operator", "source")
            return {"source_ref": "demo:1", "created": True}

    evidence = {"phases": {}}
    result = setup_operation(
        evidence, lambda: None, "setup_source", "operator", "configure_source",
        lambda: configure_demo_source(Host()),
    )
    assert result == {"source_ref": "demo:1", "created": True}
    phase = evidence["phases"]["setup_source"]
    assert (phase["status"], phase["role"], phase["action"]) == (
        "passed", "operator", "configure_source",
    )


@pytest.mark.parametrize("response", [
    {"created": True},
    {"source_ref": "demo:1"},
    {"source_ref": "demo:1", "created": True, "extra": "value"},
    {"source_ref": "demo:1", "created": 1},
    {"source_ref": "other:1", "created": True},
    {"source_ref": "demo:1", "created": False},
    ["demo:1", True],
])
def test_source_configuration_contract_failures_are_typed_in_setup_phase(response):
    class Host:
        def role(self, role, action):
            return response

    evidence = {"phases": {}}
    with pytest.raises(OperationFailure) as caught:
        setup_operation(
            evidence, lambda: None, "setup_source", "operator", "configure_source",
            lambda: configure_demo_source(Host()),
    )
    assert caught.value.code.value == "source_configuration_invalid"
    phase = evidence["phases"]["setup_source"]
    assert (phase["status"], phase["role"], phase["action"], phase["code"]) == (
        "failed", "operator", "configure_source", "source_configuration_invalid",
    )


def test_shared_source_configuration_receipt_rejects_wrong_types_and_extra_fields():
    assert source_configuration_receipt({
        "source_ref": "demo:1", "created": True,
    }).model_dump(mode="json") == {"source_ref": "demo:1", "created": True}
    for response in (
        {"source_ref": "demo:1", "created": 1},
        {"source_ref": "demo:1", "created": True, "extra": False},
    ):
        with pytest.raises(DemoError, match="^source_configuration_invalid$"):
            source_configuration_receipt(response)


@pytest.mark.parametrize("output", ["not-json", "[]", '"scalar"', "null"])
def test_role_result_json_boundary_rejects_malformed_or_non_object_payloads(output):
    host = object.__new__(DemoHost)
    host.compose = lambda *args, **kwargs: output
    with pytest.raises(DemoError, match="^invalid_role_result$"):
        host.role("operator", "source")


def test_container_role_failure_has_stable_role_and_action_envelope():
    with pytest.raises(OperationFailure) as caught:
        role_operation("upstream-tools", "initialize",
                       lambda: (_ for _ in ()).throw(ModuleNotFoundError("private path")))
    assert caught.value.envelope() == {
        "type": "photo_wall_helper_failure", "schema": 1,
        "phase": "role_action", "role": "upstream-tools",
        "action": "initialize", "code": "demo_failed",
    }


def test_code_shaped_unknown_exceptions_and_demo_errors_are_not_public_codes():
    secret = "lowercasesecrettoken"
    assert public_failure(RuntimeError(secret)) == {"error": "demo_failed"}
    assert str(DemoError(secret)) == "demo_failed"
    with pytest.raises(OperationFailure) as caught:
        role_operation("upstream-tools", "initialize",
                       lambda: (_ for _ in ()).throw(DemoError(secret)))
    assert caught.value.code.value == "demo_failed"
    invalid_action = OperationFailure(
        "role_action", "operator", secret, "operator_http_503"
    )
    assert invalid_action.envelope()["action"] == "unknown"
    assert invalid_action.code.value == "demo_failed"
    assert secret not in json.dumps(invalid_action.envelope())


def test_immich_runtime_provenance_is_derived_from_complete_bundle():
    from scripts.immich_fixture import fixture_provenance_paths

    runtime, host = fixture_provenance_paths()
    assert {item.target for item in IMMICH_RUNTIME_BUNDLE.files} <= set(runtime)
    assert {item.source for item in IMMICH_RUNTIME_BUNDLE.files} <= set(host)
    assert "scripts/harness_failure.py" in runtime
    assert "scripts/harness_failure.py" in host


@pytest.mark.parametrize("code", [
    "active_before_outage_timeout", "central_recovery_timeout", "deleted_refresh_timeout",
    "new_media_not_presented", "permission_not_reported", "permission_recovery_timeout",
    "player_rejoin_timeout", "upstream_outage_not_reported", "upstream_recovery_timeout",
    "fixture_http_400", "fixture_http_401", "fixture_http_403", "fixture_http_404",
    "fixture_http_409", "fixture_http_422", "fixture_http_429", "fixture_http_500",
    "fixture_http_502", "fixture_http_503", "fixture_http_504",
])
def test_indirect_bounded_failure_codes_remain_in_closed_vocabulary(code):
    assert str(DemoError(code)) == code


def test_code_shaped_unknown_exception_cannot_escape_as_public_failure():
    secret = "lowercase_secret_token_12345678"
    assert public_failure(RuntimeError(secret)) == {"error": "demo_failed"}
    evidence = {"phases": {}}
    with pytest.raises(OperationFailure) as caught:
        setup_operation(evidence, lambda: None, "setup_upstream", "upstream-tools",
                        "initialize", lambda: (_ for _ in ()).throw(RuntimeError(secret)))
    assert caught.value.code == "demo_failed"
    assert secret not in json.dumps(evidence)


def test_player_runner_recorder_logs_only_successful_presentations():
    """Exercise the exact Recorder class shipped inside the Player image."""
    from contracts.models import Calibration, FrameProfile, Layer, OutputBinding, Variant
    from player.rendering import LocalLayer, OutputComposition

    tree = ast.parse(PLAYER_RUNNER)
    imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    recorder_class = next(node for node in tree.body
                          if isinstance(node, ast.ClassDef) and node.name == "Recorder")
    module = ast.fix_missing_locations(ast.Module(body=[*imports, recorder_class], type_ignores=[]))
    namespace = {"__name__": "test_player_runner_recorder"}
    exec(compile(module, "<PLAYER_RUNNER Recorder>", "exec"), namespace)

    renderer = namespace["Recorder"]()
    binding = OutputBinding(output_id="HDMI-A-1", frame_id="frame-1", generation=1,
                            profile=FrameProfile(width_px=64, height_px=48, diagonal_inches=20))
    variant = Variant(sha256="a" * 64, size=1, media_type="image/png", width=1, height=1)
    layer = Layer(assignment_id="assignment-1", run_id="run-1", output_id="HDMI-A-1",
                  frame_id="frame-1", binding_generation=1, start=100, end=110,
                  media_origin=100, variant=variant)
    composition = OutputComposition(binding, Calibration(), (LocalLayer(layer, None, 0, 1),))

    renderer.pending.add("assignment-1")
    assert renderer.present(composition).status == "pending"
    assert not renderer.events and renderer.last == {}
    renderer.pending.clear()
    renderer.presentation_failures.add("assignment-1")
    assert renderer.present(composition).status == "failed"
    assert not renderer.events and renderer.last == {}
    renderer.presentation_failures.clear()
    assert renderer.present(composition).status == "presented"
    assert len(renderer.events) == 1 and renderer.last["HDMI-A-1"]


def example():
    jobs = [dict(state="ready", variant=dict(sha256=digest, media_type=kind))
            for digest, kind in (("a"*64, "image/jpeg"), ("b"*64, "video/mp4"))]
    events = [dict(output_id="HDMI-A-1", layers=[dict(job["variant"])]) for job in jobs]
    return dict(jobs=jobs, observations=[{}], groups=[dict(status="committed", members={"assignment": ["player-one"]})]), {"player-one": dict(persistence="volatile", release_accepted=True,
        forbidden_imports_absent=True, commit_checks=2, commit_failures=[], outputs=1, events=events)}


def test_baseline_requires_real_byte_types_and_simulated_commit_evidence():
    snapshot, players = example()
    result = baseline_checks(snapshot, players)
    assert result == dict(prepared_variants=2, observed_media_types=["image/jpeg", "video/mp4"],
                          players=1, outputs=1, commit_checks=2, actuation="simulated")


@pytest.mark.parametrize("mutation,code", [
    (lambda s, p: s["jobs"].pop(), "video_not_prepared"),
    (lambda s, p: p["player-one"].update(outputs=2), "output_not_drawn"),
    (lambda s, p: p["player-one"].update(forbidden_imports_absent=False), "player_boundary"),
    (lambda s, p: p["player-one"].update(commit_failures=["bad"]), "readiness_commit_proof"),
    (lambda s, p: p["player-one"]["events"][0]["layers"][0].update(sha256="c"*64), "unexpected_player_bytes"),
    (lambda s, p: s.update(observations=[]), "central_observation_missing"),
    (lambda s, p: s["groups"][0].update(status="skipped"), "complete_group_commit_missing"),
])
def test_baseline_fails_closed_for_missing_or_mismatched_evidence(mutation, code):
    snapshot, players = copy.deepcopy(example())
    mutation(snapshot, players)
    with pytest.raises(DemoError, match=code):
        baseline_checks(snapshot, players)


def test_evidence_and_generated_configuration_are_private(tmp_path):
    target = tmp_path / "private.json"
    write_json(target, {"test": True})
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_mutation_proof_freezes_still_live_locks_but_allows_expired_ones_to_disappear():
    lock = dict(player_id="one", authority_epoch=1, assignment_id="a", sha256="a"*64,
                end=100, valid_until=110)
    before = dict(utc=80, locks=[lock])
    assert preserved_locks(before, dict(utc=90, locks=[dict(lock)])) == 1
    for changed in ([], [dict(lock, sha256="b"*64)], [dict(lock, authority_epoch=2)]):
        with pytest.raises(DemoError, match="secured_assignment_changed"):
            preserved_locks(before, dict(utc=90, locks=changed))
    assert preserved_locks(before, dict(utc=100, locks=[])) == 0


def test_outage_checks_current_fallback_even_when_no_new_transition_occurs():
    report = dict(utc=105, outputs=2, events=[
        dict(output_id=name, utc=90, layers=[], fallback=True) for name in ("one", "two")])
    outage_checks({"player": report}, 100)
    with pytest.raises(DemoError, match="outage_report_stale"):
        outage_checks({"player": dict(report, utc=99)}, 100)
    report["events"].append(dict(output_id="two", utc=99, layers=[{}], fallback=False))
    with pytest.raises(DemoError, match="outage_fallback_missing"):
        outage_checks({"player": report}, 100)
    report["events"][-1]["utc"] = 102
    report["events"].append(dict(output_id="two", utc=103, layers=[], fallback=True))
    with pytest.raises(DemoError, match="outage_lease_overrun"):
        outage_checks({"player": report}, 100)


def test_real_http_client_transport_failure_is_coded_for_bounded_recovery(monkeypatch):
    import httpx

    client = httpx.Client

    def disconnected(request):
        raise httpx.ConnectError("private transport details must not escape", request=request)

    monkeypatch.setenv("DEMO_ADMIN_TOKEN", "synthetic-test-token")
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(disconnected)))
    with pytest.raises(DemoError, match="^operator_transport$") as caught:
        operator_action("health")
    assert retryable_operator_error(caught.value)


@pytest.mark.parametrize("code", ["operator_http_401", "operator_http_403", "operator_http_422",
                                  "operator_http_500", "readiness_commit_proof", "demo_failed"])
def test_recovery_does_not_retry_authority_schema_or_unknown_failures(code):
    assert not retryable_operator_error(DemoError(code))


@pytest.mark.parametrize("central,worker", [
    (None, "sha256:" + "a" * 64),
    ("sha256:" + "a" * 64, None),
    ("latest", "sha256:" + "a" * 64),
    ("sha256:" + "A" * 64, "sha256:" + "b" * 64),
    ("sha256:" + "a" * 63, "sha256:" + "b" * 64),
])
def test_core_image_override_requires_paired_lowercase_sha256_ids(central, worker):
    with pytest.raises(DemoError):
        core_image_mapping(central, worker)


def test_invalid_core_image_override_is_rejected_before_state_creation(tmp_path):
    state = tmp_path / "state"
    with pytest.raises(DemoError, match="core_images_must_be_paired"):
        DemoHost.create(state, tmp_path / "fixture", tmp_path / "wheelhouse", "baseline",
                        central_image="sha256:" + "a" * 64)
    assert not state.exists()


def test_core_image_override_defaults_are_historical_and_persisted(tmp_path, monkeypatch):
    import scripts.immich_fixture as immich_fixture

    class FakeFixture:
        def __init__(self, state):
            self.project = "pw-immich-fixture-123456abcdef"

    monkeypatch.setattr(immich_fixture, "FixtureHost", FakeFixture)
    wheelhouse = tmp_path / "wheelhouse"
    (wheelhouse / "wheels").mkdir(parents=True)
    requirements = wheelhouse / "requirements.txt"
    requirements.write_text("")
    write_json(wheelhouse / "inventory.json", {
        "revision": "dda8e98c5c54dc8ca9c007599f8a919eadbd5248",
        "requirements_sha256": hashlib.sha256(b"").hexdigest(), "wheels": [],
    })
    override = {"central": "sha256:" + "c" * 64, "worker": "sha256:" + "d" * 64}
    host = DemoHost.create(tmp_path / "state", tmp_path / "fixture", wheelhouse, "baseline",
                           central_image=override["central"], worker_image=override["worker"])
    assert host.core_images == override
    marker = json.loads((host.state / "demo.json").read_text())
    assert marker["core_images"] == override
    assert marker["revision"] == PLAYER_REVISION

    old_state = tmp_path / "old-state"
    old_state.mkdir()
    write_json(old_state / "demo.json", {
        "schema": 1, "project": "pw-wall-demo-123456abcdef", "state": str(old_state),
        "immich_state": str(tmp_path / "fixture"), "wheelhouse": str(wheelhouse),
        "scenario": "baseline", "capture_start": 0,
    })
    old_host = DemoHost(old_state)
    assert old_host.core_images == CORE_IMAGES
    assert old_host.revision == PLAYER_REVISION


def test_nonhistorical_revision_requires_paired_images_and_matching_wheelhouse(tmp_path, monkeypatch):
    revision = "e" * 40
    monkeypatch.setattr("scripts.demo_wall._git_output",
                        lambda *args: "commit" if args[:2] == ("cat-file", "-t") else "")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    write_json(wheelhouse / "inventory.json", {"revision": PLAYER_REVISION})
    with pytest.raises(DemoError, match="revision_requires_core_images"):
        validate_selected_revision(revision, wheelhouse)
    with pytest.raises(DemoError, match="player_revision_mismatch"):
        validate_selected_revision(revision, wheelhouse, "sha256:" + "a" * 64,
                                   "sha256:" + "b" * 64)


def test_nonhistorical_source_mismatch_is_rejected_before_demo_creation(tmp_path, monkeypatch):
    revision = "f" * 40
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    write_json(wheelhouse / "inventory.json", {"revision": revision})
    called = False

    def create(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("scripts.demo_wall._git_output", lambda *args: "different")
    monkeypatch.setattr(DemoHost, "create", create)
    with pytest.raises(DemoError, match="core_revision_mismatch"):
        run_demo(
            tmp_path / "state", tmp_path / "fixture", wheelhouse, "baseline", True,
            "sha256:" + "a" * 64, "sha256:" + "b" * 64, revision)
    assert not (tmp_path / "state").exists()
    assert not called


def test_selected_historical_revision_rejects_tracked_core_drift(tmp_path, monkeypatch):
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    write_json(wheelhouse / "inventory.json", {"revision": PLAYER_REVISION})

    def git_probe(*args):
        if args[:2] == ("cat-file", "-t"):
            return "commit"
        if args[:2] == ("diff", "--name-only"):
            return "central/app.py"
        return ""

    monkeypatch.setattr("scripts.demo_wall._git_output", git_probe)
    with pytest.raises(DemoError, match="core_dirty"):
        validate_selected_revision(PLAYER_REVISION, wheelhouse)


def test_selected_revision_rejects_untracked_core_drift(tmp_path, monkeypatch):
    revision = "2" * 40
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    write_json(wheelhouse / "inventory.json", {"revision": revision})

    def git_probe(*args):
        if args[:2] == ("cat-file", "-t"):
            return "commit"
        if args[:2] == ("status", "--short"):
            return "?? player/new_source.py"
        return ""

    monkeypatch.setattr("scripts.demo_wall._git_output", git_probe)
    with pytest.raises(DemoError, match="core_dirty"):
        validate_selected_revision(revision, wheelhouse,
                                   "sha256:" + "a" * 64, "sha256:" + "b" * 64)


def test_valid_selected_commit_allows_later_noncore_checkout_changes(tmp_path, monkeypatch):
    revision = "3" * 40
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    write_json(wheelhouse / "inventory.json", {"revision": revision})
    probes = []

    def git_probe(*args):
        probes.append(args)
        if args[:2] == ("cat-file", "-t"):
            return "commit"
        return ""

    monkeypatch.setattr("scripts.demo_wall._git_output", git_probe)
    images = validate_selected_revision(revision, wheelhouse,
                                        "sha256:" + "a" * 64, "sha256:" + "b" * 64)
    assert images == {"central": "sha256:" + "a" * 64, "worker": "sha256:" + "b" * 64}
    assert probes[0][:2] == ("cat-file", "-t")
    assert not any(args[:2] == ("rev-parse", "HEAD") for args in probes)


def test_revision_must_be_lowercase_40_hex_before_git_or_filesystem_access(tmp_path, monkeypatch):
    called = False

    def git_probe(*args):
        nonlocal called
        called = True
        return "commit"

    monkeypatch.setattr("scripts.demo_wall._git_output", git_probe)
    with pytest.raises(DemoError, match="exact_revision_required"):
        validate_selected_revision("G" * 40, tmp_path / "missing-wheelhouse")
    assert not called


def test_deleted_secured_audit_is_saved_before_mutation_and_survives_wait_failure():
    events = []
    saved = []
    evidence = {"phases": {}}
    before = {"utc": 100.0, "locks": [{
        "player_id": "player-one", "authority_epoch": 2,
        "assignment_id": "assignment-portrait", "run_id": "run-1", "output_id": "HDMI-A-1",
        "sha256": "p" * 64, "start": 110.0, "end": 118.0, "valid_until": 118.0,
    }]}
    reports = {"player-one": {"player_id": "player-one", "events": []}}

    class Host:
        def role(self, role, action):
            events.append(("role", role, action))
            if action == "refresh":
                return {"source_ref": "demo:1", "requested_revision": 2,
                        "completed_revision": 1, "coalesced": False}
            return {"action": action, "assets": [{"label": "portrait", "deleted": True}]}

    def save():
        import copy
        events.append(("save",))
        saved.append(copy.deepcopy(evidence))

    result, completed, receipt = delete_secured_original(
        Host(), evidence, save, before, reports, "p" * 64
    )
    assert result["action"] == "delete"
    assert completed == evidence["phases"]["deleted_secured_delete"]["completed_utc"]
    assert events[0] == ("save",)
    assert events[1] == ("save",)
    assert events[2] == ("role", "upstream-tools", "delete")
    assert events[3] == ("role", "operator", "refresh")
    assert events[4] == ("save",)
    assert receipt == evidence["phases"]["deleted_secured_delete"]["refresh"]
    pre = saved[0]["phases"]["deleted_secured_pre_delete"]
    assert pre["central"] == before and pre["players"] == reports
    assert pre["future_portrait_locks"] == [before["locks"][0]]
    assert "completed_utc" not in saved[1]["phases"]["deleted_secured_delete"]
    # A later timeout update cannot erase the pre-delete lock identity or mutation result.
    evidence["phases"]["deleted_secured_wait"] = {"error": "deleted_secured_not_presented"}
    save()
    assert saved[-1]["phases"]["deleted_secured_pre_delete"] == pre
    assert saved[-1]["phases"]["deleted_secured_delete"]["result"] == result


def _selected_presentation_fixture():
    lock = {
        "player_id": "p-player-one", "authority_epoch": 2,
        "assignment_id": "assignment-portrait", "run_id": "run-1", "output_id": "HDMI-A-1",
        "sha256": "p" * 64, "start": 110.0, "end": 118.0, "valid_until": 118.0,
    }
    event = {
        "utc": 111.0, "output_id": "HDMI-A-1", "fallback": False,
        "layers": [{"assignment_id": "assignment-portrait", "run_id": "run-1",
                     "sha256": "p" * 64}],
    }
    before = {"utc": 100.0, "locks": [lock]}
    reports = {"player-one": {"player_id": "p-player-one", "authority_epoch": 2,
                               "events": [event]}}
    return before, reports, lock, event


@pytest.mark.parametrize("mutation", [
    lambda lock, event, reports: event["layers"][0].update(assignment_id="other"),
    lambda lock, event, reports: reports["player-one"].update(player_id="other"),
    lambda lock, event, reports: reports["player-one"].update(authority_epoch=3),
    lambda lock, event, reports: event.update(output_id="HDMI-A-2"),
    lambda lock, event, reports: event["layers"][0].update(run_id="other"),
    lambda lock, event, reports: event["layers"][0].update(sha256="q" * 64),
    lambda lock, event, reports: event.update(utc=109.0),
    lambda lock, event, reports: event.update(utc=109.5),
    lambda lock, event, reports: event.update(utc=118.0),
    lambda lock, event, reports: event.update(fallback=True),
])
def test_selected_secured_presentation_requires_exact_postdelete_identity(mutation):
    before, reports, lock, event = _selected_presentation_fixture()
    mutation(lock, event, reports)
    assert selected_secured_presentation(before, reports, lock["sha256"], 109.0) is None


def test_selected_secured_presentation_records_exact_match_after_late_snapshot():
    before, reports, lock, _ = _selected_presentation_fixture()
    proof = selected_secured_presentation(before, reports, lock["sha256"], 109.0)
    assert proof == {
        "player_id": "p-player-one", "authority_epoch": 2, "output_id": "HDMI-A-1",
        "assignment_id": "assignment-portrait", "run_id": "run-1", "sha256": "p" * 64,
        "event_utc": 111.0,
    }


def test_selected_secured_presentation_uses_earlier_valid_until_boundary():
    before, reports, lock, event = _selected_presentation_fixture()
    lock["end"] = 120.0
    lock["valid_until"] = 112.0
    event["utc"] = 111.0
    assert selected_secured_presentation(before, reports, lock["sha256"], 109.0)
    event["utc"] = 112.0
    assert selected_secured_presentation(before, reports, lock["sha256"], 109.0) is None


def test_rejoined_player_waits_for_central_release_acceptance():
    old = {"player_id": "p-player-one", "authority_epoch": 1}
    report = {
        "player_id": "p-player-one",
        "authority_epoch": 2,
        "release_accepted": False,
        "outputs": 1,
        "events": [{"output_id": "HDMI-A-1", "utc": 101.0, "layers": [{}], "fallback": False}],
    }

    assert not rejoined_player_ready(old, report, 100.0)
    report["release_accepted"] = True
    assert rejoined_player_ready(old, report, 100.0)


def test_delete_secured_original_refuses_empty_selection_without_mutation():
    events = []
    evidence = {"phases": {}}
    before = {"utc": 100.0, "locks": []}
    reports = {"player-one": {"player_id": "player-one", "events": []}}

    class Host:
        def role(self, role, action):
            events.append((role, action))
            raise AssertionError("mutation must not run")

    with pytest.raises(DemoError, match="portrait_not_secured"):
        delete_secured_original(Host(), evidence, lambda: None, before, reports, "p" * 64)
    assert events == []


def test_evolved_audit_is_saved_before_upstream_mutation_and_survives_wait_failure():
    events = []
    saved = []
    evidence = {"phases": {}}
    before = {"utc": 100.0, "locks": [{"player_id": "player-one", "authority_epoch": 2,
        "assignment_id": "assignment-live", "run_id": "run-1", "sha256": "l" * 64,
        "start": 110.0, "end": 118.0, "valid_until": 118.0,
    }]}
    reports = {"player-one": {"player_id": "player-one", "events": [{"output_id": "one", "layers": []}]}}

    class Host:
        def role(self, role, action):
            events.append(("role", role, action))
            if action == "refresh":
                return {"source_ref": "demo:1", "requested_revision": 3,
                        "completed_revision": 2, "coalesced": False}
            return {"action": action, "assets": [{"label": "older-live", "sha1": "l", "deleted": False}]}

    def save():
        import copy
        events.append(("save",))
        saved.append(copy.deepcopy(evidence))

    result, completed, receipt = journal_upstream_mutation(
        Host(), evidence, save, before, reports, "evolve",
        pre_key="evolved_pre_change",
        change_key="evolved_change",
    )
    assert result["action"] == "evolve"
    assert completed == evidence["phases"]["evolved_change"]["completed_utc"]
    assert events[0] == ("save",)
    assert events[1] == ("save",)
    assert events[2] == ("role", "upstream-tools", "evolve")
    assert events[3] == ("role", "operator", "refresh")
    assert events[4] == ("save",)
    assert receipt == evidence["phases"]["evolved_change"]["refresh"]
    pre = saved[0]["phases"]["evolved_pre_change"]
    assert pre["central"] == before and pre["players"] == reports
    assert pre["action"] == "evolve"
    assert "completed_utc" not in saved[1]["phases"]["evolved_change"]
    evidence["phases"]["evolved_wait"] = {"error": "live_membership_timeout"}
    save()
    assert saved[-1]["phases"]["evolved_pre_change"] == pre
    assert saved[-1]["phases"]["evolved_change"]["result"] == result


def test_refresh_completion_matches_the_exact_accepted_revision():
    receipt = {"source_ref": "demo:1", "requested_revision": 4,
               "completed_revision": 3, "coalesced": True}
    source = {"source_ref": "demo:1", "refresh_completed_revision": 4, "status": "ok"}
    snapshot = {"media": {"sources": [source]}}

    assert source_refresh_completed(snapshot, receipt, status="ok")
    assert not source_refresh_completed(
        {"media": {"sources": [dict(source, refresh_completed_revision=3)]}},
        receipt,
        status="ok",
    )
    assert not source_refresh_completed(snapshot, receipt, status="permission")


def test_journaled_upstream_mutation_preserves_error_timepoint_and_result_fields():
    events = []
    saved = []
    evidence = {"phases": {}}
    before = {"utc": 200.0, "locks": []}
    reports = {"player-one": {"player_id": "player-one", "events": []}}

    class Host:
        def role(self, _, __):
            events.append(("role", _, __))
            raise DemoError("operator_http_503")

    def save():
        import copy
        events.append(("save",))
        saved.append(copy.deepcopy(evidence))

    with pytest.raises(DemoError, match="operator_http_503"):
        journal_upstream_mutation(
            Host(), evidence, save, before, reports, "evolve",
            pre_key="evolved_pre_change",
            change_key="evolved_change",
        )
    assert events[:2] == [("save",), ("save",)]
    assert events[2] == ("role", "upstream-tools", "evolve")
    assert evidence["phases"]["evolved_change"]["error"] == "operator_http_503"
    assert "result" not in evidence["phases"]["evolved_change"]
    assert "completed_utc" not in evidence["phases"]["evolved_change"]
    assert evidence["phases"]["evolved_change"]["failed_utc"] >= evidence["phases"]["evolved_change"]["invoked_utc"]
    evidence["phases"]["evolved_wait"] = {"error": "live_membership_timeout"}
    save()
    assert saved[-1]["phases"]["evolved_pre_change"]["central"] == before
    assert saved[-1]["phases"]["evolved_change"]["error"] == "operator_http_503"


def test_journaled_mutation_does_not_promote_code_shaped_unknown_exception():
    evidence = {"phases": {}}

    class Host:
        def role(self, role, action):
            raise RuntimeError("lowercasesecrettoken")

    with pytest.raises(RuntimeError, match="lowercasesecrettoken"):
        journal_upstream_mutation(
            Host(), evidence, lambda: None, {"locks": []}, {}, "evolve",
            pre_key="evolved_pre_change", change_key="evolved_change",
        )
    assert evidence["phases"]["evolved_change"]["error"] == "demo_failed"
    assert "lowercasesecrettoken" not in json.dumps(evidence)


def test_journaled_mutation_records_refresh_request_failure_after_upstream_success():
    evidence = {"phases": {}}

    class Host:
        def role(self, role, action):
            if role == "upstream-tools":
                return {"action": action, "assets": []}
            raise DemoError("operator_http_503")

    with pytest.raises(DemoError, match="operator_http_503"):
        journal_upstream_mutation(
            Host(), evidence, lambda: None, {"locks": []}, {}, "evolve",
            pre_key="evolved_pre_change",
            change_key="evolved_change",
        )

    change = evidence["phases"]["evolved_change"]
    assert change["result"] == {"action": "evolve", "assets": []}
    assert change["completed_utc"] >= change["invoked_utc"]
    assert change["failed_utc"] >= change["completed_utc"]
    assert change["error"] == "operator_http_503"
    assert "refresh" not in change


def test_plan_records_selected_revision_and_image_requirement(monkeypatch, capsys):
    revision = "1" * 40
    monkeypatch.setattr(sys, "argv", ["demo_wall.py", "plan", "--revision", revision])
    main()
    plan = json.loads(capsys.readouterr().out)
    assert plan["revision"] == revision
    assert plan["player_revision"] == revision
    assert plan["requires_core_images"] is True


def _mock_audit_host(tmp_path, monkeypatch, mutation=None):
    state = tmp_path / "state"
    (state / "contexts/helper").mkdir(parents=True)
    harness = state / "contexts/helper/demo_wall.py"
    harness.write_bytes(b"copied harness")
    bundle = {"schema": 1, "files": {"demo_wall.py": "a" * 64}}
    write_json(state / "contexts/helper/bundle.json", bundle)
    wheelhouse = tmp_path / "wheelhouse"
    (wheelhouse).mkdir()
    write_json(wheelhouse / "inventory.json", {})
    host = object.__new__(DemoHost)
    host.state = state
    host.marker = {"wheelhouse": str(wheelhouse)}
    expected = local_source_inventory()
    harness_sha = hashlib.sha256(harness.read_bytes()).hexdigest()

    def compose(*args, **kwargs):
        role = args[2]
        files = dict(expected)
        if mutation and role == mutation[0]:
            mutation[1](files)
        return json.dumps({"files": files,
                           "core_inventory_sha256": hashlib.sha256(
                               json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                           "adapter_sha256": files["media/immich.py"],
                           "preparer_sha256": files["media/prepare.py"],
                           "harness_sha256": harness_sha,
                           "helper_bundle": bundle})

    monkeypatch.setattr(host, "compose", compose)
    return host


def test_source_audit_requires_exact_inventory_for_both_roles_and_retains_worker_fields(tmp_path, monkeypatch):
    host = _mock_audit_host(tmp_path, monkeypatch)
    audit = host.source_audit()
    assert set(audit["role_audits"]) == {"central", "worker"}
    assert audit["files"] == audit["role_audits"]["worker"]["files"]
    assert audit["adapter_sha256"] == audit["role_audits"]["worker"]["adapter_sha256"]
    assert audit["harness_sha256"] == audit["role_audits"]["worker"]["harness_sha256"]


@pytest.mark.parametrize("role,mutate", [
    ("central", lambda files: files.update({"central/stale.py": "a" * 64})),
    ("worker", lambda files: files.pop(next(iter(files)))),
    ("worker", lambda files: files.update({"extra.py": "b" * 64})),
])
def test_source_audit_rejects_stale_missing_or_extra_role_inventory(tmp_path, monkeypatch, role, mutate):
    host = _mock_audit_host(tmp_path, monkeypatch, (role, mutate))
    with pytest.raises(DemoError, match="core_image_source_mismatch"):
        host.source_audit()
