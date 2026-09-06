"""Common PXE filename policy and bounded real tftpd-hpa integration gate."""

import fcntl
import ipaddress
import os
import platform
import shutil
import socket
import struct
import subprocess
import time
from pathlib import Path

import pytest

MAP = Path(__file__).parents[1] / "appliance/provisioning/tftpd.map"


def _daemon_or_skip():
    if platform.system() != "Linux":
        pytest.skip("actual in.tftpd integration requires Linux")
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        pytest.skip("actual in.tftpd integration requires root for the chroot gate")
    daemon = shutil.which("in.tftpd")
    if daemon is None:
        pytest.skip("in.tftpd is not installed")
    version = subprocess.run([daemon, "--version"], capture_output=True, text=True,
                             timeout=3, check=False)
    if "remap" not in (version.stdout + version.stderr).lower():
        pytest.skip("in.tftpd was built without filename remapping")
    return daemon


def _dnsmasq_or_skip():
    dnsmasq = shutil.which("dnsmasq")
    if dnsmasq is None:
        pytest.skip("dnsmasq is not installed")
    return dnsmasq


def _loopback_namespace_or_skip():
    """The daemon's accepted :port form is safe only in a loopback namespace."""
    addresses = []
    request = 0x8915  # Linux SIOCGIFADDR.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        for _, name in socket.if_nameindex():
            interface = struct.pack("256s", name.encode()[:15])
            try:
                result = fcntl.ioctl(control.fileno(), request, interface)
            except OSError:
                continue
            addresses.append(socket.inet_ntoa(result[20:24]))
    if not addresses or not all(ipaddress.ip_address(address).is_loopback for address in addresses):
        pytest.skip("actual in.tftpd integration requires a loopback-only IPv4 namespace")


def _rrq(host, port, name, timeout=1.0):
    """Fetch one octet-mode file with a bounded, option-free TFTP RRQ."""
    packet = struct.pack("!H", 1) + name.encode("ascii") + b"\0octet\0"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(timeout)
        client.sendto(packet, (host, port))
        peer = None
        body = bytearray()
        for expected in range(1, 1 + 1024):
            response, address = client.recvfrom(4 + 512)
            if peer is None:
                peer = address
            elif address != peer:
                continue
            opcode = struct.unpack("!H", response[:2])[0]
            if opcode == 5:
                code = struct.unpack("!H", response[2:4])[0]
                return None, code
            if opcode != 3:
                raise AssertionError(f"unexpected TFTP opcode {opcode}")
            block = struct.unpack("!H", response[2:4])[0]
            if block != expected:
                raise AssertionError(f"unexpected TFTP block {block}, expected {expected}")
            chunk = response[4:]
            body.extend(chunk)
            client.sendto(struct.pack("!HH", 4, block), peer)
            if len(chunk) < 512:
                return bytes(body), None
        raise AssertionError("bounded TFTP RRQ exceeded 1024 blocks")


def _wrq(host, port, name, timeout=1.0):
    packet = struct.pack("!H", 2) + name.encode("ascii") + b"\0octet\0"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(timeout)
        client.sendto(packet, (host, port))
        response, _ = client.recvfrom(516)
    opcode = struct.unpack("!H", response[:2])[0]
    if opcode != 5:
        raise AssertionError(f"WRQ was not rejected with ERROR: opcode {opcode}")
    return struct.unpack("!H", response[2:4])[0]


def test_real_tftpd_serves_common_tree_and_denies_unsafe_requests(tmp_path):
    daemon = _daemon_or_skip()
    dnsmasq = _dnsmasq_or_skip()
    _loopback_namespace_or_skip()
    root = tmp_path / "public"
    root.mkdir()
    expected = b"common kernel bytes\0\x01"
    (root / "kernel.img").write_bytes(expected)
    os.chmod(root / "kernel.img", 0o444)
    map_file = tmp_path / "tftpd.map"
    map_file.write_bytes(MAP.read_bytes())
    proxy_config = tmp_path / "dnsmasq-proxy-pxe.conf"
    proxy_config.write_text(
        "interface=lo\n"
        "bind-interfaces\n"
        "port=0\n"
        "dhcp-range=192.0.2.0,proxy,255.255.255.0\n"
        "pxe-service=0,\"Raspberry Pi Boot\",bootcode.bin,192.0.2.10\n"
    )
    dnsmasq_check = subprocess.run(
        [dnsmasq, "--test", f"--conf-file={proxy_config}"],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert dnsmasq_check.returncode == 0, dnsmasq_check.stderr or dnsmasq_check.stdout

    # Select a high loopback port without ever binding a user-LAN address.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # network=none gives this test a loopback-only network namespace.  The
    # packaged daemon accepts the wildcard local address form for --address;
    # all client packets below still target 127.0.0.1.
    command = [daemon, "--foreground", "--ipv4", "--address", f":{port}",
               "--secure", "--map-file", str(map_file), str(root)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=1)
                raise AssertionError(f"in.tftpd exited: {stdout!r} {stderr!r}")
            try:
                data, error = _rrq("127.0.0.1", port, "kernel.img", timeout=0.2)
                if data is not None or error is not None:
                    break
            except socket.timeout:
                continue
        else:
            raise AssertionError("in.tftpd did not answer on loopback")

        assert _rrq("127.0.0.1", port, "kernel.img")[0] == expected
        assert _rrq("127.0.0.1", port, "deadbeef/kernel.img")[0] == expected
        data, error = _rrq("127.0.0.1", port, "deadbeef/deadbeef/kernel.img")
        assert data is None
        assert error == 1  # TFTP ENOENT: one prefix remains after the single rewrite.
        data, error = _rrq("127.0.0.1", port, "deadbeeZ/kernel.img")
        assert data is None
        assert error == 1  # TFTP ENOENT: non-hex prefixes are not rewritten.
        for unsafe in (
            "../kernel.img",
            "/kernel.img",
            "sub/../kernel.img",
            "..\\kernel.img",
            "deadbeef/../kernel.img",
        ):
            data, error = _rrq("127.0.0.1", port, unsafe)
            assert data is None
            assert error == 2  # TFTP EACCES: remap rule refused the request.
        assert _wrq("127.0.0.1", port, "new-file") == 2
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
