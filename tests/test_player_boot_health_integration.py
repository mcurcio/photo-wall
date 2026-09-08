"""Real Player/HTTP/PostgreSQL clock-health boundary; no appliance or renderer proof.

The RTT/offset pairs are individual retained probes from full run 34188891575,
not a consecutive probe trace. Their recurrence below is explicitly constructed
to exercise interrupted central dwell; it does not establish the hosted cause.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import uuid
from concurrent.futures import Future

import httpx
import pytest

from central.app import create_app
from contracts.enrollment import OutputReport
from player.identity import load_identity
from player.rendering import RecordingRenderer
from player.service import BootContext, PlayerConfig, PlayerService
from scripts.vm_release_contract import ReleaseEvidenceResult, decode_release_result
from scripts.vm_release_probe import evidence

# e2e-report.json SHA256 d1e03025d31df4ba758f3f81d765f3d2816e7740367b0175dff0c113794fefe9,
# health_diagnostics["69b7119b-94cd-48ad-985b-a0f549f51115"][13/14].clock.
# Keep the measured RTT/offset only; dispatch delay and microsecond clock drift
# are intentionally absent from this controllable-clock transport model.
BAD_PROBE = (0.13671164100003352, 0.034253835678100586)
GOOD_PROBE = (0.0682146130000092, -0.002229928970336914)


def immediate(callback):
    future = Future()
    try:
        future.set_result(callback())
    except Exception as error:
        future.set_exception(error)
    return future


class ReceivedBody(httpx.AsyncByteStream):
    def __init__(self, inner, clock, delay):
        self.inner, self.clock, self.delay = inner, clock, delay

    async def __aiter__(self):
        # The Player's real receipt timestamp must include this body delay.
        self.clock.advance(self.delay)
        async for chunk in self.inner:
            yield chunk

    async def aclose(self):
        await self.inner.aclose()


class ProbeTimingTransport(httpx.AsyncBaseTransport):
    """Run actual handlers/authentication; advance only time-request boundaries."""

    def __init__(self, app, clock):
        self.inner, self.clock = httpx.ASGITransport(app=app), clock
        self.probe = GOOD_PROBE
        self.health = []

    async def handle_async_request(self, request):
        if request.url.path == "/v1/player/boot-health":
            body = json.loads(request.content)
            self.health.append((body["healthy"], body["observed_at"]))
        if request.url.path != "/v1/player/time":
            return await self.inner.handle_async_request(request)
        rtt, offset = self.probe
        before, after = rtt / 2 + offset, rtt / 2 - offset
        assert before >= 0 and after >= 0
        self.clock.advance(before)
        response = await self.inner.handle_async_request(request)
        response.stream = ReceivedBody(response.stream, self.clock, after)
        return response

    async def aclose(self):
        await self.inner.aclose()


def test_observed_probe_rejection_interrupts_central_dwell_until_full_healthy_recovery(
        registry, tmp_path):
    """Constructed 10-second recurrence starves unchanged 30-second acceptance."""
    authority, clock = registry.release_authority, registry.clock
    assert (authority.health_seconds, authority.health_max_age) == (30, 2)
    app = create_app(db=registry.db, clock=clock, release_authority=authority,
                     admin_token="integration-only-admin-" + "x" * 32, run_scheduler=False)
    transport = ProbeTimingTransport(app, clock)

    async def check():
        async with httpx.AsyncClient(transport=transport) as client:
            device_id, boot_id = "device-" + "e" * 64, str(uuid.uuid4())
            response = await client.post("http://central/v1/bootstrap/boot", json=dict(
                device_id=device_id, boot_id=boot_id, request_id="f" * 48))
            assert response.status_code == 200
            ticket = response.json()
            context = BootContext.model_validate(dict(schema=2, device_id=device_id,
                boot_id=boot_id, ticket_id=ticket["ticket_id"], release_id=ticket["release_id"],
                trial=ticket["trial"], persistence="volatile"))
            cache = tmp_path / "cache"
            cache.mkdir(mode=0o700)
            service = PlayerService(PlayerConfig(central_origin="http://central", allow_http=True,
                cache_dir=str(cache)), load_identity(),
                (OutputReport(output_id="HDMI-A-1", width_px=0, height_px=0),),
                RecordingRenderer(), immediate, clock=clock, client=client, time_client=client,
                boot_context=context, health_path=None)
            try:
                await service.enroll()
                assert service.registration.authority_epoch == 1
                assert (service.mapping.max_uncertainty, service.mapping.max_step,
                        service.mapping.max_age) == (.1, .25, 30)

                def state():
                    with registry.db.transaction() as conn:
                        conn.execute("SET TRANSACTION READ ONLY")
                        attempt = conn.execute(
                            "SELECT healthy_since,health_received_at,health_observed_at,status "
                            "FROM appliance_boot_attempts WHERE ticket_id=%s",
                            (ticket["ticket_id"],)).fetchone()
                        value = evidence(conn, device_id, boot_id)
                    # Exercise the actual public producer/host serialized boundary.
                    result = decode_release_result("evidence", ReleaseEvidenceResult(
                        schema_version=1, kind="release-evidence", evidence=value).model_dump_json().encode())
                    value = result.evidence
                    assert value.current and value.device_id == device_id and value.boot_id == boot_id
                    assert value.current_player_id == service.registration.player_id
                    assert value.current_authority_epoch == service.registration.authority_epoch
                    assert value.release_id == ticket["release_id"]
                    assert value.ticket_sha256 == hashlib.sha256(ticket["ticket_id"].encode()).hexdigest()
                    return attempt, value

                async def control():
                    await service.poll_state()
                    healthy, reason = service._health_status()
                    await service._report_boot_health(healthy)
                    assert transport.health[-1] == (healthy, clock.utc())
                    attempt, value = state()
                    assert attempt["health_received_at"] == attempt["health_observed_at"] == clock.utc()
                    return healthy, reason, attempt, value

                start = clock.monotonic()
                rejected, previous = 0, None
                # Probe each second and report each half-second, matching the
                # production cadences while advancing a manual clock. One
                # observed bad pair recurs every 10 seconds; this cadence is
                # constructed, not inferred from the retained hosted snapshots.
                for tick in range(120):
                    clock.advance(start + tick * .5 - clock.monotonic())
                    bad = tick % 20 == 18
                    if bad:
                        assert service.mapping.healthy() and previous["healthy_since"] is not None
                    if tick % 2 == 0:
                        transport.probe = BAD_PROBE if bad else GOOD_PROBE
                        assert await service.probe_time() is not bad
                        diagnostic = service.mapping.diagnostics
                        rtt, offset = transport.probe
                        assert diagnostic.rtt == pytest.approx(rtt, abs=1e-10)
                        assert diagnostic.central_offset == pytest.approx(offset, abs=1e-10)
                        assert diagnostic.uncertainty == pytest.approx(rtt / 2 + abs(offset))
                    healthy, reason, attempt, value = await control()
                    if bad:
                        rejected += 1
                        assert diagnostic.status == "uncertainty"
                        assert not service.mapping.healthy() and math.isinf(service.mapping.uncertainty)
                        assert not healthy and reason == "clock"
                        assert attempt["healthy_since"] is None
                    elif tick % 2 == 0:
                        assert healthy and attempt["healthy_since"] is not None
                    assert attempt["status"] == value.status == "booting"
                    assert not service.release_accepted
                    previous = attempt
                assert rejected == 6 and clock.monotonic() - start > 30

                # The last bad probe cleared dwell. Good observed measurements
                # can recover only after a new uninterrupted full 30 seconds.
                recovery, since = start + 60, None
                for tick in range(62):
                    clock.advance(recovery + tick * .5 - clock.monotonic())
                    if tick % 2 == 0:
                        transport.probe = GOOD_PROBE
                        assert await service.probe_time()
                    healthy, reason, attempt, value = await control()
                    assert healthy and reason == "healthy"
                    if since is None:
                        assert attempt["healthy_since"] == clock.utc()
                    since = attempt["healthy_since"] if since is None else since
                    assert attempt["healthy_since"] == since
                    elapsed = clock.utc() - since
                    if elapsed < 30:
                        assert not service.release_accepted and value.status == "booting"
                    else:
                        assert service.release_accepted and value.status == "healthy"
                        assert value.accepted_release_id == ticket["release_id"]
                        break
                else:
                    pytest.fail("unchanged central dwell did not accept healthy recovery")
                assert 30 <= elapsed < 30.5
            finally:
                if service.cache is not None:
                    service.cache.close()
                service._worker.shutdown(wait=True, cancel_futures=True)

    asyncio.run(check())
