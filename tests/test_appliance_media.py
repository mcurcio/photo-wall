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
