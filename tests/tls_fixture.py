"""Test TLS material and servers for every uplink test that crosses a real socket.

Certificates are minted per process with `cryptography` to the strict X.509 profile that
`uplink.trust.Trust` enforces (critical basicConstraints, keyUsage, SKI/AKI); no key or
certificate is kept in the tree. `serve_tls` runs an ASGI app (real Central) under uvicorn over
TLS. `serve_stub` runs a stand-in `http.server` handler, TLS-wrapped when given a leaf, and
records every request that reaches it. `central_stub` is the ONE stand-in base for Central: it
answers LOCATE_PATH with `identity_body()`, so no stand-in can drift from the real identity. The
stand-ins themselves are stdlib only and live in scripts/uplink_device_harness.py, which runs
them on the device's Python too; this module adds the minted leaves.
"""

import contextlib
import datetime
import http.server
import ipaddress
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from scripts import uplink_device_harness as harness
from scripts.uplink_device_harness import Stub, central_stub, server_context

__all__ = ["Stub", "central_stub"]      # re-exported: one stdlib stub implementation

DAY = datetime.timedelta(days=1)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


@dataclass(frozen=True)
class Authority:
    certificate: x509.Certificate
    key: ec.EllipticCurvePrivateKey

    @property
    def pem(self) -> bytes:
        return self.certificate.public_bytes(serialization.Encoding.PEM)


@dataclass(frozen=True)
class Leaf:
    certificate: x509.Certificate
    key: ec.EllipticCurvePrivateKey

    @property
    def pem(self) -> bytes:
        return self.certificate.public_bytes(serialization.Encoding.PEM)

    @contextlib.contextmanager
    def files(self) -> Iterator[tuple[str, str]]:
        """(certfile, keyfile) paths, removed on exit: what ssl and uvicorn load from."""
        with tempfile.TemporaryDirectory(prefix="uplink-tls-") as directory:
            certfile, keyfile = Path(directory, "leaf.pem"), Path(directory, "leaf.key")
            certfile.write_bytes(self.pem)
            keyfile.write_bytes(self.key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()))
            yield str(certfile), str(keyfile)


def mint_ca(name: str, *, key_usage: bool = True) -> Authority:
    """A self-signed root. `key_usage=False` leaves out keyUsage, which VERIFY_X509_STRICT
    refuses in a CA (the strictness probe)."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    builder = (x509.CertificateBuilder()
               .subject_name(subject).issuer_name(subject).public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(_now() - DAY).not_valid_after(_now() + 365 * DAY)
               .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                              critical=False))
    if key_usage:
        builder = builder.add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
            encipher_only=False, decipher_only=False), critical=True)
    return Authority(builder.sign(key, hashes.SHA256()), key)


def mint_leaf(ca: Authority, *, dns: tuple[str, ...] = (), ips: tuple[str, ...] = (),
              not_before: datetime.datetime | None = None,
              not_after: datetime.datetime | None = None) -> Leaf:
    """A server certificate under `ca` for the given names, valid from a day ago for 30 days
    unless the dates are given."""
    key = ec.generate_private_key(ec.SECP256R1())
    names = [x509.DNSName(name) for name in dns]
    names += [x509.IPAddress(ipaddress.ip_address(address)) for address in ips]
    not_before = not_before or _now() - DAY
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, (dns or ips)[0])]))
        .issuer_name(ca.certificate.subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before).not_valid_after(not_after or not_before + 30 * DAY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
            encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                       critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
            ca.key.public_key()), critical=False)
        .sign(ca.key, hashes.SHA256()))
    return Leaf(certificate, key)


def write_bundle(path: Path, *authorities: Authority) -> Path:
    path.write_bytes(b"".join(authority.pem for authority in authorities))
    return path


# The material every TLS test shares, minted once per process.
CA = mint_ca("uplink test CA")
OTHER_CA = mint_ca("uplink other CA")
LAX_CA = mint_ca("uplink CA without keyUsage", key_usage=False)
CENTRAL = mint_leaf(CA, dns=("localhost",), ips=("127.0.0.1",))
OTHER_NAME = mint_leaf(CA, dns=("other.test",))
OTHER_IP = mint_leaf(CA, ips=("127.0.0.2",))
NOT_YET_VALID = mint_leaf(CA, dns=("localhost",), ips=("127.0.0.1",),
                          not_before=_now() + DAY)
EXPIRED = mint_leaf(CA, dns=("localhost",), ips=("127.0.0.1",),
                    not_before=_now() - 60 * DAY, not_after=_now() - DAY)
FOREIGN = mint_leaf(OTHER_CA, dns=("localhost",), ips=("127.0.0.1",))
UNDER_LAX_CA = mint_leaf(LAX_CA, dns=("localhost",), ips=("127.0.0.1",))


@contextlib.contextmanager
def serve_tls(app, leaf: Leaf) -> Iterator[int]:
    """Run `app` under a real uvicorn server over TLS with `leaf` on an ephemeral 127.0.0.1
    port; yield the port. The TLS form of the wire tests' `_serve`."""
    with leaf.files() as (certfile, keyfile):
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning",
                                lifespan="on", ssl_certfile=certfile, ssl_keyfile=keyfile)
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 30
            while not server.started or not server.servers:
                if time.monotonic() > deadline:
                    raise RuntimeError("uvicorn did not start in time")
                time.sleep(0.02)
            yield server.servers[0].sockets[0].getsockname()[1]
        finally:
            server.should_exit = True
            thread.join(timeout=30)


def serve_stub(handler: type[http.server.BaseHTTPRequestHandler], *,
               leaf: Leaf | None = None) -> contextlib.AbstractContextManager[Stub]:
    """Serve `handler` on an ephemeral 127.0.0.1 port, over TLS with `leaf` when given; yield a
    Stub whose `requests` records every request target that reached the handler."""
    return _with_leaf(harness.serve_stub, handler, leaf=leaf)


def redirect_stub(location: str, *, leaf: Leaf | None = None,
                  status: int = 301) -> contextlib.AbstractContextManager[Stub]:
    """A stand-in gateway that answers every GET with `status` and `Location: location`."""
    return _with_leaf(harness.redirect_stub, location, leaf=leaf, status=status)


@contextlib.contextmanager
def _with_leaf(serve, *args, leaf: Leaf | None, **kwargs) -> Iterator[Stub]:
    """`serve(*args, context=..., **kwargs)`, TLS-wrapped with `leaf`'s files when given."""
    with contextlib.ExitStack() as stack:
        context = None if leaf is None else server_context(*stack.enter_context(leaf.files()))
        yield stack.enter_context(serve(*args, context=context, **kwargs))
