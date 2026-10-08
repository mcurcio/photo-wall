# Run brief: wave 3, lane E3c (the Node bus as a base unit)

> **Where this file goes.** Its home is `.claude/runs/wave3-e3c.md` in the e3c worktree (`/Volumes/Dock/Home/Code/photo-wall/.claude/worktrees/wave3-e3c`, branch `claude/wave3-e3c`). The code architect's session could not write into that worktree (a worktree-isolation hook) and may not commit, so it is staged at `/Volumes/Dock/tmp/node-redesign/e3c-lane/`. **First action of the S1 implementer (or the orchestrator), in the e3c worktree:** copy this file to `.claude/runs/wave3-e3c.md`, append `/Volumes/Dock/tmp/node-redesign/e3c-lane/errata-e3c-cut.md` to `.claude/errata.md` (`cat … >> .claude/errata.md`), commit both alone as `docs(e3c): lane brief and cut errata` with the trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, push `claude/wave3-e3c`, then start S1.

**Design:** `/Volumes/Dock/tmp/node-redesign/e3b-design.md`, revision 3 (approved; owner answers Q1–Q3 in `/Volumes/Dock/tmp/node-redesign/owner-answers.md`). E3c's rows: §7.2 "Bus unit (E3c)", §12 changes 1 and 10, §13, §15 ("Bus unit and prepare helper, pinned binary in the base and the node-pid1 fixture, the base deb, nats-py at runtime, `memory_peak:bus`, fence job re-run with kill -9 → empty store and small messages"; "the bus memory line equals `NODE_BUS_MEMORY_MAX`", E2a). An implementer reads **its slice page below plus the design sections it names**, nothing else.
**Base:** E3b landed on PR 49 (`claude/wave1-node-api` at 3e1baf6): `nodeapi` session, NodeLink and WallWriter as a library; run brief `.claude/runs/wave2-e3b.md`; errata `grep -a 'E-E3B' .claude/errata.md` (FR1–FR4 included).
**Branch:** `claude/wave3-e3c`, checkout `/Volumes/Dock/Home/Code/photo-wall/.claude/worktrees/wave3-e3c`. One bead per slice, landed in order. **Rules:** `~/.claude/skills/implementation-workflow/SKILL.md` (binding).
**Scope:** the bus systemd unit and its environment (leaf URL from the boot origin and the serial, server name, port, `GOMEMLIMIT`), `MemoryMax` from `contracts`, `Restart=always`, a private TMPDIR (E-E3B-S3-1), nothing waits on it; the pinned nats-server in the base (`node-base.deb`, which the squashfs and the node-pid1 image both install); `nodeapi`, `contracts` and nats-py in the base; the bus memory line in `appliance/kernel/capacity.py` equal to the unit's cap (E2a); `memory_peak:bus` and `oom_kill:bus`; the fence job's smallest-event and kill -9 legs on linux-arm64.
**Packages touched:** `appliance/`, `scripts/` (base build), `contracts/` (additive constants only: S1; the fit numbers only under S2's pre-decided rule; metric family counts: S5), `tests/`, `.github/workflows/checks.yml` (S2). **Never** `central/` or `nodeapi/`.
**Not in scope:** the hub runner, its ingress route and fixture hub, NodeLink supervisors and DB tables (E3d); the join (E3e); component documents (E5); apps and the Player on `nodeapi` (E6, E8); E-E3B-FR2/FR3 fixes (they live in `nodeapi`). Docs run after integration as their own bead (not a slice of this lane).

## Owner rules (binding)

- The Node is self-sufficient; Central is a client. UX over security: no security mechanisms (the bus unit carries no sandbox lines beyond the functional ones on its page).
- A finding is a defect only if it happens in normal operation or the Node cannot recover by itself. RAM is not a limiting factor.
- Integration tests of functional requirements with real servers; no unit tests of internals (a config test only where a table, unit file or configuration is the thing under test).
- Make failure classes impossible rather than patching instances. Right-size; DRY/SOLID.
- Deploys only through an iac PR the owner merges: never apply to a cluster. Deployment changes are written down (below), not applied.
- **Report where the spec or plan is wrong:** append to `.claude/errata.md` (append-only; read with `grep -a`) with ids `E-E3C-S<n>-<k>`, then STOP if the frozen page cannot be met. A page changes only through an erratum and a re-cut.

## Environment

- `source ~/.nvm/nvm.sh && nvm use 20` before any test run. Console tests also need `cd central/console && npm ci` once in this worktree (preflight below: 65 console failures without it).
- `/Volumes/Dock/Home/Code/photo-wall/.claude/worktrees/wave3-e3c/.venv/bin/python` only; never another worktree's, never `uv run`. **`uv.lock` and `[project].dependencies` stay unchanged in every slice** (revert `uv.lock` if anything rewrites it).
- `PHOTO_WALL_NATS_SERVER=/Volumes/Dock/tmp/nats/nats-server-v2.15.0-darwin-arm64/nats-server`; fence: Docker plus `PHOTO_WALL_BUS_FENCE_SERVER=/Volumes/Dock/tmp/nats/linux/nats-server-v2.15.0-linux-arm64/nats-server`.
- pytest `--basetemp=/Volumes/Dock/tmp/w3bt/<slice>/<gate>` (short paths: AF_UNIX sockets) and `TMPDIR=/Volumes/Dock/tmp/w3bt/<slice>`; `-n 4` where the gate allows it.
- New bus test files are named `tests/integration/test_node_bus_*.py`, so CI's `node-bus` job (`-k node_bus`) and the unit tier's allowlisted skip pick them up with no CI edit.
- Probes restore by `cp` of a backup, never `git checkout`/`stash`/`reset`. Commit WIP before a probe.
- **node-pid1 locally** (arm64 Docker, privileged): commit first (the builders read a committed revision), then, from the checkout:
  ```
  export TMPDIR=/Volumes/Dock/tmp UV_CACHE_DIR=/Volumes/Dock/tmp/uv-cache DOCKER_CONTEXT=default
  REV=$(git rev-parse HEAD); OUT=/Volumes/Dock/tmp/pw-node/$REV; EPOCH=$(.venv/bin/python scripts/debian_packages.py epoch)
  SOURCE_DATE_EPOCH=$EPOCH PYTHONPATH=$PWD .venv/bin/python /Volumes/Dock/tmp/pw-node/build_components_macos.py --repository "$PWD" --revision "$REV" --output $OUT/components
  SOURCE_DATE_EPOCH=$EPOCH .venv/bin/python -m scripts.build_node_pid1_fixture --components $OUT/components --output $OUT/fixture
  PHOTO_WALL_NODE_PID1_FIXTURE=$OUT/fixture PHOTO_WALL_TEST_REQUIRE_NODE_PID1=1 .venv/bin/python scripts/test_local.py -q -m node_pid1 -k "<leg>" --basetemp /Volumes/Dock/tmp/w3bt/<slice>/pid1 tests/test_node_pid1.py
  ```
  (`build_components_macos.py` only swaps `run_dpkg_deb` for a container run and calls `scripts.build_node_components.main`, so S1's `stage_package` flows through it.)

### Gates

| Gate | Command | Slices |
|---|---|---|
| G-static | `.venv/bin/python -m ruff check .` · `.venv/bin/lint-imports` · `python3 scripts/check_docs.py` | all |
| G-unit | `PHOTO_WALL_TEST_REQUIRE_DATABASE=1 .venv/bin/python -m pytest -q -m "not db and not browser" -n 4 --dist worksteal --basetemp=/Volumes/Dock/tmp/w3bt/<slice>/unit` | all; baseline failures `/Volumes/Dock/tmp/w1/baseline.txt` (2, environmental) |
| G-bus | `PHOTO_WALL_NATS_SERVER=… .venv/bin/python -m pytest -q tests/integration -k node_bus -n 4 --basetemp=/Volumes/Dock/tmp/w3bt/<slice>/bus` | all |
| G-fence | `PHOTO_WALL_BUS_FENCE_SERVER=… PHOTO_WALL_BUS_FENCE_MINUTES=1 PHOTO_WALL_BUS_FENCE_STALL_SECONDS=20 .venv/bin/python -m pytest -q -s tests/integration/test_bus_memory_fence.py --basetemp=/Volumes/Dock/tmp/w3bt/<slice>/fence` | S1 (environment change), S2 |
| G-pid1 | the node-pid1 recipe above | S1 (`success`), S4 (`success`), S5 (all five legs: E2a's own acceptance) |
| G-db | `docker compose -f tests/integration/compose.test-database.yml up -d --wait`, then `.venv/bin/python scripts/test_local.py -q -m db -n 4 --dist loadgroup` | not needed per bead: no slice touches `central/` or a migration; the lane PR's CI run covers it |

One full verify per bead (all its gates once, unpiped). The milestone gate is the lane PR's CI run (`node-pid1.yml` runs every leg; `checks.yml` runs `node-bus` and `bus-fence`).

### Preflight (2026-10-08, at 3e1baf6, clean tree)

| Check | Result |
|---|---|
| Runtime | node v20.20.2 via nvm; Python 3.12.11 in this worktree's `.venv`; nats-py importable |
| Servers | darwin-arm64 `nats-server: v2.15.0`; the linux-arm64 binary is at the fence path |
| Docker | 29.8.1, aarch64 |
| G-static | ruff 0, lint-imports 20 kept 0 broken, check_docs 0 |
| G-bus | 38 passed, 1 xfailed (strict reload xfail, E-W1-TD-S3), 61 s |
| G-unit | 67 failed, 3987 passed, 94 skipped (111 s). 65 are `tests/test_console_*` with `ERR_MODULE_NOT_FOUND`: no `central/console/node_modules` in this worktree (environment: run `npm ci` there, re-run once and replace the baseline). The other 2 are the baseline's |
| node-pid1 | not run at this tip; the local recipe above worked on 4f460df (one leg 54 s once the fixture exists) |
| Models | pin by id at launch (skill §6.7): implementers with a clear page on the approved Sonnet id; the per-slice review lens and the course-correction architect on the approved Opus id |

### Budget gates

- Per bead: wall-clock 120 min for S1, S4 and S5 (each rebuilds the node-pid1 fixture), 90 min for S2 and S3; at most 8 agents.
- Feature: 10 h wall-clock and 5 M tokens; warning milestone at 80 %, stop after the current bead at 100 %.
- At most two fix cycles per gate; then save to a pushed `wip/e3c-s<n>` branch and stop. Two consecutive stops on different slices = re-cut before any further implementation.
- A course-correction architect pass after S3 and before the docs bead.
- Review lens per slice (skill §2.3): S1 (a new unit and the base package's architecture), S4 (third-party code shipped in the base) and S5 (memory caps lowered) each get one adversarial lens on their diff (S1: systemd and packaging; S4: packaging and closure; S5: Node memory and lifecycle). S2 and S3 go straight to verify.

## Ledger

| Bead | Status | Sha | Note |
|---|---|---|---|
| D0 lane brief and cut errata | open | | commit this file and E-E3C-CUT-1..8 (see the box at the top) |
| S1 tracer: the bus runs on a Node from the base package | open | | |
| S2 the fence, re-measured: smallest events and a hard kill | open | | |
| S3 an https origin's leaf dials wss; every harness Node uses the derivation | landed | (this commit) | G-bus darwin 39 passed, 1 skipped (wss, its reason), 1 xfailed; Linux (python:3.12.11-slim-trixie + procps, linux-arm64 nats-server 2.15.0, `-k node_bus -n 4`) 40 passed, 1 xfailed, wss test green; G-unit 2 failed (the baseline's two), 4049 passed; G-static green; mutation (`leaf_url` always ws) red on Linux: the leaf never links ("unexpected EOF" at the TLS listener) |
| S4 `nodeapi` ships in the base: HostCore writes `birth` on the host line | landed | 651dc48 | G-static green (lint-imports 20 kept, after E-E3C-S4-1); G-unit 2 failed (the baseline's two), the rest green at 651dc48; G-bus 41 passed, 1 skipped (wss on macOS), 1 xfailed; node-pid1 `success` green locally (birth `v99.0.0`, reborn in a new epoch after kill -9, HostCore's MainPID unchanged, no OOM in its unit); mutation (no wheel staged) red: nats imported from site-packages, not the staged tree; errata E-E3C-S4-1..3 |
| S5 E2a lands with the bus line; the bus's memory is reported | open | | |

## Slice order and why

```
D0 ─▶ S1 ─▶ S2 ─▶ S3 ─▶ (course correction) ─▶ S4 ─▶ S5 ─▶ docs bead (after integration)
```

- **Tracer first (S1):** the thinnest path from the boot handoff to a running bus under real PID1, through the riskiest delivery seam: pinned bytes in the base package, a unit running the memory store, and the environment derived from the boot origin. The http leaf is proven on real servers in the same slice because the derivation lands there.
- **S2 next:** the heap factor is the one unmeasured number the fit rests on (E-E3B-R2-5); if linux-arm64 needs more, the contracts change now, before anything else assumes them (pre-decided rule on S2's page).
- **S3:** the production origin is https; the wss leaf is the remaining connectivity risk.
- **S4:** the first program on the bus in the base (E-E3C-CUT-2), which also proves a bus crash refills on a real Node.
- **S5 last:** E2a carries lowered caps for six other units, proven only by all five node-pid1 legs (E-E3C-CUT-1); landing it last keeps a red leg there from blocking the bus work.
- **Package depth:** S1 `appliance/boot`, `appliance/systemd`, `scripts` (+ three `contracts` constants); S2 `tests/integration` + `checks.yml`; S3 `tests/integration`; S4 `scripts` (closure, vendoring) + `appliance/host`; S5 `appliance/kernel` + `appliance/host` (+ `contracts/node_observation.py` counts).

## Shared primitives (frozen when first cut)

| Primitive | Frozen in | Named consumers |
|---|---|---|
| `contracts.node_link.LEAF_PATH`, `NODE_BUS_PORT`, `NODE_BUS_URL` | S1 | the bus environment (S1); every Node component's session URL (`appliance/host/bus.py`, S4; apps, display, health, content in E5/E6; the Player in E8); E3d's ingress route and fixture hub (`LEAF_PATH`) |
| `appliance.boot.bus_environment.bus_environment` | S1 | the handoff stage (S1); the fence container (S2); the test harness's Node servers (`tests/integration/bus_servers.node_server`, S3) |
| `scripts.pinned_fetch.cached_pinned` | S1 | `scripts/nats_server.fetch` (S1); `scripts/vendored_packages.stage_wheel` (S4) |
| `scripts.vendored_packages` (`VendoredWheel`, `import_table`, `stage_wheel`) | S4 | the base's host-core launcher (S4); the Player bundle (E8, design §12 change 10) |

---

# S1 — tracer: the bus runs on a Node from the base package

**Goal.** A V2 Node boots, its handoff stage writes the bus's environment from the boot origin and the serial, and `photo-wall-bus.service` runs the pinned nats-server from `node-base.deb` on 127.0.0.1 only, named for the Node, with its memory fence from `contracts`. A kill -9 restarts it at once, and nothing else on the Node notices: the Player and the broker keep running. A real bus started with that environment links its leaf to a hub through a path-prefix proxy at `LEAF_PATH`. Design: §3 (bus unit), §7.2 "Bus unit (E3c)", §6 (memory store), §9.5, §12 changes 1 and 10, §13; errata E-E3B-S3-1, E-E3C-CUT-4, -5, -8.

**Files.**
- New: `appliance/boot/bus_environment.py`, `appliance/systemd/photo-wall-bus.service`, `scripts/pinned_fetch.py`, `tests/integration/test_node_bus_leaf_origin.py`, `tests/node/test_node_bus_unit.py`, `tests/node_pid1_bus_probe.py`.
- Changed: `contracts/node_link.py` (three constants), `appliance/boot/node_bootstrap.py` (`materialize_handoff`), `appliance/systemd/photo-wall-node.target` (`Wants=`), `scripts/nats_server.py`, `scripts/build_node_base_deb.py`, `scripts/build_node_components.py`, `scripts/release_plan.py`, `tests/node_pid1_central_inner.py`, `tests/test_node_pid1.py`, `tests/node/test_node_boot_stage.py` (two assertions), `tests/integration/test_bus_memory_fence.py` (`_fenced_bus`'s container environment from `bus_environment`), and whatever `tests/test_release_plan.py` and `tests/test_node_component_inputs.py` pin about the base's sources.

**Frozen signatures and configuration.**

```python
# contracts/node_link.py (additions; stdlib only)
NODE_BUS_PORT: Final = 4222                                   # the bus's client port on 127.0.0.1, on every Node
NODE_BUS_URL: Final = f"nats://127.0.0.1:{NODE_BUS_PORT}"     # what every Node component and the local UI dial
LEAF_PATH: Final = "photo-wall/bus"   # the boot origin's path prefix routed to the hub's leaf listener (E3d/E4);
                                      # nats-server dials /photo-wall/bus/leafnode under it

# appliance/boot/bus_environment.py (new; stdlib, contracts and uplink only: boot is an island)
BUS_ENVIRONMENT: Final = Path("run/photo-wall-node/bus.env")  # relative to the root, as node_boot_handoff.HANDOFF

def leaf_url(central: str, serial: str) -> str:
    """The leaf's URL: ws:// for an http origin, wss:// for https; the origin's host and port unchanged
    (default port omitted, IPv6 bracketed, as uplink.origin.Origin.__str__); user and password both
    contracts.node_link.node_user(serial); path "/" + LEAF_PATH.
    ValueError("bus_origin_invalid") when Origin.parse_root refuses `central`;
    ValueError("node_link_serial") for an unsafe serial."""

def bus_environment(central: str, serial: str) -> dict[str, str]:
    """Every variable node-bus.conf and the Go runtime read, exactly these four keys:
    PHOTO_WALL_BUS_NAME = node_user(serial); PHOTO_WALL_BUS_PORT = str(NODE_BUS_PORT);
    PHOTO_WALL_BUS_LEAF_URL = leaf_url(central, serial); GOMEMLIMIT = f"{NODE_BUS_GOMEMLIMIT // MIB}MiB"
    (the module refuses at import, RuntimeError("bus_gomemlimit_not_whole_mib"), a GOMEMLIMIT that is not
    a whole MiB)."""

def write_bus_environment(root: Path, central: str, serial: str) -> Path:
    """bus_environment as a systemd EnvironmentFile (KEY=value lines, keys sorted, no quoting: no value
    holds whitespace, quotes, '$' or '\\'), written atomically (uplink.files.write_atomically) at
    root / BUS_ENVIRONMENT, mode 0600; returns the path."""

# appliance/boot/node_bootstrap.py — materialize_handoff: after host.json is written and BEFORE the
# base-marker and ABI checks, write_bus_environment(root, central, offer.serial). A stale handoff (boot
# id mismatch) still writes nothing.

# scripts/pinned_fetch.py (new; stdlib only, runnable by the system python3)
def fetch_pinned(url: str, sha256: str, *, timeout: float = 120.0) -> bytes:
    """The bytes at `url`; ValueError("pinned_fetch_digest") unless their sha256 is `sha256`."""

def cached_pinned(url: str, sha256: str, cache: Path, *, timeout: float = 120.0) -> Path:
    """cache/<sha256>: fetched once with fetch_pinned, written beside the target and renamed into place
    (a concurrent or interrupted fetch never leaves a partial file there); an existing file is re-hashed
    and refused with ValueError("pinned_fetch_digest") if it differs."""

# scripts/nats_server.py (CLI unchanged)
NODE_PLATFORM: Final = ("linux", "arm64")   # the Node's: the base package ships this asset's binary
def fetch(dest: Path, *, system: str | None = None, machine: str | None = None) -> Path:
    """The pinned binary for (system, machine), default this host, under `dest`; downloaded once through
    pinned_fetch and extracted atomically, as today."""

# scripts/build_node_base_deb.py
UNITS += ("photo-wall-bus.service",)
BUS_DIRECTORY: Final = "usr/lib/photo-wall-bus"   # nats-server and node-bus.conf
def sources(tree: Path) -> set[str]:             # + "appliance/bus/node-bus.conf", "scripts/nats_server.py", "scripts/pinned_fetch.py"
def stage_tree(tree: Path, destination: Path) -> str:
    """Unchanged signature, still network-free. Also stages appliance/bus/node-bus.conf byte for byte at
    BUS_DIRECTORY/node-bus.conf and the bus unit, adds `u pw-bus 10008 "Photo Wall Node bus" /nonexistent`
    to the sysusers file, puts the pinned NODE_PLATFORM asset's sha256 into the identity, and writes
    `Architecture: arm64` (NODE_PLATFORM's machine: the package carries that binary)."""
def stage_vendored(destination: Path, downloads: Path) -> None:
    """The pinned NODE_PLATFORM nats-server at BUS_DIRECTORY/nats-server, mode 0755 (S4 adds wheels)."""
def stage_package(tree: Path, destination: Path, downloads: Path) -> str:
    """stage_tree, then stage_vendored: the one path to a .deb (main and build_node_components use it)."""
# main(): --downloads DIR (default: a directory inside its temporary work dir); output
# photo-wall-node-base_<version>_arm64.deb.
```

`appliance/systemd/photo-wall-bus.service` (exact; the config test binds every line the acceptance criteria name):

```ini
[Unit]
Description=Photo Wall Node bus (nats-server on loopback; every start is empty)
ConditionKernelCommandLine=photowall.node=v2
After=photo-wall-node-handoff.service
StartLimitIntervalSec=0

[Service]
Type=simple
User=pw-bus
Group=pw-bus
EnvironmentFile=/run/photo-wall-node/bus.env
ExecStart=/usr/lib/photo-wall-bus/nats-server -c /usr/lib/photo-wall-bus/node-bus.conf
Restart=always
RestartSec=1
# nats-server makes $TMPDIR/nats/jetstream even on the memory store (erratum E-E3B-S3-1).
PrivateTmp=yes
MemoryMax=256M
MemorySwapMax=0
OOMScoreAdjust=-500
```

`photo-wall-node.target`: `Wants=` gains `photo-wall-bus.service`. No other unit names `photo-wall-bus.service` (nothing waits on it; HostCore's session in S4 connects with backoff). `scripts/release_plan.py`: `node-base-deb` and `base-bundle` claim `appliance/bus/**`, `scripts/nats_server.py`, `scripts/pinned_fetch.py`; `scripts/nats_server.py` leaves `NOT_SHIPPED`.

**Tests (functional, real servers).**
1. `tests/integration/test_node_bus_leaf_origin.py::test_a_node_bus_links_from_its_boot_origin_and_serial` (G-bus; darwin and Linux): a hub from Fleet's generator for SERIAL; `PrefixProxy(hub.websocket_port, LEAF_PATH)`; a `BusServer` on a copy of the shipped `node-bus.conf` whose environment is `bus_environment(f"http://127.0.0.1:{proxy.port}", SERIAL)` with only `PHOTO_WALL_BUS_PORT` replaced by a free port. Asserts: the leaf links (`leaf_connections(hub)` lists the Node's account) and `proxy.paths == [f"/{LEAF_PATH}/leafnode"]`; a local client's INFO `server_name == node_user(SERIAL)`; Central's client in that account reaches the Node's JetStream (`account_info` in domain `node`).
2. `tests/node/test_node_bus_unit.py` (G-unit; the unit file and the staged package are the things under test): acceptance criteria 2–4 below.
3. `tests/node/test_node_boot_stage.py`: the existing ABI-mismatch handoff test also asserts `run/photo-wall-node/bus.env` holds exactly the four keys of `bus_environment(central, serial)`; the stale-handoff test also asserts no `bus.env`.
4. node-pid1 `success` leg (`tests/test_node_pid1.py`, after `node.cold`, before the stage): copy `tests/node_pid1_bus_probe.py` in (as `node_pid1_package_verify.py` is) and run `python3 /var/lib/node_pid1_bus_probe.py info 4222` (stdlib only: the INFO line from 127.0.0.1:4222 and the LISTEN rows for that port from `/proc/net/tcp` and `/proc/net/tcp6`), printing `{"server_name", "jetstream", "listen"}`. Asserts `server_name == node_user(<serial from /run/photo-wall-node/host.json>)`, `jetstream` true, `listen == ["127.0.0.1"]`; `systemctl show photo-wall-bus.service -p MemoryMax --value == str(NODE_BUS_MEMORY_MAX)`; `/proc/<MainPID>/environ` holds `GOMEMLIMIT=140MiB`. Then record the MainPIDs of `photo-wall-node-player.service` and `photo-wall-app-broker.service`, `systemctl kill --signal=SIGKILL photo-wall-bus.service`, and within 15 s: `NRestarts=1`, `ActiveState=active`, a new MainPID, the probe answers again; the Player's and the broker's MainPIDs are unchanged. `tests/node_pid1_central_inner.py` adds `photo-wall-bus.service` to its condition-cleared units and to its final `systemctl start` list.

**Acceptance criteria.**
1. Tests 1, 3 and 4 pass; G-static, G-unit (baseline set only), G-bus, G-fence (1 min) and the node-pid1 `success` leg green; `uv.lock` unchanged.
2. The unit file: `MemoryMax=` equals `NODE_BUS_MEMORY_MAX` (parsed, not string-compared); `Restart=always`; `StartLimitIntervalSec=0`; `PrivateTmp=yes`; `EnvironmentFile=` is `"/" + str(BUS_ENVIRONMENT)`; no `Requires=`, `BindsTo=`, `Requisite=` or `PartOf=`; no `GOMEMLIMIT` in it.
3. Across `appliance/systemd/`, only `photo-wall-node.target` names `photo-wall-bus.service` (in `Wants=`).
4. `stage_tree(REPO, tmp)` (no network) yields `usr/lib/photo-wall-bus/node-bus.conf` byte-equal to `appliance/bus/node-bus.conf`, `lib/systemd/system/photo-wall-bus.service`, the `pw-bus` sysusers line and `Architecture: arm64`; `stage_package` adds `usr/lib/photo-wall-bus/nats-server` (the node-pid1 leg runs it, which proves it is the linux-arm64 binary).
5. `bus.env` is written before the ABI checks and never for a stale handoff.

**Mutation probe.** `leaf_url` drops the `/` + `LEAF_PATH` path: test 1 must fail (the proxy answers 404 and the leaf never links). Restore by `cp`.

---

# S2 — the fence, re-measured: smallest events and a hard kill

**Goal.** On the Pi's architecture (linux-arm64, the `bus-fence` job's `ubuntu-24.04-arm`), the shipped bus inside its fence holds a store full of the smallest `nodeapi` events with every consumer the caps admit, within the fit `contracts` checks at import, with the heap factor measured and printed; and a kill -9 restarts it with an empty store that accepts every line again. Design: §6, §7.2 contracts (the fit), §11 "Bus crash, OOM or restart", §12 change 1, §14 (the linux-arm64 assumption); errata E-E3B-R2-5, E-E3C-CUT-7.

**Files.** `tests/integration/test_bus_memory_fence.py`; `.github/workflows/checks.yml` (`bus-fence`: `timeout-minutes: 30`; its comment states both new legs and drops "returns with E3c"); `contracts/node_link.py` and `tests/test_node_bus_config.py::test_the_memory_store_fits_the_bus_fence` **only** under the pre-decided rule below.

**Frozen behaviour (no new public names).**
- The smallest event: subject `<line>.record.e` (or `<line>.observation.e` for health and content), headers exactly what `nodeapi` sends with an event (`MESSAGE_ID_HEADER` = a 32-hex-digit id as `nodeapi.envelope` makes it, `SCHEMA_MAJOR_HEADER` = `"1"`), empty payload; so no stored `nodeapi` event is smaller.
- `test_a_store_of_the_smallest_events_fits_the_measured_heap_factor`: a fenced bus (`_fenced_bus`); every line of `line_slices()` applied and the WALL mirror declared; `NODE_MAX_CONSUMERS` consumers on every stream; sample `anon` (baseline); fill every circular stream with smallest events until its `first_seq` moves (full, dropping oldest) and every keyed bucket at every key at its largest; wait 5 s; sample `anon` and read the store's bytes (`account_info().memory`). factor = (anon − baseline) / stored bytes. Asserts: no OOM kill, one start, factor ≤ `MEMORY_STORE_FACTOR`, peak ≤ `NODE_BUS_GOMEMLIMIT + NODE_BUS_HEADROOM`. Prints the factor, the stored bytes and the peak (the record the job keeps).
- `test_a_full_store_keeps_writing_inside_the_bus_fence` (existing): every circular writer cycles the smallest event, 200 B and 4000 B bodies.
- `test_a_hard_kill_restarts_the_fenced_bus_empty`: a fenced bus with every line applied and filled; `kill -9` of the server inside the container (the supervisor loop restarts it). Within 15 s: a second start, `oom_kill == 0`, the restarted server's JetStream lists no stream (`account_info().streams == 0`), every line applies again without refusal, and a re-declared WALL mirror re-syncs the hub's wall value.
- `_fenced_bus`'s container environment is `bus_environment(f"http://{hub_name}:8080", SERIAL)` (S1; its port is `NODE_BUS_PORT`, 4222, the published container port).

**Pre-decided rule (not a STOP).** If the measured factor on linux-arm64 exceeds 5: set `MEMORY_STORE_FACTOR` to the measured factor rounded up to a whole number, raise `NODE_BUS_GOMEMLIMIT` to the smallest whole MiB at which the import-time fit holds, keep `NODE_BUS_MEMORY_MAX` and `NODE_BUS_HEADROOM`, update the pinned numbers in `test_the_memory_store_fits_the_bus_fence`, and record the measurement as `E-E3C-S2-1`. `bus.env`'s `GOMEMLIMIT` follows by construction. **STOP** (owner decision: the fence size) only if `GOMEMLIMIT + NODE_BUS_HEADROOM` would then pass 256 MiB.

**Tests.** The three fence tests above (G-fence locally at 1 min; CI's `bus-fence` job runs 5 min).

**Acceptance criteria.** G-fence green locally (Docker Desktop arm64) with the factor printed; G-static, G-unit, G-bus green; the job comment matches the legs; if the rule fired, `contracts` imports cleanly and `tests/test_node_bus_config.py` is green.

**Mutation probe.** Set `MEMORY_STORE_FACTOR = 2` in `contracts/node_link.py` (the import-time fit still holds: 24 + 20.72 + 48 MiB < 140 MiB): the smallest-event leg must fail on the factor (X14 measured 4.7 on darwin). Restore by `cp`.

---

# S3 — an https origin's leaf dials wss; every harness Node uses the derivation

**Goal.** A Node whose boot origin is https links its leaf over wss, verified against the system's roots, through a TLS-terminating proxy at `LEAF_PATH`, with the same shipped `node-bus.conf`. Every Node the test harness starts takes its environment from `bus_environment`, so every G-bus test exercises the real derivation. Design: §7.2 "Bus unit", §11 "Wi-Fi, ingress or WAN stall"; the netboot trust stance (https preferred; the initramfs already verifies Central with the Debian CA bundle, so a Node that booted over https trusts the same roots); errata E-E3C-CUT-5, -8.

**Files.** `tests/integration/bus_servers.py`; `tests/integration/test_node_bus_leaf_origin.py`; `tests/integration/test_node_bus_api.py` and `tests/integration/test_node_bus_seam.py` (their private `LEAF_PREFIX` constants go; they use `LEAF_PATH`).

**Frozen signatures (test harness).**

```python
# tests/integration/bus_servers.py
class PrefixProxy:
    def __init__(self, upstream_port: int, prefix: str, *, tls: ssl.SSLContext | None = None) -> None:
        """As today; with `tls`, it terminates TLS on its listener (server side) before reading the
        request path, as an https ingress does."""

def node_server(tmp: Path, serial: str, hub: BusServer, *, leaf_port: int | None = None,
                scheme: str = "http", host: str = "127.0.0.1",
                extra_environment: Mapping[str, str] | None = None) -> BusServer:
    """A Node on the shipped configuration file whose environment is
    bus_environment(f"{scheme}://{host}:{leaf_port or hub.websocket_port}", serial) with
    PHOTO_WALL_BUS_PORT replaced by a free port, plus `extra_environment` (e.g. SSL_CERT_FILE).
    The `prefix` parameter is gone: the path is always LEAF_PATH."""
```

**Tests.** `test_node_bus_leaf_origin.py::test_a_node_bus_links_to_an_https_origin_over_wss` (G-bus; **Linux only**, skipped on darwin with the reason "Go on macOS verifies with the system keychain and ignores SSL_CERT_FILE"; CI's `node-bus` job runs it): a test CA and a server certificate for `localhost` made with `cryptography`; `PrefixProxy(hub.websocket_port, LEAF_PATH, tls=<context>)`; `node_server(..., scheme="https", host="localhost", leaf_port=proxy.port, extra_environment={"SSL_CERT_FILE": <ca.pem>})`. Asserts the leaf links, `proxy.paths == [f"/{LEAF_PATH}/leafnode"]`, and Central's client reaches the Node's JetStream through it. S1's http test moves onto `node_server`.

**Acceptance criteria.** G-bus green on darwin (the wss test skipped with its reason) and on Linux (once, in a Linux container with the matching pinned server, or the lane PR's `node-bus` job, recorded in the ledger); no test keeps a private leaf path; G-static, G-unit green.

**Mutation probe.** `leaf_url` always uses `ws`: the wss test must fail (a plain HTTP upgrade reaches a TLS listener). Restore by `cp`.

---

# S4 — `nodeapi` ships in the base: HostCore writes `birth` on the host line

**Goal.** The base package carries `nodeapi`, `contracts` and nats-py 2.16.0 (the wheel `uv.lock` pins) inside HostCore's launcher, and HostCore runs the host component's session: it declares the host line and writes `birth` and one `base` state key at every attach. After a kill -9 of the bus, the host's `birth` is back in a new epoch with no help from Central. Design: §3, §7.2 node, §7.3 (host line; "every component has a state stream, because birth lives there"), §9.5, §11 "Bus crash", §12 change 10; errata E-E3C-CUT-2, -3.

**Files.**
- New: `scripts/vendored_packages.py`, `appliance/host/bus.py`, `tests/test_vendored_packages.py`, `tests/integration/test_node_bus_host.py`.
- Changed: `scripts/module_closure.py` (ClosurePolicy `vendored`), `scripts/build_node_base_deb.py` (host-core policy, `stage_vendored`, identity, `sources`), `appliance/host/host_runner.py` (`main`), `scripts/release_plan.py` (`node-base-deb` claims `nodeapi/**` and `scripts/vendored_packages.py`; `nodeapi/**` leaves `NOT_SHIPPED`), `tests/node_pid1_bus_probe.py` (`birth`), `tests/test_node_pid1.py` (`success` leg).

**Frozen signatures.**

```python
# scripts/vendored_packages.py (new; stdlib only)
@dataclass(frozen=True, slots=True)
class VendoredWheel:
    distribution: str            # the uv.lock package name
    version: str
    url: str                     # the uv.lock wheel url (py3-none-any)
    sha256: str                  # the uv.lock wheel hash
    imports: tuple[str, ...]     # the import roots it gives first-party code

WHEELS: Final[tuple[VendoredWheel, ...]] = (
    VendoredWheel("nats-py", "2.16.0",
                  "https://files.pythonhosted.org/packages/48/a3/16cec37172144d3362d7551bb823a098b2b2903281fc095dfe4480b8c2fd/nats_py-2.16.0-py3-none-any.whl",
                  "aeb1ff123966c05833d26c7df7e1d54c1c6d32b612428b21677a2e921f1fecae", ("nats",)),
)

def import_table() -> Mapping[str, str]:
    """import root -> distribution. The module refuses at import, ValueError("vendored_import_twice"),
    a root two wheels give, or a wheel and scripts.debian_packages both give."""

def wheel(distribution: str) -> VendoredWheel:
    """The entry for `distribution`; KeyError on an unknown name."""

def stage_wheel(wheel: VendoredWheel, into: Path, downloads: Path) -> None:
    """Every member of the wheel (its packages and its dist-info, licence included) under `into`, from
    pinned_fetch.cached_pinned(wheel.url, wheel.sha256, downloads); ValueError("vendored_wheel_member")
    for a member path that resolves outside `into`."""

# scripts/module_closure.py
@dataclass(frozen=True, slots=True)
class ClosurePolicy:
    name: ...                                                      # unchanged
    roots: tuple[str, ...]
    forbidden: tuple[str, ...]
    third_party: Mapping[str, str]                                 # import root -> Debian package
    vendored: Mapping[str, str] = MappingProxyType({})             # import root -> vendored distribution
# closure_for allows, and records in Closure.third_party, both tables' roots (compute_closure's signature is
# unchanged: closure_for passes the union); unreached_imports refuses an unreached root of either table.

# scripts/build_node_base_deb.py
# POLICIES["host-core"] gains vendored={"nats": "nats-py"} (from vendored_packages.import_table()).
def stage_vendored(destination: Path, downloads: Path) -> None:
    """As S1, plus stage_wheel for every vendored root of every policy, into usr/lib/photo-wall-<name>."""
# stage_tree's identity also carries each vendored wheel's sha256; sources() += "scripts/vendored_packages.py".

# appliance/host/bus.py (new)
HOST_VERSION: Final = "1.0.0"                  # the host component's release version (birth)
HOST_STATE: Final = KeyTable({"base": 256})    # the host line's state table beside birth and outbox
HOST_SLICE: Final[Slice]                       # Slice("host", (event_buffer("host", "record", <the line's
                                               # bytes less the state bucket's cap>), state_bucket("host", HOST_STATE)))

def host_session(base_tag: str, *, url: str = NODE_BUS_URL) -> NodeSession:
    """The host component's session, not started: it declares the host line, writes birth (Release(
    HOST_VERSION, digest=base_tag, schema_majors={"host": 1})) at every attach, and holds `base` =
    canonical JSON {"base_tag": base_tag}. Its start() returns at once and it connects forever, so
    HostCore never waits on the bus."""

# appliance/host/host_runner.py main(): session = host_session(value["base_tag"]); session.start() before
# the loop; session.stop() in the finally. Nothing else in HostCore changes.
```

**Tests (functional).**
1. `tests/integration/test_node_bus_host.py::test_the_host_component_writes_birth_and_refills_it_after_a_bus_crash` (G-bus): a Node bus from the harness (no hub needed); `host_session("base-test", url=node.client_url).start()`; within 10 s `KV_state_host` holds `birth` (`component` host, `pipe` fleet, `release_digest` "base-test", `slice_digest` = `HOST_SLICE.digest`) and `base`; `node.crash()` then `node.start()`: within 10 s both are back under a different epoch, and `RECORD_host` exists.
2. `tests/integration/test_node_bus_host.py::test_the_staged_host_core_imports_nats_and_nodeapi_from_its_own_directory` (G-bus tier, network; pinned downloads cached under the pytest base temp's parent, shared by xdist workers): `stage_package(REPO, tmp, downloads)`; `isolated_import(<tmp>/usr/lib/photo-wall-host-core, ["nats", "nodeapi.node", "appliance.host.bus"], policy=POLICIES["host-core"])` succeeds.
3. `tests/test_vendored_packages.py` (G-unit; the table is the thing under test): every `WHEELS` entry equals its `uv.lock` `[[package]]` (name, version, the wheel's url and hash, read with `tomllib`); `import_table()` keys are given by no Debian package; the host-core closure computed from the tree reaches exactly `{"nats"}` among vendored roots.
4. node-pid1 `success` leg: `python3 -I -B /var/lib/node_pid1_bus_probe.py birth 4222` (the probe puts `/usr/lib/photo-wall-host-core` first on `sys.path` and reads `KV_state_host` with nats-py) prints `{"birth", "base", "epoch"}`; birth's `component` is host and `release_digest` the boot's `base_tag`; after S1's kill -9, within 15 s `birth` is back with a different `epoch`.

**Acceptance criteria.** Tests 1–4 pass; G-static (lint-imports: `appliance.host` importing `nodeapi` breaks no contract), G-unit, G-bus green; node-pid1 `success` green; `uv.lock` and `[project].dependencies` unchanged; HostCore's `MemoryMax` (96 MiB) unchanged and its leg shows no OOM.

**Mutation probe.** `stage_vendored` stages no wheel: test 2 must fail with nats not importable. Restore by `cp`.

---

# S5 — E2a lands with the bus line; the bus's memory is reported

**Goal.** The memory line table (E2a-1, implemented as bf73312 on the unpushed local branch `w1/e2a`, never landed: E-E3C-CUT-1) lands, with its `bus` line equal to `NODE_BUS_MEMORY_MAX` and bound to `photo-wall-bus.service`, so the unit's cap, the table and the fit are one number. HostCore reports `memory_peak:bus` and `oom_kill:bus`, the Pi's real reading of the fence on its 16 KiB pages (E-E3C-CUT-7). Design: §15 (E2a row, `memory_peak:bus`); epics E2a; errata E-W1-E2a-1-1, E-E3C-CUT-1, -6.

**Files.** `git cherry-pick bf73312` (resolve `.claude/errata.md` by appending its E-W1-E2a-1-1 line; `tests/test_node_pid1.py` may need a hand merge beside S1/S4's hunks), then: `appliance/kernel/capacity.py` (the bus line), `appliance/host/host_linux.py` (`_SLICES`, `_OOM_SLICES`), `contracts/node_observation.py` (family counts), `tests/test_node_metric_families.py`, `tests/node/test_node_boot_stage.py` (if it pins the row set).

**Frozen changes.**

```python
# appliance/kernel/capacity.py — E2a's LINES, with the bus line replaced by:
MemoryLine("bus", NODE_BUS_MEMORY_MAX,
           "contracts.node_link fit (E3b design §7.2); measured by checks.yml bus-fence",
           cgroup="photo-wall-bus.service")          # no parent: the bus is in system.slice (E-E3C-CUT-6)
# (from contracts.node_link import NODE_BUS_MEMORY_MAX; the kernel may import contracts)

# appliance/host/host_linux.py
_SLICES += (("bus", "system.slice/photo-wall-bus.service"),)
_OOM_SLICES += ("bus",)

# contracts/node_observation.py
MetricFamily("memory_peak:", 6),   # hostcore, base, preparation, app, display (Weston), bus
MetricFamily("oom_kill:", 4),      # base, preparation, app, bus
```

**Tests.** E2a's `tests/node/apps/test_memory_lines.py` (the bus unit's `MemoryMax=` is its line; the bus line names a node unit file); `tests/test_node_metric_families.py` (new counts; its "keeps the first three `oom_kill:` rows" case becomes four); node-pid1, all five legs, with E2a's `assert_memory_lines` (which now covers `photo-wall-bus.service` wherever it started) and, in `success`, a `memory_peak:bus` row in Host Management's reported facts.

**Acceptance criteria.** G-static, G-unit, G-bus green; all five node-pid1 legs green locally (E2a's own acceptance: no `oom_kill` under the lowered caps); a red leg for a code reason after two fix cycles = save to `wip/e3c-s5`, STOP (failure protocol); `systemctl show photo-wall-bus.service -p MemoryMax` equals `line("bus").cap_bytes`.

**Mutation probe.** The bus line's cap becomes `NODE_BUS_MEMORY_MAX // 2`: `test_every_node_unit_cap_is_its_line` must fail. Restore by `cp`.

---

## Deployment notes (written down, not applied)

- E3c changes no Central deployment. The base reaches Nodes through the normal release flow (merge → tag → base image → TFTP staging via iac); a Node takes it at its next reboot.
- Until E3d's ingress route exists in iac, a Node on this base redials its leaf to `<origin>/photo-wall/bus/leafnode` about once a second (a 404 or a non-upgrade answer): self-recovering log noise, no functional effect. **For E3d / the iac PR:** route `/photo-wall/bus/` on the Central host (and every host a Node's boot origin can end on) to the hub's WebSocket listener, upgrade allowed, path unchanged; the hub must accept Node leaves there (E-E3C-CUT-5).

## Orchestrator choices this cut makes (not owner decisions)

1. E2a-1 lands in this lane (S5) rather than separately (E-E3C-CUT-1). Alternative: land E2a first on its own branch; S5 then shrinks to the bus line and the two metrics.
2. HostCore is the base's first `nodeapi` program (S4, E-E3C-CUT-2). Alternative: defer shipping `nodeapi` and nats-py to E5/E6, and E3e's join has no Node program on the bus.
3. `LEAF_PATH = "photo-wall/bus"` is frozen here and must reach the e3d lane before its ingress page freezes (E-E3C-CUT-5).

## Errata

Cut errata (code architect, 2026-10-08; full text staged in `/Volumes/Dock/tmp/node-redesign/e3c-lane/errata-e3c-cut.md`, appended to `.claude/errata.md` by D0; then `grep -a 'E-E3C-CUT' .claude/errata.md`):
- **E-E3C-CUT-1** E2a-1 never landed (bf73312 on unpushed local `w1/e2a`); S5 carries it, with the bus line at `NODE_BUS_MEMORY_MAX`. Push `w1/e2a` as `wip/e2a-1` now: it exists on one disk only.
- **E-E3C-CUT-2** shipping `nodeapi` needs a base launcher that imports it; HostCore is the first (S4).
- **E-E3C-CUT-3** nats-py is not in Debian trixie; a vendored-wheel table, test-bound to `uv.lock` (S4).
- **E-E3C-CUT-4** the server ships in `node-base.deb` (one install site for the squashfs and node-pid1); the package becomes arm64; `stage_tree` stays network-free.
- **E-E3C-CUT-5** `LEAF_PATH` and `NODE_BUS_PORT` frozen in `contracts`; relay to E3d.
- **E-E3C-CUT-6** the bus sits in `system.slice`, not the base slice.
- **E-E3C-CUT-7** the fence already runs on arm64; E3c adds the smallest-event factor and kill -9; 16 KiB pages stay bench-only.
- **E-E3C-CUT-8** the environment is written by the handoff stage, not an `ExecStartPre=`; one conf serves ws and wss (probe `/Volumes/Dock/tmp/node-redesign/e3c-probes/wss_leaf_tls.out`).

Implementer and verifier errata append below as `E-E3C-S<n>-<k>` (one line each here; full text in `.claude/errata.md`).
- **E-E3C-S1-1** (implementer, S1, 2026-10-08) the page's `sources()` additions `scripts/nats_server.py` and `scripts/pinned_fetch.py` are wrong: `sources()` lists paths of the *fetched* tree, and `fetch_tree` archives only first-party packages, `scripts/debian_packages.py` and `pyproject.toml`, so `fetch_sources` failed (`FileNotFoundError: .../tree/scripts/nats_server.py`) and every `test_node_component_inputs.py` key test went red. Both scripts are the build process's own imports and are already keyed by `node_component_inputs.builder_files` (the `ENTRY_MODULES` closure lists both). `sources()` adds only `appliance/bus/node-bus.conf`; `release_plan` still claims both scripts for `node-base-deb` and `base-bundle`.
- **E-E3C-S1-2** (implementer, S1, 2026-10-08) `base-bundle` already claims `appliance/**`, so it gains only the two scripts, not a redundant `appliance/bus/**`. `scripts/nats_server.py` keeps its `python3 scripts/nats_server.py fetch` CLI (checks.yml) by the repo-root `sys.path` guard `scripts/device_root_checks.py` uses, since it now imports `scripts.pinned_fetch`. Test 1 reads the INFO line with `tests/node_pid1_bus_probe.server_info` (one reader for the integration test and the node-pid1 leg).
- **E-E3C-S2-1** (implementer, S2, 2026-10-08) the pre-decided rule fired. Heap factor of a store of the smallest `nodeapi` event (`<line>.record.e` / `.observation.e`, `event_headers(1)`, empty payload; 11,452,885 B stored, every stream at 12 consumers), 2.15.0 linux-arm64 in Docker Desktop's arm64 VM (4 KiB pages), ten runs: 4.15, 4.33, 4.36, 4.37, 4.39, 4.53, 4.54, 4.55, 4.72, **5.13**. The spread is the method's: the baseline `anon` varies 30.6–37.0 MiB (garbage not yet collected around the consumer creates), the full one 80.3–86.6 MiB. 5.13 > 5, so `MEMORY_STORE_FACTOR` = 6 and `NODE_BUS_GOMEMLIMIT` = 141 MiB (fit 72 + 20.72 + 48 = 140.72 MiB; + 4 MiB headroom = 145 ≤ 256: no STOP); `NODE_BUS_MEMORY_MAX` and `NODE_BUS_HEADROOM` unchanged; `bus.env` carries `GOMEMLIMIT=141MiB` by construction. Mutation probe (`MEMORY_STORE_FACTOR = 2`): red, "heap per stored byte 4.39 > 2". **For the docs bead:** decision 0017's C5 row still states "5 ×, measured 4.7", 140 MiB and 128.72 MiB (not edited here, per the lane rules). CI's own `ubuntu-24.04-arm` figure is not yet seen; a printed factor above 6 there fires the rule again.
- **E-E3C-S2-2** (implementer, S2, 2026-10-08) finding, not a defect by the owner rule: the fit leaves out the server's dedupe ids. `nodeapi.buffers.event_buffer` leaves `duplicate_window` at the server default (2 min), and every event carries a fresh `Nats-Msg-Id`, so the bus keeps every id written in the last two minutes: heap that grows with write rate × window, not with the store. With the page's headers on every circular write, the full-store leg at CI length (5 min, ~7,500 writes/s) reached `anon` 146.5 MiB, past `GOMEMLIMIT` (141), and a cgroup peak of 169.7 MiB; the 256 MiB fence held with no OOM. A Node's normal event rate is orders of magnitude lower, and an OOM restarts the bus empty, from which the Node recovers. Class fix, in `nodeapi` (outside E3c): bound event buffers' `duplicate_window` to the outbox's retry span, or charge rate × window in the fit. Route to whoever next owns `nodeapi.buffers`.
- **E-E3C-S2-3** (implementer, S2, 2026-10-08) one G-bus run (`-n 4`) failed `test_node_bus_seam.py::test_nothing_a_node_program_does_crosses_the_leaf_while_the_hub_is_stalled`; it was green alone and in a full `-n 4` rerun. S2 cannot reach it: its Node servers build their environment by hand (no `GOMEMLIMIT`, no `bus_environment`), and `-k node_bus` does not select the fence file. The failure text was not captured; if it recurs, capture it (likely the 1 s or 5 s timeouts under load).
- **E-E3C-S3-1** (implementer, S3, 2026-10-08) every harness Node now runs on `bus_environment`, so every G-bus test runs with `GOMEMLIMIT=141MiB` and its leaf at `/photo-wall/bus` (directly on the hub's WebSocket listener when no proxy is given: the hub accepts any path); no test changed outcome. The wss test asserts behaviour only (link, `proxy.paths`, Central's JetStream reach), not the URL's scheme, so the mutation fails on the TLS listener rather than on a string check. A Linux run of G-bus in `python:*-slim` needs `procps`: `BusServer.rss_bytes` shells out to `ps` (the stalled-hub seam test), which CI's ubuntu runner has. Go dials `localhost` as `[::1]` first and retries `127.0.0.1` (log noise only; the proxy listens on IPv4).
- **E-E3C-S4-1** (implementer, S4, 2026-10-08) the page's acceptance "lint-imports: `appliance.host` importing `nodeapi` breaks no contract" was wrong: pyproject's "Only the Node API library talks to NATS" is a `forbidden` contract, which judges indirect chains by default, so `appliance.host.bus -> nodeapi.node -> nats` broke it. Fixed in `pyproject.toml` (outside the page's file list; `[project].dependencies` untouched) with `allow_indirect_imports = true`: the contract's intent is that only `nodeapi` names `nats`, and every Node program (E3d's NodeLink supervisors in `central`, E5/E6 components, the Player in E8) reaches it through `nodeapi`. Probed: a module in `appliance` with a direct `import nats` still breaks it.
- **E-E3C-S4-2** (implementer, S4, 2026-10-08) the page's `sources() += "scripts/vendored_packages.py"` is wrong for E-E3C-S1-1's reason (the fetched tree holds no script but `scripts/debian_packages.py`). `scripts/vendored_packages.py` is the build process's own import, keyed by `node_component_inputs.builder_files` (checked: the `ENTRY_MODULES` closure lists it); `nodeapi/**` reaches `sources()` through host-core's closure. Not added.
- **E-E3C-S4-3** (implementer, S4, 2026-10-08) `base-bundle` also claims `nodeapi/**` and `scripts/vendored_packages.py`, beside `node-base-deb` (page): `test_every_script_a_release_build_runs_is_claimed_by_what_it_builds` requires every import of `scripts/build_node_components.py` to be `base-bundle`'s, and the squashfs installs `node-base.deb`. `stage_tree` now refuses a policy's declared root no code reaches (`unreached_imports`), as the bootstrapper and Player builders do, so a stale `vendored` entry cannot ship an unused wheel. The mutation probe (no wheel staged) fails test 2 with "nats: imported from …/.venv/…/site-packages/nats/__init__.py, not <staged tree>" rather than "not importable": `isolated_import` appends the interpreter's site-packages; on a Node the import fails outright.
