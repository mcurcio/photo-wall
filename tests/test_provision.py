"""0009 slice 2 -- the base bootstrapper (appliance/provision.py).

All external effects (mDNS discovery, HTTP central, dpkg, systemctl) are
faked or injected: this is not a real Pi boot, which per the task brief is
the owner's bench step. The HTTP fake mirrors `tests/test_bootstrap.py`'s
`Opener`/`Response` pattern used for `appliance/bootstrap.py`'s own
`Fetcher`, so `AppFetcher` is exercised end to end (request path, headers,
streaming, size bound) rather than mocked away.
"""

import asyncio
import functools
import hashlib
import io
import json
import urllib.error
from concurrent.futures import Future
from urllib.parse import urlsplit

import pytest

from appliance.provision import (
    AppUnconfigured,
    Bootstrapper,
    ProvisionError,
    apt_install,
    fetch_manifest,
    fetch_package,
    start_player_unit,
    write_public_config,
)
from player.identity import load_identity
from player.rendering import RecordingRenderer
from player.service import PlayerService, load_config

BODY = b"pretend photo-wall-player_1.2.3+gabc.deb bytes"
SHA256 = hashlib.sha256(BODY).hexdigest()
ORIGIN = "http://central.local:8080"


def immediate(callback):
    future = Future()
    try:
        future.set_result(callback())
    except Exception as error:
        future.set_exception(error)
    return future


class Response:
    def __init__(self, body=b"", *, status=200, headers=None):
        self.status = status
        self.body = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, size):
        return self.body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.body.close()


class Central:
    """Fake `_photowall._tcp` central: `/v1/app/manifest` and
    `/v1/app/package/{sha256}.deb`, path-routed like the real one
    (`central/app.py:458`, `:464`).
    """

    def __init__(self, *, manifest_status=200, manifest_body=b"", package_status=200,
                 package_body=b""):
        self.manifest_status = manifest_status
        self.manifest_body = manifest_body
        self.package_status = package_status
        self.package_body = package_body
        self.requests = []

    def open(self, request, **kwargs):
        self.requests.append(request.full_url)
        path = urlsplit(request.full_url).path
        if path == "/v1/app/manifest":
            status, body = self.manifest_status, self.manifest_body
        elif path.startswith("/v1/app/package/"):
            status, body = self.package_status, self.package_body
        else:
            raise AssertionError(f"unexpected path {path}")
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, None)
        return Response(body, headers={"Content-Length": str(len(body))})


def manifest_json(*, sha256=SHA256, size=len(BODY), version="1.2.3+gabc"):
    return json.dumps({"version": version, "sha256": sha256, "size": size}).encode()


class FakeDiscovery:
    def __init__(self, origin):
        self.origin = origin
        self.calls = 0

    async def discover(self):
        self.calls += 1
        return self.origin


class NeverDiscovery:
    def __init__(self):
        self.calls = 0

    async def discover(self):
        self.calls += 1
        return None


class OrderRecorder:
    """Records install/write_origin/start_unit calls in the order the
    bootstrapper makes them, plus their arguments."""

    def __init__(self):
        self.order = []

    def install(self, package, manifest):
        self.order.append(("install", package, manifest))

    def write_origin(self, origin):
        self.order.append(("write_origin", origin))

    def start_unit(self):
        self.order.append(("start_unit",))


class Sleeps:
    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


def never_called(*_args, **_kwargs):
    raise AssertionError("must not be called")


# --- AppFetcher / manifest+package fetch discipline -------------------------


def test_fetch_manifest_and_package_roundtrip_and_content_length_enforced():
    central = Central(manifest_body=manifest_json(), package_body=BODY)
    manifest = fetch_manifest(ORIGIN, opener=central)
    assert manifest == {"version": "1.2.3+gabc", "sha256": SHA256, "size": len(BODY)}
    package = fetch_package(ORIGIN, manifest, opener=central)
    assert package == BODY
    assert central.requests[0] == ORIGIN + "/v1/app/manifest"
    assert central.requests[1] == ORIGIN + f"/v1/app/package/{SHA256}.deb"


def test_fetch_manifest_503_raises_app_unconfigured():
    central = Central(manifest_status=503)
    with pytest.raises(AppUnconfigured):
        fetch_manifest(ORIGIN, opener=central)


def test_fetch_package_oversize_body_rejected():
    central = Central(package_body=b"x" * 10)
    manifest = {"version": "v", "sha256": "a" * 64, "size": 1}
    with pytest.raises(ProvisionError, match="provision_limit"):
        fetch_package(ORIGIN, manifest, opener=central)


# --- Bootstrapper orchestration ----------------------------------------------


def test_bootstrapper_happy_path_installs_handsoff_origin_then_starts(tmp_path):
    central = Central(manifest_body=manifest_json(), package_body=BODY)
    discovery = FakeDiscovery(ORIGIN)
    recorder = OrderRecorder()
    bootstrapper = Bootstrapper(
        discovery=discovery,
        fetch_manifest=functools.partial(fetch_manifest, opener=central),
        fetch_package=functools.partial(fetch_package, opener=central),
        install=recorder.install,
        write_origin=recorder.write_origin,
        start_unit=recorder.start_unit,
        sleep=Sleeps(),
    )
    result = asyncio.run(bootstrapper.run(max_attempts=1))
    assert result is True
    # Order matters: never start the unit before the origin handoff, never
    # hand off the origin before the bytes are installed.
    assert recorder.order == [
        ("install", BODY, {"version": "1.2.3+gabc", "sha256": SHA256, "size": len(BODY)}),
        ("write_origin", ORIGIN),
        ("start_unit",),
    ]


def test_bootstrapper_integrity_mismatch_never_installs_and_retries():
    """Mutation probe: skip the sha256 check and this must fail -- install
    would then be called on the corrupt bytes below."""
    corrupt = BODY[:-1] + bytes([BODY[-1] ^ 1])
    central = Central(manifest_body=manifest_json(), package_body=corrupt)
    discovery = FakeDiscovery(ORIGIN)
    sleeps = Sleeps()
    bootstrapper = Bootstrapper(
        discovery=discovery,
        fetch_manifest=functools.partial(fetch_manifest, opener=central),
        fetch_package=functools.partial(fetch_package, opener=central),
        install=never_called,
        write_origin=never_called,
        start_unit=never_called,
        sleep=sleeps,
    )
    result = asyncio.run(bootstrapper.run(max_attempts=3))
    assert result is False
    assert len(sleeps.calls) == 3  # retried every attempt, never installed


def test_bootstrapper_no_manifest_yet_waits_and_retries_without_installing():
    central = Central(manifest_status=503)
    discovery = FakeDiscovery(ORIGIN)
    sleeps = Sleeps()
    bootstrapper = Bootstrapper(
        discovery=discovery,
        fetch_manifest=functools.partial(fetch_manifest, opener=central),
        fetch_package=never_called,
        install=never_called,
        write_origin=never_called,
        start_unit=never_called,
        sleep=sleeps,
    )
    result = asyncio.run(bootstrapper.run(max_attempts=2))
    assert result is False
    assert len(sleeps.calls) == 2


def test_bootstrapper_no_central_bounded_retry_no_crash():
    discovery = NeverDiscovery()
    sleeps = Sleeps()
    bootstrapper = Bootstrapper(
        discovery=discovery,
        fetch_manifest=never_called,
        fetch_package=never_called,
        install=never_called,
        write_origin=never_called,
        start_unit=never_called,
        sleep=sleeps,
    )
    result = asyncio.run(bootstrapper.run(max_attempts=3))
    assert result is False
    assert discovery.calls == 3
    assert len(sleeps.calls) == 3


def test_bootstrapper_eventually_succeeds_once_central_and_app_appear():
    """A base with no central, then a central with no app yet, then a
    promoted app -- all bounded retries, one eventual success."""
    central = Central(manifest_status=503)

    class FlakyDiscovery:
        def __init__(self):
            self.calls = 0

        async def discover(self):
            self.calls += 1
            return None if self.calls == 1 else ORIGIN

    def flaky_fetch_manifest(origin):
        # First origin-having attempt still sees no app; second succeeds.
        if flaky_fetch_manifest.calls == 0:
            flaky_fetch_manifest.calls += 1
            return fetch_manifest(origin, opener=central)
        return fetch_manifest(origin, opener=Central(manifest_body=manifest_json()))

    flaky_fetch_manifest.calls = 0
    recorder = OrderRecorder()
    bootstrapper = Bootstrapper(
        discovery=FlakyDiscovery(),
        fetch_manifest=flaky_fetch_manifest,
        fetch_package=lambda origin, manifest: fetch_package(
            origin, manifest, opener=Central(package_body=BODY)
        ),
        install=recorder.install,
        write_origin=recorder.write_origin,
        start_unit=recorder.start_unit,
        sleep=Sleeps(),
    )
    result = asyncio.run(bootstrapper.run(max_attempts=5))
    assert result is True
    assert [step[0] for step in recorder.order] == ["install", "write_origin", "start_unit"]


# --- Origin handoff + resolve_origin precedence ------------------------------


def test_origin_handoff_writes_explicit_central_origin_with_http_opt_in(tmp_path):
    path = tmp_path / "public.json"
    write_public_config(ORIGIN, path=path)
    written = json.loads(path.read_text())
    # A plain-HTTP discovered origin (T0 home-LAN baseline) needs allow_http
    # for PlayerConfig's explicit-origin validator (player/service.py
    # _validate_origin) -- an *explicit* http origin is rejected without it,
    # unlike a *discovered* one (resolve_origin passes allow_http=True only
    # for discovery, service.py:423).
    assert written == {"schema": 1, "central_origin": ORIGIN, "allow_http": True}
    config = load_config(path)
    assert config.central_origin == ORIGIN
    assert config.allow_http is True


def test_origin_handoff_preserves_existing_keys(tmp_path):
    path = tmp_path / "public.json"
    path.write_text(json.dumps({"schema": 1, "cache_bytes": 2 * 1024**2}))
    write_public_config("https://central.local:8443", path=path)
    written = json.loads(path.read_text())
    assert written["cache_bytes"] == 2 * 1024**2
    assert written["central_origin"] == "https://central.local:8443"
    assert "allow_http" not in written


def test_origin_handoff_makes_app_skip_rediscovery(tmp_path):
    """Mutation probe: skip the origin-handoff write and this must fail --
    with no `central_origin` written, `resolve_origin` (player/service.py:419)
    falls through to `self.discovery.discover()`, which this test's discovery
    stub raises on. See also
    tests/test_player_service.py:311
    `test_explicit_origin_wins_over_discovery_and_provider_is_not_consulted`,
    which proves the same precedence in the running app's own test suite.
    """
    path = tmp_path / "public.json"
    write_public_config(ORIGIN, path=path)
    config = load_config(path)

    class RaisingDiscovery:
        async def discover(self):
            raise AssertionError("discovery must not be consulted: central_origin is set")

    service = PlayerService(
        config, load_identity(), (), RecordingRenderer(), immediate,
        discovery=RaisingDiscovery(),
    )
    resolved = asyncio.run(service.resolve_origin())
    assert resolved == ORIGIN


# --- dpkg / systemctl are thin, replaceable shell-outs -----------------------


def test_apt_install_and_start_unit_are_real_but_easily_stubbed(monkeypatch, tmp_path):
    """Not exercised against real apt-get/systemctl (no such sandbox in CI --
    the owner's Pi bench step covers that); this just proves the default
    implementations are simple, injectable `subprocess.run` calls, matching
    the brief's "thin, injectable/mockable step" requirement.

    The default install is `apt-get install -y <deb path>` (not `dpkg -i`) so
    the Player .deb's declared Depends resolve from the base's distro sources
    (0009 p4-deb-full-depends): a bare dpkg unpack would leave them unsatisfied.
    """
    calls = []
    monkeypatch.setattr(
        "appliance.provision.subprocess.run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    apt_install(BODY, {"sha256": SHA256})
    start_player_unit()
    args, kwargs = calls[0]
    assert args[0][:3] == ["apt-get", "install", "-y"]
    # A local .deb file path, not a bare package name to look up.
    assert args[0][3].endswith(".deb")
    # Non-interactive so the boot-time install never blocks on a prompt.
    assert kwargs["env"]["DEBIAN_FRONTEND"] == "noninteractive"
    assert calls[1][0][0] == ["systemctl", "start", "photo-wall-player.service"]
