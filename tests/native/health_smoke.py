"""Opt-in native health and release acceptance smoke fixture.

This is deliberately a fixture, rather than a qualification test.  It runs as
root on a disposable Linux/Xvfb host and uses temporary paths only.  The
central authority, rootfs, and physical display are synthetic; the native
adapter, Player health generation, signed SlotStore, and 30 second acceptance
gate are real production objects.
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
import threading
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


def _write_boot_report(path: Path, *, boot_id: str, release_id: str, slot: str) -> None:
    # The bootstrap report is the protected root-owned fixture input.  The
    # healthy report below is always produced by PlayerService._write_health.
    payload = {
        "boot_id": boot_id,
        "fault": None,
        "persistence": "durable",
        "release_id": release_id,
        "schema": 1,
        "slot": slot,
        "trial": True,
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            fd = -1
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


def _build_fixture(report_path: Path | None) -> dict:
    if sys.platform != "linux":
        raise RuntimeError("linux_required")
    if os.geteuid() != 0:
        raise RuntimeError("root_required")
    if not os.environ.get("DISPLAY"):
        raise RuntimeError("xvfb_display_required")

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from appliance import updates
    from contracts.enrollment import OutputReport
    from contracts.models import FrameProfile, OutputBinding, PlayerConfiguration
    from contracts.release import Release
    from player.cache import Cache
    from player.executor import Executor
    from player.identity import load_identity
    from player.native import NativeOutput, NativeRenderer
    from player.service import PlayerConfig, PlayerService, Registration

    started = time.monotonic()
    renderer = None
    cache = None
    accept_thread = None
    accept_result: dict[str, object] = {}
    # ExitStack closes the worker, cache, and GTK resources before the
    # TemporaryDirectory removes their backing paths.  The acceptance thread
    # is joined even on a failed assertion so no background writer survives.
    with tempfile.TemporaryDirectory(prefix="photo-wall-native-health-") as directory, \
            contextlib.ExitStack() as cleanup:
        root = Path(directory)
        state_root = root / "appliance-state"
        state_root.mkdir(mode=0o755)
        (state_root / ".photo-wall-state-v1").write_bytes(updates.MARKER)
        os.chmod(state_root / ".photo-wall-state-v1", 0o444)
        service_state = root / "player-state"
        service_state.mkdir(mode=0o700)
        runtime = root / "player-runtime"
        runtime.mkdir(mode=0o700)
        health_path = runtime / "service-health.json"
        boot_report = root / "boot.json"
        public_key_path = root / "release.pub.pem"
        ca_path = root / "synthetic-ca.pem"
        ca_path.write_bytes(b"synthetic central; no network is used\n")

        signing_key = Ed25519PrivateKey.generate()
        public_key_path.write_bytes(signing_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        os.chmod(public_key_path, 0o644)
        rootfs = b"synthetic rootfs fixture\n"
        boot_abi = "a" * 64
        configuration_sha256 = "b" * 64
        release = Release(
            revision="c" * 40,
            boot_abi=boot_abi,
            configuration_sha256=configuration_sha256,
            rootfs_sha256=hashlib.sha256(rootfs).hexdigest(),
            rootfs_size=len(rootfs),
        )
        manifest = release.encode()
        signature = signing_key.sign(manifest)
        store = updates.SlotStore(state_root, public_key_path, boot_abi, configuration_sha256)
        staged = store.stage(manifest, signature, (rootfs,))
        boot_id = updates._linux_boot_id()
        selection = store.select_boot(boot_id)
        if selection is None or selection.release.release_id != staged.release_id:
            raise RuntimeError("trial_selection_failed")
        _write_boot_report(boot_report, boot_id=boot_id,
                           release_id=release.release_id, slot=selection.slot)

        native_outputs = (
            NativeOutput("health-output-1", "org.photowall.health1", 320, 240),
            NativeOutput("health-output-2", "org.photowall.health2", 320, 240),
        )
        renderer = NativeRenderer(native_outputs, decoder_limit=2,
                                  texture_budget=32 * 1024**2)
        cleanup.callback(renderer.close)
        native_capacity_before = renderer.capacity(()).available
        if native_capacity_before:
            raise RuntimeError("native_capacity_started_initialized")

        identity = load_identity(service_state)
        player_id = "p-" + hashlib.sha256(bytes.fromhex(identity.public_key)).hexdigest()[:32]
        outputs = tuple(OutputReport(output_id=output.output_id,
                                     width_px=output.width, height_px=output.height)
                        for output in native_outputs)
        config = PlayerConfig(
            central_origin="https://synthetic.invalid",
            ca_file=str(ca_path),
            state_dir=str(service_state),
            cache_bytes=8 * 1024**2,
            decoder_limit=2,
            texture_budget=32 * 1024**2,
        )
        service = PlayerService(config, identity, outputs, renderer, lambda callback: None,
                                health_path=health_path)
        service.registration = Registration(player_id=player_id,
                                            token="synthetic-authority-token-" + "x" * 16,
                                            authority_epoch=1)
        cache = Cache(service_state / "cache", config.cache_bytes)
        cleanup.callback(cache.close)
        service.cache = cache
        service.executor = Executor(player_id, cache, renderer, service.clock,
                                    service.mapping, service_state / "execution.json")
        cleanup.callback(service._worker.shutdown, wait=True, cancel_futures=True)
        bindings = tuple(
            OutputBinding(output_id=output.output_id, frame_id=f"health-frame-{index}",
                          generation=1,
                          profile=FrameProfile(width_px=output.width, height_px=output.height,
                                               diagonal_inches=20))
            for index, output in enumerate(native_outputs, 1)
        )
        configuration = PlayerConfiguration(
            player_id=player_id, authority_epoch=1, configuration_revision=1,
            bindings=bindings, enabled_outputs=tuple(binding.output_id for binding in bindings),
        )
        service.executor.accept_configuration(configuration)
        service._configuration = configuration
        # Establish the same bounded clock mapping the production service
        # receives from its authority before checking native capacity.  This
        # makes the first false health result specifically exercise the native
        # gate, rather than an uninitialized clock.
        service.mapping.establish(.01)
        if not service.mapping.healthy():
            raise RuntimeError("synthetic_clock_not_healthy")
        health_before_native = service._health()
        service._write_health(health_before_native)
        if health_before_native:
            raise RuntimeError("uninitialized_health_started_healthy")

        if not _wait_native_capacity(renderer):
            raise RuntimeError("native_capacity_timeout")
        native_capacity_after = renderer.capacity(()).available
        health_after_mapping = service._health()
        if not (native_capacity_after and health_after_mapping):
            raise RuntimeError("initialized_health_not_healthy")

        def accept() -> None:
            try:
                accept_result["accepted"] = updates.accept_trial(
                    store, release.release_id, boot_report=boot_report,
                    health_report=health_path)
            except Exception as error:  # public result is deliberately sanitized
                accept_result["error"] = type(error).__name__

        accept_thread = threading.Thread(target=accept, name="native-health-accept")
        accept_started = time.monotonic()
        accept_thread.start()
        cleanup.callback(accept_thread.join)
        while accept_thread.is_alive():
            _pump_glib()
            # Synthetic authority samples keep the normal mapping fresh;
            # health itself still comes from the production native adapter.
            service.mapping.establish(.01)
            healthy = service._health()
            service._write_health(healthy)
            time.sleep(.1)
            if time.monotonic() - accept_started > 185:
                raise RuntimeError("acceptance_timeout")
        accept_thread.join()
        acceptance_elapsed = time.monotonic() - accept_started
        if acceptance_elapsed < 30:
            raise RuntimeError("acceptance_interval_shortened")
        if accept_result.get("accepted") is not True:
            raise RuntimeError("trial_not_accepted")
        state = store._state()
        selected_after = state.get("selected")
        if (not state.get("active") or state["active"]["release_id"] != release.release_id
                or not selected_after or not selected_after.get("accepted")):
            raise RuntimeError("accepted_state_missing")

        result = {
            "schema": 1,
            "result": "passed",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "acceptance_elapsed_seconds": round(acceptance_elapsed, 3),
            "boot_id": boot_id,
            "release_id": release.release_id,
            "native_capacity_before": native_capacity_before,
            "native_capacity_after": native_capacity_after,
            "health_before_native_initialization": health_before_native,
            "health_after_native_initialization": health_after_mapping,
            "qualification": {
                "production_native_adapter": True,
                "central": "synthetic",
                "rootfs": "synthetic",
                "physical": False,
            },
        }
        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
        return result

    # Kept outside the TemporaryDirectory scope only to make cleanup explicit
    # in the face of an acceptance thread or native constructor failure.


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
