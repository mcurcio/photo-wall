"""Control-service authority and network bounds; simulated renderer is not Pi evidence."""

import asyncio
import base64
import contextlib
import hashlib
import inspect
import json
import stat
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError
from test_executor import binding, layer

from contracts.enrollment import Enrollment, enrollment_message
from contracts.models import Commit, Plan, PlayerConfiguration, Revocation
from contracts.time import ManualClock
from player.identity import load_identity
from player.output_discovery import (
    CONFIGURED_OUTPUT_IDS,
    discover_outputs,
    output_app_id,
    weston_ini,
)
from player.rendering import CapacityResult, RecordingRenderer
from player.service import (
    MAX_JSON,
    GLibDispatcher,
    PlayerConfig,
    PlayerService,
    ServiceError,
    State,
    Unauthorized,
    _ChunkBridge,
    _json,
    load_config,
)


def immediate(callback):
    future = Future()
    try:
        future.set_result(callback())
    except Exception as error:
        future.set_exception(error)
    return future


def private_dir(path):
    path.mkdir(mode=0o700)
    return path


def test_identity_is_durable_private_and_signed_without_storing_token(tmp_path):
    state = private_dir(tmp_path / "state")
    first, second = load_identity(state), load_identity(state)
    assert first.persistence == second.persistence == "durable"
    assert first.public_key == second.public_key
    assert stat.S_IMODE((state / "identity.key").stat().st_mode) == 0o600
    assert len((state / "identity.key").read_bytes()) == 32
    proof = first.enrollment("f" * 64, ())
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(first.public_key)).verify(
        base64.b64decode(proof.signature), enrollment_message(proof.nonce, (), "durable"))
    assert sorted(p.name for p in state.iterdir()) == ["identity.key"]
    assert "_key=" not in repr(first)


@pytest.mark.parametrize("fault", ["missing", "directory_mode", "key_mode", "corrupt", "symlink", "directory_symlink"])
def test_identity_storage_failure_preserves_existing_key(tmp_path, fault):
    state = private_dir(tmp_path / "state")
    key = state / "identity.key"
    if fault == "missing":
        state.rmdir()
    elif fault == "directory_mode":
        state.chmod(0o755)
    elif fault in ("key_mode", "corrupt"):
        key.write_bytes(b"bad" if fault == "corrupt" else b"x" * 32)
        key.chmod(0o600 if fault == "corrupt" else 0o644)
    elif fault == "symlink":
        target = tmp_path / "target"
        target.write_bytes(b"unchanged")
        key.symlink_to(target)
    else:
        link = tmp_path / "link"
        link.symlink_to(state, target_is_directory=True)
        state = link
    before = key.read_bytes() if key.exists() else None
    identity = load_identity(state)
    assert identity.persistence == "volatile" and identity.fault == "identity_storage"
    assert identity.enrollment("a" * 64, ()).persistence == "volatile"
    if before is not None:
        assert key.read_bytes() == before
    else:
        assert not key.exists()


@pytest.mark.parametrize("origin", ["http://central", "https://user:secret@central", "https://central/path",
    "https://central?token=x", "https://central#x", "https://central\\evil", "file:///tmp/media",
    "https://central:bad", "https://central:0", "https://central\n"])
def test_config_rejects_untrusted_or_non_origin_urls(tmp_path, origin):
    with pytest.raises(ValidationError):
        PlayerConfig(central_origin=origin, state_dir=str(tmp_path))


def test_config_is_strict_bounded_public_json(tmp_path):
    path = tmp_path / "public.json"
    path.write_text(json.dumps({"schema": 1, "central_origin": "https://central:8443/",
                              "state_dir": str(tmp_path), "cache_bytes": 1048576}))
    assert load_config(path).central_origin == "https://central:8443/"
    for data in ({"unexpected": True}, {"cache_bytes": "1048576"}, {"allow_http": "false"}):
        with pytest.raises(ValidationError):
            PlayerConfig.model_validate({"central_origin": "https://central", "state_dir": str(tmp_path), **data})
    path.write_bytes(b" " * (MAX_JSON + 1))
    with pytest.raises(ServiceError, match="body_limit"):
        load_config(path)
    with pytest.raises(ServiceError):
        _json('{"schema":1,"schema":2}')


def test_drm_ids_are_stable_disconnected_and_do_not_guess_scanout(tmp_path):
    for name, status in (("card1-HDMI-A-2", "disconnected"), ("card1-HDMI-A-1", "connected")):
        path = tmp_path / name
        path.mkdir()
        (path / "status").write_text(status)
        (path / "modes").write_text("3840x2160\n1920x1080\n")
    found = discover_outputs(tmp_path)
    assert found.fault is None
    assert [(o.output_id, o.connected, o.width_px, o.height_px) for o in found.outputs] == [
        ("HDMI-A-1", True, 0, 0), ("HDMI-A-2", False, 0, 0)]
    assert output_app_id(found.outputs[0].output_id) == "photo-wall-HDMI-A-1"
    (tmp_path / "card2-HDMI-A-1").mkdir()
    assert discover_outputs(tmp_path).fault == "output_discovery"
    with pytest.raises(ValueError):
        output_app_id("../../config")


def test_virtual_drm_ids_are_stable_and_use_canonical_routing(tmp_path):
    for name, status in (("card0-Virtual-2", "disconnected"), ("card0-Virtual-1", "connected")):
        path = tmp_path / name
        path.mkdir()
        (path / "status").write_text(status)
    found = discover_outputs(tmp_path)
    assert found.fault is None
    assert [(o.output_id, o.connected, o.width_px, o.height_px) for o in found.outputs] == [
        ("Virtual-1", True, 0, 0), ("Virtual-2", False, 0, 0)]
    assert output_app_id("Virtual-1") == "photo-wall-Virtual-1"


def test_connector_discovery_rejects_mixed_or_duplicate_virtual_outputs(tmp_path):
    for index, name in enumerate(("HDMI-A-1", "Virtual-1", "Virtual-2")):
        path = tmp_path / f"card{index}-{name}"
        path.mkdir()
        (path / "status").write_text("connected")
    assert discover_outputs(tmp_path).fault == "output_discovery"
    duplicate = tmp_path / "duplicate"
    for index in range(2):
        path = duplicate / f"card{index}-Virtual-1"
        path.mkdir(parents=True)
        (path / "status").write_text("connected")
    assert discover_outputs(duplicate).fault == "output_discovery"


def test_connector_routing_and_generated_weston_config_are_bounded():
    assert CONFIGURED_OUTPUT_IDS == ("HDMI-A-1", "HDMI-A-2", "Virtual-1", "Virtual-2")
    for output_id in CONFIGURED_OUTPUT_IDS:
        assert output_app_id(output_id) == "photo-wall-" + output_id
    assert output_app_id("HDMI-A-3") == "photo-wall-HDMI-A-3"
    for invalid in ("Virtual-3", "DP-1", "../../config"):
        with pytest.raises(ValueError):
            output_app_id(invalid)
    config = weston_ini()
    assert config.count("[output]") == 4
    assert all(f"name={output_id}\napp-ids=photo-wall-{output_id}" in config
               for output_id in CONFIGURED_OUTPUT_IDS)


def test_numeric_hdmi_connector_suffix_remains_supported(tmp_path):
    path = tmp_path / "card0-HDMI-A-3"
    path.mkdir()
    (path / "status").write_text("connected")
    found = discover_outputs(tmp_path)
    assert found.fault is None
    assert found.outputs[0].output_id == "HDMI-A-3"


class Server:
    def __init__(self, clock):
        self.clock = clock
        self.epoch = 0
        self.requests = []
        self.proofs = []
        self.state = None
        self.data = b"picture"
        self.media_response = None

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/enrollment/challenge":
            return httpx.Response(200, json={"nonce": "a" * 64, "expires_at": self.clock.utc() + 60})
        if path == "/v1/enrollment/register":
            proof = Enrollment.model_validate_json(request.content)
            self.proofs.append(proof)
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(proof.public_key)).verify(
                base64.b64decode(proof.signature), enrollment_message(proof.nonce, proof.outputs, proof.persistence))
            self.epoch += 1
            self.player_id = "p-" + hashlib.sha256(bytes.fromhex(proof.public_key)).hexdigest()[:32]
            return httpx.Response(200, json={"player_id": self.player_id,
                "authority_epoch": self.epoch, "token": str(self.epoch) * 32})
        if path == "/v1/player/state":
            return httpx.Response(200, json=self.state.model_dump(mode="json"))
        if path.startswith("/v1/media/"):
            return self.media_response or httpx.Response(200, content=self.data,
                headers={"Content-Type": "image/png", "Content-Length": str(len(self.data))})
        return httpx.Response(200, json={"accepted": True})

    def offer(self, *, revision=1, layers=None, commits=(), revocations=(), absent=False):
        configuration = PlayerConfiguration(player_id=self.player_id, authority_epoch=self.epoch,
            configuration_revision=1, bindings=(binding(),), enabled_outputs=("hdmi1",))
        plan = Plan(plan_id="offered", revision=revision, player_id=self.player_id,
            authority_epoch=self.epoch, issued_at=self.clock.utc(), valid_from=self.clock.utc(),
            valid_until=300, bindings=configuration.bindings,
            layers=(layer(data=self.data),) if layers is None else layers)
        self.state = State(configuration=configuration, plan=None if absent else plan,
            commits=commits, revocations=revocations, server_time=self.clock.utc())
        return self.state


async def rig(tmp_path, *, volatile=False, server=None):
    directory = private_dir(tmp_path / "state")
    if volatile:
        directory.chmod(0o755)
    clock = ManualClock(100)
    server = server or Server(clock)
    config = PlayerConfig(central_origin="http://central", allow_http=True, state_dir=str(directory),
                          cache_bytes=1024**2)
    client = httpx.AsyncClient(transport=httpx.MockTransport(server), trust_env=False)
    service = PlayerService(config, load_identity(directory), (), RecordingRenderer(), immediate,
        clock=clock, client=client, websocket_connect=False, health_path=None)
    await service.enroll()
    server.offer()
    await service.poll_state()
    return service, server


async def close(service):
    await service.client.aclose()
    if service.cache:
        service.cache.close()
    service._worker.shutdown(wait=True, cancel_futures=True)


def test_exact_acquisition_readiness_commit_observation_and_absent_plan(tmp_path):
    async def check():
        service, server = await rig(tmp_path)
        try:
            readiness, _ = service._feedback()
            assert readiness.secured == readiness.prepared == ()
            job = service._jobs[0]
            assert await service.download(job)
            service.tick_main()
            readiness, _ = service._feedback()
            assert readiness.prepared == readiness.secured == ("picture",)
            commit = Commit(plan_id="offered", revision=1, authority_epoch=1,
                readiness_sequence=readiness.sequence, assignment_ids=("picture",), committed_at=100)
            server.offer(commits=(commit,))
            await service.poll_state()
            _, observations = service._feedback()
            assert [(o.assignment_id, o.status, o.observed_at) for o in observations] == [("picture", "presented", 100)]
            assert service.renderer.outputs["hdmi1"].layers[0].path.read_bytes() == server.data
            server.offer(absent=True)
            await service.poll_state()
            service._feedback()
            assert not service._jobs and not service._authorized(job)
            assert service.renderer.outputs["hdmi1"].layers[0].layer.assignment_id == "picture"
            assert all(request.url.host == "central" for request in server.requests)
            assert server.requests[-2].headers["authorization"] == "Bearer " + "1" * 32
        finally:
            await close(service)
    asyncio.run(check())


def test_volatile_registration_never_constructs_cache_or_accepts_execution(tmp_path):
    async def check():
        service, server = await rig(tmp_path, volatile=True)
        try:
            assert server.proofs[0].persistence == "volatile"
            assert service.executor is service.cache is None
            assert not (Path(service.config.state_dir) / "cache").exists()
            assert service._feedback() == (None, ())
            assert not service._health()
        finally:
            await close(service)
    asyncio.run(check())


def test_same_key_reenrollment_rotates_authority_and_rejects_old_jobs(tmp_path):
    async def check():
        service, server = await rig(tmp_path)
        try:
            service._feedback()
            old_job, old_state = service._jobs[0], server.state
            await service.enroll()
            server.offer()
            await service.poll_state()
            assert server.proofs[0].public_key == server.proofs[1].public_key
            assert service.registration.authority_epoch == 2
            assert not service._authorized(old_job)
            with pytest.raises(ServiceError, match="state_authority"):
                service._apply_state(old_state)
        finally:
            await close(service)
    asyncio.run(check())


@pytest.mark.parametrize("kind", ["redirect", "oversize", "encoded", "type", "unauthorized", "invalid_json"])
def test_metadata_rejects_unsafe_response_without_following_redirect(tmp_path, kind):
    async def check():
        service, server = await rig(tmp_path)
        try:
            responses = {
                "redirect": httpx.Response(302, headers={"Location": "http://upstream/private"}),
                "oversize": httpx.Response(200, content=b"{}", headers={"Content-Type": "application/json", "Content-Length": str(MAX_JSON + 1)}),
                "encoded": httpx.Response(200, content=b"{}", headers={"Content-Type": "application/json", "Content-Encoding": "x-fixture"}),
                "type": httpx.Response(200, text="{}"),
                "unauthorized": httpx.Response(401),
                "invalid_json": httpx.Response(200, content=b'{"a":NaN}', headers={"Content-Type": "application/json"}),
            }
            await service.client.aclose()
            calls = []
            def handle(request):
                calls.append(request.url)
                return responses[kind]
            service.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            with pytest.raises(Unauthorized if kind == "unauthorized" else ServiceError):
                await service.request("GET", "/v1/player/state")
            assert len(calls) == 1 and calls[0].host == "central"
        finally:
            await close(service)
    asyncio.run(check())


@pytest.mark.parametrize("kind", ["redirect", "truncated", "wrong_hash", "wrong_type", "missing_length"])
def test_failed_download_unblocks_worker_and_never_publishes_readiness(tmp_path, kind):
    async def check():
        service, server = await rig(tmp_path)
        try:
            service._feedback()
            responses = {
                "redirect": httpx.Response(302, headers={"Location": "http://upstream/media"}),
                "truncated": httpx.Response(200, content=b"p", headers={"Content-Type": "image/png", "Content-Length": "7"}),
                "wrong_hash": httpx.Response(200, content=b"corrupt", headers={"Content-Type": "image/png"}),
                "wrong_type": httpx.Response(200, content=b"picture", headers={"Content-Type": "text/plain"}),
                "missing_length": httpx.Response(200, content=b"picture", headers={"Content-Type": "image/png"}),
            }
            if kind == "missing_length":
                del responses[kind].headers["Content-Length"]
            server.media_response = responses[kind]
            with context_optional_error():
                assert not await asyncio.wait_for(service.download(service._jobs[0]), 2)
            service.tick_main()
            assert service._feedback()[0].secured == ()
            # Prove the sole media worker is free, rather than just inspecting a flag.
            assert await asyncio.get_running_loop().run_in_executor(service._worker, lambda: 17) == 17
        finally:
            await close(service)
    asyncio.run(check())


class context_optional_error:
    def __enter__(self):
        return self

    def __exit__(self, kind, error, traceback):
        return kind is not None and issubclass(kind, ServiceError)


def test_queue_backpressure_cancellation_unblocks_producer_and_consumer():
    async def check():
        bridge = _ChunkBridge(lambda: True)
        for _ in range(4):
            await bridge.put(b"x" * 65536)
        pending = asyncio.create_task(bridge.put(b"y"))
        await asyncio.sleep(.02)
        assert not pending.done() and bridge.queue.qsize() == 4
        bridge.stopped.set()
        with pytest.raises(ServiceError):
            await asyncio.wait_for(pending, .2)
        with pytest.raises(ServiceError):
            next(bridge.chunks())
    asyncio.run(check())


def test_cancel_during_http_response_releases_download_worker(tmp_path):
    async def check():
        service, server = await rig(tmp_path)
        try:
            service._feedback()
            entered = asyncio.Event()
            async def waiting(request):
                entered.set()
                await asyncio.Event().wait()
            await service.client.aclose()
            service.client = httpx.AsyncClient(transport=httpx.MockTransport(waiting))
            task = asyncio.create_task(service.download(service._jobs[0]))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            assert await asyncio.get_running_loop().run_in_executor(service._worker, lambda: "free") == "free"
            assert not service._feedback()[0].secured
        finally:
            await close(service)
    asyncio.run(check())


def test_revocation_and_full_plan_omission_cancel_jobs_before_draw(tmp_path):
    async def check():
        service, server = await rig(tmp_path)
        try:
            service._feedback()
            job = service._jobs[0]
            revoked = Revocation(plan_id="offered", revision=1, authority_epoch=1,
                                 sequence=1, assignment_ids=("picture",), mode="cancel")
            server.offer(revocations=(revoked,))
            await service.poll_state()
            assert not service._authorized(job)
            service._feedback()
            assert not service._jobs
            server.offer(revision=2, layers=())
            await service.poll_state()
            assert not service._authorized(job)
            assert not service.renderer.outputs["hdmi1"].layers
        finally:
            await close(service)
    asyncio.run(check())


def test_bad_clock_measurement_withholds_preparation_and_health(tmp_path):
    async def check():
        service, server = await rig(tmp_path)
        try:
            service._feedback()
            assert await service.download(service._jobs[0])
            server.state = server.state.model_copy(update={"server_time": 110})
            await service.poll_state()
            readiness, _ = service._feedback()
            assert not readiness.prepared and not service._health()
            assert readiness.clock_uncertainty == 10
            server.offer()
            await service.poll_state()
            assert service.mapping.healthy()
        finally:
            await close(service)
    asyncio.run(check())


@pytest.mark.parametrize("stage", ["json", "schema"])
@pytest.mark.parametrize("delay,healthy", [(.2, True), (1.1, False)])
def test_clock_transport_sample_excludes_local_parsing_but_bounds_its_age(
        tmp_path, monkeypatch, stage, delay, healthy):
    async def check():
        from player import service as module
        service, _ = await rig(tmp_path)
        try:
            owner, attribute = (module, "_json") if stage == "json" else (State, "model_validate")
            original = getattr(owner, attribute)
            def delayed(value):
                parsed = original(value)
                service.clock.advance(delay)
                return parsed
            monkeypatch.setattr(owner, attribute, delayed)
            await service.poll_state()
            assert service.mapping.healthy() is healthy
            assert service.mapping.uncertainty == (0 if healthy else 86400)
        finally:
            await close(service)
    asyncio.run(check())


def test_clock_transport_sample_includes_delayed_body_receipt(tmp_path):
    async def check():
        service, server = await rig(tmp_path)
        class DelayedBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                body = server.state.model_dump_json().encode()
                yield body[:20]
                service.clock.advance(.2)
                yield body[20:]
        def respond(request):
            assert request.method == "GET" and request.url.path == "/v1/player/state"
            return httpx.Response(200, headers={"Content-Type": "application/json"},
                                  stream=DelayedBody())
        try:
            await service.client.aclose()
            service.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            await service.poll_state()
            assert service.mapping.uncertainty == pytest.approx(.2)
            assert not service.mapping.healthy()
        finally:
            await close(service)
    asyncio.run(check())


@pytest.mark.parametrize("fault", ["dispatch_delay", "parse_clock_step"])
def test_clock_receipt_sample_rejects_stale_dispatch_and_post_receipt_step(tmp_path, monkeypatch, fault):
    async def check():
        from player import service as module
        service, _ = await rig(tmp_path)
        try:
            if fault == "dispatch_delay":
                original = service.dispatch
                async def dispatch(callback):
                    service.clock.advance(1.1)
                    return await original(callback)
                monkeypatch.setattr(service, "dispatch", dispatch)
            else:
                original = module._json
                def parse(value):
                    parsed = original(value)
                    service.clock.step_utc(.02)
                    return parsed
                monkeypatch.setattr(module, "_json", parse)
            await service.poll_state()
            assert service.mapping.uncertainty == 86400
            assert not service.mapping.healthy()
        finally:
            await close(service)
    asyncio.run(check())


def test_glib_dispatch_is_bounded_ordered_and_cancellation_skips_work():
    class GLib:
        callbacks = []
        @classmethod
        def idle_add(cls, callback):
            cls.callbacks.append(callback)
    dispatcher = GLibDispatcher(GLib)
    seen = []
    futures = [dispatcher(lambda n=n: seen.append(n)) for n in range(4)]
    assert isinstance(dispatcher(lambda: None).exception(), ServiceError)
    futures[1].cancel()
    for callback in GLib.callbacks:
        callback()
    assert seen == [0, 2, 3]
    fifth = dispatcher(lambda: seen.append(4))
    GLib.callbacks[-1]()
    assert fifth.done() and seen == [0, 2, 3, 4]


def test_health_has_boot_authority_and_no_secrets(tmp_path):
    async def check():
        service, server = await rig(tmp_path)
        try:
            service.health_path = tmp_path / "health.json"
            service.boot_id = "00000000-1111-2222-3333-444444444444"
            service._write_health(*service._health_status())
            body = json.loads(service.health_path.read_text())
            assert body["healthy"] and body["authority_epoch"] == 1
            assert body["health_reason"] == "healthy"
            assert body["sampled_monotonic"] == 0 and body["boot_id"] == service.boot_id
            assert "token" not in body and "public_key" not in body
            assert stat.S_IMODE(service.health_path.stat().st_mode) == 0o600
            service._write_health(False)
            body = json.loads(service.health_path.read_text())
            assert not body["healthy"] and body["health_reason"] == "disconnected"
            service.boot_id = ""
            service._write_health(True)
            body = json.loads(service.health_path.read_text())
            assert not body["healthy"] and body["health_reason"] == "identity"
        finally:
            await close(service)
    asyncio.run(check())


@pytest.mark.parametrize("reason", ["executor", "identity", "configuration", "clock",
                                    "renderer_capacity", "healthy"])
def test_health_reason_preserves_gate_order_and_single_evaluation(tmp_path, monkeypatch, reason):
    async def check():
        service, _ = await rig(tmp_path)
        try:
            calls = []
            def clock():
                calls.append("clock")
                return reason != "clock"
            def capacity(_):
                calls.append("renderer_capacity")
                return CapacityResult(reason == "healthy")
            monkeypatch.setattr(service.mapping, "healthy", clock)
            monkeypatch.setattr(service.renderer, "capacity", capacity)
            if reason == "executor":
                service.executor = None
            if reason in {"executor", "identity"}:
                service.identity = replace(service.identity, persistence="volatile")
            if reason in {"executor", "identity", "configuration"}:
                service._configuration = None
            assert service._health_status() == (reason == "healthy", reason)
            expected = ([] if reason in {"executor", "identity", "configuration"}
                        else ["clock"] if reason == "clock" else ["clock", "renderer_capacity"])
            assert calls == expected
            calls.clear()
            assert service._health() is (reason == "healthy")
            assert calls == expected
        finally:
            await close(service)
    asyncio.run(check())


@pytest.mark.parametrize("healthy,reason", [(True, "clock"), (False, "healthy"),
                                            (False, "private-error"), (False, "")])
def test_health_writer_rejects_inconsistent_or_unbounded_reasons(tmp_path, healthy, reason):
    async def check():
        service, _ = await rig(tmp_path)
        try:
            service.health_path = tmp_path / "health.json"
            service.boot_id = "00000000-1111-2222-3333-444444444444"
            with pytest.raises(ValueError, match="inconsistent health reason"):
                service._write_health(healthy, reason)
            assert not service.health_path.exists()
        finally:
            await close(service)
    asyncio.run(check())


@pytest.mark.parametrize("reason", ["healthy", "clock"])
def test_control_writes_one_consistent_health_sample_consumed_by_vm_probe(tmp_path, monkeypatch, reason):
    from scripts.vm_health_probe import sample_once
    async def check():
        service, _ = await rig(tmp_path)
        try:
            service.health_path = tmp_path / "health.json"
            service.boot_id = "00000000-1111-2222-3333-444444444444"
            calls = []
            async def poll():
                service._stop.set()
            def status():
                calls.append(reason)
                return reason == "healthy", reason
            monkeypatch.setattr(service, "poll_state", poll)
            monkeypatch.setattr(service, "_feedback", lambda: (None, ()))
            monkeypatch.setattr(service, "_health_status", status)
            await service._control_loop()
            assert calls == [reason]
            value = sample_once(1, service.boot_id, service.clock.monotonic(),
                report_reader=lambda: json.loads(service.health_path.read_text()),
                systemctl_runner=lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=""),
                socket_state=lambda: "present")
            assert value["report_status"] == "present"
            assert value["health_reason"] == reason
            assert value["healthy"] is (reason == "healthy")
            assert value["identity_valid"] and value["current_boot"] and value["sample_age"] == "fresh"
        finally:
            await close(service)
    asyncio.run(check())


def test_health_default_is_player_private_path():
    default = inspect.signature(PlayerService).parameters["health_path"].default
    assert default == Path("/run/photo-wall/player/service-health.json")


def test_existing_identity_on_unwritable_volume_reuses_key_but_reports_volatile(tmp_path, monkeypatch):
    directory = private_dir(tmp_path / "state")
    first = load_identity(directory)
    original = (directory / "identity.key").read_bytes()
    def fail(_):
        raise OSError("read-only filesystem")
    monkeypatch.setattr("player.identity._probe_storage", fail)
    second = load_identity(directory)
    assert second.public_key == first.public_key
    assert second.persistence == "volatile"
    assert (directory / "identity.key").read_bytes() == original


def test_cache_initialization_failure_reenrolls_same_key_as_volatile(tmp_path):
    async def check():
        directory = private_dir(tmp_path / "state")
        clock = ManualClock(100)
        server = Server(clock)
        def fail(*_):
            raise OSError("full volume")
        service = PlayerService(PlayerConfig(central_origin="http://central", allow_http=True,
            state_dir=str(directory)), load_identity(directory), (), RecordingRenderer(), immediate,
            clock=clock, client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
            cache_factory=fail, health_path=None)
        try:
            with pytest.raises(ServiceError, match="identity_storage"):
                await service.enroll()
            assert service.cache is service.executor is service.registration is None
            await service.enroll()
            assert [proof.persistence for proof in server.proofs] == ["durable", "volatile"]
            assert server.proofs[0].public_key == server.proofs[1].public_key
        finally:
            await close(service)
    asyncio.run(check())


def test_delayed_glib_clock_sample_is_unhealthy(tmp_path):
    async def check():
        service, server = await rig(tmp_path)
        try:
            sample = (.01, service.clock.utc(), service.clock.monotonic())
            service.clock.advance(2)
            service._apply_state(server.state, sample)
            assert not service.mapping.healthy()
            server.offer(revision=2)
            await service.poll_state()
            assert service.mapping.healthy()
        finally:
            await close(service)
    asyncio.run(check())


@pytest.mark.parametrize("damage", ["delete", "corrupt"])
def test_worker_verification_reopens_acquisition_after_cache_damage(tmp_path, damage):
    async def check():
        service, server = await rig(tmp_path)
        worker = None
        try:
            service._feedback()
            job = service._jobs[0]
            assert await service.download(job)
            service.tick_main()
            assert service._feedback()[0].secured == ("picture",)
            path = service.cache.path_for(job.layer.variant)
            if damage == "delete":
                path.unlink()
            else:
                path.write_bytes(b"corrupt")
            worker = asyncio.create_task(service._media_loop())
            async def invalidated():
                while service._feedback()[0].secured:
                    await asyncio.sleep(.01)
            await asyncio.wait_for(invalidated(), 2)
            # Stop scheduling while checking the now-visible new acquisition job.
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            service._feedback()
            assert service._jobs == (job,)
            assert await service.download(job)
            service.tick_main()
            assert service._feedback()[0].secured == ("picture",)
            assert service.cache.path_for(job.layer.variant).read_bytes() == server.data
        finally:
            if worker:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            await close(service)
    asyncio.run(check())


def test_websocket_has_explicit_bounds_and_rejects_oversized_state(tmp_path):
    async def check():
        service, server = await rig(tmp_path)
        calls = []
        class Socket:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                pass
            async def __aiter__(self):
                yield "x" * (MAX_JSON + 1)
        def connect(uri, **options):
            calls.append((uri, options))
            return Socket()
        service.websocket_connect = connect
        try:
            with pytest.raises(ServiceError, match="body_limit"):
                await service._websocket_loop()
            uri, options = calls[0]
            assert uri == "ws://central/v1/player/session"
            assert options["max_size"] == MAX_JSON and options["max_queue"] == 4
            assert options["proxy"] is options["compression"] is None
            assert options["additional_headers"]["Authorization"] == "Bearer " + "1" * 32
        finally:
            await close(service)
    asyncio.run(check())


def test_running_service_reenrolls_on_401_and_shutdown_clears_token(tmp_path, monkeypatch):
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)
    async def check():
        service, server = await rig(tmp_path)
        seen = asyncio.Event()
        unauthorized = True
        def handle(request):
            nonlocal unauthorized
            if request.url.path == "/v1/player/state" and unauthorized:
                unauthorized = False
                return httpx.Response(401)
            result = server(request)
            if request.url.path == "/v1/enrollment/register":
                server.offer()
            if request.url.path == "/v1/player/readiness" and server.epoch == 2:
                seen.set()
            return result
        await service.client.aclose()
        service.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(seen.wait(), 3)
            assert server.proofs[0].public_key == server.proofs[1].public_key
            assert service._configuration.authority_epoch == 2
        finally:
            service.stop()
            await asyncio.wait_for(task, 3)
            await service.client.aclose()
        assert service.registration is None
    asyncio.run(check())


def test_retired_key_refusal_never_rotates_identity(tmp_path, monkeypatch):
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)
    async def check():
        service, server = await rig(tmp_path)
        service.registration = None
        keys = []
        seen = asyncio.Event()
        def handle(request):
            keys.append(json.loads(request.content)["public_key"])
            if len(keys) == 3:
                seen.set()
            return httpx.Response(403, json={"error": "retired_player"})
        await service.client.aclose()
        service.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(seen.wait(), 3)
            assert len(set(keys)) == 1 and keys[0] == service.identity.public_key
        finally:
            service.stop()
            await asyncio.wait_for(task, 3)
            await service.client.aclose()
    asyncio.run(check())


def test_observation_renewal_race_fetches_current_state_without_reenrollment(tmp_path):
    async def check():
        service, server = await rig(tmp_path)
        task = None
        try:
            service._feedback()
            assert await service.download(service._jobs[0])
            service.tick_main()
            readiness, _ = service._feedback()
            commit = Commit(plan_id="offered", revision=1, authority_epoch=1,
                readiness_sequence=readiness.sequence, assignment_ids=("picture",), committed_at=100)
            server.offer(commits=(commit,))
            await service.poll_state()
            service._outgoing.extend(service._feedback()[1])
            seen = asyncio.Event()
            def handle(request):
                if request.url.path == "/v1/player/observations":
                    server.offer(revision=2)
                    return httpx.Response(409, json={"error": "stale_observation"})
                result = server(request)
                if request.url.path == "/v1/player/state":
                    seen.set()
                return result
            await service.client.aclose()
            service.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            task = asyncio.create_task(service._observation_loop())
            await asyncio.wait_for(seen.wait(), 2)
            await asyncio.sleep(.01)
            assert service._plan.revision == 2 and not task.done()
            assert service.registration.authority_epoch == 1
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await close(service)
    asyncio.run(check())


def test_real_central_http_media_commit_outage_rejoin_and_renewal(registry, tmp_path):
    """Loopback TCP + PostgreSQL + actual gateway bytes; RecordingRenderer only."""
    import socket
    import threading
    import time

    import uvicorn
    from fastapi.responses import JSONResponse
    from test_coordination import schedule
    from test_media_store import RECIPE, VARIANT, staged
    from test_registry import ADMIN, frame

    from central.app import create_app
    from central.media_repository import MediaRepository, StoreLimits
    from central.media_store import MediaStore
    from contracts.enrollment import OutputReport
    from contracts.models import Calibration
    from media.models import SourceSpec

    repository = MediaRepository(registry.db, registry.clock, StoreLimits(
        max_bytes=10000, max_original_bytes=1000, max_image_bytes=1000, max_video_bytes=2000))
    repository.set_recipe(RECIPE)
    storage = MediaStore(repository, tmp_path / "central-media")
    with storage.worker_lock():
        lease, _, prepared = staged(storage)
        repository.configure_source(SourceSpec(source_ref="library:1", connection_ref="fixture"))
        with repository.transaction() as conn:
            conn.execute("UPDATE media_sources SET status='ok'")
            conn.execute("INSERT INTO source_members VALUES('library:1',%s)", (lease.asset.asset_id,))
        storage.publish(lease, prepared)
    app = create_app(registry.db, registry.clock, ADMIN, media_root=storage.root)
    unavailable = False

    @app.middleware("http")
    async def outage(request, call_next):
        if unavailable and request.url.path.startswith("/v1/player"):
            return JSONResponse({"error": "fixture_outage"}, status_code=503)
        return await call_next(request)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]))
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started

    async def check():
        nonlocal unavailable
        state_dir = private_dir(tmp_path / "player-state")
        client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
        service = PlayerService(PlayerConfig(central_origin=origin, allow_http=True,
            state_dir=str(state_dir)), load_identity(state_dir),
            (OutputReport(output_id="HDMI-A-1", width_px=0, height_px=0),),
            RecordingRenderer(), immediate, clock=registry.clock, client=client, health_path=None)
        socket_task = None
        try:
            await service.enroll()
            registered = service.registration
            await service.poll_state()
            assert service._plan is None
            frame(registry, "portrait")
            registry.bind("portrait", registered.player_id, "HDMI-A-1", expected_generation=0)
            registry.calibrate("portrait", "commit", 1, Calibration(), expected_generation=1)
            schedule(app.state.coordinator, ["portrait"], starts=1000, media=True)
            await service.poll_state()
            service._feedback()
            offered = service._jobs[0]
            assert await service.download(offered)
            service.tick_main()
            readiness, _ = service._feedback()
            assert offered.layer.assignment_id in readiness.prepared
            assert await service.request("POST", "/v1/player/readiness",
                body=readiness.model_dump(mode="json")) == {"accepted": True}
            await service.poll_state()
            _, observations = service._feedback()
            assert observations and observations[0].status == "presented"
            await service.request("POST", "/v1/player/observations",
                                  body=observations[0].model_dump(mode="json"))
            assert service.cache.path_for(prepared.variant).read_bytes() == VARIANT
            # Exercise the actual websockets client and central endpoint too.
            socket_task = asyncio.create_task(service._websocket_loop())
            await asyncio.sleep(.15)
            assert not socket_task.done()
            socket_task.cancel()
            await asyncio.gather(socket_task, return_exceptions=True)
            socket_task = None
            unavailable = True
            with pytest.raises(ServiceError, match="http_503"):
                await service.poll_state()
            service.tick_main()
            assert service.renderer.outputs["HDMI-A-1"].layers
            unavailable = False
            # The old observation races a real Plan renewal and is rejected.
            # Complete-cycle extent may span adjacent 30-second horizon quanta.
            registry.clock.advance(61)
            app.state.coordinator.advance()
            latest = app.state.coordinator.delivery(registered.player_id, 1)["plan"]
            assert latest.revision > service._plan.revision
            service._outgoing.append(observations[0])
            old_revision = service._plan.revision
            sender = asyncio.create_task(service._observation_loop())
            try:
                async def renewed():
                    while service._plan.revision == old_revision:
                        if sender.done():
                            sender.result()
                            pytest.fail("observation sender ended")
                        await asyncio.sleep(.01)
                await asyncio.wait_for(renewed(), 3)
                assert not sender.done()
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            await service.enroll()
            assert service.registration.player_id == registered.player_id
            assert service.registration.authority_epoch == registered.authority_epoch + 1
            old = await client.get(origin + "/v1/player/state",
                                   headers={"Authorization": "Bearer " + registered.token})
            assert old.status_code == 401
            app.state.coordinator.advance()
            await service.poll_state()
            service._feedback()
            assert service._configuration.authority_epoch == 2
            assert not service._authorized(offered)
        finally:
            if socket_task:
                socket_task.cancel()
                await asyncio.gather(socket_task, return_exceptions=True)
            await close(service)

    try:
        asyncio.run(check())
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()
