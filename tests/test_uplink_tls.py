"""The real TLS seam, DB-free: locate through real redirects to real Central (uvicorn) over
verified TLS, with certificates minted by tests/tls_fixture.py; and Trust's own refusals."""

import hashlib
import ssl

import pytest

from contracts.time import ManualClock
from tests import tls_fixture as tls
from tests.test_central_health import FakeCoordinator, FakeDatabase, app_with
from uplink.causes import Cause, UplinkError
from uplink.locate import locate
from uplink.origin import Origin
from uplink.transport import HttpTransport
from uplink.trust import Trust


@pytest.fixture
def transport(tmp_path) -> HttpTransport:
    return HttpTransport(trust=Trust.public(tls.write_bundle(tmp_path / "ca.pem", tls.CA)))


@pytest.fixture
def central(monkeypatch):
    """Real Central over TLS with a leaf for localhost and 127.0.0.1; its database is down."""
    app = app_with(FakeDatabase(healthy=False), FakeCoordinator(), ManualClock(1000), monkeypatch,
                   enabled=False)
    with tls.serve_tls(app, tls.CENTRAL) as port:
        yield port


def test_locate_follows_a_301_from_http_to_central_over_verified_tls(transport, central):
    hops = []
    with tls.redirect_stub(f"https://127.0.0.1:{central}/v1/locate") as gateway:
        root = Origin.parse_root(f"http://localhost:{gateway.port}/")
        located = locate(root, transport=transport,
                         on_hop=lambda url, status, peer: hops.append((str(url), status, peer)))
    assert located.origin == Origin("https", "127.0.0.1", central) != root
    assert hops == [(f"http://localhost:{gateway.port}/v1/locate", 301, "127.0.0.1"),
                    (f"https://127.0.0.1:{central}/v1/locate", 200, "127.0.0.1")]
    assert gateway.requests == ["/v1/locate"]


def test_sni_and_the_name_check_follow_each_hop(transport, central):
    # Hop 1 verifies DNS:localhost; hop 2 verifies IP:127.0.0.1 on another server.
    with tls.redirect_stub(f"https://127.0.0.1:{central}/v1/locate", leaf=tls.CENTRAL,
                           status=302) as gateway:
        located = locate(Origin.parse_root(f"https://localhost:{gateway.port}/"),
                         transport=transport)
    assert located.origin == Origin("https", "127.0.0.1", central)
    assert gateway.requests == ["/v1/locate"]


def test_an_https_to_http_redirect_is_refused_before_anything_is_sent(transport):
    with tls.serve_stub(tls.central_stub()) as plain:
        with tls.redirect_stub(f"http://127.0.0.1:{plain.port}/v1/locate", leaf=tls.CENTRAL,
                               status=302) as gateway:
            with pytest.raises(UplinkError) as caught:
                locate(Origin.parse_root(f"https://localhost:{gateway.port}/"),
                       transport=transport)
    assert (caught.value.cause, caught.value.reason) == (Cause.REDIRECT, "downgrade")
    assert gateway.requests == ["/v1/locate"] and plain.requests == []


@pytest.mark.parametrize(("leaf", "host", "anchors", "expected"), [
    (tls.FOREIGN, "127.0.0.1", (tls.CA,), (Cause.TLS, "untrusted", "verify_code=20")),
    (tls.OTHER_NAME, "localhost", (tls.CA,), (Cause.TLS, "hostname", "verify_code=62")),
    (tls.OTHER_IP, "127.0.0.1", (tls.CA,), (Cause.TLS, "hostname", "verify_code=64")),
    (tls.NOT_YET_VALID, "127.0.0.1", (tls.CA,), (Cause.TIME, "not_yet_valid", "verify_code=9")),
    (tls.EXPIRED, "127.0.0.1", (tls.CA,), (Cause.TIME, "expired", "verify_code=10")),
    # Its CA is trusted but lacks keyUsage: only VERIFY_X509_STRICT refuses the chain.
    (tls.UNDER_LAX_CA, "127.0.0.1", (tls.CA, tls.LAX_CA), (Cause.TLS, "untrusted", None)),
], ids=["wrong-ca", "dns-name", "ip-address", "not-yet-valid", "expired", "strict"])
def test_certificate_refusals_are_named(tmp_path, leaf, host, anchors, expected):
    transport = HttpTransport(trust=Trust.public(tls.write_bundle(tmp_path / "ca.pem", *anchors)))
    with tls.serve_stub(tls.central_stub(), leaf=leaf) as server:
        with pytest.raises(UplinkError) as caught:
            locate(Origin.parse_root(f"https://{host}:{server.port}/"), transport=transport)
    error = caught.value
    assert (error.cause, error.reason, error.host) == (*expected[:2], host)
    assert expected[2] is None or error.detail == expected[2]
    assert server.requests == []                  # nothing was asked over a refused channel


def test_trust_loads_only_its_bundle_with_every_check_explicit(tmp_path):
    bundle = tls.write_bundle(tmp_path / "ca.pem", tls.CA, tls.OTHER_CA)
    trust = Trust.public(bundle)
    assert trust.anchors == 2
    assert trust.sha256 == hashlib.sha256(bundle.read_bytes()).hexdigest()
    context = trust.context
    assert len(context.get_ca_certs()) == 2       # no OpenSSL default paths
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert not context.hostname_checks_common_name
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert context.verify_flags == (ssl.VERIFY_X509_TRUSTED_FIRST | ssl.VERIFY_X509_STRICT
                                    | ssl.VERIFY_X509_PARTIAL_CHAIN)


def test_trust_ignores_text_between_certificates(tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_bytes("# Főtanúsítvány\n".encode() + tls.CA.pem + b"# trailer\n")
    assert Trust.public(bundle).anchors == 1


@pytest.mark.parametrize("content", [
    None,                                         # missing
    b"",                                          # empty
    b"not a certificate bundle\n",
    b"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",   # unparsable
    "leaf",                                       # a certificate, but not a CA
], ids=["missing", "empty", "no-pem", "unparsable", "no-ca"])
def test_an_unusable_trust_store_is_named(tmp_path, content):
    bundle = tmp_path / "ca.pem"
    if content == "leaf":
        bundle.write_bytes(tls.CENTRAL.pem)
    elif content is not None:
        bundle.write_bytes(content)
    with pytest.raises(UplinkError) as caught:
        Trust.public(bundle)
    assert (caught.value.cause, caught.value.reason) == (Cause.TLS, "trust_store")
