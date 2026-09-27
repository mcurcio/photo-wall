"""PR #28 fix set, Player side (decision 0014): the bearer is bound to the origin that issued
it (M1/U8), CentralLink owns every network guard and names every failure (R4c), the cmdline
root wins before any saved value is read (R4a/U3), and the Player owns its deadline through
systemd's watchdog (M5). Seeded from the review's bearer-leak probe and surviving mutants."""

import asyncio
import errno
import json
import logging
import sys
from pathlib import Path

import httpx
import pytest

from player.central_link import REQUEST_TIMEOUT, Session
from player.mdns_discovery import DEFAULT_TIMEOUT as MDNS_TIMEOUT
from player.service import (
    BACKOFF,
    PlayerConfig,
    Registration,
    ServiceError,
    Unauthorized,
    build_finder,
)
from player.service import main as player_main
from tests import tls_fixture as tls
from tests.test_player_service import (
    CMDLINE,
    LOCATED,
    NoMdns,
    _until_steady,
    close,
    finder_rig,
    gateway,
    rig,
)
from tests.test_player_uplink_faults import _BrokenBody
from tests.uplink_fakes import FakeTransport, central, finding, located
from uplink.causes import Cause, UplinkError
from uplink.locate import LOCATE_DEADLINE

TOKEN = "1" * 32
REGISTRATION = Registration(player_id="p-" + "a" * 32, token=TOKEN, authority_epoch=1)


# --- M1 / U8: the bearer goes to the origin that issued it, or nowhere -----------------------

@pytest.mark.parametrize("second", ["https://central.example:8443", "http://central.example",
                                    "https://other.example", "http://attacker.example"])
def test_a_session_keeps_its_registration_only_on_the_issuing_origin(second):
    session = Session(located("https://central.example")).enrolled(REGISTRATION)
    assert session.relocated(located("https://central.example")).registration is REGISTRATION
    assert session.relocated(located(second)).registration is None


def test_a_session_is_never_built_with_a_registration():
    with pytest.raises(TypeError):
        Session(located("https://central.example"), REGISTRATION)
    assert Session(located("https://central.example")).registration is None


def test_after_relocating_elsewhere_nothing_authenticated_is_sent():
    """The probe_claim1 sequence, direct: enrolled at https://central.example, a failed cycle,
    then a locate that lands on http://attacker.example. No bearer is sent there."""
    async def check():
        first = finding("https://central.example")
        service, _ = await finder_rig(first)
        seen = []

        def attacker(request):
            seen.append(request.headers.get("authorization"))
            return httpx.Response(200, json={})

        try:
            await service.locate_central()
            await service.enroll()
            assert service.registration is not None
            service._located = False                       # what run() does on a failed cycle
            service._find_central = finding("http://attacker.example")
            await service.link.client.aclose()
            service.link.client = service.link.time_client = httpx.AsyncClient(
                transport=httpx.MockTransport(attacker))
            await service.locate_central()
            assert service.registration is None
            with pytest.raises(Unauthorized):
                await service.probe_time()
            with pytest.raises(Unauthorized, match="not_registered"):
                await service.poll_state()
            assert seen == []
        finally:
            await close(service)
    asyncio.run(check())


@pytest.mark.parametrize("second", ["http://attacker.example", "http://central.example"])
def test_the_run_loop_reenrolls_at_a_new_origin_and_never_leaks_the_bearer(monkeypatch, second):
    """Seeded from the security review's rig: the first cycle enrolls at
    https://central.example and fails; the next locate lands on `second` (another host, or the
    same host downgraded to http). The Player re-enrolls there, silently, and every bearer it
    sends is the one that origin issued."""
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)
    real, other = finding("https://central.example"), finding(second)
    calls = []

    async def find():
        calls.append(1)
        return await (real() if len(calls) == 1 else other())

    async def check():
        service, server = await finder_rig(find)
        steady = asyncio.Event()
        inner = _until_steady(server, steady, fail_first_state=True)
        sent = []

        def handle(request):
            origin = f"{request.url.scheme}://{request.url.host}"
            sent.append((origin, request.url.path, request.headers.get("authorization")))
            return inner(request)

        await service.link.client.aclose()
        service.link.client = service.link.time_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle))
        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(steady.wait(), 10)
        finally:
            service.stop()
            await asyncio.wait_for(task, 10)
            await close(service)
        return sent, server

    sent, server = asyncio.run(check())
    assert len(server.proofs) == 2                 # re-enrolled at the second origin
    first_token, second_token = "Bearer " + "1" * 32, "Bearer " + "2" * 32
    to_second = [auth for origin, _, auth in sent if origin == second and auth]
    assert to_second and set(to_second) == {second_token}
    assert all(auth in (None, first_token) for origin, _, auth in sent
               if origin == "https://central.example")


# --- R4c: CentralLink owns the guards --------------------------------------------------------

def _redirecting(calls, path_prefix):
    def handle(request):
        calls.append(str(request.url))
        if request.url.path.startswith(path_prefix):
            return httpx.Response(302, headers={"Location": "http://upstream.example/copy"})
        return httpx.Response(200, content=b"picture", headers={
            "Content-Type": "image/png", "Content-Length": "7"})
    return handle


@pytest.mark.parametrize("kind", ["media", "state"])
def test_a_client_that_follows_redirects_still_never_follows_one(tmp_path, kind):
    """The redirect guard is CentralLink's response hook, not a per-call argument: even an
    injected client built with follow_redirects=True never fetches the Location."""
    async def check():
        service, _ = await rig(tmp_path)
        try:
            service._feedback()
            calls = []
            await service.link.client.aclose()
            service.link.client = httpx.AsyncClient(follow_redirects=True,
                transport=httpx.MockTransport(_redirecting(
                    calls, "/v1/media/" if kind == "media" else "/v1/player/state")))
            with pytest.raises(UplinkError) as excinfo:
                if kind == "media":
                    await service.download(service._jobs[0])
                else:
                    await service.request("GET", "/v1/player/state")
            assert (excinfo.value.cause, excinfo.value.reason) == (Cause.REDIRECT, "unexpected")
            assert "location=upstream.example" in excinfo.value.detail
            assert len(calls) == 1 and calls[0].startswith("http://central/")
        finally:
            await close(service)
    asyncio.run(check())


def test_a_media_body_that_breaks_after_the_status_line_is_a_transfer_failure(tmp_path):
    async def check():
        service, server = await rig(tmp_path)
        try:
            service._feedback()
            server.media_response = httpx.Response(200, stream=_BrokenBody(), headers={
                "Content-Type": "image/png", "Content-Length": "7"})
            with pytest.raises(UplinkError) as excinfo:
                await service.download(service._jobs[0])
            assert (excinfo.value.cause, excinfo.value.reason) == (Cause.TRANSFER, "short")
        finally:
            await close(service)
    asyncio.run(check())


@pytest.mark.parametrize("origin,ssl_expected", [("https://central.example:8443", True),
                                                 ("http://central", False)])
def test_the_websocket_uses_the_one_trust_for_wss_only(origin, ssl_expected):
    async def check():
        service, _ = await finder_rig(finding(origin))
        seen = []

        def connect(uri, **options):
            seen.append(options)
            raise ServiceError("stop")

        service.link.websocket_connect = connect
        try:
            await service.locate_central()
            await service.enroll()
            with pytest.raises(ServiceError, match="stop"):
                await service._websocket_loop()
            if ssl_expected:
                assert seen[0]["ssl"] is service.link.trust.context
            else:
                assert "ssl" not in seen[0]
            assert seen[0]["proxy"] is None
        finally:
            await close(service)
    asyncio.run(check())


def test_only_uplink_errors_are_named_as_network_causes(tmp_path):
    async def check():
        service, _ = await rig(tmp_path)
        try:
            service._fault_for(OSError(errno.ENOSPC, "No space left on device"))
            assert (service.last_fault, service.last_fault_detail) == ("player_error", "OSError")
            service._fault_for(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))
            assert service.last_fault == "player_error"
            service._fault_for(UplinkError(Cause.CONNECT, "refused", host="central"))
            assert service.last_fault == "connect_refused"
            assert "cause=connect reason=refused host=central" in service.last_fault_detail
        finally:
            await close(service)
    asyncio.run(check())


def _central_503(request):
    return httpx.Response(503, json={"error": "fixture_outage"})


def test_the_time_loop_carries_the_cause_and_a_redirect_ends_it(tmp_path):
    async def check():
        service, server = await rig(tmp_path)
        try:
            await service.link.client.aclose()
            service.link.client = service.link.time_client = httpx.AsyncClient(
                transport=httpx.MockTransport(_central_503))
            task = asyncio.create_task(service._time_loop())
            while service.last_fault != "clock_probe":
                await asyncio.sleep(.01)
            assert "cause=central reason=error" in service.last_fault_detail
            assert not task.done()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

            await service.link.client.aclose()
            service.link.client = service.link.time_client = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: httpx.Response(
                    302, headers={"Location": "http://moved.example/v1/player/time"})))
            with pytest.raises(UplinkError) as excinfo:
                await asyncio.wait_for(service._time_loop(), 5)
            assert excinfo.value.cause is Cause.REDIRECT
        finally:
            await close(service)
    asyncio.run(check())


def test_the_media_loop_carries_the_cause(tmp_path, monkeypatch):
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)

    async def check():
        service, server = await rig(tmp_path)
        try:
            service._feedback()
            server.media_response = httpx.Response(503, json={"error": "media_unavailable"})
            task = asyncio.create_task(service._media_loop())
            try:
                async def faulted():
                    while service.last_fault != "media_download":
                        await asyncio.sleep(.01)
                await asyncio.wait_for(faulted(), 5)
                assert "cause=central reason=error" in service.last_fault_detail
                assert not task.done()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        finally:
            await close(service)
    asyncio.run(check())


def test_a_media_redirect_ends_the_cycle_and_locates_again(monkeypatch, caplog):
    """0014: a refused redirect triggers a new locate. Before the fix a 302 on /v1/media/*
    was retried every 60 s forever against the same located origin."""
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)
    caplog.set_level(logging.WARNING, logger="photo_wall.player")
    find = finding("http://central")

    async def check():
        service, server = await finder_rig(find)
        steady = asyncio.Event()
        inner = _until_steady(server, steady)
        redirected, fetched = [], asyncio.Event()

        def handle(request):
            if request.url.path.startswith("/v1/media/"):
                if not redirected:
                    redirected.append(request)
                    return httpx.Response(302, headers={"Location": "http://cdn.example/x"})
                fetched.set()
            return inner(request)

        await service.link.client.aclose()
        service.link.client = service.link.time_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle))
        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(fetched.wait(), 10)
            assert find.calls == 2
            assert len(server.proofs) == 1          # same origin: the registration is kept
        finally:
            service.stop()
            await asyncio.wait_for(task, 10)
            await close(service)

    asyncio.run(check())
    assert any("redirect_unexpected" in message for message in caplog.messages)


# --- R4a / U3: the cmdline wins before anything saved is read --------------------------------

def test_a_cmdline_root_wins_and_a_bad_saved_root_is_never_read(tmp_path, monkeypatch):
    monkeypatch.setattr("player.mdns_discovery.MdnsCentralDiscovery", NoMdns)
    cmdline = tmp_path / "cmdline"
    cmdline.write_text(f"console=ttyAMA0 photowall.central={CMDLINE} quiet\n")
    config = PlayerConfig(central_origin="not a central root")
    find = build_finder(config, transport=gateway(), cmdline_path=cmdline)
    found = asyncio.run(find())
    assert (found.root, found.source, found.central.origin) == (CMDLINE, "cmdline", LOCATED)


def test_without_a_cmdline_the_saved_root_is_validated_and_used(tmp_path, monkeypatch):
    monkeypatch.setattr("player.mdns_discovery.MdnsCentralDiscovery", NoMdns)
    missing = tmp_path / "absent"
    transport = FakeTransport({str(LOCATED.url("/v1/locate")): central})
    found = asyncio.run(build_finder(PlayerConfig(central_origin="http://central"),
                                     transport=transport, cmdline_path=missing)())
    assert (found.root, found.source) == (LOCATED, "saved")
    with pytest.raises(UplinkError) as excinfo:
        build_finder(PlayerConfig(central_origin="not a central root"), transport=transport,
                     cmdline_path=missing)
    assert (excinfo.value.cause, excinfo.value.reason) == (Cause.CONFIGURATION, "invalid")


def test_main_reads_the_kernel_command_line_before_anything_else(tmp_path, monkeypatch, caplog):
    """main()'s pre-GTK step resolves the real cmdline: an invalid photowall.central exits 1
    before identity, outputs or GTK. A main that ignored the cmdline would go on past it."""
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("photowall.central=ftp://nowhere\n")
    config = tmp_path / "public.json"
    config.write_text(json.dumps({"schema": 1, "central_origin": "http://central",
                                  "ca_file": str(tls.write_bundle(tmp_path / "ca.pem", tls.CA))}))
    monkeypatch.setattr("player.service.KERNEL_COMMAND_LINE", cmdline)
    monkeypatch.setattr(sys, "argv", ["player", "--config", str(config)])

    def reached(*_args, **_kwargs):
        raise AssertionError("main went past the cmdline")

    monkeypatch.setattr("player.service.load_identity", reached)
    caplog.set_level(logging.ERROR, logger="photo_wall.player")
    with pytest.raises(SystemExit) as excinfo:
        player_main()
    assert excinfo.value.code == 1
    assert any("cause=configuration reason=invalid" in message for message in caplog.messages)


# --- M5: the Player's deadline owner ---------------------------------------------------------

def _unit() -> dict[str, str]:
    text = Path("appliance/systemd/player.service").read_text()
    return dict(line.split("=", 1) for line in text.splitlines()
                if "=" in line and not line.startswith("#"))


def test_the_player_unit_owns_a_watchdog_sized_for_a_slow_healthy_cycle():
    """The longest healthy gap between pets: one backoff, then a full reconnect up to the
    first completed control exchange (locate + mDNS, challenge, register, base-health, clock,
    state, then state + readiness + a stale re-poll)."""
    unit = _unit()
    assert unit["Type"] == "notify" and unit["NotifyAccess"] == "main"
    watchdog_sec = float(unit["WatchdogSec"])
    slowest = BACKOFF[-1] + LOCATE_DEADLINE + MDNS_TIMEOUT + 5 * REQUEST_TIMEOUT \
        + 3 * REQUEST_TIMEOUT
    assert watchdog_sec >= 1.25 * slowest


def test_a_steady_session_and_every_cycle_pet_the_watchdog(monkeypatch):
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)
    pets = []
    monkeypatch.setattr("uplink.watchdog.pet", lambda: pets.append(1) or True)
    failing = []

    async def find():
        failing.append(1)
        raise UplinkError(Cause.CONFIGURATION, "absent", detail="not_discovered")

    async def cycles():
        service, _ = await finder_rig(find)
        task = asyncio.create_task(service.run())
        try:
            async def cycled():
                while len(failing) < 3:
                    await asyncio.sleep(.01)
            await asyncio.wait_for(cycled(), 5)
        finally:
            service.stop()
            await asyncio.wait_for(task, 10)
            await close(service)

    asyncio.run(cycles())
    assert len(pets) >= 2                         # at least one per completed (failed) cycle

    pets.clear()
    find_ok = finding("http://central")

    async def steady_session():
        service, server = await finder_rig(find_ok)
        steady = asyncio.Event()
        await service.link.client.aclose()
        service.link.client = service.link.time_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_until_steady(server, steady)))
        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(steady.wait(), 10)

            async def petted():
                while not pets:                   # no cycle has ended: the control loop pets
                    await asyncio.sleep(.01)
            await asyncio.wait_for(petted(), 5)
            assert find_ok.calls == 1
        finally:
            service.stop()
            await asyncio.wait_for(task, 10)
            await close(service)

    asyncio.run(steady_session())
