"""Harness correctness and containment; real media execution is separate evidence."""

import ast
import copy
import hashlib
import json
import stat

import pytest

from scripts.demo_wall import (
    CORE_IMAGES,
    PLAYER_RUNNER,
    DemoError,
    DemoHost,
    baseline_checks,
    composition,
    core_image_mapping,
    local_source_inventory,
    operator_action,
    outage_checks,
    preserved_locks,
    retryable_operator_error,
    write_json,
)


@pytest.mark.parametrize("scenario,count", [("baseline", 1), ("full", 2)])
def test_role_topology_has_no_player_upstream_or_private_material(scenario, count):
    document = composition("pw-wall-demo-123456abcdef", "pw-immich-fixture-123456abcdef", scenario)
    assert not any(service.get("ports") for service in document["services"].values())
    players = {name: service for name, service in document["services"].items() if name.startswith("player-")}
    assert len(players) == count
    for name, service in players.items():
        assert service["networks"] == ["wall"]
        assert service["volumes"] == [dict(type="volume", source=name, target="/state",
            read_only=False, volume=dict(nocopy=True))]
        assert "environment" not in service
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


def example():
    jobs = [dict(state="ready", variant=dict(sha256=digest, media_type=kind))
            for digest, kind in (("a"*64, "image/jpeg"), ("b"*64, "video/mp4"))]
    events = [dict(output_id="HDMI-A-1", layers=[dict(job["variant"])]) for job in jobs]
    return dict(jobs=jobs, observations=[{}], groups=[dict(status="committed", members={"assignment": ["player-one"]})]), {"player-one": dict(persistence="durable",
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
    assert json.loads((host.state / "demo.json").read_text())["core_images"] == override

    old_state = tmp_path / "old-state"
    old_state.mkdir()
    write_json(old_state / "demo.json", {
        "schema": 1, "project": "pw-wall-demo-123456abcdef", "state": str(old_state),
        "immich_state": str(tmp_path / "fixture"), "wheelhouse": str(wheelhouse),
        "scenario": "baseline", "capture_start": 0,
    })
    old_host = DemoHost(old_state)
    assert old_host.core_images == CORE_IMAGES


def _mock_audit_host(tmp_path, monkeypatch, mutation=None):
    state = tmp_path / "state"
    (state / "contexts/helper").mkdir(parents=True)
    harness = state / "contexts/helper/demo_wall.py"
    harness.write_bytes(b"copied harness")
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
                           "harness_sha256": harness_sha})

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
