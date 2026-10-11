"""R1 shared by provisioning and the Player (design §2.1): the cmdline root wins, then the saved
root; then one locate."""

import asyncio
import threading

import pytest
from uplink_fakes import FakeReply, FakeTransport, central

from contracts.central_identity import LOCATE_PATH
from uplink.causes import Cause, UplinkError
from uplink.finder import Found, choose_root, find_central
from uplink.locate import LocatedCentral
from uplink.origin import Origin
from uplink.resolver import Configured, Unconfigured

CMDLINE = Origin.parse_root("http://photo-wall.localdomain/")
SAVED = Origin.parse_root("http://192.0.2.20:8000")


def test_a_configured_cmdline_wins_and_never_consults_saved():
    assert choose_root(Configured(CMDLINE), saved=SAVED) == (CMDLINE, "cmdline")


def test_unconfigured_uses_the_saved_root():
    assert choose_root(Unconfigured("absent"), saved=SAVED) == (SAVED, "saved")


@pytest.mark.parametrize("proof", [Unconfigured("absent"), Unconfigured("no_cmdline")])
def test_unconfigured_and_nothing_saved_is_none(proof):
    assert choose_root(proof, saved=None) is None


def test_find_central_with_no_root_is_configuration_absent_not_discovered():
    transport = FakeTransport({})
    with pytest.raises(UplinkError) as raised:
        asyncio.run(find_central(Unconfigured("absent"), transport=transport))
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
                                     on_hop=lambda url, status, peer: hops.append(status)))
    assert isinstance(found, Found) and isinstance(found.central, LocatedCentral)
    assert (found.root, found.source) == (CMDLINE, "cmdline")
    assert found.central.origin == Origin.parse_root("https://photo-wall.example")
    assert transport.urls == [str(CMDLINE.url(LOCATE_PATH)), target]
    assert hops == [301, 200]
    assert threads and all(thread is not threading.main_thread() for thread in threads)


def test_find_central_reports_a_saved_root_and_raises_locate_failures_unchanged():
    root = str(SAVED.url(LOCATE_PATH))
    found = asyncio.run(find_central(Unconfigured("absent"),
                                     transport=FakeTransport({root: central()}), saved=SAVED))
    assert (found.root, found.source) == (SAVED, "saved")
    refused = UplinkError(Cause.CONNECT, "refused", host="192.0.2.20")
    with pytest.raises(UplinkError) as raised:
        asyncio.run(find_central(Unconfigured("absent"), transport=FakeTransport(
            {root: refused}), saved=SAVED))
    assert raised.value is refused
