"""A Node bus started with the environment its handoff stage writes links its leaf to the hub
through the boot origin's path prefix, ws:// for an http origin and wss:// for https (E3c S1, S3;
errata E-E3C-CUT-5, -8).

The hub runs Fleet's generated configuration; a path-prefix proxy at `LEAF_PATH` stands in for the
origin's ingress route, TLS-terminating for an https origin; the Node runs the shipped
`node-bus.conf` with `bus_environment(<origin>, SERIAL)` (`node_server`), only its client port
replaced by a free one (on a Node it is NODE_BUS_PORT, which tests running side by side cannot
share). The wss leaf verifies the ingress against the system's roots, as the initramfs verifies
Central: here a test CA named by SSL_CERT_FILE.
"""
from __future__ import annotations

import asyncio
import datetime
import ipaddress
import ssl
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from integration.bus_servers import (
    BusServer,
    PrefixProxy,
    _port_of,
    central,
    hub_server,
    leaf_connections,
    node_server,
    until,
)
from node_pid1_bus_probe import server_info

from contracts.node_link import LEAF_PATH, NODE_DOMAIN, account_id, node_user

SERIAL = "serial-origin"


def test_a_node_bus_links_from_its_boot_origin_and_serial(tmp_path):
    hub = hub_server(tmp_path, [SERIAL])
    proxy = PrefixProxy(hub.websocket_port, LEAF_PATH)
    node = node_server(tmp_path, SERIAL, hub, leaf_port=proxy.port)
    _links_through(hub, proxy, node)


@pytest.mark.skipif(sys.platform == "darwin",
                    reason="Go on macOS verifies with the system keychain and ignores SSL_CERT_FILE")
def test_a_node_bus_links_to_an_https_origin_over_wss(tmp_path):
    hub = hub_server(tmp_path, [SERIAL])
    ca_pem, ingress = _test_ingress(tmp_path / "tls")
    proxy = PrefixProxy(hub.websocket_port, LEAF_PATH, tls=ingress)
    node = node_server(tmp_path, SERIAL, hub, scheme="https", host="localhost", leaf_port=proxy.port,
                       extra_environment={"SSL_CERT_FILE": str(ca_pem)})
    _links_through(hub, proxy, node)


def _links_through(hub: BusServer, proxy: PrefixProxy, node: BusServer) -> None:
    """The Node's leaf links through `proxy` at LEAF_PATH, once; the Node's server is named for it;
    Central's client in the Node's account reaches the Node's JetStream across the leaf."""
    hub.start()

    async def run():
        await proxy.start()
        node.start()
        try:
            async def linked():
                return account_id(SERIAL) in leaf_connections(hub)
            await until(linked, 10, "the Node's leaf at the hub")
            assert proxy.paths == [f"/{LEAF_PATH}/leafnode"]
            assert server_info(_port_of(node.client_url))["server_name"] == node_user(SERIAL)
            central_client = await central(hub, SERIAL)
            try:
                info = await central_client.jetstream(domain=NODE_DOMAIN).account_info()
                assert info.domain == NODE_DOMAIN
            finally:
                await central_client.close()
        finally:
            node.stop()
            await proxy.close()

    try:
        asyncio.run(run())
    finally:
        node.stop()
        hub.stop()


def _test_ingress(directory: Path) -> tuple[Path, ssl.SSLContext]:
    """A test CA (its certificate written to ca.pem) and the ingress's server context: a
    certificate for `localhost` the CA signed."""
    directory.mkdir(parents=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    validity = (now - datetime.timedelta(minutes=5), now + datetime.timedelta(days=1))

    def certificate(subject: str, key, issuer: x509.Name, issuer_key, *, ca: bool) -> x509.Certificate:
        builder = (x509.CertificateBuilder()
                   .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
                   .issuer_name(issuer).public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(validity[0]).not_valid_after(validity[1])
                   .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
        if ca:
            builder = builder.add_extension(
                x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                              content_commitment=False, key_encipherment=False, data_encipherment=False,
                              key_agreement=False, encipher_only=False, decipher_only=False),
                critical=True)
        else:
            builder = builder.add_extension(x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False)
        return builder.sign(issuer_key, hashes.SHA256())

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "photo-wall test CA")])
    ca = certificate("photo-wall test CA", ca_key, ca_name, ca_key, ca=True)
    server_key = ec.generate_private_key(ec.SECP256R1())
    server = certificate("localhost", server_key, ca_name, ca_key, ca=False)

    ca_pem = directory / "ca.pem"
    ca_pem.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    chain = directory / "server.pem"
    chain.write_bytes(server.public_bytes(serialization.Encoding.PEM))
    key = directory / "server.key"
    key.write_bytes(server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(chain, key)
    return ca_pem, context
