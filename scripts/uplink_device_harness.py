#!/usr/bin/env python3
"""Uplink on the device's runtime (decision 0014 §11): the Pi runs Debian trixie's python3
(3.13) and OpenSSL (3.5) with nothing but the stdlib and stage 1's closure, while CI runs 3.12
with a venv. This harness proves the seam there, in the CI device root (the device set at
the declared pin, `scripts/test_netboot_e2e.py device-root`):

  mint DIR                        on the runner (the repo's venv): write the test CA bundle and
                                  the leaves tests/tls_fixture.py mints (no key kept in-tree)
  run --closure DIR --certs DIR   in the container, under `python3 -I -S`: import every staged
                                  closure module, then run the 301 chain, a TLS-to-TLS hop, the
                                  downgrade chain and the name-mismatch rows (verify codes 62, 64)
                                  against stdlib servers on loopback
  node-boot --closure DIR         in the container, under `python3 -I -S`: stage 1's node branch
    --cmdline-central URL           (`appliance.netboot_init.netboot`) from a command line naming
    --serial HEX8 --boot-id UUID    URL and `photowall.node=v2`, over the real transport, locate
    --rootmnt DIR                   and verified base fetch; it fakes only what no container has
                                    (the mount, RAM, the clock gate, the watchdog, the serial
                                    and the kernel boot id), writing the handoff under DIR

`run` exits 1 on any failed import or row. `node-boot` prints one JSON line
`{"outcome", "error", "sha256"}` and exits 0 on a handed-off boot, 3 on a named refusal. The stand-in servers below are stdlib only, and
tests/tls_fixture.py serves its stubs through them too, so there is one implementation.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.server
import importlib
import json
import ssl
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

# The certificates `mint` writes: (certfile, keyfile) per leaf, plus the CA bundle.
CA_BUNDLE = "ca.pem"
LEAVES = ("central", "other-name", "other-ip")


@dataclass
class Stub:
    port: int
    requests: list[str] = field(default_factory=list)   # request targets, in arrival order


class _StubServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, handler, context: ssl.SSLContext | None) -> None:
        super().__init__(("127.0.0.1", 0), handler)
        self.context = context
        self.stub = Stub(self.server_address[1])

    def finish_request(self, request, client_address) -> None:
        if self.context is not None:
            try:        # per connection, on the connection's own thread
                request = self.context.wrap_socket(request, server_side=True)
            except (ssl.SSLError, OSError):
                return  # the client refused the certificate: nothing was requested
        super().finish_request(request, client_address)


def server_context(certfile: str | Path, keyfile: str | Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(certfile), str(keyfile))
    return context


@contextlib.contextmanager
def serve_stub(handler: type[http.server.BaseHTTPRequestHandler], *,
               context: ssl.SSLContext | None = None) -> Iterator[Stub]:
    """Serve `handler` on an ephemeral 127.0.0.1 port, over TLS with `context` when given;
    yield a Stub whose `requests` records every request target that reached the handler."""

    class Recorded(handler):
        def parse_request(self) -> bool:
            parsed = super().parse_request()
            if parsed:
                self.server.stub.requests.append(self.path)
            return parsed

        def log_message(self, *args) -> None:
            pass

    server = _StubServer(Recorded, context)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        yield server.stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def redirect_stub(location: str, *, context: ssl.SSLContext | None = None,
                  status: int = 301, keep_path: bool = False
                  ) -> contextlib.AbstractContextManager[Stub]:
    """A stand-in gateway that answers every GET with `status` and `Location: location`, or,
    with `keep_path`, `location` followed by the request target (a gateway that moved every
    path to another host)."""

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status)
            self.send_header("Location", location + self.path if keep_path else location)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return serve_stub(Redirect, context=context)


def central_stub(handler: type[http.server.BaseHTTPRequestHandler]
                 = http.server.BaseHTTPRequestHandler
                 ) -> type[http.server.BaseHTTPRequestHandler]:
    """The one stand-in base for Central: GET LOCATE_PATH answers `identity_body()` as Central's
    real route does; every other request goes to `handler`."""
    from contracts.central_identity import LOCATE_PATH, identity_body

    class CentralStub(handler):
        def do_GET(self) -> None:
            if urlsplit(self.path).path != LOCATE_PATH:
                delegate = getattr(super(), "do_GET", None)
                return delegate() if delegate is not None else self.send_error(404)
            body = identity_body()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return CentralStub


def mint(directory: Path) -> None:
    """On the runner: the CA bundle and the three leaves, from tests/tls_fixture.py."""
    from tests import tls_fixture as tls

    directory.mkdir(parents=True, exist_ok=True)
    tls.write_bundle(directory / CA_BUNDLE, tls.CA)
    for name, leaf in zip(LEAVES, (tls.CENTRAL, tls.OTHER_NAME, tls.OTHER_IP), strict=True):
        with leaf.files() as (certfile, keyfile):
            (directory / f"{name}.pem").write_bytes(Path(certfile).read_bytes())
            (directory / f"{name}.key").write_bytes(Path(keyfile).read_bytes())


def closure_modules(closure: Path) -> list[str]:
    """Every module staged under `closure`, as a dotted name."""
    names = []
    for path in sorted(closure.rglob("*.py")):
        parts = path.relative_to(closure).with_suffix("").parts
        names.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return names


def _rows(certs: Path) -> list[tuple[str, Callable[[], None]]]:
    from uplink.causes import Cause, UplinkError
    from uplink.locate import locate
    from uplink.origin import Origin
    from uplink.transport import HttpTransport
    from uplink.trust import Trust

    transport = HttpTransport(trust=Trust.public(certs / CA_BUNDLE))
    context = {name: server_context(certs / f"{name}.pem", certs / f"{name}.key")
               for name in LEAVES}

    def refused(root: str, expected: tuple[Cause, str, str]) -> None:
        try:
            locate(Origin.parse_root(root), transport=transport)
        except UplinkError as error:
            got = (error.cause, error.reason, error.detail)
            assert got == expected, f"{got} != {expected}"
            return
        raise AssertionError(f"{root} located; expected {expected}")

    def chain_301() -> None:
        with serve_stub(central_stub(), context=context["central"]) as central, \
                redirect_stub(f"https://127.0.0.1:{central.port}/v1/locate") as gateway:
            located = locate(Origin.parse_root(f"http://localhost:{gateway.port}/"),
                             transport=transport)
        assert located.origin == Origin("https", "127.0.0.1", central.port), located

    def tls_to_tls() -> None:
        with serve_stub(central_stub(), context=context["central"]) as central, \
                redirect_stub(f"https://127.0.0.1:{central.port}/v1/locate",
                              context=context["central"], status=302) as gateway:
            located = locate(Origin.parse_root(f"https://localhost:{gateway.port}/"),
                             transport=transport)
        assert located.origin == Origin("https", "127.0.0.1", central.port), located

    def downgrade() -> None:
        with serve_stub(central_stub()) as plain, \
                redirect_stub(f"http://127.0.0.1:{plain.port}/v1/locate",
                              context=context["central"], status=302) as gateway:
            refused(f"https://localhost:{gateway.port}/", (Cause.REDIRECT, "downgrade", ""))
        assert plain.requests == [], plain.requests

    def dns_name_mismatch() -> None:
        with serve_stub(central_stub(), context=context["other-name"]) as server:
            refused(f"https://localhost:{server.port}/",
                    (Cause.TLS, "hostname", "verify_code=62"))

    def ip_mismatch() -> None:
        with serve_stub(central_stub(), context=context["other-ip"]) as server:
            refused(f"https://127.0.0.1:{server.port}/",
                    (Cause.TLS, "hostname", "verify_code=64"))

    return [("301 chain to Central over verified TLS", chain_301),
            ("TLS-to-TLS hop: SNI and the name check follow the hop", tls_to_tls),
            ("https -> http downgrade refused, nothing sent", downgrade),
            ("DNS-name mismatch (62)", dns_name_mismatch),
            ("IP-address mismatch (64)", ip_mismatch)]


def run(closure: Path, certs: Path) -> int:
    print(f"python3 {sys.version.split()[0]}; {ssl.OPENSSL_VERSION}; flags isolated="
          f"{sys.flags.isolated} no_site={sys.flags.no_site}")
    sys.path.insert(0, str(closure))
    failures = 0
    for name in closure_modules(closure):
        try:
            importlib.import_module(name)
        except Exception as error:  # every import failure is reported, then the leg fails
            print(f"FAIL import {name}: {type(error).__name__}: {error}")
            failures += 1
    if failures:
        return 1
    print(f"OK imported {len(closure_modules(closure))} closure modules")
    for label, row in _rows(certs):
        try:
            row()
        except Exception as error:
            print(f"FAIL {label}: {type(error).__name__}: {error}")
            failures += 1
        else:
            print(f"OK {label}")
    return 1 if failures else 0


NODE_HANDED_OFF, NODE_REFUSED = 0, 3


class _NodeLog:
    """Stage 1's console, on stderr."""

    debug = False

    def info(self, message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    def detail(self, message: str) -> None:
        print(message, file=sys.stderr, flush=True)


class _NodeKeeper:
    """The watchdog keeper: no watchdog here, every pet is a no-op."""

    summary = "harness (no watchdog)"

    def pet(self) -> None:
        pass

    def paced(self, blocks):
        yield from blocks

    def hand_over(self) -> None:
        pass


class _NodeOps:
    """No mount or RAM disk in a container: `ram()` is a temp dir and `mount_root` records the
    sha256 of the verified image it was handed."""

    def __init__(self, run_root: Path) -> None:
        self.run_root = run_root
        self.mounted: str | None = None

    def configure_networking(self) -> None:
        pass

    def network_info(self) -> dict:
        return {"ip": "127.0.0.1"}

    def ram(self) -> Path:
        path = self.run_root / "ram"
        path.mkdir(exist_ok=True)
        return path

    def mount_root(self, image: Path, rootmnt: Path) -> None:
        digest = hashlib.sha256()
        with image.open("rb") as stream:
            while block := stream.read(1 << 20):
                digest.update(block)
        self.mounted = digest.hexdigest()

    def hand_over_modules(self, rootmnt: Path, *, pet, release=None) -> str:
        return "modules=none (harness)"


def node_boot(closure: Path | None, *, central: str, serial: str, boot_id: str, rootmnt: Path,
              ca_bundle: Path | None = None) -> tuple[int, dict]:
    """One stage 1 node boot from `central` (the command line's root); (exit code, the printed
    JSON). `closure` goes first on sys.path (the device run); `ca_bundle` replaces the device's
    trust store (an in-process run with no Debian bundle)."""
    if closure is not None:
        sys.path.insert(0, str(closure))
    from appliance.bootstrap import BootstrapError
    from appliance.netboot_init import NetbootError, netboot
    from contracts.clock_record import ClockRecord, ClockState
    from uplink.causes import UplinkError
    from uplink.transport import HttpTransport
    from uplink.trust import Trust

    class Settled:
        def settle(self) -> ClockRecord:
            return ClockRecord(state=ClockState.SYNCED, floor=1, raised_to_floor=False,
                               tier=None, source=None, offset=None, stepped=False, tried=(),
                               writer="netboot", written_at=time.time())

    rootmnt.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="node-boot-") as run_root:
        ops = _NodeOps(Path(run_root))
        trust = Trust.public() if ca_bundle is None else Trust.public(ca_bundle)
        try:
            netboot({"photowall.central": central, "photowall.node": "v2"}, rootmnt, ops=ops,
                    transport=HttpTransport(trust=trust), clock_gate=Settled(),
                    keeper=_NodeKeeper(), trust_provenance="harness", serial_reader=lambda: serial,
                    log=_NodeLog(), offer_mode=True, boot_id_reader=lambda: boot_id)
        except (NetbootError, BootstrapError) as error:
            return NODE_REFUSED, {"outcome": "refused", "error": str(error), "sha256": None}
        except UplinkError as error:
            code = error.central_error or f"{error.cause.value}:{error.reason}"
            return NODE_REFUSED, {"outcome": "refused", "error": code, "sha256": None}
    return NODE_HANDED_OFF, {"outcome": "handed_off", "error": None, "sha256": ops.mounted}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    minting = commands.add_parser("mint")
    minting.add_argument("directory", type=Path)
    running = commands.add_parser("run")
    running.add_argument("--closure", type=Path, required=True)
    running.add_argument("--certs", type=Path, required=True)
    booting = commands.add_parser("node-boot")
    booting.add_argument("--closure", type=Path, required=True)
    booting.add_argument("--cmdline-central", required=True)
    booting.add_argument("--serial", required=True)
    booting.add_argument("--boot-id", required=True)
    booting.add_argument("--rootmnt", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "mint":
        mint(args.directory)
        return 0
    if args.command == "node-boot":
        status, result = node_boot(args.closure, central=args.cmdline_central, serial=args.serial,
                                   boot_id=args.boot_id, rootmnt=args.rootmnt)
        print(json.dumps(result, sort_keys=True))
        return status
    return run(args.closure, args.certs)


if __name__ == "__main__":
    sys.exit(main())
