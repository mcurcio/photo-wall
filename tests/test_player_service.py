"""Control-service authority and network bounds; simulated renderer is not Pi evidence."""

import ast
import asyncio
import base64
import contextlib
import copy
import functools
import hashlib
import itertools
import json
import logging
import re
import tempfile
import threading
import time
import types
import typing
from concurrent.futures import Future
from pathlib import Path

import httpx
import pytest
import tls_fixture as tls
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError
from support.position import PORTRAIT_ON_WIDE, save_position
from test_executor import binding, layer
from uplink_fakes import FakeReply, FakeTransport, central, finding
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response as Http11Response

from central.db import ProcessTransactionClock
from contracts.continuation import continuation_assignment_id
from contracts.enrollment import Enrollment, OutputReport, enrollment_message
from contracts.models import (
    Commit,
    Continuation,
    Observation,
    Plan,
    PlayerConfiguration,
    Revocation,
)
from contracts.player_control import ControlSelection
from contracts.time import ManualClock
from player.executor import AcquisitionStopped
from player.identity import load_identity
from player.luts import PlayerLuts
from player.mainloop import CONTROL, DispatchRefused, LoopLate, MainLoopDispatcher
from player.output_discovery import (
    OUTPUT_IDS,
    discover_outputs,
    output_app_id,
    weston_ini,
)
from player.rendering import PrepareResult, PresentationResult, RecordingRenderer
from player.service import (
    MAX_JSON,
    BootContext,
    IdentifyOutput,
    MainLoopLate,
    PlayerConfig,
    PlayerService,
    ServiceError,
    StaleFeedback,
    State,
    Unauthorized,
    _ChunkBridge,
    _json,
    central_finder,
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
        "device_id": "device-" + "b" * 64,
        "run_id": "12345678-1234-1234-1234-123456789abc",
    })


def test_identity_is_fresh_signed_and_never_written(tmp_path):
    state = private_dir(tmp_path / "state")
    first, second = load_identity(), load_identity()
    assert first.public_key != second.public_key
    context = boot_context()
    fields = dict(device_id=context.device_id, boot_id=context.run_id)
    proof = first.enrollment("f" * 64, (), **fields)
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(first.public_key)).verify(
        base64.b64decode(proof.signature), enrollment_message(proof.nonce, (), **fields))
    assert not list(state.iterdir())
    assert "_key=" not in repr(first)


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


def test_config_accepts_an_http_root(tmp_path):
    """R2 makes an http root legal."""
    path = tmp_path / "public.json"
    path.write_text(json.dumps({"schema": 1, "central_origin": "http://192.0.2.10:8000"}))
    assert load_config(path).saved_root() == Origin.parse_root("http://192.0.2.10:8000")
    assert PlayerConfig().saved_root() is None


def test_config_is_strict_bounded_public_json(tmp_path):
    path = tmp_path / "public.json"
    path.write_text(json.dumps({"schema": 1, "central_origin": "https://central:8443/",
                              "cache_dir": str(tmp_path), "cache_bytes": 1048576}))
    assert load_config(path).central_origin == "https://central:8443/"
    for data in ({"unexpected": True}, {"cache_bytes": "1048576"}, {"allow_http": True},
                 {"boot_context_file": "/run/photo-wall/boot.json"},
                 {"decoder_limit": 4}, {"texture_budget": 512 * 1024**2}):   # the old renderer's
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
    assert OUTPUT_IDS == ("HDMI-A-1", "HDMI-A-2", "Virtual-1", "Virtual-2")
    for output_id in OUTPUT_IDS:
        assert output_app_id(output_id) == "photo-wall-" + output_id
    assert output_app_id("HDMI-A-3") == "photo-wall-HDMI-A-3"
    for invalid in ("Virtual-3", "DP-1", "../../config"):
        with pytest.raises(ValueError):
            output_app_id(invalid)
    config = weston_ini()
    assert config.count("[output]") == 4
    assert all(f"name={output_id}\napp-ids=photo-wall-{output_id}" in config
               for output_id in OUTPUT_IDS)


def test_numeric_hdmi_connector_suffix_remains_supported(tmp_path):
    path = tmp_path / "card0-HDMI-A-3"
    path.mkdir()
    (path / "status").write_text("connected")
    found = discover_outputs(tmp_path)
    assert found.fault is None
    assert found.outputs[0].output_id == "HDMI-A-3"


_DELIVERIES = itertools.count(1)


def delivered(**fields) -> State:
    """A State as Central sends it: every delivery carries its id, its sequence and Central's
    clock (`Server` stamps the clock again as it answers)."""
    sequence = next(_DELIVERIES)
    fields.setdefault("server_time", 0.0)
    return State(delivery_id=f"{sequence:032x}", delivery_sequence=sequence, **fields)


def repr_digest(data: bytes) -> str:
    return "sha-256=:" + base64.b64encode(hashlib.sha256(data).digest()).decode() + ":"


def media_headers(data: bytes, **headers) -> dict:
    """Central's media answer: the body's type, `Content-Length` and `Repr-Digest` (RFC 9530)."""
    return {"Content-Type": "image/png", "Content-Length": str(len(data)),
            "Repr-Digest": repr_digest(data), **headers}


class PushSocket:
    """The fake Central's session socket (`Server.connect`): it sends the State first and again
    whenever `Server.state` is replaced, as Central's socket pushes on each change (central/app.py
    `session`); `Server.close_sockets` ends it as Central's close does."""

    def __init__(self, server: "Server", *, once: bool = False) -> None:
        self.server = server
        self.sent = None
        self.closed = False
        self.once = once

    async def __aenter__(self):
        self.server.sockets.append(self)
        return self

    async def __aexit__(self, *_):
        self.server.sockets.remove(self)

    def __aiter__(self):
        return self

    async def __anext__(self):
        while not self.closed:
            state = self.server.state
            if state is not None and state is not self.sent:
                self.sent = state
                self.closed = self.once
                return json.dumps({"type": "state", **self.server.state_body()})
            await asyncio.sleep(.005)
        raise StopAsyncIteration


class Server:
    def __init__(self, clock):
        self.clock = clock
        self.epoch = 0
        self.requests = []
        self.sockets: list[PushSocket] = []
        self.connects = []
        self.proofs = []
        self.state = None
        self.data = b"picture"
        self.media_response = None
        self.time_epoch = None
        self.time_player = None
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
                    proof.nonce, proof.outputs, proof.device_id, proof.boot_id))
            self.epoch += 1
            self.player_id = "p-" + hashlib.sha256(proof.device_id.encode()).hexdigest()[:32]
            return httpx.Response(200, json={"player_id": self.player_id,
                "authority_epoch": self.epoch, "token": str(self.epoch) * 32})
        if path == "/v1/player/time":
            return httpx.Response(200, json={
                "player_id": self.time_player or self.player_id,
                "authority_epoch": self.time_epoch or self.epoch,
                "server_time": self.clock.utc() + self.time_offset,
            })
        if path == "/v1/player/hello":
            offer = json.loads(request.content)
            return httpx.Response(200, json={"authority_epoch": offer["authority_epoch"],
                                             "schema": 2, "capabilities": []})
        if path.startswith("/v1/media/"):
            return self.media_response or httpx.Response(200, content=self.data,
                headers=media_headers(self.data))
        if path == "/v1/player/state":    # no such route: Central pushes the state (decision 0034)
            return httpx.Response(404, json={"error": "not_found"})
        return httpx.Response(200, json={"accepted": True})

    def state_body(self) -> dict:
        """The State as Central's socket sends it: Central's clock read as it sends, as
        central/app.py `timed_state` does."""
        return {**self.state.model_dump(mode="json"),
                "server_time": self.clock.utc() + self.time_offset}

    def connect(self, uri, **options):
        """The Player's websocket connector, onto this fake's session socket."""
        self.connects.append((uri, options))
        return PushSocket(self)

    def close_sockets(self) -> None:
        for socket in self.sockets:
            socket.closed = True

    async def through_socket(self, service) -> None:
        """The State over the Player's own socket loop (`_websocket_loop`): sent once, then the
        socket closes, which ends the loop as a session's close does."""
        connect = service.link.websocket_connect
        service.link.websocket_connect = lambda uri, **options: PushSocket(self, once=True)
        try:
            with pytest.raises(ServiceError, match="session_closed"):
                await service._websocket_loop()
        finally:
            service.link.websocket_connect = connect

    async def push(self, service) -> None:
        """The session socket sending the State now, as the Player's `_websocket_loop` receives
        it (a show-clock sample on the Player's own clock)."""
        now = service.clock.monotonic()
        await service.receive_state(self.state_body(), now, now)

    def offer(self, *, revision=1, layers=None, commits=(), revocations=(), absent=False,
              bindings=None, configuration_revision=1, continuations=()):
        bindings = (binding(),) if bindings is None else bindings
        configuration = PlayerConfiguration(player_id=self.player_id, authority_epoch=self.epoch,
            configuration_revision=configuration_revision, bindings=bindings,
            enabled_outputs=tuple(bound.output_id for bound in bindings))
        plan = Plan(plan_id="offered", revision=revision, player_id=self.player_id,
            authority_epoch=self.epoch, issued_at=self.clock.utc(), valid_from=self.clock.utc(),
            valid_until=300, bindings=configuration.bindings,
            layers=(layer(data=self.data),) if layers is None else layers,
            continuations=continuations)
        self.state = delivered(configuration=configuration, plan=None if absent else plan,
            commits=commits, revocations=revocations)
        return self.state


async def rig(tmp_path, *, server=None, negotiate=False, clock=None, probe=True, **options):
    """`clock` is the Pi's; a `server` built on another stands for Central's clock apart from it."""
    directory = tmp_path / "cache"
    directory.mkdir(mode=0o700, exist_ok=True)
    clock = clock or ManualClock(100)
    server = server or Server(clock)
    config = PlayerConfig(central_origin="http://central", cache_dir=str(directory),
                          cache_bytes=1024**2)
    client = httpx.AsyncClient(transport=httpx.MockTransport(server), trust_env=False)
    service = PlayerService(config, load_identity(), (), RecordingRenderer(), immediate,
        find_central=finding("http://central"), trust=TRUST, clock=clock,
        client=client, time_client=client,
        websocket_connect=server.connect, boot_context=boot_context(), luts=PlayerLuts(), **options)
    await service.locate_central()
    await service.enroll()
    if negotiate:
        await service.hello_protocol()
    if probe:
        await service.probe_time()
    server.offer()
    if probe:
        await server.push(service)
    else:
        # Applied without a round trip, so no Central answer has anchored the ShowClock yet.
        await service.dispatch(lambda: service._apply_state(server.state))
    return service, server


def test_an_unsequenced_state_is_refused_at_parse(tmp_path):
    """Central sequences every delivery, so state without its id and sequence is a protocol
    error the session names, never applied. Mutation probe: make the pair optional again."""
    async def check():
        service, server = await rig(tmp_path)
        try:
            body = server.state.model_dump(mode="json")
            del body["delivery_id"], body["delivery_sequence"]
            server.state = types.SimpleNamespace(model_dump=lambda mode: body)
            applied = service._highest_control_sequence
            with pytest.raises(ValidationError):
                await server.push(service)
            assert service._highest_control_sequence == applied
        finally:
            await close(service)
    asyncio.run(check())


def test_player_negotiates_before_state_and_acknowledges_ordered_v2(tmp_path):
    class V2Server(Server):
        def __call__(self, request):
            if request.url.path == "/v1/player/hello":
                self.requests.append(request)
                return httpx.Response(200, json={"authority_epoch": self.epoch,
                    "schema": 2, "capabilities": []})
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
            assert paths.index("/v1/player/hello") < paths.index("/v1/player/control-acks")
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


def test_the_player_offers_no_capability_and_an_identify_cue_changes_nothing(tmp_path):
    """s3-page decision 1: nothing shows on an Output without a Frame, so the hello offers no
    `identify_output` (Central then refuses the console's Identify), and a cue in a State, even
    under a selection that names it, reaches nothing."""
    async def check():
        service, server = await rig(tmp_path)
        try:
            await service.hello_protocol()
            hello = json.loads(next(request.content for request in server.requests
                                    if request.url.path == "/v1/player/hello"))
            assert (hello["schemas"], hello["capabilities"]) == ([2], [])
            service._control_selection = ControlSelection(
                authority_epoch=service.registration.authority_epoch, schema=2,
                capabilities=("identify_output",))
            service.outputs = (OutputReport(output_id="hdmi2", width_px=1920,
                                            height_px=1080, connected=True),)
            config = PlayerConfiguration(player_id=service.registration.player_id,
                authority_epoch=service.registration.authority_epoch,
                configuration_revision=2, bindings=(), enabled_outputs=())
            plain = delivered(configuration=config, plan=None, commits=(), revocations=())
            assert service._apply_state(plain) == "applied"
            renderer = copy.deepcopy(vars(service.renderer))
            cue = IdentifyOutput(request_id="identify-1", output_id="hdmi2",
                authority_epoch=service.registration.authority_epoch, remaining_seconds=10)
            cued = delivered(configuration=config, plan=None, commits=(), revocations=(),
                             identify_output=cue, identify_expires_at=service.clock.utc() + 10)
            assert service._apply_state(cued) == "applied"
            service.tick_main()
            assert vars(service.renderer) == renderer
        finally:
            await close(service)
    asyncio.run(check())


def pending(service) -> list[Observation]:
    """The reports waiting in the service's outbox, taken from it in order."""
    taken = []
    while (report := service._reports.take()) is not None:
        taken.append(report)
    return taken


async def close(service):
    if service.link.time_client is not None and service.link.time_client is not service.link.client:
        await service.link.time_client.aclose()
    if service.link.client is not None:
        await service.link.client.aclose()
    if service.cache:
        service.cache.close()
    service._worker.shutdown(wait=True, cancel_futures=True)


# --- R1: the Player finds Central through find_central (design §2.3) -------------

CMDLINE = Origin.parse_root("http://photo-wall.localdomain")
LOCATED = Origin.parse_root("http://central")


def gateway():
    """The command line's root 301s to Central at http://central (the Server's host)."""
    return FakeTransport({
        str(CMDLINE.url("/v1/locate")): lambda: FakeReply(
            301, location=str(LOCATED.url("/v1/locate"))),
        str(LOCATED.url("/v1/locate")): central})


async def finder_rig(find, handle=None, server=None):
    """`server` is the fake Central `handle` serves, when the test brings its own."""
    clock = ManualClock(100) if server is None else server.clock
    server = server or Server(clock)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle or server), trust_env=False)
    service = PlayerService(PlayerConfig(), load_identity(), (), RecordingRenderer(), immediate,
        find_central=find, trust=TRUST, clock=clock, client=client, time_client=client,
        websocket_connect=server.connect, boot_context=boot_context(), luts=PlayerLuts())
    return service, server


def test_a_cmdline_root_wins_over_the_saved_root(caplog):
    caplog.set_level(logging.INFO, logger="photo_wall.player")
    config = PlayerConfig(central_origin="http://192.0.2.20:8000")
    found = asyncio.run(central_finder(config, Configured(CMDLINE), transport=gateway())())
    assert (found.root, found.source, found.central.origin) == (CMDLINE, "cmdline", LOCATED)
    assert caplog.messages == ["player: the saved central_origin is ignored: "
                               "the kernel command line names http://photo-wall.localdomain"]


@pytest.mark.parametrize("unconfigured", [Unconfigured("absent"), Unconfigured("no_cmdline")])
def test_the_saved_root_is_used_without_a_cmdline_root(unconfigured):
    transport = FakeTransport({str(LOCATED.url("/v1/locate")): central})
    config = PlayerConfig(central_origin="http://central")
    found = asyncio.run(central_finder(config, unconfigured, transport=transport)())
    assert (found.root, found.source, found.central.origin) == (LOCATED, "saved", LOCATED)


def test_without_a_cmdline_or_saved_root_nothing_is_found():
    transport = FakeTransport({str(LOCATED.url("/v1/locate")): central})
    find = central_finder(PlayerConfig(), Unconfigured("absent"), transport=transport)
    with pytest.raises(UplinkError) as excinfo:
        asyncio.run(find())
    assert (excinfo.value.cause, excinfo.value.reason, excinfo.value.detail) == (
        Cause.CONFIGURATION, "absent", "not_discovered")


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


def test_a_run_reaches_a_sustained_session(
        tmp_path, monkeypatch):
    """Drive the control loop through run() and assert the session survives: the websocket
    loop connects exactly once (never torn down and
    reconnected by a crash restart), and the media loop completes a real download."""
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)

    async def check():
        directory = tmp_path / "cache"
        directory.mkdir(mode=0o700)
        clock = ManualClock(100)
        server = Server(clock)
        media_calls, connect_calls = [], []

        def handle(request):
            if request.url.path.startswith("/v1/media/"):
                media_calls.append(request)
            result = server(request)
            if request.url.path == "/v1/enrollment/register":
                server.offer()
            return result

        config = PlayerConfig(central_origin="http://central",
                              cache_dir=str(directory), cache_bytes=1024**2)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False)

        def connect(uri, **options):
            # Central's session socket stays open, pushing the state on each change.
            connect_calls.append(True)
            return server.connect(uri, **options)

        service = PlayerService(config, load_identity(), (), RecordingRenderer(), immediate,
            find_central=finding("http://central"), trust=TRUST, clock=clock,
            client=client, time_client=client, websocket_connect=connect,
            boot_context=boot_context(), luts=PlayerLuts())
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
            # keeps running rather than tearing its tasks down and restarting.
            await asyncio.sleep(.2)
            assert service.registration is not None
            assert connect_calls == [True]
        finally:
            service.stop()
            await asyncio.wait_for(task, 3)
            await close(service)
    asyncio.run(check())


def _until_steady(server, steady, *, fail_first_hello=False, redirect_first_hello=False):
    """Serves `server`, offers a plan the instant enrollment completes, and sets `steady` on
    the first readiness report: only `_control_loop` sends one, i.e. only once run() is blocked
    on its steady-state tasks, the one stable cancellation point every run()-driven test here
    relies on. `fail_first_hello` makes the first hello wait out its pool (one failed cycle:
    since decision 0032 a request Central answers, 503 included, no longer ends one);
    `redirect_first_hello` answers it 301 (one failed cycle named REDIRECT/unexpected)."""
    failed = []

    def handle(request):
        if (fail_first_hello or redirect_first_hello) and not failed and (
                request.url.path == "/v1/player/hello"):
            failed.append(request)
            if redirect_first_hello:
                return httpx.Response(301, headers={
                    "Location": "http://moved.example/v1/player/hello"})
            raise httpx.PoolTimeout("fixture: no slot")
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
    """0014: a failed cycle (here a hello that never left its pool) is followed by a new
    locate; it lands
    on the origin that issued the registration, so the registration is kept (U8) and there is
    no second enrollment."""
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)
    find = finding("http://central")

    async def check():
        service, server = await finder_rig(find)
        steady = asyncio.Event()
        await service.link.client.aclose()
        service.link.client = service.link.time_client = httpx.AsyncClient(transport=httpx.MockTransport(
            _until_steady(server, steady, fail_first_hello=True)))
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


def test_a_clock_probe_that_never_leaves_its_pool_ends_the_cycle(tmp_path, monkeypatch):
    """A clock probe's pool wait is LOCAL/pool: the clock loop does not just fault and keep
    the same clients, it ends the cycle, so the next one builds fresh ones (CentralLink)."""
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)
    find = finding("http://central")

    async def check():
        service, server = await finder_rig(find)
        steady, recovered = asyncio.Event(), asyncio.Event()
        serve = _until_steady(server, steady)
        starved = []

        def handle(request):
            if request.url.path == "/v1/player/time" and steady.is_set():
                if not starved:
                    starved.append(request)
                    raise httpx.PoolTimeout("no slot")
                recovered.set()
            return serve(request)

        await service.link.client.aclose()
        service.link.client = service.link.time_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle))
        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(recovered.wait(), 10)
            assert service.last_fault == "local_pool"
            assert "host=" not in service.last_fault_detail
            assert find.calls == 2
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
            _until_steady(server, steady, redirect_first_hello=True)))
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
            readiness = service._feedback()
            assert readiness.secured == readiness.prepared == ()
            job = service._jobs[0]
            assert await service.download(job)
            service.tick_main()
            readiness = service._feedback()
            assert readiness.prepared == readiness.secured == ("picture",)
            commit = Commit(plan_id="offered", revision=1, authority_epoch=1,
                readiness_sequence=readiness.sequence, assignment_ids=("picture",), committed_at=100)
            server.offer(commits=(commit,))
            await server.push(service)
            observations = pending(service)
            assert [(o.assignment_id, o.status, o.observed_at) for o in observations] == [("picture", "presented", 100)]
            assert service.renderer.outputs["hdmi1"].layers[0].path.read_bytes() == server.data
            server.offer(absent=True)
            await server.push(service)
            service._feedback()
            assert not service._jobs and not service._authorized(job)
            assert service.renderer.outputs["hdmi1"].layers[0].layer.assignment_id == "picture"
            assert all(request.url.host == "central" for request in server.requests)
            assert server.requests[-2].headers["authorization"] == "Bearer " + "1" * 32
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
            assert restarted._feedback().secured == ("picture",)
        finally:
            await close(restarted)
    asyncio.run(check())


def test_media_is_fetched_by_name_and_a_re_made_copy_is_accepted(tmp_path):
    """The Player asks for the plan's variant by name and keeps whatever Central served under it,
    checked against the served `Repr-Digest` and `Content-Length`: a re-make's other bytes (and
    another length) are the same content. Mutation probe 3 (check against the plan's digest)
    turns this red."""
    async def check():
        service, server = await rig(tmp_path)
        try:
            service._feedback()
            job = service._jobs[0]
            remade = b"a re-made picture, other bytes"
            server.media_response = httpx.Response(200, content=remade,
                                                   headers=media_headers(remade))
            server.requests.clear()
            assert await service.download(job)
            (request,) = [r for r in server.requests if r.url.path.startswith("/v1/media/")]
            variant = job.layer.variant
            assert request.url.path == f"/v1/media/{variant.asset_id}/{variant.recipe_id}"
            assert service.cache.path_for(variant).read_bytes() == remade
            assert service._feedback().secured == ("picture",)
        finally:
            await close(service)
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
            await server.push(service)
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
                    await service.request("GET", "/v1/player/time")
            elif kind == "redirect":
                with pytest.raises(UplinkError) as excinfo:
                    await service.request("GET", "/v1/player/time")
                assert (excinfo.value.cause, excinfo.value.reason) == (Cause.REDIRECT, "unexpected")
                assert "location=upstream" in excinfo.value.detail
            else:
                with pytest.raises(ServiceError):
                    await service.request("GET", "/v1/player/time")
            assert len(calls) == 1 and calls[0].host == "central"
        finally:
            await close(service)
    asyncio.run(check())


@pytest.mark.parametrize("kind", ["redirect", "truncated", "wrong_hash", "wrong_type",
                                  "missing_length", "missing_digest", "malformed_digest",
                                  "other_algorithm"])
def test_failed_download_unblocks_worker_and_never_publishes_readiness(tmp_path, kind):
    async def check():
        service, server = await rig(tmp_path)
        try:
            service._feedback()
            picture = media_headers(b"picture")
            responses = {
                "redirect": httpx.Response(302, headers={"Location": "http://upstream/media"}),
                "truncated": httpx.Response(200, content=b"p", headers=picture),
                # Same length, other bytes than the digest Central named.
                "wrong_hash": httpx.Response(200, content=b"corrupt", headers=picture),
                "wrong_type": httpx.Response(200, content=b"picture",
                                             headers={**picture, "Content-Type": "text/plain"}),
                "missing_length": httpx.Response(200, content=b"picture", headers=picture),
                "missing_digest": httpx.Response(200, content=b"picture", headers={
                    key: value for key, value in picture.items() if key != "Repr-Digest"}),
                "malformed_digest": httpx.Response(200, content=b"picture", headers={
                    **picture, "Repr-Digest": "sha-256=:not base64!:"}),
                "other_algorithm": httpx.Response(200, content=b"picture", headers={
                    **picture, "Repr-Digest": "sha-512=:" + base64.b64encode(
                        hashlib.sha512(b"picture").digest()).decode() + ":"}),
            }
            if kind == "missing_length":
                del responses[kind].headers["Content-Length"]
            server.media_response = responses[kind]
            with context_optional_error():
                assert not await asyncio.wait_for(service.download(service._jobs[0]), 2)
            service.tick_main()
            assert service._feedback().secured == ()
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
        with pytest.raises(AcquisitionStopped):
            await asyncio.wait_for(pending, .2)
        with pytest.raises(AcquisitionStopped):
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
            assert not service._feedback().secured
        finally:
            await close(service)
    asyncio.run(check())


@pytest.mark.parametrize("stop", ["session_ends", "plan_moves_on"])
def test_a_download_the_player_stops_is_not_a_download_failure(tmp_path, stop):
    """The Player stops a download part-way through, after its bytes started reaching the cache:
    its session ends (run() cancels the media loop) or Central's next plan revision takes the job's
    authority. That stop is the Player's own, not the content's: the assignment is not secured,
    its next readiness carries no `download` failure, and no `media_download` fault is raised.
    (CI: a session that ended mid-download made the next report say the picture failed.)"""
    async def check():
        service, server = await rig(tmp_path)
        try:
            service._feedback()
            job = service._jobs[0]
            first, rest = asyncio.Event(), asyncio.Event()

            async def body():
                yield server.data[:3]
                first.set()
                await rest.wait()
                yield server.data[3:]

            def serve(request):
                if request.url.path.startswith("/v1/media/"):
                    return httpx.Response(200, headers=media_headers(server.data), content=body())
                return server(request)
            await service.link.client.aclose()
            service.link.client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
            faults = []
            record = service.fault
            service.fault = lambda code, **kw: (faults.append(code), record(code, **kw))
            attempt = asyncio.create_task(service._attempt(
                {}, job, functools.partial(service.download, job), "media_download"))
            await asyncio.wait_for(first.wait(), 2)
            if stop == "session_ends":
                attempt.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(attempt, 2)
            else:
                server.offer(revision=2)
                await server.push(service)
                rest.set()
                await asyncio.wait_for(attempt, 2)
            readiness = service._feedback()
            assert "picture" not in readiness.secured
            assert readiness.failures == (), readiness.failures
            assert "media_download" not in faults, faults
        finally:
            await close(service)
    asyncio.run(check())


@pytest.mark.parametrize("end", ["short", "reset"])
def test_a_download_that_breaks_mid_stream_is_a_download_failure(tmp_path, end):
    """A download whose bytes already reach the cache breaks on the network side: the body ends
    short of its Content-Length, or the connection resets. That is the content's failure, not the
    Player's stop: the next readiness carries a `download` failure and `media_download` is raised.
    (The counterpart of test_a_download_the_player_stops_is_not_a_download_failure.)"""
    async def check():
        service, server = await rig(tmp_path)
        try:
            service._feedback()
            job = service._jobs[0]

            async def body():
                yield server.data[:3]
                await asyncio.sleep(.05)       # the cache worker is waiting for the next chunk
                if end == "reset":
                    raise httpx.ReadError("connection reset")

            def serve(request):
                if request.url.path.startswith("/v1/media/"):
                    return httpx.Response(200, headers=media_headers(server.data), content=body())
                return server(request)
            await service.link.client.aclose()
            service.link.client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
            faults = []
            record = service.fault
            service.fault = lambda code, **kw: (faults.append(code), record(code, **kw))
            await asyncio.wait_for(service._attempt(
                {}, job, functools.partial(service.download, job), "media_download"), 2)
            readiness = service._feedback()
            assert "picture" not in readiness.secured
            assert [failure.code for failure in readiness.failures] == ["download"], \
                readiness.failures
            assert faults == ["media_download"], faults
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
            await server.push(service)
            assert not service._authorized(job)
            service._feedback()
            assert not service._jobs
            server.offer(revision=2, layers=())
            await server.push(service)
            assert not service._authorized(job)
            assert not service.renderer.outputs["hdmi1"].layers
        finally:
            await close(service)
    asyncio.run(check())


def test_central_stepping_its_clock_re_anchors_on_the_next_sample(tmp_path):
    async def check():
        service, server = await rig(tmp_path)
        try:
            before = service.show_clock.timebase()
            server.time_offset = 10
            assert await service.probe_time()
            assert service.show_clock.timebase() == pytest.approx(before + 10)
            assert service.show_clock.uncertainty == pytest.approx(0, abs=.001)
        finally:
            await close(service)
    asyncio.run(check())


def test_a_slow_clock_answer_keeps_readiness(tmp_path):
    """One slow /v1/player/time answer (a 0.3 s round trip) consistent with the anchor is kept and
    never loosens it, and withholds nothing: the prepared assignment stays ready. Main pipeline run
    38586247137 (upstream-faults): one such probe on one Player reported readiness_lost "clock",
    Central could not commit the next wall-wide group, and every Frame of both Players fell back.

    Mutation: a slow sample re-anchors at rtt / 2, or ends the grant -> the readiness or the bound
    below changes."""
    async def check():
        service, server = await rig(tmp_path)

        class SlowBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                service.clock.advance(.15)
                body = json.dumps({"player_id": server.player_id, "authority_epoch": 1,
                                   "server_time": service.clock.utc()}).encode()
                service.clock.advance(.15)
                yield body

        def respond(request):
            return httpx.Response(200, headers={"Content-Type": "application/json"},
                                  stream=SlowBody())
        try:
            service._feedback()
            assert await service.download(service._jobs[0])
            service.tick_main()
            readiness = service._feedback()
            assert readiness.prepared == ("picture",)
            commit = Commit(plan_id="offered", revision=1, authority_epoch=1,
                            readiness_sequence=readiness.sequence, assignment_ids=("picture",),
                            committed_at=service.clock.utc())
            server.offer(commits=(commit,))
            await server.push(service)
            service.link.time_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            assert await service.probe_time()
            assert service.show_clock.diagnostics.rejected == 0
            assert service.show_clock.uncertainty == pytest.approx(0, abs=.001)
            service.tick_main()
            readiness = service._feedback()
            assert readiness.prepared == ("picture",)
            assert not any(failure.code == "clock" for failure in readiness.failures)
            assert [(o.assignment_id, o.status) for o in pending(service)] == [
                ("picture", "presented")]
        finally:
            await close(service)
    asyncio.run(check())


@pytest.mark.parametrize("stage", ["json", "schema"])
@pytest.mark.parametrize("delay", [.2, 1.1])
def test_slow_state_parsing_does_not_change_independent_clock_mapping(
        tmp_path, monkeypatch, stage, delay):
    async def check():
        from player import service as module
        service, server = await rig(tmp_path, probe=False)
        try:
            owner, attribute = (module, "_json") if stage == "json" else (State, "model_validate")
            original = getattr(owner, attribute)
            def delayed(value):
                parsed = original(value)
                service.clock.advance(delay)
                return parsed
            monkeypatch.setattr(owner, attribute, delayed)
            await server.through_socket(service)
            assert service.show_clock.anchored
            assert service.show_clock.uncertainty == pytest.approx(0, abs=.001)
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
                service.clock.advance(.3)
                yield body[20:]
        def respond(request):
            assert request.method == "GET" and request.url.path == "/v1/player/time"
            return httpx.Response(200, headers={"Content-Type": "application/json"},
                                  stream=DelayedBody())
        try:
            service.link.time_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            assert await service.probe_time()
            assert service.show_clock.diagnostics.rtt == pytest.approx(.3)
            assert service.show_clock.diagnostics.rejected == 0
            # The late body widened the sample's interval, which holds the rig's anchor: kept,
            # and a slow consistent answer never loosens it.
            assert service.show_clock.uncertainty == pytest.approx(0, abs=.001)
        finally:
            await close(service)
    asyncio.run(check())


@pytest.mark.parametrize("fault", ["dispatch_delay", "parse_delay"])
def test_a_late_dispatch_or_slow_parse_never_moves_the_show_clock(tmp_path, monkeypatch, fault):
    """The receipt was read off the wire; neither its dispatch to the main thread nor parsing the
    body is transport latency, so neither changes the sample."""
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
                    service.clock.advance(1.1)
                    return parsed
                monkeypatch.setattr(module, "_json", parse)
            before = service.show_clock.timebase()
            assert await service.probe_time()
            assert service.show_clock.timebase() == before
            assert service.show_clock.diagnostics.rejected == 0
        finally:
            await close(service)
    asyncio.run(check())


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


def test_a_late_main_loop_raises_the_health_fault_and_never_withdraws_readiness(
        tmp_path, monkeypatch):
    """G8, E-G8-8 decision: the GLib loop stops running dispatches (a starved control queue).
    The Player raises main_loop_late on its health channel and posts nothing on readiness, so
    the readiness Central holds (prepared included) stands; a late report with prepared=()
    would cancel the wall's coordinated groups. The late callbacks never run, and when the loop
    recovers readiness is unchanged, so nothing is posted: Central's holds, nothing withdrawn."""
    monkeypatch.setattr("player.service.DISPATCH_DEADLINE", .05)
    monkeypatch.setattr("player.service.FEEDBACK_INTERVAL", .01)

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
        request = service.request

        async def counted(method, path, **kwargs):
            try:
                return await request(method, path, **kwargs)
            finally:
                if len(readiness_posts(server)) >= posted + count:
                    service._stop.set()
        monkeypatch.setattr(service, "request", counted)
        service._stop.clear()
        await asyncio.wait_for(service._control_loop(), 5)
        monkeypatch.setattr(service, "request", request)
        return readiness_posts(server)[posted:]

    async def check():
        service, server = await rig(tmp_path)
        try:
            service.boot_id = "00000000-1111-2222-3333-444444444444"
            service._feedback()
            assert await service.download(service._jobs[0])
            service.tick_main()
            live = (await turn(service, server, 1))[-1]
            assert live["secured"] == ["picture"] and not live["failures"]

            # The production dispatcher (4 slots), through more late cycles than it has slots.
            service.dispatcher = MainLoopDispatcher(StarvedGLib)
            late_cycles = []
            raise_late = service._main_loop_late

            def counted():
                raise_late()
                late_cycles.append(True)
                if len(late_cycles) >= 8:
                    service._stop.set()
            monkeypatch.setattr(service, "_main_loop_late", counted)
            posted = len(readiness_posts(server))
            service._stop.clear()
            await asyncio.wait_for(service._control_loop(), 5)
            assert len(late_cycles) >= 8
            assert readiness_posts(server)[posted:] == []   # readiness untouched
            assert len(StarvedGLib.callbacks) <= 4      # one abandoned post holds the line
            assert service.last_fault == "main_loop_late"
            # The loop runs again: nothing it missed runs late, and reporting resumes.
            ran = []
            monkeypatch.setattr(service, "_apply_state", lambda state: ran.append(state))
            for callback in StarvedGLib.callbacks:
                callback()
            assert ran == []
            service.dispatcher = immediate
            monkeypatch.undo()
            posted = len(readiness_posts(server))
            turns = []
            feedback = service._feedback

            def counted_feedback():
                turns.append(True)
                if len(turns) >= 3:
                    service._stop.set()
                return feedback()
            monkeypatch.setattr(service, "_feedback", counted_feedback)
            service._stop.clear()
            await asyncio.wait_for(service._control_loop(), 5)
            assert readiness_posts(server)[posted:] == []   # unchanged: Central's report holds
            recovered = service.executor.readiness()
            assert recovered.sequence == live["sequence"]    # nothing in between
            assert recovered.failures == () and list(recovered.secured) == live["secured"]
        finally:
            await close(service)
    asyncio.run(check())


def test_a_blocked_main_loop_keeps_its_session_and_resumes_once_the_loop_runs(
        tmp_path, monkeypatch):
    """E-G8-7, through run() with the time loop live and the production dispatcher: a GLib
    loop that stops running dispatches never turns into a dispatch_capacity refusal. The
    Player keeps raising main_loop_late (and posts no readiness) across many deadlines and
    across a session that ends meanwhile (its reconnect's cycle-start dispatches are late, not
    fatal: it re-locates once and that session holds), and reporting resumes once the loop runs
    again. A loop that stays stuck is the broker's probe kill to end."""
    monkeypatch.setattr("player.service.DISPATCH_DEADLINE", .05)
    monkeypatch.setattr("player.service.FEEDBACK_INTERVAL", .01)
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)

    class GLib:
        blocked, queued = False, []

        @classmethod
        def idle_add(cls, callback, *, priority):
            cls.queued.append(callback) if cls.blocked else callback()

    async def check():
        clock = ManualClock(100)
        server = Server(clock)
        posts = []

        def handle(request):
            result = server(request)
            if request.url.path == "/v1/enrollment/register":
                server.offer()
            if request.url.path == "/v1/player/readiness":
                posts.append(json.loads(request.content))
            return result

        find = finding("http://central")
        locates = []

        async def counted_find():
            locates.append(True)
            return await find()

        service, _ = await finder_rig(counted_find, handle, server)
        service.dispatcher = MainLoopDispatcher(GLib)
        faults = []
        record = service.fault
        monkeypatch.setattr(service, "fault",
                            lambda code, **kw: (faults.append(code), record(code, **kw)))

        def late_count():
            return faults.count("main_loop_late")

        async def until(predicate):
            while not predicate():
                await asyncio.sleep(.005)

        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(until(lambda: posts), 5)
            GLib.blocked = True
            # Only the control loop posts, one exchange at a time, so its first late dispatch
            # means no readiness post is still in flight: count posts from there.
            await asyncio.wait_for(until(lambda: late_count() >= 1), 5)
            blocked_at = len(posts)
            await asyncio.wait_for(until(lambda: late_count() >= 6), 5)
            connects, located_at = len(server.connects), len(locates)
            server.close_sockets()          # the session ends while the loop is blocked
            await asyncio.wait_for(until(lambda: len(server.connects) > connects), 5)
            ended_at = late_count()
            await asyncio.wait_for(until(lambda: late_count() >= ended_at + 6), 5)
            # The reconnect's late cycle-start dispatches keep its session: one re-locate,
            # then the session holds (a late start that ended it would re-locate every cycle).
            assert len(locates) <= located_at + 1
            assert service._located
            assert len(posts) == blocked_at         # no readiness while the loop is late
            assert "dispatch_capacity" not in faults
            assert len(GLib.queued) <= 4
            # The loop runs again: the abandoned posts drain and reporting resumes.
            GLib.blocked = False
            for callback in GLib.queued:
                callback()
            await asyncio.wait_for(
                until(lambda: len(posts) > blocked_at), 5)
            assert all(not report["failures"] for report in posts), posts
        finally:
            service.stop()
            await asyncio.wait_for(task, 5)
            await close(service)
    asyncio.run(check())


def test_a_cycle_ends_though_a_library_swallows_its_cancel(monkeypatch):
    """CI 2026-11-19: Central closed the session's socket while the control loop's readiness post
    was connecting; anyio's connect_tcp (under httpx) was cancelling its own happy-eyeballs scope
    at that moment, took run()'s cancel for its own and swallowed it, so the control loop ran on
    and run() waited on it for good: no next cycle ever reached Central. Here the post swallows
    its first cancel the same way (returns as if connected); the cycle must still end and the
    next one open a socket."""
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)
    monkeypatch.setattr("player.cancellation.CANCEL_AGAIN", .05)

    async def check():
        clock = ManualClock(100)
        server = Server(clock)

        def handle(request):
            result = server(request)
            if request.url.path == "/v1/enrollment/register":
                server.offer()
            return result

        service, _ = await finder_rig(finding("http://central"), handle, server)
        connecting, swallowed = asyncio.Event(), []
        request = service.request

        async def readiness_connecting(method, path, **options):
            if path != "/v1/player/readiness":
                return await request(method, path, **options)
            if not swallowed:
                connecting.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    swallowed.append(asyncio.current_task())    # as anyio's cancel scope does
                return {"accepted": True}
            if asyncio.current_task() in swallowed:
                # Its later posts answer too, so no failed request ends that loop by chance.
                await asyncio.sleep(0)
                return {"accepted": True}
            return await request(method, path, **options)

        monkeypatch.setattr(service, "request", readiness_connecting)

        async def until(predicate):
            while not predicate():
                await asyncio.sleep(.005)

        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(connecting.wait(), 5)
            connects = len(server.connects)
            server.close_sockets()          # Central ends the session while the post connects
            await asyncio.wait_for(until(lambda: len(server.connects) > connects), 5)
            assert swallowed, "the post never met run()'s cancel"
        finally:
            service.stop()
            await asyncio.wait_for(task, 5)
            await close(service)
    asyncio.run(check())


def test_stale_feedback_on_a_late_loop_keeps_the_session(tmp_path, monkeypatch):
    """Central refuses readiness as stale (409) and the turn that follows misses the dispatch
    deadline: the Player raises main_loop_late and the control loop carries on (the next turn
    tries again); a late loop never ends the session here. A readiness refused 409 is not
    answered, so it goes again until Central takes it (two refusals, then the third taken)."""
    monkeypatch.setattr("player.service.FEEDBACK_INTERVAL", .01)

    async def check():
        service, server = await rig(tmp_path)
        turns, stale, taken = [], [], []
        request = service.request

        async def refusing(method, path, **kwargs):
            if path == "/v1/player/readiness":
                if len(stale) < 2:
                    stale.append(True)
                    raise StaleFeedback("state_changed")
                taken.append(True)
                service._stop.set()
            return await request(method, path, **kwargs)
        monkeypatch.setattr(service, "request", refusing)
        dispatch = service.dispatch

        async def late_after_each_refusal(callback):
            if callback == service._feedback:
                turns.append(True)
                if len(turns) % 2 == 0:         # the turn after each stale refusal is late
                    raise MainLoopLate("main_loop_late")
            return await dispatch(callback)
        monkeypatch.setattr(service, "dispatch", late_after_each_refusal)
        try:
            await asyncio.wait_for(service._control_loop(), 5)
            assert len(stale) == 2 and taken == [True] and len(turns) == 5
            assert service.last_fault == "main_loop_late"
        finally:
            await close(service)
    asyncio.run(check())


def test_a_late_clock_probe_is_main_loop_late_not_a_clock_fault(tmp_path, monkeypatch):
    """The time loop names a missed dispatch deadline main_loop_late; MainLoopLate is a
    ServiceError, so without its own branch it would read as that ServiceError's code."""
    async def check():
        service, _ = await rig(tmp_path)

        async def late():
            service._stop.set()
            raise MainLoopLate("main_loop_late")
        monkeypatch.setattr(service, "probe_time", late)
        try:
            await asyncio.wait_for(service._time_loop(), 5)
            assert service.last_fault == "main_loop_late"
        finally:
            await close(service)
    asyncio.run(check())


def _time_route_down(server):
    """`server`, with Central's time route answering 503 for the whole session."""
    def handle(request):
        if request.url.path == "/v1/player/time":
            return httpx.Response(503, json={"error": "fixture_outage"})
        return server(request)
    return handle


def test_one_pushed_state_anchors_the_show_clock_while_the_time_route_fails(tmp_path):
    """The 2026-11-15 freeze: Central's time route failed and the Pi had no show time. Now every
    state Central pushes carries Central's clock: one anchors the ShowClock, the plan is held and
    its due layer reported prepared. Mutation: drop the sample in `receive_state` -> no
    readiness."""
    async def check():
        service, server = await rig(tmp_path, probe=False)
        down = httpx.AsyncClient(transport=httpx.MockTransport(_time_route_down(server)))
        service.link.time_client = down
        try:
            with pytest.raises(UplinkError):
                await service.probe_time()
            assert not service.show_clock.anchored
            await server.through_socket(service)
            assert service.show_clock.anchored
            assert service.show_clock.uncertainty == pytest.approx(0, abs=.001)
            service._feedback()
            assert await service.download(service._jobs[0])
            service.tick_main()
            readiness = service._feedback()
            assert readiness.prepared == ("picture",) and readiness.failures == ()
        finally:
            await down.aclose()
            await close(service)
    asyncio.run(check())


def test_run_applies_the_plan_while_the_time_route_fails_and_never_faults_clock_probe(
        tmp_path, monkeypatch):
    """run() asks for no time before its loops; time is asked only in the time loop, whose
    failures are named like any other. Readiness is posted (so the pushed plan was applied on an
    anchored clock) although /v1/player/time never answers. Mutation: put `probe_time` back
    before the loops in run() -> every cycle ends at the probe and no readiness is posted."""
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)

    async def check():
        service, server = await finder_rig(finding("http://central"))
        steady = asyncio.Event()
        faults = []
        recorded = service.fault
        monkeypatch.setattr(service, "fault",
                            lambda code, **kwargs: (faults.append(code), recorded(code, **kwargs)))
        await service.link.client.aclose()
        service.link.client = service.link.time_client = httpx.AsyncClient(
            transport=httpx.MockTransport(_time_route_down(_until_steady(server, steady))))
        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(steady.wait(), 10)
            assert service.show_clock.anchored
            assert "clock_probe" not in faults
        finally:
            service.stop()
            await asyncio.wait_for(task, 10)
            await close(service)
    asyncio.run(check())


def test_first_enrollment_on_a_late_loop_keeps_its_registration(tmp_path, monkeypatch):
    """The executor's create() dispatch misses its deadline at first enrollment: the abandoned
    post never ran, so it is posted again until the loop runs it. The registration stands (one
    register, no re-enrollment that would supersede this boot), as main's unbounded wait did."""
    monkeypatch.setattr("player.service.FEEDBACK_INTERVAL", .01)
    late = [2]

    def dispatcher(callback):
        if late[0]:
            late[0] -= 1
            refused = Future()
            refused.set_exception(LoopLate("main_loop_late"))
            return refused
        return immediate(callback)

    async def check():
        service, server = await finder_rig(finding("http://central"))
        service.dispatcher = dispatcher
        try:
            await service.locate_central()
            await service.enroll()
            registers = [request for request in server.requests
                         if request.url.path == "/v1/enrollment/register"]
            assert len(registers) == 1 and late == [0]
            assert service.executor is not None and service.cache is not None
            assert service.registration is not None
            assert service.last_fault == "main_loop_late"
        finally:
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


def _player_failure_codes() -> set[str]:
    """Every readiness failure code player/ can emit: Failure(code=...) literals, every
    literal assigned to an assignment's `failure`, and the renderer's result codes."""
    def literals(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            return literals(node.body) | literals(node.orelse)
        return set()
    codes = {code for result in (PrepareResult, PresentationResult)
             for code in typing.get_args(typing.get_type_hints(result)["code"])} - {"none"}
    for path in (Path(__file__).resolve().parents[1] / "player").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Failure":
                codes |= {code for keyword in node.keywords if keyword.arg == "code"
                          for code in literals(keyword.value)}
            elif isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Attribute) and target.attr == "failure"
                    and getattr(target.value, "id", None) == "assignment"
                    for target in node.targets):
                codes |= literals(node.value)
    return codes


def test_every_player_failure_code_has_console_recovery_wording():
    """A code the console does not know falls back to the generic wording silently, so the
    operator would never read a new code's recovery. (main_loop_late is a health fault, never a
    readiness failure code: E-G8-8 decision.)"""
    source = (Path(__file__).resolve().parents[1] / "central/console/src/readinessRecovery.js"
              ).read_text()
    table = source[source.index("const RECOVERY"):source.index("});")]
    known = set(re.findall(r"^  ([a-z_]+):", table, re.MULTILINE))
    codes = _player_failure_codes()
    assert {"capacity", "decode", "download", "integrity"} <= codes
    # Show time is a quality, never a failure (decision 0032).
    assert "clock" not in codes and "main_loop_late" not in codes
    assert codes - known == set()


def test_cache_initialization_failure_fails_session_without_local_fallback(tmp_path):
    async def check():
        directory = private_dir(tmp_path / "state")
        clock = ManualClock(100)
        server = Server(clock)
        def fail(*_):
            raise OSError("full volume")
        client = httpx.AsyncClient(transport=httpx.MockTransport(server))
        service = PlayerService(PlayerConfig(central_origin="http://central",
            cache_dir=str(directory)), load_identity(), (), RecordingRenderer(), immediate,
            find_central=finding("http://central"), trust=TRUST, clock=clock,
            client=client, time_client=client, cache_factory=fail,
            boot_context=boot_context(), luts=PlayerLuts())
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
            assert service._feedback().secured == ("picture",)
            path = service.cache.path_for(job.layer.variant)
            if damage == "delete":
                path.unlink()
            else:
                path.write_bytes(b"corrupt")
            worker = asyncio.create_task(service._media_loop())
            async def invalidated():
                while service._feedback().secured:
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
            assert service._feedback().secured == ("picture",)
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
                await asyncio.sleep(.1)
                for callback in GLib.queued:
                    callback()              # the loop runs again
                service.dispatcher = immediate
                yield message

        service.link.websocket_connect = lambda uri, **options: Socket()
        service.dispatcher = MainLoopDispatcher(GLib)
        try:
            with pytest.raises(ServiceError, match="session_closed"):
                await asyncio.wait_for(service._websocket_loop(), 5)
            assert len(applied) == 1 and service.last_fault == "main_loop_late"
        finally:
            await close(service)
    asyncio.run(check())


def test_a_state_pushed_while_the_loop_is_late_is_applied_once_it_runs(tmp_path, monkeypatch):
    """PR 156's end review: Central pushes a state once, when its delivery changes, and the
    Player no longer polls for it. A push whose dispatch misses the deadline (the GLib loop busy at
    cold boot) is held and applied once the loop runs again, with no further push: the Frame never
    stays on its old plan while Central reads the Player connected.

    Mutation probe: drop the held state's retry -> nothing is applied."""
    monkeypatch.setattr("player.service.DISPATCH_DEADLINE", .05)
    monkeypatch.setattr("player.service.FEEDBACK_INTERVAL", .05)

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
                await asyncio.sleep(.1)
                for callback in GLib.queued:
                    callback()              # the loop runs again
                service.dispatcher = immediate
                await asyncio.sleep(.5)     # Central pushes nothing more

        service.link.websocket_connect = lambda uri, **options: Socket()
        service.dispatcher = MainLoopDispatcher(GLib)
        try:
            with pytest.raises(ServiceError, match="session_closed"):
                await asyncio.wait_for(service._websocket_loop(), 5)
            assert len(applied) == 1 and service.last_fault == "main_loop_late"
        finally:
            await close(service)
    asyncio.run(check())


def test_a_state_applied_across_the_deadline_is_acknowledged_not_reported_late(
        tmp_path, monkeypatch):
    """E-G8-10: GLib reaches a delivery's dispatch before DISPATCH_DEADLINE and the apply ends
    after it. The callback ran, so its outcome is the answer: the state is applied once and
    acknowledged, and nothing reads main_loop_late (a late report would withdraw `prepared`)."""
    monkeypatch.setattr("player.service.DISPATCH_DEADLINE", .1)

    class GLib:
        """A GLib thread that reaches each post at 20 ms."""
        threads = []

        @classmethod
        def idle_add(cls, callback, *, priority):
            thread = threading.Timer(.02, callback)
            cls.threads.append(thread)
            thread.start()

    async def check():
        service, server = await rig(tmp_path)
        applied, acked, faults = [], [], []
        record = service.fault
        monkeypatch.setattr(service, "fault",
                            lambda code, **kw: (faults.append(code), record(code, **kw)))

        def slow_apply(state):
            time.sleep(.3)                  # starts at 20 ms, ends past the 100 ms deadline
            applied.append(state)
            return "applied"

        async def ack(state, result):
            acked.append(result)
        monkeypatch.setattr(service, "_apply_state", slow_apply)
        monkeypatch.setattr(service, "_ack_control", ack)
        monkeypatch.setattr(service, "_main", lambda: None)   # each post runs on a timer thread
        message = json.dumps({"type": "state", **server.state.model_dump(mode="json")})

        class Socket:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def __aiter__(self):
                yield message

        service.link.websocket_connect = lambda uri, **options: Socket()
        service.dispatcher = MainLoopDispatcher(GLib)
        try:
            with pytest.raises(ServiceError, match="session_closed"):
                await asyncio.wait_for(service._websocket_loop(), 5)
            assert len(applied) == 1 and acked == ["applied"]
            assert "main_loop_late" not in faults and not service._late_posts.late
        finally:
            for thread in GLib.threads:
                thread.join()
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
        service = PlayerService(PlayerConfig(central_origin="http://central"),
            load_identity(), (), RecordingRenderer(), immediate,
            find_central=finding("http://central"), trust=TRUST, clock=clock,
            client=client, time_client=client, boot_context=boot_context(), luts=PlayerLuts())
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
            if request.url.path == "/v1/player/hello" and unauthorized:
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


def test_observation_renewal_race_takes_the_pushed_state_without_reenrollment(tmp_path):
    """Central answers a report stale while it offers a newer revision: the socket pushes that
    revision, and the report goes again under it, with no re-enrollment and no state request."""
    async def check():
        service, server = await rig(tmp_path)
        task = socket = None
        try:
            service._feedback()
            assert await service.download(service._jobs[0])
            service.tick_main()
            readiness = service._feedback()
            commit = Commit(plan_id="offered", revision=1, authority_epoch=1,
                readiness_sequence=readiness.sequence, assignment_ids=("picture",), committed_at=100)
            server.offer(commits=(commit,))
            await server.push(service)
            assert len(service._reports) == 1
            seen, posted = asyncio.Event(), []
            def handle(request):
                if request.url.path == "/v1/player/observations":
                    posted.append(Observation.model_validate_json(request.content))
                    if len(posted) == 1:
                        server.offer(revision=2, commits=server.state.commits)
                        return httpx.Response(409, json={"error": "stale_observation"})
                    seen.set()
                return server(request)
            await service.link.client.aclose()
            service.link.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            socket = asyncio.create_task(service._websocket_loop())
            task = asyncio.create_task(service._observation_loop())
            await asyncio.wait_for(seen.wait(), 2)
            assert service._plan.revision == 2 and not task.done() and not socket.done()
            assert [report.revision for report in posted] == [1, 2]
            assert service.registration.authority_epoch == 1
            assert not [r for r in server.requests if r.url.path == "/v1/player/state"]
        finally:
            for running in (task, socket):
                if running:
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
            await close(service)
    asyncio.run(check())


def test_a_report_central_answers_stale_is_sent_again_under_the_newer_revision(tmp_path):
    """A report is a change of state, made once (player/executor.py `_report`), so one Central
    answers stale (409: it offered a newer revision meanwhile) is never made again: it waits for
    the Player to hold a newer revision and is sent under it with its number unchanged, and not
    before (a 409 with no newer revision held sends nothing more). Mutation probe: drop the report
    on StaleFeedback, or re-queue it unchanged."""
    async def check():
        service, server = await rig(tmp_path)
        sender = None
        try:
            service._feedback()
            assert await service.download(service._jobs[0])
            service.tick_main()
            readiness = service._feedback()
            server.offer(commits=(Commit(plan_id="offered", revision=1, authority_epoch=1,
                readiness_sequence=readiness.sequence, assignment_ids=("picture",),
                committed_at=100),))
            await server.push(service)
            assert len(service._reports) == 1
            posted, refused = [], asyncio.Event()

            def handle(request):
                if request.url.path == "/v1/player/observations":
                    posted.append(Observation.model_validate_json(request.content))
                    if len(posted) == 1:
                        refused.set()
                        return httpx.Response(409, json={"error": "stale_observation"})
                return server(request)

            await service.link.client.aclose()
            service.link.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            sender = asyncio.create_task(service._observation_loop())
            await asyncio.wait_for(refused.wait(), 2)
            await asyncio.sleep(.2)
            assert len(posted) == 1 and posted[0].revision == 1 and len(service._reports) == 1
            server.offer(revision=2, commits=server.state.commits)
            await server.push(service)

            async def sent_again():
                while len(posted) < 2:
                    await asyncio.sleep(.01)
            await asyncio.wait_for(sent_again(), 2)
            await asyncio.sleep(.1)
            assert posted == [posted[0], posted[0].model_copy(update={"revision": 2})]
            assert not service._reports
        finally:
            if sender:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            await close(service)
    asyncio.run(check())


def test_real_central_http_media_commit_outage_rejoin_and_renewal(registry, tmp_path):
    """Loopback TCP + PostgreSQL + actual Frame media bytes; RecordingRenderer only. The Player
    plays the Scene on the states Central pushes over its real session socket, and never asks for
    its state: its HTTP client records no `GET /v1/player/state` (decision 0034). Mutation probe:
    restore a state poll in the control loop -> a request to that path is recorded."""
    import socket
    import threading
    import time

    import uvicorn
    from fastapi.responses import JSONResponse
    from public_media import MediaCache, make_variant, request_photo
    from support.measured_photos import measure
    from support.photo_pools import photo, seed_pool
    from test_coordination import schedule
    from test_registry import ADMIN, frame, report_modes

    from central.app import create_app
    from central.media_repository import MediaRepository
    from contracts.enrollment import OutputReport

    variant_bytes = b"public synthetic prepared bytes"
    repository = MediaRepository(registry.db, registry.clock,
                                 times=ProcessTransactionClock(registry.clock))
    frame(registry, "portrait")  # the wall's need names the recipe the photo is prepared under
    picture = photo("photo-1", taken_at=1000)   # a photo of Source library:1's pool
    seed_pool(registry, "library:1", (picture,))
    cache = MediaCache(repository, tmp_path / "central-cache")
    prepared = make_variant(cache, request_photo(cache, picture.asset_id), variant_bytes,
                            width=picture.width, height=picture.height)
    measure(registry, picture)   # roadmap 5a: the Planner places only measured photos
    app = create_app(
        registry.db,
        registry.clock,
        ADMIN,
        cache_root=cache.root,
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
    # The lifespan runs the LISTEN that wakes the session socket on each Coordination commit.
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
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
        context = BootContext(device_id=device_id, run_id=boot_id)
        paths = []

        class Recording(httpx.AsyncHTTPTransport):
            async def handle_async_request(self, request):
                paths.append(request.url.path)
                return await super().handle_async_request(request)

        client = httpx.AsyncClient(transport=Recording(), trust_env=False, follow_redirects=False)
        # An http root needs no trust store; the fixture CA keeps the host's out of it.
        trust = Trust.public(tls.write_bundle(tmp_path / "ca.pem", tls.CA))
        transport = HttpTransport(trust=trust)
        service = PlayerService(PlayerConfig(central_origin=origin, cache_dir=str(cache_dir)),
            load_identity(), (OutputReport(output_id="HDMI-A-1", width_px=1920, height_px=1080),),
            RecordingRenderer(), immediate,
            find_central=functools.partial(find_central, Configured(Origin.parse_root(origin)),
                                           transport=transport),
            trust=trust, clock=registry.clock, client=client, time_client=client, boot_context=context, luts=PlayerLuts())
        socket_task = None

        async def until(predicate, seconds=5):
            async def wait():
                while not predicate():
                    if socket_task is not None and socket_task.done():
                        socket_task.result()
                    await asyncio.sleep(.01)
            await asyncio.wait_for(wait(), seconds)

        try:
            assert (await service.locate_central()).origin == Origin.parse_root(origin)
            await service.enroll()
            # The screen's size is the mode Weston runs, from the Node's Output report (issue #124).
            report_modes(registry, device_id, ("HDMI-A-1",), (1920, 1080))
            await service.hello_protocol()
            registered = service.registration
            await service.probe_time()
            # The actual websockets client and Central's endpoint: the Player's one state source.
            socket_task = asyncio.create_task(service._websocket_loop())
            await until(lambda: service._configuration is not None)
            assert service._plan is None
            registry.bind("portrait", registered.player_id, "HDMI-A-1", expected_generation=0, by="console")
            save_position(registry, "portrait", PORTRAIT_ON_WIDE)
            schedule(app.state.coordinator, ["portrait"], starts=1000, media=True)
            await until(lambda: service._plan is not None and service._plan.layers)
            service._feedback()
            offered = service._jobs[0]
            assert await service.download(offered)
            service.tick_main()
            readiness = service._feedback()
            assert offered.layer.assignment_id in readiness.prepared
            assert await service.request("POST", "/v1/player/readiness",
                body=readiness.model_dump(mode="json")) == {"accepted": True}
            observations = []

            def presented():
                observations.extend(pending(service))
                return any(report.status == "presented" for report in observations)
            await until(presented)
            observations = [report for report in observations if report.status == "presented"]
            assert observations and observations[0].status == "presented"
            await service.request("POST", "/v1/player/observations",
                                  body=observations[0].model_dump(mode="json"))
            assert service.cache.path_for(prepared).read_bytes() == variant_bytes
            # The control loop's turns, as a session runs them: feedback and readiness, no state.
            control = asyncio.create_task(service._control_loop())
            await asyncio.sleep(1.2)
            control.cancel()
            await asyncio.gather(control, return_exceptions=True)
            assert not socket_task.done()
            unavailable = True
            # The outage middleware answers 503 with Central's own {"error": ...} body, so
            # the exchange names it CENTRAL/error (not the bare HTTP/status a gateway would get).
            with pytest.raises(UplinkError) as excinfo:
                await service.probe_time()
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
            service._reports.put(observations[0])
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
            # The old enrollment's socket wakes to find its authority gone, and is closed.
            await asyncio.wait_for(asyncio.gather(socket_task, return_exceptions=True), 5)
            old = await client.get(origin + "/v1/player/time",
                                   headers={"Authorization": "Bearer " + registered.token})
            assert old.status_code == 401
            await service.hello_protocol()
            app.state.coordinator.advance()
            socket_task = asyncio.create_task(service._websocket_loop())
            await until(lambda: service._configuration.authority_epoch == 2)
            service._feedback()
            assert service._configuration.authority_epoch == 2
            assert not service._authorized(offered)
            assert paths and "/v1/player/state" not in paths
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


def test_frames_black_with_no_executor_cannot_tell(tmp_path):
    """None, never (): an empty report would claim nothing is black."""
    async def scenario():
        service, _server = await rig(tmp_path)
        try:
            assert service.frames_black() is not None
            service.executor = None
            assert service.frames_black() is None
        finally:
            await close(service)

    asyncio.run(scenario())


def test_a_photo_a_continuation_lists_is_still_fetched_past_its_layer(tmp_path):
    """Decision 0032: a layer's bytes are wanted while a continuation of the held plan names it,
    past the layer's own end; a layer no continuation names is not fetched once it ended."""
    async def check():
        service, server = await rig(tmp_path)
        try:
            listed = layer("listed", data=server.data, end=150)
            unlisted = layer("unlisted", data=b"other", end=150)
            server.offer(revision=2, layers=(listed, unlisted), continuations=(Continuation(
                output_id="hdmi1", run_id="run", photos=("listed",), start=150, seconds=10),))
            await server.push(service)
            service.clock.advance(100)                 # 200: both layers have ended
            service._feedback()
            assert [job.layer.assignment_id for job in service._jobs] == ["listed"]
            assert service._authorized(service._jobs[0])
        finally:
            await close(service)
    asyncio.run(check())


def test_a_failed_request_inside_a_session_keeps_the_session_and_the_next_download_proceeds(
        tmp_path, monkeypatch):
    """C1, decision 0032: on 2026-11-15 one Central answer of 503 ended the whole session and its
    downloads. A failed request (here the clock probe) is now a fault and the next turn tries
    again: the session stands (no second locate) and the held plan's photo is downloaded once
    Central answers media again. Mutation probe: re-raise the error from `_time_loop`."""
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)
    monkeypatch.setattr("player.service.MEDIA_RETRY_BACKOFF", (0,) * 4)
    find = finding("http://central")

    async def check():
        service, server = await finder_rig(find)
        steady, downloaded = asyncio.Event(), asyncio.Event()
        serve = _until_steady(server, steady)
        failed = []

        def handle(request):
            path = request.url.path
            if path == "/v1/player/time" and steady.is_set() and not failed:
                failed.append(request)
                return httpx.Response(503, json={"error": "lock_busy"})
            if path.startswith("/v1/media/"):
                if not failed:
                    return httpx.Response(503, json={"error": "lock_busy"})
                downloaded.set()
            return serve(request)

        await service.link.client.aclose()
        service.link.client = service.link.time_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle))
        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(downloaded.wait(), 10)
            assert failed
            assert find.calls == 1 and len(server.proofs) == 1
        finally:
            service.stop()
            await asyncio.wait_for(task, 10)
            await close(service)
    asyncio.run(check())


def test_a_session_that_ends_keeps_the_held_plans_downloads(tmp_path, monkeypatch):
    """C2, decision 0032: a session's end never empties what the Player still has to fetch for the
    plan it holds; the next `_feedback` lists the held plan's missing photos again. Mutation probe:
    leave `_jobs` empty until a new plan."""
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)

    async def check():
        service, _server = await rig(tmp_path)
        located = []

        async def unreachable():
            located.append(True)
            raise UplinkError(Cause.CONNECT, "refused")

        try:
            service._feedback()
            assert [job.layer.assignment_id for job in service._jobs] == ["picture"]
            service._find_central, service._located = unreachable, False
            task = asyncio.create_task(service.run())
            try:
                async def cycles():
                    while len(located) < 2:            # the first cycle ended and its cleanup ran
                        await asyncio.sleep(.01)
                await asyncio.wait_for(cycles(), 5)
                service._feedback()
                assert [job.layer.assignment_id for job in service._jobs] == ["picture"]
            finally:
                service.stop()
                await asyncio.wait_for(task, 5)
        finally:
            await close(service)
    asyncio.run(check())


def test_an_earlier_revisions_continuation_observation_reaches_central_after_an_outage(tmp_path):
    """C3, decision 0032: a continuation slot's "presented", observed under a revision Central has
    since replaced, is still sent (Central records it against its source photo), and a failed post
    is tried again rather than lost. Mutation probe: drop earlier revisions in
    `_observation_current`."""
    async def check():
        service, server = await rig(tmp_path)
        try:
            continuation = Continuation(output_id="hdmi1", run_id="run", photos=("listed",),
                                        start=150, seconds=10)
            listed = layer("listed", data=server.data, end=150)
            server.offer(revision=2, layers=(listed,), continuations=(continuation,))
            await server.push(service)
            seen = Observation(plan_id="offered", revision=2,
                               authority_epoch=service.registration.authority_epoch,
                               assignment_id=continuation_assignment_id(continuation, 0, "listed"),
                               sequence=1, observed_at=150, status="presented")
            server.offer(revision=3, layers=(listed,), continuations=(continuation,))
            await server.push(service)
            assert service._observation_current(seen)

            posted = []

            def handle(request):
                if request.url.path == "/v1/player/observations":
                    posted.append(Observation.model_validate_json(request.content))
                    if len(posted) == 1:
                        return httpx.Response(503, json={"error": "lock_busy"})
                return server(request)

            await service.link.client.aclose()
            service.link.client = service.link.time_client = httpx.AsyncClient(
                transport=httpx.MockTransport(handle))
            pending(service)                 # the ticks' own reports, sent apart
            service._reports.put(seen)
            sender = asyncio.create_task(service._observation_loop())
            try:
                async def sent_twice():
                    while len(posted) < 2:
                        await asyncio.sleep(.01)
                await asyncio.wait_for(sent_twice(), 5)
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            assert posted == [seen, seen] and not service._reports
        finally:
            await close(service)
    asyncio.run(check())


def test_the_frames_report_states_since_when_a_frame_plays_its_continuation(tmp_path):
    """Decision 0032: the Player line's `frames` carries each Frame's `continuing_since`, the
    executor's fact (Galleria reads it through the Pi's read-only door)."""
    async def check():
        service, _server = await rig(tmp_path)
        try:
            assert service.frame_reports().frames[0].continuing_since is None
            service.executor.continuing_since = lambda bound: 1234.5
            assert service.frame_reports().frames[0].continuing_since == 1234.5
        finally:
            await close(service)
    asyncio.run(check())


def test_a_first_request_that_fails_still_starts_the_sessions_loops(tmp_path, monkeypatch):
    """Decision 0032 (SLICE-C's invariant: the held plan's downloads continue while Central answers
    any request): the session's first clock probe answering 503 is a fault like any other
    request's, not the cycle's end; the loops run, readiness is posted, and no second locate
    happens. Mutation probe: let the time loop's failure end the cycle."""
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)
    find = finding("http://central")

    async def check():
        service, server = await finder_rig(find)
        steady = asyncio.Event()
        serve = _until_steady(server, steady)
        failed = []

        def handle(request):
            if request.url.path == "/v1/player/time" and not failed:
                failed.append(request)
                return httpx.Response(503, json={"error": "lock_busy"})
            return serve(request)

        await service.link.client.aclose()
        service.link.client = service.link.time_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle))
        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(steady.wait(), 10)
            assert failed and find.calls == 1 and len(server.proofs) == 1
        finally:
            service.stop()
            await asyncio.wait_for(task, 10)
            await close(service)
    asyncio.run(check())


def test_a_failed_readiness_post_keeps_the_session_and_the_next_turn_posts_again(
        tmp_path, monkeypatch):
    """C1, decision 0032: a readiness post Central answers 503 is a fault, never the session's end;
    the next turn posts readiness again. Mutation probe: drop `_request_failed` after the post."""
    monkeypatch.setattr("player.service.BACKOFF", (.001,) * 4)
    monkeypatch.setattr("player.service.FEEDBACK_INTERVAL", .01)
    find = finding("http://central")

    async def check():
        service, server = await finder_rig(find)
        steady = asyncio.Event()
        serve = _until_steady(server, steady)
        posts = []

        def handle(request):
            if request.url.path == "/v1/player/readiness":
                posts.append(request)
                if len(posts) == 1:
                    return httpx.Response(503, json={"error": "lock_busy"})
            return serve(request)

        await service.link.client.aclose()
        service.link.client = service.link.time_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle))
        task = asyncio.create_task(service.run())
        try:
            await asyncio.wait_for(steady.wait(), 10)      # the second post was answered
            assert len(posts) >= 2 and find.calls == 1 and len(server.proofs) == 1
            assert service.last_fault is not None
        finally:
            service.stop()
            await asyncio.wait_for(task, 10)
            await close(service)
    asyncio.run(check())


def test_an_observation_central_refuses_is_not_sent_again_and_the_next_one_is(tmp_path):
    """Decision 0032: an observation Central judged and refused (a 4xx) is final: it is not put back
    at the head of the queue, where it would hold back every later one, and the next is posted.
    Mutation probe: treat no refusal as judged."""
    async def check():
        service, server = await rig(tmp_path)
        try:
            epoch = service.registration.authority_epoch
            plan = service.executor._plan
            first, second = (Observation(plan_id=plan.plan_id, revision=plan.revision,
                                         authority_epoch=epoch, assignment_id=key, sequence=number,
                                         observed_at=at, status="presented")
                             for key, number, at in (("picture", 1, 101), ("other", 2, 102)))
            posted = []

            def handle(request):
                if request.url.path == "/v1/player/observations":
                    posted.append(Observation.model_validate_json(request.content))
                    if len(posted) == 1:
                        return httpx.Response(422, json={"error": "observation_refused"})
                return server(request)

            await service.link.client.aclose()
            service.link.client = service.link.time_client = httpx.AsyncClient(
                transport=httpx.MockTransport(handle))
            service._reports.put(first)
            service._reports.put(second)
            sender = asyncio.create_task(service._observation_loop())
            try:
                async def both():
                    while len(posted) < 2:
                        await asyncio.sleep(.01)
                await asyncio.wait_for(both(), 5)
                await asyncio.sleep(.1)
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            assert posted == [first, second] and not service._reports
        finally:
            await close(service)
    asyncio.run(check())


def test_a_state_of_another_registration_never_samples_the_show_clock(tmp_path):
    """Slice A: a Central answer is a show-clock sample only when it names this registration (its
    Player and authority epoch). Mutation probe: sample whatever registration a State names."""
    async def check():
        service, _server = await rig(tmp_path)
        try:
            registration = service.registration
            before = service.show_clock.diagnostics.samples

            def sample(player_id, epoch):
                return service._sample_show_clock(player_id, epoch, 500.0, 10.0, 10.5)

            assert not await service.dispatch(lambda: sample("p-other", registration.authority_epoch))
            assert not await service.dispatch(
                lambda: sample(registration.player_id, registration.authority_epoch + 1))
            assert service.show_clock.diagnostics.samples == before
            assert await service.dispatch(
                lambda: sample(registration.player_id, registration.authority_epoch))
            assert service.show_clock.diagnostics.samples == before + 1
        finally:
            await close(service)
    asyncio.run(check())


async def _drawn_still(tmp_path):
    """A rig whose one still photo ("picture") Central granted and the renderer draws."""
    service, server = await rig(tmp_path)
    service._feedback()
    assert await service.download(service._jobs[0])
    service.tick_main()
    commit = Commit(plan_id="offered", revision=1, authority_epoch=1,
                    readiness_sequence=service.executor.readiness().sequence,
                    assignment_ids=("picture",), committed_at=100)
    server.offer(commits=(commit,))
    await server.push(service)
    return service, server


def test_a_still_photo_reaches_central_once_while_central_commits_then_answers_lock_busy(
        tmp_path, monkeypatch):
    """2026-11-16: a still photo, unchanged, was reported presented every half second, and each
    report Central committed and then answered lock_busy was sent again: 553 rows in six minutes
    for one photo. A report is a change, sent until Central answers it and never again, with one
    number on every send (Central writes each number once: test_shown_history's
    `test_an_observation_sent_again_adds_no_row`). Mutation probe: report presented on every tick
    (drop the `reported` check in `Executor._report`)."""
    monkeypatch.setattr("player.service.FEEDBACK_INTERVAL", .01)

    async def check():
        service, server = await _drawn_still(tmp_path)
        posted = []

        def handle(request):
            if request.url.path == "/v1/player/observations":
                posted.append(Observation.model_validate_json(request.content))
                if len(posted) <= 3:      # committed, then answered too late
                    return httpx.Response(503, json={"error": "lock_busy"})
            return server(request)

        await service.link.client.aclose()
        service.link.client = service.link.time_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handle))
        loops = [asyncio.create_task(service._control_loop()),
                 asyncio.create_task(service._observation_loop())]
        try:
            for _ in range(150):                         # five seconds of 33 ms ticks
                service.clock.advance(.033)
                service.tick_main()
                assert service.renderer.outputs["hdmi1"].layers
                await asyncio.sleep(.002)
            await asyncio.sleep(.1)
        finally:
            for loop in loops:
                loop.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            await close(service)
        assert len(posted) == 4 and posted[0].status == "presented"
        assert all(report == posted[0] for report in posted)
    asyncio.run(check())


def test_a_report_never_holds_the_picture_while_central_is_away(tmp_path):
    """A Central that answers nothing (stuck behind a lock) never stops the picture
    or fills the Player's queue: a still photo drawn for 200 ticks waits as one report, sent once
    Central answers. The old queue held 128 and raised `observation_capacity`, dropping its oldest
    entries. Mutation probe: report on every tick and key `ReportOutbox` by report number (the
    old queue's shape): 200 reports wait, and all are sent."""
    async def check():
        service, server = await _drawn_still(tmp_path)
        faults = []
        recorded = service.fault
        service.fault = lambda code, **kwargs: (faults.append(code), recorded(code, **kwargs))
        posted = []

        def handle(request):
            if request.url.path == "/v1/player/observations":
                posted.append(Observation.model_validate_json(request.content))
            return server(request)

        sender = None
        try:
            for _ in range(200):
                service.clock.advance(.033)
                service.tick_main()
                assert service.renderer.outputs["hdmi1"].layers
            assert "observation_capacity" not in faults
            await service.link.client.aclose()
            service.link.client = service.link.time_client = httpx.AsyncClient(
                transport=httpx.MockTransport(handle))
            sender = asyncio.create_task(service._observation_loop())
            await asyncio.sleep(.2)
        finally:
            if sender:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            await close(service)
        assert [(report.assignment_id, report.status) for report in posted] == [
            ("picture", "presented")]
    asyncio.run(check())


def test_an_idle_player_sends_its_readiness_and_its_acknowledgment_once(tmp_path, monkeypatch):
    """Each turn of the control loop asks the executor for readiness, and Central pushes the state
    over the socket; while neither changes, the Player sends no further readiness and acknowledges
    the same delivery no further (2026-11-16: every half second each was a commit under Central's
    Coordination lock). A change is sent at once: a new delivery is acknowledged, a new readiness
    posted. Mutation probes: post every turn's readiness, or acknowledge every state -> many
    posts."""
    monkeypatch.setattr("player.service.FEEDBACK_INTERVAL", .01)

    async def check():
        service, server = await rig(tmp_path, negotiate=True)
        try:
            def posts(path):
                return [request for request in server.requests if request.url.path == path]

            before = len(posts("/v1/player/control-acks"))
            assert before == 1      # the rig's first state acknowledged its delivery
            loops = [asyncio.create_task(service._control_loop()),
                     asyncio.create_task(service._websocket_loop())]
            await asyncio.sleep(.3)         # about thirty turns of an unchanged state
            assert len(posts("/v1/player/readiness")) == 1
            assert len(posts("/v1/player/control-acks")) == before
            server.offer(revision=2)        # a new delivery: acknowledged, and readiness changes
            await asyncio.sleep(.2)
            for loop in loops:
                loop.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            assert not posts("/v1/player/state")
            assert len(posts("/v1/player/control-acks")) == before + 1
            sent = [json.loads(request.content) for request in posts("/v1/player/readiness")]
            assert [report["revision"] for report in sent] == [1, 2]
            assert sent[0]["sequence"] < sent[1]["sequence"]
        finally:
            await close(service)
    asyncio.run(check())


def _readiness_posts(server):
    return [json.loads(request.content) for request in server.requests
            if request.url.path == "/v1/player/readiness"]


async def _control_turns(service, count):
    """`count` turns of the control loop."""
    turns = []
    feedback = service._feedback

    def counted():
        turns.append(True)
        if len(turns) >= count:
            service._stop.set()
        return feedback()
    service._feedback = counted
    service._stop.clear()
    try:
        await asyncio.wait_for(service._control_loop(), 5)
    finally:
        service._feedback = feedback


def test_a_reenrolled_idle_player_sends_the_first_readiness_of_its_new_epoch(tmp_path, monkeypatch):
    """An idle Player answered readiness 1 in its first epoch; a re-enrollment restarts the
    executor's numbers at 1, so the new epoch's first readiness carries the number Central
    answered in the old one. It is a change of its own and is posted, else Central holds no
    readiness for the new epoch and grants nothing. Mutation: key the answered readiness by its
    sequence alone -> no readiness is posted in epoch 2."""
    monkeypatch.setattr("player.service.FEEDBACK_INTERVAL", .01)

    async def check():
        service, server = await rig(tmp_path)
        try:
            server.offer(revision=2, layers=())
            await server.push(service)
            await _control_turns(service, 4)
            first = _readiness_posts(server)
            assert [(post["authority_epoch"], post["sequence"]) for post in first] == [(1, 1)]
            await service.enroll()
            await service.probe_time()
            server.offer(layers=())
            await server.push(service)
            await _control_turns(service, 4)
            renewed = _readiness_posts(server)[len(first):]
            assert [(post["authority_epoch"], post["sequence"]) for post in renewed] == [(2, 1)]
        finally:
            await close(service)
    asyncio.run(check())


def test_a_readiness_refused_409_is_sent_again_until_central_takes_it(tmp_path, monkeypatch):
    """Central refuses readiness 409 for reasons that clear by themselves (`media_unavailable`
    while a variant is made again, `content_lock_conflict`) as well as for a newer plan: the
    Player cannot tell them apart, so an unchanged readiness refused 409 is posted again on the
    next turn, until Central answers it. Mutation: count a 409 as answered -> one post only, and
    Central never holds the readiness."""
    monkeypatch.setattr("player.service.FEEDBACK_INTERVAL", .01)

    async def check():
        service, server = await rig(tmp_path)
        refusals = []
        respond = server.__call__

        def refusing(request):
            if request.url.path == "/v1/player/readiness" and len(refusals) < 2:
                refusals.append(True)
                server.requests.append(request)
                return httpx.Response(409, json={"error": "media_unavailable"})
            return respond(request)
        await service.link.client.aclose()
        service.link.client = httpx.AsyncClient(transport=httpx.MockTransport(refusing))
        service.link.time_client = service.link.client
        try:
            server.offer(revision=2, layers=())
            await server.push(service)
            await _control_turns(service, 6)
            posts = [(post["authority_epoch"], post["sequence"]) for post in _readiness_posts(server)]
            assert posts == [(1, 1)] * 3, posts      # refused twice, then taken: never again
        finally:
            await close(service)
    asyncio.run(check())
