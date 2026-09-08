"""Real HTTP/SQL/Player evidence join; generated JPEG and RecordingRenderer only.

This invokes the real publication transaction with explicitly synthetic build
identity. It does not run an upstream server, conversion worker, or native GTK.
"""

import asyncio
import contextlib
import hashlib
import io
import json
import socket
import threading
import time
import uuid
from types import SimpleNamespace

import httpx
import uvicorn
from media_queue import RecordingMediaQueue
from PIL import Image
from test_player_service import close, immediate
from test_registry import ADMIN

from central.app import create_app
from central.catalog import CatalogSnapshot
from central.installation_models import EquipmentSessionObservation
from central.media_repository import MediaRepository, StoreLimits
from central.media_store import MediaStore
from contracts.enrollment import OutputReport
from contracts.models import Variant
from media.models import OriginalAsset, RefreshResult
from media.prepare import BuildIdentity, PreparedMedia
from player.identity import load_identity
from player.rendering import RecordingRenderer
from player.service import BootContext, PlayerConfig, PlayerService
from scripts import boot_gateway
from scripts import vm_media_probe as probe
from scripts.appliance_media import ApplianceMedia


def test_real_photo_presentation_reaches_exact_worker_original_host_evidence(
        registry, tmp_path, monkeypatch):
    clock = registry.clock
    clock.advance(time.time() - clock.utc())
    repository = MediaRepository(registry.db, clock, StoreLimits(
        max_bytes=100000, max_original_bytes=10000, max_image_bytes=10000,
        max_video_bytes=20000), queue=RecordingMediaQueue())
    repository.set_recipe("e" * 64)
    storage = MediaStore(repository, tmp_path / "central-media")
    app = create_app(registry.db, clock, ADMIN, run_scheduler=False,
        media_root=storage.root, release_authority=registry.release_authority,
        media_queue=RecordingMediaQueue())
    deliveries, delivery_records = [], []
    log_offset = 0

    def delivery_record(value, *, flush):
        assert flush and len(value.encode()) < 1024
        delivery_records.append(value)

    monkeypatch.setattr(boot_gateway, "print", delivery_record, raising=False)
    boot_gateway.install_delivery_observer(app)

    @app.middleware("http")
    async def delivered(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/v1/media/"):
            deliveries.append((request.method, request.url.path, response.status_code))
        return response

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
    operator = object.__new__(probe.Operator)
    operator.client = httpx.Client(base_url=origin, trust_env=False,
                                   headers={"Authorization": "Bearer " + ADMIN})
    operator.close = lambda: None
    monkeypatch.setattr(probe, "Operator", lambda: operator)
    monkeypatch.setenv("PHOTO_WALL_DATABASE_URL", registry.db.dsn)
    serialized, calls = [], []

    def execute(args, **kwargs):
        if args[:2] == ["docker", "logs"]:
            assert args == ["docker", "logs", "--since", "integration-window", "central"]
            # Substitute only Docker's log transport. These lines came from
            # the actual observer and real current-session authentication.
            return ("\n".join(delivery_records[log_offset:]) + "\n").encode()
        assert args[:6] == ["docker", "exec", "observer", "python", "-m", "scripts.vm_media_probe"]
        calls.append(args[6:])
        assert len(calls) < 16, "bounded host wait did not find the actual presentation"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            probe.main(args[6:])
        payload = output.getvalue().encode()
        serialized.append(payload)
        return payload

    harness = SimpleNamespace(report={"media": {"presentations": {}}},
        fixture_observer=lambda: "observer", fixture_central=lambda: "central",
        checked_vm=lambda: {"Running": True}, run=execute)
    media = ApplianceMedia(harness, "synthetic-worker-build")
    original_buffer, variant_buffer = io.BytesIO(), io.BytesIO()
    Image.new("RGB", (108, 192), (35, 93, 161)).save(original_buffer, format="JPEG", quality=95)
    Image.new("RGB", (108, 192), (35, 93, 161)).save(variant_buffer, format="JPEG", quality=90)
    original, derivative = original_buffer.getvalue(), variant_buffer.getvalue()
    original_sha256 = hashlib.sha256(original).hexdigest()
    media.photo = {"captured": "1970-01-01T00:16:40Z", "sha256": original_sha256}

    async def check():
        nonlocal log_offset
        client = httpx.AsyncClient(trust_env=False)
        service = None
        try:
            device_id, boot_id = "device-" + "f" * 64, str(uuid.uuid4())
            boot = await client.post(origin + "/v1/bootstrap/boot", json=dict(
                device_id=device_id, boot_id=boot_id, request_id="d" * 48))
            assert boot.status_code == 200
            ticket = boot.json()
            private_values = [ADMIN, ticket["ticket_id"]]
            context = BootContext.model_validate(dict(schema=2, device_id=device_id,
                boot_id=boot_id, ticket_id=ticket["ticket_id"], release_id=ticket["release_id"],
                trial=ticket["trial"], persistence="volatile"))
            cache = tmp_path / "player-cache"
            cache.mkdir(mode=0o700)
            service = PlayerService(PlayerConfig(central_origin=origin, allow_http=True,
                cache_dir=str(cache)), load_identity(),
                (OutputReport(output_id="HDMI-A-1", width_px=0, height_px=0),),
                RecordingRenderer(), immediate, clock=clock, client=client,
                time_client=client, health_path=None, boot_context=context)
            await service.enroll()
            session = service.registration
            private_values.append(session.token)
            current = EquipmentSessionObservation(player_id=session.player_id,
                authority_epoch=session.authority_epoch, device_id=device_id, retired=False)
            media.configure(current)

            # The actual source refresh contract creates membership; publication
            # verifies both original and derivative bytes and records provenance.
            refresh = repository.begin_scheduled_refresh()
            asset = OriginalAsset(connection_id="fixture-library", upstream_id=str(uuid.UUID(int=1)),
                original_sha1=hashlib.sha1(original).hexdigest(), kind="image", raw_width=108,
                raw_height=192, orientation=1, captured_at=1000, file_size=len(original))
            assert repository.publish_refresh(refresh, RefreshResult(snapshot=CatalogSnapshot(
                source_ref=probe.SOURCE, refreshed_at=clock.utc(), candidates=(asset.candidate,)), assets=(asset,)))
            projection = app.state.coordinator.advance()
            assert repository.request_acquisitions(projection.acquisitions) == 1
            with storage.worker_lock():
                lease = repository.claim_job()
                assert lease is not None and lease.asset == asset
                paths = storage.staging(lease)
                paths.original.write_bytes(original)
                paths.variant.write_bytes(derivative)
                variant = Variant(sha256=hashlib.sha256(derivative).hexdigest(), size=len(derivative),
                    media_type="image/jpeg", width=108, height=192)
                build = BuildIdentity(preparation_sha256="a" * 64, ffmpeg_sha256="b" * 64,
                    ffprobe_sha256="c" * 64, ffmpeg_version="synthetic", ffprobe_version="synthetic",
                    python_version="synthetic", pillow_version="synthetic", littlecms_version="synthetic",
                    jpeg_version="synthetic", zlib_version="synthetic", platform="synthetic",
                    memory_limit_enforced=False)
                prepared = PreparedMedia(path=paths.variant, variant=variant,
                    original_sha256=original_sha256, recipe_id="e" * 64, build=build)
                assert storage.publish(lease, prepared) == variant
            clock.advance(media.setup["starts_at"] - 1 - clock.utc())
            app.state.coordinator.advance()
            assert await service.probe_time()
            await service.poll_state()
            service._feedback()
            assert service._jobs
            offered = service._jobs[0]
            assert offered.layer.variant == variant
            assert await service.download(offered)
            service.tick_main()
            readiness, _ = service._feedback()
            assert offered.layer.assignment_id in readiness.prepared
            assert await service.request("POST", "/v1/player/readiness",
                body=readiness.model_dump(mode="json")) == {"accepted": True}
            await service.poll_state()
            clock.advance(media.setup["starts_at"] + .1 - clock.utc())
            service.tick_main()
            _, observations = service._feedback()
            assert observations and observations[0].status == "presented"
            await service.request("POST", "/v1/player/observations",
                                  body=observations[0].model_dump(mode="json"))
            assert service.cache.path_for(variant).read_bytes() == derivative
            result = media.wait_presentation("fresh", current)
            assert result["sha256"] == variant.sha256 and result["original_sha256"] == original_sha256
            assert result["assignment_id"] == observations[0].assignment_id
            assert result["player_id"] == session.player_id and result["authority_epoch"] == 1
            assert deliveries == [("GET", "/v1/media/" + variant.sha256, 200)]
            expected_delivery = dict(event="photo-wall-fixture-media-attempt", sha256=variant.sha256,
                authenticated=True, status_class="2xx", player_id=session.player_id, authority_epoch=1)
            assert media.delivery_attempts("integration-window", variant.sha256) == [expected_delivery]
            assert media.probe("evidence", current, "--output-id", "HDMI-A-1",
                "--original-sha256", "0" * 64)["presentations"] == []

            # Each process gets a fresh key and current central epoch; only
            # public media bytes survive. Rejoin the same active Run and join
            # its real new observation through the exact helper/host gate.
            for action in ("surviving", "deleted", "corrupt"):
                old_session, old_key = service.registration, service.identity.public_key
                cache_path = service.cache.path_for(variant)
                await close(service)
                service = None
                if action == "deleted":
                    cache_path.unlink()
                elif action == "corrupt":
                    cache_path.write_bytes(b"x" * len(derivative))
                prior_deliveries = len(deliveries)
                log_offset = len(delivery_records)
                client = httpx.AsyncClient(trust_env=False)
                service = PlayerService(PlayerConfig(central_origin=origin, allow_http=True,
                    cache_dir=str(cache)), load_identity(),
                    (OutputReport(output_id="HDMI-A-1", width_px=0, height_px=0),),
                    RecordingRenderer(), immediate, clock=clock, client=client,
                    time_client=client, health_path=None, boot_context=context)
                assert service.identity.public_key != old_key
                await service.enroll()
                session = service.registration
                private_values.append(session.token)
                assert session.player_id == old_session.player_id
                assert session.authority_epoch == old_session.authority_epoch + 1
                old_request = await client.get(origin + "/v1/player/state",
                    headers={"Authorization": "Bearer " + old_session.token})
                assert old_request.status_code == 401
                current = EquipmentSessionObservation(player_id=session.player_id,
                    authority_epoch=session.authority_epoch, device_id=device_id, retired=False)
                app.state.coordinator.advance()
                assert await service.probe_time()
                await service.poll_state()
                service._feedback()
                assert len(service._jobs) <= 4
                for job in service._jobs:
                    assert await service.download(job)
                service.tick_main()
                readiness, _ = service._feedback()
                assert result["assignment_id"] in readiness.prepared
                assert await service.request("POST", "/v1/player/readiness",
                    body=readiness.model_dump(mode="json")) == {"accepted": True}
                await service.poll_state()
                _, observations = service._feedback()
                presented = next(value for value in observations if value.status == "presented")
                await service.request("POST", "/v1/player/observations",
                                      body=presented.model_dump(mode="json"))
                latest = media.wait_presentation(action, current, verify_reboot=False)
                assert all(latest[key] == result[key] for key in (
                    "frame_id", "output_id", "run_id", "assignment_id", "sha256", "size", "original_sha256"))
                assert latest["authority_epoch"] == session.authority_epoch
                assert media.probe("stale", current, "--prior-epoch", str(old_session.authority_epoch)) == {
                    "old_session_current": False, "valid_grants": 0}
                assert service.cache.path_for(variant).read_bytes() == derivative
                expected_count = 0 if action == "surviving" else 1
                assert deliveries[prior_deliveries:] == [(
                    "GET", "/v1/media/" + variant.sha256, 200)] * expected_count
                assert media.delivery_attempts("integration-window", variant.sha256) == [
                    expected_delivery | {"authority_epoch": session.authority_epoch}] * expected_count
            assert all(value.encode() not in payload for value in private_values for payload in serialized)
            assert all(value not in line for value in private_values for line in delivery_records)
            (tmp_path / "presentation.json").write_text(json.dumps(dict(
                presentations=harness.report["media"]["presentations"], deliveries=deliveries,
                delivery_records=[json.loads(line) for line in delivery_records]), sort_keys=True))
        finally:
            if service is not None:
                await close(service)
            else:
                await client.aclose()

    try:
        asyncio.run(check())
    finally:
        operator.client.close()
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        assert not thread.is_alive()
