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
from support.position import LIGHTING

from contracts.models import Calibration, RectangleFit
from scripts.container_build import daemon_image_build
from scripts.demo_wall import (
    CORE_IMAGES,
    PLAYER_REVISION,
    PLAYER_RUNNER,
    SCENARIOS,
    SCENE_CYCLE_SECONDS,
    SECURED_LEAD_SECONDS,
    DemoError,
    DemoHost,
    OperationFailure,
    await_central,
    await_connection_ok,
    baseline_checks,
    check_finished,
    checksum_revision,
    composition,
    configure_demo_source,
    connect_demo_library,
    core_image_mapping,
    delete_secured_original,
    demo_position,
    demo_query,
    expected_pool,
    fault_holds,
    fault_reported,
    journal_upstream_mutation,
    local_source_inventory,
    main,
    operator_action,
    outage_checks,
    player_cache_audit_code,
    preserved_locks,
    public_failure,
    read_pool,
    rejoined_player_ready,
    retryable_operator_error,
    role_operation,
    run_demo,
    selected_secured_presentation,
    setup_operation,
    validate_selected_revision,
    write_json,
)
from scripts.harness_bundle import IMMICH_RUNTIME_BUNDLE, WALL_HELPER_BUNDLE


def _app_image(directory: Path, revision: str, data: bytes = b"app root") -> Path:
    """A node component set's app image as node_release_writer leaves it: app.squashfs beside
    components.json (its ref) and revision.json (the stamp)."""
    directory.mkdir(parents=True, exist_ok=True)
    image = directory / "app.squashfs"
    image.write_bytes(data)
    write_json(directory / "components.json", {"app_environment": {
        "environment_sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}})
    write_json(directory / "revision.json", {"schema": 1, "revision": revision,
                                             "inputs_sha256": "a" * 64})
    return image


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
    assert daemon_image_build("pid1:local", tmp_path, platform="linux/arm64")[6:10] == [
        "--tag", "pid1:local", "--platform", "linux/arm64"]


@pytest.mark.parametrize("scenario,count",
                         [(name, 1 if name == "baseline" else 2) for name in SCENARIOS])
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
    # Roadmap 4: the worker reads no connections file; the key reaches Central through the
    # operator's API request. Only the upstream role (which writes the handoff), the operator
    # (which reads it) and the root-only, networkless volume initializer mount `private`.
    assert "PHOTO_WALL_CONNECTIONS_FILE" not in json.dumps(document)
    private = {name: mount["read_only"] for name, service in document["services"].items()
               if name != "database"
               for mount in service.get("volumes", []) if mount["source"] == "private"}
    assert private == {"upstream-tools": False, "operator": True, "init": False}
    assert document["services"]["init"]["network_mode"] == "none"


def test_the_demo_shortens_plan_and_retry_timing_within_their_constraints():
    """Demo-only timing: shorter than a deployment's, valid where Central and the Player bound it,
    and long enough that the steps waiting on it still test something."""
    from central.coordination import CoordinationLimits
    from contracts.liveness import SESSION_BACKOFF

    central = composition("pw-wall-demo-123456abcdef", "pw-immich-fixture-123456abcdef",
                          "full")["services"]["central"]["environment"]
    demo = CoordinationLimits(horizon_seconds=float(central["PHOTO_WALL_HORIZON_SECONDS"]),
                              renewal_seconds=float(central["PHOTO_WALL_RENEWAL_SECONDS"]))
    deployed = CoordinationLimits()
    assert demo.horizon_seconds < deployed.horizon_seconds
    assert demo.renewal_seconds < deployed.renewal_seconds
    # Every plan reaches at least the horizon ahead, so each Output always holds an assignment
    # starting beyond the deletion step's lead: that step always has a secured target.
    assert demo.horizon_seconds >= SECURED_LEAD_SECONDS + SCENE_CYCLE_SECONDS
    [backoff] = [ast.literal_eval(node.value) for node in ast.walk(ast.parse(PLAYER_RUNNER))
                 if isinstance(node, ast.Assign)
                 and any(getattr(target, "attr", None) == "BACKOFF" for target in node.targets)]
    # The Player indexes BACKOFF[min(attempt, 3)], and Central's silence threshold is derived
    # from its first step.
    assert len(backoff) == len(SESSION_BACKOFF) and backoff[0] == SESSION_BACKOFF[0]
    assert max(backoff) < max(SESSION_BACKOFF)


def test_player_runner_imports_only_stdlib_and_source_neutral_packages():
    tree = ast.parse(PLAYER_RUNNER)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module.split(".")[0])
    import sys
    # uplink is the Player's Central client (R1: the runner finds Central through it); it runs
    # on the stdlib and stdlib-only contracts (the initrd closure test, pyproject's import
    # contract), so it carries nothing source-specific.
    assert imports <= sys.stdlib_module_names | {"player", "contracts", "uplink"}
    assert "RecordingRenderer" in PLAYER_RUNNER and "simulated_actuation" in PLAYER_RUNNER


def test_wall_helper_bundle_is_import_closed_without_host_orchestration(tmp_path):
    files = WALL_HELPER_BUNDLE.stage(Path(__file__).parents[1], tmp_path)
    assert set(files) == {
        "demo_wall.py", "scripts/harness_failure.py", "scripts/immich_actions.py",
        "scripts/provenance_models.py", "scripts/runtime_provenance.py",
        "support/position.py",
    }
    assert not (tmp_path / "scripts/immich_fixture.py").exists()
    assert not (tmp_path / "scripts/docker_diagnostics.py").exists()
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); "
         "import demo_wall, scripts.immich_actions, scripts.runtime_provenance; print('closed')",
         str(tmp_path)],
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
        setup_operation(evidence, lambda: None, "setup_connection", "operator", "connect",
                        lambda: (_ for _ in ()).throw(DemoError("operator_http_503")))
    assert caught.value.code == "operator_http_503"

    result = setup_operation(evidence, lambda: None, "setup_source", "operator",
                             "configure_source", lambda: {"source_ref": "demo:1"})
    assert result == {"source_ref": "demo:1"}
    assert evidence["phases"]["setup_source"]["status"] == "passed"


def test_source_configuration_saves_a_draft_and_checks_its_receipt_in_the_setup_phase():
    class Host:
        def role(self, role, action):
            assert (role, action) == ("operator", "source")
            return {"source_ref": "demo:1"}

    evidence = {"phases": {}}
    result = setup_operation(
        evidence, lambda: None, "setup_source", "operator", "configure_source",
        lambda: configure_demo_source(Host()),
    )
    assert result == {"source_ref": "demo:1"}
    phase = evidence["phases"]["setup_source"]
    assert (phase["status"], phase["role"], phase["action"]) == (
        "passed", "operator", "configure_source",
    )


@pytest.mark.parametrize("response", [
    {},
    {"source_ref": "demo:1", "extra": "value"},
    {"source_ref": 1},
    {"source_ref": "other:1"},
    {"source_ref": "demo:2"},
    ["demo:1"],
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


@pytest.mark.parametrize("response,code", [
    ({"connection_ref": "library-0123456789ab"}, None),
    ({"connection_ref": "library-0123456789ab", "key": "secret"}, "connection_invalid"),
    ({}, "connection_invalid"),
])
def test_the_connection_receipt_is_centrals_answer_and_nothing_else(response, code):
    class Host:
        def role(self, role, action):
            assert (role, action) == ("operator", "connect")
            return response

    if code is None:
        assert connect_demo_library(Host()) == response
    else:
        with pytest.raises(DemoError, match=f"^{code}$"):
            connect_demo_library(Host())


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


INDIRECT_CODES = [
    "active_before_outage_timeout", "central_recovery_timeout", "deleted_check_timeout",
    "check_pending", "connection_not_ok", "fault_pool_changed", "fault_presentation_stopped",
    "new_media_not_presented", "permission_not_reported", "permission_recovery_timeout",
    "player_rejoin_timeout", "upstream_outage_not_reported", "upstream_recovery_timeout",
    "fixture_http_400", "fixture_http_401", "fixture_http_403", "fixture_http_404",
    "fixture_http_409", "fixture_http_422", "fixture_http_429", "fixture_http_500",
    "fixture_http_502", "fixture_http_503", "fixture_http_504",
]


def test_indirect_bounded_failure_codes_remain_in_closed_vocabulary():
    assert [str(DemoError(code)) for code in INDIRECT_CODES] == INDIRECT_CODES


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
    from contracts.models import FrameProfile, Layer, OutputBinding, Variant
    from contracts.photo_crop import CENTRED
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
                            profile=FrameProfile(width_px=64, height_px=48, diagonal_inches=20), lighting=LIGHTING)
    variant = Variant(asset_id="asset-a", recipe_id="e" * 64, sha256="a" * 64, size=1, media_type="image/png", width=1, height=1)
    layer = Layer(assignment_id="assignment-1", run_id="run-1", output_id="HDMI-A-1",
                  frame_id="frame-1", binding_generation=1, start=100, end=110,
                  media_origin=100, variant=variant, crop=CENTRED)
    composition = OutputComposition(binding, (LocalLayer(layer, None, 0, 1),))

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
    return dict(jobs=jobs, observations=[{}], groups=[dict(status="committed", members={"assignment": ["player-one"]})]), {"player-one": dict(persistence="volatile",
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


class _HealthProbe:
    """A host whose operator health answers fail with `failures` in order, then succeed."""

    def __init__(self, *failures):
        self.failures, self.calls = list(failures), 0

    def role(self, role, action):
        assert (role, action) == ("operator", "health")
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return {"status": "ok"}


def test_central_wait_retries_while_central_is_not_answering(monkeypatch):
    monkeypatch.setattr("scripts.demo_wall.time.sleep", lambda _: None)
    host = _HealthProbe(DemoError("operator_transport"), DemoError("operator_http_503"))
    assert await_central(host) == {"status": "ok"}
    assert host.calls == 3


@pytest.mark.parametrize("failure", [DemoError("demo_failed"), DemoError("operator_http_401"),
                                     ModuleNotFoundError("support")])
def test_central_wait_fails_at_once_with_the_real_failure(monkeypatch, failure):
    monkeypatch.setattr("scripts.demo_wall.time.sleep", lambda _: pytest.fail("retried"))
    host = _HealthProbe(failure)
    with pytest.raises(type(failure)) as caught:
        await_central(host)
    assert caught.value is failure and host.calls == 1
    expected = str(failure) if isinstance(failure, DemoError) else "demo_failed"
    assert public_failure(caught.value)["error"] == expected


def test_central_wait_times_out_only_on_a_silent_central(monkeypatch):
    monkeypatch.setattr("scripts.demo_wall.time.sleep", lambda _: None)
    host = _HealthProbe(*[DemoError("operator_transport")] * 3)
    with pytest.raises(DemoError, match="^central_startup_timeout$"):
        await_central(host, seconds=0)


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
        DemoHost.create(state, tmp_path / "fixture", tmp_path / "components/app.squashfs", "baseline",
                        central_image="sha256:" + "a" * 64)
    assert not state.exists()


def test_core_image_override_defaults_are_historical_and_persisted(tmp_path, monkeypatch):
    import scripts.immich_fixture as immich_fixture

    class FakeFixture:
        def __init__(self, state):
            self.project = "pw-immich-fixture-123456abcdef"

    monkeypatch.setattr(immich_fixture, "FixtureHost", FakeFixture)
    app_image = _app_image(tmp_path / "components", "dda8e98c5c54dc8ca9c007599f8a919eadbd5248")
    override = {"central": "sha256:" + "c" * 64, "worker": "sha256:" + "d" * 64}
    host = DemoHost.create(tmp_path / "state", tmp_path / "fixture", app_image, "baseline",
                           central_image=override["central"], worker_image=override["worker"])
    assert host.core_images == override
    marker = json.loads((host.state / "demo.json").read_text())
    assert marker["core_images"] == override
    assert marker["revision"] == PLAYER_REVISION
    assert marker["app_image_ref"] == json.loads(
        (app_image.parent / "components.json").read_text())["app_environment"]

    old_state = tmp_path / "old-state"
    old_state.mkdir()
    write_json(old_state / "demo.json", {
        "schema": 1, "project": "pw-wall-demo-123456abcdef", "state": str(old_state),
        "immich_state": str(tmp_path / "fixture"), "app_image": str(app_image),
        "scenario": "baseline", "capture_start": 0,
    })
    old_host = DemoHost(old_state)
    assert old_host.core_images == CORE_IMAGES
    assert old_host.revision == PLAYER_REVISION


def test_nonhistorical_revision_requires_paired_images_and_matching_app_image(tmp_path, monkeypatch):
    revision = "e" * 40
    monkeypatch.setattr("scripts.demo_wall._git_output",
                        lambda *args: "commit" if args[:2] == ("cat-file", "-t") else "")
    app_image = _app_image(tmp_path / "components", PLAYER_REVISION)
    with pytest.raises(DemoError, match="revision_requires_core_images"):
        validate_selected_revision(revision, app_image)
    with pytest.raises(DemoError, match="player_revision_mismatch"):
        validate_selected_revision(revision, app_image, "sha256:" + "a" * 64,
                                   "sha256:" + "b" * 64)


def test_an_app_image_that_is_not_its_components_ref_is_refused_before_state(tmp_path, monkeypatch):
    import scripts.immich_fixture as immich_fixture

    class FakeFixture:
        def __init__(self, state):
            self.project = "pw-immich-fixture-123456abcdef"

    monkeypatch.setattr(immich_fixture, "FixtureHost", FakeFixture)
    app_image = _app_image(tmp_path / "components", PLAYER_REVISION)
    app_image.write_bytes(b"another root")
    with pytest.raises(DemoError, match="player_image_mismatch"):
        DemoHost.create(tmp_path / "state", tmp_path / "fixture", app_image, "baseline")
    assert not (tmp_path / "state").exists()


def test_nonhistorical_source_mismatch_is_rejected_before_demo_creation(tmp_path, monkeypatch):
    revision = "f" * 40
    app_image = _app_image(tmp_path / "components", revision)
    called = False

    def create(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("scripts.demo_wall._git_output", lambda *args: "different")
    monkeypatch.setattr(DemoHost, "create", create)
    with pytest.raises(DemoError, match="core_revision_mismatch"):
        run_demo(
            tmp_path / "state", tmp_path / "fixture", app_image, "baseline", True,
            "sha256:" + "a" * 64, "sha256:" + "b" * 64, revision)
    assert not (tmp_path / "state").exists()
    assert not called


def test_selected_historical_revision_rejects_tracked_core_drift(tmp_path, monkeypatch):
    app_image = _app_image(tmp_path / "components", PLAYER_REVISION)

    def git_probe(*args):
        if args[:2] == ("cat-file", "-t"):
            return "commit"
        if args[:2] == ("diff", "--name-only"):
            return "central/app.py"
        return ""

    monkeypatch.setattr("scripts.demo_wall._git_output", git_probe)
    with pytest.raises(DemoError, match="core_dirty"):
        validate_selected_revision(PLAYER_REVISION, app_image)


def test_selected_revision_rejects_untracked_core_drift(tmp_path, monkeypatch):
    revision = "2" * 40
    app_image = _app_image(tmp_path / "components", revision)

    def git_probe(*args):
        if args[:2] == ("cat-file", "-t"):
            return "commit"
        if args[:2] == ("status", "--short"):
            return "?? player/new_source.py"
        return ""

    monkeypatch.setattr("scripts.demo_wall._git_output", git_probe)
    with pytest.raises(DemoError, match="core_dirty"):
        validate_selected_revision(revision, app_image,
                                   "sha256:" + "a" * 64, "sha256:" + "b" * 64)


def test_valid_selected_commit_allows_later_noncore_checkout_changes(tmp_path, monkeypatch):
    revision = "3" * 40
    app_image = _app_image(tmp_path / "components", revision)
    probes = []

    def git_probe(*args):
        probes.append(args)
        if args[:2] == ("cat-file", "-t"):
            return "commit"
        return ""

    monkeypatch.setattr("scripts.demo_wall._git_output", git_probe)
    images = validate_selected_revision(revision, app_image,
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
        validate_selected_revision("G" * 40, tmp_path / "missing/app.squashfs")
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
            if action == "check":
                return {"health": {"state": "ok", "checked_at": 90.0},
                        "check": {"state": "complete", "at": 90.0}}
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
    assert events[3] == ("role", "operator", "check")
    assert events[4] == ("save",)
    assert receipt == evidence["phases"]["deleted_secured_delete"]["check"]
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


def test_rejoined_player_ready_requires_fresh_epoch_and_post_rejoin_draw():
    """0009: the signed-release boot-health acceptance gate is retired, so a
    rejoined player is ready once it re-enrolls under a fresh authority epoch
    and draws complete, non-fallback content after the restart."""
    old = {"player_id": "p-player-one", "authority_epoch": 1}
    report = {
        "player_id": "p-player-one",
        "authority_epoch": 1,
        "outputs": 1,
        "events": [{"output_id": "HDMI-A-1", "utc": 101.0, "layers": [{}], "fallback": False}],
    }

    # Same authority epoch: the replacement has not re-enrolled yet.
    assert not rejoined_player_ready(old, report, 100.0)
    report["authority_epoch"] = 2
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
            if action == "check":
                return {"health": {"state": "ok", "checked_at": 90.0},
                        "check": {"state": "complete", "at": 90.0}}
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
    assert events[3] == ("role", "operator", "check")
    assert events[4] == ("save",)
    assert receipt == evidence["phases"]["evolved_change"]["check"]
    pre = saved[0]["phases"]["evolved_pre_change"]
    assert pre["central"] == before and pre["players"] == reports
    assert pre["action"] == "evolve"
    assert "completed_utc" not in saved[1]["phases"]["evolved_change"]
    evidence["phases"]["evolved_wait"] = {"error": "live_membership_timeout"}
    save()
    assert saved[-1]["phases"]["evolved_pre_change"] == pre
    assert saved[-1]["phases"]["evolved_change"]["result"] == result


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


def test_journaled_mutation_records_check_request_failure_after_upstream_success():
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
    assert "check" not in change


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
    bundle = {"schema": 1, "files": {
        "demo_wall.py": hashlib.sha256(harness.read_bytes()).hexdigest(),
    }}
    write_json(state / "contexts/helper/bundle.json", bundle)
    host = object.__new__(DemoHost)
    host.state = state
    host.marker = {"app_image_ref": {"environment_sha256": "e" * 64, "size_bytes": 1}}
    expected = local_source_inventory()
    harness_sha = hashlib.sha256(harness.read_bytes()).hexdigest()

    def compose(*args, **kwargs):
        role = args[2]
        assert args == ("exec", "-T", role, "python", "/harness/scripts/runtime_provenance.py")
        files = dict(expected)
        if mutation and role == mutation[0]:
            mutation[1](files)
        return json.dumps({"schema": 1, "files": files,
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
    assert audit["player_image"] == {"environment_sha256": "e" * 64, "size_bytes": 1}
    assert json.loads((tmp_path / "state/runtime-provenance.json").read_text()) == audit


@pytest.mark.parametrize("role,mutate", [
    ("central", lambda files: files.update({"central/stale.py": "a" * 64})),
    ("worker", lambda files: files.pop(next(iter(files)))),
    ("worker", lambda files: files.update({"extra.py": "b" * 64})),
])
def test_source_audit_rejects_stale_missing_or_extra_role_inventory(tmp_path, monkeypatch, role, mutate):
    host = _mock_audit_host(tmp_path, monkeypatch, (role, mutate))
    with pytest.raises(DemoError, match="core_image_source_mismatch"):
        host.source_audit()


@pytest.mark.parametrize("role", ["central", "worker"])
@pytest.mark.parametrize("payload", ["not json", "null", "[]", '{"files": {}}'])
def test_source_audit_rejects_malformed_role_results(tmp_path, monkeypatch, role, payload):
    host = _mock_audit_host(tmp_path, monkeypatch)
    original = host.compose
    monkeypatch.setattr(host, "compose", lambda *args, **kwargs:
        payload if args[2] == role else original(*args, **kwargs))
    with pytest.raises(DemoError, match="^runtime_provenance_invalid$"):
        host.source_audit()
    assert not (host.state / "runtime-provenance.json").exists()


@pytest.mark.parametrize("role", ["central", "worker"])
@pytest.mark.parametrize("changed,error", [
    ("harness", "harness_image_mismatch"), ("bundle", "harness_bundle_mismatch"),
])
def test_source_audit_compares_self_consistent_role_hashes_to_staged_host(
        tmp_path, monkeypatch, role, changed, error):
    host = _mock_audit_host(tmp_path, monkeypatch)
    original = host.compose

    def compose(*args, **kwargs):
        value = json.loads(original(*args, **kwargs))
        if args[2] == role:
            if changed == "harness":
                value["harness_sha256"] = "c" * 64
                value["helper_bundle"]["files"]["demo_wall.py"] = "c" * 64
            else:
                value["helper_bundle"]["files"]["scripts/runtime_provenance.py"] = "c" * 64
        return json.dumps(value)

    monkeypatch.setattr(host, "compose", compose)
    with pytest.raises(DemoError, match=error):
        host.source_audit()
    assert not (host.state / "runtime-provenance.json").exists()


def test_source_audit_failure_is_journaled_with_a_fixed_phase():
    evidence = {"phases": {}}
    saved = []

    def fail():
        raise DemoError("runtime_provenance_invalid")

    with pytest.raises(OperationFailure) as caught:
        setup_operation(evidence, lambda: saved.append(copy.deepcopy(evidence)),
                        "source_audit", "host", "source_audit", fail)
    assert saved[0]["phases"]["source_audit"]["status"] == "running"
    assert saved[-1]["phases"]["source_audit"]["status"] == "failed"
    assert caught.value.envelope()["phase"] == "source_audit"
    assert caught.value.envelope()["code"] == "runtime_provenance_invalid"


def test_source_audit_journals_validated_container_failure_stage_and_role():
    from scripts.provenance_models import ProvenanceCollectionError

    evidence = {"phases": {}}
    failure = ProvenanceCollectionError.from_code("provenance_manifest_unreadable").failure

    def fail():
        raise ProvenanceCollectionError(failure, "worker")

    with pytest.raises(OperationFailure, match="provenance_manifest_unreadable"):
        setup_operation(evidence, lambda: None, "source_audit", "host", "source_audit", fail)
    record = evidence["phases"]["source_audit"]
    assert record["provenance_role"] == "worker"
    assert record["provenance_failure"] == failure.model_dump(mode="json", by_alias=True)


# --- Roadmap 4: the demo pastes its connection and builds its Source through the API ------------

CONNECTION = "library-0123456789ab"
CAPTURE = 700_000_000


def test_the_demo_query_is_favourites_in_the_capture_window_photos_and_videos():
    """Erratum E-P4-FX2-1's disposition: screenshots are not left out."""
    from central.photo_library.model import Exclusions, SourceQuery, TakenBetween

    assert SourceQuery.model_validate(demo_query(CONNECTION, CAPTURE)) == SourceQuery(
        connection_ref=CONNECTION, favourites_only=True,
        taken=TakenBetween(start=CAPTURE, end=CAPTURE + 60), media_types=("photo", "video"),
        exclude=Exclusions(screenshots=False))


class _Operator:
    """Central's operator API as the demo's operator role meets it: records every request."""

    def __init__(self, pages=None, health=None, check=None):
        self.requests = []
        self.pages = pages or [{"items": [], "next": None}]
        self.health = health or {"state": "ok", "checked_at": 5.0}
        self.check = check or {"state": "complete", "at": 5.0}

    def __call__(self, request):
        import httpx

        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path,
                              dict(request.url.params), body))
        path = request.url.path
        if path == "/v1/operator/library/connections" and request.method == "POST":
            return httpx.Response(201, json={"connection_ref": CONNECTION})
        if path == "/v1/operator/library/connections":
            return httpx.Response(200, json=[{"connection_ref": CONNECTION, "name": "demo-library",
                                              "key_hint": "…cret", "health": self.health}])
        if path == "/v1/operator/photo-drafts":
            return httpx.Response(201, json={"draft_ref": "0b9f4c4e-8f43-4c43-9d0e-6a2b0d3c1a11"})
        if path.endswith("/save"):
            return httpx.Response(200, json={"source_ref": "demo:1"})
        if path.endswith("/checks"):
            return httpx.Response(202)
        if path == "/v1/operator/sources/demo:1/pool":
            index = int(request.url.params.get("after", "0"))
            page = self.pages[index]
            return httpx.Response(200, json={
                "check": self.check, "in_pool": {"photos": 0, "videos": 0}, "waiting": 0,
                "left_out": [], "items": [{"asset_id": item} for item in page["items"]],
                "next": page["next"]})
        return httpx.Response(404)


@pytest.fixture
def operator(monkeypatch, tmp_path):
    import httpx

    client = httpx.Client
    api = _Operator()
    monkeypatch.setenv("DEMO_ADMIN_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("DEMO_CAPTURE_START", str(CAPTURE))
    monkeypatch.setattr("scripts.demo_wall.CONNECTION_HANDOFF", tmp_path / "private/connection.json")
    monkeypatch.setattr("scripts.demo_wall.CONNECTION_STATE", tmp_path / "control/connection.json")
    (tmp_path / "private").mkdir()
    (tmp_path / "control").mkdir()
    write_json(tmp_path / "private/connection.json",
               {"url": "http://immich:2283/api", "key": "synthetic-runtime-key"})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(lambda request: api(request))))
    return api, tmp_path


def test_the_operator_pastes_the_handoff_through_the_api_and_keeps_only_the_ref(operator):
    api, root = operator
    result = operator_action("connect")
    assert result == {"connection_ref": CONNECTION}
    assert api.requests == [("POST", "/v1/operator/library/connections", {}, {
        "name": "demo-library", "url": "http://immich:2283/api", "key": "synthetic-runtime-key"})]
    assert json.loads((root / "control/connection.json").read_text()) == {
        "connection_ref": CONNECTION}
    assert stat.S_IMODE((root / "control/connection.json").stat().st_mode) == 0o600
    # The key never leaves the request: not in the answer, not in what the operator keeps.
    assert "synthetic-runtime-key" not in json.dumps(result)
    assert "synthetic-runtime-key" not in (root / "control/connection.json").read_text()


def test_the_connection_row_the_demo_records_carries_no_key_hint(operator):
    api, _ = operator
    write_json(operator[1] / "control/connection.json", {"connection_ref": CONNECTION})
    assert operator_action("connection") == {
        "connection_ref": CONNECTION, "health": {"state": "ok", "checked_at": 5.0}}


def test_the_operator_builds_the_source_through_a_draft_and_saves_it_as_demo(operator):
    api, root = operator
    write_json(root / "control/connection.json", {"connection_ref": CONNECTION})
    assert operator_action("source") == {"source_ref": "demo:1"}
    assert api.requests == [
        ("POST", "/v1/operator/photo-drafts", {}, {"query": demo_query(CONNECTION, CAPTURE)}),
        ("POST", "/v1/operator/photo-drafts/0b9f4c4e-8f43-4c43-9d0e-6a2b0d3c1a11/save", {},
         {"name": "demo"}),
    ]


def test_check_now_answers_what_the_connection_and_source_showed_just_before(operator):
    api, root = operator
    write_json(root / "control/connection.json", {"connection_ref": CONNECTION})
    assert operator_action("check") == {"health": api.health, "check": api.check}
    assert [(method, path) for method, path, _, _ in api.requests if method == "POST"] == [
        ("POST", f"/v1/operator/library/connections/{CONNECTION}/checks"),
        ("POST", "/v1/operator/sources/demo:1/checks"),
    ]


def test_the_pool_is_read_through_every_page():
    pages = {None: {"check": {"state": "complete", "at": 1.0}, "in_pool": {}, "waiting": 0,
                    "left_out": [], "items": [{"asset_id": "b"}], "next": "c1"},
             "c1": {"check": {"state": "complete", "at": 1.0}, "in_pool": {}, "waiting": 1,
                    "left_out": [], "items": [{"asset_id": "a"}], "next": None}}
    assert read_pool(pages.__getitem__) == {
        "check": {"state": "complete", "at": 1.0}, "in_pool": {}, "waiting": 1,
        "left_out": [], "asset_ids": ["a", "b"]}
    endless = {"check": {}, "in_pool": {}, "waiting": 0, "left_out": [], "items": [],
               "next": "again"}
    with pytest.raises(DemoError, match="^pool_page_bound$"):
        read_pool(lambda after: endless)


def test_expected_pool_ids_are_the_asset_identity_of_each_favourite_not_deleted():
    import base64

    from central.photo_library.model import asset_identity

    digest = hashlib.sha1(b"landscape").digest()
    revision = checksum_revision(base64.b64encode(digest).decode())
    assert revision == digest.hex()
    upstream = {"assets": [
        dict(label="landscape", upstream_id="u1", revision=revision, favorite=True, deleted=False),
        dict(label="portrait", upstream_id="u2", revision="b" * 40, favorite=True, deleted=True),
        dict(label="other", upstream_id="u3", revision="c" * 40, favorite=False, deleted=False),
    ]}
    assert expected_pool(upstream, CONNECTION) == {asset_identity(CONNECTION, "u1", revision)}
    for value in (None, "not base64!", base64.b64encode(b"short").decode()):
        with pytest.raises(DemoError, match="^upstream_checksum_invalid$"):
            checksum_revision(value)


def _snapshot(check, health, asset_ids=("a", "b")):
    return {"pool": {"check": check, "asset_ids": list(asset_ids)},
            "connection": {"connection_ref": CONNECTION, "health": health}}


OK = {"state": "ok", "checked_at": 5.0}
DENIED = {"state": "needs_a_look", "problem": {"kind": "missing_permissions", "missing": ["x"]},
          "since": 6.0, "checked_at": 6.0}
DOWN = {"state": "needs_a_look", "problem": {"kind": "unreachable"}, "since": 7.0,
        "checked_at": 7.0}
COMPLETE = {"state": "complete", "at": 5.0}
FAILED = {"state": "failed", "problem": "connection", "last_complete_at": 5.0}


def test_a_check_finished_only_when_it_differs_from_the_one_read_before():
    before = {"health": OK, "check": COMPLETE}
    assert not check_finished(_snapshot(COMPLETE, OK), before, "ok")
    assert not check_finished(_snapshot({"state": "checking", "seen_so_far": 0,
                                         "started_at": 6.0}, OK), before, "ok")
    assert check_finished(_snapshot({"state": "complete", "at": 6.0}, OK), before, "ok")
    assert not check_finished(_snapshot({"state": "complete", "at": 6.0}, OK), before,
                              "permission")
    assert check_finished(_snapshot(FAILED, DENIED), before, "permission")
    gone = dict(FAILED, problem="chosen_item_gone")
    assert not check_finished(_snapshot(gone, DENIED), before, "permission")


@pytest.mark.parametrize("before,snapshot,outcome,reported", [
    ({"health": OK, "check": COMPLETE}, _snapshot(FAILED, DENIED), "permission", True),
    ({"health": OK, "check": COMPLETE}, _snapshot(FAILED, DOWN), "permission", False),
    ({"health": OK, "check": COMPLETE}, _snapshot(FAILED, DOWN), "unavailable", True),
    ({"health": OK, "check": COMPLETE}, _snapshot(FAILED, OK), "unavailable", False),
    ({"health": DENIED, "check": FAILED}, _snapshot(COMPLETE, OK), "ok", True),
    ({"health": DENIED, "check": FAILED}, _snapshot(COMPLETE, DENIED), "ok", False),
    ({"health": DOWN, "check": FAILED}, _snapshot(FAILED, OK), "ok", False),
])
def test_a_fault_is_reported_by_the_connections_health_and_the_sources_check(
        before, snapshot, outcome, reported):
    assert fault_reported(before, outcome)(snapshot, {}) is reported


def _presenting(fallback=False, layers=({"sha256": "a" * 64},)):
    return {"player-one": {"outputs": 1, "events": [
        {"output_id": "HDMI-A-1", "layers": list(layers), "fallback": fallback}]}}


def test_c7_the_last_pool_plays_on_through_a_fault():
    fault_holds(_snapshot(FAILED, DENIED), _presenting(), ["a", "b"])
    # Mutation probe: a fault that empties the pool, or stops a Player, fails the e2e.
    with pytest.raises(DemoError, match="^fault_pool_changed$"):
        fault_holds(_snapshot(FAILED, DENIED, asset_ids=()), _presenting(), ["a", "b"])
    with pytest.raises(DemoError, match="^fault_presentation_stopped$"):
        fault_holds(_snapshot(FAILED, DENIED), _presenting(fallback=True, layers=()), ["a", "b"])


def test_setup_waits_for_the_pasted_connection_to_be_healthy(monkeypatch):
    monkeypatch.setattr("scripts.demo_wall.time.sleep", lambda _: None)
    answers = [{"connection_ref": CONNECTION, "health": {"state": "not_checked"}},
               {"connection_ref": CONNECTION, "health": OK}]

    class Host:
        def role(self, role, action):
            assert (role, action) == ("operator", "connection")
            return answers.pop(0)

    assert await_connection_ok(Host()) == {"connection_ref": CONNECTION, "health": OK}
    answers[:] = [{"connection_ref": CONNECTION, "health": DENIED}]
    with pytest.raises(DemoError, match="^connection_not_ok$"):
        await_connection_ok(Host(), seconds=0)


def test_no_image_or_service_names_a_connections_file():
    root = Path(__file__).resolve().parents[1]
    for name in ("Dockerfile", "compose.yaml", "scripts/demo_wall.py", "scripts/service_base.py",
                 "tests/support/workers.py"):
        source = (root / name).read_text()
        assert "PHOTO_WALL_CONNECTIONS_FILE" not in source and "connections.json" not in source


def test_the_demo_operator_drives_centrals_photo_routes_end_to_end(registry, tmp_path, monkeypatch):
    """Seam g (run ledger .claude/runs/photo-4.md): the wall demo's operator role pastes its
    connection, builds `demo:1` from a draft and asks for both checks through Central's real
    C1 and C2 routes, with the statuses `operator_status` expects; its pool read follows the
    Source's check as the worker's side records it. The demo's own requests run against
    create_app over a real database (no Immich, no worker); CI's software e2e then runs the
    whole wall on top of this."""
    from uuid import UUID

    import httpx
    from fastapi.testclient import TestClient
    from runtime_fakes import apply_procrastinate_schema

    import scripts.demo_wall as demo
    from central.app import create_app
    from central.photo_library.model import InPool, LibraryAsset
    from central.photo_library.ports import CheckComplete, CheckStep

    token = "demo-operator-" + "t" * 32
    monkeypatch.setenv("PHOTO_WALL_CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setenv("DEMO_ADMIN_TOKEN", token)
    monkeypatch.setenv("DEMO_CAPTURE_START", "1700000000")
    handoff, state = tmp_path / "handoff.json", tmp_path / "connection.json"
    handoff.write_text(json.dumps({"url": "http://immich.invalid", "key": "PUBLIC-DEMO-KEY-1234"}))
    monkeypatch.setattr(demo, "CONNECTION_HANDOFF", handoff)
    monkeypatch.setattr(demo, "CONNECTION_STATE", state)
    apply_procrastinate_schema(registry.db.dsn)
    app = create_app(registry.db, registry.clock, token)
    with TestClient(app, base_url="http://central:8000") as client:
        def central(*, base_url, headers, **_):
            client.headers.update(headers)
            return client

        monkeypatch.setattr(httpx, "Client", central)

        class Host:
            def role(self, role, action):
                assert role == "operator"
                return operator_action(action)

        connection = connect_demo_library(Host())
        assert json.loads(state.read_text()) == connection
        assert operator_action("connection")["health"] == {"state": "not_checked"}
        assert configure_demo_source(Host())["source_ref"] == "demo:1"
        before = operator_action("check")
        assert before["check"] == {"state": "never_checked"}
        library = app.state.photo_library
        with registry.db.transaction() as conn:
            queued = {row["task_name"] for row in conn.execute(
                "SELECT task_name FROM procrastinate_jobs").fetchall()}
            pool_key = conn.execute("SELECT pool_key FROM media_sources WHERE source_ref='demo:1'"
                                    ).fetchone()["pool_key"]
        assert {"photo_wall.library.check_connection", "photo_wall.library.check_pool"} <= queued
        photo = LibraryAsset(connection_ref=connection["connection_ref"],
                             upstream_id=str(UUID(int=1)), revision="1" * 40, kind="image",
                             taken_at=1700000010, width=4000, height=3000)
        lease = library.begin_check(pool_key)
        assert library.record_step(lease, CheckStep(progress={"page": 1}, entries=(
            InPool(asset=photo),), seen_so_far=1))
        assert library.finish_check(lease, CheckComplete())
        after = operator_action("check")
        assert demo.check_finished({"pool": after}, before, "ok")


@pytest.mark.parametrize("project", ["pw-wall-demo-000000000000", "pw-wall-demo-5a5a5a5a5a5a",
                                     "pw-wall-demo-f00dfeedbeef"])
def test_every_demo_upload_fills_every_demo_frame(project):
    """Roadmap 5a's fit rule refuses a photo whose Frame-shaped window, inside the Scene's crop
    limit, has fewer pixels than the Frame, or cuts its key content. Every demo image, measured
    as the worker measures it (no faces: the fixture's machine learning is off), and the video
    fill every demo Frame at the demo Scene's crop limit, so the wall's baseline shows both."""
    from central.wall_plan.fit import Fits, FitTarget, judge, shape_fit
    from central.wall_plan.fit_settings import FitSettings
    from contracts.models import FrameProfile
    from media.key_content import measure
    from scripts.demo_wall import (
        DEMO_CROP_LIMIT,
        DEMO_FRAME_PROFILE,
        DEMO_IMAGES,
        DEMO_VIDEO_SIZE,
    )
    from scripts.immich_actions import synthetic_image

    profile = FrameProfile.model_validate(DEMO_FRAME_PROFILE)
    # Full screen on a Display of the profile's size: the largest Frame output it can have.
    target, house = FitTarget(profile=profile,
                              output_px=(profile.width_px, profile.height_px)), FitSettings()
    for label, (size, _) in DEMO_IMAGES.items():
        key = measure((), synthetic_image(f"{project}-{label}", size, "2026-01-01T00:00:00Z"))
        verdict = judge("image", *size, key, target, DEMO_CROP_LIMIT, house)
        assert isinstance(verdict, Fits), (label, verdict)
    assert shape_fit("video", *DEMO_VIDEO_SIZE, target, DEMO_CROP_LIMIT, house) is None


def test_every_demo_upload_fills_the_demo_frame_output_and_none_fills_the_whole_display():
    """The e2e baseline needs photos on the wall: the fit rule judges them against the Frame
    output (owner, 2026-10-11), so the demo's Position is a mat opening its small uploads fill,
    on the 1920 x 1080 Output its simulated Pis report. Full screen, none of them would."""
    from central.wall_plan.fit import FitTarget, shape_fit
    from central.wall_plan.fit_settings import FitSettings
    from contracts.models import FrameProfile, frame_output
    from scripts.demo_wall import DEMO_CROP_LIMIT, DEMO_FRAME_PROFILE, DEMO_IMAGES, DEMO_VIDEO_SIZE

    assert "width_px=1920" in PLAYER_RUNNER and "height_px=1080" in PLAYER_RUNNER
    profile = FrameProfile.model_validate(DEMO_FRAME_PROFILE)
    display = (1920, 1080)
    opening = frame_output(profile, demo_position({"width_px": 1920, "height_px": 1080}), display,
                           turn=0)
    whole = frame_output(profile, Calibration(fit=RectangleFit(width=1080 / 1920)), display, turn=0)
    assert opening == (90, 90) and whole == (1080, 1080)        # the square Frame on the whole height
    uploads = [("image", size) for size, _ in DEMO_IMAGES.values()] + [("video", DEMO_VIDEO_SIZE)]
    for kind, (width, height) in uploads:
        assert shape_fit(kind, width, height, FitTarget(profile=profile, output_px=opening),
                         DEMO_CROP_LIMIT, FitSettings()) is None, (kind, width, height)
    for size, _ in DEMO_IMAGES.values():
        refused = shape_fit("image", *size, FitTarget(profile=profile, output_px=whole),
                            DEMO_CROP_LIMIT, FitSettings())
        assert refused is not None and refused.reason == "below_frame_pixels"


def test_the_byte_audit_reads_the_files_the_player_cache_writes(tmp_path: Path) -> None:
    """The demo's byte audit runs over the files a real Player cache wrote, named by its variant
    key and its bytes' digest, and hashes each against the digest part of its name."""
    from contracts.models import Variant
    from player.cache import Cache

    data = b"a photo the Player fetched"
    digest = hashlib.sha256(data).hexdigest()
    item = Variant(asset_id="asset-one", recipe_id="e" * 64, sha256=digest, size=len(data),
                   media_type="image/jpeg", width=2, height=2)
    Cache(tmp_path, max_bytes=1024).secure(item, [data], pin="audit", sha256=digest, size=len(data))
    audit = subprocess.run([sys.executable, "-c", player_cache_audit_code(str(tmp_path))],
                           check=True, capture_output=True, text=True)
    assert json.loads(audit.stdout) == [dict(key=hashlib.sha256(item.key.encode()).hexdigest(),
                                             sha256=digest, size=len(data))]
