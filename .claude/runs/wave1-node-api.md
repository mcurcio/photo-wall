# Run brief: wave 1 (E3a tracer ∥ E2a ∥ E2b ∥ D-0017)

**Owner go:** 2026-10-06 ("GO"). **One PR:** every lane lands on the integration branch `claude/wave1-node-api` (this PR), updated bead by bead. This replaces the plan's separate E2/E3/docs branches below.
**Lanes:** each lane builds in its own worktree under `/Volumes/Dock/tmp/w1/<lane>` on a lane branch; a verified bead is landed onto the integration branch one at a time (cherry-pick), then pushed.
**Owner, after the plan was cut:** N16–N19 (every change emits an event; documents readable by any client; own namespace plus one-message broadcast; the Node holds its events) are guidelines, not requirements: "reasonable guidelines. I'm open to revising in certain cases." D-0017 records them as revisable current choices.
**Env:** `source ~/.nvm/nvm.sh && nvm use 20`; each worktree's own `.venv` (`uv sync --frozen`, `UV_CACHE_DIR=/Volumes/Dock/tmp/uv-cache`); pytest `--basetemp=/Volumes/Dock/tmp/w1bt/<lane>` (short path, AF_UNIX); never write at a repo root; Linux-only tests in a container with the source mounted read-only and a venv built inside (never bind-mount `.venv`, E-ENV-1); revert `uv.lock` if anything rewrites it unless the bead adds a dependency.

---

# E2+E3 wave 1 beads: E3a (tracer) ∥ E2a ∥ E2b ∥ D-0017

**Status:** bead cut for wave 1 only. Re-cut 2026-10-06 against `origin/main` at 7d2fd49 (PR 47 merged). Its tree is byte-identical to 8b121ad, so every file:line below holds on `main`. Waves 2–4 are one line each (§7), plus the list of what they must carry; they are cut when they start.
**Base:** `main`. E3a builds on an **E3 branch**. E2a and E2b build on an **E2 branch**, which merges to `main` as soon as E2c is green. D-0017 builds on its own docs branch and merges to `main` before E3b starts.
**Rules:** `~/.claude/skills/implementation-workflow/SKILL.md`. Each bead is green on its own, gets one full verify, and carries its own frozen page. The format copies `.claude/runs/e1-layered-codebase.md`.

**Owner answers applied** (they steer; they are not requirements unless stated):
- E2 and E3 run as one workstream. E2 merges to `main` when E2c is green. The production hub (iac) waits for E4.
- Integration tests over internal unit tests. A unit test appears only where a configuration file or a table is the thing under test.
- Nothing added here can refuse a Node on the trusted network: no credentials, JWTs or ACLs.
- Wave 1 Q1: estimated caps, with some buffer, for the four unmeasured base services (applied in E2a, W4).
- Wave 1 Q2: the two frame-changing tracer fallbacks stop for the owner: a TCP leaf port, and a per-Node JetStream domain (§6 rows 1–2).
- Protocol framing: the Node exposes an API over NATS and Central is one client of it. The Node owns all its buckets and streams. One account per Node. No Node id in any subject. Each component owns its own service, buckets and streams, and announces itself through `$SRV.INFO` plus a birth event.
- API1: methods (NATS micro services) for one-shot and live actions; desired-state documents, with reported status, for anything that should stay true. Every state change emits an event. Documents are readable by any client.
- API3: Central writes every held major. API5: S1 covers event streams and state.
- API4 note: the Node holds its events, read by Central's durable consumer. There is no hub buffer, no hub intent mirror and no direction filter.
- API6: a wall-wide account at the hub holds one stream. Every Node account imports it, and each Node keeps a read-only local mirror.
- API7: no sync beat. Timing documents ride NATS; clocks follow NTP.
- API8: five retention classes in a 12 MiB store inside the 64 MiB bus fence. Components set defaults; Central overrides per Node.
- API9: writes are ordered by the Node's KV per-key revision plus a conditional update (expected revision), with the writer recorded on each write. The Node never refuses. When another client changed a value, Central adopts the Node's value and shows the operator.
- API10: Central sends with lead time. A late Node keeps last good, then joins late.

**Sources:** [e2e3-epics.md](e2e3-epics.md); [design-r3.md](design-r3.md) §2.4, §3.3, §18 (A2, A2b, A3, A11, A12, A14); [memory-truth.md](memory-truth.md); [owner-answers.md](owner-answers.md) "Protocol framing", "Node API round 1", "Node API round 2"; the Node API discussion page (`…/scratchpad/node-api-discussion/node-api.html`, round api2: N1–N19, surfaces, plug-ins, classes, F1–F7); upstream nats-server v2.15.0 source and `docs.nats.io/reference/jetstream/cross-account-subjects` (cited as `ns:<file>:<line>`).

## Ledger

| Bead | Lane | Status | Sha | Note |
|---|---|---|---|---|
| E3a-1 | E3 (tracer) | open | | WebSocket leaf, a method across it, same-domain isolation, the wall-wide mirror. Three stop branches |
| E3a-2 | E3 | open | | Central writes a Node bucket conditionally, drains a Node stream after commit, counts a gap; hub reload and hub wipe |
| E3a-3 | E3 | open | | The Node server refuses past its store limit; each retention class keeps its promise |
| E2a-1 | E2 | open | | Line table; caps from readings; double counts removed |
| E2b-1 | E2 | open | | Reproducible squashfs image from the sealed archive, proven by mount and `verify_root` |
| D-0017 | docs | open | | 0017 updated to the Node API framing and the API answers |
| Wave-1 verify | all | open | | One CI run per branch |

## What this re-cut changed (history, one line per round)

- 2026-10-06 08:43, first cut: E3a proved r3's shape (hub `INTENT` bucket mirrored to the Node, `UPLINK` work-queue at the hub, leaf direction filters, a frozen object table in `contracts/node_bus.py`).
- 2026-10-06, this re-cut (Node API rounds 1–2): E3a becomes protocol-agnostic plumbing plus probes of the Node-served framing, in three beads instead of two. Dropped: the intent mirror, uplink stall, direction-filter and Node-store-resourcing probes; the object table and every subject grammar (each probe owns its streams and subjects). Kept: the pinned server (now the measured 2.15 line, F1), the harness, the `node-bus` CI job, the account generator (now emitting the wall-wide account), same-domain isolation, reload-keeps-leaves. Added: a method across the leaf, a conditional write into a Node bucket, Central's durable consumer with ack-after-commit and a counted gap, the wall-wide mirror (stop branch), the store-limit refusal, per-class retention. E2a: one basis cell (API8's store fits the bus line) and Q1 applied. E2b: unchanged. New: D-0017.

---

## 0. Where the real tree contradicts the plan (re-cuts made here)

| # | The plan said | Real tree | Consequence |
|---|---|---|---|
| W1 | E2b makes squashfs the release asset and the fixture input, makes its digest `environment_sha256`, and binds the format into `base_abi` | The Node consumes only tar: `stage_archive` (`appliance/apps/environment.py:186`) parses the stream with `tarfile.open(..., "r\|")` and checks the tar's digest. If E2b swapped the asset, every node-pid1 leg would go red until E2c lands. That breaks green-alone (§3.1). | **E2b is additive.** It adds the image builder and proves it on the real built archives in CI. Shipped bytes, `components.json`, the fixture and `base_abi` are unchanged. **The flip moves to E2c bead 1**, together with the Node staging the image. `scripts/build_node_pid1_fixture.py` is not touched in wave 1. |
| W2 | "`squashfs-tools` is already in `appliance/Dockerfile.builder:16`" | That image is the OS-image builder. The node components build runs in `BUILDER_IMAGE`, a pinned Debian python image (`scripts/node_build_inputs.py:25`). | The image tool runs in its own pinned image: `BUILDER_IMAGE` plus the `PIN` snapshot plus `squashfs-tools`, built by the existing `docker_build`. |
| W3 | E2a: every slice cap from the table, and a table that leaves no content line fails | In the tar era the store is 2560 MiB on 4 GB (`appliance/kernel/capacity.py:33`). The r3 lines plus that store exceed 4045. Design-r3 §2.4 forbids lowering the preparation slice before the tar path is gone. | E2a binds every cap except preparation's, which stays `PREPARATION_SLICE_BYTES` (4 GiB) as a named interim line. **The content-line check lands in E2c.** |
| W4 | E2a: caps follow the readings, 2 × peak | Only the base slice (138) and Weston's unit (9) have readings. The other four members have none. Their caps today add up to 800, more than the 768 slice. | Estimates bounded by the measured slice: 96 + 96 + 64 + 64, plus Weston's 64, makes 384 of 480. **Owner Q1: "yes, with some buffer"** — the buffer is already in the numbers: the four estimated caps total 320, about 2.5 × the 129 MiB those four used together on the Pi, each alone at least half of it, and the slice keeps 96 MiB unassigned. A wrong estimate shows as an `oom_kill:base` row (A12). |
| W5 | E2a touches `capacity.py` and `appliance/systemd` | The transient Player unit has its own `"MemoryMax=2G"` at `appliance/apps/process_linux.py:71`. | E2a also edits `process_linux.py`; the cap comes from the table. |
| W6 | Users `node-<serial>`, `central-<serial>` | The serial charset is `[A-Za-z0-9:_.-]{1,128}` (`central/content_catalog/catalog.py:59`). A `:` breaks the leaf URL's `user:password@`; a `.` splits `$SYS.ACCOUNT.<name>.…` subjects. | The account id is `account_id(serial)`, a stdlib hash in `contracts`. Users are `node-<id>` and `central-<id>`. Both sides derive it from the serial, so no wire change. |
| W7 | The hub "reload" proof | SIGHUP does not reach across compose or pod containers. | The generator emits a `SYS` account with user `fleet`; E3a-2 reloads through it. |
| W8 | E3a runs "in CI" | The `unit` job's timeout is 4 minutes (`.github/workflows/checks.yml:44`). | E3a adds a `node-bus` job to `checks.yml`. The `unit` tier collects the bus tests and skips them with an allowlisted reason. |
| W9 | (page, Probe B) Central puts into a Node-owned bucket "through domain `node`" | nats-py 2.16.0 writes every key to `$KV.<bucket>.<key>` whatever the domain (`nats/js/kv.py:222,295`; prefix fixed at `nats/js/client.py:1413`). nats-server denies `$JS.API.>`, `$KV.>` and `$OBJ.>` on a leaf whose two sides have different domains, for every non-system account (`ns:server/jetstream_api.go:371`, applied at `ns:server/leafnode.go:2086,2111`), and maps `$JS.<domain>.API.$KV.>` to `$KV.>` on the owning server (`ns:server/jetstream_api.go:394`). | **A plain nats-py `kv.put` from Central never reaches the Node.** Central writes on `$JS.node.API.$KV.<bucket>.<key>` with the `Nats-Expected-Last-Subject-Sequence` header. E3a-2 proves both halves; E3b's hub role owns the subject. Reads (direct get, watch, consumers) already go through `$JS.node.API` and are unaffected. |
| W10 | (page) Wall copy 0.5 MiB on the Node; "a stream of about 1 MiB" at the hub | A mirror with a smaller `max_bytes` than its origin discards its own oldest messages, which can be a topic's only value. | **One number**, `WALL_STREAM_BYTES` = 512 KiB in `contracts/node_link.py`, is both the hub stream's and every mirror's `max_bytes`. |
| W11 | (C5) The leaf runs over WebSocket, with no new LAN port | nats-server accepts a leaf on its WebSocket listener only when `leafnodes` has a port (`ns:server/websocket.go:1335`). The remote dials `<url path>/leafnode` (`ns:server/leafnode.go:3622`). | The generator emits a `leafnodes` listener bound to loopback, which no Node dials. The ingress route (E3d/E4) forwards a path prefix; E3a's leaf URL carries a prefix so that form is proven. |
| W12 | Pin `2.12.14` | The fence (39.4 MiB peak) was measured on 2.15.0 (F1). 2.15.0 is the newest 2.15 release on 2026-10-06; 2.15.1 is at RC.2. | Pin **2.15.0**, the same on Node and hub; re-check at bead start and take 2.15.1 if it is released. nats-py **2.16.0** (newest; its `nats.micro` serves `$SRV`). |
| W13 | (page, API8) "nodeapi never declares a stream without a byte cap" | nats-server has a per-account `max_bytes_required` limit (`ns:server/opts.go:2440`; refusal 10113 "account requires a stream config to have max bytes set"). | The Node config puts local clients in one account, `API`, with `max_bytes_required: true` through `no_auth_user`. The rule moves from library convention to the server's refusal. E3a-3 proves it. |

---

## 1. Shared facts every bead uses

**Gates** (from AGENTS.md):

| Gate | Command |
|---|---|
| G-static | `.venv/bin/python -m ruff check .` · `.venv/bin/lint-imports` · `python3 scripts/check_docs.py` |
| G-unit | `PHOTO_WALL_TEST_REQUIRE_DATABASE=1 .venv/bin/python -m pytest -q -m "not db and not browser" -n 4 --dist worksteal` |
| G-release | `pytest -q tests/test_release_plan.py tests/test_module_closure.py tests/test_package_closures.py` |
| G-bus (new, E3a) | `PHOTO_WALL_NATS_SERVER=$(python3 scripts/nats_server.py fetch --dest /Volumes/Dock/tmp/nats) .venv/bin/python -m pytest -q tests/integration -k node_bus`. Runs on the Mac (darwin-arm64 binary); CI runs it in `checks.yml` `node-bus`. |
| G-pid1 | node-pid1 legs `success`, `failure`, `outage`, `reboot`, `refused` (CI `node-pid1.yml`; locally arm64 Docker, privileged, fixture from `scripts/build_node_pid1_fixture.py`) |
| G-image (new, E2b) | `PHOTO_WALL_IMAGE_MOUNT_TESTS=1 PHOTO_WALL_NODE_COMPONENTS=<dir> .venv/bin/python -m pytest -q tests/node/apps/test_environment_image.py` (Linux, docker, passwordless sudo or root; CI `node-components.yml`) |
| G-docs (D-0017) | `python3 scripts/check_docs.py` plus the bead's grep checks |
| G-CI | the branch PR's `pipeline.yml` run |

**Environment (copied from E1's run brief):**
- Run `source ~/.nvm/nvm.sh && nvm use 20` before pytest.
- Use `.venv/bin/python`.
- Put TMPDIR and scratch under `/Volumes/Dock/tmp`.
- Revert `uv.lock` if anything other than E3a-1's dependency edit rewrites it.
- Run Linux-only tests in a container with the source mounted read-only and a venv built inside. Never bind-mount `.venv` (errata E-ENV-1).
- The local baseline is "the same failure set as the base tip on this machine". CI is authoritative.

**Lanes and checkouts:**
- Four worktrees, one implementer each: `e3a` on the E3 branch; `e2a` and `e2b` each on its own branch, merged into the E2 branch; `d0017` on `docs/0017-node-api`.
- Each worktree runs the skill's §6 preflight.
- The lanes are disjoint by file (§5). Two shared files take one hunk each, in fixed places.

**Budget gates:**
- Per bead: 60 min of wall-clock and 6 agents.
- Wave: 3 h of wall-clock, with a warning at 2.4 h.
- One fix cycle per gate, then save to `wip/<bead>` and stop.
- A tracer probe that fails switches to its fallback **only** where §6 marks it pre-approved. Otherwise the lane stops.

**Errata:** append to `.claude/errata.md` with ids `E-E3A-n`, `E-E2A-n`, `E-E2B-n`, `E-D0017-n`. A frozen page changes only through an erratum and a re-cut.

---

# Lane E3: E3a, the Node API seam tracer (3 beads)

**What it proves.** The NATS plumbing the Node-served API stands on, with production configuration and raw clients:
- the leaf link runs over WebSocket, through a path prefix, with credentials in the URL;
- one account per Node at the hub, with Central as a client inside it, isolated even though every Node uses domain `node`;
- Central calls a Node micro service across the leaf, and discovers it with `$SRV.PING` and `$SRV.INFO`;
- Central writes a Node-owned bucket across the leaf, and the server rejects a conditional update on a stale revision (API9's mechanism);
- Central's durable pull consumer on a Node stream acknowledges only after commit, survives a crash mid-drain, and sees overflow as a counted gap (API4 note, API5, F3);
- one Central write into the wall-wide account's stream reaches every Node's read-only local mirror (API6; **unprobed anywhere, so a stop branch**);
- the Node server refuses a stream past its store limit, and one without a byte cap (API8);
- each retention class keeps what it promises (API8).

**How:** the hub configuration from Fleet's real generator, the Node configuration file the base will ship, real `nats-server` processes on the pinned version, and raw nats-py clients playing Central and Node components. **Every stream, bucket, service and subject a probe uses is owned by the test.** Nothing here freezes a subject grammar, an envelope or a class table; those are E3b's.

**Non-goals:** `nodeapi`'s API (E3b), the systemd unit and the Pi fence (E3c), Central's database, worker sessions and presence (E3d), the ingress route and the iac hub (E3d/E4).

## Bead E3a-1: WebSocket leaf, a method across it, same-domain isolation, the wall-wide mirror

**Goal:** prove the four topology checks first. Three of them have frame-changing fallbacks that stop for the owner (§6 rows 1–3), so they run before anything else is built on them. The bead also lands the primitives every later E3 bead consumes: the link names, the generator, the shipped Node configuration and the pinned server.
**Est.:** 45 min, including one full verify. **Depends on:** `main` (PR 47).

### Frozen page

**1. `contracts/node_link.py`** (new; stdlib only). The names both ends of a leaf link derive on their own. No subject grammar lives here: each component owns its subjects (0017 C4). Consumers: Fleet's generator and the E3a harness (now); E3b `nodeapi` (the wall mirror's declare); E3c's bus configuration test (`NODE_DOMAIN`); E3d's Central sessions and its `WALL` declare.

```python
NODE_DOMAIN: Final = "node"                  # every Node bus's JetStream domain; Central reaches it as $JS.node.API
HUB_DOMAIN: Final = "hub"                    # the hub's JetStream domain; only the wall-wide account uses JetStream there
WALL_ACCOUNT: Final = "WALL"                 # the hub account holding the wall-wide stream (API6)
WALL_STREAM: Final = "WALL"                  # its one stream, latest per subject; each Node keeps a mirror of it
WALL_STREAM_BYTES: Final = 512 * 1024        # max_bytes of the hub stream AND of every Node mirror (W10; API8 "wall copy")
WALL_API_PREFIX: Final = "ACC.WALL.API"      # a Node account's import prefix for WALL's consumer API
WALL_DELIVER_PREFIX: Final = "DELIVER.WALL"  # the mirror's delivery prefix, the same in every Node account
WALL_WRITER_USER: Final = "central-wall"     # Central's user in WALL; a selector, not a secret

def account_id(serial: str) -> str: ...
    # "N" + sha256(serial.encode()).hexdigest()[:24]
    # ValueError("node_link_serial") when serial is outside [A-Za-z0-9:_.-]{1,128}
def node_user(serial: str) -> str: ...       # "node-" + account_id(serial)
def central_user(serial: str) -> str: ...    # "central-" + account_id(serial)
```

**2. `central/fleet/node_bus_accounts.py`** (new): Fleet's account generator. Pure: no I/O and no `nats` import (E1's fence at `pyproject.toml:243-247` holds).

```python
FLEET_SYSTEM_USER: Final = "fleet"

@dataclass(frozen=True, slots=True)
class HubListeners:
    server_name: str
    client_host: str
    client_port: int
    websocket_host: str
    websocket_port: int          # the Nodes' leaf listener (C5): ws://<user>:<user>@<host>:<port>[/<prefix>]
    leaf_host: str               # W11: nats-server accepts WebSocket leaves only with a leafnodes port; no Node dials this one
    leaf_port: int
    monitor_port: int | None     # /leafz; None in production until E3d decides
    store_dir: str
    max_file_store_bytes: int    # the hub's JetStream store; ValueError("hub_store_too_small") when < WALL_STREAM_BYTES

def hub_configuration(serials: Iterable[str], listeners: HubListeners) -> str: ...
    """nats-server configuration as JSON text (the server's parser reads JSON):
    json.dumps(..., sort_keys=True, indent=2) + "\\n", so the same set of serials, in any order and
    with duplicates folded, gives the same bytes.
    - jetstream: domain HUB_DOMAIN, store_dir, max_file_store = max_file_store_bytes, max_memory_store 0.
    - Account WALL_ACCOUNT: jetstream {max_file: WALL_STREAM_BYTES, max_mem: 0}; user WALL_WRITER_USER
      (password = user name); exports, none restricted by `accounts`:
        service "$JS.API.CONSUMER.CREATE.WALL", service "$JS.API.CONSUMER.CREATE.WALL.>",
        service "$JS.API.CONSUMER.DELETE.WALL.*", stream "DELIVER.WALL.>",
        service "$JS.FC.WALL.>", service "$JS.FC.*.*.WALL.>"
      (the cross-account-subjects reference's mirror set, v1 and v2 flow-control forms).
    - One account per serial, named account_id(serial). NO jetstream key: the Node holds its own
      objects (API4 note). Users node_user and central_user, password = user name (a selector).
      Imports exactly the six above: the three CONSUMER services mapped `to` WALL_API_PREFIX +
      ".CONSUMER.…", the stream and both $JS.FC services unmapped. NO exports, and no import from
      any other account: the generator has no form for either.
    - Account SYS (system_account) with user FLEET_SYSTEM_USER (reload requests, E3a-2).
    - websocket {host, port, no_tls: true}; leafnodes {host: leaf_host, port: leaf_port};
      http monitor on monitor_port when set.
    ValueError("node_link_serial") as account_id."""
```

**3. `appliance/bus/node-bus.conf`** (new): the file the base ships (E3c installs it). Every per-Node value comes from the unit's environment as a nats-server `$VAR`:

```
server_name: $PHOTO_WALL_BUS_NAME
host: 127.0.0.1
port: $PHOTO_WALL_BUS_PORT
max_pending: 2MB
write_deadline: "2s"
jetstream {
  domain: node                    # == NODE_DOMAIN; held by the config test
  store_dir: $PHOTO_WALL_BUS_STORE
  max_file_store: 12MB            # API8's store; E3c binds it to the line table (§7 carry)
  max_memory_store: 0             # see §6 row 10
}
accounts {
  API {                           # every local client lands here (W13); the leaf binds it
    jetstream { max_file: 12MB, max_mem: 0, max_bytes_required: true }
    users: [{user: local, password: local}]
  }
}
no_auth_user: local               # local clients connect with no credentials
leafnodes {
  remotes: [{
    urls: [$PHOTO_WALL_BUS_LEAF_URL]   # ws://node-<id>:node-<id>@<origin host>[:port]/<prefix>
    account: API
  }]
}
```

No `deny_imports` or `deny_exports`: direction filters are gone (API4 note).

**4. `scripts/nats_server.py`** (new, stdlib only): the pinned server. E3c and E3d reuse the constant, so the hub, the base and CI run one version.

```python
NATS_SERVER_VERSION: Final = "2.15.0"   # W12: the measured line (F1); take 2.15.1 if released at bead start
ASSETS: Final[Mapping[tuple[str, str], tuple[str, str]]]
    # (platform.system().lower(), normalized machine) -> (asset, sha256 from the release's SHA256SUMS):
    # ("linux","amd64")  nats-server-v2.15.0-linux-amd64.tar.gz  5d2c51caca950333aba84911df7d377f826f3a59ec36061c6539105084f65c92
    # ("linux","arm64")  nats-server-v2.15.0-linux-arm64.tar.gz  cdc208f5a3f42963a52b6ab06ef65626bb870315dc936e26ba571780c6351112
    # ("darwin","arm64") nats-server-v2.15.0-darwin-arm64.tar.gz e1c4e22d70bd44abfa0bcb3c16f7cf0c66f648c2e728c58924e8a1ce88913cc8
def asset_for(system: str, machine: str) -> tuple[str, str]: ...   # ValueError("nats_server_platform")
def fetch(dest: Path) -> Path: ...      # download, verify sha256, extract nats-server into dest; cached, idempotent
def main() -> None: ...                 # `fetch --dest DIR` prints the binary path
```

**5. Harness** (`tests/integration/bus_servers.py`, imported as `integration.bus_servers`; test-owned):

```python
@dataclass
class BusServer:
    name: str
    config: Path
    store: Path
    client_url: str
    environment: Mapping[str, str]
    def start(self) -> None: ...            # Popen; waits until the client port accepts
    def stop(self) -> None: ...             # SIGTERM, wait; the store stays
    def wipe(self) -> None: ...             # stop, then delete the store directory

def nats_server_binary() -> Path: ...       # PHOTO_WALL_NATS_SERVER, else pytest.skip("set PHOTO_WALL_NATS_SERVER (checks.yml node-bus runs it)")
def hub_server(tmp: Path, serials: Sequence[str]) -> BusServer: ...    # writes hub_configuration(...) on free loopback ports
def node_server(tmp: Path, serial: str, hub: BusServer, *, prefix: str = "bus") -> BusServer: ...
    # the shipped node-bus.conf, unmodified; PHOTO_WALL_BUS_LEAF_URL = ws://node-<id>:node-<id>@127.0.0.1:<ws port>/<prefix>
async def central(hub: BusServer, serial: str) -> "nats.aio.client.Client": ...   # as central_user(serial) at the hub's client port
async def wall_writer(hub: BusServer) -> "nats.aio.client.Client": ...           # as WALL_WRITER_USER
async def local(node: BusServer) -> "nats.aio.client.Client": ...                # no credentials (no_auth_user)
async def declare_wall(writer: "nats.aio.client.Client") -> None: ...
    # create-if-absent: stream WALL_STREAM, subjects ("wall.>",) (test-owned), max_msgs_per_subject 1,
    # discard NEW, max_bytes WALL_STREAM_BYTES, file storage
async def declare_wall_mirror(node_client: "nats.aio.client.Client") -> None: ...
    # create-if-absent on the Node: stream WALL_STREAM, mirror StreamSource(name=WALL_STREAM,
    # external=ExternalStream(api=WALL_API_PREFIX, deliver=WALL_DELIVER_PREFIX)), max_bytes WALL_STREAM_BYTES
async def wall_value(node_client: "nats.aio.client.Client", subject: str) -> bytes | None: ...
    # latest message for subject in the local mirror (get_last_msg / direct get)
```

**Tests:**
- `tests/integration/test_node_bus_seam.py` (G-bus):
  - `test_central_calls_a_node_service_across_the_websocket_leaf`
  - `test_two_node_accounts_in_domain_node_stay_isolated`
  - `test_one_wall_write_reaches_every_node_mirror_and_the_mirror_is_read_only`
- `tests/test_node_bus_config.py` (unit tier; a configuration-file and generator test):
  - `test_the_shipped_node_config_is_loopback_domain_node_and_caps_every_stream`
  - `test_node_accounts_import_only_the_wall_set_and_export_nothing`
  - `test_the_generated_configuration_is_deterministic`
  - `test_account_id_is_stable_and_refuses_an_unsafe_serial`

**6. Dependencies and CI:**
- `pyproject.toml`: add `nats-py==2.16.0` (newest at bead start) to `[dependency-groups] dev` (`pyproject.toml:23-33`). E3b moves it to runtime. `uv.lock` is regenerated with `uv lock` (intended).
- `.github/workflows/checks.yml`: new job `node-bus` (ubuntu-24.04, timeout 8 min): checkout, python-uv, `uv sync --frozen`, `python3 scripts/nats_server.py fetch --dest "$RUNNER_TEMP/nats"` into `PHOTO_WALL_NATS_SERVER`, then `pytest -q tests/integration -k node_bus`.
- `tests/conftest.py`: one line, **inserted as the first entry** of `CI_SKIP_ALLOWLIST` (`tests/conftest.py:41`): `"set PHOTO_WALL_NATS_SERVER",  # checks.yml node-bus runs the bus tests`.
- `scripts/release_plan.py`: one line in **`NOT_SHIPPED`** (`scripts/release_plan.py:310`): `"scripts/nats_server.py",  # E3c claims it when the base ships the server`.

### Files touched

| File | Change |
|---|---|
| `contracts/node_link.py` | new |
| `central/fleet/node_bus_accounts.py` | new |
| `appliance/bus/node-bus.conf` | new. Data, not a package; `base-bundle` claims it through `appliance/**`. |
| `scripts/nats_server.py` | new |
| `tests/integration/bus_servers.py`, `tests/integration/test_node_bus_seam.py`, `tests/test_node_bus_config.py` | new |
| `pyproject.toml`, `uv.lock` | dev dependency (**E3a only**) |
| `.github/workflows/checks.yml` | `node-bus` job (**E3a only**) |
| `tests/conftest.py` | **shared with E2b**: E3a takes the first entry of the tuple |
| `scripts/release_plan.py` | **shared with E2b**: E3a's line goes in `NOT_SHIPPED` |

**Release effect:** `contracts/**`, `appliance/**` and `uv.lock` make every node package and the base bundle due on the E3 PR; on `main` they release only when E3 merges. The shipped bytes change only by one contracts module and an unused configuration file.

### Acceptance, each with its proving gate and one mutation probe

Restore every probe by reversing the edit, never with `git checkout`.

| AC | Criterion | Proved by | Mutation probe |
|---|---|---|---|
| 1 | The Node's leaf, configured only with a `ws://` URL carrying a path prefix and credentials, appears in the hub's `/leafz` under that Node's account. Central, as `central_user(serial)` at the hub's client port, finds the Node's test service with `$SRV.PING` and `$SRV.INFO.<name>`, and a request to its endpoint returns the Node's reply within 2 s | G-bus `test_central_calls_a_node_service_across_the_websocket_leaf` | `hub_configuration` omits the `websocket` block → red |
| 2 | With the Node server stopped and the hub up, Central's request raises "no responders" in under 1 s, not a timeout | same test | The harness's Node stop becomes a no-op → the request is answered → red |
| 3 | Two Nodes, both domain `node`, in two generated accounts, each running a same-named service and a same-named stream: Central-A's `$SRV.PING` sees only Node A's instance id; Central-A's `$JS.node.API` stream info reports Node A's message count; a Node A publish on a test subject reaches no subscriber on Node B or as Central-B | G-bus `test_two_node_accounts_in_domain_node_stay_isolated` | `hub_configuration` puts both serials' users in one account → red |
| 4 | One Central publish into `WALL` reaches the local mirror of two Nodes within 10 s. Node B, stopped during a second publish (store kept), holds the latest value within 10 s of restart. A local publish on a `wall.` subject on Node A changes neither its mirror nor the hub stream | G-bus `test_one_wall_write_reaches_every_node_mirror_and_the_mirror_is_read_only` | Drop the `DELIVER.WALL.>` stream import from the Node accounts → red |
| 5 | The shipped Node file listens on 127.0.0.1, uses domain `NODE_DOMAIN`, sets `max_bytes_required: true` on account `API`, binds the leaf to `API`, and has no `deny_` key. Generated Node accounts carry no `jetstream`, no exports, and exactly the six WALL imports; the WALL account's `max_file` is `WALL_STREAM_BYTES`. The output is byte-identical for a permuted, duplicated serial list | G-unit `tests/test_node_bus_config.py` | Generate one extra export on a Node account → red |
| 6 | The bus tests run in CI without Docker against the pinned binary, and the unit tier's skip is owned | G-CI `checks / node-bus` and `checks / unit` green | Remove the allowlist line → `unit` fails "skips no CI job owns" |

**Stated costs:**
- The seam test owns its servers' lifetimes. A crashed `nats-server` leaves only a temp directory.
- The WebSocket path goes through no reverse proxy here; the ingress route is E3d's and E4's.
- The `fleet` system user, the per-Node users and `central-wall` are selectors, not secrets. Anyone on the LAN who reaches the hub's client port can act as any of them. That is the owner's trusted-network posture, stated.
- The WALL exports are unrestricted and every Node account shares one delivery prefix, so any account at the hub could read another Node's copy of wall data. Wall data is the same for every Node by definition, so nothing is revealed. The hub copies each mirror's delivery into every Node account (N² internal copies of about 0.5 MiB); only the owning leaf has interest, so no extra traffic crosses a leaf.
- "Read-only" holds against every ordinary publish: a mirror captures no subject. A raw local client that forges messages on its own mirror's delivery subject is not stopped, because N4 rules out a permission (the page's F4 class).

---

## Bead E3a-2: Central writes a Node bucket, drains a Node stream, counts a gap; hub reload and hub wipe

**Goal:** prove the mechanisms Central's client role rests on (API9's conditional write, API4/API5's durable read with ack after commit, F3's counted gap), and the two hub lifecycle checks E3d needs (reload keeps leaves; the wall mirror survives a hub that lost its store).
**Est.:** 35 min, including one full verify. **Depends on:** E3a-1.

### Frozen page

No new production surface. The harness gains:

```python
def node_kv_subject(bucket: str, key: str) -> str: ...
    # f"$JS.{NODE_DOMAIN}.API.$KV.{bucket}.{key}": the only subject a cross-domain KV write lands on (W9)
async def central_put(central_client: "nats.aio.client.Client", bucket: str, key: str, value: bytes,
                      *, expected_revision: int | None) -> int: ...
    # js.publish on node_kv_subject with "Nats-Expected-Last-Subject-Sequence" when expected_revision
    # is set; returns the new revision; raises nats.js.errors.APIError (err_code 10071) on a stale one
@dataclass
class Recorder:                              # Central's "commit": an append-only file of (stream seq, payload)
    path: Path
    def commit(self, seq: int, payload: bytes) -> None: ...      # append + flush, then return
    def sequences(self) -> list[int]: ...
    def gaps(self) -> list[tuple[int, int]]: ...                 # recorded (first missing, count) rows
async def reload_hub(hub: BusServer, serials: Sequence[str]) -> None: ...
    # rewrite hub_configuration(serials, ...) in place, then a request as FLEET_SYSTEM_USER on
    # "$SYS.REQ.SERVER.<server id>.RELOAD"; waits for the reply
def leaf_connections(hub: BusServer) -> Mapping[str, int]: ...    # account -> leaf cid, from /leafz
```

Test functions, in `tests/integration/test_node_bus_seam.py`:
- `test_central_conditionally_updates_a_node_bucket_across_the_leaf`
- `test_central_durable_consumer_acks_after_commit_and_loses_nothing` (parametrized: a plain stream; a KV bucket's stream)
- `test_a_stream_that_overflows_while_central_is_away_reaches_central_as_a_counted_gap`
- `test_a_reload_that_adds_an_account_keeps_existing_leaf_links`
- `test_the_wall_mirror_catches_up_after_the_hub_loses_its_store`

### Files touched

`tests/integration/bus_servers.py` and `tests/integration/test_node_bus_seam.py` only. A pre-approved fallback (§6) may also change `appliance/bus/node-bus.conf`, through an erratum.

### Acceptance

| AC | Criterion | Proved by | Mutation probe |
|---|---|---|---|
| 1 | A Node component creates a bucket (history 2, discard new, a byte cap). Central reads a key's revision r across the leaf, writes with expected revision r and gets r′ > r; the Node's watcher sees Central's value. A second write with expected revision r is rejected with 10071 and the Node's value stays the one written at r′. A plain `$KV.<bucket>.<key>` publish from Central never lands (W9) | G-bus `test_central_conditionally_updates_a_node_bucket_across_the_leaf` | `node_kv_subject` returns `$KV.<bucket>.<key>` → red |
| 2 | The Node publishes 200 events. Central's durable pull consumer (`$JS.node.API`, explicit ack, ack wait 2 s) commits each one, then acknowledges it. The Central client is closed mid-batch at about event 80, without acknowledging that batch; a new client binds the same durable. The record then holds every stream sequence 1–200, none missing (duplicates allowed, folded by sequence). Same result for a KV bucket's stream | G-bus `test_central_durable_consumer_acks_after_commit_and_loses_nothing` | The harness acknowledges before it commits, and the crash lands between the two → sequences missing → red |
| 3 | A Node stream with limits retention, discard old and a small byte cap: Central records 10 and stops. The Node publishes 1000, every publish accepted. Central resumes: the first delivered sequence is past its last recorded one, and Central records one gap row (first missing, count) **before** any later message; the count equals the stream's `first_seq` − last recorded − 1 | G-bus `…reaches_central_as_a_counted_gap` | Declare that stream with discard NEW → publishes are refused → red |
| 4 | Adding a third serial and reloading through the system account keeps the first two Nodes' leaf cids. The third Node's leaf connects, and its mirror gets the current wall values | G-bus `…keeps_existing_leaf_links` | The harness restarts the hub instead of reloading → cids change → red |
| 5 | After the hub is stopped, its store deleted and the hub restarted, Central re-declares `WALL` and re-puts the latest value of every subject; each Node mirror then holds the latest value of every subject, including one changed during the outage, within 30 s | G-bus `…catches_up_after_the_hub_loses_its_store` | The harness Central declares only on its first connect → red |

**Stated costs:** the probes run on Linux and macOS (amd64 or arm64) with the CI host's page size. The Pi's 16 KiB-page fence is E3c's (A3).

---

## Bead E3a-3: The Node server refuses past its store limit; each retention class keeps its promise

**Goal:** prove that API8's budget is enforced by the server, not by convention, and that each class's limits behave as the page states, including the sizing that makes "a bucket cannot fill" true.
**Est.:** 25 min, including one full verify. **Depends on:** E3a-1 (independent of E3a-2; serial only because the lane has one implementer).

### Frozen page

No new production surface. The harness gains one helper; `tests/integration/test_node_bus_limits.py` is new.

```python
def kv_bucket_bytes(bucket: str, keys: Sequence[str], history: int, max_value: int,
                    header_bytes: int = 0) -> int: ...
    # sum over keys of history * (34 + len(f"$KV.{bucket}.{key}") + max_value
    #   + (4 + header_bytes if header_bytes else 0)): nats-server's per-message file-store size
    #   (ns:server/filestore.go:10055-10062). The class sizing E3b inherits.
```

Test functions:
- `test_the_node_server_refuses_streams_past_its_store_limit_and_without_a_cap`
- `test_each_retention_class_keeps_its_promise` (parametrized: `record`, `observation`, `state`, `desired`)

The class parameters are the page's (section 6), held in the test only: record = limits, discard old, byte cap, no age; observation = limits, discard old, small byte cap, `max_age` 1 s here (6 h in production); state = KV, history 4, discard new, `max_value_size`, byte cap from `kv_bucket_bytes`; desired = KV, history 2, otherwise as state. The wall copy is E3a-1 AC4 and E3a-2 AC5.

### Files touched

`tests/integration/bus_servers.py` (one helper), `tests/integration/test_node_bus_limits.py` (new). A pre-approved fallback (§6 row 10) may change `appliance/bus/node-bus.conf`, through an erratum.

### Acceptance

| AC | Criterion | Proved by | Mutation probe |
|---|---|---|---|
| 1 | On the shipped Node file: streams whose byte caps sum to 12 MiB are accepted; one more 1 MiB stream is refused (10047), and the existing streams are intact. Raising an existing stream's cap past the limit is refused and its old cap stays. A stream with no byte cap is refused (10113). A memory-storage stream is refused | G-bus `test_the_node_server_refuses_streams_past_its_store_limit_and_without_a_cap` | Remove `max_bytes_required: true` from `node-bus.conf` → the uncapped declare succeeds → red |
| 2 | **record:** past its cap the oldest go, every publish is accepted, and `first_seq` advances by the number dropped. **observation:** a message older than `max_age` is gone. **state** and **desired:** updates of the listed keys, at up to `max_value` bytes and ten times the bucket's capacity in total, are all accepted, and each key keeps `history` values; a key beyond the list is refused once the bucket is full, and every listed key keeps its last value; a value over `max_value_size` is refused and the key keeps its last good value | G-bus `test_each_retention_class_keeps_its_promise` | Size the state bucket without the per-message term (`history * max_value` per key) → updates refused → red |

**Stated costs:** these numbers are the page's starting split, owned by the test. E3b's class table may change them; the behaviour is what this bead fixes.

---

# Lane E2a: the line table (1 bead)

## Bead E2a-1: One line table; every program cap from it; double counts removed

**Goal:**
- Put one device-class line table in `appliance/kernel/capacity.py`.
- Every program slice and unit `MemoryMax`, and the transient Player unit's, equals its line.
- A cap with a reading equals the rule's value: floor + max(2 × peak, peak + 32 MiB), rounded up to 32 MiB.
- Members inside a slice add up to no more than the slice.
- The bus line is reserved at 64 MiB.
- Preparation and the store are untouched (W3).

**Est.:** 35 min, including one full verify. Proof of real PID1 behaviour is CI only. **Depends on:** `main` (PR 47).
**Unchanged by the Node API answers** except the bus row's basis (API8 checked against it) and W4 (Q1 applied).

### Frozen page

**1. `appliance/kernel/capacity.py`** (additions; the existing names are unchanged):

```python
MEMORY_ROUND: Final = 32 * MIB

def cap_from_peak(peak_bytes: int, floor_bytes: int = 0) -> int: ...
    """floor + max(2 * peak, peak + 32 MiB), rounded up to MEMORY_ROUND (design-r3 §2.4 rule 3)."""

@dataclass(frozen=True, slots=True)
class MemoryLine:
    name: str
    cap_bytes: int
    basis: str                       # the reading or estimate, and where it comes from
    peak_bytes: int | None = None    # the highest measured or spike-measured peak the cap follows
    floor_bytes: int = 0             # a configured budget the cap never goes below (Player textures)
    cgroup: str | None = None        # the unit or slice file whose MemoryMax= is this line
    parent: str | None = None        # the line of the slice that cgroup sits in
    def __post_init__(self) -> None: ...
        # ValueError("memory_line_invalid") when cap_bytes < 1, or peak_bytes is set and
        # cap_bytes != cap_from_peak(peak_bytes, floor_bytes)

LINES: Final[tuple[MemoryLine, ...]]
def line(name: str) -> MemoryLine: ...       # KeyError on an unknown name
```

`LINES`, exactly (MiB):

| name | cap | peak | floor | cgroup | parent | basis |
|---|---:|---:|---:|---|---|---|
| kernel | 200 | | | | | E: kernel, slab, page tables, PID1, journald, udev |
| cma | 64 | | | | | E |
| base-image | 320 | | | | | M 311, image size |
| overlay | 128 | | | | | E use; the 512 MiB mount size is unchanged (§2.4 rule 6) |
| hostcore | 96 | 34 | | `photowallhostcore.slice` | | M 26 + S 8 |
| host-core | 96 | | | `photo-wall-host-core.service` | hostcore | the slice's whole line |
| base | 480 | 228 | | `photowallbase.slice` | | M 138 + S/E 90 |
| broker | 96 | | | `photo-wall-app-broker.service` | base | E inside the measured slice (W4) |
| manager-supervisor | 96 | | | `photo-wall-manager-supervisor.service` | base | E (W4); leaves with AppManager |
| display | 64 | 9 | | `photo-wall-display.service` | base | M 9 |
| display-controller | 64 | | | `photo-wall-display-controller.service` | base | E (W4) |
| health | 64 | | | `photo-wall-health.service` | base | E (W4); unchanged |
| bus | 64 | | | | | owner fence. With API8's 12 MiB store: S server peak 39.4 + store pages 12 × 1.45 = 17.4, ≈ 57 (4 KiB pages; A3 owes the Pi's 16 KiB reading). E3c binds its unit and the store limit |
| app | 992 | 229 | 512 | `photowallapp.slice` | | M 229; texture budget 512 (`player/service.py:128`) |
| preparation | 4096 | | | `photowallpreparation.slice` | | **interim, tar era**: `PREPARATION_SLICE_BYTES`; E2c sets it to the root lines plus the process line |

**2. Unit files** (`MemoryMax=` only; `MemoryMin=` is unchanged and must stay ≤ its line):

| File | Was | Now |
|---|---|---|
| `photowallbase.slice` | 768M | 480M |
| `photowallapp.slice` | 2G | 992M |
| `photo-wall-app-broker.service` | 192M | 96M |
| `photo-wall-manager-supervisor.service` | 192M | 96M |
| `photo-wall-display.service` | 256M | 64M |
| `photo-wall-display-controller.service` | 96M | 64M |
| `photo-wall-health.service`, `photowallhostcore.slice`, `photo-wall-host-core.service`, `photowallpreparation.slice` | unchanged | unchanged |

**3. `appliance/apps/process_linux.py:71`:** `"MemoryMax=2G"` becomes `f"MemoryMax={line('app').cap_bytes}"`. Apps may import kernel (E1 layers).

**4. Tests:**

```python
# tests/node/apps/test_memory_lines.py   (config-file test; imports kernel and apps only, per E1's placement rule)
NODE_UNITS: Final  # appliance/systemd files matching node-base-deb's globs: photo-wall-*.service, photowall*.slice
def test_every_node_unit_cap_is_its_line() -> None: ...          # each MemoryMax= present == the line naming that file; each line with a cgroup names a file carrying exactly that cap
def test_members_fit_inside_their_slice() -> None: ...            # sum of caps per parent <= the parent's cap
def test_every_memory_min_is_within_its_line() -> None: ...
def test_the_transient_player_unit_cap_is_the_app_line() -> None: ...   # app_unit_properties(Path("/r"))

# tests/test_node_pid1.py   (integration under real systemd)
def assert_memory_lines(node: Node) -> None: ...
    """For every line with a cgroup that the boot started: `systemctl show <cgroup> -p MemoryMax`
    equals cap_bytes, and each capped slice's memory.events reads oom_kill 0. Called at the end
    of stage_and_complete (success, failure, outage) and after boot B in the reboot leg."""
```

### Files touched

| File | Change |
|---|---|
| `appliance/kernel/capacity.py` | additions |
| `appliance/systemd/{photowallbase,photowallapp}.slice`, `photo-wall-{app-broker,manager-supervisor,display,display-controller}.service` | `MemoryMax=` |
| `appliance/apps/process_linux.py` | one property |
| `tests/node/apps/test_memory_lines.py` | new |
| `tests/test_node_pid1.py` | `assert_memory_lines` and its 2 call sites (**E2a only in wave 1**) |

**Release effect:**
- node-base-deb, node-manager-deb (`appliance/kernel/**`) and the base bundle are due, so node-pid1 and base-image run.
- `player-environment` is not due, because `environment.py` is unchanged.

### Acceptance

| AC | Criterion | Proved by | Mutation probe |
|---|---|---|---|
| 1 | Every node unit or slice `MemoryMax` is its line, and no line names a file without that cap | G-unit `test_every_node_unit_cap_is_its_line` | `photo-wall-app-broker.service` back to `MemoryMax=192M` → red |
| 2 | Base-slice members add up to ≤ 480 (384 today) | G-unit `test_members_fit_inside_their_slice` | Raise the broker's line and unit to 320 → red (sum 608) |
| 3 | A cap with a reading follows the rule | Construction: `MemoryLine.__post_init__` runs at import, so every importer of `capacity` goes red | Set the app line's cap to 1024 → `memory_line_invalid` at import |
| 4 | The transient Player unit's cap is the app line, not a literal | G-unit `test_the_transient_player_unit_cap_is_the_app_line` | Restore `"MemoryMax=2G"` → red |
| 5 | Real PID1 applies the table and nothing OOM-kills under it: legs success, failure, outage and reboot are green, with `assert_memory_lines` passing; refused is green | G-pid1 (CI) | Health's line and unit set to `16M` → the base slice's `oom_kill` > 0 → red. Costs one CI run on `wip/E2a-1-probe`. |

**Stated costs:**
- The four estimated member caps rest on one boot's slice reading (A12), with the buffer the owner asked for (W4). The running check is the Pi's `oom_kill:base` row after the release.
- API8 keeps the bus line at 64 MiB: the 12 MiB store fits with about 7 MiB of margin on 4 KiB pages. API8's alternative (b) would change this one row to 96 MiB.
- Weston's 64 MiB comes from a 9 MiB reading taken with the Player running. A GPU-heavy show could exceed it, and the `oom_kill` row would name it.
- The content-line check waits for E2c (W3).

---

# Lane E2b: the image builder (1 bead)

## Bead E2b-1: A reproducible squashfs image from the sealed archive, proven by mount and `verify_root`

**Goal:** build a release root's squashfs image, deterministically, from the exact archive the Node consumes today. The tree is the one the Node's own `stage_archive` produces, so the image equals what a Node would unpack. Prove three things on the real CI-built `app.tar` and `manager-primary.tar`:
- two builds give one digest;
- the mounted image passes `verify_root` unchanged (A14);
- the image is smaller than the tar.

**Ships nothing** (W1).
**Est.:** 40 min, including one full verify. Proof on the real archives is CI only. **Depends on:** `main` (PR 47). **Unchanged by the Node API answers:** it touches no wire.

### Frozen page

```python
# scripts/build_environment_image.py   (new)
IMAGE_SUFFIX: Final = ".squashfs"
SQUASHFS_OPTIONS: Final[tuple[str, ...]] = ("-noappend", "-no-progress", "-all-root", "-no-xattrs",
                                            "-comp", "zstd", "-b", "128K")
# SOURCE_DATE_EPOCH = PIN.epoch is set in the tool's environment: it fixes mkfs and inode times.

@dataclass(frozen=True, slots=True)
class EnvironmentImage:
    sha256: str
    size_bytes: int
    path: Path                       # <output>/<sha256>.squashfs

def tools_image(*, architecture: str) -> str: ...
    """The pinned image tool: BUILDER_IMAGE + PIN's snapshot sources + squashfs-tools, built by
    node_build_inputs.docker_build(role="image-tools"); returns its image ID. A snapshot pin, not
    the runner's apt, decides the mksquashfs version, so the digest cannot drift with the runner."""

def image_from_archive(archive: Path, reference: AppEnvironmentRefV2, output: Path, *,
                       base_abi: str, graphics_abi: str, plugin_abi: str,
                       tools: str) -> EnvironmentImage: ...
    """stage_archive(archive, <private roots>, reference, ..., owner_uid=os.geteuid()) builds the
    verified tree. Its top directory is set to 0755. Then
    `docker run --rm --network none -e SOURCE_DATE_EPOCH -v <tree>:/in:ro -v <output>:/out <tools>
    mksquashfs /in /out/<tmp> *SQUASHFS_OPTIONS`, hashed, and renamed to <sha256>.squashfs.
    Raises stage_archive's ValueErrors unchanged. No root is needed: -all-root sets ownership."""
```

```python
# tests/node/apps/test_environment_image.py   (new; integration: real docker, real loop mount)
# Module skip unless PHOTO_WALL_IMAGE_MOUNT_TESTS=1:
#   "set PHOTO_WALL_IMAGE_MOUNT_TESTS (docker, loop devices, sudo; node-components.yml runs it)"
# Under that flag PHOTO_WALL_NODE_COMPONENTS is required (a missing one fails, never skips).
# Parametrized over the archives in components.json: app_environment, manager_primary.
# pytest runs as the runner user; only mount/umount go through `sudo -n` (direct when euid 0).
def test_two_builds_of_one_archive_give_one_image_digest(role: str) -> None: ...
def test_the_mounted_image_passes_verify_root_unchanged(role: str) -> None: ...
    # mount -t squashfs -o loop,ro,nodev,nosuid; verify_root(mount, ref, ..., owner_uid=0)
def test_the_image_has_the_contract_format_and_is_smaller_than_the_archive(role: str) -> None: ...
    # `unsquashfs -s` inside the tools image: zstd, block size 131072, no xattrs; size < archive size
```

**CI** (`.github/workflows/node-components.yml`, job `build`): one step after "Stamp the component set". It runs on a cache hit or a build alike, on the arm64 runner:

```yaml
      - name: Prove the environment images (reproducible, mount, verify_root)
        env:
          PHOTO_WALL_IMAGE_MOUNT_TESTS: "1"
          PHOTO_WALL_NODE_COMPONENTS: ${{ runner.temp }}/node-components
        run: .venv/bin/python -m pytest -q tests/node/apps/test_environment_image.py
```

**Bookkeeping:**
- `tests/conftest.py`: the skip reason above, **appended as the last entry** of `CI_SKIP_ALLOWLIST`.
- `scripts/release_plan.py`, in the **`PACKAGES`/`SUITES` block** (lines 266–300 and 422–436), never in `NOT_SHIPPED`:
  - add `"scripts/build_environment_image.py"` to `player-environment`'s and `base-bundle`'s inputs (it becomes their shipped form in E2c);
  - add `"tests/node/apps/test_environment_image.py"` to the `node-pid1` suite's `paths`, so a test-only change still runs node-components.

### Files touched

| File | Change |
|---|---|
| `scripts/build_environment_image.py` | new |
| `tests/node/apps/test_environment_image.py` | new |
| `.github/workflows/node-components.yml` | one step (**E2b only**) |
| `tests/conftest.py` | **shared with E3a**: E2b takes the last entry of the tuple |
| `scripts/release_plan.py` | **shared with E3a**: E2b edits the `PACKAGES`/`SUITES` hunks |

E2b does not touch `build_node_components.py`, `build_app_environment.py`, `build_node_pid1_fixture.py`, `appliance/**` or `contracts/**`.

### Acceptance

| AC | Criterion | Proved by | Mutation probe |
|---|---|---|---|
| 1 | Two builds of the same real archive give the same sha256, for both roles | G-image in CI (`node-components / build`) | Drop `SOURCE_DATE_EPOCH` from the tool's environment → digests differ → red |
| 2 | Mounted read-only, the image passes `verify_root(owner_uid=0)` unchanged: ownership, directory mode 0755, no xattrs, per-file sha256 | G-image | Drop `-all-root` → `environment_root_ownership` (staging runs as the runner user) → red |
| 3 | The verify is not vacuous over the mount | G-image | Append one byte to `rootfs/usr/bin/python3` between staging and squashing → `environment_root_digest_mismatch` → red |
| 4 | The format is the contract's (zstd, 128 KiB), and the image is smaller than its tar. The run records the measured sizes (expected about 288 and 62 MiB). | G-image | `-b 256K` → red |
| 5 | Shipped bytes are unchanged, every new path is classified, and a test-only change makes the job due | G-release; G-CI node-pid1 legs green on the E2 branch with the same `components.json` schema | Remove the `player-environment` claim → `test_every_tracked_file_ships_in_a_package_or_is_declared_unshipped` → red |

**Local verify:** a privileged arm64 Linux container on the Mac, with the Docker socket mounted, against a components directory built locally. If that is impractical, the verifier reports AC1–AC4 as **CI-only**, and they are proven on the E2 PR's run. A skipped local run is reported explicitly, never as a pass.

**Stated costs:**
- The step adds about 3–5 minutes to node-components: four mksquashfs runs over about 1.4 GB, plus one hash of each tree.
- Mounting needs `sudo` on the runner. That is CI's posture, not the Node's.
- The Node never runs this tool. It mounts the image in E2c, through PID1 (C1).

---

# Lane D: the 0017 docs bead (1 bead)

## Bead D-0017: 0017 describes the Node API framing and the API answers

**Goal:** `docs/decisions/0017-node-redesign-r3.md` stops describing r3's shape (an intent mirror, an uplink queue, accounts with no imports, descriptors in `contracts`) and records the owner's protocol framing and Node API answers. Requirements and revisable current choices stay in separate tables. No owner answer is called a decision or a ruling.
**Est.:** 25 min, including its gate. **Depends on:** nothing in wave 1 (docs only; it reads `owner-answers.md`, the Node API page and this file's §0).
**Where it lands:** its own worktree and branch `docs/0017-node-api` off `main`, as a docs-only PR to `main`. `docs/**` is `NOT_SHIPPED` (`scripts/release_plan.py:311`), so no package is due and nothing releases. It runs **alongside wave 1** and must merge **before E3b starts**; the E3 branch rebases onto `main` at the wave-1 checkpoint, so E3b's page cites the updated record.

### Frozen page (the edit, row by row)

Only `docs/decisions/0017-node-redesign-r3.md` changes. Row ids C1–C10 keep their numbers, because `docs/decisions/0015-player-base-layer-and-health-overlay.md:35` cites "C3–C6" for the Node API and must stay true. 0016 is owner-written and is not edited.

| Part | Change |
|---|---|
| Status line | Adds "and the Node API discussion, rounds 1–2, on 2026-10-06". |
| Requirements | **New row S1′**: "Central records every event and every state change it reads from a Node, before and apart from judging it; a refused one is still recorded, with the reason. Method replies and discovery answers go to Central's action log." Source: 0016 "Accepted alongside" (S1), scoped by API5 ("streams-and-state"). Nothing else moves into this table unless Q1 (§9) is answered "requirements". |
| C3, rewritten in place | **The Node serves its own API over NATS; Central is one client.** Each Node runs a NATS server on loopback, owns every bucket and stream on it, and dials one leaf link to a hub in Central's deployment unit, over WebSocket through the origin. The hub holds one account per Node, with Central as a client inside it, plus one wall-wide account (C14). No Node id appears in any subject. Bulk bytes stay on HTTP. Source: gate r3 OQ1 ("nats-leaf"); protocol framing, chat 2026-10-06; API2 note. |
| C4, rewritten in place | **Plug-in model.** Each component owns its own micro service (its methods), its buckets and its streams, declares them itself, and announces itself through `$SRV.INFO` and a birth event carrying its name, version, pipe, release digest and schema majors. Descriptors ship with each release, not in shared `contracts`. The API never judges and never gates. Source: artifact comment 2026-10-05; gate Q2 note; protocol framing. |
| C5, rewritten in place | **The NATS rules are held by structure.** Only `nodeapi` imports the NATS client (import contract, landed in E1). Every connect re-declares. Nothing on a main path waits for a publish acknowledgement. The bus is fenced at 64 MiB with a 12 MiB store, and the Node server itself refuses a stream past the store limit or without a byte cap (E3a-3 probes it). Every event carries its boot, so Central keys its read position per Node, boot and stream. Source: gate r3 OQ1 and the measured spike; API8. |
| C6 rule 2 | "Central stores a fact before judging it" becomes "Central records an event or state change before judging it (S1′)". |
| **C11** (new) | **Two kinds of operation, split by lifetime.** Methods (micro service endpoints) for one-shot and live actions: reboot, Identify, calibration preview, resync. Desired-state documents, with reported status, for anything that should stay true: app release, Output layout, Show per Frame, mute, retention. Every change on a Node emits an event, whoever caused it, so a reply is never the only signal; documents and state are readable by any client. Source: API1 and its note. |
| **C12** (new) | **Central writes every held major.** Central writes each document in every schema major a held release reads, current and rollback, until the old release leaves. Source: API3. |
| **C13** (new) | **The Node holds its events.** Central reads them with its own durable consumers and acknowledges after it records. There is no hub buffer, no hub intent mirror and no direction filter. Five retention classes (record, observation, state, desired, wall copy) share a 12 MiB store inside the 64 MiB bus line. Each component sets its topics' defaults; Central may override per Node through a retention key in that component's desired bucket. A loss before Central reads is a counted gap. Source: API4 note; API5; API8. |
| **C14** (new) | **Wall-wide data.** A wall-wide account at the hub holds one stream that every Node account imports; each Node keeps a read-only local mirror. Broadcast is reference data, never a command: a Frame acts on a wall-wide value only when its own per-Node document names it. Source: API2 note; API6. |
| **C15** (new) | **Show timing rides NATS; clocks ride NTP.** Timing documents (Run anchors, cue instants, the house calendar) travel in the wall-wide stream; each Node's clock follows NTP (C7); there is no sync beat on the bus. Source: API7. |
| **C16** (new) | **Write order.** A write is ordered by the Node's per-key revision with a conditional update (expected revision), and records its writer. The Node never refuses. When another client changed a value, Central adopts the Node's value as the new truth and shows the change to the operator. Source: API9 (chat, after the round-2 page). |
| **C17** (new) | **Lead time and late join.** Central writes a document naming a time at least a lead time ahead and watches each Frame's horizon. A Node that is late keeps last good with a late fault on that Frame only, then joins the Run at its current position. Source: API10. |
| Open (new short list) | The design's recommendations the owner has not answered, so recorded as open, never as choices: presence from link events plus `$SRV.PING` with no heartbeat key; a read-only view of each component's own desired bucket in `nodeapi` (F4); a reserved Content method for bytes by digest (F5); Central as the only writer of desired state for now. |
| What this replaces | **New row:** r3's Node API shape (an `INTENT` bucket at the hub mirrored to each Node, an `UPLINK` queue at the hub, leaf direction filters, one object table and namespace descriptors in `contracts`, a `describe` state key) → C3–C5, C11–C14. **New row:** 0016's S1 sentence, as scoped → S1′. |
| Costs | **Replace** "Three NATS mechanisms are still unprobed …" with: the WebSocket leaf, same-domain isolation and the wall-wide mirror across accounts and the leaf are unprobed until E3a; each has a fallback, and each fallback stops for the owner. **Add:** Central keeps one adapter per schema major still in the field (C12). About 10–19 hours of as-run survive Central's absence; longer gaps are counted holes, and a reboot while Central is away loses the unread tail, count unknown (F7). "A Node never invents intent" moves, for desired buckets, from construction to library plus test (F4). One console prompt when another client changed a value (C16). |
| History | One line: "2026-10-06, Node API rounds 1–2: the owner's framing inverted r3's Central-owned intent into a Node-served API (C3–C5 rewritten; S1′; C11–C17)." |

### Files touched

`docs/decisions/0017-node-redesign-r3.md` only.

### Acceptance

| AC | Criterion | Proved by | Mutation probe |
|---|---|---|---|
| 1 | Every recorded answer names its source id: `for id in API1 API2 API3 API4 API5 API6 API7 API8 API9 API10; do grep -q "$id" docs/decisions/0017-node-redesign-r3.md \|\| echo "missing $id"; done` prints nothing | G-docs grep | Delete row C16 → "missing API9" → red |
| 2 | No owner answer is called a decision or a ruling: `grep -niE "decision\|decided\|ruling" docs/decisions/0017-node-redesign-r3.md` prints only C6's existing "another component's decision at runtime" | G-docs grep | Write "API6 decision" into C14 → a second match → red |
| 3 | Requirements and current choices stay apart: the requirements table gains only S1′ (or, if Q1 is answered "requirements", S1′ plus N16–N19); every new current-choice row names the owner answer it comes from | reviewer read against `owner-answers.md` | Move C13 into the requirements table → reviewer flags → red |
| 4 | Links resolve, and 0015:35's "C3–C6" still points at the Node API rows | G-static `check_docs.py`; reviewer read | Point the 0016 link at `0016-missing.md` → `check_docs.py` red |

**Stated costs:** until D-0017 merges, `main`'s 0017 describes r3's shape. The architecture docs (`docs/architecture.md`, the fleet implementation map) are not touched here; E3's milestone docs bead updates them after E3e (F6).

---

## 5. Lane disjointness (wave 1)

| Path | E3a | E2a | E2b | D-0017 |
|---|:-:|:-:|:-:|:-:|
| `contracts/node_link.py` (new) | ✓ (E3a-1) | | | |
| `central/fleet/node_bus_accounts.py` (new) | ✓ (E3a-1) | | | |
| `appliance/bus/node-bus.conf` (new) | ✓ (E3a-1; E3a-2/3 only by erratum) | | | |
| `scripts/nats_server.py` (new) | ✓ (E3a-1) | | | |
| `tests/integration/bus_servers.py`, `test_node_bus_seam.py` (new) | ✓ (all three, serially) | | | |
| `tests/integration/test_node_bus_limits.py` (new) | ✓ (E3a-3) | | | |
| `tests/test_node_bus_config.py` (new) | ✓ (E3a-1) | | | |
| `pyproject.toml`, `uv.lock` | ✓ (E3a-1) | | | |
| `.github/workflows/checks.yml` | ✓ (E3a-1) | | | |
| `appliance/kernel/capacity.py` | | ✓ | | |
| `appliance/systemd/*` (6 files) | | ✓ | | |
| `appliance/apps/process_linux.py` | | ✓ | | |
| `tests/node/apps/test_memory_lines.py` (new) | | ✓ | | |
| `tests/test_node_pid1.py` | | ✓ | | |
| `scripts/build_environment_image.py` (new) | | | ✓ | |
| `tests/node/apps/test_environment_image.py` (new) | | | ✓ | |
| `.github/workflows/node-components.yml` | | | ✓ | |
| `docs/decisions/0017-node-redesign-r3.md` | | | | ✓ |
| **`tests/conftest.py`** | first entry of `CI_SKIP_ALLOWLIST` (`:41`) | | last entry | |
| **`scripts/release_plan.py`** | `NOT_SHIPPED` (`:310`) | | `PACKAGES` (`:258`) / `SUITES` (`:393`) | |
| `scripts/build_node_pid1_fixture.py` | | | (not touched in wave 1; W1 moves it to E2c) | |

**Verdict: the four lanes are disjoint except two files, where E3a and E2b each take a fixed, separated hunk.** The re-cut moved no file between lanes; it renamed E3a's contracts module (`node_bus.py` → `node_link.py`), renamed its config test, and added one E3a-only test file.
- E2a ∩ E2b, E2a ∩ E3a, D-0017 ∩ any lane: no shared file.
- E3a ∩ E2b: `tests/conftest.py` (first versus last tuple entry, 10 entries apart) and `scripts/release_plan.py` (`NOT_SHIPPED` at 310 versus `PACKAGES` at 258–300 and `SUITES` at 393–436). Git merges both cleanly when E3 rebases onto `main` after E2 merges.
- Inside E3a, the three beads share the harness and the seam test; they are serial in one worktree, so they never conflict.
- Shared by directory only: `tests/node/apps/` (E2a and E2b); `tests/integration/` (E3a's new files beside the existing compose files).

**Shared but read-only:** `appliance/apps/environment.py` (E2b imports `stage_archive` and `verify_root`); `scripts/node_build_inputs.py` and `scripts/debian_packages.py` (E2b imports `docker_build`, `BUILDER_IMAGE`, `PIN`); `owner-answers.md` and the Node API page (D-0017 reads them).

**Serial points:** E2a before E3c (the bus line). E3a-1's `scripts/nats_server.py` before E3c and E3d reuse it. E2a and E2b before E2c. **D-0017 before E3b.**

---

## 6. The tracer's failure branches

"Pre-approved" means the fallback keeps every owner answer and changes only `nodeapi` code, a configuration value or a test: the lane switches in-bead, appends an erratum and asserts the fallback form. "Stop" means the fallback changes the frame: the lane saves `wip/E3a-n` and the question goes to the owner.

| # | Probe that fails | Where it shows | Fallback | Cost | Kind |
|---|---|---|---|---|---|
| 1 | The WebSocket leaf (credentials in the `ws://` URL, the path prefix, or JetStream traffic over a ws leaf) | E3a-1 AC1 | A TCP leaf on the hub's port 7422 | A new LAN port on Central: compose now, iac in E4. C5 reversed | **Stop** (owner, wave-1 Q2) |
| 2 | Same-domain isolation: two leaves with domain `node` in different accounts collide, or `$JS.node.API` misroutes | E3a-1 AC3 | A per-Node domain `node-<account_id>`, from one more variable in `node-bus.conf`; Central addresses each Node's domain | The Node names something Node-specific beyond its leaf user | **Stop** (owner, wave-1 Q2) |
| 3 | The wall-wide mirror: no flow across the account import and the leaf, a stall on flow control (10228), or a local publish landing in it | E3a-1 AC4 | API6 (c): Central writes the same document into each Node's own desired bucket (N writes) | No one-message broadcast; Central tracks one write per Node; the generator loses the WALL account; C14 changes | **Stop** (the page names it) |
| 4 | `$SRV.PING`/`$SRV.INFO` do not cross the leaf while endpoint requests do | E3a-1 AC1 | Discovery from the birth event only (it carries the same descriptor) | Central loses the on-demand "now" view; presence rests on link events and last heard | Pre-approved (the page's redundancy) |
| 5 | A request to an absent Node times out instead of "no responders" | E3a-1 AC2 | Central treats a short method timeout as absent | Slower failure in the console | Pre-approved (Central side only) |
| 6 | The domain-prefixed KV write does not land, or the expected-revision header is not honoured across the leaf | E3a-2 AC1 | Desired writes become a method on the owning component's service, which does the conditional update locally and replies with the revision | API9's mechanism moves from the server into every component | **Stop** |
| 7 | Durable-consumer acknowledgements do not cross the leaf (`$JS.ACK` is outside the leaf deny list, `ns:server/jetstream_api.go:371`, so this is unlikely) | E3a-2 AC2 | Central reads through an ordered, unacknowledged consumer from its own recorded high-water mark per (Node, boot, stream) (F3's form) | The read position lives only in Central's records | Pre-approved |
| 8 | A reload drops other accounts' leaf links (A2) | E3a-2 AC4 | Accept a reconnect of about 1 s per enrollment; E3d batches reloads | Each enrollment blips every link; the show never rides the bus | Pre-approved |
| 9 | The reload request through the system account is unsupported or ignored | E3a-2 AC4 | SIGHUP from a process sharing the hub's PID namespace | E3d's mechanism changes; iac touched in E4 | Stop for E3d's page, not for E3a (E3a records it) |
| 10 | The store limit: `12MB` refuses the page's split (overhead), `max_mem: 0` does not refuse memory streams, or the `API` account with `no_auth_user` misbehaves | E3a-3 AC1, or E3a-1's first declare | 16 MiB store (inside A3's estimate under the fence); a 1 MB memory cap plus a config-test rule that no stream uses memory; back to the default account with the byte-cap rule in `nodeapi` | 4 MiB of fence margin, or the byte-cap guarantee moves from the server to the library | Pre-approved (configuration) |
| 11 | The wall mirror stalls after the hub's store reset (the origin's sequence restarts at 1) | E3a-2 AC5 | `nodeapi` deletes and re-creates the mirror when the origin's sequence goes backwards; if that also fails, row 3 | One `nodeapi` rule, a re-mirror of 0.5 MiB | Pre-approved |
| 12 | A class breaks its promise (a state bucket sized with the per-message term still fills; age not enforced) | E3a-3 AC2 | A sizing margin in `nodeapi`'s class table | Bytes inside the 12 MiB | Pre-approved (numbers) |
| 13 | nats-py cannot read the mirror's latest value per subject | E3a-1 AC4 | An ordered consumer delivering the last message per subject, inside `nodeapi` (A2b's form) | `nodeapi` code only | Pre-approved |

Any probe that fails stops `nodeapi` (E3b) from starting until the branch is resolved. That is the tracer's purpose.

---

## 7. Waves 2–4 (not cut; one line each)

- **Wave 2:** E3b `nodeapi` (after D-0017 merges) ∥ E2c (stage in place through a PID1 mount unit; plus W1's flip: shipped `.squashfs`, `components.json`, fixture, `base_abi`; preparation slice to the root lines; the content-line check; E2's docs bead). E2 merges to `main` when green.
- **Wave 3:** E3c (bus unit, 64 MiB fence, pinned binary in the base and the fixture) ∥ E3d (hub in compose and the fixture, accounts on the boot offer with "retry later", reload, Central's per-Node client sessions).
- **Wave 4:** E3e, the join: in node-pid1 `success`, Central calls one method, writes one desired document and records one event through the real stack, and the wall mirror holds one value. Then E3's docs bead and the milestone coherence review.

**What later waves must carry** (one line each; not cut here):
- **E3b, envelope:** every document records its writer and every event its boot and caller (API9, N16), in one envelope built only inside `nodeapi`.
- **E3b, conditional updates:** the hub role writes desired values on `$JS.node.API.$KV.<bucket>.<key>` with the expected revision (W9); on 10071 it adopts the Node's value and raises the operator view; the Node side never refuses.
- **E3b, events on every change:** each accept, refuse, state change and method effect emits an event naming its caller, so no reply is the only signal (N16).
- **E3b, retention classes:** the five classes as one `nodeapi` table, sized with the per-message term E3a-3 proves, every declare capped; a retention desired key applied as a stream update, and a server refusal kept as old limits plus a `retention_refused` event.
- **E3b, birth and `$SRV` registration:** each component registers its micro service (name, semver, instance id; metadata pipe, release digest, schema majors) and emits a birth event on every start; `resync` re-emits it.
- **E3b, Central's read:** a durable consumer per Node stream that records a gap before later messages and acknowledges after commit, with the high-water mark keyed per (Node, boot, stream) (F3, F7).
- **E3b, read-only views:** a component gets a read-only view of its own desired bucket, and the wall mirror is declared from `contracts/node_link.py` (F4).
- **E3c:** a `BUS_STORE_BYTES` constant beside the line table, bound by test to `node-bus.conf`'s two 12 MB values; the fence measured with the full store on the Pi's 16 KiB pages (A3).
- **E3d, wall-wide account:** the hub runs JetStream for `WALL` only; Central declares `WALL` (latest per subject, discard new, `WALL_STREAM_BYTES`) and re-puts it on every connect; each reload carries every Node account's WALL imports.
- **E3d, presence:** leaf connect and disconnect from the hub's system events per account, plus `$SRV.PING` (the page's Probe D, moved out of E3a: its fallback changes nothing in the frame).
- **E3d/E4, ingress:** a route for the leaf's path prefix plus `/leafnode`; the `leafnodes` TCP listener stays on loopback.
- **E5:** API9's console prompt and API10's lead-time and horizon rule. **E8:** API7 (timing read from the wall mirror; no beat).

---

## 8. Time

| Lane | Beads | Build + one verify each |
|---|---|---:|
| E3 | E3a-1 45, E3a-2 35, E3a-3 25 | 105 min |
| E2a | E2a-1 35 | 35 min |
| E2b | E2b-1 40 | 40 min |
| D | D-0017 25 | 25 min |
| **Serial sum** | 6 beads | **≈ 3.4 h** |

**Wall-clock:** four parallel worktrees, so the E3 lane (105 min) plus one CI round sets it.
- CI rounds: about 12 min for `checks` on the E3 branch; node-components plus 5 node-pid1 legs on the E2 branch take 25–35 min cold, but start after 40 min, so they finish inside the E3 lane's time.
- One fix cycle in one lane adds about 20 min.
- Expected **about 2–2.3 h**, inside the 3 h ceiling; the 2.4 h warning fires only with a fix cycle in the E3 lane.

The previous cut was 70 min for E3 and about 1.5–2 h wall-clock. The page estimated E3a at 1.5–2 h; this cut lands at 1.75 h of build.

## 9. Owner questions (before the run)

Wave-1 Q1 and Q2 are answered (applied in W4 and §6 rows 1–2).

- **Q1. Do N16–N19 bind as requirements?** These are the Node API page's four rows written from your round-1 notes: every change emits an event (N16); documents and state are readable by any client (N17); each Node has its own namespace, and Central can broadcast wall-wide data in one message (N18); the Node holds its own events, retention tuned per topic (N19). The page asked whether they put your notes in the right words, and that question is unanswered. D-0017 needs to know which table they go in.
  - **Recommended: requirements.** Each one is your stated reason for the framing. Each is cheap to hold, and each is already carried by a later wave (§7).
  - **Alternative: revisable current choices** (C11, C13, C14 in 0017). This is D-0017's default while the question stays unanswered.
