"""Resource and source boundaries of the VM media extension."""

from types import SimpleNamespace

import pytest

from scripts.appliance_media import ApplianceMedia
from scripts.boot_fixture import FixtureError
from scripts.vm_media_probe import ProbeError, configure


class Operator:
    def __init__(self):
        self.calls = []
        self.player = dict(id="p1", authority_epoch=1, retired_at=None, health={"persistence": "durable"})
        self.outputs = [dict(player_id="p1", output_id="Virtual-1",
                             observation=dict(connected=True, width_px=1280, height_px=720))]

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return dict(players=[self.player], outputs=self.outputs)
        return {}


@pytest.mark.parametrize("width,height", [(1280, 720), (720, 1280), (1080, 1080)])
def test_configuration_uses_real_output_and_one_second_image_query(width, height):
    from central.registry import FrameCreate

    operator = Operator()
    operator.outputs[0]["observation"].update(width_px=width, height_px=height)
    result = configure(operator, player_id="p1", epoch=1, captured_from=100,
                       captured_until=101, now=lambda: 200)
    assert result["output_id"] == "Virtual-1" and result["starts_at"] == 290
    source = operator.calls[1][2]
    assert source["connection_ref"] == "fixture-library" and source["media_types"] == ["image"]
    assert (source["captured_from"], source["captured_until"]) == (100, 101)
    frame = FrameCreate.model_validate(operator.calls[2][2])
    assert frame.profile.width_px == width and frame.profile.height_px == height
    assert frame.width_mm / frame.height_mm == pytest.approx(width / height)
    assert operator.calls[-1][2]["starts_at"] > 200


@pytest.mark.parametrize("begin,end", [(None, None), (100, 102), (101, 100), (float("nan"), 101)])
def test_invalid_capture_cannot_broaden_fixture_query(begin, end):
    operator = Operator()
    with pytest.raises(ValueError):
        configure(operator, player_id="p1", epoch=1, captured_from=begin, captured_until=end)
    assert operator.calls == []


@pytest.mark.parametrize("fault", ["epoch", "retired", "volatile", "missing_output"])
def test_no_configuration_writes_without_current_durable_player(fault):
    operator = Operator()
    if fault == "epoch":
        operator.player["authority_epoch"] = 2
    elif fault == "retired":
        operator.player["retired_at"] = 100
    elif fault == "volatile":
        operator.player["health"]["persistence"] = "volatile"
    else:
        operator.outputs = []
    with pytest.raises(ProbeError):
        configure(operator, player_id="p1", epoch=1, captured_from=100, captured_until=101)
    assert all(method == "GET" for method, _, _ in operator.calls)


def test_replaced_probe_never_prevents_independent_upstream_cleanup():
    media = ApplianceMedia(SimpleNamespace(), "sha256:" + "a" * 64)
    removed = []
    media.upstream = SimpleNamespace(cleanup=lambda: removed.append("upstream"))

    def replaced():
        raise FixtureError("cache_probe_identity_changed")

    media.cleanup_probe = replaced
    with pytest.raises(FixtureError, match="media_resource_cleanup_failed"):
        media.down()
    assert removed == ["upstream"]


def test_cache_probe_refuses_running_vm_before_creating_any_resource():
    harness = SimpleNamespace(checked_vm=lambda: {"Running": True})
    media = ApplianceMedia(harness, "sha256:" + "a" * 64)
    with pytest.raises(FixtureError, match="cache_probe_requires_stopped_vm"):
        media.verify_cache("before_restart")


def test_worker_image_mismatch_fails_before_state_creation(tmp_path):
    from scripts.test_appliance_e2e import ApplianceE2E

    state = tmp_path / "state"
    with pytest.raises(FixtureError, match="worker_image_manifest_mismatch"):
        ApplianceE2E({"worker_image": "sha256:" + "a" * 64}, state,
                     "sha256:" + "c" * 64, "sha256:" + "d" * 64, worker_image="sha256:" + "b" * 64)
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


@pytest.mark.parametrize("erase_restart,erase_rollback", [(False, False), (True, False), (False, True)])
def test_cache_qualification_cannot_reacquire_lost_bytes(tmp_path, monkeypatch, erase_restart, erase_rollback):
    from scripts import test_appliance_e2e as e2e

    platform = dict(running=True, epoch=0, cached=False, acquisitions=0, blocked=False)
    harness = object.__new__(e2e.ApplianceE2E)
    harness.state, harness.name, harness.central_image = tmp_path, "vm", "image"
    harness.inputs = dict(bundle=tmp_path, deployment=tmp_path)
    harness.report = dict(boots=[], checks={}, qualification=e2e.unqualified())
    def player():
        return dict(player_id="p-" + "b" * 32, authority_epoch=platform["epoch"], persistence="durable", retired=False)
    harness.inventory = lambda: [player()] if platform["epoch"] else []
    def enroll(previous=None):
        platform["epoch"] += 1
        harness.report["boots"].append(dict(slot="A", trial=False))
        return player()
    harness.wait_enrollment = enroll
    harness.checked_vm = lambda: dict(Running=platform["running"])
    def run(args, **kwargs):
        if args[:2] == ["docker", "stop"] and args[-1] == "vm":
            platform["running"] = False
        if args[:2] == ["docker", "start"] and args[-1] == "vm":
            platform["running"] = True
            if erase_restart:
                platform["cached"] = False
        return b""
    harness.run = run
    harness.fixture_central = lambda: "central"
    harness.start_vm = harness.wait_trial_acceptance = lambda: None
    harness.wait_player_requests = lambda _: None
    def rollback(previous):
        if erase_rollback:
            platform["cached"] = False
        harness.report["fallback_enrollment"] = enroll(previous)
    harness.exercise_rollback = rollback
    class Media:
        def prepare(self):
            return {}, tmp_path / "connections"
        def network_denial(self, label):
            pass
        def configure(self, player):
            pass
        def wait_presentation(self, label, player):
            if not platform["cached"]:
                if platform["blocked"]:
                    raise FixtureError("native_photo_presentation_timeout")
                platform["acquisitions"] += 1
                platform["cached"] = True
        def verify_cache(self, label):
            assert not platform["running"] and platform["cached"]
        def block_delivery(self):
            assert not platform["running"]
            platform["blocked"] = True
    harness.media = Media()
    monkeypatch.setattr(e2e.BootFixture, "prepare", lambda *a, **kw: SimpleNamespace(up=lambda: None))
    monkeypatch.setattr(e2e.time, "sleep", lambda _: None)
    if erase_restart or erase_rollback:
        with pytest.raises(FixtureError, match="native_photo_presentation_timeout"):
            harness.execute()
        assert not any(harness.report["qualification"].values())
        assert "populated_cache_survives_restart_and_rollback" not in harness.report["checks"]
    else:
        harness.execute()
        assert harness.report["checks"]["populated_cache_survives_restart_and_rollback"] is True
    assert platform["acquisitions"] == 1
