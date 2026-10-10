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
import re
import ssl
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from integration.bus_servers import (
    NODE_BUS_CONF,
    BusServer,
    PrefixProxy,
    _free_port,
    _port_of,
    central,
    hub_server,
    leaf_connections,
    node_server,
    until,
)
from node_pid1_bus_probe import server_info
from systemd_environment import read_environment_file

from appliance.boot.bus_environment import bus_environment, write_bus_environment
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


@pytest.mark.parametrize(("origin", "port"), [("http://127.0.0.1", 80), ("https://127.0.0.1", 443),
                                            ("http://127.0.0.1:80", 80)])
def test_a_node_bus_dials_a_default_port_origin_at_that_port(tmp_path, origin, port):
    """An origin with the scheme's default port (the production root's https origin) is dialled at
    80 or 443: nats-server fills a ws/wss remote with no port with its leafnode port, 7422, and the
    leaf never reaches the ingress (E-E3C-S1-3). Nothing need listen: the server logs the address
    it dials, connected or refused (`127.0.0.1:<port>` either way)."""
    hub = hub_server(tmp_path, [SERIAL])
    node = node_server(tmp_path, SERIAL, hub, origin=origin)
    log = node.config.with_suffix(".log")
    dialled = re.compile(rf"\b127\.0\.0\.1:{port}\b")

    async def run():
        node.start()
        try:
            async def tried():
                return dialled.search(log.read_text(errors="replace"))
            await until(tried, 10, f"the Node's leaf dialling 127.0.0.1:{port}")
            assert ":7422" not in log.read_text(errors="replace")
        finally:
            node.stop()

    asyncio.run(run())


@pytest.mark.parametrize(("origin", "dialled"), [
    ("http://central.invalid", "central.invalid:80"),
    ("https://central.invalid:8443", "central.invalid:8443"),
    ("http://127.0.0.1:8080", "127.0.0.1:8080"),
    ("https://127.0.0.1", "127.0.0.1:443"),
    ("http://[fd00::5]:8080", "[fd00::5]:8080"),
    ("https://[fd00::5]", "[fd00::5]:443"),
])
def test_the_shipped_conf_starts_on_every_origin_shape_through_the_environment_file(
        tmp_path, origin, dialled):
    """Every origin shape uplink accepts (hostname, IPv4, IPv6; default and explicit port) goes
    through write_bus_environment, is read as systemd reads the unit's EnvironmentFile, and the
    shipped conf starts on it and dials the origin: nats-server parses a `$VAR`'s value as config,
    so an unquoted IPv6 URL ended the urls array and the bus exited 1 on every start (E-E3C-S1-5).
    Only the client port is replaced by a free one."""
    environment = read_environment_file(write_bus_environment(tmp_path, origin, SERIAL))
    assert environment == bus_environment(origin, SERIAL)
    port = _free_port()
    config = tmp_path / "node-bus.conf"
    config.write_bytes(NODE_BUS_CONF.read_bytes())
    node = BusServer(name=f"node on {origin}", config=config, client_url=f"nats://127.0.0.1:{port}",
                     environment={**environment, "PHOTO_WALL_BUS_PORT": str(port)})
    log = config.with_suffix(".log")

    async def run():
        node.start()
        try:
            async def tried():
                return dialled in log.read_text(errors="replace")
            await until(tried, 10, f"the Node's leaf dialling {dialled}")
            assert server_info(port)["server_name"] == node_user(SERIAL)
        finally:
            node.stop()

    asyncio.run(run())


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
