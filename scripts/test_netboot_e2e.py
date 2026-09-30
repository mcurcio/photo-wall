"""p4-boot-chain s3, Part A -- process-level boot-time app-delivery tracer (Project 2 S1c).

Owner-chosen shape (2026-09-12): process-level, NOT a QEMU kernel boot, and
self-contained -- it builds the REAL bootstrapper and Player `.deb`s in-job,
with NO `release-artifacts` reusable workflow and NO pre-qualified OS-base.
This proves the 0009 *software* contract, on the device's own package set:

  the PACKAGED provisioner (the bootstrapper `.deb`, installed with dpkg) runs
  as its unit runs it (`umask 077; python3 -I -B
  /usr/lib/photo-wall-bootstrapper`) inside the DEVICE ROOT: the base's device
  set (`scripts/debian_packages.py`, the bootstrapper's and the Player's
  packages) built by mmdebstrap at the declared snapshot, with no package
  lists. Its command line names a gateway stub that 301s every request
  cross-host (by name) to a REAL central (uvicorn) + REAL Postgres: it locates
  Central through the gateway, fetches the app manifest and the REAL Player
  `.deb` from the located origin, verifies its sha256 (corruption check), and
  INSTALLS it with `dpkg --install` alone. `apt` and `apt-get` are shims that
  fail the run if called: every `Depends` must already be in the root, as it
  is in the base the Pi boots. It then writes the handoff (0644, no
  central_origin: the Player reads a command-line root itself) and starts the
  Player unit (a recording `systemctl` shim). When a served byte is flipped,
  the Bootstrapper REFUSES (no install).

What is REAL here vs captured
-----------------------------
- REAL F3/T0 seam: the exact installed bootstrapper closure runs one OS-agent
  check-in before any Player package lands, then a packaged one-attempt
  provisioner refuses corrupted package bytes and the agent checks in again.
  Central's v2 HTTP route, PostgreSQL rows and operator projection must retain
  the base claim with unknown app state and no acceptance/command authority.
  The synthetic serial/boot and one-shot calls do not qualify the systemd unit
  schedule, switch-root, Pi hardware or mixed Central pods.
- REAL: central + Postgres (docker compose, the production `central` image);
  the app-package HTTP surface (`/v1/locate`, `/v1/app/manifest`,
  `/v1/app/package/{sha256}.deb`, read through the asset cache); the packaged
  provisioner end to end -- its computed closure under the pinned python3, the
  real `uplink.finder.find_central` through a cross-host 301, `DirectFetch` to
  the located origin, the streamed sha256 check, `dpkg --install` of the REAL
  production `.deb` against the device set, the handoff written under
  `umask 077`; the Python-3.13 import smoke in the post-install root (the
  Player's first-party closure plus the GTK/GStreamer/OpenGL bindings import
  under the pinned distro versions); the corruption refusal (a flipped byte in
  the served `.deb`, driven in-process through the real Bootstrapper).
- SEEDED: the release catalog. There is no hand-uploaded `.deb` any more (the
  design's catalog learns packages from releases), and this tracer runs no
  worker and reaches no GitHub, so `release_seed_sql` writes what a release sync
  plus a finished `FetchPackage` would have: one release referencing the staged
  `.deb`, and its Asset record with the produced facts. The tracer then promotes
  it through the REAL operator route (`promote_path`).
- CAPTURED: systemd -- there is no PID 1 in the container, so `systemctl` is a
  shim that records its arguments; the tracer asserts the provisioner asked it
  to start the Player unit, after a landed install. Real GTK/HDMI pixels and a
  real kernel/initrd Pi boot are the owner's hardware bench step and are out of
  scope (0009); the import smoke catches 3.13/version import breakage in CI
  without rendering a pixel.

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
import contextlib
import functools
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import IO

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, "") and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from appliance.provision import DEFAULT_UNIT, AppManifest, Bootstrapper  # noqa: E402
from contracts.central_identity import LOCATE_PATH  # noqa: E402
from scripts.build_bootstrapper_deb import INSTALL_DIR as BOOTSTRAPPER_DIR  # noqa: E402
from scripts.build_player_deb import INSTALL_DIR as PLAYER_DIR  # noqa: E402
from scripts.debian_packages import DEVICE_CONSUMERS, mmdebstrap_argv  # noqa: E402
from scripts.demo_wall import POSTGRES_IMAGE  # noqa: E402
from scripts.uplink_device_harness import Stub, redirect_stub  # noqa: E402
from uplink.finder import find_central  # noqa: E402
from uplink.origin import Origin  # noqa: E402
from uplink.resolver import Configured  # noqa: E402
from uplink.transport import HttpTransport  # noqa: E402
from uplink.trust import Trust  # noqa: E402

# The device root (`device_root_image`): a cold build fetches the whole device set from the
# snapshot archive, which is slow; a warm run only re-imports the cached tar.
DEVICE_ROOT_SECONDS = 2400
IMPORT_SECONDS = 900
PROVISION_SECONDS = 300
# The provisioner exactly as appliance/systemd/photo-wall-provision.service runs it
# (UMask=0077, `python3 -I -B <BOOTSTRAPPER_DIR>`), from a command line the tracer writes.
CMDLINE = "/tmp/cmdline"
PROVISION = f"umask 077; python3 -I -B {BOOTSTRAPPER_DIR} --cmdline {CMDLINE}"
# Where the shims record their calls, inside the device root.
CALLS_DIR = "/var/log/photo-wall-e2e"


def _shim(name: str, calls: str, status: int) -> str:
    return f'#!/bin/sh\nprintf "%s\\n" "{name} $*" >> {CALLS_DIR}/{calls}\nexit {status}\n'


# Ahead of the real ones on PATH. apt resolving anything on the device is the failure this
# tracer exists to catch, so its shims fail; systemd has no PID 1 here, so its shim records.
SHIMS = {"apt": _shim("apt", "apt.calls", 99), "apt-get": _shim("apt-get", "apt.calls", 99),
         "systemctl": _shim("systemctl", "systemctl.calls", 0)}

# The synthetic release the tracer promotes. The manifest's `version` is the
# release tag (the catalog's label), not the `.deb`'s own version.
TRACER_TAG = "v0.0.1"
TRACER_DEB_URL = "https://example.invalid/photo-wall-player_all.deb"  # never fetched
PROBE_SERIAL = "10000000cafef00d"
PROBE_BOOT_ID = "11111111-2222-3333-4444-555555555555"
PROBE_STATE = "/run/photo-wall/packaged-probe"
_TAG_SHAPE = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_SHA256_SHAPE = re.compile(r"[0-9a-f]{64}")

# The new-model `.deb` is Architecture: all (only `.py` files + units; the
# native stack is its Depends, already in the base), so the filename ends in `_all.deb`.
DEB_NAME = re.compile(r"photo-wall-player_(?P<version>[A-Za-z0-9][A-Za-z0-9.+~-]*)_all\.deb")

# The bindings whose import closure must resolve on trixie's Python 3.13 + distro
# package versions. This is an import smoke (no display, no render) -- it catches
# 3.13/version import breakage at the pinned distro versions; real pixels remain
# the owner's Pi bench step (0009).
def import_smoke_script(player_dir: PurePosixPath, *, bindings: bool = True) -> str:
    """The script `import_smoke` runs as `python3 -I -B -c <script>` in the post-install root
    (and tests/test_netboot_e2e_wire.py runs directly against a staged fake closure): inserts
    `player_dir` at sys.path[0], json-loads `{player_dir}/closure.json` and
    `importlib.import_module`s EVERY module it lists -- proving the PRIVATE install directory
    (not dist-packages) is what resolves the whole computed closure. Then, as today,
    `gi.require_version`/`from gi.repository import Gtk, Gst`/`import OpenGL` (the render
    bindings) and `import player.service` (the module the import path check below reads).
    `bindings=False` (tests only: the dev venv carries no gi/OpenGL) leaves out the render
    bindings; production always runs with `bindings=True`."""
    bindings_block = (
        "import gi\n"
        "gi.require_version('Gtk', '3.0')\n"
        "gi.require_version('Gst', '1.0')\n"
        "from gi.repository import Gtk, Gst\n"
        "import OpenGL\n"
    ) if bindings else ""
    versions = ("'gtk': Gtk._version, 'gst': Gst._version" if bindings
               else "'gtk': None, 'gst': None")
    return (
        "import importlib, json, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(player_dir)!r})\n"
        f"closure = json.loads((Path({str(player_dir)!r}) / 'closure.json').read_text())\n"
        "for name in closure['modules']:\n"
        "    importlib.import_module(name)\n"
        f"{bindings_block}"
        "import player.service\n"
        "print(json.dumps({'python': sys.version.split()[0], "
        "'player_service_file': player.service.__file__, "
        f"'modules': len(closure['modules']), {versions}}}))\n"
    )


class TracerError(RuntimeError):
    """A fixed, safe diagnostic code -- never untrusted command/HTTP output."""


def require(condition: object, code: str) -> None:
    if not condition:
        raise TracerError(code)


def promote_path(tag: str) -> str:
    """The operator promote route: the seed's promotion goes through it, so the policy row is
    written by the catalog's own writer (`PgReleaseRecords.set_promoted`), never by SQL here."""
    return f"/v1/operator/app/releases/{tag}/promote"


def release_seed_sql(tag: str, sha256: str, size: int, url: str = TRACER_DEB_URL) -> str:
    """One release whose `.deb` is already produced (flow (b): served from disk).

    It is the state a release sync plus a finished `FetchPackage` leave behind: the
    `app_releases` row with the package locator, and the `player-deb` Asset with its
    produced facts and its reference. The caller then promotes the tag through
    `promote_path`. The values are validated here, so the SQL text carries no untrusted
    input.
    """
    match = _TAG_SHAPE.fullmatch(tag)
    require(match is not None, "seed_tag_invalid")
    require(_SHA256_SHAPE.fullmatch(sha256) is not None, "seed_sha256_invalid")
    require(type(size) is int and size > 0, "seed_size_invalid")
    require(re.fullmatch(r"https://[A-Za-z0-9./_-]{1,256}", url) is not None, "seed_url_invalid")
    major, minor, patch = match.groups()
    now = "EXTRACT(EPOCH FROM now())"
    return (
        "BEGIN;\n"
        "INSERT INTO app_releases(tag, major, minor, patch, prerelease, is_prerelease, "
        "asset_sha256, asset_size, asset_url, mirror_state, discovered_at, updated_at) "
        f"VALUES ('{tag}', {major}, {minor}, {patch}, '', FALSE, '{sha256}', {size}, '{url}', "
        f"'discovered', {now}, {now});\n"
        "INSERT INTO assets(kind, identity, produced_size, produced_sha256, created_at) "
        f"VALUES ('player-deb', '{sha256}', {size}, '{sha256}', {now});\n"
        "INSERT INTO asset_references(kind, identity, owner, locator_url, locator_sha256, "
        "locator_size, expected_size, expected_sha256, added_at) "
        f"VALUES ('player-deb', '{sha256}', '{tag}', '{url}', '{sha256}', {size}, {size}, "
        f"'{sha256}', {now});\n"
        "COMMIT;\n"
    )


def _text(output: str | bytes | None) -> str:
    return output.decode(errors="replace") if isinstance(output, bytes) else (output or "")


def run(args: list[str], *, timeout: int = 120, capture: bool = True,
        stdin: IO[bytes] | None = None, stdout: IO[bytes] | None = None,
        log: Path | None = None) -> str:
    """Run `args`; a non-zero exit (or a timeout) echoes the command's WHOLE captured output to
    our stderr, so the CI log shows the real cause, then raises a TracerError carrying its last
    line. `stdin` feeds it a file; `stdout` sends its output to a file instead of capturing it;
    `log` also keeps its whole stderr in that file, whatever the outcome (a workflow artifact)."""
    piped = subprocess.PIPE if capture else None
    try:
        result = subprocess.run(args, stdin=stdin, stdout=piped if stdout is None else stdout,
                                stderr=piped, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        _keep(args, _text(error.stderr), _text(error.stdout), log=log, failed=capture)
        raise TracerError(f"command_failed:{args[0]}") from error
    except (OSError, subprocess.SubprocessError) as error:
        raise TracerError(f"command_failed:{args[0]}") from error
    failed = result.returncode != 0
    _keep(args, result.stderr or "", result.stdout or "", log=log, failed=failed and capture)
    if failed:
        detail = (result.stderr or result.stdout or "").strip().splitlines()[-1:] if capture else []
        raise TracerError(f"command_failed:{args[0]}:{''.join(detail)[:200]}")
    return (result.stdout or "") if capture else ""


def _keep(args: list[str], stderr: str, stdout: str, *, log: Path | None, failed: bool) -> None:
    """`stderr` into `log` (when given); on a failure, the whole output onto our stderr."""
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(stderr)
    if failed:
        output = (stderr or stdout).rstrip("\n")
        print(f"--- {args[0]} failed; its whole output follows ---\n{output}\n"
              f"--- end of {args[0]} output ---", file=sys.stderr, flush=True)


def device_root_image(*, arch: str, tag: str, cache: Path, log: Path | None = None) -> str:
    """docker image name for the device root: mmdebstrap_argv(arch=arch, target="-",
    consumers=DEVICE_CONSUMERS) piped to `docker import - <tag>`; the tar is kept under `cache`
    (actions/cache key = hashFiles('scripts/debian_packages.py')) and re-imported on a hit.

    The tar's name carries a digest of the exact mmdebstrap command, so a changed declaration
    never re-imports a stale root, whatever the cache key. mmdebstrap runs as root (its root
    mode; unprivileged user namespaces are restricted on the runner); a failed build leaves no
    tar behind, and its whole stderr on ours (and in `log`, when given)."""
    argv = mmdebstrap_argv(arch=arch, target="-", consumers=DEVICE_CONSUMERS)
    digest = hashlib.sha256("\n".join(argv).encode()).hexdigest()[:16]
    tar = cache / f"device-root-{arch}-{digest}.tar"
    if not tar.is_file():
        cache.mkdir(parents=True, exist_ok=True)
        partial = tar.with_name(tar.name + ".partial")
        as_root = () if os.geteuid() == 0 else ("sudo", "-n")
        try:
            with partial.open("wb") as out:
                run([*as_root, *argv], stdout=out, timeout=DEVICE_ROOT_SECONDS, log=log)
            partial.replace(tar)
        finally:
            partial.unlink(missing_ok=True)
    with tar.open("rb") as stream:
        run(["docker", "import", "-", tag], stdin=stream, timeout=IMPORT_SECONDS)
    return tag


def gateway_stub(central_port: int) -> contextlib.AbstractContextManager[Stub]:
    """The command line's root: a stand-in gateway on the runner's loopback that 301s every
    request, same path, to Central under another host NAME (`localhost`) -- the Pi's
    cross-host case, in plain http (TLS hops are the device harness's rows)."""
    return redirect_stub(f"http://localhost:{central_port}", keep_path=True)


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
                    # 0013: one cache root; central derives apps/ from it and
                    # serves the staged `.deb` mounted at that subpath.
                    "PHOTO_WALL_CACHE_ROOT": "/var/cache/photo-wall",
                    "PHOTO_WALL_MDNS_ADVERTISE": "false",
                    "PHOTO_WALL_HORIZON_SECONDS": "15",
                },
                "volumes": [f"{app_root}:/var/cache/photo-wall/apps:ro"],
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

    def seed_promoted_release(self, tag: str, sha256: str, size: int) -> None:
        # After central is healthy, so its migrations (the assets tables) have run.
        self.compose("exec", "-T", "database", "psql", "-v", "ON_ERROR_STOP=1",
                     "-U", "wall", "-d", "wall", "-c", release_seed_sql(tag, sha256, size))
        self._request("POST", promote_path(tag), authenticated=True)

    def manifest(self) -> dict:
        return self._request("GET", "/v1/app/manifest")

    def fleet(self) -> dict:
        return self._request("GET", "/v1/operator/fleet", authenticated=True)

    def os_rows(self) -> list[dict]:
        """Read committed T0 rows from the same PostgreSQL the HTTP pod writes."""
        query = (
            "SELECT COALESCE(json_agg(json_build_object("
            "'schema',o.observation_schema,'sequence',o.observation_sequence,"
            "'boot_id',o.kernel_boot_id::text,'phase',o.phase,'fault',o.fault_code,"
            "'attempted',o.attempted_app_sha256,"
            "'installed',o.app_installed_sha256,'running',o.app_running_sha256,"
            "'installed_reason',o.app_installed_reason,"
            "'running_reason',o.app_running_reason) "
            "ORDER BY o.observation_sequence),'[]'::json) "
            "FROM fleet_os_observations o JOIN devices d USING(device_id) "
            f"WHERE d.serial='{PROBE_SERIAL}'"
        )
        output = self.compose("exec", "-T", "database", "psql", "-At", "-v",
                              "ON_ERROR_STOP=1", "-U", "wall", "-d", "wall", "-c", query)
        return json.loads(output.strip())

    def accepted_count(self) -> int:
        query = ("SELECT count(*) FROM fleet_accepted_artifacts a "
                 "JOIN devices d USING(device_id) "
                 f"WHERE d.serial='{PROBE_SERIAL}'")
        output = self.compose("exec", "-T", "database", "psql", "-At", "-v",
                              "ON_ERROR_STOP=1", "-U", "wall", "-d", "wall", "-c", query)
        return int(output.strip())

    def log(self) -> str:
        return self.compose("logs", "--no-color", "central")


class DeviceRoot:
    """A long-lived container from the device root image (`device_root_image`): the base's
    device set at the pin and no package lists, on the host network, so the gateway stub and
    Central on the runner's loopback are reachable as they are from a Pi on the LAN. The
    bootstrapper `.deb` is installed with dpkg alone (its Depends are already in the root), then
    run as its unit runs it, with the SHIMS ahead of the real tools on PATH."""

    WORK = "/e2e"          # the host work dir, read-only: the bootstrapper .deb, the shims
                           # and the staged root-check tools
    PATH = f"{WORK}/shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

    def __init__(self, image: str, work: Path, container: str):
        self.image = image
        self.work = work
        self.container = container

    def start(self, bootstrapper_deb: Path) -> None:
        shims = self.work / "shims"
        shims.mkdir(parents=True, exist_ok=True)
        for name, script in SHIMS.items():
            (shims / name).write_text(script)
            (shims / name).chmod(0o755)
        # The post-install root-check tool and the declaration it reads (`from scripts import
        # debian_packages` resolves as a namespace package under -I; no __init__.py needed),
        # staged read-only under WORK so root_checks() can run them from the container.
        tools = self.work / "tools" / "scripts"
        tools.mkdir(parents=True, exist_ok=True)
        for name in ("device_root_checks.py", "debian_packages.py",
                     "packaged_os_agent_probe.py"):
            target = tools / name
            target.write_bytes((ROOT / "scripts" / name).read_bytes())
            target.chmod(0o644)
        (self.work / "bootstrapper.deb").write_bytes(bootstrapper_deb.read_bytes())
        (self.work / "bootstrapper.deb").chmod(0o644)
        run([
            "docker", "run", "-d", "--name", self.container,
            "--platform", "linux/arm64", "--network", "host",
            "--volume", f"{self.work}:{self.WORK}:ro",
            self.image, "sleep", "infinity",
        ], timeout=180, capture=False)
        self._exec("sh", "-ec", f"cd {self.WORK}; dpkg --install ./bootstrapper.deb; "
                                f"mkdir -p {CALLS_DIR}", timeout=300)
        lists = self._exec("sh", "-ec", "test ! -d /var/lib/apt/lists || "
                                        "find /var/lib/apt/lists -name '*_Packages*'")
        require(lists.strip() == "", "device_root_has_package_lists")

    def _exec(self, *args: str, timeout: int = 120) -> str:
        return run(["docker", "exec", self.container, *args], timeout=timeout, capture=True)

    def _calls(self, name: str) -> list[str]:
        return self._exec("sh", "-ec", f"cat {CALLS_DIR}/{name} 2>/dev/null || true").splitlines()

    def packaged_probe(self, mode: str, origin: str, *, log: Path) -> dict:
        require(mode in ("report", "refuse-package"), "probe_mode_invalid")
        try:
            output = run([
                "docker", "exec", self.container,
                "python3", "-I", "-B", f"{self.WORK}/tools/scripts/packaged_os_agent_probe.py",
                mode, "--root", origin, "--serial", PROBE_SERIAL,
                "--boot-id", PROBE_BOOT_ID, "--state", PROBE_STATE],
                timeout=240 if mode == "refuse-package" else 60, log=log)
        except TracerError as error:
            if not log.is_file() or not log.stat().st_size:
                log.write_text(f"{error}\n")
            raise
        log.write_text(output)
        try:
            result = json.loads(output.strip().splitlines()[-1])
        except (ValueError, IndexError) as error:
            raise TracerError("packaged_probe_output_invalid") from error
        require(result.get("mode") == mode, "packaged_probe_mode_mismatch")
        return result

    def assert_player_absent(self) -> None:
        self._exec("sh", "-ec", "! dpkg-query -W -f='${Status}' photo-wall-player "
                   "2>/dev/null | grep -q 'install ok installed'")
        require(self._calls("systemctl.calls") == [], "player_started_before_package")

    def provision(self, root: str, *, log: Path) -> None:
        """Run the packaged provisioner once, bounded, from a command line naming `root`. Its
        output is kept in `log` whatever happens (it retries forever on a network failure, so
        a timeout is how a failing run ends)."""
        self._exec("sh", "-ec", f'printf "%s\\n" "$1" > {CMDLINE}', "sh",
                   f"photowall.central={root}")
        argv = ["docker", "exec", "--env", f"PATH={self.PATH}", self.container,
                "sh", "-c", PROVISION]
        try:
            result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=PROVISION_SECONDS, check=False)
        except subprocess.TimeoutExpired as expired:
            log.write_bytes(expired.stdout or b"")
            raise TracerError("provision_timeout") from None
        except (OSError, subprocess.SubprocessError) as error:
            raise TracerError("command_failed:docker") from error
        log.write_bytes(result.stdout)
        require(result.returncode == 0, f"provision_exit_{result.returncode}")

    def assert_provisioned(self) -> dict:
        """Both packages are installed by dpkg on this one root, apt was never called, the
        unit start was asked for, and the handoff is readable by the `wall` user whatever the
        unit's umask: a 0755 directory, a 0644 file naming no Central (a command-line root is
        read by the Player itself; allow_http is never written)."""
        status = self._exec("dpkg", "-s", "photo-wall-player").splitlines()
        require("Status: install ok installed" in status, "player_not_installed")
        bootstrapper_status = self._exec("dpkg", "-s", "photo-wall-bootstrapper").splitlines()
        require("Status: install ok installed" in bootstrapper_status,
                "bootstrapper_not_installed")
        require(self._calls("apt.calls") == [], "apt_called_on_the_device")
        systemctl = self._calls("systemctl.calls")
        require(f"systemctl start {DEFAULT_UNIT}" in systemctl, "player_unit_not_started")
        modes = self._exec("stat", "-c", "%a", "/etc/photo-wall",
                           "/etc/photo-wall/public.json").split()
        require(modes == ["755", "644"], "handoff_modes_unexpected")
        handoff = json.loads(self._exec("cat", "/etc/photo-wall/public.json"))
        require(handoff == {"schema": 1}, "handoff_unexpected")
        return {"dpkg_status": "install ok installed",
                "bootstrapper_dpkg_status": "install ok installed", "systemctl": systemctl,
                "handoff": handoff, "handoff_modes": modes}

    def assert_landed(self) -> dict:
        """Prove dpkg landed the Player's closure PRIVATELY under {PLAYER_DIR} (design §2.7,
        rule 2) -- the generated `__main__.py`, `closure.json` and `player/service.py` (the
        module the import smoke then loads) -- and NOTHING first-party under the shared
        `/usr/lib/python3/dist-packages`, plus both shipped systemd units."""
        try:
            self._exec(
                "sh", "-ec",
                f"test -f {PLAYER_DIR}/__main__.py; "
                f"test -f {PLAYER_DIR}/closure.json; "
                f"test -f {PLAYER_DIR}/player/service.py",
            )
        except TracerError:
            raise TracerError("install_private_dir_missing") from None
        try:
            self._exec(
                "sh", "-ec",
                "; ".join(f"test ! -e /usr/lib/python3/dist-packages/{name}"
                         for name in ("player", "contracts", "uplink", "appliance")),
            )
        except TracerError:
            raise TracerError("install_leaked_to_dist_packages") from None
        listing = self._exec(
            "sh", "-ec",
            "test -f /etc/systemd/system/photo-wall-player.service; "
            "test -f /etc/systemd/system/photo-wall-weston.service; "
            "ls /etc/systemd/system/photo-wall-*.service",
        )
        units = sorted(Path(line).name for line in listing.split())
        require("photo-wall-player.service" in units, "install_player_unit_missing")
        require("photo-wall-weston.service" in units, "install_weston_unit_missing")
        return {"player_dir": str(PLAYER_DIR), "units": units}

    def import_smoke(self) -> dict:
        """Python 3.13 import smoke inside the post-install root: prove every module of the
        Player's closure.json imports from the PRIVATE {PLAYER_DIR}, then the GTK/GStreamer/
        OpenGL bindings, under the pinned trixie Python 3.13 and distro package versions."""
        output = self._exec("python3", "-I", "-B", "-c", import_smoke_script(PLAYER_DIR),
                            timeout=120)
        try:
            result = json.loads(output.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise TracerError("import_smoke_unparsable") from None
        require(str(result.get("python", "")).startswith("3.13"),
                "import_smoke_not_python_313")
        require(str(result.get("player_service_file", "")).startswith(f"{PLAYER_DIR}/player/"),
                "import_smoke_wrong_module_path")
        require(int(result.get("modules", 0)) > 0, "import_smoke_no_modules")
        return result

    def root_checks(self) -> dict:
        """scripts/device_root_checks.py (staged read-only under WORK by `start`) over this
        post-install root: the watchdog-override, time-daemon and resolver-writer checks only
        -- no `--require-installed` (needs the built .debs' own Depends) and no
        `--pinned-sources` (needs the base's apt sources), both out of this tracer's scope."""
        try:
            output = self._exec("python3", "-I", "-B",
                                "/e2e/tools/scripts/device_root_checks.py", "--root", "/")
        except TracerError:
            raise TracerError("device_root_checks_failed") from None
        return {"output": output.strip()}

    def stop(self) -> None:
        try:
            run(["docker", "rm", "-f", self.container], timeout=60, capture=False)
        except TracerError:
            pass


class Recorder:
    def __init__(self):
        self.installs = 0

    def refuse_install(self, package: bytes, manifest: AppManifest) -> None:
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


def _bootstrapper(origin: str, **effects) -> Bootstrapper:
    """The REAL Bootstrapper as appliance.provision.main wires it when the
    kernel command line names `origin`: the real find_central (locate, no
    mDNS), the real transport, DirectFetch to the located origin. No clock
    record exists on the runner."""
    transport = HttpTransport(trust=Trust.public())
    find = functools.partial(find_central, Configured(Origin.parse_root(origin)),
                             transport=transport)
    return Bootstrapper(find=find, transport=transport, clock=None, **effects)


def part_a_happy(central: Central, device: DeviceRoot, central_port: int, log: Path) -> dict:
    """The crux, as the Pi runs it: the packaged provisioner in the device root, from a
    command line naming the gateway stub, locates Central through its cross-host 301, fetches
    + verifies the REAL `.deb` from the located origin, installs it with dpkg alone, writes the
    handoff and starts the unit (the shim). Then the Python 3.13 import smoke and the
    post-install root checks run in the post-install root."""
    with gateway_stub(central_port) as gateway:
        device.provision(f"http://127.0.0.1:{gateway.port}/", log=log)
    # Only the locate went through the gateway (the one request that follows a redirect);
    # every fetch went straight to the located origin, and Central answered that locate.
    require(gateway.requests == [LOCATE_PATH], "gateway_saw_more_than_locate")
    require(f'"GET {LOCATE_PATH} ' in central.log(), "central_saw_no_locate")
    provisioned = device.assert_provisioned()
    landed = device.assert_landed()
    smoke = device.import_smoke()
    root_checks = device.root_checks()
    return {"provisioned": provisioned, "installed": landed, "import_smoke": smoke,
            "root_checks": root_checks, "gateway_requests": gateway.requests}


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
    bootstrapper = _bootstrapper(
        origin,
        install=recorder.refuse_install,
        write_handoff=lambda _: (_ for _ in ()).throw(TracerError("wrote_handoff_on_refusal")),
        start_unit=lambda: (_ for _ in ()).throw(TracerError("started_on_refusal")),
        sleep=_NoSleep(),
    )
    require(_run_bootstrapper(bootstrapper, max_attempts=2) is False,
            "corrupt_deb_not_refused")
    require(recorder.installs == 0, "corrupt_deb_installed")
    staged.write_bytes(good)
    staged.chmod(0o644)
    return {"refused": True, "install_calls": recorder.installs}


def os_claim_evidence(central: Central, *, sequence: int, phase: str,
                      fault: str | None) -> dict:
    """Require one more committed v2 row and an observational status projection."""
    rows = central.os_rows()
    require(len(rows) == sequence, "os_check_in_row_count")
    require([row["sequence"] for row in rows] == list(range(1, sequence + 1)),
            "os_check_in_sequence")
    require(all(row["schema"] == 2 and row["boot_id"] == PROBE_BOOT_ID
                and row["installed"] is None and row["running"] is None
                and row["installed_reason"] and row["running_reason"]
                for row in rows), "os_check_in_app_claim_invalid")
    require(rows[-1]["phase"] == phase and rows[-1]["fault"] == fault,
            "os_check_in_phase_invalid")
    require(central.accepted_count() == 0, "os_check_in_created_acceptance")
    status = central.fleet()
    devices = [device for device in status["devices"] if device["serial"] == PROBE_SERIAL]
    require(len(devices) == 1, "os_check_in_device_missing")
    device = devices[0]
    base, installed, running = (device[key] for key in ("base", "installed", "running"))
    require(base["source"] == "serial_claim" and base["assurance"] == "t0_unverified"
            and base["boot_id"] == PROBE_BOOT_ID and base["phase"] == phase
            and base["fault_code"] == fault, "os_check_in_base_projection")
    require(all(item["state"] == "unknown" and item["source"] == "serial_claim"
                and item["assurance"] == "t0_unverified"
                and item["boot_id"] == PROBE_BOOT_ID and item["digest"] is None
                for item in (installed, running)), "os_check_in_app_projection")
    require(device["accepted_fallback"] is None and
            device["update_now"]["available"] is False and
            status["commands_available"] is False, "os_check_in_promoted_authority")
    return {"rows": rows, "base": base, "installed": installed, "running": running,
            "commands_available": status["commands_available"]}


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
        (state / "central.log").write_text(central.log())
        (state / "compose-ps.txt").write_text(central.compose("ps", "-a"))
    except TracerError:
        pass


def run_tracer(state: Path, *, central_image: str, device_root: str, bootstrapper_deb: Path,
               deb: Path, port: int, keep: bool) -> dict:
    require(state.is_absolute(), "absolute_state_required")
    require(deb.is_file(), "player_deb_missing")
    require(bootstrapper_deb.is_file(), "bootstrapper_deb_missing")
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
    device = DeviceRoot(device_root, state / "device", project + "-device")
    evidence: dict = {"schema": 1, "status": "running", "central_image": central_image,
                      "device_root": device_root, "bootstrapper_deb": bootstrapper_deb.name,
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
        evidence["player_deb_version"] = version
        central.seed_promoted_release(TRACER_TAG, sha256, size)
        manifest = central.manifest()
        require(manifest == {"version": TRACER_TAG, "sha256": sha256, "size": size},
                "manifest_mismatch")
        evidence["phases"]["manifest"] = manifest
        save()

        device.start(bootstrapper_deb)
        device.assert_player_absent()
        first = device.packaged_probe("report", origin, log=state / "os-agent-before.log")
        require(first == {"mode": "report", "accepted": True, "sequence": 1,
                          "phase": "base_ready"}, "os_first_receipt_invalid")
        evidence["phases"]["os_absent"] = os_claim_evidence(
            central, sequence=1, phase="base_ready", fault=None)
        save()

        good = staged.read_bytes()
        corrupt = good[:-1] + bytes([good[-1] ^ 0x01])
        require(len(corrupt) == size, "corruption_changed_size")
        try:
            staged.write_bytes(corrupt)
            staged.chmod(0o644)
            refused = device.packaged_probe("refuse-package", origin,
                                            log=state / "packaged-refusal.log")
            require(refused == {"mode": "refuse-package", "refused": True,
                                "phase": "retry_wait", "fault": "app_integrity",
                                "attempted_sha256": sha256},
                    "packaged_refusal_invalid")
            device.assert_player_absent()
            second = device.packaged_probe("report", origin,
                                           log=state / "os-agent-after.log")
            require(second == {"mode": "report", "accepted": True, "sequence": 2,
                               "phase": "retry_wait"}, "os_second_receipt_invalid")
            evidence["phases"]["os_after_package_failure"] = os_claim_evidence(
                central, sequence=2, phase="retry_wait", fault="app_integrity")
            evidence["phases"]["packaged_refusal"] = refused
            save()
        finally:
            staged.write_bytes(good)
            staged.chmod(0o644)

        evidence["phases"]["part_a_happy"] = part_a_happy(
            central, device, port, state / "provision.log"
        )
        save()
        device.stop()

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
        device.stop()
        if not keep:
            try:
                central.compose("down", "--volumes", "--remove-orphans",
                                timeout=120, capture=False)
            except TracerError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    root = commands.add_parser("device-root",
                               help="build (or re-import) the device root image; print its name")
    root.add_argument("--arch", default="arm64")
    root.add_argument("--tag", required=True)
    root.add_argument("--cache", type=Path, required=True,
                      help="where the root's tar is kept between runs (actions/cache)")
    root.add_argument("--log", type=Path,
                      help="where mmdebstrap's whole stderr is kept when it runs")
    tracer = commands.add_parser("run", help="run the tracer")
    tracer.add_argument("--state-dir", type=Path, required=True)
    tracer.add_argument("--central-image", required=True)
    tracer.add_argument("--device-root", required=True,
                        help="the device root image `device-root` printed")
    tracer.add_argument("--bootstrapper-deb", type=Path, required=True,
                        help="the REAL bootstrapper .deb, installed into the device root")
    tracer.add_argument("--deb", type=Path, required=True,
                        help="the REAL production Player .deb to serve and install")
    tracer.add_argument("--port", type=int, default=18080)
    tracer.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "device-root":
            print(device_root_image(arch=args.arch, tag=args.tag, cache=args.cache,
                                    log=args.log))
            return
        result = run_tracer(args.state_dir, central_image=args.central_image,
                            device_root=args.device_root,
                            bootstrapper_deb=args.bootstrapper_deb, deb=args.deb,
                            port=args.port, keep=args.keep)
    except TracerError as error:
        print(json.dumps({"status": "failed", "error": str(error)}), file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
