"""Linux base DisplayHost controller with a root-only broker ingress.

This entry point does not accept app self-reported authority or expose a network
listener. Central transport and calibration Save remain separate owners. The
bounded local event feed is observation only and acknowledges no Runtime commit.
"""

from __future__ import annotations

import argparse
import json
import os
import selectors
import socket
import struct
import subprocess
from dataclasses import asdict, is_dataclass
from pathlib import Path
from uuid import UUID

from appliance.feed import Feed, answer_feed_read
from appliance.feed_socket import FEED_READERS, FEEDS_GROUP, FeedListener
from appliance.unix_credentials import receive_credential_packet

from .weston import MAX_PACKET, SurfaceGrant, WestonBackend, _pairs, _surface

# The display feed for node readers (root, pw-health): the `events` op only, over the kernel
# feed socket. The root-only ingress (`<runtime>/ingress.sock`) keeps every operation.
FEED_SOCKET = Path("/run/photo-wall-display-feed/feed.sock")


def _unit(unit: str) -> dict[str, str]:
    result = subprocess.run(
        ["/usr/bin/systemctl", "show", unit, "--property=MainPID,InvocationID,ControlGroup"],
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def verify_process(grant: SurfaceGrant) -> bool:
    """Bracket kernel identity with the base-owned service manager invocation."""
    try:
        before = _unit("photo-wall-node-player.service")
        process = grant.surface.process
        if (
            int(before["MainPID"]) != process.pid
            or UUID(before["InvocationID"]) != process.invocation_id
        ):
            return False

        def kernel_sample():
            proc = Path(f"/proc/{process.pid}")
            stat = (proc / "stat").read_text()
            ticks = int(stat.rpartition(") ")[2].split()[19])
            uid_line = next(
                row for row in (proc / "status").read_text().splitlines() if row.startswith("Uid:")
            )
            return (
                ticks,
                tuple(int(v) for v in uid_line.split()[1:]),
                tuple((proc / "cgroup").read_text().splitlines()),
            )

        sample = kernel_sample()
        after = _unit("photo-wall-node-player.service")
        return (
            sample[0] == process.start_ticks
            and sample[1] == (grant.uid,) * 4
            and f"0::{before['ControlGroup']}" in sample[2]
            and after == before
            and kernel_sample() == sample
        )
    except (OSError, ValueError, KeyError, IndexError, StopIteration, subprocess.SubprocessError):
        return False


def _output_document(state) -> dict:
    """One `OutputState` as the feed snapshot shows it (JSON-native values only)."""
    admitted = state.admitted
    return {
        "output_id": state.key.output_id,
        "connected": state.connected,
        "admitted": None
        if admitted is None
        else {
            "pid": admitted.process.pid,
            "start_ticks": admitted.process.start_ticks,
            "invocation_id": str(admitted.process.invocation_id),
            "app_epoch": admitted.app_epoch,
            "frame_id": admitted.frame_id,
        },
        "diagnostic": state.diagnostic,
        "fault": state.fault,
    }


def feed_listener(
    controller: Controller, path: Path = FEED_SOCKET, *, owner_uid: int | None = None,
    group: int = FEEDS_GROUP, **seams
) -> FeedListener:
    """The display feed socket: owned by the controller's uid (pw-display), group
    pw-node-feeds, read by root and pw-health only (`seams`: `peer` and `kind`, for tests)."""
    return FeedListener(
        path,
        owner_uid=os.getuid() if owner_uid is None else owner_uid,
        group=group,
        readers=FEED_READERS,
        answer=controller.feed_events,
        max_reply=MAX_PACKET,
        **seams,
    )


class Controller:
    def __init__(self, backend: WestonBackend):
        self.backend = backend
        self.host = backend.initialize()
        self.feed = Feed(256)
        self.service = None

    def observe(self) -> None:
        observed = self.backend.dispatch()
        if observed is None:
            return
        if self.service is not None:
            self.service.observe(observed)
        self.feed.append(
            type(observed).__name__, asdict(observed) if is_dataclass(observed) else observed
        )

    def revise(self, decision) -> None:
        self.backend.revise(
            decision.surface, decision.decision_id, expires_boottime_ms=decision.expires_boottime_ms
        )

    def outputs_snapshot(self) -> list[dict]:
        """Every Output the host knows (bounded by its `max_outputs`) with its admission, so a
        reader that lapped the ring or restarted learns the Output set from any one read."""
        return [_output_document(state) for state in self.host.states()]

    def feed_events(self, value: dict) -> dict:
        """The feed socket's one operation: `events` (an absent `op` reads as `events`)."""
        if value.get("op", "events") != "events":
            raise ValueError("feed_read_request")
        return self.receive({**value, "op": "events"})

    def receive(self, value: dict) -> dict:
        operation = value.get("op")
        if operation == "events":
            return {
                **answer_feed_read(self.feed, value),
                "boot_id": self.host.boot_id,
                "incarnation_id": self.host.incarnation_id,
                "outputs": self.outputs_snapshot(),
            }
        if operation == "outputs":
            return {"outputs": [asdict(state) for state in self.host.states()]}
        if operation == "candidate":
            surface = _surface(value["surface"])
            grant = SurfaceGrant(surface, value["uid"], value.get("starting_new"))
            grant_id = self.backend.allow(grant)
            try:
                self.host.offer(surface)
            except Exception:
                self.backend.grants.pop(surface.output.output_id, None)
                raise
            return {"grant_id": grant_id}
        if operation == "withdraw":
            output = value["output_id"]
            return {"state": asdict(self.host.authorized_withdrawal(output))}
        if operation == "handoff":
            import time

            surface = _surface(value["surface"])
            state = self.host.authorize_handoff(surface, now_ms=time.monotonic_ns() // 1_000_000)
            return {"handoff_id": state.handoff_id}
        raise ValueError("display_operation_unsupported")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=Path("/run/photo-wall-display"))
    parser.add_argument(
        "--config", type=Path, help="base-owned systemd credential JSON; optional runtime path"
    )
    args = parser.parse_args()
    config = {}
    if args.config is not None:
        config = json.loads(args.config.read_bytes(), object_pairs_hook=_pairs)
        if type(config) is not dict or set(config) - {"runtime", "central", "serial", "offer_id"}:
            raise ValueError("display_config_fields")
        if "runtime" in config:
            args.runtime = Path(config["runtime"])
            if not args.runtime.is_absolute():
                raise ValueError("display_runtime_absolute")
    os.umask(0o077)
    peer = _unit("photo-wall-display.service")
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as channel:
        channel.connect(str(args.runtime / "control.sock"))
        backend = WestonBackend(
            channel,
            display_uid=os.getuid(),
            compositor_pid=int(peer["MainPID"]),
            verify_process=verify_process,
        )
        controller = Controller(backend)
        if config:
            from .service import DisplayService

            controller.service = DisplayService(
                controller,
                central=config["central"],
                serial=config["serial"],
                offer_id=UUID(config["offer_id"]),
                runtime=args.runtime,
            )
        with (
            feed_listener(controller) as feeds,
            socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as ingress,
        ):
            path = args.runtime / "ingress.sock"
            path.unlink(missing_ok=True)
            ingress.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            ingress.bind(str(path))
            os.chmod(path, 0o600)
            ingress.listen(4)
            with selectors.DefaultSelector() as selector:
                selector.register(channel, selectors.EVENT_READ, "backend")
                selector.register(ingress, selectors.EVENT_READ, "ingress")
                selector.register(feeds.listener, selectors.EVENT_READ, "feed")
                while True:
                    while backend.pending:
                        controller.observe()
                    if controller.service is not None:
                        controller.service.tick()
                    for key, _mask in selector.select(timeout=0.1):
                        if key.data == "backend":
                            controller.observe()
                            continue
                        if key.data == "feed":
                            feeds.serve()  # bounded: READS_PER_TURN short reads at most
                            continue
                        connection, _ = ingress.accept()
                        with connection:
                            connection.settimeout(1)
                            _pid, uid, _gid = struct.unpack(
                                "3i",
                                connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12),
                            )
                            if uid != 0:
                                continue
                            try:
                                credentials, raw = receive_credential_packet(
                                    connection, maximum=MAX_PACKET
                                )
                                if credentials[1] != 0:
                                    raise ValueError("display_ingress_peer")
                                value = json.loads(raw, object_pairs_hook=_pairs)
                                if type(value) is not dict:
                                    raise ValueError("display_ingress_type")
                                result = {"accepted": True, **controller.receive(value)}
                            except (ValueError, KeyError, TypeError, OSError) as exc:
                                result = {"accepted": False, "reason": type(exc).__name__}
                            wire = json.dumps(result, default=str, separators=(",", ":")).encode()
                            if len(wire) > MAX_PACKET:
                                wire = b'{"accepted":false,"reason":"response_bound"}'
                            connection.sendall(wire)


if __name__ == "__main__":
    main()
