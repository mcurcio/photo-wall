"""Real mDNS `CentralDiscovery` provider: bounded browse, TXT scheme, tiebreak.

Registration uses zeroconf directly (the LAN-side "central" a player would
discover); tests that need real loopback multicast are guarded to skip
cleanly if the sandbox doesn't support it, per the repo's other env-gated
skips (e.g. tests/test_pxe_mapping.py). The bounded-timeout test (criterion
2 — no responder, discover() must not hang) needs no multicast to succeed
and always runs.

Every test that registers a real responder advertises (and browses) a
unique per-test service type (`_unique_service_type()`), so this suite
never collides with a real `_photowall._tcp` responder that happens to be
on the network (e.g. a compose central in CI's Docker bridge network).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from secrets import token_hex

import pytest
from zeroconf import ServiceInfo, Zeroconf

from player.mdns_discovery import SERVICE_TYPE, MdnsCentralDiscovery


def _unique_service_type() -> str:
    """A per-test service type so this test can't collide with a real
    `_photowall._tcp` responder on the network (e.g. CI's compose central,
    or any other LAN device advertising the production type)."""
    return f"_pwtest{token_hex(4)}._tcp.local."


@contextlib.contextmanager
def _advertised(*services: tuple[str, int, dict[bytes, bytes] | None], service_type: str = SERVICE_TYPE):
    """Register `(name, port, properties)` services on loopback for the test's life.

    Skips the test if this sandbox can't join the loopback multicast group
    (real-network dependent, unlike the rest of the suite).
    """
    zc = Zeroconf(interfaces=["127.0.0.1"])
    infos = [
        ServiceInfo(
            service_type,
            f"{name}.{service_type}",
            addresses=[socket.inet_aton("127.0.0.1")],
            port=port,
            properties=properties or {},
        )
        for name, port, properties in services
    ]
    try:
        for info in infos:
            zc.register_service(info)
    except OSError as error:
        zc.close()
        pytest.skip(f"loopback multicast unavailable in this sandbox: {error}")
    try:
        yield
    finally:
        for info in infos:
            zc.unregister_service(info)
        zc.close()


def test_discover_finds_advertised_central():
    service_type = _unique_service_type()
    with _advertised(("central-a", 8123, None), service_type=service_type):
        async def check():
            return await MdnsCentralDiscovery(timeout=3.0, service_type=service_type).discover()

        origin = asyncio.run(check())
    assert origin == "http://127.0.0.1:8123"


def test_discover_returns_none_without_any_responder_bounded():
    # Browses a unique type nothing advertises -- a real `_photowall._tcp`
    # responder elsewhere on the network (e.g. CI's compose central) must
    # stay invisible to this discovery. Registers nothing, so this needs no
    # multicast support to succeed and always runs.
    async def check():
        return await MdnsCentralDiscovery(timeout=1.0, service_type=_unique_service_type()).discover()

    started = time.monotonic()
    origin = asyncio.run(check())
    elapsed = time.monotonic() - started
    assert origin is None
    # Bounded: must not block past (roughly) the configured timeout.
    assert elapsed < 3.0


def test_discover_ignores_real_photowall_responder_on_unique_type():
    """Regression for the CI collision: a real `_photowall._tcp` responder
    (simulated here as a decoy on the production SERVICE_TYPE) must not be
    seen by a discovery browsing a unique type nothing else advertises."""
    with _advertised(("decoy-central", 8199, None)):  # production SERVICE_TYPE
        async def check():
            return await MdnsCentralDiscovery(
                timeout=1.0, service_type=_unique_service_type()
            ).discover()

        origin = asyncio.run(check())
    assert origin is None


def test_discover_honors_https_txt_scheme():
    service_type = _unique_service_type()
    with _advertised(("central-secure", 8443, {b"scheme": b"https"}), service_type=service_type):
        async def check():
            return await MdnsCentralDiscovery(timeout=3.0, service_type=service_type).discover()

        origin = asyncio.run(check())
    assert origin == "https://127.0.0.1:8443"


def test_discover_multiple_centrals_deterministic_tiebreak():
    # Tiebreak: lowest fully-qualified service name wins — "central-a" sorts
    # below "central-b", independent of registration or answer order.
    service_type = _unique_service_type()
    with _advertised(
        ("central-b", 8200, None), ("central-a", 8100, None), service_type=service_type
    ):
        async def check():
            return await MdnsCentralDiscovery(timeout=3.0, service_type=service_type).discover()

        origin = asyncio.run(check())
    assert origin == "http://127.0.0.1:8100"
