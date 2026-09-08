"""Bounded Docker regression driver for actual Player clock and boot health.

Uses a RecordingRenderer; this is neither a VM nor native-rendering proof.
Only public session identities and bounded measurements leave the process.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import ssl
import time
import uuid
from concurrent.futures import Future
from dataclasses import asdict
from pathlib import Path

import httpx

from contracts.enrollment import OutputReport
from player.identity import load_identity
from player.rendering import RecordingRenderer
from player.service import BootContext, PlayerConfig, PlayerService

WINDOW_SECONDS = 75


def dispatch(callback):
    future = Future()
    try:
        future.set_result(callback())
    except Exception as exc:
        future.set_exception(exc)
    return future


def record(target, value):
    if len(target) >= 400:
        raise ValueError("measurement_bound")
    target.append(value)


async def main():
    output = Path("/out")
    source = Path(__file__).parent
    manifest = json.loads((source / "sources.json").read_bytes())
    observed = {path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in source.rglob("*.py")}
    if observed != manifest:
        raise ValueError("clock_client_sources_changed")
    context = ssl.create_default_context(cafile="/public/ca.pem")
    probes, responses = [], []
    async with httpx.AsyncClient(verify=context, trust_env=False, timeout=15,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2)) as client, \
            httpx.AsyncClient(verify=context, trust_env=False, timeout=15,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1)) as time_client:
        device, boot = "device-" + secrets.token_hex(32), str(uuid.uuid4())
        selected = await client.post("https://photo-wall.test/v1/bootstrap/boot", json=dict(
            device_id=device, boot_id=boot, request_id=secrets.token_hex(24)))
        selected.raise_for_status()
        ticket = selected.json()
        service = PlayerService(PlayerConfig(central_origin="https://photo-wall.test",
            ca_file="/public/ca.pem"), load_identity(),
            (OutputReport(output_id="HDMI-A-1", width_px=1920, height_px=1080),),
            RecordingRenderer(), dispatch, client=client, time_client=time_client,
            health_path=None, boot_context=BootContext.model_validate(dict(schema=2,
                ticket_id=ticket["ticket_id"], device_id=device, boot_id=boot,
                release_id=ticket["release_id"], trial=ticket["trial"], persistence="volatile")))
        tasks = []
        try:
            await service.enroll()
            await service.poll_state()
            original_probe, original_request = service.probe_time, service.request

            async def probe():
                accepted = await original_probe()
                record(probes, dict(elapsed=time.monotonic() - started,
                    returned=accepted, **asdict(service.mapping.diagnostics)))
                return accepted

            async def request(method, path, **kwargs):
                result = await original_request(method, path, **kwargs)
                if path == "/v1/player/boot-health":
                    record(responses, dict(elapsed=time.monotonic() - started,
                        sent_healthy=kwargs["body"]["healthy"], **result))
                return result

            service.probe_time, service.request = probe, request
            session = service.registration
            (output / "session.json").write_text(json.dumps(dict(device_id=device, boot_id=boot,
                player_id=session.player_id, authority_epoch=session.authority_epoch)))
            deadline = time.monotonic() + 60
            while not (output / "start").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("measurement_start")
                await asyncio.sleep(.1)
            started = time.monotonic()
            tasks = [asyncio.create_task(service._time_loop()), asyncio.create_task(service._control_loop())]
            await asyncio.sleep(WINDOW_SECONDS)
            service._stop.set()
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            faults = [type(value).__name__ for value in results if isinstance(value, BaseException)
                      and not isinstance(value, asyncio.CancelledError)]
            result = json.dumps(dict(elapsed=time.monotonic() - started,
                probes=probes, health_responses=responses, release_accepted=service.release_accepted,
                task_faults=faults, constants=dict(max_uncertainty=service.mapping.max_uncertainty,
                    max_step=service.mapping.max_step, max_age=service.mapping.max_age),
                source_files=observed), allow_nan=False)
            if len(result.encode()) > 1024 * 1024:
                raise ValueError("result_bound")
            print(result, flush=True)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            service._worker.shutdown(wait=True)


if __name__ == "__main__":
    asyncio.run(main())
