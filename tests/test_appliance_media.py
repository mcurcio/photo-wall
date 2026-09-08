"""Resource and source boundaries of the VM media extension."""

import pytest

from scripts.appliance_media import ApplianceMedia
from scripts.boot_fixture import FixtureError
from scripts.vm_media_probe import VM_PHOTO_FRAME, ProbeError, configure


class Operator:
    def __init__(self):
        self.calls = []
        self.player = dict(id="p1", device_id="device1", authority_epoch=1,
                           registered_at=100, last_seen=100, retired_at=None, health={})
        self.outputs = [dict(player_id="p1", output_id="Virtual-1",
                             observation=dict(output_id="Virtual-1", connected=True,
                                              width_px=1280, height_px=720))]

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return dict(players=[self.player], outputs=self.outputs, frames=[])
        return {}


@pytest.mark.parametrize("width,height", [(0, 0), (1280, 720), (720, 1280), (1080, 1080), (0, 1080)])
def test_configuration_authors_frame_independently_of_output_dimensions(width, height):
    from central.registry import FrameCreate

    operator = Operator()
    operator.outputs[0]["observation"].update(width_px=width, height_px=height)
    result = configure(operator, player_id="p1", epoch=1, captured_from=100,
                       captured_until=101, now=lambda: 200)
    assert result.output_id == "Virtual-1" and result.starts_at == 290
    source = operator.calls[1][2]
    assert source["connection_ref"] == "fixture-library" and source["media_types"] == ["image"]
    assert (source["captured_from"], source["captured_until"]) == (100, 101)
    frame = FrameCreate.model_validate(operator.calls[2][2])
    assert frame == VM_PHOTO_FRAME == result.authored_frame
    assert frame.profile.width_px == 1920 and frame.profile.height_px == 1080
    assert frame.width_mm / frame.height_mm == pytest.approx(16 / 9)
    assert result.observed_output.width_px == width and result.observed_output.height_px == height
    assert operator.outputs[0]["observation"] == result.observed_output.model_dump(mode="json")
    assert operator.calls[-1][2]["starts_at"] > 200


@pytest.mark.parametrize("begin,end", [(None, None), (100, 102), (101, 100), (float("nan"), 101)])
def test_invalid_capture_cannot_broaden_fixture_query(begin, end):
    operator = Operator()
    with pytest.raises(ValueError):
        configure(operator, player_id="p1", epoch=1, captured_from=begin, captured_until=end)
    assert operator.calls == []


@pytest.mark.parametrize("fault", ["epoch", "retired", "wrong_player", "missing_output", "other_owner",
                                  "disconnected", "mismatched_output_identity"])
def test_no_configuration_writes_without_current_session_and_owned_output(fault):
    operator = Operator()
    if fault == "epoch":
        operator.player["authority_epoch"] = 2
    elif fault == "retired":
        operator.player["retired_at"] = 100
    elif fault == "wrong_player":
        operator.player["id"] = "another-player"
    elif fault == "other_owner":
        operator.outputs[0]["player_id"] = "another-player"
    elif fault == "disconnected":
        operator.outputs[0]["observation"]["connected"] = False
    elif fault == "mismatched_output_identity":
        operator.outputs[0]["observation"]["output_id"] = "Virtual-2"
    else:
        operator.outputs = []
    with pytest.raises(ProbeError):
        configure(operator, player_id="p1", epoch=1, captured_from=100, captured_until=101)
    assert all(method == "GET" for method, _, _ in operator.calls)


@pytest.mark.parametrize("health", [
    {}, {"boot_id": "current-boot", "ticket_id": "current-ticket"},
    {"persistence": "durable", "healthy": False, "authority_epoch": 0},
    {"persistence": "volatile", "healthy": False, "sampled_monotonic": 0},
])
def test_current_session_configuration_is_independent_of_observational_health(health):
    operator = Operator()
    operator.player["health"] = health
    result = configure(operator, player_id="p1", epoch=1, captured_from=100,
                       captured_until=101, now=lambda: 200)
    assert result.player_id == "p1" and result.authority_epoch == 1
    assert operator.calls[-1][1] == "/v1/operator/programs/vm-photo"


def test_configuration_binds_connected_owned_output_without_mode_preference():
    operator = Operator()
    operator.outputs[0]["observation"].update(width_px=0, height_px=0)
    operator.outputs += [dict(player_id="p1", output_id="Virtual-2",
        observation=dict(output_id="Virtual-2", connected=True, width_px=1920, height_px=1080)),
        dict(player_id="another-player", output_id="HDMI-A-1",
        observation=dict(output_id="HDMI-A-1", connected=True, width_px=3840, height_px=2160))]
    result = configure(operator, player_id="p1", epoch=1, captured_from=100, captured_until=101)
    assert result.output_id == "Virtual-1"
    assert result.observed_output.width_px == result.observed_output.height_px == 0
    operator = Operator()
    operator.outputs[0]["observation"]["connected"] = False
    operator.outputs += [dict(player_id="p1", output_id="Virtual-2",
        observation=dict(output_id="Virtual-2", connected=True, width_px=0, height_px=0))]
    result = configure(operator, player_id="p1", epoch=1, captured_from=100, captured_until=101)
    assert result.output_id == "Virtual-2"


def test_worker_image_mismatch_fails_before_state_creation(tmp_path):
    from scripts.test_appliance_e2e import ApplianceE2E

    state = tmp_path / "state"
    with pytest.raises(FixtureError, match="worker_image_manifest_mismatch"):
        ApplianceE2E({"worker_image": "sha256:" + "a" * 64}, state,
                     "sha256:" + "c" * 64, "sha256:" + "d" * 64, worker_image="sha256:" + "b" * 64)
    assert not state.exists()


def test_full_scope_requires_the_exact_media_worker_before_state_creation(tmp_path):
    from scripts.test_appliance_e2e import ApplianceE2E

    state = tmp_path / "state"
    with pytest.raises(FixtureError, match="full_worker_required"):
        ApplianceE2E({"worker_image": None}, state,
                     "sha256:" + "c" * 64, "sha256:" + "d" * 64, scope="full")
    assert not state.exists()


def test_upstream_reuses_exact_loaded_image_instead_of_rebuilding(tmp_path, monkeypatch):
    from scripts.immich_fixture import FixtureHost

    host = FixtureHost.create(tmp_path / "upstream")
    calls = []
    digest = "sha256:" + "a" * 64

    def command(args, **kwargs):
        calls.append(args)
        return digest if args[1:3] == ["image", "inspect"] else ""

    monkeypatch.setattr(host, "_command", command)
    host.build(base_image=digest)
    assert calls[1] == ["docker", "tag", digest, host.project + "-base:local"]
    assert calls[-1][-4:] == ["build", "--builder", "default", "central-probe"]
    assert not any(args[:2] == ["docker", "build"] for args in calls)


def test_upstream_rejects_changed_image_before_tagging(tmp_path, monkeypatch):
    from scripts.immich_fixture import FixtureHost, HarnessError

    host = FixtureHost.create(tmp_path / "upstream")
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        return "sha256:" + "b" * 64

    monkeypatch.setattr(host, "_command", command)
    with pytest.raises(HarnessError, match="fixture_base_changed"):
        host.build(base_image="sha256:" + "a" * 64)
    assert len(calls) == 1




@pytest.mark.parametrize('delivered', [False, True])
def test_native_rehydration_requires_delivery_from_the_current_boot(delivered):
    from types import SimpleNamespace

    from scripts.vm_cache_evidence import CacheEvidenceError

    digest = 'a'*64
    before = dict(player_id='p', authority_epoch=1, sha256=digest, size=100)
    after = before | dict(authority_epoch=2)
    boot = dict(boot_id='old', device_id='device-'+'b'*64, persistence='volatile')
    calls = []
    def run(args, **kwargs):
        import json

        calls.append(args)
        return json.dumps(dict(event='photo-wall-fixture-media-attempt', sha256=digest,
                               authenticated=True, player_id='p',
                               authority_epoch=2 if delivered else 1, status_class='2xx')).encode()
    harness = SimpleNamespace(report=dict(media=dict(presentations={'fresh': before}, rehydration={}),
        boots=[boot, boot | dict(boot_id='new')]), run=run, boot_started_at='current-boot-start',
        fixture_central=lambda: 'central')
    media = ApplianceMedia(harness, 'sha256:'+'a'*64)
    if delivered:
        media.verify_rehydration('after_restart', after)
        assert harness.report['media']['rehydration']['after_restart']['delivery_observed']
    else:
        with pytest.raises(CacheEvidenceError, match='media_rehydration_unproven'):
            media.verify_rehydration('after_restart', after)
        assert not harness.report['media']['rehydration']
    assert calls[0][2:4] == ['--since', 'current-boot-start']


def test_media_probe_consumes_the_typed_equipment_session():
    import json
    from types import SimpleNamespace

    from central.installation_models import EquipmentSessionObservation

    player = EquipmentSessionObservation(player_id='p-'+'a'*32, device_id='device-'+'b'*64,
                                         authority_epoch=2, retired=False)
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return json.dumps({'ok': True, 'value': {'presentations': [], 'grants': []}}).encode()
    harness = SimpleNamespace(run=run, fixture_central=lambda: 'central', report={})
    assert ApplianceMedia(harness, 'image').probe('evidence', player) == dict(presentations=[], grants=[])
    assert calls[0][-4:] == ['--player-id', player.player_id, '--epoch', '2']


def test_process_restart_records_exact_cache_and_authority_evidence(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from central.installation_models import EquipmentSessionObservation

    before = EquipmentSessionObservation(player_id="p-" + "a" * 32,
        device_id="device-" + "b" * 64, authority_epoch=1, retired=False)
    after = before.model_copy(update={"authority_epoch": 2})
    proof = dict(frame_id="frame", output_id="out", run_id="run", sha256="c" * 64, size=10,
                 original_sha256="d" * 64, player_id=before.player_id, authority_epoch=2)
    state = tmp_path / "state"
    (state / "vm/share").mkdir(parents=True)
    boot_id = "01234567-89ab-cdef-0123-456789abcdef"
    harness = SimpleNamespace(state=state, report={"boots": [{"boot_id": boot_id}],
        "media": {"presentations": {}, "rehydration": {}}},
        wait_session_replacement=lambda previous: after)
    media = ApplianceMedia(harness, "image")
    monkeypatch.setattr(media, "wait_presentation", lambda *args, **kwargs: proof)
    monkeypatch.setattr(media, "delivery_attempts", lambda since, value: [])
    monkeypatch.setattr(media, "wait_control_event", lambda request: None)
    monkeypatch.setattr(media, "probe", lambda *args: {"old_session_current": False, "valid_grants": 0})

    assert media.restart_player("valid_reuse", before, proof, action="restart",
                                delivery_expected=False) == after
    request = json.loads((state / "vm/share/player-control.json").read_text())
    assert request == dict(schema=1, revision=1, boot_id=boot_id, player_id=before.player_id,
                           prior_epoch=1, action="restart", sha256=None)
    assert harness.report["media"]["process_restarts"]["valid_reuse"]["old_session_rejected"]


@pytest.mark.parametrize("attempts,changed_run,error", [
    ([{"authenticated": True, "status_class": "4xx", "player_id": "p-" + "a" * 32,
       "authority_epoch": 2}], False, "cache_delivery_expectation_failed"),
    ([{"authenticated": True, "status_class": "2xx", "player_id": "p-" + "a" * 32,
       "authority_epoch": 1}], False, "cache_delivery_expectation_failed"),
    ([], True, "secured_selection_changed"),
])
def test_process_restart_rejects_missing_delivery_wrong_epoch_or_changed_run(
        tmp_path, monkeypatch, attempts, changed_run, error):
    from types import SimpleNamespace

    from central.installation_models import EquipmentSessionObservation

    before = EquipmentSessionObservation(player_id="p-" + "a" * 32,
        device_id="device-" + "b" * 64, authority_epoch=1, retired=False)
    after = before.model_copy(update={"authority_epoch": 2})
    baseline = dict(frame_id="frame", output_id="out", run_id="run", sha256="c" * 64,
                    size=10, original_sha256="d" * 64, player_id=before.player_id,
                    authority_epoch=1)
    proof = baseline | {"authority_epoch": 2, "run_id": "changed" if changed_run else "run"}
    state = tmp_path / "state"
    (state / "vm/share").mkdir(parents=True)
    harness = SimpleNamespace(state=state, report={"boots": [{"boot_id":
        "01234567-89ab-cdef-0123-456789abcdef"}], "media": {}},
        wait_session_replacement=lambda previous: after)
    media = ApplianceMedia(harness, "image")
    monkeypatch.setattr(media, "wait_control_event", lambda request: None)
    monkeypatch.setattr(media, "wait_presentation", lambda *args, **kwargs: proof)
    monkeypatch.setattr(media, "delivery_attempts", lambda *args: attempts)
    monkeypatch.setattr(media, "probe", lambda *args: {"old_session_current": False, "valid_grants": 0})
    with pytest.raises(FixtureError, match=error):
        media.restart_player("fault", before, baseline, action="delete", delivery_expected=True)


def test_control_event_precedes_accepting_a_spontaneous_new_epoch(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from central.installation_models import EquipmentSessionObservation

    before = EquipmentSessionObservation(player_id="p-" + "a" * 32,
        device_id="device-" + "b" * 64, authority_epoch=1, retired=False)
    state = tmp_path / "state"
    (state / "vm/share").mkdir(parents=True)
    accepted = []
    harness = SimpleNamespace(state=state, report={"boots": [{"boot_id":
        "01234567-89ab-cdef-0123-456789abcdef"}]},
        wait_session_replacement=lambda previous: accepted.append(previous))
    media = ApplianceMedia(harness, "image")
    monkeypatch.setattr(media, "wait_control_event",
                        lambda request: (_ for _ in ()).throw(FixtureError("player_control_event_missing")))
    with pytest.raises(FixtureError, match="player_control_event_missing"):
        media.restart_player("reuse", before, {"sha256": "c" * 64},
                             action="restart", delivery_expected=False)
    assert accepted == []
