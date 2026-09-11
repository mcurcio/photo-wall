"""Real mDNS `CentralDiscovery` provider: bounded browse, TXT scheme, tiebreak.

Registration uses zeroconf directly (the LAN-side "central" a player would
discover); tests that need real loopback multicast are guarded to skip
cleanly if the sandbox doesn't support it, per the repo's other env-gated
skips (e.g. tests/test_pxe_mapping.py). The bounded-timeout test (criterion
2 — no responder, discover() must not hang) needs no multicast to succeed
and always runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time

import pytest
from zeroconf import ServiceInfo, Zeroconf

from player.mdns_discovery import SERVICE_TYPE, MdnsCentralDiscovery


@contextlib.contextmanager
def _advertised(*services: tuple[str, int, dict[bytes, bytes] | None]):
    """Register `(name, port, properties)` services on loopback for the test's life.

    Skips the test if this sandbox can't join the loopback multicast group
    (real-network dependent, unlike the rest of the suite).
    """
    zc = Zeroconf(interfaces=["127.0.0.1"])
    infos = [
        ServiceInfo(
            SERVICE_TYPE,
            f"{name}.{SERVICE_TYPE}",
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
    with _advertised(("central-a", 8123, None)):
        async def check():
            return await MdnsCentralDiscovery(timeout=3.0).discover()

        origin = asyncio.run(check())
    assert origin == "http://127.0.0.1:8123"


def test_discover_returns_none_without_any_responder_bounded():
    async def check():
        return await MdnsCentralDiscovery(timeout=1.0).discover()

    started = time.monotonic()
    origin = asyncio.run(check())
    elapsed = time.monotonic() - started
    assert origin is None
    # Bounded: must not block past (roughly) the configured timeout.
    assert elapsed < 3.0


def test_discover_honors_https_txt_scheme():
    with _advertised(("central-secure", 8443, {b"scheme": b"https"})):
        async def check():
            return await MdnsCentralDiscovery(timeout=3.0).discover()

        origin = asyncio.run(check())
    assert origin == "https://127.0.0.1:8443"


def test_discover_multiple_centrals_deterministic_tiebreak():
    # Tiebreak: lowest fully-qualified service name wins — "central-a" sorts
    # below "central-b", independent of registration or answer order.
    with _advertised(("central-b", 8200, None), ("central-a", 8100, None)):
        async def check():
            return await MdnsCentralDiscovery(timeout=3.0).discover()

        origin = asyncio.run(check())
    assert origin == "http://127.0.0.1:8100"
