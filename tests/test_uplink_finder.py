"""R1 shared by provisioning and the Player (design §2.1): the cmdline root wins, then the saved
root, then discovery, which only an Unconfigured proof starts; then one locate."""

import asyncio
import threading

import pytest

from contracts.central_identity import LOCATE_PATH
from tests.uplink_fakes import FakeReply, FakeTransport, central
from uplink.causes import Cause, UplinkError
from uplink.finder import DiscoveryError, Found, choose_root, find_central
from uplink.locate import LocatedCentral
from uplink.origin import Origin
from uplink.resolver import Configured, Unconfigured

CMDLINE = Origin.parse_root("http://photo-wall.localdomain/")
SAVED = Origin.parse_root("http://192.0.2.20:8000")
DISCOVERED = Origin.parse_root("http://192.0.2.10:8000")


class ForbiddenDiscovery:
    """Fails the test if it is ever consulted."""

    async def discover(self, unconfigured):
        raise AssertionError("discovery must not run")


class Discovery:
    def __init__(self, root):
        self.root = root
        self.proofs = []

    async def discover(self, unconfigured):
        self.proofs.append(unconfigured)
        return self.root


def choose(resolution, *, saved=None, discovery=None):
    return asyncio.run(choose_root(resolution, saved=saved, discovery=discovery))


def test_a_configured_cmdline_wins_and_never_consults_saved_or_discovery():
    assert choose(Configured(CMDLINE), saved=SAVED, discovery=ForbiddenDiscovery()) == (
        CMDLINE, "cmdline")


def test_unconfigured_uses_the_saved_root_without_discovery():
    assert choose(Unconfigured("absent"), saved=SAVED, discovery=ForbiddenDiscovery()) == (
        SAVED, "saved")


@pytest.mark.parametrize("proof", [Unconfigured("absent"), Unconfigured("no_cmdline")])
def test_unconfigured_and_nothing_saved_discovers_with_the_proof(proof):
    discovery = Discovery(DISCOVERED)
    assert choose(proof, discovery=discovery) == (DISCOVERED, "discovered")
    assert discovery.proofs == [proof]


@pytest.mark.parametrize("discovery", [None, Discovery(None)])
def test_no_root_at_all_is_none(discovery):
    assert choose(Unconfigured("absent"), discovery=discovery) is None


def test_find_central_with_no_root_is_configuration_absent_not_discovered():
    transport = FakeTransport({})
    with pytest.raises(UplinkError) as raised:
        asyncio.run(find_central(Unconfigured("absent"), transport=transport,
                                 discovery=Discovery(None)))
    assert (raised.value.cause, raised.value.reason, raised.value.detail) == (
        Cause.CONFIGURATION, "absent", "not_discovered")
    assert transport.sent == []


def test_find_central_locates_on_a_worker_thread_and_returns_a_real_located_central():
    threads = []

    class Recording(FakeTransport):
        def send(self, url, **kwargs):
            threads.append(threading.current_thread())
            return super().send(url, **kwargs)

    target = "https://photo-wall.example/v1/locate"
    transport = Recording({str(CMDLINE.url(LOCATE_PATH)): FakeReply(301, location=target),
                           target: central()})
    hops = []
    found = asyncio.run(find_central(Configured(CMDLINE), transport=transport, saved=SAVED,
                                     discovery=ForbiddenDiscovery(),
                                     on_hop=lambda url, status, peer: hops.append(status)))
    assert isinstance(found, Found) and isinstance(found.central, LocatedCentral)
    assert (found.root, found.source) == (CMDLINE, "cmdline")
    assert found.central.origin == Origin.parse_root("https://photo-wall.example")
    assert transport.urls == [str(CMDLINE.url(LOCATE_PATH)), target]
    assert hops == [301, 200]
    assert threads and all(thread is not threading.main_thread() for thread in threads)


def test_find_central_reports_a_discovered_root_and_raises_locate_failures_unchanged():
    root = str(DISCOVERED.url(LOCATE_PATH))
    found = asyncio.run(find_central(Unconfigured("absent"),
                                     transport=FakeTransport({root: central()}),
                                     discovery=Discovery(DISCOVERED)))
    assert (found.root, found.source, found.central.origin) == (
        DISCOVERED, "discovered", DISCOVERED)
    refused = UplinkError(Cause.CONNECT, "refused", host="192.0.2.20")
    with pytest.raises(UplinkError) as raised:
        asyncio.run(find_central(Unconfigured("absent"), transport=FakeTransport(
            {str(SAVED.url(LOCATE_PATH)): refused}), saved=SAVED))
    assert raised.value is refused


class BrokenDiscovery:
    def __init__(self, error):
        self.error = error

    async def discover(self, unconfigured):
        raise self.error


@pytest.mark.parametrize("error,detail", [
    (OSError(19, "no multicast"), "discovery_OSError"),
    (TimeoutError(), "discovery_TimeoutError"),
    (DiscoveryError("NotRunningException"), "discovery_NotRunningException"),
], ids=["OSError", "TimeoutError", "DiscoveryError"])
def test_an_expected_discovery_failure_is_named_so_find_central_raises_an_uplink_error(
        error, detail):
    transport = FakeTransport({})
    with pytest.raises(UplinkError) as excinfo:
        asyncio.run(find_central(Unconfigured("absent"), transport=transport,
                                 discovery=BrokenDiscovery(error)))
    assert (excinfo.value.cause, excinfo.value.reason, excinfo.value.detail) == (
        Cause.CONFIGURATION, "absent", detail)
    assert excinfo.value.__cause__ is error
    assert transport.sent == []


@pytest.mark.parametrize("kind", ["", "no multicast", "détail", "a;b"])
def test_a_discovery_error_kind_is_an_identifier(kind):
    """The kind becomes the UplinkError's console detail token, so it is refused when the
    DiscoveryError is built, not when the failure is being reported."""
    with pytest.raises(ValueError):
        DiscoveryError(kind)


@pytest.mark.parametrize("error", [TypeError("a bug"), AttributeError("a bug"),
                                   KeyError("a bug"), ValueError("a bug")],
                         ids=["TypeError", "AttributeError", "KeyError", "ValueError"])
def test_a_discovery_bug_is_never_dressed_up_as_configuration(error):
    """R9: a programming error in a discovery propagates as itself (the Player reports it as
    "player_error"), never as CONFIGURATION/absent."""
    transport = FakeTransport({})
    with pytest.raises(type(error)) as excinfo:
        asyncio.run(find_central(Unconfigured("absent"), transport=transport,
                                 discovery=BrokenDiscovery(error)))
    assert excinfo.value is error
    assert transport.sent == []


def test_a_discovery_uplink_error_passes_unchanged():
    error = UplinkError(Cause.DNS, "failed")
    with pytest.raises(UplinkError) as excinfo:
        asyncio.run(find_central(Unconfigured("absent"), transport=FakeTransport({}),
                                 discovery=BrokenDiscovery(error)))
    assert excinfo.value is error
