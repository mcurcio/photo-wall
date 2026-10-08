# Run brief: wave 3, E3d (Central's side of the Node API)

**Design:** `/Volumes/Dock/tmp/node-redesign/e3b-design.md`, revision 3 (approved). E3d's share is §15's row "Hub runner in compose and the fixture, the shared account volume and its regenerate-and-reload triggers, retirement, both NodeLink supervisors, DB tables (raw records, gap rows, cursors, own tokens, action log, WALL mark), WallWriter wiring, ingress route for the leaf (with E4), presence", with §7.2 (rows "Hub runner and Fleet generator", "NodeLink supervisors", "`nodeapi.hub`: NodeLink / WallWriter"), §7.4, §9.1, §9.3, §9.6, §9.7, §11 and §13. An implementer reads **its slice page below plus the design sections it names**, nothing else.
**Base:** E3b landed on PR 49 (`claude/wave1-node-api` at 3e1baf6): `nodeapi.hub.NodeLink` and `WallWriter` as a library, recorded by the test double `tests/integration/bus_servers.py::FileLinkStore`. Run brief `.claude/runs/wave2-e3b.md`; errata `grep -a 'E-E3B' .claude/errata.md` (FR1-FR4 included).
**Branch:** `claude/wave3-e3d`, one bead per slice, landed in order. **Rules:** `~/.claude/skills/implementation-workflow/SKILL.md` (binding).
**Scope:** E-E3B-FR3 (first, alone), Central's PostgreSQL link store and WALL mark, both NodeLink supervisors, Fleet's hub keeper (configuration file, reload twice, WALL, enrolment and retirement), the leaf's way in through Central's origin, presence on the console, and the hub as a Compose service beside a worker that runs all of it. The Kubernetes change is written down below for an iac PR; nothing is applied to a cluster.
**Not in scope:** `appliance/` (E3c), the bus unit and the node-pid1 image (E3c), the join and the hub in the node-pid1 fixture (E3e, erratum E-E3D-CUT-5), document contents and projections (E5), the wall table (E8), switch-over (E9).

## Owner answers and rules (binding)

- Q1 = start clean (memory store, every bus and hub start is empty); Q2 = per-Node retention override removed; Q3 = `WALL_STREAM_BYTES` frozen.
- The Node is self-sufficient; Central is a client. UX over security: no security mechanisms (no auth on the bridge, selector users only).
- A finding is a defect only if it happens in normal operation or the Node cannot recover by itself. RAM is not a limiting factor.
- Integration tests of functional requirements with real servers (nats-server, PostgreSQL, uvicorn); no unit tests of internals. A unit test only where a table, wording or configuration is the thing under test.
- Make failure classes impossible rather than patching instances. Right-size; DRY/SOLID.
- Deploys only through an iac PR the owner merges. Never apply anything to a cluster; the deployment change is written down (§ "Kubernetes change" below), not applied.
- **Report where the spec or plan is wrong:** append to `.claude/errata.md` (append-only; read with `grep -a`) with ids `E-E3D-S<n>-<k>`, then STOP if the frozen page cannot be met. A page changes only through an erratum and a re-cut.

## Environment

- Checkout `/Volumes/Dock/Home/Code/photo-wall/.claude/worktrees/wave3-e3d`; always `/Volumes/Dock/Home/Code/photo-wall/.claude/worktrees/wave3-e3d/.venv/bin/python` (written `.venv/bin/python` below, run from the checkout); never another worktree's venv, never `uv run`.
- `uv.lock` stays unchanged in every slice except S5, and in S5 only by the gated 4-line move (erratum E-E3D-CUT-3). Anything else that rewrites it is reverted by `cp` from a backup.
- `source ~/.nvm/nvm.sh && nvm use 20` before any test run.
- `PHOTO_WALL_NATS_SERVER=/Volumes/Dock/tmp/nats/nats-server-v2.15.0-darwin-arm64/nats-server`. Fence server (not needed by E3d): `PHOTO_WALL_BUS_FENCE_SERVER`, see `tests/integration/test_bus_memory_fence.py`.
- DB: `docker compose -f tests/integration/compose.test-database.yml up -d --wait`, then `.venv/bin/python scripts/test_local.py …`.
- pytest `--basetemp=/Volumes/Dock/tmp/w3bt/<slice>/<gate>` (short: AF_UNIX sockets); `TMPDIR=/Volumes/Dock/tmp/w3bt/<slice>`; `-n 4` wherever the tests allow it.
- Probes restore by `cp` from a backup, never `git checkout`, `stash` or `reset`.
- **New test files are named `tests/integration/test_central_*.py`, never `*node_bus*`:** they need the database, so they belong to the DB tier (`-m db`, derived from the `database` fixture), and CI's `node-bus` job (`-k node_bus`, no database) must not collect them. S1 adds the pinned nats-server fetch to CI's `db` job so they run there.

### Gates

| Gate | Command | Slices |
|---|---|---|
| G-static | `.venv/bin/python -m ruff check .` · `.venv/bin/lint-imports` · `python3 scripts/check_docs.py` | all |
| G-unit | `PHOTO_WALL_TEST_REQUIRE_DATABASE=1 .venv/bin/python -m pytest -q -m "not db and not browser" -n 4 --dist worksteal --basetemp=/Volumes/Dock/tmp/w3bt/<slice>/unit` | all; baseline failures `/Volumes/Dock/tmp/w1/baseline.txt` (2, environmental) |
| G-bus | `PHOTO_WALL_NATS_SERVER=… .venv/bin/python -m pytest -q tests/integration -k node_bus -n 4 --basetemp=/Volumes/Dock/tmp/w3bt/<slice>/bus` | all code slices (E3b's suite stays green) |
| G-db | `PHOTO_WALL_NATS_SERVER=… .venv/bin/python scripts/test_local.py -q -m db -n 4 --dist loadgroup --basetemp=/Volumes/Dock/tmp/w3bt/<slice>/db` | S1-S5 |
| G-console | `cd central/console && npm ci && npm run build`; the console tests under G-unit | S4 |
| G-compose | `python3 scripts/configure.py` once; `docker compose build && docker compose up -d --wait`, the S5 page's hub checks, `docker compose down --volumes`. Registry pulls may fail: retry, then name CI's `image-smoke` as the gate | S5 |

One full verify per bead (all its gates once, unpiped). The milestone gate is the lane PR's CI run after D1.

### Preflight (2026-10-08, at `3e1baf6`, clean tree)

| Check | Result |
|---|---|
| Runtime | node v20.20.2 via nvm; Python 3.12.11 in the lane `.venv`; `nats` and `websockets` 15.0.1 importable |
| Server | `nats-server: v2.15.0` (darwin-arm64) at the path above |
| Docker | 29.8.1, aarch64; the test database container `photo-wall-test-database-test-database-1` is up |
| G-static | ruff 0, lint-imports 20 kept / 0 broken, check_docs 0 |
| G-bus | 38 passed, 1 xfailed (the strict reload xfail, E-W1-TD-S3), 57 s |
| G-unit, G-db | not re-run at this tip; the unit baseline file is from `0638f10`. **Re-run both once at launch** and record the failure sets here before S0 |
| Models | pin by id at launch (skill §6.7): implementers on the approved Sonnet id, review lenses and the course-correction architect on the approved Opus id |

### Budget gates

- Per bead: wall-clock 120 min for S1, S2 and S5; 90 min for S0, S3, S4; at most 8 agents.
- Feature: 14 h wall-clock and 8 M tokens; warning milestone at 80 %, stop after the current bead at 100 %.
- At most two fix cycles per gate; then save to a pushed `wip/e3d-s<n>` branch and stop. Two consecutive stops on different slices = re-cut before any further implementation.
- A course-correction architect pass after S3 (owner memory: at least every 5 implementers) and before D1.
- Review lens per slice (skill §2.3), one each, on the diff: S1 correctness/data loss (migration, records); S2 NATS (hub reload, WALL); S3 regression (a new route on Central's public app); S4 verify only (additive migration, read-only field); S5 deployment/regression (dependency, image, compose).
- **S5 is gated** (erratum E-E3D-CUT-3): it starts only once the orchestrator approves the 4-line `uv.lock` change, or E3c has landed the identical move first (then S5 drops that part).

## Ledger

| Bead | Status | Sha | Note |
|---|---|---|---|
| S0 the outbox drop count reaches Central whenever it changes (E-E3B-FR3) | open | | |
| S1 tracer: every enrolled Node's bus recorded in PostgreSQL | open | | |
| S2 Fleet keeps the hub: configuration, reload, WALL, enrolment, retirement | open | | |
| S3 the leaf reaches the hub through Central's origin | open | | |
| S4 presence: the console shows whether a Node's leaf is linked | open | | |
| S5 the hub in Central's deployment (gated) | open | | |
| D1 docs: deployment note for iac, runbook, 0017, code map | open | | after integration; not a slice |

## Slice order and why

```
S0 ─▶ S1 ─▶ S2 ─▶ S3 ─▶ S4 ─▶ S5 ─▶ D1
```

- **S0 first, alone** (lane instruction): a `nodeapi/node.py` class fix with no Central code.
- **S1 is the tracer:** the riskiest seam is where a Node's facts become Central's records (loss, duplication), and the boundary where `central` first imports `nodeapi` (E-E3D-CUT-1/-2). It runs both real supervisors on real servers, with the hub configured by the existing generator, so it needs nothing from S2.
- **Package depth:** S1 and S2 each add to `nodeapi/hub.py` the connection-owning entries Central needs (Central may not import nats, so it cannot build the `Client` NodeLink and WallWriter take; E-E3D-CUT-2), then the Central modules that use them. S3 is `central/app.py` plus one `contracts` constant. S4 is `central/fleet` plus the console. S5 is deployment only.
- **S5 last** so the dependency question (E-E3D-CUT-3) blocks nothing earlier: every earlier slice runs its servers in-process under the dev environment, where nats-py already is.
- **Docs after integration** (D1), never inside a code bead.

## Shared primitives (frozen when first cut)

| Primitive | Frozen in | Named consumers |
|---|---|---|
| `central.infra.node_link_store.PgLinkStores` / `PgLinkStore` (satisfies `nodeapi.hub.LinkStore`) | S1 | the fleet `NodeLinks`; the show `NodeLinks`; E5's projections read its tables |
| `central.infra.node_links.NodeLinks` (one supervisor, the pipe a parameter) | S1 | the fleet pipe's links; the show pipe's links (both in S1's test, S2's `FleetHub`, S5's `NodeBus`) |
| `nodeapi.hub.run_link` | S1 | `NodeLinks` (fleet); `NodeLinks` (show) |
| `nodeapi.hub.HubAdmin` | S2 | `FleetHub` reload and identity (S2); `linked()` for the retirement check (S2) and presence (S4) |
| `nodeapi.hub.HubUnavailable` | S2 | `FleetHub` around `HubAdmin` and `WallWriter`; E5's wall writes later |
| `central.fleet.node_bus_hub.enrolled_serials_in` | S2 | `FleetHub` configuration and link tracking (S2); presence rows (S4) |
| `contracts.node_link.LEAF_PATH` | S3 | Central's leaf bridge (S3); E3c's leaf URL on the Node |

`FileLinkStore` stays the E3b tests' double; it and `PgLinkStore` are two implementations of one protocol (maintenance risk below).

## Module layering (S1 changes the contracts)

- `pyproject.toml` contract "Only the Node API library talks to NATS" gains `allow_indirect_imports = true`: the direct ban stays, and `central` (and later `appliance`, E3c) reach NATS only through `nodeapi`. Without it `central -> nodeapi.hub -> nats` is reported (probe, E-E3D-CUT-1).
- New forbidden contract "Central uses only the hub role of the Node API library": `source_modules = ["central"]`, `forbidden_modules = ["nodeapi.node"]`.
- New Central modules: `central/infra/node_link_store.py` and `central/infra/node_links.py` (in the "Central content-serving layers" contract's `central.infra` layer; they import `central.db`, `central.content_catalog.catalog` and `nodeapi` only), `central/fleet/node_bus_hub.py`, `central/fleet/leaf_bridge.py`, `central/node_bus_wiring.py` (composition, beside `content_wiring.py`).

---

# S0 — the outbox drop count reaches Central whenever it changes (E-E3B-FR3)

**Goal.** A Node session's `outbox` state key always converges to the number of events its full outbox dropped, without waiting for the next attach. The class fix: the count is ordinary held state, so every path that publishes held state carries it.
**Design:** §7.2 `nodeapi.node` ("overflow drops oldest and is counted in state"), §11 row "Outbox full while the bus is down"; erratum E-E3B-FR3.
**Files:** `nodeapi/node.py`; `tests/integration/test_node_bus_api_recovery.py`.

**Frozen (no signature changes):**

```
# nodeapi/node.py — behaviour only
# NodeSession holds OUTBOX_KEY in its held state from construction: {"dropped": 0} (compact JSON, as today).
# Every change of the drop count re-holds OUTBOX_KEY = {"dropped": n} and marks it dirty:
#   _emit on a full outbox (count + 1) and _done's take-back of an acknowledged in-flight event (count - 1).
# The publisher publishes a dirty OUTBOX_KEY only when no event is queued in the outbox, so an overflow
#   costs no event throughput; every other dirty state key keeps its precedence over events.
# _attach re-puts the held state (which now includes OUTBOX_KEY) and birth; no separate outbox put.
# State.put still refuses OUTBOX_KEY and BIRTH_KEY ("state_key_reserved").
```

**Test (functional, real servers):** new `test_an_outbox_that_overflows_while_attached_is_counted_in_state` beside the existing outbox test, on the same `_bed`:
1. The display session is attached and the fleet link has recorded its birth and `outbox` = `{"dropped": 0}`. Precondition asserted as in the existing test: `OUTBOX_MESSAGES` such events fit `RECORD_display`'s cap, so only the outbox can lose one.
2. `bed.node.pause()` (SIGSTOP: the connection stays, no reattach), emit `OUTBOX_MESSAGES + 100` events, `bed.node.resume()`.
3. Within 30 s: the newest `outbox` record in Central's store, plus the number of `RECORD_display` records of this epoch, equals the events emitted in this epoch; its `dropped` is at least 99 (one may have been in flight and acknowledged, E-E3B-S6-2 a); the state stream holds exactly one `birth` record in this epoch (no reattach happened).

**Acceptance:** G-static, G-unit, G-bus green; the existing `test_an_outbox_that_overflows_while_the_bus_is_down_is_counted_in_state` still asserts exactly one `outbox` record `{"dropped": 100}` in the new epoch (attach settles the dirty put of the same value); `node.py` delta expected under 30 lines.
**Mutation probe:** stop re-holding `OUTBOX_KEY` when the count changes (put it only at attach, as before): the new test fails with Central's newest `outbox` still `{"dropped": 0}`.

---

# S1 — tracer: every enrolled Node's bus recorded in PostgreSQL

**Goal.** Central's two real link supervisors (fleet, show) record a Node's events, state and Central's own document writes in PostgreSQL through `nodeapi.hub.NodeLink`: each message once, losses as gap rows, the two pipes apart, a bus restart's unread tail as one "unknown" row per stream, records kept when a Node's links stop.
**Design:** §4 rules 1 and 3, §7.2 rows NodeLink and "NodeLink supervisors", §7.4, §8 (b), §9.1, §9.3, §9.5, §11 rows "Central crash mid-drain", "Rolling Central deploy", "Event buffer full while Central is away", "Bus crash".
**Files:** `central/migrations/066_node_link_records.sql` (next free number at implementation time); `central/infra/node_link_store.py`; `central/infra/node_links.py`; `nodeapi/hub.py` (additive); `pyproject.toml` (import-linter only); `.github/workflows/checks.yml` (`db` job: fetch the pinned nats-server, the same step as the `node-bus` job); `tests/integration/test_central_node_links.py`.

**Frozen:**

```sql
-- central/migrations/066_node_link_records.sql
-- E3d: what Central read from each Node's bus (E3b design §7.4, §9.3; nodeapi.hub.LinkStore). A message is
-- keyed by (Node, stream, epoch, sequence), so a repeat is a no-op. Times are Central's own clock only.
CREATE TABLE node_link_records (
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    stream TEXT NOT NULL,
    epoch TEXT NOT NULL,
    seq BIGINT NOT NULL CHECK (seq > 0),
    subject TEXT NOT NULL,
    message_id TEXT,                         -- the envelope's message id (events); NULL on state
    headers JSONB NOT NULL,                  -- every header as read (schema major, writer)
    data BYTEA NOT NULL,
    recorded_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (device_id, stream, epoch, seq)
);
CREATE TABLE node_link_gaps (
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    stream TEXT NOT NULL,
    epoch TEXT NOT NULL,
    after_seq BIGINT NOT NULL CHECK (after_seq >= 0),   -- the last sequence read before the gap
    lost BIGINT CHECK (lost > 0),                       -- NULL: unknown, the epoch ended
    recorded_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (device_id, stream, epoch, after_seq)
);
CREATE TABLE node_link_cursors (
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    stream TEXT NOT NULL,
    pipe TEXT NOT NULL CHECK (pipe IN ('fleet', 'show')),
    epoch TEXT NOT NULL,
    seq BIGINT NOT NULL CHECK (seq >= 0),
    PRIMARY KEY (device_id, stream)
);
CREATE TABLE node_link_documents (           -- Central's own last write per desired key (own tokens)
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    stream TEXT NOT NULL,
    key TEXT NOT NULL,
    digest TEXT NOT NULL,
    epoch TEXT NOT NULL,
    seq BIGINT NOT NULL CHECK (seq > 0),
    PRIMARY KEY (device_id, stream, key)
);
CREATE TABLE node_link_actions (             -- the action log: calls, discovery, refused and adopted documents
    action_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    pipe TEXT NOT NULL CHECK (pipe IN ('fleet', 'show')),
    kind TEXT NOT NULL,
    body JSONB NOT NULL,
    recorded_at DOUBLE PRECISION NOT NULL
);
```

```python
# nodeapi/hub.py (additive; nothing existing changes)
async def run_link(url: str, serial: str, pipe: Pipe, store: LinkStore, documents: DocumentSource,
                   stop: asyncio.Event) -> None:
    """One Node's link on one pipe, as Central's user in the Node's hub account (central_user(serial),
    inboxes under CENTRAL_INBOX_PREFIX): connect, retrying with the NodeLink backoff until `stop`; run
    NodeLink(...).run(stop); connect again if the client closed; close the client on `stop`. The client
    reconnects forever. A store error propagates (Central's process is what failed); a hub that does not
    answer never raises out of here. Callers never see a nats type."""
```

```python
# central/infra/node_link_store.py
LINK_STORE_CONCURRENCY: Final = 4   # store calls in flight at once across every link of a process; the
                                    # rest wait in the event loop, never in the pool's queue (E-E3D-CUT-9)

class PgLinkStores:
    """One per process: every (Node, pipe) link's store over one Database, behind one gate."""
    def __init__(self, db: Database) -> None: ...
    def store(self, serial: str, pipe: Pipe) -> PgLinkStore: ...   # device_id = device_id_for_serial(serial)

class PgLinkStore:
    """nodeapi.hub.LinkStore for one Node and one pipe. Every method runs its SQL in a worker thread
    (asyncio.to_thread) under the PgLinkStores gate; times come from DatabaseTransactionClock."""
    async def cursors(self) -> Mapping[str, Token]: ...
        # this Node's cursors of THIS PIPE only (load-bearing: NodeLink.reconcile ends every cursor whose
        # stream it does not see, so another pipe's cursors must never reach it)
    async def commit(self, stream: str, batch: Batch) -> None: ...
        # one transaction: every Gap as a node_link_gaps row (count None -> lost NULL), every Read as a
        # node_link_records row (message_id via nodeapi.envelope.message_id), both ON CONFLICT DO NOTHING;
        # then the cursor upserted with this pipe (last write wins), or deleted when batch.cursor is None
    async def action(self, kind: str, body: Mapping[str, object]) -> None: ...
    async def own_tokens(self, stream: str) -> Mapping[str, tuple[str, Token]]: ...
    async def wrote(self, stream: str, key: str, digest: str, token: Token) -> None: ...   # upsert
```

```python
# central/infra/node_links.py
class NodeLinks:
    """Every tracked Node's link on one pipe, each a long-lived task (never a job, never a process per
    Node): a serial that joins gets a link, one that leaves has its link stopped; records are kept."""
    def __init__(self, pipe: Pipe, hub_url: str, stores: PgLinkStores, documents: DocumentSource) -> None: ...
    async def track(self, serials: Collection[str]) -> None: ...
        # start run_link for each serial not linked; stop and await each linked serial not listed
    async def run(self, stop: asyncio.Event) -> None: ...
        # until `stop`, then stop every link; raises the first exception a link raised (a store error)
```

```toml
# pyproject.toml [tool.importlinter]
# "Only the Node API library talks to NATS": add  allow_indirect_imports = true   (with a comment why)
[[tool.importlinter.contracts]]
name = "Central uses only the hub role of the Node API library"
type = "forbidden"
source_modules = ["central"]
forbidden_modules = ["nodeapi.node"]
```

**Test:** `tests/integration/test_central_node_links.py::test_central_records_every_node_fact_once` (db; real hub from `bus_servers.hub_server`, a real Node bus, real `NodeSession`s for the `display` line (fleet: records, state, desired) and the `content` line (show), a test projection for `KV_desired_display`; a `devices` row inserted for the serial). Both supervisors track the serial.
1. **Steady, pipes apart:** display emits 50 events and puts state; content emits 20. In PostgreSQL: `RECORD_display` has the 50 in emission order, each message id present and distinct; the state streams hold `birth`; the projection's document is on the Node and its own-token row matches the Node's token; fleet cursors name only fleet streams and show cursors only show streams; no gap rows.
2. **Away and overflow:** `track([])` on both (the links stop; record counts unchanged). Display emits until `RECORD_display` has dropped its oldest. `track([serial])` again: exactly one new gap row for `RECORD_display`, `lost` = (the stream's first sequence − 1) − the last recorded sequence; the remaining events recorded; no (epoch, seq) recorded twice.
3. **Two workers overlap (rolling deploy):** a second pair of `NodeLinks` on a second `PgLinkStores` (its own `Database`) tracks the same serial while display emits 100 more: each recorded exactly once, neither supervisor raises.
4. **Bus kill -9 and restart:** the sessions reattach; exactly one `lost IS NULL` gap row per stream of each pipe that had a cursor; events emitted after the restart are recorded in the new epoch; `birth` is recorded in the new epoch; the document is back on the Node and its own-token row names the new epoch.

**Acceptance:** G-static (with the two contract changes), G-unit, G-bus, G-db green; CI's `db` job sets `PHOTO_WALL_NATS_SERVER`; `rg "import nats|from nats" central` is empty; no nats exception type is caught or named under `central/`.
**Mutation probe:** drop the pipe filter from `PgLinkStore.cursors()`: leg 1 or 4 fails (one pipe's link ends the other pipe's cursors with "unknown" gap rows).

---

# S2 — Fleet keeps the hub: configuration, reload, WALL, enrolment, retirement

**Goal.** One Fleet loop in the worker keeps the hub matched to Central's DB: it writes the hub configuration for the enrolled set to the shared path, reloads the hub twice, re-creates WALL past every mirror with Central's wall documents after the hub restarted empty, and points both link supervisors at the enrolled set. Triggers: worker start, the hub coming back (its server id changed), every enrolment change. Retirement removes the account and stops both links; records stay.
**Design:** §7.2 rows "Hub runner and Fleet generator", "`nodeapi.hub`: WallWriter", "NodeLink supervisors"; §9.6; §9.7 Enrol/Retire; §10; §11 rows "Hub restart or Central deploy", "Hub starts with a stale or missing account file", "Hub reload misses new service imports", "A Node dials before its account exists"; errata E-E3B-FR1 (mirror lag up to about 60 s), E-E3D-CUT-6, -7.
**Files:** `nodeapi/hub.py` (additive, plus `HubUnavailable` from `WallWriter.ensure`/`put`); `central/migrations/067_node_bus_wall.sql`; `central/infra/node_link_store.py` (`PgWallMarks`); `central/fleet/node_bus_hub.py`; `tests/integration/test_central_hub.py`.

**Frozen:**

```python
# nodeapi/hub.py (additive)
class HubUnavailable(Exception):
    """The hub did not answer (down, restarting, refused). Raised instead of any nats error by every
    entry below and by WallWriter.ensure/put; a store error still propagates as itself."""

HUB_RELOAD_REQUESTS: Final = 2   # X6: a reload-added account's service imports are wired only by the second

class HubAdmin:
    """Fleet's system-account client of the hub."""
    @classmethod
    async def connect(cls, url: str, user: str) -> HubAdmin: ...   # reconnects forever once connected
    async def identity(self) -> str: ...            # the running server's id ($SYS.REQ.SERVER.PING.IDZ): new at every hub start
    async def reload(self) -> None: ...             # HUB_RELOAD_REQUESTS reload requests, each answered once applied
    async def linked(self) -> frozenset[str]: ...   # account names holding a leaf ($SYS.REQ.SERVER.PING.LEAFZ)
    async def close(self) -> None: ...

class WallWriter:   # existing; two additions
    @classmethod
    async def connect(cls, url: str, table: KeyTable, marks: WallMarks) -> WallWriter: ...   # as WALL_WRITER_USER
    async def close(self) -> None: ...
```

```sql
-- central/migrations/067_node_bus_wall.sql
-- E3d: the highest WALL sequence Central recorded (nodeapi.hub.WallMarks); WALL is re-created at mark + 1 + K.
CREATE TABLE node_bus_wall (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    mark BIGINT NOT NULL CHECK (mark >= 0)
);
```

```python
# central/infra/node_link_store.py (addition)
class WallDocuments(Protocol):
    async def wall_documents(self) -> Mapping[str, bytes]: ...   # Central's wall documents (E5/E8); none until then

class PgWallMarks:
    """nodeapi.hub.WallMarks over node_bus_wall; wall documents from the injected source."""
    def __init__(self, db: Database, documents: WallDocuments) -> None: ...
    async def mark(self) -> int: ...                       # 0 with no row
    async def record_mark(self, seq: int) -> None: ...     # upsert GREATEST(mark, seq): never lowers it
    async def wall_documents(self) -> Mapping[str, bytes]: ...
```

```python
# central/fleet/node_bus_hub.py
HUB_LISTENERS: Final = HubListeners(            # in-container invariants of the hub service (S5, Kubernetes note)
    server_name="photo-wall-hub", client_host="0.0.0.0", client_port=4222,
    websocket_host="0.0.0.0", websocket_port=8080, leaf_host="127.0.0.1", leaf_port=7422,
    monitor_port=8222, max_memory_store_bytes=4 * 1024 * 1024)
HUB_LOOK_SECONDS: Final = 2.0

def enrolled_serials_in(conn) -> frozenset[str]: ...
    # serials of devices with retired_at IS NULL whose fleet_device_lifecycle.revoked_at IS NULL and serial
    # IS NOT NULL (the predicate FleetService.status uses); a serial account_id refuses is left out, logged

class FleetHub:
    """Fleet's keeper of the hub (one per worker process)."""
    def __init__(self, db: Database, hub_url: str, config_path: Path, listeners: HubListeners,
                 wall_table: KeyTable, marks: WallMarks, links: Sequence[NodeLinks]) -> None: ...
    @property
    def wall(self) -> WallWriter | None: ...   # for E5/E8's wall writes; None until connected
    async def run(self, stop: asyncio.Event) -> None: ...
        # Every HUB_LOOK_SECONDS until `stop`:
        #  1. serials = enrolled_serials_in (DB; a DB error propagates)
        #  2. text = hub_configuration(serials, listeners); if it differs from the file's bytes (or no file):
        #     write it atomically (temp file in the same directory, then os.replace), mode 0644
        #  3. connect HubAdmin (FLEET_SYSTEM_USER) and WallWriter if not connected
        #  4. if the file changed this look, or identity() differs from the last seen (worker start, hub
        #     came back): reload(); then wall.ensure()
        #  5. track(serials) on every NodeLinks
        # HubUnavailable at 3 or 4: log, keep the file, retry next look (links are still tracked in 5).
        # On `stop`: close HubAdmin and WallWriter.
```

**Test:** `tests/integration/test_central_hub.py::test_fleet_keeps_the_hub_with_the_enrolled_set` (db; `FleetHub` with loopback `HubListeners` on free ports, a temp `config_path`, a test `WallDocuments` holding `timing`, both `NodeLinks`; the test starts nats-server from `config_path` only once the file exists, as the Compose hub does; Nodes from `bus_servers.node_server` each with a `display` session that reads WALL).
1. **Worker start:** one enrolled device; empty config dir. The file appears listing its account; the hub starts from it; the Node's leaf links; Central records its `birth`; WALL's first sequence is 1 + 1024 (mark 0) and the Node's wall view holds `timing`.
2. **Enrol while running:** insert a second device. Within 10 s the file lists its account; the second Node links, its wall view holds `timing` (the new account's imported WALL consumer API answers: the second reload), and its `birth` is recorded.
3. **Hub restart, empty:** kill -9 the hub and start it from the same file. Within 60 s (E-E3B-FR1): WALL's first sequence = the mark recorded before the restart + 1 + 1024; `timing` re-put and current in both Nodes' wall views; an event emitted after the restart is recorded; no `lost IS NULL` gap row (the Node buses kept their epochs).
4. **Retire:** set the second device's `retired_at`. Within 10 s the file no longer lists its account and `HubAdmin.linked()` lacks it; an event it emits afterwards is not recorded; its earlier records are still in the DB.

**Acceptance:** G-static, G-unit, G-bus (E3b tests that construct `WallWriter(client, …)` stay green), G-db; no nats type named under `central/`.
**Mutation probe:** `HUB_RELOAD_REQUESTS = 1`: leg 2 fails (the second Node's wall view never gets `timing`: "no responders" on its WALL consumer API, X6).

---

# S3 — the leaf reaches the hub through Central's origin

**Goal.** A Node dials its leaf at its origin (the host and port it already boots from) plus `LEAF_PATH`; Central's HTTP app pipes that WebSocket to the hub's leaf listener. No new LAN port and no ingress router (production exposes Central's :8000 directly; erratum E-E3D-CUT-4). Bytes are relayed untouched, both ways.
**Design:** §3 picture (leaf to the hub), §9.6 step 1, §11 rows "Wi-Fi, ingress or WAN stall", "A Node dials before its account exists"; wave-1 W11 (`.claude/runs/wave1-node-api.md:67`: the remote dials `<url path>/leafnode`).
**Files:** `contracts/node_link.py` (one constant); `central/fleet/leaf_bridge.py`; `central/app.py` (one keyword and the mount); `tests/integration/test_central_leaf_bridge.py`.

**Frozen:**

```python
# contracts/node_link.py
LEAF_PATH: Final = "/bus"   # the path under a Node's origin its leaf dials (ws[s]://<user>:<user>@<origin host>:<origin port>/bus);
                            # nats-server appends /leafnode. E3c builds the Node's leaf URL from it.

# central/fleet/leaf_bridge.py
LEAF_ROUTE: Final = LEAF_PATH + "/leafnode"

def mount_leaf_bridge(app: FastAPI, hub_leaf_url: str) -> None:
    """Accept a WebSocket at LEAF_ROUTE and relay it to `hub_leaf_url` (the hub's leaf WebSocket, e.g.
    ws://photo-wall-hub:8080/leafnode) with the `websockets` client: every binary and text message in both
    directions, unchanged, until either side closes, which closes the other. A hub that does not accept
    closes the Node's socket (code 1011) at once; the leaf redials. No authentication, no inspection, no
    buffering beyond one message. max_size >= NODE_MAX_PAYLOAD + 64 KiB; no compression."""

# central/app.py
def create_app(..., hub_leaf_url: str | None = None) -> FastAPI:
    # hub_leaf_url defaults to os.environ.get("PHOTO_WALL_HUB_LEAF_URL"); when set, mount_leaf_bridge(app, hub_leaf_url)
```

**Test:** `tests/integration/test_central_leaf_bridge.py::test_a_node_leaf_links_through_centrals_origin` (db; a real Central app from `central.app.create_app(..., hub_leaf_url=<hub ws>/leafnode)` under uvicorn on a free loopback port; a hub from `hub_server`; a Node from `node_server(..., leaf_port=<central port>, prefix="bus")`; S1's fleet `NodeLinks`).
1. The hub reports the Node's account linked within 10 s; Central records the display session's `birth` (Node → hub); a WALL write reaches the Node's wall view (hub → Node).
2. Stop and restart the Central server on the same port: the leaf relinks within 15 s and an event emitted after it is recorded.
3. A Central app built without `hub_leaf_url` refuses a WebSocket at `LEAF_ROUTE`.

**Acceptance:** G-static, G-unit, G-bus, G-db; one definition of `LEAF_PATH`, used by the bridge.
**Mutation probe:** relay only Node → hub (drop the hub → Node pump): leg 1 fails (the leaf never completes its handshake; the hub never lists the account).

---

# S4 — presence: the console shows whether a Node's leaf is linked

**Goal.** Each Player's page says whether its Node's leaf is linked to Central's hub, as Central's inference with its age, and says "unknown" when Central has not looked recently or runs no hub. Presence never gates anything.
**Design:** §13 Deferrals ("Presence for the console ($SRV.PING plus leaf state) stays a 0017 Open item, built by E3d"); 0017 Open item "Presence from link events plus $SRV.PING"; the truth kinds in `central/console/src/facts.js`'s header. Leaf state only; component presence by `$SRV.PING` is deferred (erratum E-E3D-CUT-8).
**Files:** `central/migrations/068_node_bus_presence.sql`; `central/fleet/node_bus_hub.py`; `central/fleet/service.py` (`status()` field); `central/console/src/facts.js`, `central/console/src/PlayerPage.jsx` and the console test that covers facts; `tests/integration/test_central_presence.py`.

**Frozen:**

```sql
-- central/migrations/068_node_bus_presence.sql
-- E3d: Central's hub holds a Node's leaf or not, as Central's last look saw it (0017 Open item; never gates).
CREATE TABLE node_bus_presence (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
    linked BOOLEAN NOT NULL,
    changed_at DOUBLE PRECISION NOT NULL      -- Central's clock at the look that saw the change
);
CREATE TABLE node_bus_hub_looks (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    looked_at DOUBLE PRECISION NOT NULL       -- Central's clock at the last look that reached the hub
);
```

```python
# central/fleet/node_bus_hub.py (additions)
PRESENCE_STALE_SECONDS: Final = 30.0
# FleetHub.run, after a look whose HubAdmin.linked() answered: one transaction upserts node_bus_presence for
# each enrolled device whose linked state differs from its row (or has none), and sets looked_at.
# A look that did not reach the hub writes nothing.

# central/fleet/service.py — FleetService.status(): every device dict gains
#   "bus_link": {"linked": bool | None, "changed_at": float | None, "looked_at": float | None}
# (None where no row exists; looked_at is the singleton's, the same for every device)
```

```js
// central/console/src/facts.js
/** The Node API link fact: derived "Node API link: linked" / "not linked" (Central's inference: Central's
 *  hub holds / does not hold this Node's leaf) with its age from changed_at; unknown "Unknown: Central has
 *  not looked at its hub for <age>" when readAt - looked_at > 30 s; unknown "Unknown: Central runs no hub"
 *  when looked_at is null. Never throws. */
export function busLinkFact(busLink, readAt) { /* … */ }
// PlayerPage.jsx renders busLinkFact(device.bus_link, <the snapshot's read time>) in the Player's facts.
```

**Test:** `tests/integration/test_central_presence.py::test_the_console_reads_whether_a_node_is_linked` (db; S2's bed with one enrolled Node; `FleetService(...).status()` with an injected clock for the read time):
1. Within 10 s `bus_link.linked` is true and `looked_at` is recent.
2. Crash the Node bus: within 10 s `linked` is false with a later `changed_at`; restart it: true again.
3. Stop the FleetHub: a status read at `looked_at + 31 s` (injected clock) gives a `bus_link` that `busLinkFact` renders unknown. The console test covers the wordings: linked, not linked, stale, no hub.

**Acceptance:** G-static, G-unit (console tests included), G-db, G-console build.
**Mutation probe:** FleetHub stops updating `looked_at`: leg 1 fails (`looked_at` absent or stale; the fact reads unknown).

---

# S5 — the hub in Central's deployment (gated: E-E3D-CUT-3)

**Goal.** `docker compose up` runs a hub beside Central; the worker runs FleetHub and both NodeLinks supervisors as long-lived tasks under its one signal handler; Central relays leaves to the hub; the images carry `nodeapi` and nats-py. Without `PHOTO_WALL_HUB_URL` the worker runs exactly as before (today's Kubernetes deployment).
**Design:** §7.2 rows "Hub runner and Fleet generator", "NodeLink supervisors" (one worker, never a worker per Node or job kind), §9.6, §12 Deletes "Hub JetStream persistence", §13 cost "Two hub connections per Node".
**Gate:** the orchestrator approves the `uv.lock` change, or E3c landed the identical move (then drop the `pyproject`/`uv.lock` part).
**Files:** `pyproject.toml` + `uv.lock` (nats-py to `[project].dependencies`, removed from `dev`; `uv lock`; the diff must be exactly the 4 lines probed in E-E3D-CUT-3, else STOP); `Dockerfile`; `compose.yaml`; `central/node_bus_wiring.py`; `media/worker.py`; `.github/workflows/checks.yml` (`image-smoke` hub checks); `tests/integration/test_central_worker_hub.py`; `tests/test_content_wiring.py` (the `_run_workers` tests).

**Frozen:**

```python
# central/node_bus_wiring.py
HUB_URL_ENV: Final = "PHOTO_WALL_HUB_URL"          # the worker's client URL of the hub, e.g. nats://photo-wall-hub:4222
HUB_CONFIG_ENV: Final = "PHOTO_WALL_HUB_CONFIG"    # the shared configuration file
DEFAULT_HUB_CONFIG: Final = Path("/var/lib/photo-wall/hub/hub.json")
WALL_TABLE: Final = KeyTable({"timing": 4096})    # placeholder until E8 freezes the wall table in contracts (§10; E-E3D-CUT-7)

class NodeBus:
    """The worker's side of every Node's bus: FleetHub and the fleet and show NodeLinks."""
    async def run(self, stop: asyncio.Event) -> None: ...   # until `stop`; raises the first failure

def build_node_bus(dsn: str, env: Mapping[str, str]) -> NodeBus | None: ...
    # None without HUB_URL_ENV (no hub deployed). Its own Database(dsn, pool_size=LINK_STORE_CONCURRENCY + 2),
    # PgLinkStores, NodeLinks(Pipe.FLEET, …) and NodeLinks(Pipe.SHOW, …) with empty document sources (E5),
    # PgWallMarks with an empty wall source (E5/E8), FleetHub(…, HUB_LISTENERS, WALL_TABLE, …).

# media/worker.py
async def _run_workers(media: Awaitable[None], runtime: JobRuntime, bus: NodeBus | None = None) -> None: ...
    # the bus runs as a third task under the same handler: SIGTERM/SIGINT sets its stop; a bus failure ends
    # the process non-zero, as either other loop's does. _entry passes build_node_bus(dsn, os.environ).
```

```yaml
# compose.yaml (shape; the implementer pins the image digest)
  hub:
    image: nats:2.15.0-alpine@sha256:<pinned>
    command: ["sh", "-c", "until [ -s /var/lib/photo-wall/hub/hub.json ]; do sleep 1; done; exec nats-server -c /var/lib/photo-wall/hub/hub.json"]
    volumes: [{type: volume, source: hub-config, target: /var/lib/photo-wall/hub, read_only: true}]
    networks: {default: {aliases: [photo-wall-hub]}}
    ports: ["127.0.0.1:${PHOTO_WALL_HUB_MONITOR_PORT:-8222}:8222"]
    depends_on: {worker: {condition: service_started}}   # the worker's image populates the volume as `wall`
    restart: unless-stopped
    read_only: true
    tmpfs: [/tmp]                                        # nats-server makes $TMPDIR/nats even with the memory store (E-E3B-S3-1)
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    mem_limit: 256m
    healthcheck: {test: ["CMD", "wget", "-q", "--spider", "http://127.0.0.1:8222/healthz"], interval: 2s, timeout: 3s, retries: 30}
  worker:   # additions
    environment: {PHOTO_WALL_HUB_URL: "nats://photo-wall-hub:4222", PHOTO_WALL_HUB_CONFIG: /var/lib/photo-wall/hub/hub.json}
    volumes: [hub-config:/var/lib/photo-wall/hub]
  central:  # addition
    environment: {PHOTO_WALL_HUB_LEAF_URL: "ws://photo-wall-hub:8080/leafnode"}
volumes: {hub-config: {}}
```

```dockerfile
# Dockerfile: the source stage adds   COPY --link nodeapi /app/nodeapi
# media-worker stage, before USER: install -d -o "$PHOTO_WALL_PUID" -g "$PHOTO_WALL_PGID" -m 0755 /var/lib/photo-wall/hub
```

**Tests:**
- `tests/integration/test_central_worker_hub.py::test_the_worker_keeps_the_hub_and_records_a_node` (db): `build_node_bus(dsn, env)` with `PHOTO_WALL_HUB_CONFIG` in a temp dir and a hub started from that file, run through `media.worker._run_workers(<an idle media loop>, <an idle runtime>, bus)`: an enrolled Node's `birth` is recorded and WALL exists; the stop the handler sets ends all three; `build_node_bus(dsn, {})` is None.
- `tests/test_content_wiring.py`: a failing bus ends `_run_workers` with that failure, as a failing runtime does today.
- `tests/test_compose_healthchecks.py` passes with the hub's healthcheck (no first-party code in it).
- G-compose locally and CI `image-smoke`: after `docker compose up -d --no-build --wait`, `GET http://127.0.0.1:8222/connz?auth=true` lists connections authorized as `fleet` and `central-wall`, and `GET /jsz?accounts=true&streams=true` lists stream `WALL` (the worker reached the hub and WallWriter created WALL).

**Acceptance:** all gates including G-compose (or CI `image-smoke` named as the gate when local registry pulls fail); the `uv.lock` diff is exactly the probed 4 lines; the Kubernetes change below unchanged or amended by an erratum.
**Mutation probe:** `_run_workers` ignores its `bus` argument: the worker-hub test fails (no `birth` recorded) and CI's `connz` check fails.

---

# D1 — docs (after integration; not a slice)

The Kubernetes note below moved into `docs/runbook.md`; runbook env vars and the Compose hub; `docs/decisions/0017-node-redesign-r3.md` (C3: the leaf via Central's origin bridge and `LEAF_PATH`; the Open item "Presence" built as leaf state, `$SRV.PING` deferred; History); the fleet implementation map and `docs/central-system-architecture.md` (the worker's bus side); `AGENTS.md` code-map rows for `central/node_bus_wiring.py`, `central/infra/node_link_*`, `central/fleet/node_bus_hub.py`, `central/fleet/leaf_bridge.py`.

## Kubernetes change (written down for an iac PR; not applied)

Workload `workloads/photo_wall/__init__.py` on mcurcio/iac main (one pod, `replicas=1`, `Recreate`; `central` main container, `worker` native sidecar). The owner merges; an earlier owner answer holds the production hub until E4, so the PR is opened only when the orchestrator says so.

1. **Volume:** a new `emptyDir` `hub-config` (the hub starts empty at every pod start anyway; nothing to persist, design §12).
2. **Hub container:** `nats:2.15.0-alpine` pinned by digest, a native sidecar ordered **after** `worker`, with **no startup probe that holds the worker** (the hub waits for the file the worker writes; a gating probe ordered before the worker would deadlock). Command as in Compose. Mounts `hub-config` read-only at `/var/lib/photo-wall/hub` and an `emptyDir` at `/tmp`. Read-only root filesystem, no privilege escalation, all capabilities dropped, memory limit 256 Mi. Liveness: HTTP GET `127.0.0.1:8222/healthz`. No Service port and no LAN exposure; the pod reaches it on localhost.
3. **Worker:** mounts `hub-config` read-write at `/var/lib/photo-wall/hub` (emptyDir is world-writable; the worker drops to uid 10001); env `PHOTO_WALL_HUB_URL=nats://127.0.0.1:4222`, `PHOTO_WALL_HUB_CONFIG=/var/lib/photo-wall/hub/hub.json`.
4. **Central:** env `PHOTO_WALL_HUB_LEAF_URL=ws://127.0.0.1:8080/leafnode`. The leaf arrives on the existing VIP :8000 at `/bus/leafnode` (WebSocket upgrade): no new VIP port, no route.
5. **Images:** the Central and media-worker release after S5 (it adds `nodeapi` and nats-py); bump `VERSION_TAG` to it.

## Maintenance risks

- **Two `LinkStore` implementations** (`FileLinkStore` in tests, `PgLinkStore` shipped). E3b's tracer proves the file one, E3d's tests the Pg one; a divergence lets E3b's tests pass on behaviour Central does not ship. Retire `FileLinkStore` when E3b's tests can take the database tier (a later epic's call).
- **`node_link_records` and `node_link_actions` grow without bound** until E5 sets Central's retention (design §13). Nothing is recorded until Nodes run the bus (E3c, E9).
- **Every Central deploy restarts the hub** (same pod, `Recreate`): leaves relink in about 1 s, mirrors lag up to about 60 s (E-E3B-FR1); expected and self-recovering.
- **The leaf rides Central's HTTP process** (S3): a Central restart drops every leaf for about a second, and every leaf byte crosses Python. Volumes are Central's own budgeted pulls and WALL deliveries.
- **Wall table placeholder** (`{"timing": 4096}`) lives in Central until E8 moves the table to `contracts`.

## Errata (this lane)

Appended to `.claude/errata.md` with this brief: E-E3D-CUT-1 … E-E3D-CUT-9 (full text there). Summaries:

- **CUT-1** `central` cannot import `nodeapi.hub` under today's NATS contract (indirect imports are checked). S1 sets `allow_indirect_imports = true` on it and adds "Central uses only the hub role". E3c meets the same for `appliance -> nodeapi.node`; the edit is identical in both lanes.
- **CUT-2** Central cannot build the nats `Client` that `NodeLink(client, …)` and `WallWriter(client, …)` take, nor catch a nats error: E3d adds connection-owning entries to `nodeapi/hub.py` (`run_link`, `HubAdmin`, `WallWriter.connect/close`, `HubUnavailable`). The lane scope said `nodeapi/node.py` only.
- **CUT-3** Central's images install `--no-dev` and copy no `nodeapi`: the bus side needs nats-py at runtime (probed: a 4-line `uv.lock` diff, same resolution) and `nodeapi` in the image. Contradicts the lane's "uv.lock unchanged"; E-E3B-CUT-1 gave the move to E3c, which ships only the base deb. S5 is gated on it.
- **CUT-4** Production has no HTTP router in front of Central (iac: an L4 VIP to central :8000; Compose: uvicorn on :8000), so §15's "ingress route for the leaf" has no host. Central relays the leaf (S3), within W11's "no new LAN port". `LEAF_PATH` is shared with E3c.
- **CUT-5** The hub in the node-pid1 fixture Central moves to E3e's first bead: nothing dials it until E3c's unit is in the fixture image. E3d provides what the fixture calls.
- **CUT-6** "Regenerate on every enrolment change" is a look every 2 s that diffs the generated file, not a hook per write site (inserts at `central/fleet/service.py:174`, `central/infra/catalog_records.py:459`, `central/registry.py:691`; retirement at `central/registry.py:702`); "the hub connection returns" is the hub's server id changing.
- **CUT-7** No wall table exists (§10: E8) and `WallWriter` refuses an empty one: S5 wires a Central-side placeholder. A table change reaches WALL only when WALL is re-created (every hub restart, so every Central deploy); E8 must not rely on a live update.
- **CUT-8** `NodeLink.discover` is never invoked (§9.1 shows it at every reconcile; `NodeLink.run` does not call it). Left to E5, the first method caller; presence is leaf state only.
- **CUT-9** `Database`'s pool refuses waiters past twice its size (`central/db.py:63-69`); dozens of drains committing at once through `to_thread` would exceed it and end the worker in normal operation. The bus side gets its own `Database` and a store gate (`LINK_STORE_CONCURRENCY`).

Recorded by the slices (here, not in `.claude/errata.md`):

- **E-E3D-S0-1** (implementer, S0, 2026-10-08): the count can over-count by one per bus hang longer than `_PUBLISH_SECONDS` (2 s). The publisher never retries an in-flight event the full outbox dropped meanwhile (its next `_next()` takes the new head), so when that publish times out `_done` never takes it back, yet the hung server may still store it on resume: Central then records it and counts it lost. Pre-existing (E-E3B-S6-2 a); not normal operation (hang plus overflow); no change in S0. The new test's hang is the emit loop only (well under 2 s) and observed `{"dropped": 99}`: the take-back path ran.
- **E-E3D-S0-2** (implementer, S0, 2026-10-08): `State.get("outbox")` now returns the held count (before: None), since the count is ordinary held state. No caller reads it; `State.put` still refuses the key.
- **E-E3D-S1-1** (implementer, S1, 2026-10-08): in a rolling-deploy overlap, each worker's reader keeps its own in-memory cursor while the stored cursor is last write wins, so the stored cursor can step back, and if the buffer overflows during the overlap, a gap row's `lost` can count sequences the other worker recorded (gap rows from different `after_seq` both stand). Records are never lost or duplicated (keyed, `ON CONFLICT DO NOTHING`); only a loss count can be overstated. Rare (overlap seconds plus an overflow); no change in S1. E5's projections should read a gap's `lost` as an upper bound and subtract records held in its range.
- **E-E3D-S1-2** (implementer, S1, 2026-10-08): CI's `db` job has `timeout-minutes: 5`; the local DB tier ran 5 min 41 s on this tip (937 tests, `-n 4`), S1's test about 10 s of it. If CI's run nears the limit, raise the timeout (not changed here: the page names only the nats-server fetch step).
- **E-E3D-S2-1** (implementer, S2, 2026-10-08): `run_link` never saw `stop` while the hub was away: nats-py's `connect` with `max_reconnect_attempts=-1` loops inside `connect` until the server accepts (probe: `run_link` against a closed port, `stop` set after 1 s, still running 5 s later). `NodeLinks.track`/`run` await a stopped link, so retiring a Node or stopping the worker while the hub is down hung for good. Separately, nats-py's `close()` raised `TypeError` on a socket the hub had closed (seen once when retirement's reload dropped Central's user), which ended the show `NodeLinks` as if a store error, and so the worker. Class fix in `nodeapi/hub.py`: every hub client (`run_link`, `HubAdmin`, `WallWriter.connect`) goes through one `_connect` (two attempts, then `HubUnavailable`; the attempt count is lifted once connected, so it reconnects forever after) and one `_close` that never raises.
- **E-E3D-S2-2** (implementer, S2, 2026-10-08): a reload that removes an account keeps the leaf already linked in it (nats-server 2.15.0; probe: after `reload_hub(hub, [a])` `/leafz` still listed b's leaf under the same cid for 10 s; `$SYS.REQ.SERVER.<id>.KICK {"cid": …}` closed it and its redial got "Authorization Violation"). So §9.7's "Retire: remove the account, reload twice" leaves the retired Node linked, and the page's leg 4 (`linked()` lacks it) failed. Added, additively to the frozen `HubAdmin`: `unlink(accounts)` (LEAFZ, then KICK each matching leaf by cid); `FleetHub` closes, every look, any leaf whose account is not enrolled. Without it leg 4 fails ("not within 10s: Fleet removes the retired Node's account").
- **E-E3D-S2-3** (implementer, S2, 2026-10-08): a Node whose leaf dials before its account exists receives its first WALL about 40 s later (40.3–41.0 s in 4 runs; the mirror reads `active=-1`, no error, until then), not within the page's 10 s: nats-server retries the mirror's consumer create made while the leaf was refused only after about 40 s. Self-recovering, not a defect. The test starts the second Node once the hub admits Central's two links for it (`/connz`), all still within 10 s of the insert; design §11 row "A Node dials before its account exists" should read "first WALL about 40 s later", and E3e's join and E8 budget it (with E-E3B-FR1).
- **E-E3D-S2-4** (implementer, S2, 2026-10-08): two refinements of the frozen `FleetHub` page, same interface. (a) The file changing forgets the hub identity Fleet last loaded, and the identity is remembered only after both reload and `ensure()` succeeded, so a look whose reload fails (hub away, or refused) is repeated at the next look rather than lost until the next hub restart. (b) `FleetHub.wall` is `None` until WALL was ensured once, not merely connected (a put before `ensure()` raises `wall_writer_not_ensured`).
- **E-E3D-S3-1** (implementer, S3, 2026-10-08): **files outside the page.** A WebSocket relay cannot carry a leaf that negotiated S2 compression, and every leaf does by default (`s2_auto` on both ends; the accepting side offers it on a WebSocket leaf too, ns:server/leafnode.go:1354). nats-server applies S2 *below* the WebSocket framing: the hub's first bytes after the Node's INFO were `\xff\x06\x00\x00S2sTwO` (an S2 stream header) wrapping the next ws frame, so the bridge's `websockets` client closed `1002 invalid opcode` and the leaf redialled every second ("Leafnode connection closed: Read Error" on both ends). A raw-byte proxy (`PrefixProxy`, the design's ingress route) carried it, which is why no earlier test saw it. Fix, in Central's generator so every hub it writes has it: `hub_configuration`'s `leafnodes` gains `"compression": "off"` (the accepting side's "off" wins whatever the Node offers; `central/fleet/node_bus_accounts.py`, and its config test `tests/test_node_bus_config.py`). Probe: the same hub config with `"compression": "off"` linked through the bridge at once. Cost: leaf bytes are not compressed (they are Central's budgeted pulls and WALL). E3c needs no node-bus.conf change.
- **E-E3D-S3-2** (implementer, S3, 2026-10-08): the page's `max_size >= NODE_MAX_PAYLOAD + 64 KiB` reads per message, but a non-browser nats-server writes all it has pending for a connection as ONE WebSocket frame (ns:server/websocket.go `wsCollapsePtoNB`), up to that connection's max_pending (the hub's default 64 MiB). Probe (bridge capped at NODE_MAX_PAYLOAD + 64 KiB; Central publishes 40 × 200 KiB to a Node subscriber): 5 of 40 delivered and the leaf was closed (cid 8 → 10); uncapped, 40 of 40 on the same leaf. So the bridge's hub side has no cap (`HUB_MAX_MESSAGE = None`; still ">="). The Node side is uvicorn's `ws_max_size` (16 MiB default), above node-bus.conf's `max_pending` (2 MiB), which bounds a Node's frames; S5 and the iac note must not lower it.
- **E-E3D-S3-3** (implementer, S3, 2026-10-08; note, no change): a Node's leaf must finish the HTTP upgrade through Central, the bridge's dial of the hub and the hub's INFO within the remote's `first_info_timeout` (default 1 s, ns:server/leafnode.go `SetDeadline(infoTimeout)`, const.go:203), else it redials. Loopback takes milliseconds; on a slow uplink E3c may set `first_info_timeout` on the remote if the bench shows redials.
- **E-E3D-S4-1** (implementer, S4, 2026-10-08): **files outside the page.** The console cannot read `FleetService.status()`: it is served only at `GET /v1/operator/fleet`, a V1-lane route the console's V2 posture scan refuses (`tests/test_console_v2_posture.py` `V2_POSTURE["routes"]`; owner: V2-only posture). So `status()` gains `bus_link` as frozen (the test reads it there), and the Player page reads the same value from the node device read it already polls (`GET /v1/operator/node/devices/<id>`, `central/fleet/node_observations.py`). Both go through one new module, `central/fleet/node_bus_presence.py` (`bus_links_in` for the reads, `record_look_in` for FleetHub's one transaction), the one owner of the two tables and free of `nodeapi`: `service.py` and `node_observations.py` run in Central's web process, whose image has no nats-py until S5 (E-E3D-CUT-3), so they must not import `node_bus_hub`. Cost: the fact shows only where the node read does (node control on, box not retired), as every node section. `FleetHub` gains an optional `clock` (default the system's), additively.
- **E-E3D-S4-2** (implementer, S4, 2026-10-08): wording. The frozen "Node API link: linked" is the rendered line: `FactLine` label "Node API link", value "linked for <age>" / "not linked for <age>" (a derived `fact()` carries no age, so the age rides in the value, as hostHealth.js's "silent · …" does), e.g. "Node API link: linked for 3 min (Central's inference: Central's hub holds this Node's leaf)". `busLinkFact` never throws, so its unlabelled inputs read unknown naming what is missing: "the Node API link is not served", "Central's read time is not served", "when Central last looked at its hub is not served", and "Central's hub has not looked for this Node yet" (an enrolled box before the first look that sees it). Before the node read answers, the page shows the node read's own unknown (`nodeUnknown`).
- **E-E3D-S4-3** (implementer, S4, 2026-10-08; note, no change): `looked_at` and `changed_at` are the worker's clock, the read's `read_at` the web process's; the page calls both "Central's clock", true while they share a host (S5 runs the worker beside Central, as Compose does). If a deployment splits them across hosts, the 30 s staleness compares two hosts' clocks; the iac note (D1) should keep them on one host or say so.
