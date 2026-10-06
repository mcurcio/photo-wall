"""R9 over real sockets: the Player's PlayerService names every network failure of its run
loop as `<cause>_<reason>` (player/service.py `_fault_for`), with the console detail plus the
clock summary for TIME/TLS-untrusted failures. These tests never inject a fake transport for
the failure itself -- a real socket (a closed port, an unresolvable name, a mismatched TLS
leaf) produces the classified error, exercising `Exchange.name`/`classify` end to end."""

import asyncio
import logging

import httpx
import pytest
import tls_fixture as tls
from fastapi import FastAPI
from test_player_service import Server, _until_steady, boot_context, close, immediate
from test_uplink_diagnosis import RECORD
from test_uplink_transport import closed_port
from uplink_fakes import finding

from contracts.time import ManualClock
from player import central_link
from player.identity import load_identity
from player.rendering import RecordingRenderer
from player.service import PlayerConfig, PlayerService
from uplink.trust import Trust


@pytest.fixture
def trust(tmp_path) -> Trust:
    return Trust.public(tls.write_bundle(tmp_path / "ca.pem", tls.CA))


@pytest.fixture
def cache_dir(tmp_path):
    directory = tmp_path / "cache"
    directory.mkdir(mode=0o700)
    return directory


async def _wait_for_fault(service, timeout: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while service.last_fault is None:
        assert loop.time() < deadline, "no fault observed within the timeout"
        await asyncio.sleep(.01)


async def _run_to_fault(service, timeout: float = 15.0) -> None:
    """asyncio.create_task(service.run()); waits for last_fault; stops and closes the session,
    whether or not the wait succeeded."""
    task = asyncio.create_task(service.run())
    try:
        await _wait_for_fault(service, timeout)
    finally:
        service.stop()
        await asyncio.wait_for(task, timeout)
        await close(service)


def _service(*, find_central, trust, cache_dir=None, client=None, time_client=None,
            clock=None, clock_record=None):
    kwargs = dict(cache_dir=str(cache_dir)) if cache_dir is not None else {}
    return PlayerService(
        PlayerConfig(**kwargs), load_identity(), (), RecordingRenderer(), immediate,
        find_central=find_central, trust=trust, client=client, time_client=time_client,
        clock=clock, clock_record=clock_record or (lambda: RECORD),
        websocket_connect=False, health_path=None, boot_context=boot_context())


# --- AC2: R9's fault table, over a real socket for every case ----------------------------

@pytest.mark.parametrize("kind", ["tls_untrusted", "time_not_yet_valid", "connect_refused",
                                   "dns_failed"])
def test_player_faults_are_named(tmp_path, monkeypatch, trust, cache_dir, kind):
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)

    async def check(origin):
        service = _service(find_central=finding(origin), trust=trust, cache_dir=cache_dir)
        await _run_to_fault(service)
        return service.last_fault, service.last_fault_detail

    if kind == "tls_untrusted":
        with tls.serve_tls(FastAPI(), tls.FOREIGN) as port:
            code, detail = asyncio.run(check(f"https://localhost:{port}"))
        assert code == "tls_untrusted"
        assert "verify_code=" in detail
        assert "clock=unsynced floor=" in detail
    elif kind == "time_not_yet_valid":
        with tls.serve_tls(FastAPI(), tls.NOT_YET_VALID) as port:
            code, detail = asyncio.run(check(f"https://localhost:{port}"))
        assert code == "time_not_yet_valid"
        assert "clock=unsynced floor=" in detail
    elif kind == "connect_refused":
        code, detail = asyncio.run(check(f"http://127.0.0.1:{closed_port()}"))
        assert code == "connect_refused"
        assert "clock=" not in detail
    else:
        code, detail = asyncio.run(check("http://photo-wall.invalid"))
        assert code == "dns_failed"


def test_central_own_error_is_named(monkeypatch, trust, cache_dir):
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)

    def handle(request):
        return httpx.Response(503, json={"error": "app_unconfigured"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False)
    service = _service(find_central=finding("http://central"), trust=trust, cache_dir=cache_dir,
                       client=client, time_client=client)

    asyncio.run(_run_to_fault(service))
    assert service.last_fault == "central_error"


class _BrokenBody(httpx.AsyncByteStream):
    """Answers with a partial JSON object, then breaks mid-body -- a transfer failure, not a
    connect one (the status line was already read)."""

    async def __aiter__(self):
        yield b"{"
        raise httpx.RemoteProtocolError("truncated mid body")


def test_a_transfer_failure_is_named_transfer(monkeypatch, trust, cache_dir):
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)

    def handle(request):
        return httpx.Response(200, headers={"content-type": "application/json"},
                              stream=_BrokenBody())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False)
    service = _service(find_central=finding("http://central"), trust=trust, cache_dir=cache_dir,
                       client=client, time_client=client)

    asyncio.run(_run_to_fault(service))
    assert service.last_fault == "transfer_short"


# --- AC4: a redirect on a direct request relocates the session ---------------------------

def test_a_redirect_is_followed_by_a_new_locate(monkeypatch, caplog, trust):
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)
    caplog.set_level(logging.WARNING, logger="photo_wall.player")
    find = finding("http://central")

    async def check():
        clock = ManualClock(100)
        server = Server(clock)
        steady = asyncio.Event()
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            _until_steady(server, steady, redirect_first_state=True)), trust_env=False)
        service = _service(find_central=find, trust=trust, clock=clock,
                           client=client, time_client=client)
        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(steady.wait(), 10)
            assert find.calls == 2
            assert len(server.proofs) == 1
        finally:
            service.stop()
            await asyncio.wait_for(task, 10)
            await close(service)

    asyncio.run(check())
    assert any("redirect_unexpected" in message for message in caplog.messages)


# --- AC5: run() builds both its httpx clients from the one Trust -------------------------

def test_the_run_loop_builds_both_clients_from_the_one_trust(monkeypatch, trust, cache_dir):
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)
    calls = []
    real_central_http_client = central_link.central_http_client

    def recorder(passed_trust, *, connections, keepalive):
        calls.append((passed_trust, connections, keepalive))
        return real_central_http_client(passed_trust, connections=connections,
                                        keepalive=keepalive)

    monkeypatch.setattr("player.central_link.central_http_client", recorder)
    service = _service(find_central=finding(f"http://127.0.0.1:{closed_port()}"), trust=trust,
                       cache_dir=cache_dir)

    asyncio.run(_run_to_fault(service))
    assert calls == [(trust, 4, 2), (trust, 1, 1)]
