"""Locate, then the direct base fetch, against real Central (uvicorn over TLS, real Postgres):
through a gateway's 301 to Central's verified TLS origin, then `/v1/netboot/base` streamed and
digest-checked. Skips without PHOTO_WALL_TEST_DATABASE_URL, like every DB-backed test."""

import hashlib

from appliance.bootstrap import CHUNK
from appliance.netboot_init import NETBOOT_BASE_PATH, SERIAL_HEADER, parse_digest_header
from contracts.release import MAX_ROOTFS_BYTES
from tests import tls_fixture as tls
from tests.test_netboot_e2e_wire import SERIAL, SQUASHFS, _app, _seed_base
from uplink.fetch import DirectFetch
from uplink.locate import locate
from uplink.origin import Origin
from uplink.transport import HttpTransport
from uplink.trust import Trust


def test_locate_then_the_direct_base_fetch_over_verified_tls(registry, tmp_path):
    cache_root = tmp_path / "cache"
    _seed_base(registry, cache_root)
    transport = HttpTransport(trust=Trust.public(tls.write_bundle(tmp_path / "ca.pem", tls.CA)))
    with tls.serve_tls(_app(registry, cache_root), tls.CENTRAL) as port:
        with tls.redirect_stub(f"https://127.0.0.1:{port}/v1/locate") as gateway:
            located = locate(Origin.parse_root(f"http://localhost:{gateway.port}/"),
                             transport=transport)
        assert located.origin == Origin("https", "127.0.0.1", port)
        seen = {}
        fetcher = DirectFetch(located, transport=transport, seconds=60)
        body = b"".join(fetcher.chunks(
            NETBOOT_BASE_PATH, MAX_ROOTFS_BYTES, block=CHUNK, headers={SERIAL_HEADER: SERIAL},
            on_response=lambda headers: seen.update(digest=headers.get("Digest"))))
    assert body == SQUASHFS
    assert hashlib.sha256(body).hexdigest() == parse_digest_header(seen["digest"])
    assert gateway.requests == ["/v1/locate"]
