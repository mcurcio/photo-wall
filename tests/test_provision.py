"""0009 slice 2 -- the base bootstrapper (appliance/provision.py), on uplink (Project 2 S1a).

Central is a scripted `uplink` Transport (tests/uplink_fakes.py): provisioning finds it through
the real `uplink.finder.find_central` and fetches through the real `uplink.fetch.DirectFetch`,
so the request order, the located origin and every named failure are exercised end to end.
dpkg, systemd and mDNS are injected: this is not a real Pi boot, which is the owner's bench step
(the packaged provisioner runs for real in the netboot-e2e device root).
"""

import asyncio
import fcntl
import functools
import hashlib
import json
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from uplink_fakes import FakeReply, FakeTransport, central, finding, located

import uplink.watchdog
from appliance import provision
from appliance.boot_offer import write_handoff as write_boot_handoff
from appliance.provision import (
    APP_MANIFEST_PATH,
    DEVICE_MANIFEST_PATH,
    MAX_APP_PACKAGE_BYTES,
    SERIAL_HEADER,
    AppManifest,
    Bootstrapper,
    Handoff,
    ProvisionError,
    fetch_manifest,
    fetch_package,
    install_package,
    parse_manifest,
    start_player_unit,
    write_handoff,
)
from contracts.central_identity import LOCATE_PATH
from contracts.clock_record import ClockRecord, ClockState
from player.service import load_config
from uplink.causes import Cause, UplinkError
from uplink.diagnosis import failure_text
from uplink.fetch import DirectFetch
from uplink.finder import choose_root, find_central
from uplink.origin import Origin
from uplink.resolver import Configured, Unconfigured

REPO = Path(__file__).resolve().parents[1]
BODY = b"pretend photo-wall-player_1.2.3+gabc.deb bytes"
SHA256 = hashlib.sha256(BODY).hexdigest()
MANIFEST = AppManifest("1.2.3+gabc", SHA256, len(BODY), None)
ORIGIN = "http://central.local:8080"
# The command line names the gateway; locate follows its 301 to Central.
CMDLINE = Origin.parse_root("http://photo-wall.localdomain")
CENTRAL = Origin.parse_root("https://central.example:8443")
DISCOVERED = Origin.parse_root("http://192.0.2.10:8000")
SERIAL = "10000000cafef00d"
TAG = "v9.9.9"


def record(state):
    return ClockRecord(state=state, floor=1790380800, raised_to_floor=False, tier=None,
                       source=None, offset=None, stepped=False, tried=("dhcp:none",),
                       writer="netboot", written_at=1790380810.0)


def reply(body, **kwargs):
    return FakeReply(200, body=body, headers={"Content-Length": str(len(body))}, **kwargs)


def manifest_json(*, sha256=SHA256, size=len(BODY), version="1.2.3+gabc", **extra):
    return json.dumps({"version": version, "sha256": sha256, "size": size, **extra}).encode()


def url(origin, target):
    return str(origin.url(target))


def served(origin, *, manifest=None, package=None):
    """Central's app routes at `origin`, answered fresh on every request."""
    return {url(origin, APP_MANIFEST_PATH): manifest or (lambda: reply(manifest_json())),
            url(origin, f"/v1/app/package/{SHA256}.deb"): package or (lambda: reply(BODY))}


def gateway(**routes):
    """The command line's root 301s to CENTRAL, which serves the identity and the app."""
    return FakeTransport({
        url(CMDLINE, LOCATE_PATH): lambda: FakeReply(301, location=url(CENTRAL, LOCATE_PATH)),
        url(CENTRAL, LOCATE_PATH): central,
        **served(CENTRAL, **routes)})


class Recorder:
    """Records install / write_handoff / start_unit in the order the bootstrapper makes them."""

    def __init__(self):
        self.order = []

    def install(self, package, manifest):
        self.order.append(("install", package, manifest))

    def write_handoff(self, handoff):
        self.order.append(("write_handoff", handoff))

    def start_unit(self):
        self.order.append(("start_unit",))


class Dpkg:
    """subprocess.run as install_package calls it: records each call and what the temp file
    held (and its mode) while dpkg ran; `fail` makes it exit non-zero, `hang` makes it raise
    subprocess.TimeoutExpired instead (outliving INSTALL_SECONDS)."""

    def __init__(self, *, fail=False, hang=False):
        self.calls, self.seen, self.fail, self.hang = [], [], fail, hang

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        deb = Path(argv[-1])
        self.seen.append((deb.read_bytes(), stat.S_IMODE(deb.stat().st_mode)))
        if self.hang:
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        if self.fail:
            raise subprocess.CalledProcessError(1, argv)


class Sleeps:
    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


class Watchdog:
    """Records `uplink.watchdog.extend_start`/`ready` calls in order, so tests can pin M5's
    per-attempt renewal and the single READY=1 on success."""

    def __init__(self):
        self.calls = []

    def extend(self, seconds):
        self.calls.append(("extend", seconds))
        return True

    def ready(self):
        self.calls.append(("ready",))
        return True


class FixedDiscovery:
    def __init__(self, root):
        self.root, self.proofs = root, []

    async def discover(self, unconfigured):
        self.proofs.append(unconfigured)
        return self.root


def never_called(*_args, **_kwargs):
    raise AssertionError("must not be called")


def bootstrapper(transport, *, find=None, clock=None, recorder=None, **options):
    recorder = recorder or Recorder()
    options.setdefault("install", recorder.install)
    options.setdefault("write_handoff", recorder.write_handoff)
    options.setdefault("start_unit", recorder.start_unit)
    options.setdefault("sleep", Sleeps())
    find = find or functools.partial(find_central, Configured(CMDLINE), transport=transport)
    return Bootstrapper(find=find, transport=transport, clock=clock, **options)


def run(bootstrapper, attempts):
    return asyncio.run(bootstrapper.run(max_attempts=attempts))


# --- manifest and package ------------------------------------------------------


@pytest.mark.parametrize("body", [
    b"not json",
    manifest_json(extra="key"),
    json.dumps({"version": "v", "sha256": SHA256}).encode(),
    manifest_json(sha256=SHA256.upper()),
    manifest_json(size=0),
    manifest_json(size=True),
    manifest_json(size=MAX_APP_PACKAGE_BYTES + 1),
    manifest_json(version=""),
    manifest_json(version="v" * 257),
    manifest_json(tag=TAG),
])
def test_a_manifest_that_is_not_exactly_the_contract_is_invalid(body):
    with pytest.raises(ProvisionError, match="provision_manifest_invalid"):
        parse_manifest(body, per_device=False)


def test_the_global_manifest_and_the_package_come_from_the_located_origin():
    transport = FakeTransport(served(Origin.parse_root(ORIGIN)))
    fetch = DirectFetch(located(ORIGIN), transport=transport, seconds=30)
    assert fetch_manifest(fetch, None) == MANIFEST
    assert fetch_package(fetch, MANIFEST) == BODY
    assert transport.urls == [ORIGIN + APP_MANIFEST_PATH, f"{ORIGIN}/v1/app/package/{SHA256}.deb"]
    assert all(SERIAL_HEADER not in headers for _url, headers, *_ in transport.sent)


def test_a_package_over_the_manifest_size_is_a_transfer_limit():
    transport = FakeTransport(served(Origin.parse_root(ORIGIN),
                                     package=lambda: reply(b"x" * (len(BODY) + 1))))
    fetch = DirectFetch(located(ORIGIN), transport=transport, seconds=120)
    with pytest.raises(UplinkError) as raised:
        fetch_package(fetch, MANIFEST)
    assert (raised.value.cause, raised.value.reason) == (Cause.TRANSFER, "limit")


def test_app_unconfigured_arrives_as_central_error():
    transport = FakeTransport(served(Origin.parse_root(ORIGIN), manifest=lambda: FakeReply(
        503, body=b'{"error":"app_unconfigured"}')))
    with pytest.raises(UplinkError) as raised:
        fetch_manifest(DirectFetch(located(ORIGIN), transport=transport, seconds=30), None)
    assert (raised.value.cause, raised.value.reason, raised.value.central_error) == (
        Cause.CENTRAL, "error", "app_unconfigured")


# --- one attempt, end to end ----------------------------------------------------


@pytest.mark.parametrize("clock", [record(ClockState.SYNCED), record(ClockState.UNSYNCED), None])
def test_a_configured_run_locates_fetches_installs_hands_off_then_starts(clock):
    """The clock record changes nothing about the install: dpkg reads no Release file."""
    transport, recorder = gateway(), Recorder()
    assert run(bootstrapper(transport, clock=clock, recorder=recorder), 1) is True
    assert transport.urls == [url(CMDLINE, LOCATE_PATH), url(CENTRAL, LOCATE_PATH),
                              url(CENTRAL, APP_MANIFEST_PATH),
                              url(CENTRAL, f"/v1/app/package/{SHA256}.deb")]
    # Never start the unit before the handoff, never hand off before the install. A command
    # line root is read by the Player itself: the handoff names no Central.
    assert recorder.order == [("install", BODY, MANIFEST),
                              ("write_handoff", Handoff(None, None)),
                              ("start_unit",)]


@pytest.mark.parametrize("source, handed_off", [
    ("cmdline", None), ("saved", None), ("discovered", Origin.parse_root(ORIGIN))])
def test_only_a_discovered_root_is_handed_off(source, handed_off):
    recorder = Recorder()
    find = finding(ORIGIN, source)
    transport = FakeTransport(served(Origin.parse_root(ORIGIN)))
    assert run(bootstrapper(transport, find=find, recorder=recorder), 1) is True
    assert recorder.order[1] == ("write_handoff", Handoff(handed_off, None))


def test_an_unconfigured_run_discovers_with_the_proof_and_hands_the_root_off():
    discovery, recorder = FixedDiscovery(DISCOVERED), Recorder()
    transport = FakeTransport({url(DISCOVERED, LOCATE_PATH): central, **served(DISCOVERED)})
    find = functools.partial(find_central, Unconfigured("absent"), transport=transport,
                             discovery=discovery)
    assert run(bootstrapper(transport, find=find, recorder=recorder), 1) is True
    assert discovery.proofs == [Unconfigured("absent")]
    assert recorder.order[1] == ("write_handoff", Handoff(DISCOVERED, None))


# --- failures: named, logged, retried; TIME leaves ------------------------------


def test_integrity_mismatch_never_installs_and_retries():
    """Mutation probe: skip the sha256 check and this fails -- install would then be called on
    the corrupt bytes below."""
    corrupt = BODY[:-1] + bytes([BODY[-1] ^ 1])
    sleeps = Sleeps()
    subject = bootstrapper(gateway(package=lambda: reply(corrupt)), install=never_called,
                           write_handoff=never_called, start_unit=never_called, sleep=sleeps)
    assert run(subject, 3) is False
    assert len(sleeps.calls) == 3


def test_app_unconfigured_and_a_moved_central_are_logged_named_and_retried(caplog):
    """A 503 app_unconfigured, then a 302 on the package (the gateway moved Central since
    locate): each is one failure_text line and a retry that locates again."""
    manifests = iter([FakeReply(503, body=b'{"error":"app_unconfigured"}'),
                      reply(manifest_json()), reply(manifest_json())])
    packages = iter([FakeReply(302, location="https://elsewhere.example/app.deb"), reply(BODY)])
    transport = gateway(manifest=lambda: next(manifests), package=lambda: next(packages))
    clock, recorder, sleeps = record(ClockState.UNSYNCED), Recorder(), Sleeps()
    caplog.set_level(logging.WARNING, logger=provision.LOG.name)
    subject = bootstrapper(transport, clock=clock, recorder=recorder, sleep=sleeps)
    assert run(subject, 3) is True
    unconfigured = UplinkError(Cause.CENTRAL, "error", host="central.example",
                               detail="app_unconfigured", central_error="app_unconfigured")
    moved = UplinkError(Cause.REDIRECT, "unexpected", host="central.example",
                        detail="status=302;location=elsewhere.example")
    assert caplog.messages == [
        f"provision: {failure_text(unconfigured, clock=clock)} (attempt 1)",
        f"provision: {failure_text(moved, clock=clock)} (attempt 2)"]
    assert len(sleeps.calls) == 2
    assert transport.urls.count(url(CMDLINE, LOCATE_PATH)) == 3     # every attempt locates
    assert [step[0] for step in recorder.order] == ["install", "write_handoff", "start_unit"]


def test_an_untrusted_chain_is_logged_with_the_clock_record(caplog):
    untrusted = UplinkError(Cause.TLS, "untrusted", host="central.example",
                            detail="verify_code=20")
    transport = gateway()
    transport.script[url(CENTRAL, LOCATE_PATH)] = untrusted
    clock = record(ClockState.UNSYNCED)
    caplog.set_level(logging.WARNING, logger=provision.LOG.name)
    assert run(bootstrapper(transport, clock=clock, install=never_called), 1) is False
    assert caplog.messages == [f"provision: {untrusted.console()} {clock.summary()} (attempt 1)"]


def test_no_central_found_is_retried_without_a_crash():
    discovery, sleeps = FixedDiscovery(None), Sleeps()
    transport = FakeTransport({})
    find = functools.partial(find_central, Unconfigured("absent"), transport=transport,
                             discovery=discovery)
    subject = bootstrapper(transport, find=find, install=never_called, sleep=sleeps)
    assert run(subject, 3) is False
    assert len(discovery.proofs) == 3 and len(sleeps.calls) == 3
    assert transport.sent == []


def test_watchdog_extend_renews_before_every_attempt_ready_once_after_start_unit():
    """M5: extend_start(PROVISION_ATTEMPT_TIMEOUT_SECONDS) renews the deadline at the start of
    every attempt (a retried failure, then the successful one); ready() (READY=1) fires exactly
    once, after start_unit -- Type=notify's starting phase does not end without it."""
    manifests = iter([FakeReply(503, body=b'{"error":"app_unconfigured"}'), reply(manifest_json())])
    transport = gateway(manifest=lambda: next(manifests))
    watchdog, recorder, sleeps = Watchdog(), Recorder(), Sleeps()
    subject = bootstrapper(transport, recorder=recorder, sleep=sleeps,
                           watchdog_extend=watchdog.extend, watchdog_ready=watchdog.ready)
    assert run(subject, 2) is True
    assert watchdog.calls == [
        ("extend", provision.PROVISION_ATTEMPT_TIMEOUT_SECONDS),
        ("extend", provision.PROVISION_ATTEMPT_TIMEOUT_SECONDS),
        ("ready",)]
    assert recorder.order[-1] == ("start_unit",)


def test_watchdog_ready_is_never_sent_on_a_failed_or_incomplete_run():
    watchdog, sleeps = Watchdog(), Sleeps()
    subject = bootstrapper(FakeTransport({}), install=never_called, sleep=sleeps,
                           watchdog_extend=watchdog.extend, watchdog_ready=watchdog.ready,
                           find=functools.partial(find_central, Unconfigured("absent"),
                                                  transport=FakeTransport({}),
                                                  discovery=FixedDiscovery(None)))
    assert run(subject, 3) is False
    assert ("ready",) not in watchdog.calls
    assert [call for call in watchdog.calls if call[0] == "extend"] == [
        ("extend", provision.PROVISION_ATTEMPT_TIMEOUT_SECONDS)] * 3


def test_every_attempt_renews_its_window_before_its_first_step():
    """M5: the renewal OPENS each attempt, so the window it grants is that attempt plus the
    backoff after it (what LONGEST_ATTEMPT_SECONDS sums) -- never process start-up plus attempt
    1, and no exit from an attempt can skip it. Mutation probe: renew after the attempt instead
    (the hand reconstruction's placement) and the order below changes."""
    manifests = iter([FakeReply(503, body=b'{"error":"app_unconfigured"}'), reply(manifest_json())])
    transport = gateway(manifest=lambda: next(manifests))
    watchdog = Watchdog()
    events = watchdog.calls
    locate_it = functools.partial(find_central, Configured(CMDLINE), transport=transport)

    async def find():
        events.append(("find",))
        return await locate_it()

    async def sleep(_seconds):
        events.append(("sleep",))

    subject = bootstrapper(transport, find=find, sleep=sleep,
                           start_unit=lambda: events.append(("start_unit",)),
                           watchdog_extend=watchdog.extend, watchdog_ready=watchdog.ready)
    assert run(subject, 2) is True
    window = ("extend", provision.PROVISION_ATTEMPT_TIMEOUT_SECONDS)
    assert events == [window, ("find",), ("sleep",),
                      window, ("find",), ("start_unit",), ("ready",)]


def test_the_attempt_steps_run_under_the_bounds_the_window_sums(monkeypatch):
    """Each term of LONGEST_ATTEMPT_SECONDS is the bound the step actually runs under: the
    manifest and package DirectFetch deadlines here; the backoff's ceiling; dpkg, systemctl and
    the mDNS browse in their own tests below."""
    seconds = []
    original = provision.DirectFetch

    def recording(central, **kwargs):
        seconds.append(kwargs["seconds"])
        return original(central, **kwargs)

    monkeypatch.setattr(provision, "DirectFetch", recording)
    assert run(bootstrapper(gateway()), 1) is True
    assert seconds == [provision.MANIFEST_SECONDS, provision.PACKAGE_SECONDS]
    backoff = provision._default_backoff
    assert max(backoff(attempt) for attempt in range(1, 1000)) == provision.MAX_BACKOFF_SECONDS


def test_eventually_succeeds_once_central_has_an_app():
    """A malformed manifest (ProvisionError), then app_unconfigured, then a promoted app."""
    manifests = iter([reply(b"{}"), FakeReply(503, body=b'{"error":"app_unconfigured"}'),
                      reply(manifest_json())])
    recorder, sleeps = Recorder(), Sleeps()
    subject = bootstrapper(gateway(manifest=lambda: next(manifests)), recorder=recorder,
                           sleep=sleeps)
    assert run(subject, 5) is True
    assert len(sleeps.calls) == 2
    assert [step[0] for step in recorder.order] == ["install", "write_handoff", "start_unit"]


@pytest.mark.parametrize("where", ["locate", "manifest"])
def test_a_time_failure_leaves_run_for_the_reboot_path(where):
    """Nothing in stage 2 steps the clock, so waiting would wait forever (rule 3)."""
    expired = UplinkError(Cause.TIME, "not_yet_valid", host="central.example",
                          detail="verify_code=9")
    transport = gateway()
    transport.script[url(CENTRAL, LOCATE_PATH if where == "locate" else APP_MANIFEST_PATH)] = (
        expired)
    sleeps = Sleeps()
    subject = bootstrapper(transport, clock=record(ClockState.UNSYNCED), install=never_called,
                           sleep=sleeps)
    with pytest.raises(UplinkError) as raised:
        run(subject, 3)
    assert raised.value is expired
    assert sleeps.calls == []


def test_a_failed_dpkg_escapes_run(monkeypatch):
    """The real install_package inside Bootstrapper.run: dpkg's non-zero exit (a Depends the
    base lacks) is not retried in-process; it leaves for the unit's start limit."""
    dpkg, sleeps = Dpkg(fail=True), Sleeps()
    monkeypatch.setattr("appliance.provision.subprocess.run", dpkg)
    with pytest.raises(subprocess.CalledProcessError) as raised:
        run(bootstrapper(gateway(), install=install_package, write_handoff=never_called,
                         start_unit=never_called, sleep=sleeps), 3)
    assert raised.value.cmd[:2] == ["dpkg", "--install"]
    assert dpkg.seen == [(BODY, 0o600)] and not Path(raised.value.cmd[-1]).exists()
    assert sleeps.calls == []


def test_a_hung_dpkg_escapes_run_the_same_way(monkeypatch):
    """The real install_package inside Bootstrapper.run: a dpkg that outlives INSTALL_SECONDS
    (subprocess.TimeoutExpired) is handled exactly like the non-zero exit above -- not retried
    in-process; it leaves for the unit's start limit."""
    dpkg, sleeps = Dpkg(hang=True), Sleeps()
    monkeypatch.setattr("appliance.provision.subprocess.run", dpkg)
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        run(bootstrapper(gateway(), install=install_package, write_handoff=never_called,
                         start_unit=never_called, sleep=sleeps), 3)
    assert raised.value.cmd[:2] == ["dpkg", "--install"]
    assert dpkg.seen == [(BODY, 0o600)] and not Path(raised.value.cmd[-1]).exists()
    assert sleeps.calls == []


# --- the handoff ------------------------------------------------------------------


def test_a_cmdline_handoff_keeps_other_keys_and_drops_central_origin_and_allow_http(tmp_path):
    path = tmp_path / "public.json"
    path.write_text(json.dumps({"schema": 1, "central_origin": "http://old.example:1",
                                "allow_http": True, "cache_bytes": 2 * 1024**2,
                                "base_running_tag": "v1.0.0"}))
    write_handoff(Handoff(None, None), path=path)
    assert json.loads(path.read_text()) == {"schema": 1, "cache_bytes": 2 * 1024**2}


def test_a_discovered_root_is_written_and_is_the_players_saved_root(tmp_path):
    """R2: the Player loads an http root with no allow_http, and uses it only while the
    command line names no Central, without a second browse."""
    path = tmp_path / "public.json"
    path.write_text(json.dumps({"schema": 1, "allow_http": True}))
    write_handoff(Handoff(DISCOVERED, None), path=path)
    assert json.loads(path.read_text()) == {"schema": 1, "central_origin": str(DISCOVERED)}
    saved = load_config(path).saved_root()
    assert saved == DISCOVERED
    assert asyncio.run(choose_root(Unconfigured("absent"), saved=saved,
                                   discovery=FixedDiscovery(None))) == (DISCOVERED, "saved")


def test_the_handoff_is_readable_by_the_player_whatever_the_umask(tmp_path):
    path = tmp_path / "etc" / "photo-wall" / "public.json"
    previous = os.umask(0o077)
    try:
        write_handoff(Handoff(None, None), path=path)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


# --- 0012 bead 6: opt-in per-device `.deb` manifest + served-tag handoff -----------


def device_manifest(**extra):
    return lambda: reply(manifest_json(**extra))


def test_per_device_manifest_fetch_sends_serial_and_returns_served_tag():
    transport = FakeTransport({ORIGIN + DEVICE_MANIFEST_PATH: device_manifest(tag=TAG)})
    fetch = DirectFetch(located(ORIGIN), transport=transport, seconds=30)
    assert fetch_manifest(fetch, SERIAL) == AppManifest("1.2.3+gabc", SHA256, len(BODY), TAG)
    [(sent_url, headers, *_)] = transport.sent
    assert (sent_url, headers[SERIAL_HEADER]) == (ORIGIN + DEVICE_MANIFEST_PATH, SERIAL)


@pytest.mark.parametrize("extra", [{}, {"tag": "not-a-tag"}, {"tag": 1}])
def test_per_device_manifest_missing_or_malformed_tag_is_rejected(extra):
    transport = FakeTransport({ORIGIN + DEVICE_MANIFEST_PATH: device_manifest(**extra)})
    with pytest.raises(ProvisionError, match="provision_manifest_invalid"):
        fetch_manifest(DirectFetch(located(ORIGIN), transport=transport, seconds=30), SERIAL)


def test_opt_in_fetches_the_per_device_manifest_and_hands_the_tag_forward(tmp_path):
    public = tmp_path / "public.json"
    transport = FakeTransport({ORIGIN + DEVICE_MANIFEST_PATH: device_manifest(tag=TAG),
                               **served(Origin.parse_root(ORIGIN))})
    subject = bootstrapper(transport, find=finding(ORIGIN), install=lambda *_: None,
                           write_handoff=functools.partial(write_handoff, path=public),
                           start_unit=lambda: None, serial_reader=lambda: SERIAL)
    assert run(subject, 1) is True
    assert transport.sent[0][1][SERIAL_HEADER] == SERIAL
    assert json.loads(public.read_text()) == {"schema": 1, "base_running_tag": TAG}


def test_an_unreadable_serial_keeps_the_global_manifest():
    def unreadable():
        raise OSError("no devicetree")

    transport = FakeTransport(served(Origin.parse_root(ORIGIN)))
    recorder = Recorder()
    subject = bootstrapper(transport, find=finding(ORIGIN), recorder=recorder,
                           serial_reader=unreadable)
    assert run(subject, 1) is True
    assert transport.urls[0] == ORIGIN + APP_MANIFEST_PATH
    assert recorder.order[1] == ("write_handoff", Handoff(None, None))


def test_frozen_initial_app_fetches_exact_offer_without_mutable_manifest():
    offer_id = "12345678-1234-1234-1234-123456789abc"
    route = f"/v1/netboot/offers/{offer_id}/app"
    transport = FakeTransport({ORIGIN + route: lambda: reply(BODY)})
    recorder = Recorder()
    phases = []
    subject = bootstrapper(transport, find=finding(ORIGIN), recorder=recorder,
                           phase=lambda *args: phases.append(args),
                           boot_handoff={"mode": "offer", "offer_id": offer_id,
                                         "initial_app": {"tag": TAG, "sha256": SHA256,
                                                         "size": len(BODY)}})
    assert run(subject, 1) is True
    assert transport.urls == [ORIGIN + route]
    assert recorder.order[0][0:2] == ("install", BODY)
    assert [phase for phase, _, _ in phases] == [
        "fetching_app", "verifying_app", "installing_app", "starting_app",
        "player_unit_started"]


def test_app_less_offer_keeps_base_ready_without_central_or_install():
    watchdog = Watchdog()
    subject = bootstrapper(FakeTransport({}), find=never_called,
                           install=never_called, start_unit=never_called,
                           boot_handoff={"mode": "offer", "offer_id": "ignored",
                                         "initial_app": None},
                           watchdog_extend=watchdog.extend, watchdog_ready=watchdog.ready)
    assert run(subject, 1) is False
    assert watchdog.calls == [("extend", provision.PROVISION_ATTEMPT_TIMEOUT_SECONDS),
                             ("ready",)]


def test_offer_bytes_with_wrong_size_never_reach_installer():
    offer_id = "12345678-1234-1234-1234-123456789abc"
    route = f"/v1/netboot/offers/{offer_id}/app"
    transport = FakeTransport({ORIGIN + route: lambda: reply(BODY[:-1])})
    recorder = Recorder()
    subject = bootstrapper(transport, find=finding(ORIGIN), recorder=recorder,
                           boot_handoff={"mode": "offer", "offer_id": offer_id,
                                         "initial_app": {"tag": TAG, "sha256": SHA256,
                                                         "size": len(BODY)}})
    assert run(subject, 1) is False
    assert recorder.order == []


def test_schema_two_payload_uses_base_executor_and_never_runs_dpkg():
    offer_id = "12345678-1234-1234-1234-123456789abc"
    abi = "sha256:" + "c" * 64
    route = f"/v1/netboot/offers/{offer_id}/app"
    transport = FakeTransport({ORIGIN + route: lambda: reply(BODY)})

    class Executor:
        calls = []

        def recover(self, **_kwargs):
            return None

        def activate(self, body, **kwargs):
            self.calls.append((body, kwargs))
            return "committed"

    executor = Executor()
    recorder = Recorder()
    subject = bootstrapper(transport, find=finding(ORIGIN), install=never_called,
                           start_unit=never_called, data_executor=executor,
                           write_handoff=recorder.write_handoff,
                           base_abi_reader=lambda: abi,
                           boot_handoff={"schema": 2, "mode": "offer", "offer_id": offer_id,
                                         "base_tag": "v1-base",
                                         "initial_app": {"tag": TAG, "sha256": SHA256,
                                                         "size": len(BODY),
                                                         "format": "pw-player-data-v1",
                                                         "base_abi": abi}})
    assert run(subject, 1) is True
    assert executor.calls == [(BODY, {"sha256": SHA256, "size": len(BODY),
                                     "base_abi": abi, "attempt_id": offer_id,
                                     "expected_base_abi": abi})]
    assert recorder.order == [("write_handoff", Handoff(None, None))]


def test_schema_two_abi_mismatch_never_fetches_or_mutates():
    class Executor:
        def recover(self, **_kwargs):
            return None

    subject = bootstrapper(FakeTransport({}), find=finding(ORIGIN),
                           install=never_called, start_unit=never_called,
                           data_executor=Executor(),
                           base_abi_reader=lambda: "sha256:" + "d" * 64,
                           boot_handoff={"schema": 2, "mode": "offer",
                                         "offer_id": "12345678-1234-1234-1234-123456789abc",
                                         "initial_app": {"tag": TAG, "sha256": SHA256,
                                                         "size": len(BODY),
                                                         "format": "pw-player-data-v1",
                                                         "base_abi": "sha256:" + "c" * 64}})
    assert run(subject, 1) is False


def test_schema_two_repairs_local_attempt_before_central_discovery():
    order = []

    class Executor:
        def recover(self, *, expected_base_abi):
            order.append(("recover", expected_base_abi))
            return None

    async def unavailable():
        order.append(("find",))
        raise ProvisionError("central_unavailable")

    abi = "sha256:" + "c" * 64
    subject = bootstrapper(FakeTransport({}), find=unavailable,
                           data_executor=Executor(), base_abi_reader=lambda: abi,
                           boot_handoff={"schema": 2, "mode": "offer",
                                         "offer_id": "12345678-1234-1234-1234-123456789abc",
                                         "initial_app": {"tag": TAG, "sha256": SHA256,
                                                         "size": len(BODY),
                                                         "format": "pw-player-data-v1",
                                                         "base_abi": abi}})
    assert run(subject, 1) is False
    assert order == [("recover", abi), ("find",)]


def test_schema_two_recovery_waits_for_os_shared_lock_before_central(tmp_path):
    lock_path = tmp_path / "executor.lock"
    lock_path.touch()
    events = []
    watchdog = Watchdog()
    abi = "sha256:" + "c" * 64

    class TracedExecutor(provision.AppExecutor):
        def recover(self, *, expected_base_abi):
            events.append(("recover", expected_base_abi))
            return super().recover(expected_base_abi=expected_base_abi)

    executor = TracedExecutor(roots=tmp_path / "apps", journal=tmp_path / "journal",
                              lock=lock_path, legacy_override=tmp_path / "legacy")
    with lock_path.open("rb") as observer:
        fcntl.flock(observer, fcntl.LOCK_SH | fcntl.LOCK_NB)

        async def release_observer(seconds):
            events.append(("sleep", seconds))
            fcntl.flock(observer, fcntl.LOCK_UN)

        subject = bootstrapper(FakeTransport({}), find=never_called,
                               data_executor=executor, sleep=release_observer,
                               base_abi_reader=lambda: abi,
                               watchdog_extend=watchdog.extend,
                               watchdog_ready=watchdog.ready,
                               boot_handoff={"schema": 2, "mode": "offer",
                                             "initial_app": None})
        assert run(subject, 2) is False  # app-less offer, after recovery succeeds
    assert events == [("recover", abi), ("sleep", provision.EXECUTOR_BUSY_RETRY_SECONDS),
                      ("recover", abi)]
    assert watchdog.calls == [
        ("extend", provision.PROVISION_ATTEMPT_TIMEOUT_SECONDS),
        ("extend", provision.PROVISION_ATTEMPT_TIMEOUT_SECONDS), ("ready",)]


def test_schema_two_busy_recovery_obeys_finite_test_budget(tmp_path):
    lock_path = tmp_path / "executor.lock"
    lock_path.touch()
    waits, phases = [], []
    executor = provision.AppExecutor(roots=tmp_path / "apps", journal=tmp_path / "journal",
                                     lock=lock_path, legacy_override=tmp_path / "legacy")

    async def record_wait(seconds):
        waits.append(seconds)

    with lock_path.open("rb") as observer:
        fcntl.flock(observer, fcntl.LOCK_SH | fcntl.LOCK_NB)
        subject = bootstrapper(FakeTransport({}), find=never_called, data_executor=executor,
                               base_abi_reader=lambda: "sha256:" + "c" * 64,
                               sleep=record_wait, phase=lambda *args: phases.append(args),
                               boot_handoff={"schema": 2, "mode": "offer",
                                             "initial_app": None})
        assert run(subject, 3) is False
    assert waits == [provision.EXECUTOR_BUSY_RETRY_SECONDS] * 2
    assert phases[-1] == ("retry_wait", None, "executor_busy")

def test_schema_two_activation_waits_for_os_shared_lock_without_refetch(tmp_path):
    offer_id = "12345678-1234-1234-1234-123456789abc"
    abi = "sha256:" + "c" * 64
    route = f"/v1/netboot/offers/{offer_id}/app"
    transport = FakeTransport({ORIGIN + route: lambda: reply(BODY)})
    lock_path = tmp_path / "executor.lock"
    calls, phases, waits = [], [], []
    watchdog = Watchdog()

    class LockingExecutor(provision.AppExecutor):
        def activate(self, body, **kwargs):
            calls.append((body, kwargs))
            with self._lock() as locked:
                fcntl.flock(locked, fcntl.LOCK_UN)
            return "committed"

    executor = LockingExecutor(roots=tmp_path / "apps", journal=tmp_path / "journal",
                               lock=lock_path, legacy_override=tmp_path / "legacy")
    recorder = Recorder()
    with lock_path.open("a+b") as observer:
        def hold_during_activation(handoff):
            recorder.write_handoff(handoff)
            fcntl.flock(observer, fcntl.LOCK_SH | fcntl.LOCK_NB)

        async def release_after_long_collection(seconds):
            waits.append(seconds)
            if len(waits) == 7:
                fcntl.flock(observer, fcntl.LOCK_UN)

        subject = bootstrapper(
            transport, find=finding(ORIGIN), install=never_called,
            start_unit=never_called, data_executor=executor,
            write_handoff=hold_during_activation, sleep=release_after_long_collection,
            base_abi_reader=lambda: abi, phase=lambda *args: phases.append(args),
            watchdog_extend=watchdog.extend, watchdog_ready=watchdog.ready,
            boot_handoff={"schema": 2, "mode": "offer", "offer_id": offer_id,
                          "base_tag": "v1-base",
                          "initial_app": {"tag": TAG, "sha256": SHA256, "size": len(BODY),
                                          "format": "pw-player-data-v1", "base_abi": abi}})
        assert run(subject, None) is True
    assert len(waits) == 7 and waits == [provision.EXECUTOR_BUSY_RETRY_SECONDS] * 7
    assert len(calls) == 8 and all(call == calls[0] for call in calls)
    assert calls[0][0] is calls[-1][0]
    assert transport.urls == [ORIGIN + route]
    assert recorder.order == [("write_handoff", Handoff(None, None))]
    assert ("retry_wait", SHA256, "executor_busy") in phases
    assert phases[-1] == ("player_unit_started", SHA256, None)
    assert watchdog.calls == [("extend", provision.PROVISION_ATTEMPT_TIMEOUT_SECONDS)] * 8 + [
        ("ready",)]


def test_schema_two_unrepairable_local_attempt_never_contacts_central():
    order = []

    class Executor:
        def recover(self, **_kwargs):
            order.append("recover")
            raise provision.ExecutorError("player_recovery_unconfirmed")

    async def should_not_find():
        order.append("find")
        raise AssertionError("unrepairable local state must fence network activation")

    phases, watchdog = [], Watchdog()
    subject = bootstrapper(FakeTransport({}), find=should_not_find,
                           data_executor=Executor(),
                           base_abi_reader=lambda: "sha256:" + "c" * 64,
                           phase=lambda *args: phases.append(args),
                           watchdog_extend=watchdog.extend,
                           watchdog_ready=watchdog.ready,
                           boot_handoff={"schema": 2, "mode": "offer",
                                         "offer_id": "12345678-1234-1234-1234-123456789abc",
                                         "initial_app": {"tag": TAG, "sha256": SHA256,
                                                         "size": len(BODY),
                                                         "format": "pw-player-data-v1",
                                                         "base_abi": "sha256:" + "c" * 64}})
    assert run(subject, 1) is False
    assert order == ["recover"]
    assert phases[-1] == ("retry_wait", None, "player_recovery_unconfirmed")
    assert watchdog.calls[-1] == ("ready",)


def test_rolled_back_data_attempt_reports_failure_with_fallback_running():
    offer_id = "12345678-1234-1234-1234-123456789abc"
    abi = "sha256:" + "c" * 64
    route = f"/v1/netboot/offers/{offer_id}/app"
    transport = FakeTransport({ORIGIN + route: lambda: reply(BODY)})
    phases, watchdog = [], Watchdog()

    class RolledBack:
        def recover(self, **_kwargs):
            return None

        def activate(self, *_args, **_kwargs):
            return "rolled_back"

    subject = bootstrapper(transport, find=finding(ORIGIN), install=never_called,
                           start_unit=never_called, data_executor=RolledBack(),
                           base_abi_reader=lambda: abi,
                           phase=lambda *args: phases.append(args),
                           watchdog_extend=watchdog.extend, watchdog_ready=watchdog.ready,
                           boot_handoff={"schema": 2, "mode": "offer", "offer_id": offer_id,
                                         "base_tag": "v1-base",
                                         "initial_app": {"tag": TAG, "sha256": SHA256,
                                                         "size": len(BODY),
                                                         "format": "pw-player-data-v1",
                                                         "base_abi": abi}})
    assert run(subject, 1) is False
    assert phases[-1] == ("retry_wait", SHA256, "app_attempt_rolled_back")
    assert not any(phase == "player_unit_started" for phase, _, _ in phases)
    assert watchdog.calls[-1] == ("ready",)


def test_cold_data_activation_failure_stays_in_base_diagnostic_without_reboot_loop():
    offer_id = "12345678-1234-1234-1234-123456789abc"
    abi = "sha256:" + "c" * 64
    route = f"/v1/netboot/offers/{offer_id}/app"
    transport = FakeTransport({ORIGIN + route: lambda: reply(BODY)})
    phases, watchdog = [], Watchdog()

    class Fails:
        calls = 0

        def recover(self, **_kwargs):
            return None

        def activate(self, *_args, **_kwargs):
            self.calls += 1
            raise provision.ExecutorError("candidate_start_failed")

    executor = Fails()
    subject = bootstrapper(transport, find=finding(ORIGIN), install=never_called,
                           start_unit=never_called, data_executor=executor,
                           base_abi_reader=lambda: abi,
                           phase=lambda *args: phases.append(args),
                           watchdog_extend=watchdog.extend, watchdog_ready=watchdog.ready,
                           boot_handoff={"schema": 2, "mode": "offer", "offer_id": offer_id,
                                         "base_tag": "v1-base",
                                         "initial_app": {"tag": TAG, "sha256": SHA256,
                                                         "size": len(BODY),
                                                         "format": "pw-player-data-v1",
                                                         "base_abi": abi}})
    assert run(subject, 3) is False
    assert executor.calls == 1
    assert phases[-1] == ("retry_wait", SHA256, "candidate_start_failed")
    assert watchdog.calls[-1] == ("ready",)


# --- dpkg / systemctl are thin, replaceable shell-outs ------------------------------


@pytest.mark.parametrize("fail", [False, True])
def test_install_package_is_dpkg_alone_on_a_0600_temp_deb_removed_after(monkeypatch, fail):
    """Not run against real dpkg here (the netboot-e2e device root runs it): exactly
    `dpkg --install <tmp>.deb`, non-interactive, nothing else -- no apt, no lists. The temp
    file holds the bytes at 0600 (even under a permissive umask) and is gone afterwards,
    including when dpkg fails."""
    dpkg = Dpkg(fail=fail)
    monkeypatch.setattr("appliance.provision.subprocess.run", dpkg)
    previous = os.umask(0o022)
    try:
        if fail:
            with pytest.raises(subprocess.CalledProcessError):
                install_package(BODY, MANIFEST)
        else:
            install_package(BODY, MANIFEST)
    finally:
        os.umask(previous)
    [(argv, kwargs)] = dpkg.calls
    assert argv[:2] == ["dpkg", "--install"] and len(argv) == 3 and argv[2].endswith(".deb")
    assert kwargs["check"] is True
    assert kwargs["timeout"] == provision.INSTALL_SECONDS
    assert kwargs["env"]["DEBIAN_FRONTEND"] == "noninteractive"
    assert dpkg.seen == [(BODY, 0o600)]
    assert not Path(argv[2]).exists()


def test_start_player_unit_is_one_systemctl_start(monkeypatch):
    calls = []
    monkeypatch.setattr("appliance.provision.subprocess.run",
                        lambda argv, **kwargs: calls.append((argv, kwargs)))
    start_player_unit()
    assert calls == [(["systemctl", "start", "photo-wall-player.service"],
                      {"check": True, "timeout": provision.START_UNIT_SECONDS})]


# systemctl show, as systemd 257 printed it for v0.9.1's Player on a base with no render group.
GROUP_FAILURE = ("ActiveState=activating\nSubState=auto-restart\nResult=exit-code\n"
                 "ExecMainCode=1\nExecMainStatus=216\n")


class Systemctl:
    """`systemctl start` fails (or times out); `systemctl show` answers `shown` (or raises)."""

    def __init__(self, shown, *, start_error=None, show_error=None):
        self.calls, self.shown = [], shown
        self.start_error = start_error or subprocess.CalledProcessError(1, ["systemctl"])
        self.show_error = show_error

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[1] == "start":
            raise self.start_error
        if self.show_error:
            raise self.show_error
        return subprocess.CompletedProcess(argv, 0, self.shown, "")


# systemctl show, as systemd 257 printed it for the Player still in ExecStartPre (waiting for
# the compositor's socket), three seconds into `systemctl start --no-block` on the fixed base.
ACTIVATING = ("Result=success\nExecMainCode=0\nExecMainStatus=0\nActiveState=activating\n"
              "SubState=start-pre\n")


def test_a_failed_start_is_named_from_the_units_own_status(monkeypatch):
    start_error = subprocess.CalledProcessError(1, ["systemctl"])
    systemctl = Systemctl(GROUP_FAILURE, start_error=start_error)
    monkeypatch.setattr("appliance.provision.subprocess.run", systemctl)
    with pytest.raises(provision.UnitStartError) as raised:
        start_player_unit()
    assert str(raised.value) == ("cause=unit reason=exit-code "
                                 "detail=photo-wall-player.service/status=216/GROUP")
    assert raised.value.__cause__ is start_error
    [_, (show, kwargs)] = systemctl.calls
    assert show == ["systemctl", "show", "--property=ActiveState", "--property=SubState",
                    "--property=Result", "--property=ExecMainCode", "--property=ExecMainStatus",
                    "photo-wall-player.service"]
    assert kwargs["timeout"] == provision.UNIT_STATUS_SECONDS and kwargs["check"] is False


def test_a_start_that_outlives_its_bound_is_named_a_timeout_with_the_units_state(monkeypatch):
    """systemctl's own wait ran out while the unit was still activating: systemd has recorded no
    failure (Result=success, status 0), so naming its Result would read `reason=success`."""
    start_error = subprocess.TimeoutExpired(["systemctl"], provision.START_UNIT_SECONDS)
    monkeypatch.setattr("appliance.provision.subprocess.run",
                        Systemctl(ACTIVATING, start_error=start_error))
    with pytest.raises(provision.UnitStartError) as raised:
        start_player_unit()
    assert str(raised.value) == ("cause=unit reason=timeout "
                                 "detail=photo-wall-player.service/state=activating/start-pre")
    assert raised.value.__cause__ is start_error


def test_a_failed_start_with_no_recorded_failure_is_unfinished_not_success(monkeypatch):
    monkeypatch.setattr("appliance.provision.subprocess.run", Systemctl(ACTIVATING))
    with pytest.raises(provision.UnitStartError) as raised:
        start_player_unit()
    assert str(raised.value) == ("cause=unit reason=unfinished "
                                 "detail=photo-wall-player.service/state=activating/start-pre")


def test_a_timeout_after_systemd_recorded_a_failure_still_names_the_state(monkeypatch):
    start_error = subprocess.TimeoutExpired(["systemctl"], provision.START_UNIT_SECONDS)
    monkeypatch.setattr("appliance.provision.subprocess.run",
                        Systemctl(GROUP_FAILURE, start_error=start_error))
    with pytest.raises(provision.UnitStartError) as raised:
        start_player_unit()
    assert str(raised.value) == ("cause=unit reason=timeout "
                                 "detail=photo-wall-player.service/state=activating/auto-restart")


@pytest.mark.parametrize("show_error", [OSError("no systemctl"),
                                        subprocess.TimeoutExpired(["systemctl"], 5)])
def test_an_unreadable_status_still_fails_the_start_naming_nothing(monkeypatch, show_error):
    monkeypatch.setattr("appliance.provision.subprocess.run",
                        Systemctl("", show_error=show_error))
    with pytest.raises(provision.UnitStartError) as raised:
        start_player_unit()
    assert str(raised.value) == ("cause=unit reason=unknown "
                                 "detail=photo-wall-player.service/status=unknown")


@pytest.mark.parametrize("code,status,ending", [
    ("1", "216", "status=216/GROUP"),
    ("1", "217", "status=217/USER"),
    ("1", "203", "status=203/EXEC"),
    ("1", "1", "status=1"),              # the program's own exit
    ("1", "234", "status=234"),          # unassigned by systemd
    ("2", "9", "signal=9"),              # killed
    ("3", "6", "signal=6"),              # dumped
    ("0", "216", "status=216"),          # not an exit: the number is not systemd's status
    ("1", "", "status=unknown"),
    ("1", "2x", "status=unknown"),
])
def test_unit_ending_names_systemds_own_statuses_only(code, status, ending):
    assert provision.unit_ending({"ExecMainCode": code, "ExecMainStatus": status}) == ending


def test_a_start_failure_line_is_one_token_per_field():
    """Result comes from systemd; anything that is not a plain token is not printed."""
    error = provision.UnitStartError("photo-wall-player.service",
                                     {"Result": "exit code\ninjected", "ExecMainCode": "1",
                                      "ExecMainStatus": "217"})
    assert str(error) == ("cause=unit reason=unknown "
                          "detail=photo-wall-player.service/status=217/USER")


def test_the_status_read_fits_in_the_backoff_the_exiting_path_never_sleeps():
    """A failed start exits instead of backing off, so the window's MAX_BACKOFF_SECONDS term
    covers the status read: LONGEST_ATTEMPT_SECONDS needs no term of its own for it."""
    assert provision.UNIT_STATUS_SECONDS <= provision.MAX_BACKOFF_SECONDS


def test_parse_unit_properties_reads_key_value_lines():
    assert provision.parse_unit_properties(GROUP_FAILURE + "\nnoise\nEmpty=\n") == {
        "ActiveState": "activating", "SubState": "auto-restart", "Result": "exit-code",
        "ExecMainCode": "1", "ExecMainStatus": "216", "Empty": ""}


# --- main -------------------------------------------------------------------------


def main_args(tmp_path, cmdline, *extra):
    boot_id = tmp_path / "boot-id"
    boot_id.write_text("11111111-2222-3333-4444-555555555555")
    return ["--cmdline", str(cmdline), "--config", str(tmp_path / "public.json"),
            "--boot-id", str(boot_id), *extra]


def test_help_runs_isolated_from_the_repo():
    """`python -I` (as the unit runs it): no zeroconf or network import on the way to --help."""
    result = subprocess.run([sys.executable, "-I", "-m", "appliance.provision", "--help"],
                            cwd=REPO, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "--cmdline" in result.stdout


class _Stubs:
    """main()'s effects: a scripted transport, a fixed clock record, no system trust store,
    and a Bootstrapper.run that finds Central once and then fails with `error`."""

    def __init__(self, monkeypatch, transport, error):
        self.found, self.discoveries, self.bootstrappers = [], [], []
        clock = record(ClockState.UNSYNCED)
        stubs = self

        class Trust:
            @staticmethod
            def public():
                return "trust"

        class Discovery(FixedDiscovery):
            def __init__(self, **options):
                super().__init__(None)
                self.options = options
                stubs.discoveries.append(self)

        async def run(bootstrapper, **_):
            stubs.bootstrappers.append(bootstrapper)
            stubs.found.append(await bootstrapper._find())
            raise error

        monkeypatch.setattr(provision, "Trust", Trust)
        monkeypatch.setattr(provision, "HttpTransport", lambda *, trust: transport)
        monkeypatch.setattr(provision, "RunClockRecord", lambda: type(
            "Record", (), {"read": staticmethod(lambda: clock)})())
        monkeypatch.setattr(provision.Bootstrapper, "run", run)
        monkeypatch.setattr("player.mdns_discovery.MdnsCentralDiscovery", Discovery)
        self.clock = clock


def test_main_with_a_cmdline_root_builds_no_discovery_and_exits_1_on_time(
        tmp_path, monkeypatch, caplog):
    cmdline = tmp_path / "cmdline"
    cmdline.write_text(f"console=tty1 photowall.central={CMDLINE}/\n")
    expired = UplinkError(Cause.TIME, "expired", host="central.example", detail="verify_code=10")
    stubs = _Stubs(monkeypatch, gateway(), expired)
    caplog.set_level(logging.ERROR, logger=provision.LOG.name)
    with pytest.raises(SystemExit) as raised:
        provision.main(main_args(tmp_path, cmdline))
    assert raised.value.code == 1
    assert [(found.root, found.source, found.central.origin) for found in stubs.found] == [
        (CMDLINE, "cmdline", CENTRAL)]
    assert stubs.discoveries == []
    assert caplog.messages == [f"provision: {failure_text(expired, clock=stubs.clock)}"]


def test_main_without_a_cmdline_root_discovers_with_the_proof(tmp_path, monkeypatch, caplog):
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("console=tty1 root=/dev/ram0\n")
    stubs = _Stubs(monkeypatch, FakeTransport({}), AssertionError("unreached"))
    caplog.set_level(logging.ERROR, logger=provision.LOG.name)
    with pytest.raises(SystemExit):
        provision.main(main_args(tmp_path, cmdline))
    [discovery] = stubs.discoveries
    assert discovery.proofs == [Unconfigured("absent")]
    assert discovery.options == {"timeout": provision.DISCOVERY_SECONDS}
    assert caplog.messages == [
        "provision: cause=configuration reason=absent detail=not_discovered"]


def test_main_logs_a_failed_player_start_as_one_named_line_and_exits_1(
        tmp_path, monkeypatch, caplog):
    """R9: v0.9.1's Player failed at spawn (216/GROUP) and provisioning died on an uncaught
    CalledProcessError. The start failure is one `cause=unit` line, and the process still
    exits 1 into the unit's start limit."""
    cmdline = tmp_path / "cmdline"
    cmdline.write_text(f"console=tty1 photowall.central={CMDLINE}/\n")
    failure = provision.UnitStartError("photo-wall-player.service",
                                       provision.parse_unit_properties(GROUP_FAILURE))
    _Stubs(monkeypatch, gateway(), failure)
    caplog.set_level(logging.ERROR, logger=provision.LOG.name)
    with pytest.raises(SystemExit) as raised:
        provision.main(main_args(tmp_path, cmdline))
    assert raised.value.code == 1
    assert caplog.messages == ["provision: cause=unit reason=exit-code "
                               "detail=photo-wall-player.service/status=216/GROUP"]


def test_main_builds_a_bootstrapper_on_the_real_systemd_notify_calls(tmp_path, monkeypatch):
    """M5: the deadline owner only works if production actually talks to systemd. The
    Bootstrapper main() builds renews through uplink.watchdog.extend_start and ends the
    starting phase through uplink.watchdog.ready -- the functions themselves, not a stand-in.
    Mutation probe: default either parameter to a no-op lambda and this fails."""
    cmdline = tmp_path / "cmdline"
    cmdline.write_text(f"console=tty1 photowall.central={CMDLINE}/\n")
    stubs = _Stubs(monkeypatch, gateway(), UplinkError(Cause.TIME, "expired", host="central"))
    with pytest.raises(SystemExit):
        provision.main(main_args(tmp_path, cmdline))
    [built] = stubs.bootstrappers
    assert built._watchdog_extend is uplink.watchdog.extend_start
    assert built._watchdog_ready is uplink.watchdog.ready


def test_old_stage_one_new_base_without_handoff_uses_uncorrelated_legacy_path(
        tmp_path, monkeypatch):
    cmdline = tmp_path / "cmdline"
    cmdline.write_text(f"photowall.central={CMDLINE}/\n")
    stubs = _Stubs(monkeypatch, gateway(), UplinkError(Cause.TIME, "expired"))
    with pytest.raises(SystemExit):
        provision.main(main_args(tmp_path, cmdline, "--boot-handoff",
                                 str(tmp_path / "missing.json")))
    assert stubs.bootstrappers[0]._boot_handoff == {"mode": "legacy_uncorrelated"}


def test_present_malformed_handoff_keeps_os_agent_alive_with_named_diagnostic(
        tmp_path, monkeypatch, caplog):
    cmdline, handoff = tmp_path / "cmdline", tmp_path / "handoff.json"
    cmdline.write_text(f"photowall.central={CMDLINE}/\n")
    handoff.write_text("broken")
    stubs = _Stubs(monkeypatch, gateway(), UplinkError(Cause.TIME, "expired"))
    phases, ready = [], []
    monkeypatch.setattr("appliance.os_agent.write_phase",
                        lambda *args, **kwargs: phases.append((args, kwargs)))
    monkeypatch.setattr(provision, "watchdog_ready", lambda: ready.append(True))
    provision.main(main_args(tmp_path, cmdline, "--boot-handoff", str(handoff)))
    assert stubs.bootstrappers == []
    assert phases[0][0] == ("retry_wait", None, "boot_handoff_invalid")
    assert ready == [True]
    assert "provision: boot_handoff_invalid" in caplog.messages


def test_prior_boot_handoff_never_fetches_old_offer_or_falls_back(tmp_path, monkeypatch):
    cmdline, boot_id = tmp_path / "cmdline", tmp_path / "boot-id"
    cmdline.write_text(f"photowall.central={CMDLINE}/\n")
    boot_id.write_text("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    handoff = write_boot_handoff(tmp_path, kernel_boot_id="11111111-2222-3333-4444-555555555555",
                                 nonce="c" * 32, base_digest="a" * 64, offer=None)
    stubs = _Stubs(monkeypatch, gateway(), UplinkError(Cause.TIME, "expired"))
    phases, ready = [], []
    monkeypatch.setattr("appliance.os_agent.write_phase",
                        lambda *args, **kwargs: phases.append((args, kwargs)))
    monkeypatch.setattr(provision, "watchdog_ready", lambda: ready.append(True))
    provision.main(["--cmdline", str(cmdline), "--config", str(tmp_path / "public.json"),
                    "--boot-handoff", str(handoff), "--boot-id", str(boot_id)])
    assert stubs.bootstrappers == []
    assert phases[0][0] == ("retry_wait", None, "boot_handoff_stale")
    assert ready == [True]
