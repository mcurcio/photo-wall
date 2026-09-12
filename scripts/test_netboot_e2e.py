"""p4-boot-chain s3, Part A -- process-level boot-time app-delivery tracer.

Owner-chosen shape (2026-09-12): process-level, NOT a QEMU kernel boot, and
self-contained -- it builds and installs the REAL new-model Player `.deb`
in-job, with NO `release-artifacts` reusable workflow and NO pre-qualified
OS-base. This proves the 0009 *software* contract that gates retirement of the
old signed netboot/release-authority pipeline (s4/s5):

  the REAL appliance.provision.Bootstrapper, pointed at a REAL central
  (uvicorn) + REAL Postgres by an explicit injected origin (no mDNS /
  multicast), for real fetches the app manifest, downloads the REAL Player
  `.deb`, verifies its sha256 (corruption check), and INSTALLS it with a REAL
  `apt-get install -y <deb>` inside a `debian:trixie-slim` arm64 container
  that has network + apt sources -- so the `.deb`'s declared `Depends`
  (GTK/GStreamer/weston/Mesa + the `python3-*` libraries, 0009
  p4-deb-full-depends) actually RESOLVE from deb.debian.org. It then writes the
  central_origin handoff, invokes the start step, and -- when a served byte is
  flipped -- REFUSES (no install).

What is REAL here vs captured
-----------------------------
- REAL: central + Postgres (docker compose, the production `central` image);
  the app-package HTTP surface (`/v1/operator/app`, `/v1/operator/app/current`,
  `/v1/app/manifest`, `/v1/app/package/{sha256}.deb`); the Bootstrapper's
  fetch + streamed sha256 verification (appliance/provision.py, unmodified);
  the install -- a genuine `apt-get install -y <deb>` of the REAL production
  `.deb` inside a `debian:trixie-slim` arm64 container with real apt sources,
  so its full declared dependency stack resolves from the distro repo (the
  same command appliance.provision.apt_install runs on the real base); the
  Python-3.13 import smoke inside that same post-install container (the
  Player's first-party closure plus the GTK/GStreamer/OpenGL bindings import
  under trixie's Python 3.13 + distro package versions); the corruption
  refusal (a flipped byte in the served `.deb`).
- CAPTURED: the systemd `start` step -- there is no systemd PID1 in the CI
  container, so `start_unit` is a recording stub; the tracer asserts the
  Bootstrapper *invoked* it exactly once, after a landed install. Real GTK/HDMI
  pixels and a real kernel/initrd Pi boot are the owner's hardware bench step
  and are out of scope (0009); the import smoke catches 3.13/version import
  breakage in CI without rendering a pixel.

Central is configured for the NEW ticketless model: NO release-authority env
(PHOTO_WALL_RELEASE_*) is set, proving central boots and serves the app
package with no signing key -- the retirement precondition for s4.

Part B (a headless Player enrolls TICKETLESS -> pending -> operator bind ->
render smoke) is proven by the re-keyed scripts/demo_wall.py runner under the
existing "Controller and Player software e2e" job. This tracer owns Part A
(boot-time delivery).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from appliance.provision import (  # noqa: E402
    Bootstrapper,
    fetch_manifest,
    fetch_package,
    write_public_config,
)
from scripts.demo_wall import POSTGRES_IMAGE  # noqa: E402

# The REAL install target: a stock Debian trixie arm64 image with apt sources,
# the same image base-image.yml uses to prove the `.deb`'s Depends resolve.
# Deliberately unpinned (matches base-image.yml): the apt-deps model tracks the
# distro's own floating `python3-*`/native versions, so a digest pin here would
# be false precision -- the dependencies it resolves are not pinned anyway.
TRIXIE_IMAGE = "debian:trixie-slim"

# The new-model `.deb` is Architecture: all (only `.py` files + units; the
# native stack comes from apt Depends), so the filename ends in `_all.deb`.
DEB_NAME = re.compile(r"photo-wall-player_(?P<version>[A-Za-z0-9][A-Za-z0-9.+~-]*)_all\.deb")

# The Player's first-party module the install must land, and the bindings whose
# import closure must resolve on trixie's Python 3.13 + distro package versions.
# This is an import smoke (no display, no render) -- it catches 3.13/version
# import breakage, the class of failure the apt-deps move most risks; real
# pixels remain the owner's Pi bench step (0009).
IMPORT_SMOKE = (
    "import json, sys\n"
    "import player.service\n"
    "import gi\n"
    "gi.require_version('Gtk', '3.0')\n"
    "gi.require_version('Gst', '1.0')\n"
    "from gi.repository import Gtk, Gst\n"
    "import OpenGL\n"
    "print(json.dumps({'python': sys.version.split()[0], "
    "'player_service_file': player.service.__file__, "
    "'gtk': Gtk._version, 'gst': Gst._version}))\n"
)


class TracerError(RuntimeError):
    """A fixed, safe diagnostic code -- never untrusted command/HTTP output."""


def require(condition: object, code: str) -> None:
    if not condition:
        raise TracerError(code)


def run(args: list[str], *, timeout: int = 120, capture: bool = True) -> str:
    try:
        result = subprocess.run(
            args, capture_output=capture, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise TracerError(f"command_failed:{args[0]}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()[-1:] if capture else []
        raise TracerError(f"command_failed:{args[0]}:{''.join(detail)[:200]}")
    return result.stdout if capture else ""


def compose_document(*, central_image: str, password: str, admin_token: str,
                     port: int, app_root: Path) -> dict:
    """Minimal REAL central + Postgres -- NOT the demo_wall media topology.

    No worker, Immich, media pipeline, or operator role: this tracer exercises
    only the app-package delivery surface + the Bootstrapper. Central is stood
    up with NO PHOTO_WALL_RELEASE_* env (the new ticketless model): it must
    boot and serve the `.deb` with no release authority configured.
    """
    dsn = f"postgresql://wall:{password}@wall-db:5432/wall"
    return {
        "services": {
            "database": {
                "image": POSTGRES_IMAGE,
                "environment": {
                    "POSTGRES_DB": "wall", "POSTGRES_USER": "wall",
                    "POSTGRES_PASSWORD": password,
                },
                "networks": {"pw": {"aliases": ["wall-db"]}},
                "healthcheck": {
                    "test": ["CMD", "pg_isready", "-U", "wall", "-d", "wall"],
                    "interval": "2s", "timeout": "3s", "retries": 30,
                },
                "restart": "no", "mem_limit": "192m",
            },
            "central": {
                "image": central_image,
                "user": "10001:10001",
                "environment": {
                    "PHOTO_WALL_DATABASE_URL": dsn,
                    "PHOTO_WALL_ADMIN_TOKEN": admin_token,
                    "PHOTO_WALL_APP_ROOT": "/app-packages",
                    "PHOTO_WALL_MEDIA_ROOT": "/tmp",
                    "PHOTO_WALL_MDNS_ADVERTISE": "false",
                    "PHOTO_WALL_HORIZON_SECONDS": "15",
                },
                "volumes": [f"{app_root}:/app-packages:ro"],
                "ports": [f"127.0.0.1:{port}:8000"],
                "networks": ["pw"],
                "restart": "no", "mem_limit": "384m",
                "depends_on": {"database": {"condition": "service_healthy"}},
            },
        },
        "networks": {"pw": {}},
    }


class Central:
    """Drives the REAL central over its published http port."""

    def __init__(self, base: list[str], origin: str, admin_token: str):
        self.base = base
        self.origin = origin.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.headers = {"Authorization": "Bearer " + admin_token,
                        "Content-Type": "application/json"}

    def compose(self, *args: str, timeout: int = 120, capture: bool = True) -> str:
        return run([*self.base, *args], timeout=timeout, capture=capture)

    def _request(self, method: str, path: str, body: dict | None = None,
                 *, authenticated: bool = False, expect: int = 200) -> object:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.origin + path, data=data, method=method,
                                         headers=self.headers if authenticated else {})
        try:
            with self.opener.open(request, timeout=15) as response:
                status = response.status
                payload = response.read(1024 * 1024)
        except urllib.error.HTTPError as error:
            raise TracerError(f"central_http_{error.code}:{method}:{path}") from None
        except (OSError, urllib.error.URLError) as error:
            raise TracerError("central_transport") from error
        require(status == expect, f"central_http_{status}:{method}:{path}")
        return json.loads(payload) if payload else None

    def wait_healthy(self, *, seconds: int = 120) -> None:
        deadline = time.monotonic() + seconds
        while True:
            try:
                self._request("GET", "/healthz")
                return
            except TracerError:
                require(time.monotonic() < deadline, "central_startup_timeout")
                time.sleep(1)

    def register_and_promote(self, version: str, sha256: str, size: int) -> None:
        self._request("POST", "/v1/operator/app",
                      {"version": version, "sha256": sha256, "size": size},
                      authenticated=True, expect=201)
        self._request("PUT", "/v1/operator/app/current", {"sha256": sha256},
                      authenticated=True, expect=200)

    def manifest(self) -> dict:
        return self._request("GET", "/v1/app/manifest")


class TrixieInstaller:
    """The REAL install seam: a long-lived `debian:trixie-slim` arm64 container
    with network + apt sources, into which the fetched `.deb` bytes are written
    and installed by a genuine `apt-get install -y <deb>`.

    This mirrors appliance.provision.apt_install's contract exactly -- it
    receives the downloaded bytes + manifest and runs the SAME `apt-get install
    -y <deb path>` command -- but targets a throwaway container instead of the
    running RAM root, because CI has no diskless base. Because it is a real apt
    install against real distro sources, the `.deb`'s declared `Depends`
    (GTK/GStreamer/weston/Mesa + `python3-*`) actually resolve from
    deb.debian.org: a stub could not fail on an unsatisfiable dependency, this
    does. `apt-get update` is run once at container start to populate the lists
    trixie-slim ships empty (the real base already carries them); the install
    command itself is the faithful `apt-get install -y <deb>`.
    """

    DEB_IN_CONTAINER = "/work/app.deb"

    def __init__(self, work: Path, container: str):
        self.work = work
        self.container = container
        self.deb_path = work / "app.deb"
        self.installed = False

    def start(self) -> None:
        self.work.mkdir(parents=True, exist_ok=True)
        # A long-lived container we docker-exec into; network is the default
        # bridge (NOT --network none) so apt can reach deb.debian.org.
        run([
            "docker", "run", "-d", "--name", self.container,
            "--platform", "linux/arm64",
            "--volume", f"{self.work}:/work:ro",
            TRIXIE_IMAGE, "sleep", "infinity",
        ], timeout=180, capture=False)
        self._exec("sh", "-ec", "apt-get update >/dev/null", timeout=300)

    def _exec(self, *args: str, timeout: int = 120) -> str:
        return run(["docker", "exec", self.container, *args], timeout=timeout, capture=True)

    def __call__(self, package: bytes, manifest: dict) -> None:
        # Land the fetched, sha256-verified bytes where the container sees them,
        # then run the SAME command appliance.provision.apt_install runs.
        self.deb_path.write_bytes(package)
        self.deb_path.chmod(0o644)
        self._exec(
            "sh", "-ec",
            f"DEBIAN_FRONTEND=noninteractive apt-get install -y {self.DEB_IN_CONTAINER}",
            timeout=900,
        )
        self.installed = True

    def assert_landed(self) -> dict:
        """Prove the real apt install landed the Player's first-party closure
        (staged under the distro `python3` `dist-packages`) and both shipped
        systemd units. `player/service.py` is the module the import smoke then
        loads; the units are what the (captured) start step would activate."""
        listing = self._exec(
            "sh", "-ec",
            "set -e; "
            "test -f /usr/lib/python3/dist-packages/player/service.py; "
            "test -f /usr/lib/python3/dist-packages/contracts/enrollment.py; "
            "test -f /etc/systemd/system/photo-wall-player.service; "
            "test -f /etc/systemd/system/photo-wall-weston.service; "
            "ls /etc/systemd/system/photo-wall-*.service",
        )
        units = sorted(Path(line).name for line in listing.split())
        require("photo-wall-player.service" in units, "install_player_unit_missing")
        require("photo-wall-weston.service" in units, "install_weston_unit_missing")
        return {"player_module": "usr/lib/python3/dist-packages/player/service.py",
                "units": units}

    def import_smoke(self) -> dict:
        """Python 3.13 import smoke inside the post-install container: prove the
        Player closure + the GTK/GStreamer/OpenGL bindings all import under
        trixie's Python 3.13 and its distro package versions."""
        output = self._exec("python3", "-c", IMPORT_SMOKE, timeout=120)
        try:
            result = json.loads(output.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise TracerError("import_smoke_unparsable") from None
        require(str(result.get("python", "")).startswith("3.13"),
                "import_smoke_not_python_313")
        require(str(result.get("player_service_file", "")).startswith(
            "/usr/lib/python3/dist-packages/player/"), "import_smoke_wrong_module_path")
        return result

    def stop(self) -> None:
        try:
            run(["docker", "rm", "-f", self.container], timeout=60, capture=False)
        except TracerError:
            pass


class Recorder:
    def __init__(self):
        self.starts = 0
        self.installs = 0

    def start_unit(self) -> None:
        self.starts += 1

    def refuse_install(self, package: bytes, manifest: dict) -> None:
        # Used only on the corruption path: reaching here is the failure.
        self.installs += 1
        raise TracerError("installed_corrupt_deb")


def parse_deb(deb: Path) -> tuple[str, str, int, bytes]:
    match = DEB_NAME.fullmatch(deb.name)
    require(match is not None, "player_deb_name_unexpected")
    payload = deb.read_bytes()
    return match.group("version"), hashlib.sha256(payload).hexdigest(), len(payload), payload


def stage_deb(app_root: Path, sha256: str, payload: bytes) -> Path:
    app_root.mkdir(parents=True, exist_ok=True)
    app_root.chmod(0o755)
    staged = app_root / f"app-{sha256}.deb"
    staged.write_bytes(payload)
    staged.chmod(0o644)
    return staged


def part_a_happy(origin: str, manifest: dict, install: TrixieInstaller,
                 recorder: Recorder, public_config: Path) -> dict:
    """The crux: the REAL Bootstrapper fetches + verifies + REAL-apt-installs
    the REAL `.deb`, hands off the origin, and invokes start. Every effect is
    real except the captured start step; then the Python 3.13 import smoke runs
    inside the post-install container."""
    bootstrapper = Bootstrapper(
        discovery=_FixedDiscovery(origin),
        fetch_manifest=lambda o: fetch_manifest(o),
        fetch_package=lambda o, m: fetch_package(o, m),
        install=install,
        write_origin=lambda o: write_public_config(o, path=public_config),
        start_unit=recorder.start_unit,
    )
    require(_run_bootstrapper(bootstrapper) is True, "bootstrapper_did_not_complete")
    require(install.installed is True, "install_seam_not_invoked")
    landed = install.assert_landed()
    smoke = install.import_smoke()
    require(recorder.starts == 1, "start_unit_not_invoked_once")
    written = json.loads(public_config.read_text())
    require(written.get("central_origin") == origin, "origin_handoff_missing")
    require(written.get("allow_http") is True, "http_opt_in_missing")
    return {"installed": landed, "import_smoke": smoke, "manifest": manifest,
            "central_origin": origin, "start_unit_invocations": recorder.starts}


def part_a_refusal(origin: str, staged: Path, size: int) -> dict:
    """Flip one served byte (same length, so central's size-match still serves
    it): the Bootstrapper's streamed sha256 must mismatch the manifest and it
    must REFUSE -- no install, bounded retry, no crash."""
    good = staged.read_bytes()
    corrupt = good[:-1] + bytes([good[-1] ^ 0x01])
    require(len(corrupt) == size, "corruption_changed_size")
    staged.write_bytes(corrupt)
    staged.chmod(0o644)
    recorder = Recorder()
    bootstrapper = Bootstrapper(
        discovery=_FixedDiscovery(origin),
        fetch_manifest=lambda o: fetch_manifest(o),
        fetch_package=lambda o, m: fetch_package(o, m),
        install=recorder.refuse_install,
        write_origin=lambda o: (_ for _ in ()).throw(TracerError("wrote_origin_on_refusal")),
        start_unit=lambda: (_ for _ in ()).throw(TracerError("started_on_refusal")),
        sleep=_NoSleep(),
    )
    require(_run_bootstrapper(bootstrapper, max_attempts=2) is False,
            "corrupt_deb_not_refused")
    require(recorder.installs == 0, "corrupt_deb_installed")
    staged.write_bytes(good)
    staged.chmod(0o644)
    return {"refused": True, "install_calls": recorder.installs}


class _FixedDiscovery:
    """Explicit injected origin -- the brief's "no mDNS/multicast" requirement.
    Matches player.discovery.CentralDiscovery's async protocol."""

    def __init__(self, origin: str):
        self.origin = origin

    async def discover(self) -> str:
        return self.origin


class _NoSleep:
    async def __call__(self, _seconds: float) -> None:
        return None


def _run_bootstrapper(bootstrapper: Bootstrapper, *, max_attempts: int = 1) -> bool:
    import asyncio

    return asyncio.run(bootstrapper.run(max_attempts=max_attempts))


def diagnostics(state: Path, central: Central | None) -> None:
    if central is None:
        return
    try:
        (state / "central.log").write_text(central.compose("logs", "--no-color", "central"))
        (state / "compose-ps.txt").write_text(central.compose("ps", "-a"))
    except TracerError:
        pass


def run_tracer(state: Path, central_image: str, deb: Path, port: int, keep: bool) -> dict:
    require(state.is_absolute(), "absolute_state_required")
    require(deb.is_file(), "player_deb_missing")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    project = "pw-netboot-" + secrets.token_hex(6)
    password = secrets.token_hex(24)
    admin_token = secrets.token_hex(32)
    app_root = state / "app-packages"
    compose_path = state / "compose.json"
    compose_path.write_text(json.dumps(compose_document(
        central_image=central_image, password=password,
        admin_token=admin_token, port=port, app_root=app_root,
    )))
    base = ["docker", "compose", "-p", project, "-f", str(compose_path)]
    origin = f"http://127.0.0.1:{port}"
    central = Central(base, origin, admin_token)
    installer = TrixieInstaller(state / "install", project + "-install")
    evidence: dict = {"schema": 1, "status": "running", "central_image": central_image,
                      "player_deb": deb.name, "phases": {}}

    def save() -> None:
        (state / "evidence.json").write_text(json.dumps(evidence, sort_keys=True, indent=2))

    save()
    try:
        version, sha256, size, payload = parse_deb(deb)
        evidence["player_deb_sha256"], evidence["player_deb_size"] = sha256, size
        staged = stage_deb(app_root, sha256, payload)
        central.compose("up", "-d", "database", "central", timeout=180, capture=False)
        central.wait_healthy()
        central.register_and_promote(version, sha256, size)
        manifest = central.manifest()
        require(manifest == {"version": version, "sha256": sha256, "size": size},
                "manifest_mismatch")
        evidence["phases"]["manifest"] = manifest
        save()

        installer.start()
        recorder = Recorder()
        evidence["phases"]["part_a_happy"] = part_a_happy(
            origin, manifest, installer, recorder, state / "public.json"
        )
        save()
        installer.stop()

        evidence["phases"]["part_a_refusal"] = part_a_refusal(origin, staged, size)
        save()

        evidence["status"] = "passed"
        save()
        return {"status": "passed", "state_dir": str(state),
                "installed": evidence["phases"]["part_a_happy"]["installed"],
                "import_smoke": evidence["phases"]["part_a_happy"]["import_smoke"]}
    except Exception as error:
        evidence["status"] = "failed"
        evidence["error"] = str(error) if isinstance(error, TracerError) else "tracer_failed"
        save()
        diagnostics(state, central)
        raise
    finally:
        installer.stop()
        if not keep:
            try:
                central.compose("down", "--volumes", "--remove-orphans",
                                timeout=120, capture=False)
            except TracerError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run",))
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--central-image", required=True)
    parser.add_argument("--deb", type=Path, required=True,
                        help="the REAL production Player .deb to serve and install")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    try:
        result = run_tracer(args.state_dir, args.central_image, args.deb, args.port, args.keep)
    except TracerError as error:
        print(json.dumps({"status": "failed", "error": str(error)}), file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
