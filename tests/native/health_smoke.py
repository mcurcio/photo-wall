"""Opt-in native health plus real central trial acceptance on disposable Linux.

Requires Xvfb, root, and PHOTO_WALL_TEST_DATABASE_URL. Synthetic root bytes and
clock samples do not qualify an image, Pi firmware, HDMI, or visible timing.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

# The retained Linux fixture uses GTK's GLES path under Xvfb.  Do this before
# importing player.native, whose native libraries are intentionally lazy.
os.environ.setdefault("GDK_GL", "gles")
os.environ.setdefault("PYOPENGL_PLATFORM", "egl")


def _pump_glib() -> None:
    """Run bounded GLib work on the renderer's owning (main) thread."""
    from gi.repository import GLib

    context = GLib.MainContext.default()
    for _ in range(25):
        if not context.pending():
            break
        context.iteration(False)


def _wait_native_capacity(renderer, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _pump_glib()
        if renderer.capacity(()).available:
            return True
        time.sleep(.02)
    return False


@contextlib.contextmanager
def _release_database():
    """Own one random schema, leaving the supplied disposable database intact."""
    import uuid

    import psycopg
    from psycopg.conninfo import make_conninfo

    from central.db import Database

    dsn = os.environ.get("PHOTO_WALL_TEST_DATABASE_URL")
    if not dsn:
        raise RuntimeError("disposable_postgresql_required")
    schema = "pw_native_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
    db = Database(make_conninfo(dsn, options=f"-c search_path={schema}"))
    try:
        db.migrate()
        yield db
    finally:
        db.close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema)))


def _build_fixture(report_path: Path | None) -> dict:
    if sys.platform != "linux" or os.geteuid() != 0 or not os.environ.get("DISPLAY"):
        raise RuntimeError("disposable_linux_root_xvfb_required")
    import uuid

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from appliance.updates import TrialWatchdog, _linux_boot_id
    from central.registry import Registry
    from central.releases import ReleaseAuthority
    from contracts.enrollment import OutputReport
    from contracts.models import FrameProfile, OutputBinding, PlayerConfiguration
    from contracts.release import BootRequest, Release
    from contracts.time import SystemClock
    from player.cache import Cache
    from player.executor import Executor
    from player.identity import load_identity
    from player.native import NativeOutput, NativeRenderer
    from player.service import BootContext, PlayerConfig, PlayerService, Registration

    started = time.monotonic()
    with _release_database() as db, tempfile.TemporaryDirectory(prefix="photo-wall-native-health-") as directory, \
            contextlib.ExitStack() as cleanup:
        root = Path(directory)
        health_path = root / "service-health.json"
        clock = SystemClock()
        key = Ed25519PrivateKey.generate()
        authority = ReleaseAuthority(db, clock, key.public_key(), "a" * 64, "b" * 64)
        releases = []
        for content in (b"accepted synthetic root", b"candidate synthetic root"):
            release = Release("c" * 40, "a" * 64, "b" * 64, hashlib.sha256(content).hexdigest(), len(content))
            authority.register(release.encode(), key.sign(release.encode()))
            releases.append(release)
        authority.initialize_default(releases[0].release_id)
        device_id = "device-" + "d" * 64
        authority.select_boot(BootRequest(device_id, str(uuid.uuid4()), os.urandom(24).hex()))
        authority.stage(device_id, releases[1].release_id)
        boot_id = _linux_boot_id()
        ticket = authority.select_boot(BootRequest(device_id, boot_id, os.urandom(24).hex()))
        context = BootContext.model_validate(dict(schema=2, boot_id=boot_id, device_id=device_id,
            ticket_id=ticket.ticket_id, release_id=ticket.release_id, trial=True, persistence="volatile", fault=None))
        native_outputs = (NativeOutput("health-output-1", "org.photowall.health1", 320, 240),
                          NativeOutput("health-output-2", "org.photowall.health2", 320, 240))
        renderer = NativeRenderer(native_outputs, decoder_limit=2, texture_budget=32 * 1024**2)
        cleanup.callback(renderer.close)
        native_capacity_before = renderer.capacity(()).available
        if native_capacity_before:
            raise RuntimeError("native_capacity_started_initialized")
        identity = load_identity()
        outputs = tuple(OutputReport(output_id=o.output_id, width_px=o.width, height_px=o.height) for o in native_outputs)
        registry = Registry(db, clock, authority)
        challenge = registry.challenge(identity.public_key)
        registration = registry.enroll(identity.enrollment(challenge["nonce"], outputs,
            device_id=device_id, boot_id=boot_id, ticket_id=ticket.ticket_id))
        player_id = registration["player_id"]
        config = PlayerConfig(central_origin="http://synthetic.invalid", allow_http=True,
                              cache_bytes=8 * 1024**2, decoder_limit=2, texture_budget=32 * 1024**2)
        service = PlayerService(config, identity, outputs, renderer, lambda callback: None,
                                health_path=health_path, boot_context=context, clock=clock)
        cleanup.callback(service._worker.shutdown, wait=True, cancel_futures=True)
        service.registration = Registration.model_validate(registration)
        cache = Cache(root / "cache", config.cache_bytes)
        cleanup.callback(cache.close)
        service.cache = cache
        service.executor = Executor(player_id, cache, renderer, clock, service.mapping)
        bindings = tuple(OutputBinding(output_id=o.output_id, frame_id=f"health-frame-{i}", generation=1,
            profile=FrameProfile(width_px=o.width, height_px=o.height, diagonal_inches=20))
            for i, o in enumerate(native_outputs, 1))
        configuration = PlayerConfiguration(player_id=player_id, authority_epoch=1, configuration_revision=1,
            bindings=bindings, enabled_outputs=tuple(b.output_id for b in bindings))
        service.executor.accept_configuration(configuration)
        service._configuration = configuration
        service.mapping.establish(.01)
        health_before_native = service._health()
        if health_before_native:
            raise RuntimeError("uninitialized_health_started_healthy")
        if not _wait_native_capacity(renderer):
            raise RuntimeError("native_capacity_timeout")
        signals = []
        accepted_started = clock.monotonic()
        watchdog = TrialWatchdog(boot_id, trial=True, started=accepted_started, signal_reboot=signals.append)
        while not watchdog.finished:
            _pump_glib()
            service.mapping.establish(.01)
            healthy = service._health()
            response = authority.health(ticket.ticket_id, player_id, 1, healthy=healthy, observed_at=clock.utc())
            service.release_accepted = response["accepted"]
            service._write_health(healthy)
            watchdog.observe(json.loads(health_path.read_bytes()), clock.monotonic())
            if clock.monotonic() - accepted_started > 185:
                raise RuntimeError("acceptance_timeout")
            time.sleep(.1)
        elapsed = clock.monotonic() - accepted_started
        if signals or elapsed < 30 or authority.inventory()[0]["accepted_release_id"] != ticket.release_id:
            raise RuntimeError("central_trial_not_accepted")
        result = dict(schema=2, result="passed", elapsed_seconds=round(time.monotonic() - started, 3),
            acceptance_elapsed_seconds=round(elapsed, 3), boot_id=boot_id, release_id=ticket.release_id,
            native_capacity_before=native_capacity_before, native_capacity_after=renderer.capacity(()).available,
            health_before_native_initialization=health_before_native, health_after_native_initialization=service._health(),
            qualification=dict(production_native_adapter=True, central="real PostgreSQL release domain",
                transport="in-process fixture", rootfs="synthetic", physical=False))
        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="optional bounded public JSON report path")
    args = parser.parse_args()
    try:
        result = _build_fixture(args.report)
    except Exception as error:
        result = {
            "schema": 1,
            "result": "failed",
            "error": re.sub(r"[^a-zA-Z0-9_:-]", "_", type(error).__name__)[:64],
            "qualification": {
                "production_native_adapter": False,
                "central": "synthetic",
                "rootfs": "synthetic",
                "physical": False,
            },
        }
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("result") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
