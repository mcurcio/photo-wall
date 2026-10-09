"""Control-service authority and network bounds; simulated renderer is not Pi evidence."""

import asyncio
import base64
import contextlib
import functools
import hashlib
import inspect
import json
import logging
import math
import stat
import subprocess
import sys
import tempfile
from concurrent.futures import Future
from pathlib import Path

import httpx
import pytest
import tls_fixture as tls
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from media_queue import RecordingMediaQueue
from pydantic import ValidationError
from test_executor import binding, layer
from uplink_fakes import FakeReply, FakeTransport, central, finding
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response as Http11Response

import player.service as service_module
from central.db import ProcessTransactionClock
from contracts.enrollment import Enrollment, OutputReport, enrollment_message
from contracts.models import Commit, Plan, PlayerConfiguration, Revocation
from contracts.player_control import ControlSelection
from contracts.time import ManualClock, TimeMapping
from player.identity import load_identity
from player.mainloop import CONTROL, DispatchRefused, MainLoopDispatcher
from player.output_discovery import (
    CONFIGURED_OUTPUT_IDS,
    discover_outputs,
    output_app_id,
    weston_ini,
)
from player.rendering import CapacityResult, RecordingRenderer
from player.service import (
    MAX_JSON,
    BootContext,
    IdentifyOutput,
    MainLoopLate,
    PlayerConfig,
    PlayerService,
    ServiceError,
    State,
    Unauthorized,
    _ChunkBridge,
    _json,
    central_finder,
    display_serial,
    load_config,
)
from uplink.causes import Cause, UplinkError
from uplink.finder import find_central
from uplink.origin import Origin
from uplink.resolver import Configured, Unconfigured
from uplink.transport import HttpTransport
from uplink.trust import Trust


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


# One Trust for the whole module: minted once into its own private directory (never
# tmp_path), so tests that assert tmp_path's exact contents see no incidental ca.pem.
_TRUST_DIR = Path(tempfile.mkdtemp(prefix="photo-wall-test-trust-"))
TRUST = Trust.public(tls.write_bundle(_TRUST_DIR / "ca.pem", tls.CA))


def boot_context() -> BootContext:
    return BootContext.model_validate({
        "schema": 2,
        "ticket_id": "a" * 48,
        "device_id": "device-" + "b" * 64,
        "boot_id": "12345678-1234-1234-1234-123456789abc",
        "release_id": "c" * 64,
        "trial": False,
        "persistence": "volatile",
        "fault": None,
    })


def d0_boot_context() -> BootContext:
    """m3-central-d0-enroll / 0009 re-key: flashed/ticketless
    (persistence="persistent", ticket_id=None). `hardware_boot_context`
    (player/service.py) has no netboot-ticket source and honestly reports
    `ticket_id=None`; enroll() derives the ticketless signal from the
    presence of a ticket, not from `persistence`."""
    return boot_context().model_copy(update={"persistence": "persistent", "ticket_id": None})


def test_importing_service_never_pulls_in_zeroconf():
    """The appliance chroot smoke-test does `import player.service,gi,OpenGL`
    in a netboot venv that has no zeroconf -- 0008 D1 players use an explicit
    `central_origin` and never need mDNS discovery. `MdnsCentralDiscovery` (and
    its `zeroconf` dependency) must therefore be imported lazily, only on the
    discovery branch in main(), never at `player.service` module import time.

    Run in a subprocess (not just `sys.modules` in-process) so this doesn't
    depend on whichever test happened to import zeroconf first in this
    session, and so it doesn't poison `sys.modules` for later tests."""
    result = subprocess.run(
        [sys.executable, "-c", "import player.service, sys; "
            "assert 'zeroconf' not in sys.modules, sys.modules.keys()"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_identity_is_fresh_signed_and_never_written(tmp_path):
    state = private_dir(tmp_path / "state")
    first, second = load_identity(), load_identity()
    assert first.public_key != second.public_key
    context = boot_context()
    fields = dict(device_id=context.device_id, boot_id=context.boot_id,
                  ticket_id=context.ticket_id)
    proof = first.enrollment("f" * 64, (), **fields)
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(first.public_key)).verify(
        base64.b64decode(proof.signature), enrollment_message(proof.nonce, (), **fields))
    assert not list(state.iterdir())
    assert "_key=" not in repr(first)


def test_display_serial_accepts_only_bounded_hex():
    assert display_serial(b"0123456789ABCDEF\x00") == "0123456789abcdef"
    assert display_serial(b"not-a-serial") is None
    assert display_serial(b"ff\x00injected") is None
    assert display_serial(None) is None


@pytest.mark.parametrize("origin", ["https://user:secret@central", "https://central/path",
    "https://central?token=x", "https://central#x", "https://central\\evil", "file:///tmp/media",
    "https://central:bad", "https://central:0", "https://central\n"])
def test_the_saved_root_rejects_untrusted_or_non_origin_urls_only_when_read(tmp_path, origin):
    """U3: loading the config never validates central_origin (a cmdline root must win over a
    bad saved value); saved_root(), read only without a cmdline root, refuses it."""
    config = PlayerConfig(central_origin=origin)
    with pytest.raises(UplinkError) as excinfo:
        config.saved_root()
    assert (excinfo.value.cause, excinfo.value.reason) == (Cause.CONFIGURATION, "invalid")


@pytest.mark.parametrize("allow_http", [{}, {"allow_http": False}, {"allow_http": True}])
def test_config_accepts_an_http_root_and_allow_http_is_inert(tmp_path, allow_http):
    """R2 makes an http root legal; allow_http is still accepted (older handoffs carry it) and
    changes nothing."""
    path = tmp_path / "public.json"
    path.write_text(json.dumps({"schema": 1, "central_origin": "http://192.0.2.10:8000",
                                **allow_http}))
    assert load_config(path).saved_root() == Origin.parse_root("http://192.0.2.10:8000")
    assert PlayerConfig().saved_root() is None


def test_config_is_strict_bounded_public_json(tmp_path):
    path = tmp_path / "public.json"
    path.write_text(json.dumps({"schema": 1, "central_origin": "https://central:8443/",
                              "cache_dir": str(tmp_path), "cache_bytes": 1048576}))
    assert load_config(path).central_origin == "https://central:8443/"
    for data in ({"unexpected": True}, {"cache_bytes": "1048576"}, {"allow_http": "false"}):
        with pytest.raises(ValidationError):
            PlayerConfig.model_validate({"central_origin": "https://central", **data})
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
        self.time_epoch = None
        self.time_offset = 0

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/enrollment/challenge":
            return httpx.Response(200, json={"nonce": "a" * 64, "expires_at": self.clock.utc() + 60})
        if path == "/v1/enrollment/register":
            proof = Enrollment.model_validate_json(request.content)
            self.proofs.append(proof)
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(proof.public_key)).verify(
                base64.b64decode(proof.signature), enrollment_message(
                    proof.nonce, proof.outputs, proof.device_id, proof.boot_id, proof.ticket_id
                ))
            self.epoch += 1
            self.player_id = "p-" + hashlib.sha256(proof.device_id.encode()).hexdigest()[:32]
            return httpx.Response(200, json={"player_id": self.player_id,
                "authority_epoch": self.epoch, "token": str(self.epoch) * 32})
        if path == "/v1/player/time":
            return httpx.Response(200, json={
                "player_id": self.player_id,
                "authority_epoch": self.time_epoch or self.epoch,
                "server_time": self.clock.utc() + self.time_offset,
            })
        if path == "/v1/player/state":
            return httpx.Response(200, json=self.state.model_dump(mode="json"))
        if path == "/v1/player/hello":
            # Existing Central releases have no negotiation route; a new Player
            # must fall back only for this exact 404 before its first state read.
            return httpx.Response(404, json={"detail": "Not Found"})
        if path == "/v1/player/boot-health":
            return httpx.Response(200, json={"accepted": True, "release_id": "c" * 64})
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
            commits=commits, revocations=revocations)
        return self.state


async def rig(tmp_path, *, server=None, negotiate=False):
    directory = tmp_path / "cache"
    directory.mkdir(mode=0o700, exist_ok=True)
    clock = ManualClock(100)
    server = server or Server(clock)
    config = PlayerConfig(central_origin="http://central", allow_http=True, cache_dir=str(directory),
                          cache_bytes=1024**2)
    client = httpx.AsyncClient(transport=httpx.MockTransport(server), trust_env=False)
    service = PlayerService(config, load_identity(), (), RecordingRenderer(), immediate,
        find_central=finding("http://central"), trust=TRUST, clock=clock,
        client=client, time_client=client,
        websocket_connect=False, health_path=None, boot_context=boot_context())
    await service.locate_central()
    await service.enroll()
    if negotiate:
        await service.hello_protocol()
    await service.probe_time()
    server.offer()
    await service.poll_state()
    return service, server


def test_player_negotiates_before_state_and_acknowledges_ordered_v2(tmp_path):
    class V2Server(Server):
        def __call__(self, request):
            if request.url.path == "/v1/player/hello":
                self.requests.append(request)
                return httpx.Response(200, json={"authority_epoch": self.epoch,
                    "schema": 2, "capabilities": ["identify_output"]})
            return super().__call__(request)

        def offer(self, **kwargs):
            state = super().offer(**kwargs)
            self.state = state.model_copy(update={"delivery_id": "1" * 32,
                                           "delivery_sequence": 1})
            return self.state

    async def check():
        service, server = await rig(tmp_path, server=V2Server(ManualClock(100)),
                                    negotiate=True)
        try:
            paths = [request.url.path for request in server.requests]
            assert paths.index("/v1/player/hello") < paths.index("/v1/player/state")
            assert service._control_selection.schema_version == 2
            reports = [request for request in server.requests
                       if request.url.path == "/v1/player/control-acks"]
            assert len(reports) == 1
            assert json.loads(reports[0].content) == {
                "authority_epoch": service.registration.authority_epoch,
                "delivery_id": "1" * 32, "result": "applied",
            }
        finally:
            await close(service)
    asyncio.run(check())


def test_legacy_hello_fallback_suppresses_unnegotiated_identify(tmp_path):
    async def check():
        service, _ = await rig(tmp_path)
        try:
            await service.hello_protocol()
            assert service._control_selection.schema_version == 1
            service.outputs = (OutputReport(output_id="hdmi2", width_px=1920,
                                            height_px=1080, connected=True),)
            config = PlayerConfiguration(player_id=service.registration.player_id,
                authority_epoch=service.registration.authority_epoch,
                configuration_revision=2, bindings=(), enabled_outputs=())
            cue = IdentifyOutput(request_id="old-central-cue", output_id="hdmi2",
                authority_epoch=service.registration.authority_epoch, remaining_seconds=10)
            state = State(configuration=config, plan=None, commits=(), revocations=(),
                          identify_output=cue)
            assert service._apply_state(state) == "applied"
            assert service.renderer.identify_output is None
        finally:
            await close(service)
    asyncio.run(check())


async def close(service):
    if service.link.time_client is not None and service.link.time_client is not service.link.client:
        await service.link.time_client.aclose()
    if service.link.client is not None:
        await service.link.client.aclose()
    if service.cache:
        service.cache.close()
    service._worker.shutdown(wait=True, cancel_futures=True)


def test_identify_output_deduplicates_deadline_and_clears_on_vanish_or_epoch(tmp_path):
    asyncio.run(_identify_output_scenario(tmp_path))


async def _identify_output_scenario(tmp_path):
    service, _ = await rig(tmp_path)
    try:
        service._control_selection = ControlSelection(
            authority_epoch=service.registration.authority_epoch, schema=2,
            capabilities=("identify_output",))
        service.outputs = (OutputReport(output_id="hdmi2", width_px=1920,
                                        height_px=1080, connected=True),)
        config = PlayerConfiguration(player_id=service.registration.player_id,
            authority_epoch=service.registration.authority_epoch,
            configuration_revision=2, bindings=(), enabled_outputs=())
        cue = IdentifyOutput(request_id="identify-1", output_id="hdmi2",
            authority_epoch=service.registration.authority_epoch, remaining_seconds=10)
        state = State(configuration=config, plan=None, commits=(), revocations=(),
                      identify_output=cue, identify_expires_at=service.clock.utc() + 10,
                      delivery_id="1" * 32, delivery_sequence=1)
        service._apply_state(state)
        renderer = service.renderer
        deadline = service._identify_deadline
        assert renderer.identify_output == "hdmi2"
        assert deadline == service.clock.monotonic() + 10

        service.clock.advance(3)
        repeated = state.model_copy(update={
            "identify_output": cue.model_copy(update={"remaining_seconds": 15})
        })
        service._apply_state(repeated)
        assert service._identify_deadline == deadline

        service.clock.advance(7)
        service.tick_main()
        assert renderer.identify_output is None
        assert service._identify_deadline is None
        service._apply_state(repeated)
        assert renderer.identify_output is None
        assert service._identify_deadline is None

        cleared = state.model_copy(update={"identify_output": None,
                                           "identify_expires_at": None,
                                           "delivery_id": "2" * 32,
                                           "delivery_sequence": 2})
        service._apply_state(cleared)
        assert service._apply_state(state) == "superseded"
        assert renderer.identify_output is None

        new_cue = cue.model_copy(update={"request_id": "identify-2"})
        service._apply_state(state.model_copy(update={
            "identify_output": new_cue,
            "identify_expires_at": service.clock.utc() + 5,
            "delivery_id": "3" * 32,
            "delivery_sequence": 3,
        }))
        assert renderer.identify_output == "hdmi2"

        service._apply_identify_output(cue, ("hdmi2",), config.authority_epoch + 1)
        assert renderer.identify_output is None
    finally:
        await close(service)


# --- R1: the Player finds Central through find_central (design §2.3) -------------

CMDLINE = Origin.parse_root("http://photo-wall.localdomain")
LOCATED = Origin.parse_root("http://central")


def gateway():
    """The command line's root 301s to Central at http://central (the Server's host)."""
    return FakeTransport({
        str(CMDLINE.url("/v1/locate")): lambda: FakeReply(
            301, location=str(LOCATED.url("/v1/locate"))),
        str(LOCATED.url("/v1/locate")): central})


class NoMdns:
    """Fails the test if the wiring builds an mDNS browser."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("no mDNS object may be built")


async def finder_rig(find, handle=None):
    clock = ManualClock(100)
    server = Server(clock)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle or server), trust_env=False)
    service = PlayerService(PlayerConfig(), load_identity(), (), RecordingRenderer(), immediate,
        find_central=find, trust=TRUST, clock=clock, client=client, time_client=client,
        websocket_connect=False, health_path=None, boot_context=boot_context())
    return service, server


def test_a_cmdline_root_wins_over_the_saved_root_and_no_mdns_is_built(monkeypatch, caplog):
    monkeypatch.setattr("player.mdns_discovery.MdnsCentralDiscovery", NoMdns)
    caplog.set_level(logging.INFO, logger="photo_wall.player")
    config = PlayerConfig(central_origin="http://192.0.2.20:8000")
    found = asyncio.run(central_finder(config, Configured(CMDLINE), transport=gateway())())
    assert (found.root, found.source, found.central.origin) == (CMDLINE, "cmdline", LOCATED)
    assert caplog.messages == ["player: the saved central_origin is ignored: "
                               "the kernel command line names http://photo-wall.localdomain"]


@pytest.mark.parametrize("unconfigured", [Unconfigured("absent"), Unconfigured("no_cmdline")])
def test_the_saved_root_is_used_without_a_cmdline_root_and_builds_no_mdns(
        monkeypatch, unconfigured):
    monkeypatch.setattr("player.mdns_discovery.MdnsCentralDiscovery", NoMdns)
    transport = FakeTransport({str(LOCATED.url("/v1/locate")): central})
    config = PlayerConfig(central_origin="http://central")
    found = asyncio.run(central_finder(config, unconfigured, transport=transport)())
    assert (found.root, found.source, found.central.origin) == (LOCATED, "saved", LOCATED)


def test_mdns_is_built_only_without_a_cmdline_or_saved_root(monkeypatch):
    built = []

    class Mdns:
        def __init__(self):
            self.proofs = []
            built.append(self)

        async def discover(self, unconfigured):
            self.proofs.append(unconfigured)
            return LOCATED

    monkeypatch.setattr("player.mdns_discovery.MdnsCentralDiscovery", Mdns)
    transport = FakeTransport({str(LOCATED.url("/v1/locate")): central})
    find = central_finder(PlayerConfig(), Unconfigured("absent"), transport=transport)
    found = asyncio.run(find())
    assert (found.root, found.source) == (LOCATED, "discovered")
    assert [mdns.proofs for mdns in built] == [[Unconfigured("absent")]]


def test_every_request_goes_to_the_located_origin_not_the_root(tmp_path):
    async def check():
        find = functools.partial(find_central, Configured(CMDLINE), transport=gateway())
        service, server = await finder_rig(find)
        try:
            with pytest.raises(ServiceError, match="central_origin_unavailable"):
                service.central
            located = await service.locate_central()
            assert service.central is located and located.origin == LOCATED
            await service.enroll()
            assert server.requests
            assert all(str(request.url).startswith("http://central/")
                       for request in server.requests)
        finally:
            await close(service)
    asyncio.run(check())


def test_the_websocket_url_comes_from_the_located_origin(tmp_path):
    async def check():
        service, _ = await finder_rig(finding("https://central.example:8443"))
        uris = []

        def connect(uri, **options):
            uris.append(uri)
            raise ServiceError("stop")

        service.link.websocket_connect = connect
        try:
            await service.locate_central()
            await service.enroll()
            with pytest.raises(ServiceError, match="stop"):
                await service._websocket_loop()
            assert uris == ["wss://central.example:8443/v1/player/session"]
        finally:
            await close(service)
    asyncio.run(check())


def test_d0_persistent_boot_context_enrolls_with_no_ticket_id(tmp_path):
    """The player derives the ticketless signal from `boot_context.ticket_id
    is None`, not `persistence` -- `hardware_boot_context` (player/service.py)
    now honestly reports `ticket_id=None` rather than a synthesized
    placeholder, and enroll() never has to translate `persistence` into a
    ticketless flag."""
    async def check():
        directory = tmp_path / "cache"
        directory.mkdir(mode=0o700)
        clock = ManualClock(100)
        server = Server(clock)
        config = PlayerConfig(central_origin="http://central", allow_http=True,
                              cache_dir=str(directory), cache_bytes=1024**2)
        client = httpx.AsyncClient(transport=httpx.MockTransport(server), trust_env=False)
        service = PlayerService(config, load_identity(), (), RecordingRenderer(), immediate,
            find_central=finding("http://central"), trust=TRUST, clock=clock,
            client=client, time_client=client, websocket_connect=False, health_path=None,
            boot_context=d0_boot_context())
        try:
            await service.locate_central()
            await service.enroll()
            assert server.proofs[-1].ticket_id is None
            assert server.proofs[-1].device_id == d0_boot_context().device_id
        finally:
            await close(service)
    asyncio.run(check())


def test_volatile_boot_context_enrolls_with_its_real_ticket_id_unchanged(tmp_path):
    """Netboot (persistence="volatile", a real ticket_id) is byte-unchanged:
    the real ticket_id still reaches central, exactly as before this bead."""
    async def check():
        service, server = await rig(tmp_path)
        try:
            assert server.proofs[-1].ticket_id == boot_context().ticket_id
        finally:
            await close(service)
    asyncio.run(check())


def test_volatile_persistence_with_no_ticket_still_enrolls_ticketless(tmp_path):
    """0009 re-key proof: `persistence` no longer gates enroll at all. A
    boot context that is `persistence="volatile"` (the netboot label) but
    carries `ticket_id=None` (0009's diskless bootstrapper issues no ticket)
    must still enroll ticketless and start `release_accepted=True` -- keying
    on `persistence` instead of the ticket would send this boot context's
    real-looking `persistence="volatile"` down the ticketed branch and 404."""
    async def check():
        directory = tmp_path / "cache"
        directory.mkdir(mode=0o700)
        clock = ManualClock(100)
        server = Server(clock)
        config = PlayerConfig(central_origin="http://central", allow_http=True,
                              cache_dir=str(directory), cache_bytes=1024**2)
        client = httpx.AsyncClient(transport=httpx.MockTransport(server), trust_env=False)
        diskless = boot_context().model_copy(update={"ticket_id": None})
        assert diskless.persistence == "volatile"
        service = PlayerService(config, load_identity(), (), RecordingRenderer(), immediate,
            find_central=finding("http://central"), trust=TRUST, clock=clock,
            client=client, time_client=client, websocket_connect=False, health_path=None,
            boot_context=diskless)
        try:
            await service.locate_central()
            await service.enroll()
            assert server.proofs[-1].ticket_id is None
        finally:
            await close(service)
    asyncio.run(check())


def test_d0_flashed_player_reaches_sustained_session_without_boot_health_crash_loop(
        tmp_path, monkeypatch):
    """m3-central-d0-enroll FIX: a D0/flashed player has no central-issued boot
    ticket to report against -- `boot_context.ticket_id is None`
    (hardware_boot_context) means `_control_loop` must never call
    `_report_boot_health` for it at all (0009 re-key); calling it anyway
    would 403 `stale_boot_ticket` and, uncaught, tear down every sibling task
    via run()'s FIRST_COMPLETED wait, restarting forever (never
    rendering/downloading/opening a websocket). Drive the actual control loop
    through run() and assert the session SURVIVES: the boot-health endpoint
    is never called, the websocket loop connects exactly once (never torn
    down and reconnected by a crash restart), and the media loop reaches and
    completes a real download."""
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)

    async def check():
        directory = tmp_path / "cache"
        directory.mkdir(mode=0o700)
        clock = ManualClock(100)
        server = Server(clock)
        boot_health_calls, media_calls, connect_calls = [], [], []

        def handle(request):
            if request.url.path == "/v1/player/boot-health":
                boot_health_calls.append(request)
                # Central never recorded the D0 placeholder ticket: a real
                # central would 403 any boot-health call from a D0 player.
                return httpx.Response(403, json={"error": "stale_boot_ticket"})
            if request.url.path.startswith("/v1/media/"):
                media_calls.append(request)
            result = server(request)
            if request.url.path == "/v1/enrollment/register":
                server.offer()
            return result

        config = PlayerConfig(central_origin="http://central", allow_http=True,
                              cache_dir=str(directory), cache_bytes=1024**2)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False)

        class Socket:
            async def __aenter__(self):
                connect_calls.append(True)
                return self

            async def __aexit__(self, *_):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                # A real session websocket stays open with no message due;
                # only cancellation (test teardown) ever unblocks this.
                await asyncio.Event().wait()

        def connect(uri, **options):
            return Socket()

        service = PlayerService(config, load_identity(), (), RecordingRenderer(), immediate,
            find_central=finding("http://central"), trust=TRUST, clock=clock,
            client=client, time_client=client, websocket_connect=connect, health_path=None,
            boot_context=d0_boot_context())
        task = asyncio.create_task(service.run())
        try:
            async def connected():
                while not connect_calls:
                    await asyncio.sleep(.01)
            await asyncio.wait_for(connected(), 3)

            async def downloaded():
                while not media_calls:
                    await asyncio.sleep(.01)
            await asyncio.wait_for(downloaded(), 3)

            # Give the control loop several more 0.5s cadences to prove it
            # keeps running rather than looping the 403 into a task teardown
            # and restart (m3-central-d0-enroll's crash-loop).
            await asyncio.sleep(.2)
            assert service.registration is not None
            assert not boot_health_calls
            assert connect_calls == [True]
        finally:
            service.stop()
            await asyncio.wait_for(task, 3)
            await close(service)
    asyncio.run(check())


def _until_steady(server, steady, *, fail_first_state=False, redirect_first_state=False):
    """Serves `server`, offers a plan the instant enrollment completes, and sets `steady` on
    the first readiness report: only `_control_loop` sends one, i.e. only once run() is blocked
    on its steady-state tasks, the one stable cancellation point every run()-driven test here
    relies on. `fail_first_state` answers the first state poll 503 (one failed cycle);
    `redirect_first_state` answers it 301 (one failed cycle named REDIRECT/unexpected)."""
    failed = []

    def handle(request):
        if (fail_first_state or redirect_first_state) and not failed and (
                request.url.path == "/v1/player/state"):
            failed.append(request)
            if redirect_first_state:
                return httpx.Response(301, headers={
                    "Location": "http://moved.example/v1/player/state"})
            return httpx.Response(503, json={"error": "fixture_outage"})
        result = server(request)
        if request.url.path == "/v1/enrollment/register":
            server.offer()
        if request.url.path == "/v1/player/readiness":
            steady.set()
        return result
    return handle


def test_a_failed_locate_is_retried_on_the_next_cycle_without_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)
    located = finding("http://central")
    attempts = []

    async def find():
        attempts.append(True)
        if len(attempts) == 1:
            raise UplinkError(Cause.CONFIGURATION, "absent", detail="not_discovered")
        return await located()

    async def check():
        service, server = await finder_rig(find)
        steady = asyncio.Event()
        await service.link.client.aclose()
        service.link.client = service.link.time_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_until_steady(server, steady)))
        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(steady.wait(), 10)
            assert len(attempts) == 2           # located once for the session, not per request
            assert service.registration is not None
            assert all(request.url.host == "central" for request in server.requests)
        finally:
            service.stop()
            await asyncio.wait_for(task, 10)
            await close(service)
    asyncio.run(check())


def test_every_failed_cycle_locates_again_and_keeps_the_registration(tmp_path, monkeypatch):
    """0014: a failed cycle (here Central answering 503) is followed by a new locate; it lands
    on the origin that issued the registration, so the registration is kept (U8) and there is
    no second enrollment."""
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)
    find = finding("http://central")

    async def check():
        service, server = await finder_rig(find)
        steady = asyncio.Event()
        await service.link.client.aclose()
        service.link.client = service.link.time_client = httpx.AsyncClient(transport=httpx.MockTransport(
            _until_steady(server, steady, fail_first_state=True)))
        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(steady.wait(), 10)
            assert find.calls == 2
            assert len(server.proofs) == 1
            assert "retrying" in service.renderer.central_link_history
            assert service.renderer.central_link_history[-1] == "reachable"
        finally:
            service.stop()
            await asyncio.wait_for(task, 10)
            await close(service)
    asyncio.run(check())


def test_a_redirect_on_a_player_request_is_named_and_relocates(tmp_path, monkeypatch, caplog):
    """A 3xx on a direct Player request (R9) is REDIRECT/unexpected, not a bare transport
    failure: the same "every failed cycle locates again, keeps the registration" recovery as a
    503 (test_every_failed_cycle_locates_again_and_keeps_the_registration), and the fault it
    logs names cause and reason instead of the old blanket "connection_failed"."""
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)
    caplog.set_level(logging.WARNING, logger="photo_wall.player")
    find = finding("http://central")

    async def check():
        service, server = await finder_rig(find)
        steady = asyncio.Event()
        await service.link.client.aclose()
        service.link.client = service.link.time_client = httpx.AsyncClient(transport=httpx.MockTransport(
            _until_steady(server, steady, redirect_first_state=True)))
        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(steady.wait(), 10)
            assert find.calls == 2
            assert len(server.proofs) == 1
            assert all(request.url.host == "central" for request in server.requests)
        finally:
            service.stop()
            await asyncio.wait_for(task, 10)
            await close(service)
    asyncio.run(check())
    assert any("redirect_unexpected" in message for message in caplog.messages)


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


def test_only_connected_unbound_outputs_show_enrollment_diagnostic(tmp_path):
    async def check():
        directory = private_dir(tmp_path / "cache")
        clock = ManualClock(100)
        server = Server(clock)
        client = httpx.AsyncClient(transport=httpx.MockTransport(server), trust_env=False)
        service = PlayerService(
            PlayerConfig(central_origin="http://central", cache_dir=str(directory),
                         cache_bytes=1024**2), load_identity(),
            (OutputReport(output_id="hdmi1", width_px=0, height_px=0, connected=True),
             OutputReport(output_id="hdmi2", width_px=0, height_px=0, connected=False)),
            RecordingRenderer(), immediate, find_central=finding("http://central"),
            trust=TRUST, clock=clock, client=client, time_client=client,
            websocket_connect=False, health_path=None, boot_context=boot_context(),
        )
        try:
            assert service.central_link_state == service.renderer.central_link_state == "connecting"
            assert service.renderer.configuration_received is False
            await service.locate_central()
            await service.enroll()
            assert service.renderer.unbound_outputs == ()
            initial = server.offer(layers=())
            unbound_config = initial.configuration.model_copy(update={
                "bindings": (), "enabled_outputs": (),
            })
            unbound_plan = initial.plan.model_copy(update={
                "bindings": (),
            })
            server.state = State(configuration=unbound_config, plan=unbound_plan,
                                 commits=(), revocations=())
            await service.poll_state()
            assert service.renderer.unbound_outputs == ("hdmi1",)
            assert service.renderer.enrolled_player_id == server.player_id
            assert service.central_link_state == service.renderer.central_link_state == "reachable"
            assert service.renderer.configuration_received is True

            service._set_central_link_state("retrying")
            assert service.renderer.central_link_state == "retrying"
            assert service.renderer.unbound_outputs == ("hdmi1",)
            service.config = service.config.model_copy(update={"base_running_tag": "a" * 64})
            await service._report_base_health()
            assert any(request.url.path == "/v1/player/base-health" for request in server.requests)
            assert service.renderer.central_link_state == "retrying"
            await service.poll_state()
            assert service.central_link_state == service.renderer.central_link_state == "reachable"

            readiness, _ = service._feedback()
            assert readiness.prepared == readiness.secured == ()
            assert service.renderer.presentations == []

            rebound_config = initial.configuration.model_copy(update={
                "configuration_revision": 2,
            })
            rebound_plan = initial.plan.model_copy(update={"revision": 2})
            server.state = State(configuration=rebound_config, plan=rebound_plan,
                                 commits=(), revocations=())
            await service.poll_state()
            assert service.renderer.unbound_outputs == ()
            assert service.renderer.outputs["hdmi1"].fallback

            unbound_config = unbound_config.model_copy(update={"configuration_revision": 3})
            unbound_plan = unbound_plan.model_copy(update={"revision": 3})
            server.state = State(configuration=unbound_config, plan=unbound_plan,
                                 commits=(), revocations=())
            await service.poll_state()
            assert service.renderer.unbound_outputs == ("hdmi1",)
        finally:
            await close(service)
    asyncio.run(check())


def test_valid_surviving_cache_is_verified_before_any_media_request(tmp_path):
    async def check():
        first, server = await rig(tmp_path)
        first_key = first.identity.public_key
        old_state = server.state
        first._feedback()
        assert await first.download(first._jobs[0])
        await close(first)

        restarted, server = await rig(tmp_path, server=server)
        try:
            assert restarted.identity.public_key != first_key
            assert restarted.registration.authority_epoch == 2
            with pytest.raises(ServiceError, match="state_authority"):
                restarted._apply_state(old_state)
            restarted._feedback()
            server.requests.clear()
            assert await restarted.download(restarted._jobs[0])
            assert all(not request.url.path.startswith("/v1/media/")
                       for request in server.requests)
            assert restarted._feedback()[0].secured == ("picture",)
        finally:
            await close(restarted)
    asyncio.run(check())


def test_volatile_registration_constructs_only_disposable_execution_state(tmp_path):
    async def check():
        service, server = await rig(tmp_path)
        try:
            assert service.executor is not None and service.cache is not None
            assert server.proofs[0].device_id == boot_context().device_id
            assert {path.name for path in tmp_path.iterdir()} == {"cache"}
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
            await service.link.client.aclose()
            calls = []
            def handle(request):
                calls.append(request.url)
                return responses[kind]
            service.link.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            if kind == "unauthorized":
                with pytest.raises(Unauthorized):
                    await service.request("GET", "/v1/player/state")
            elif kind == "redirect":
                with pytest.raises(UplinkError) as excinfo:
                    await service.request("GET", "/v1/player/state")
                assert (excinfo.value.cause, excinfo.value.reason) == (Cause.REDIRECT, "unexpected")
                assert "location=upstream" in excinfo.value.detail
            else:
                with pytest.raises(ServiceError):
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
        return kind is not None and issubclass(kind, (ServiceError, UplinkError))


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
            await service.link.client.aclose()
            service.link.client = httpx.AsyncClient(transport=httpx.MockTransport(waiting))
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
            server.time_offset = 10
            assert not await service.probe_time()
            readiness, _ = service._feedback()
            assert not readiness.prepared and not service._health()
            assert service.mapping.diagnostics.status == "uncertainty"
            server.time_offset = 0
            assert await service.probe_time()
            assert service.mapping.healthy()
        finally:
            await close(service)
    asyncio.run(check())


@pytest.mark.parametrize("stage", ["json", "schema"])
@pytest.mark.parametrize("delay", [.2, 1.1])
def test_slow_state_parsing_does_not_change_independent_clock_mapping(
        tmp_path, monkeypatch, stage, delay):
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
            assert service.mapping.healthy()
            assert service.mapping.uncertainty == 0
        finally:
            await close(service)
    asyncio.run(check())


def test_clock_transport_sample_includes_delayed_body_receipt(tmp_path):
    async def check():
        service, server = await rig(tmp_path)
        class DelayedBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                body = json.dumps({"player_id": server.player_id,
                                   "authority_epoch": 1, "server_time": 100}).encode()
                yield body[:20]
                service.clock.advance(.2)
                yield body[20:]
        def respond(request):
            assert request.method == "GET" and request.url.path == "/v1/player/time"
            return httpx.Response(200, headers={"Content-Type": "application/json"},
                                  stream=DelayedBody())
        try:
            service.link.time_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            assert not await service.probe_time()
            assert service.mapping.uncertainty == math.inf
            assert service.mapping.diagnostics.uncertainty == pytest.approx(.2)
            assert service.mapping.diagnostics.status == "uncertainty"
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
            await service.probe_time()
            assert service.mapping.uncertainty == math.inf
            assert not service.mapping.healthy()
            assert service.mapping.diagnostics.status == {
                "dispatch_delay": "apply_age", "parse_clock_step": "apply_drift"
            }[fault]
        finally:
            await close(service)
    asyncio.run(check())


@pytest.mark.parametrize(
    ("reason", "changes"),
    [
        ("player_mismatch", {"sample_player_id": "wrong"}),
        ("stale_epoch", {"sample_epoch": 2}),
        ("transport_time", {"received_monotonic": -1}),
        ("transport_drift", {"received_utc": 100.02}),
        ("uncertainty", {"server_time": 101}),
        ("apply_age", {"applied_utc": 102, "applied_monotonic": 2}),
        ("apply_drift", {"applied_utc": 100.02}),
    ],
)
def test_clock_probe_rejection_reasons_are_exact(reason, changes):
    clock = ManualClock(100)
    mapping = TimeMapping(clock)
    values = dict(sample_epoch=1, authority_epoch=1, server_time=100,
                  sent_utc=100, sent_monotonic=0, received_utc=100,
                  received_monotonic=0, applied_utc=100, applied_monotonic=0)
    values.update(changes)
    assert not mapping.apply_probe(**values)
    assert mapping.diagnostics.status == reason
    assert mapping.diagnostics.samples == mapping.diagnostics.rejected == 1


@pytest.mark.parametrize("reason", ["mapping_age", "clock_step"])
def test_established_mapping_reports_runtime_rejection(reason):
    clock = ManualClock(100)
    mapping = TimeMapping(clock)
    mapping.establish(0)
    if reason == "mapping_age":
        clock.advance(31)
    else:
        clock.step_utc(.3)
    assert not mapping.healthy()
    assert mapping.diagnostics.status == reason


def test_glib_dispatch_is_bounded_ordered_and_cancellation_skips_work():
    class GLib:
        callbacks = []
        @classmethod
        def idle_add(cls, callback, *, priority):
            assert priority == CONTROL
            cls.callbacks.append(callback)
    dispatcher = MainLoopDispatcher(GLib)
    seen = []
    futures = [dispatcher(lambda n=n: seen.append(n)) for n in range(4)]
    assert isinstance(dispatcher(lambda: None).exception(), DispatchRefused)
    futures[1].cancel()
    for callback in GLib.callbacks:
        callback()
    assert seen == [0, 2, 3]
    fifth = dispatcher(lambda: seen.append(4))
    GLib.callbacks[-1]()
    assert fifth.done() and seen == [0, 2, 3, 4]


def test_a_late_main_loop_is_reported_on_readiness_not_silence(tmp_path, monkeypatch):
    """G8: the GLib loop stops running dispatches (a starved control queue). The control loop
    keeps reporting: the last readiness again, one sequence higher, every assignment failed
    main_loop_late, and health says main_loop_late. The late callbacks never run, and when the
    loop recovers its next report is above every late one, so Central accepts it."""
    monkeypatch.setattr("player.service.DISPATCH_DEADLINE", .05)
    monkeypatch.setattr("player.service.REPORT_INTERVAL", .01)

    class StarvedGLib:
        callbacks = []

        @classmethod
        def idle_add(cls, callback, *, priority):
            cls.callbacks.append(callback)

    def readiness_posts(server):
        return [json.loads(request.content) for request in server.requests
                if request.url.path == "/v1/player/readiness"]

    async def turn(service, server, count):
        posted = len(readiness_posts(server))

        async def poll():
            try:
                await PlayerService.poll_state(service)
            finally:
                if len(readiness_posts(server)) >= posted + count:
                    service._stop.set()
        monkeypatch.setattr(service, "poll_state", poll)
        service._stop.clear()
        await asyncio.wait_for(service._control_loop(), 5)
        return readiness_posts(server)[posted:]

    async def check():
        service, server = await rig(tmp_path)
        try:
            service.health_path = tmp_path / "health.json"
            service.boot_id = "00000000-1111-2222-3333-444444444444"
            service._feedback()
            assert await service.download(service._jobs[0])
            service.tick_main()
            live = (await turn(service, server, 1))[-1]
            assert live["secured"] == ["picture"] and not live["failures"]

            # The production dispatcher (4 slots), through more late cycles than it has slots.
            service.dispatcher = MainLoopDispatcher(StarvedGLib)
            late = await turn(service, server, 8)
            assert len(late) >= 8
            assert len(StarvedGLib.callbacks) <= 4      # one abandoned post holds the line
            assert [report["sequence"] for report in late] == [
                live["sequence"] + n for n in range(1, len(late) + 1)]
            for report in late:
                assert report["secured"] == ["picture"] and report["prepared"] == []
                assert report["failures"] == [{"assignment_id": "picture",
                                               "code": "main_loop_late"}]
            assert late[1]["observed_at"] >= late[0]["observed_at"] >= live["observed_at"]
            assert service.last_fault == "main_loop_late"
            assert json.loads(service.health_path.read_text())["health_reason"] == "main_loop_late"
            # The loop runs again: nothing it missed runs late, and reporting resumes above.
            ran = []
            monkeypatch.setattr(service, "_apply_state", lambda state: ran.append(state))
            for callback in StarvedGLib.callbacks:
                callback()
            assert ran == []
            service.dispatcher = immediate
            monkeypatch.undo()
            recovered = (await turn(service, server, 1))[-1]
            assert recovered["sequence"] > late[-1]["sequence"]
            assert recovered["failures"] == []
        finally:
            await close(service)
    asyncio.run(check())


def test_a_blocked_main_loop_keeps_reporting_and_leaves_the_watchdog_to_restart(
        tmp_path, monkeypatch):
    """E-G8-7, through run() with the time loop live and the production dispatcher: a GLib
    loop that stops running dispatches never turns into a dispatch_capacity refusal. The
    Player keeps posting main_loop_late readiness across many deadlines and across a session
    that ends meanwhile (its reconnect's cycle-start dispatches are late, not fatal), and run()
    pets the watchdog not once until the loop runs again, so WatchdogSec restarts a Player
    whose loop stays stuck."""
    monkeypatch.setattr("player.service.DISPATCH_DEADLINE", .05)
    monkeypatch.setattr("player.service.REPORT_INTERVAL", .01)
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)
    pets = []
    monkeypatch.setattr(service_module.watchdog, "pet", lambda: pets.append(True))

    class GLib:
        blocked, queued = False, []

        @classmethod
        def idle_add(cls, callback, *, priority):
            cls.queued.append(callback) if cls.blocked else callback()

    async def check():
        clock = ManualClock(100)
        server = Server(clock)
        posts, fail_state = [], []

        def handle(request):
            if request.url.path == "/v1/player/state" and fail_state and not fail_state[0]:
                fail_state[0] = True
                return httpx.Response(503, json={"error": "fixture_outage"})
            result = server(request)
            if request.url.path == "/v1/enrollment/register":
                server.offer()
            if request.url.path == "/v1/player/readiness":
                posts.append(json.loads(request.content))
            return result

        service, _ = await finder_rig(finding("http://central"), handle)
        service.dispatcher = MainLoopDispatcher(GLib)
        faults = []
        record = service.fault
        monkeypatch.setattr(service, "fault",
                            lambda code, **kw: (faults.append(code), record(code, **kw)))

        async def until(predicate):
            while not predicate():
                await asyncio.sleep(.005)

        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(until(lambda: posts and pets), 5)
            GLib.blocked = True
            blocked_at, petted = len(posts), len(pets)
            await asyncio.wait_for(until(lambda: len(posts) >= blocked_at + 6), 5)
            fail_state.append(False)        # the session ends while the loop is blocked
            await asyncio.wait_for(until(lambda: fail_state[0]), 5)
            ended_at = len(posts)
            await asyncio.wait_for(until(lambda: len(posts) >= ended_at + 6), 5)
            late = posts[blocked_at:]
            assert all(failure["code"] == "main_loop_late"
                       for report in late for failure in report["failures"])
            assert "dispatch_capacity" not in faults and "main_loop_late" in faults
            assert len(pets) == petted              # no pet while the loop is stuck
            assert len(GLib.queued) <= 4
            # The loop runs again: the abandoned posts drain, reporting and petting resume.
            GLib.blocked = False
            for callback in GLib.queued:
                callback()
            recovered_at = len(posts)
            await asyncio.wait_for(until(lambda: len(pets) > petted and any(
                not report["failures"] for report in posts[recovered_at:])), 5)
        finally:
            service.stop()
            await asyncio.wait_for(task, 5)
            await close(service)
    asyncio.run(check())


def test_a_full_dispatcher_is_a_capacity_refusal_not_a_late_loop(tmp_path):
    """Only a refusal for a late loop reads as main_loop_late; a full dispatcher on a loop that
    is not late stays the session-ending dispatch_capacity it always was."""
    async def check():
        service, _ = await rig(tmp_path)
        try:
            def full(callback):
                refused = Future()
                refused.set_exception(DispatchRefused("dispatch_capacity"))
                return refused
            service.dispatcher = full
            with pytest.raises(ServiceError) as raised:
                await service.dispatch(lambda: None)
            assert str(raised.value) == "dispatch_capacity"
            assert not isinstance(raised.value, MainLoopLate)
        finally:
            await close(service)
    asyncio.run(check())


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
            assert body["persistence"] == "volatile"
            assert body["clock"]["status"] == "healthy"
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


@pytest.mark.parametrize("reason", ["executor", "configuration", "clock",
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
            if reason in {"executor", "configuration"}:
                service._configuration = None
            assert service._health_status() == (reason == "healthy", reason)
            expected = ([] if reason in {"executor", "configuration"}
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
def test_control_writes_one_consistent_health_sample_with_clock_diagnostics(
        tmp_path, monkeypatch, reason):
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
            value = json.loads(service.health_path.read_text())
            assert value["health_reason"] == reason
            assert value["healthy"] is (reason == "healthy")
            assert value["persistence"] == "volatile"
            assert value["clock"]["status"] == "healthy"
            assert value["clock"]["accepted"] >= 1
        finally:
            await close(service)
    asyncio.run(check())


def test_health_default_is_player_private_path():
    default = inspect.signature(PlayerService).parameters["health_path"].default
    assert default == Path("/run/photo-wall/player/service-health.json")


def test_cache_initialization_failure_fails_session_without_local_fallback(tmp_path):
    async def check():
        directory = private_dir(tmp_path / "state")
        clock = ManualClock(100)
        server = Server(clock)
        def fail(*_):
            raise OSError("full volume")
        client = httpx.AsyncClient(transport=httpx.MockTransport(server))
        service = PlayerService(PlayerConfig(central_origin="http://central", allow_http=True,
            cache_dir=str(directory)), load_identity(), (), RecordingRenderer(), immediate,
            find_central=finding("http://central"), trust=TRUST, clock=clock,
            client=client, time_client=client, cache_factory=fail, health_path=None,
            boot_context=boot_context())
        try:
            await service.locate_central()
            with pytest.raises(ServiceError, match="player_initialization"):
                await service.enroll()
            assert service.cache is service.executor is service.registration is None
            assert len(server.proofs) == 1
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
        service.link.websocket_connect = connect
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


def test_a_state_delivered_while_the_loop_is_late_is_skipped_not_fatal(tmp_path, monkeypatch):
    """G8: a websocket delivery whose dispatch misses the deadline is not applied and does
    not end the session; the next delivery, once the loop runs again, is applied."""
    monkeypatch.setattr("player.service.DISPATCH_DEADLINE", .05)

    class GLib:
        queued = []

        @classmethod
        def idle_add(cls, callback, *, priority):
            cls.queued.append(callback)

    async def check():
        service, server = await rig(tmp_path)
        applied = []
        monkeypatch.setattr(service, "_apply_state", lambda state: applied.append(state))
        message = json.dumps({"type": "state", **server.state.model_dump(mode="json")})

        class Socket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def __aiter__(self):
                yield message               # its dispatch is abandoned at the deadline
                for callback in GLib.queued:
                    callback()              # the loop runs again
                service.dispatcher = immediate
                yield message

        service.link.websocket_connect = lambda uri, **options: Socket()
        service.dispatcher = MainLoopDispatcher(GLib)
        try:
            with pytest.raises(ServiceError, match="session_closed"):
                await service._websocket_loop()
            assert len(applied) == 1 and service.last_fault == "main_loop_late"
        finally:
            await close(service)
    asyncio.run(check())


def test_the_websocket_refuses_401_as_unauthorized_and_names_a_redirect(tmp_path):
    async def check():
        service, _ = await rig(tmp_path)
        try:
            def refuses(uri, **options):
                raise InvalidStatus(Http11Response(401, "Unauthorized", Headers(), b""))
            service.link.websocket_connect = refuses
            with pytest.raises(Unauthorized):
                await service._websocket_loop()

            def redirects(uri, **options):
                raise InvalidStatus(Http11Response(301, "Moved", Headers({
                    "Location": "https://other.example/x"}), b""))
            service.link.websocket_connect = redirects
            with pytest.raises(UplinkError) as excinfo:
                await service._websocket_loop()
            assert (excinfo.value.cause, excinfo.value.reason) == (Cause.REDIRECT, "unexpected")
        finally:
            await close(service)
    asyncio.run(check())


def test_the_default_websocket_connector_refuses_redirects(tmp_path, monkeypatch):
    async def check():
        clock = ManualClock(100)
        server = Server(clock)
        client = httpx.AsyncClient(transport=httpx.MockTransport(server), trust_env=False)
        service = PlayerService(PlayerConfig(central_origin="http://central", allow_http=True),
            load_identity(), (), RecordingRenderer(), immediate,
            find_central=finding("http://central"), trust=TRUST, clock=clock,
            client=client, time_client=client, health_path=None, boot_context=boot_context())
        assert service.link.websocket_connect is None
        try:
            await service.locate_central()
            await service.enroll()
            calls = []

            class FakeConnector:
                def __init__(self, uri, **options):
                    calls.append((uri, options))

                async def __aenter__(self):
                    raise ServiceError("stop")

                async def __aexit__(self, *_):
                    return False

            monkeypatch.setattr("player.central_link.DirectWebsocket", FakeConnector)
            with pytest.raises(ServiceError, match="stop"):
                await service._websocket_loop()
            assert calls and calls[0][0] == "ws://central/v1/player/session"
        finally:
            await close(service)
    asyncio.run(check())


def test_fault_logs_the_code_and_detail_once_per_change(tmp_path, caplog):
    async def check():
        service, _ = await rig(tmp_path)
        try:
            caplog.set_level(logging.WARNING, logger="photo_wall.player")
            caplog.clear()
            service.fault("media_download")
            service.fault("media_download")  # unchanged code: no second log line
            service.fault("media_download", detail="retry")  # code unchanged: still no log
            service.fault("clock_probe", detail="timeout")
            assert caplog.messages == ["player fault: media_download",
                                       "player fault: clock_probe timeout"]
            assert (service.last_fault, service.last_fault_detail) == ("clock_probe", "timeout")
        finally:
            await close(service)
    asyncio.run(check())


def test_no_default_ssl_context_remains():
    assert "ssl.create_default_context" not in Path("player/service.py").read_text()


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
        await service.link.client.aclose()
        service.link.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        service.link.time_client = service.link.client
        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(seen.wait(), 3)
            assert server.proofs[0].public_key == server.proofs[1].public_key
            assert service._configuration.authority_epoch == 2
        finally:
            service.stop()
            await asyncio.wait_for(task, 3)
            await service.link.client.aclose()
        assert service.registration is None
    asyncio.run(check())


def test_retired_key_refusal_never_rotates_identity(tmp_path, monkeypatch):
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)
    async def check():
        service, server = await rig(tmp_path)
        service._session = service._session.unregistered()
        keys = []
        seen = asyncio.Event()
        def handle(request):
            keys.append(json.loads(request.content)["public_key"])
            if len(keys) == 3:
                seen.set()
            return httpx.Response(403, json={"error": "retired_player"})
        await service.link.client.aclose()
        service.link.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        service.link.time_client = service.link.client
        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(seen.wait(), 3)
            assert len(set(keys)) == 1 and keys[0] == service.identity.public_key
        finally:
            service.stop()
            await asyncio.wait_for(task, 3)
            await service.link.client.aclose()
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
            await service.link.client.aclose()
            service.link.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
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
        max_bytes=10000, max_original_bytes=1000, max_image_bytes=1000, max_video_bytes=2000),
        queue=RecordingMediaQueue(), times=ProcessTransactionClock(registry.clock))
    repository.set_recipe(RECIPE)
    storage = MediaStore(repository, tmp_path / "central-media")
    with storage.worker_lock():
        lease, _, prepared = staged(storage)
        repository.configure_source(SourceSpec(source_ref="library:1", connection_ref="fixture"))
        with repository.transaction() as conn:
            conn.execute("UPDATE media_sources SET status='ok'")
            conn.execute("INSERT INTO source_members VALUES('library:1',%s)", (lease.asset.asset_id,))
        storage.publish(lease, prepared)
    app = create_app(
        registry.db,
        registry.clock,
        ADMIN,
        media_root=storage.root,
    )
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
        cache_dir = private_dir(tmp_path / "player-cache")
        boot_id = "12345678-1234-1234-1234-123456789abc"
        device_id = "device-" + "d" * 64
        # Ticketless enroll (0009): the signed boot-ticket path is retired, so
        # this drives the real central with ticket_id=None, exactly as a
        # diskless/flashed player does.
        context = BootContext.model_validate({
            "schema": 2, "device_id": device_id, "boot_id": boot_id,
            "ticket_id": None, "release_id": "c" * 64,
            "trial": False, "persistence": "volatile", "fault": None,
        })
        client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
        # An http root needs no trust store; the fixture CA keeps the host's out of it.
        trust = Trust.public(tls.write_bundle(tmp_path / "ca.pem", tls.CA))
        transport = HttpTransport(trust=trust)
        service = PlayerService(PlayerConfig(central_origin=origin, cache_dir=str(cache_dir)),
            load_identity(), (OutputReport(output_id="HDMI-A-1", width_px=0, height_px=0),),
            RecordingRenderer(), immediate,
            find_central=functools.partial(find_central, Configured(Origin.parse_root(origin)),
                                           transport=transport),
            trust=trust, clock=registry.clock, client=client, time_client=client,
            health_path=None, boot_context=context)
        socket_task = None
        try:
            assert (await service.locate_central()).origin == Origin.parse_root(origin)
            await service.enroll()
            registered = service.registration
            await service.probe_time()
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
            # The outage middleware answers 503 with Central's own {"error": ...} body, so
            # the exchange names it CENTRAL/error (not the bare HTTP/status a gateway would get).
            with pytest.raises(UplinkError) as excinfo:
                await service.poll_state()
            assert (excinfo.value.cause, excinfo.value.reason) == (Cause.CENTRAL, "error")
            assert excinfo.value.central_error == "fixture_outage"
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


# --- 0012 bead 6: base-health check-in after enroll --------------------------

BASE_TAG = "v9.9.9"


def _base_health_rig(tmp_path, config, *, responses=()):
    """A PlayerService wired to a Server that captures base-health POST bodies.

    Mirrors `rig`'s construction but keeps the base-health path observable and
    lets the caller vary `config` (e.g. whether a served tag was handed forward).
    """
    directory = tmp_path / "cache"
    directory.mkdir(mode=0o700, exist_ok=True)
    clock = ManualClock(100)
    server = Server(clock)
    posts = []
    answers = iter(responses)
    inner = server.__call__

    def capture(request):
        if request.url.path == "/v1/player/base-health":
            posts.append(json.loads(request.content))
            if responses:
                return httpx.Response(200, json={"accepted": next(answers)})
        return inner(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(capture), trust_env=False)
    service = PlayerService(config, load_identity(), (), RecordingRenderer(), immediate,
        find_central=finding("http://central"), trust=TRUST, clock=clock,
        client=client, time_client=client,
        websocket_connect=False, health_path=None, boot_context=boot_context())
    return service, posts


def test_base_health_posted_once_per_epoch_after_enroll(tmp_path):
    # The enrolled player reports the handed-forward served tag healthy so the
    # latest-verified frontier can advance; the report is idempotent per epoch.
    config = PlayerConfig(central_origin="http://central", allow_http=True,
                          cache_dir=str(tmp_path / "cache"), cache_bytes=1024**2,
                          base_running_tag=BASE_TAG)

    async def check():
        service, posts = _base_health_rig(tmp_path, config)
        try:
            await service.locate_central()
            await service.enroll()
            await service._report_base_health()
            await service._report_base_health()  # no re-post within the same epoch
            epoch = service.registration.authority_epoch
            assert posts == [{"authority_epoch": epoch, "sequence": 1,
                              "running_tag": BASE_TAG, "healthy": True}]
            assert service._base_health_epoch == epoch
        finally:
            await close(service)

    asyncio.run(check())


def test_rejected_base_health_retries_with_new_sequence(tmp_path):
    config = PlayerConfig(central_origin="http://central", allow_http=True,
                          cache_dir=str(tmp_path / "cache"), cache_bytes=1024**2,
                          base_running_tag=BASE_TAG)

    async def check():
        service, posts = _base_health_rig(tmp_path, config, responses=(False, True))
        try:
            await service.locate_central()
            await service.enroll()
            await service._report_base_health()
            assert service._base_health_epoch is None
            await service._report_base_health()
            assert service._base_health_epoch == service.registration.authority_epoch
            await service._report_base_health()
            assert [post["sequence"] for post in posts] == [1, 2]
        finally:
            await close(service)

    asyncio.run(check())


def test_no_base_health_when_no_tag_handed_forward(tmp_path):
    # Flashed/D0 or the 0010 global `.deb` path: no served tag was handed
    # forward, so the player posts no base-health (regression guard on d).
    config = PlayerConfig(central_origin="http://central", allow_http=True,
                          cache_dir=str(tmp_path / "cache"), cache_bytes=1024**2)

    async def check():
        service, posts = _base_health_rig(tmp_path, config)
        try:
            await service.locate_central()
            await service.enroll()
            await service._report_base_health()
            assert posts == []
            assert service._base_health_epoch is None
        finally:
            await close(service)

    asyncio.run(check())
