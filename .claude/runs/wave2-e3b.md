# Run brief: wave 2, E3b (the Node API library)

**Design:** `/Volumes/Dock/tmp/node-redesign/e3b-design.md`, revision 3 (2026-10-07). The owner approved the 10,000-ft shape ("looks good") and answered Q1–Q3. The round-2 NATS re-review's four must-fix findings are applied to the design (history lines at its end; errata E-E3B-R2-1..5). An implementer reads **its slice page below plus the design sections it names**, nothing else.
**Branch:** `claude/wave1-node-api` (PR 49, draft), one bead per slice, landed in order. **Rules:** `~/.claude/skills/implementation-workflow/SKILL.md` (binding).
**Scope:** E3b only, as design §15 assigns it: Node session, `nodeapi.epoch`, envelope, Slice/apply and the store-line table, sticky state, self-describing streams, DocumentWriter changes, pull liveness and the cursor reader, NodeLink and WallWriter **as a library**, the memory-store switch, and the deletions (manifest, tmpfs/block charge/crash-reload tests, L3, retention override, durable drain and acks). Proven by the design's tracer (§16.1, steps 1–5) on real servers.
**Not in scope:** the bus systemd unit and nats-py at runtime (E3c; erratum E-E3B-CUT-1), the hub runner, Central's supervisors and DB tables (E3d), the whole-solution join (E3e), Player and apps adoption (E6, E8).

## Owner answers and rules (binding)

- **Q1 = start clean:** memory store; every bus start is empty (supersedes STORE1 = A, the file store on tmpfs).
- **Q2 = remove** the per-Node retention override now; retention is per topic in each release's slice. Dynamic per-Node retention is a future nice-to-have, not a requirement.
- **Q3 = freeze** `WALL_STREAM_BYTES` as a wire constant.
- The Node is self-sufficient; Central is a client. UX over security: no security mechanisms.
- A finding is a defect only if it happens in normal operation of `nodeapi` programs, or the Node cannot recover by itself.
- RAM is not a limiting factor.
- Integration tests of functional requirements on real servers; no unit tests of internals (a unit test only where a table or configuration is the thing under test). A test that only pins a deleted mechanism is deleted, and the page names the surviving coverage.
- Make failure classes impossible rather than patching instances. Right-size: no gold-plating. DRY/SOLID.
- **Report where the spec or plan is wrong:** append to `.claude/errata.md` (append-only; read with `grep -a`) with ids `E-E3B-S<n>-<k>`, then STOP if the frozen page cannot be met. A page changes only through an erratum and a re-cut.

## Environment

- `source ~/.nvm/nvm.sh && nvm use 20` before any test run.
- `.venv/bin/python` only; never `uv run`. **`uv.lock` and `[project].dependencies` stay unchanged** in every slice (revert `uv.lock` if anything rewrites it).
- `PHOTO_WALL_NATS_SERVER=/Volumes/Dock/tmp/nats/nats-server-v2.15.0-darwin-arm64/nats-server`.
- Fence: Docker plus `PHOTO_WALL_BUS_FENCE_SERVER=/Volumes/Dock/tmp/nats/linux/nats-server-v2.15.0-linux-arm64/nats-server` (`tests/integration/test_bus_memory_fence.py`; CI `bus-fence` job in `.github/workflows/checks.yml`).
- pytest `--basetemp=/Volumes/Dock/tmp/w2bt/<slice>/<gate>` (short path: AF_UNIX sockets); `TMPDIR=/Volumes/Dock/tmp/w2bt/<slice>`.
- New bus test files are named `tests/integration/test_node_bus_*.py`, so CI's `node-bus` job (`-k node_bus`) and the unit tier's allowlisted skip ("set PHOTO_WALL_NATS_SERVER") pick them up with no CI edit.

### Gates

| Gate | Command | Slices |
|---|---|---|
| G-static | `.venv/bin/python -m ruff check .` · `.venv/bin/lint-imports` · `python3 scripts/check_docs.py` | all |
| G-unit | `PHOTO_WALL_TEST_REQUIRE_DATABASE=1 .venv/bin/python -m pytest -q -m "not db and not browser" -n 4 --dist worksteal --basetemp=/Volumes/Dock/tmp/w2bt/<slice>/unit` | all; the baseline failure set is `/Volumes/Dock/tmp/w1/baseline.txt` (2 failures, both environmental: `test_uplink_device_harness`, `test_uplink_tls[dns-name]`) |
| G-bus | `PHOTO_WALL_NATS_SERVER=… .venv/bin/python -m pytest -q tests/integration -k node_bus -n 4 --basetemp=/Volumes/Dock/tmp/w2bt/<slice>/bus` | all code slices |
| G-fence | `PHOTO_WALL_BUS_FENCE_SERVER=… PHOTO_WALL_BUS_FENCE_MINUTES=1 PHOTO_WALL_BUS_FENCE_STALL_SECONDS=20 .venv/bin/python -m pytest -q tests/integration/test_bus_memory_fence.py --basetemp=/Volumes/Dock/tmp/w2bt/<slice>/fence` | S2, S3, S4 (they edit that file); CI runs the 5-minute form |

One full verify per bead (all its gates once, unpiped). The milestone gate is PR 49's CI run after D1.

### Preflight (2026-10-07, at `a173704`, clean tree)

| Check | Result |
|---|---|
| Runtime | node v20.20.2 via nvm; Python 3.12.11 in `.venv`; nats-py importable |
| Server | `nats-server: v2.15.0` (darwin-arm64); the linux-arm64 binary is at the fence path above |
| Docker | 29.8.1, aarch64 |
| G-static | ruff 0, lint-imports 0, check_docs 0 |
| G-bus | 37 passed, 1 xfailed (the strict reload xfail, E-W1-TD-S3), 30 s |
| G-unit | not re-run at this tip; the baseline file is from `0638f10`. **Re-run once at launch** and replace the baseline if it differs. |
| Models | pin by id at launch (skill §6.7); implementers with a clear page on the approved Sonnet id, the per-slice review lens and the course-correction architect on the approved Opus id |

### Budget gates

- Per bead: wall-clock 120 min for S1 and S2, 90 min for the others; at most 8 agents.
- Feature: 14 h wall-clock and 8 M tokens; warning milestone at 80 %, stop after the current bead at 100 %.
- At most two fix cycles per gate; then save to a pushed `wip/e3b-s<n>` branch and stop. Two consecutive stops on different slices = re-cut before any further implementation.
- A course-correction architect pass after S3 and before D1 (owner memory: at least every 5 implementers).
- Review lens per slice (skill §2.3): S1, S3 and S4 change public API surface and delete mechanisms, so each gets one adversarial NATS lens on its diff; S2, S5, S6 go straight to verify.

## Ledger

| Bead | Status | Sha | Note |
|---|---|---|---|
| S1 tracer 1/4: attach, record, call, recover a lost reader | open | | |
| S2 tracer 2/4: documents and the wall, hub restart | open | | |
| S3 tracer 3/4: start clean (memory store, kill -9), L3 deleted | open | | |
| S4 tracer 4/4: apply on a full store; store lines replace ClassTable | open | | |
| S5 attach waits for its declarer; the app line from held slices | open | | |
| S6 every loss is one counted row; rollout skew and table changes | open | | |
| D1 docs: 0017 amendments, AGENTS.md map | open | | |

## Slice order and why

```
S1 ─▶ S2 ─▶ S3 ─▶ S4 ─▶ S5 ─▶ S6 ─▶ D1
```

- **Tracer first, in four slices** (erratum E-E3B-CUT-2). Each extends one test, `tests/integration/test_node_bus_api.py::test_the_node_api_tracer`, by its steps. Steps 3 and 5 each carry a compile unit of their own (the store kind with the fit, config and fence test; the store-line table replacing `ClassTable` with every importer), so two slices would each pass the per-bead ceiling.
- **Package depth.** Every slice lives in `nodeapi` plus its tests, except as listed: S1 adds names to `contracts/node_link.py` and one import-linter contract; S3 also edits `appliance/bus/node-bus.conf` and Fleet's generator (`central/fleet/node_bus_accounts.py`, erratum E-E3B-CUT-3), because the builders fix the storage kind and the hub's WALL is built by them.
- **Additive, then remove.** S1 adds the new builders and `Slice` beside wave 1's `buffer`, `bucket`, `sticky_bucket`, `Documents`, `ClassTable` and `declare_table`. S2 removes `Documents` and `sticky_bucket` with the manifest; S4 removes the rest when the store-line table replaces `ClassTable`. No slice leaves a consumer of a removed name.
- **Deletions go in the slice that makes them unnecessary**, each with its surviving coverage named.

## Shared primitives (frozen when first cut)

| Primitive | Frozen in | Named consumers |
|---|---|---|
| `nodeapi.epoch` (`Token`, `epoch_origin`, `epoch_of`, `stream_epoch`, `epoch_start`) | S1 | `pull.CursorReader`; `documents.DocumentWriter`; `buffers.apply` (stamping); `hub.NodeLink` |
| `pull.CursorReader` | S1 | `hub.NodeLink` drain (StartAt.FIRST); `node.NodeSession` desired and wall views (S2, StartAt.LAST_PER_SUBJECT); the tracer's local state watch |
| `envelope` headers | S1 | `node.Events` (message id, schema major); `node` method calls and `hub.NodeLink.call` (writer as caller); `documents.DocumentWriter` (writer, S2) |
| `buffers.KeyTable` | S1 | `state_bucket` (S1); `desired_bucket`, `wall_config`, `DocumentWriter`, `WallWriter` (S2) |
| `buffers.Slice` + `apply` | S1 (create path), S4 (prune, purge, shrink, grow), S5 (`union`) | `node.NodeSession` attach; apps' `apply_line` for the app line (S5) |
| `contracts.node_link.STORE_LINES` | S1 | `buffers.Slice`; `node.NodeSession` (declarer); `hub.NodeLink` (pipe of a component) |

## Module layering (S1 adds the contract)

`pyproject.toml` `[tool.importlinter]`, a new layers contract named **"The Node API library points down"**, `containers = ["nodeapi"]`, `layers = ["node | hub", "documents", "buffers | pull | envelope", "epoch"]`, `exhaustive = true`. Siblings never import each other. `nodeapi` keeps importing only `contracts`, nats-py and the stdlib (existing contract).

---

# S1 — tracer 1/4: attach, record, call, recover a lost reader

**Goal.** One component (`display`, fleet pipe) runs on a real `NodeSession`: it emits events, puts state with `birth`, and answers a method. Central's fleet `NodeLink` records every event and state change in order through cursor readers and calls the method. A second, Node-local reader watches the state. Deleting both consumers and dropping the leaf mid-drain loses nothing, repeats nothing and invents no gap. A Central crash mid-commit resumes from the store's cursor. Design: §4, §7.1, §7.2 (epoch, pull, envelope, buffers, node, NodeLink), §8, §9.1, §9.3, §9.4, §16.1 steps 1–2.

**Files.**
- New: `nodeapi/epoch.py`, `nodeapi/envelope.py`, `nodeapi/node.py`, `nodeapi/hub.py`, `tests/integration/test_node_bus_api.py`.
- Changed: `nodeapi/pull.py`, `nodeapi/buffers.py` (additive; epoch helpers move out), `nodeapi/documents.py` (imports `Token` from `epoch`; no behaviour change), `nodeapi/__init__.py` (docstring), `contracts/node_link.py` (additions), `pyproject.toml` (import-linter contract only), `tests/test_node_bus_config.py` (store-line table test), `tests/integration/bus_servers.py` (`FileLinkStore`, `PrefixProxy.drop`/`sever`, import moves), `tests/integration/test_node_bus_seam.py`, `test_node_bus_documents.py`, `test_node_bus_limits.py`, `test_bus_memory_fence.py` (import moves; `pull` returns `Pulled`).

**Frozen signatures.**

```python
# contracts/node_link.py (additions; stdlib only)
MESSAGE_ID_HEADER: Final = "Nats-Msg-Id"              # the server's dedupe; Central's projection dedupe
SCHEMA_MAJOR_HEADER: Final = "Photo-Wall-Schema-Major"
WRITER_HEADER: Final = "Photo-Wall-Writer"            # documents' writer; a method call's caller
CENTRAL_WRITER: Final = "central"                     # every Central instance writes as this

class Pipe(StrEnum):
    FLEET = "fleet"
    SHOW = "show"

@dataclass(frozen=True)
class StoreLine:
    name: str        # [a-z]+: the line's namespace, in each stream's metadata and subjects
    pipe: Pipe
    declarer: str    # the component whose session applies this line
    streams: int
    max_bytes: int

STORE_LINES: Final[Mapping[str, StoreLine]]
# host fleet host 2 768 KiB · apps fleet apps 3 1536 KiB · display fleet display 3 1536 KiB ·
# health fleet health 3 1536 KiB · content show content 2 1280 KiB · player show apps 3 4608 KiB (design §7.3)
# Checked at import: sum(max_bytes) + WALL_STREAM_BYTES <= NODE_STORE_BYTES and
# sum(streams) + 1 <= NODE_MAX_STREAMS, else RuntimeError("store_lines_past_the_store").

# nodeapi/epoch.py (moved from buffers.py and documents.py; no re-exports left behind)
EPOCH_KEY: Final = "photo_wall_epoch"
class Token(NamedTuple):
    epoch: str
    seq: int
def epoch_origin(epoch: str) -> int: ...
def epoch_of(info: StreamInfo) -> str: ...                       # ValueError("stream_has_no_epoch")
async def stream_epoch(jetstream: JetStreamContext, stream: str) -> str: ...
def epoch_start(epoch: str) -> Token: ...                         # Token(epoch, epoch_origin(epoch) - 1)

# nodeapi/envelope.py (the only place the headers are built or read)
def event_headers(schema_major: int) -> dict[str, str]: ...      # a fresh uuid4 hex message id + the major; ValueError("schema_major")
def caller_headers(caller: str) -> dict[str, str]: ...           # {WRITER_HEADER: caller}; ValueError("caller")
def message_id(headers: Mapping[str, str] | None) -> str | None: ...
def schema_major(headers: Mapping[str, str] | None) -> int | None: ...
def writer_of(headers: Mapping[str, str] | None) -> str | None: ...

# nodeapi/pull.py
class ConsumerLost(Exception): ...   # silence through the deadline + grace, a 503, or 409 "Consumer Deleted"
class StreamAbsent(Exception): ...   # the consumer create found no such stream
class Pulled(NamedTuple):
    messages: list[Msg]
    complete: bool                   # False: a request ended with neither a full batch nor its closing status
async def pull(client: Client, stream: str, consumer: str, batch: int, *, timeout: float,
               domain: str | None = None) -> Pulled: ...
    # Any reply proves the consumer alive (message, 100, 404, 408, other 409s): Pulled([], True) is live
    # and empty. Sends no idle heartbeats (X10: each request is short and its closing status already
    # answers). The per-connection byte budget is unchanged. Raises ConsumerLost only when no request
    # of the call delivered a message.

class StartAt(Enum):
    FIRST = "first"                        # a drain: a new epoch from epoch_start(epoch); reports gaps
    LAST_PER_SUBJECT = "last_per_subject"  # a watch: a new epoch from each subject's latest; never reports gaps

@dataclass(frozen=True)
class Read:
    token: Token                      # (epoch, stream sequence)
    subject: str
    headers: Mapping[str, str]
    data: bytes

@dataclass(frozen=True)
class Gap:
    epoch: str
    after: int                        # the last sequence read before it
    count: int | None                 # None: unknown, the epoch ended

@dataclass(frozen=True)
class Batch:
    items: tuple[Read | Gap, ...]
    cursor: Token | None              # where the reader stands after `items`: commit it with them
    recreated: bool                   # the reader replaced its consumer while producing this batch

INACTIVE_THRESHOLD_SECONDS: Final = 60.0

class CursorReader:
    def __init__(self, client: Client, stream: str, cursor: Token | None, *,
                 start: StartAt = StartAt.FIRST, domain: str | None = None) -> None: ...
    @property
    def cursor(self) -> Token | None: ...
    async def read(self, batch: int, *, timeout: float) -> Batch: ...
        # Never raises ConsumerLost: recreates inside. Raises StreamAbsent; connection errors propagate.
    async def close(self) -> None: ...   # deletes its consumer, best-effort

# nodeapi/buffers.py (additions; wave-1 builders stay until S2/S4)
ROLE_KEY: Final = "photo_wall_role"
NAMESPACE_KEY: Final = "photo_wall_namespace"
PIPE_KEY: Final = "photo_wall_pipe"
TABLE_KEY: Final = "photo_wall_table"
MAX_TABLE_METADATA: Final = 16 * 1024
BIRTH_KEY: Final = "birth"
OUTBOX_KEY: Final = "outbox"             # written from S6; reserved now
RESERVED_STATE_BYTES: Final = 4096

class Role(StrEnum):
    EVENTS = "events"     # circular
    STATE = "state"       # sticky by its state table
    DESIRED = "desired"   # sticky by its document table (built from S2)
    WALL = "wall"         # sticky: the hub's WALL and every Node mirror

@dataclass(frozen=True)
class KeyTable:
    sizes: Mapping[str, int]             # key -> largest value in bytes; keys [A-Za-z0-9_-]+
    history: int = 1
    def budget(self, subject_prefix: str) -> int: ...
    @property
    def largest(self) -> int: ...
    def encoded(self) -> str: ...        # canonical JSON for TABLE_KEY
    @classmethod
    def decoded(cls, text: str) -> KeyTable: ...
    # ValueError: key_table_empty, key_table_history, key_table_key, key_table_value_past_the_leaf,
    # key_table_metadata_too_large (encoded() past MAX_TABLE_METADATA)

def event_buffer(line: str, topic: str, max_bytes: int, *, max_age: float | None = None,
                 max_msg_size: int | None = None) -> StreamConfig: ...
    # stream f"{topic.upper()}_{line}", subjects [f"{line}.{topic}.>"] (erratum E-E3B-CUT-5)
def state_bucket(line: str, table: KeyTable) -> StreamConfig: ...
    # KV_state_<line>; the table plus BIRTH_KEY and OUTBOX_KEY at RESERVED_STATE_BYTES; ValueError("state_key_reserved")
def role_of(config: StreamConfig) -> Role | None: ...      # None: not a nodeapi Node stream
def table_of(config: StreamConfig) -> KeyTable | None: ...

@dataclass(frozen=True)
class Slice:
    line: str
    buffers: tuple[StreamConfig, ...]
    # ValueError: slice_unknown_line, slice_unbuilt_buffer, slice_outside_its_line (metadata namespace
    # != line), slice_names (duplicate), slice_past_its_line (bytes or streams), slice_needs_state (exactly one)
    @property
    def store_line(self) -> StoreLine: ...
    @property
    def digest(self) -> str: ...                       # sha256 of the canonical slice; birth carries it
    def captures(self, subject: str) -> str | None: ... # the stream whose subjects take `subject`

async def apply(jetstream: JetStreamContext, slice_: Slice) -> Mapping[str, str]: ...
    # stream -> epoch. S1: creates every absent stream (stamped as `declare` stamps); keeps an existing one.

# nodeapi/node.py
@dataclass(frozen=True)
class MethodCall:
    method: str
    payload: bytes
    caller: str                         # WRITER_HEADER of the request; "unknown" when absent

MethodHandler = Callable[[MethodCall], Awaitable[bytes]]

@dataclass(frozen=True)
class Release:
    version: str
    digest: str
    schema_majors: Mapping[str, int]

OUTBOX_MESSAGES: Final = 4096
SESSION_SCHEMA_MAJOR: Final = 1         # the major of the session's own events and birth

class NodeSession:
    def __init__(self, component: str, slice_: Slice, release: Release, *, url: str,
                 methods: Mapping[str, MethodHandler] | None = None) -> None: ...
        # ValueError: session_component ([a-z]+), session_line_declared_by_another (S5 lifts it),
        # session_methods_need_records (methods given and no "record" topic in the slice)
    def start(self) -> None: ...                 # its own thread and loop; returns at once
    def stop(self, timeout: float = 5.0) -> None: ...
    def wait_attached(self, timeout: float) -> bool: ...
    @property
    def events(self) -> Events: ...
    @property
    def state(self) -> State: ...

class Events:
    def emit(self, subject: str, payload: bytes, *, schema_major: int) -> None: ...
        # thread-safe, never blocks or waits for an ack; `subject` is relative ("record.started" ->
        # "<line>.record.started"); ValueError("event_subject_not_in_slice"), ("event_too_large")

class State:
    def put(self, key: str, value: bytes) -> None: ...
        # thread-safe, never blocks; ValueError: state_key_unlisted, state_key_reserved, state_value_too_large
    def get(self, key: str) -> bytes | None: ...   # this session's own last put

# nodeapi/hub.py
class LinkStore(Protocol):
    async def cursors(self) -> Mapping[str, Token]: ...           # every stream this Node and pipe has a cursor for
    async def commit(self, stream: str, batch: Batch) -> None: ...
        # one transaction: gap rows first, raw records keyed (stream, epoch, seq) with their message id,
        # then batch.cursor; a repeat is idempotent
    async def action(self, kind: str, body: Mapping[str, object]) -> None: ...   # the action log

DRAIN_BATCH: Final = 64
DRAIN_TIMEOUT_SECONDS: Final = 1.0

class NodeLink:
    def __init__(self, client: Client, pipe: Pipe, store: LinkStore) -> None: ...
    async def reconcile(self) -> Mapping[str, str]: ...
        # stream -> epoch for every events/state stream of its pipe. STREAM.NAMES, then STREAM.INFO with
        # no options per stream; never a stream list with descriptions (X4). A cursor whose stream is
        # gone is committed as Gap(epoch, seq, None) with cursor None.
    async def run(self, stop: asyncio.Event) -> None: ...
        # reconcile, then drain every stream until `stop` or the client closes; reconcile again when a
        # batch was `recreated` or a new revision of a `birth` key is drained
    async def call(self, component: str, method: str, payload: bytes, *, timeout: float) -> bytes: ...
        # `<component>.method.<method>` with caller_headers(CENTRAL_WRITER); reply -> action("call", …);
        # ValueError("call_outside_pipe") for a component whose line is on the other pipe
    async def discover(self) -> list[Mapping[str, object]]: ...   # $SRV.INFO of its pipe's components -> action log
```

**Behaviour the page fixes.**
- Attach (declaring session): apply the slice; re-put every state key the session holds plus `birth` = JSON `{component, version, pipe, release_digest, schema_majors, slice_digest}`; register methods once as a `nats.micro` service named the component (subjects `<component>.method.<name>`); mark attached. Attach runs on the first connect and on every reconnect (the client reconnects forever).
- After each handled call the session emits `<line>.record.call` with JSON `{method, caller, outcome}` at `SESSION_SCHEMA_MAJOR`.
- The outbox publishes each event with `event_headers` built once at `emit` (a retry keeps its id) and retries until acknowledged. A full outbox drops its oldest (counting comes in S6).
- Cursor reader (design §9.4 as revised): ephemeral, AckNone, named consumer, `inactive_threshold` 60 s; re-read the epoch after each create and start over if it moved; track the consumer sequence. A consumer-sequence jump, an incomplete `Pulled` or a lost consumer: keep the contiguous prefix, delete the consumer best-effort, recreate from the cursor, set `recreated`. A gap item only for `StartAt.FIRST`, and only when the stream sequence jumps with a contiguous consumer sequence. A new epoch: `Gap(old epoch, old seq, None)` first (FIRST only), then from `epoch_start` (FIRST) or last per subject (LAST_PER_SUBJECT, whose cursor stays None until it reads).

**Tests.** `tests/integration/test_node_bus_api.py::test_the_node_api_tracer` (real hub on Fleet's generator, real Node on the shipped `node-bus.conf`, leaf through `PrefixProxy`; Central's store is `bus_servers.FileLinkStore`, an append-only file implementing `LinkStore`):
- **Step 1:** Central calls the display method across the leaf and gets the handler's reply; the call event with caller `central` is recorded; 200 emitted events are recorded once each, in order, with their message ids; every state put and `birth` (with the slice digest) is recorded; a local `CursorReader(StartAt.LAST_PER_SUBJECT)` on `KV_state_display` sees each key's latest value.
- **Step 2:** delete the drain's and the watch's consumers from a local client: both recover, no record lost or repeated. Then, while the session emits continuously, three times: `PrefixProxy.drop(0.3)` (bytes discarded) then `sever()`; the leaf redials. Every emitted event is recorded exactly once and **no gap row exists** (the stream dropped nothing). Then `FileLinkStore` raises on its next commit (Central crash mid-drain); a new `NodeLink` on the same store records nothing twice and loses nothing.
- **Liveness:** an idle live consumer pulled across the leaf returns `Pulled([], True)` on 3 consecutive pulls; after deleting it, `pull` raises `ConsumerLost`.
- `tests/test_node_bus_config.py`: `STORE_LINES` sums (11.5 of 12 MiB with the mirror, 17 of 17 streams); `Slice` refuses each listed ValueError; `KeyTable` refuses an encoding past `MAX_TABLE_METADATA`.

**Deleted, with surviving coverage.** `test_node_bus_seam.py::test_central_durable_consumer_acks_after_commit_and_loses_nothing[stream|bucket]` and the `crash_at` path of `_drain` with `_Crash` (durable drain, acks, crash mid-drain) → step 2 (consumer loss, leaf drop, crash mid-drain). `_drain` itself stays for its two remaining users (S3 and S6 delete them).

**Acceptance.**
1. G-static (including the new layers contract), G-unit (baseline set only), G-bus green.
2. Step 1, step 2 and the liveness test pass; no gap row in step 2.
3. `nodeapi.pull` imports nothing from `nodeapi` but `epoch`; `buffers`, `pull`, `envelope` import no sibling.
4. The header names are literals only in `contracts/node_link.py`; inside `nodeapi` only `envelope.py` imports them (`rg "_HEADER\b" nodeapi`).
5. No change to `uv.lock` or `[project].dependencies`.

**Mutation probe.** In `CursorReader`, report a gap whenever the stream sequence jumps, ignoring the consumer sequence: step 2 must fail on gap rows for events the stream still holds. (Second probe if time allows: `pull` returns `Pulled([], True)` on silence: the deleted-consumer leg must time out.)

---

# S2 — tracer 2/4: documents and the wall, hub restart

**Goal.** Central asserts a desired document into the display session's own bucket across the leaf and the session's desired view sees it; WallWriter writes a wall value that reaches the Node's mirror and the session's wall view; a conflicting second writer is adopted by writer; the hub restarted empty gets WALL back at mark + 1 + K and every mirror resumes. The manifest goes. Design: §7.2 (documents, node views, NodeLink assert, WallWriter), §7.4, §9.2, §9.6, §10, §16.1 steps 1 and 4.

**Files.** `nodeapi/buffers.py` (`desired_bucket`, `wall_config` change; `Documents`, `sticky_bucket`, `MANIFEST_KEY` deleted), `nodeapi/documents.py` (rewrite), `nodeapi/node.py` (views, `reads_wall`), `nodeapi/hub.py` (assert, `WallWriter`), `tests/integration/test_node_bus_api.py`, `tests/integration/bus_servers.py` (`FileLinkStore` gains own tokens and `WallMarks`; `declare_wall`/`declare_wall_mirror` follow the builders), `test_node_bus_documents.py`, `test_node_bus_seam.py`, `test_node_bus_limits.py`, `test_bus_memory_fence.py`, `tests/test_node_bus_config.py` (mechanical moves off `Documents`/`sticky_bucket`).

**Frozen signatures.**

```python
# nodeapi/buffers.py
def desired_bucket(line: str, table: KeyTable) -> StreamConfig: ...      # KV_desired_<line>, sticky, role DESIRED
def wall_config(table: KeyTable, *, first_seq: int) -> StreamConfig: ...
    # the hub's WALL: role WALL, the table in metadata, WALL_STREAM_BYTES; ValueError("wall_table_over_budget")
def wall_mirror_config() -> StreamConfig: ...                            # unchanged signature; role WALL
# deleted: Documents, sticky_bucket, MANIFEST_KEY

# nodeapi/documents.py
class Absent: ...
ABSENT: Final[Absent]
class Document(NamedTuple):
    value: bytes
    writer: str | None
    token: Token
class DocumentRefused(ValueError): ...     # refused before sending: unlisted, too large, headers too large
class Conflict(Exception):                 # the key moved on since `expect` (server 10071)
    key: str
class DocumentWriter:
    @classmethod
    async def bind(cls, client: Client, stream: str, *, writer: str,
                   domain: str | None = None) -> DocumentWriter: ...
        # reads the stream description once (no options): epoch, role DESIRED or WALL, key table from TABLE_KEY;
        # Central passes domain=NODE_DOMAIN and publishes on $JS.node.API.$KV.<bucket>.<key> (W9)
    @property
    def table(self) -> KeyTable: ...
    @property
    def epoch(self) -> str: ...
    def admit(self, key: str, value: bytes) -> None: ...
    async def put(self, key: str, value: bytes, *, expect: Token | Absent) -> Token: ...
        # always conditional (Nats-Expected-Last-Subject-Sequence; ABSENT = 0), WRITER_HEADER set;
        # raises DocumentRefused before sending, Conflict on 10071; never retries
    async def read(self, key: str) -> Document | None: ...   # re-reads the epoch; refreshes `epoch` and `table`
# deleted: listed_documents, missing_documents, StaleToken, the manifest merge loop

# nodeapi/node.py
class NodeSession:   # changed: `reads_wall`, `desired`, `wall`
    def __init__(self, component: str, slice_: Slice, release: Release, *, url: str,
                 methods: Mapping[str, MethodHandler] | None = None, reads_wall: bool = False) -> None: ...
        # reads_wall: create the WALL mirror at attach if absent
    @property
    def desired(self) -> DesiredView | None: ...      # None when the slice has no desired bucket
    @property
    def wall(self) -> WallView | None: ...            # None unless reads_wall
class DesiredView:
    def get(self, key: str) -> Document | None: ...
    def on_change(self, callback: Callable[[str, Document], None]) -> None: ...   # session thread; latest per key
class WallView:
    def get(self, key: str) -> bytes | None: ...                                 # key without the "wall." prefix
    def on_change(self, callback: Callable[[str, bytes], None]) -> None: ...

# nodeapi/hub.py
class DocumentSource(Protocol):
    async def documents(self, stream: str) -> Mapping[str, bytes]: ...   # Central's projection for one desired bucket
class LinkStore(Protocol):   # adds
    async def own_tokens(self, stream: str) -> Mapping[str, tuple[str, Token]]: ...   # key -> (digest, token) of Central's last write
    async def wrote(self, stream: str, key: str, digest: str, token: Token) -> None: ...
class NodeLink:   # changed: `documents` is required; `assert_documents` is new
    def __init__(self, client: Client, pipe: Pipe, store: LinkStore, documents: DocumentSource) -> None: ...
    async def assert_documents(self, stream: str) -> None: ...
    # by diff against own_tokens; conditional on Central's own token, or ABSENT in a new epoch. Conflict:
    # read; writer CENTRAL_WRITER -> take its token and continue; any other writer -> adopt the Node's
    # value as Central's (wrote) and action("document_adopted", {stream, key, writer})
WALL_MARGIN: Final = 1024   # K
class WallMarks(Protocol):
    async def mark(self) -> int: ...
    async def record_mark(self, seq: int) -> None: ...
    async def wall_documents(self) -> Mapping[str, bytes]: ...
class WallWriter:
    def __init__(self, client: Client, table: KeyTable, marks: WallMarks) -> None: ...   # ValueError("wall_table_over_budget")
    async def ensure(self) -> bool: ...     # WALL absent: create at mark + 1 + WALL_MARGIN, re-put every wall document; True if created
    async def put(self, key: str, value: bytes) -> int: ...   # admitted by the table; records the mark after the ack
```

Reconcile now asserts every desired bucket of its pipe whose epoch is new to Central (all keys) and on a projection change only that key.

**Tests.**
- The tracer's display slice gains its desired bucket and from here fills its line exactly (3 streams, 1.5 MiB; step 5 relies on it).
- Tracer **step 1** gains: Central's projection holds two display documents; both reach `DesiredView` with writer `central`; WallWriter's `wall.timing` value reaches `WallView` on a session with `reads_wall=True`. A local `DocumentWriter` with writer `local-ui` changes one document; Central's next assert conflicts, adopts it and logs `document_adopted`; a second assert of Central's own stale token after a lost ack takes its own token (writer `central`) without an adoption.
- Tracer **step 4:** restart the hub empty (`wipe` then `start`; same generated config), reconnect Central's clients (the test plays the E3d supervisor), `WallWriter.ensure()` re-creates WALL at mark + 1 + 1024 and re-puts; the Node's mirror and `WallView` hold the current value within 15 s; the drain resumes from Central's cursors with no lost or repeated record.
- `test_node_bus_documents.py` rewritten on `desired_bucket` + `DocumentWriter.bind`: a document written once survives every other write; WALL documents survive the largest messages in the hub and every mirror; a token from a lost store raises `Conflict` (no blind write).

**Deleted, with surviving coverage.** `test_node_bus_documents.py::test_every_writer_of_a_table_keeps_the_others_documents_in_the_manifest` (manifest deleted; nothing to cover). `test_node_bus_seam.py::test_the_wall_mirror_catches_up_after_the_hub_loses_its_store` and `_WallCentral` → tracer step 4. `missing_documents` uses in `test_node_bus_limits.py` become `DocumentWriter.read` per key.

**Acceptance.** G-static, G-unit, G-bus, G-fence green; steps 1 and 4 pass; `rg "_manifest|missing_documents|listed_documents|StaleToken|sticky_bucket|class Documents" nodeapi tests` finds nothing; every `DocumentWriter.put` call site passes `expect`.

**Mutation probe.** `WallWriter.ensure` creates WALL at `mark + 1` (K = 0): step 4 must fail on a stale mirror (X8). (Second: `assert_documents` re-puts without reading on `Conflict`: the adoption leg must fail.)

---

# S3 — tracer 3/4: start clean (memory store, kill -9), L3 deleted

**Goal.** Every bus buffer and the hub's WALL live in the memory store; a kill -9 of the bus restarts it empty and the whole system refills by itself. The fit is re-stated for the memory store. L3 (the local publish deny list) and every test that only pins misuse go. Design: §6 (answered), §7.2 contracts row, §9.5, §11, §12 changes 1 and the deletes table.

**Files.** `contracts/node_link.py`, `appliance/bus/node-bus.conf`, `nodeapi/buffers.py` (storage; `message_charge` docstring; `ClassTable` loses its fence check), `central/fleet/node_bus_accounts.py` (`HubListeners`), `tests/integration/bus_servers.py` (`BusServer.crash()`; no store env), `tests/integration/test_node_bus_api.py`, `test_bus_memory_fence.py`, `test_node_bus_seam.py`, `test_node_bus_limits.py`, `tests/test_node_bus_config.py`.

**Frozen signatures and configuration.**

```python
# contracts/node_link.py
MEMORY_STORE_FACTOR: Final = 5                  # heap per stored byte at the smallest nodeapi event (X14: 4.7)
CONSUMER_HEAP: Final = 104 * 1024               # measured heap per consumer (E-W1-CONS-2)
NODE_BUS_BASELINE: Final = 48 * 1024 * 1024     # the server's idle RSS (X14: 43.9 MiB), rounded up
# deleted: FILESTORE_BLOCK_BOUND, _FILESTORE_MAX_BLOCK, CONSUMER_BOUND, STREAM_BOUND
# Checked at import, else RuntimeError("node_bus_store_past_its_fence"):
#   NODE_STORE_BYTES * MEMORY_STORE_FACTOR + NODE_MAX_STREAMS * NODE_MAX_CONSUMERS * CONSUMER_HEAP
#     + NODE_BUS_BASELINE <= NODE_BUS_GOMEMLIMIT                      (128.7 <= 140 MiB)
#   NODE_BUS_GOMEMLIMIT + NODE_BUS_HEADROOM <= NODE_BUS_MEMORY_MAX      (144 <= 256 MiB)

# nodeapi/buffers.py: every builder sets StorageType.MEMORY (still never the caller's choice)
def message_charge(subject: str, value_bytes: int, header_bytes: int = 0) -> int: ...
    # unchanged formula; now documented as an upper bound of the memory store's charge
    # (len(subject) + len(headers) + len(value) + 16, ns:server/memstore.go), so a sticky budget is never short

# central/fleet/node_bus_accounts.py
@dataclass(frozen=True)
class HubListeners:   # store_dir and max_file_store_bytes replaced by:
    max_memory_store_bytes: int
# the hub's jetstream block: max_memory_store = max_memory_store_bytes, max_file_store = 0, no store_dir
```

`appliance/bus/node-bus.conf`: `jetstream { domain: node, max_memory_store: 12MB, max_file_store: 0 }` with no `store_dir` and no `$PHOTO_WALL_BUS_STORE`; account `API` keeps `jetstream { max_bytes_required: true, max_streams: 17, max_consumers: 12 }`; `users: [{user: local, password: local}]` with **no permissions** (L3 deleted); comments re-stated for the memory store. If nats-server 2.15.0 refuses to enable JetStream without `store_dir`, keep one on a runtime directory, append an erratum, and continue.

**Tests.**
- Tracer **step 3:** `BusServer.crash()` (SIGKILL) and `start()`; every session reconnects and re-attaches; the mirror is re-created and re-syncs; Central's readers report `recreated`, reconcile writes **one `Gap(epoch, seq, None)` per drained stream**, asserts every owned document into the new epochs; within 15 s every document, wall value and state key is back (compared with values before the crash) and newly emitted events are recorded under the new epoch.
- `tests/test_node_bus_config.py`: the shipped config is memory-only (`max_file_store` 0, no `store_dir`, no publish permissions); every builder's storage is MEMORY; the fit identity above with the exact numbers; the hub's generated jetstream block is memory-only.
- Fence (`test_bus_memory_fence.py`), re-stated for the memory store: no `--tmpfs`, no store env. `test_a_full_store_keeps_writing_inside_the_bus_fence` keeps its write phase and its consumer-cap check (12 per stream, the 13th refused); the stalled-hub test keeps a `nodeapi`-style publisher flood on a local stream, subscription churn and Central's waiting pull, and checks the bus stays inside its fence and every path works after the hub resumes.

**Deleted, with surviving coverage.**
- Fence: the crash-reload leg (kill -9 with the store full, reload, `_states == full`, consumers after restart) → nothing survives a bus start (design §12); E3c re-adds kill -9 → empty store inside the fence. The stall leg's floods on leaf exports, push consumers aimed across the leaf and the `refused` count (L3 misuse) → out of scope (R16); hub-side refusals stay covered by the seam test.
- Seam: `test_a_drain_cursor_from_a_lost_node_store_starts_the_new_creation_fresh` → tracer step 3. In `test_nothing_a_node_program_does_crosses_the_leaf_while_the_hub_is_stalled`: the export floods, push consumers and `refused_on_node` (L3) → none needed; the hub's refusals of Central and the post-resume checks stay.
- Config: the deny-list assertions; `test_the_store_fits_the_bus_fence_for_any_class_table` → the memory fit test above.
- `BusServer.wipe` → `stop()` then `start()` (a memory store starts empty); step 4 uses that. The Node's store env goes.

**Acceptance.** G-static, G-unit, G-bus, G-fence green; step 3 passes; `rg "tmpfs|FILESTORE_BLOCK_BOUND|STREAM_BOUND|CONSUMER_BOUND|StorageType.FILE|PHOTO_WALL_BUS_STORE" contracts nodeapi appliance/bus central/fleet tests` finds only history text in comments, if any.

**Mutation probe.** Switch the builders back to `StorageType.FILE` with the old config: step 3 must fail (the streams survive the kill, no new epoch, no unknown gap rows) and the config test must fail.

---

# S4 — tracer 4/4: apply on a full store; store lines replace ClassTable

**Goal.** Every line applied at its full bytes fills the store at 17 streams; a release that replaces one stream, drops a desired key and grows the state table applies on that full store; kept streams keep their epochs and values; Central records an unknown gap for the pruned stream and drains the new one. `ClassTable` and the wave-1 builders go; the tree-wide guard narrows to `nodeapi`. Design: §7.2 buffers, §7.3, §9.7, §12 changes 2–5 and 11, §16.1 step 5.

**Files.** `nodeapi/buffers.py`, `tests/integration/test_node_bus_api.py`, `tests/integration/bus_servers.py` (`node_split` → `line_slices`; `desired_documents` loses `retention`), `test_node_bus_limits.py`, `test_node_bus_seam.py`, `test_bus_memory_fence.py`, `tests/test_node_bus_config.py`.

**Frozen signatures.**

```python
# nodeapi/buffers.py
async def apply(jetstream: JetStreamContext, slice_: Slice) -> Mapping[str, str]: ...
    # stream -> epoch, in this order:
    # 1 prune: delete every stream whose NAMESPACE_KEY equals slice_.line exactly and which the slice does not name
    # 2 purge: on each sticky stream the slice keeps, purge the subjects of keys its new table drops
    # 3 shrink: update each kept stream whose new limits are smaller
    # 4 grow: update each kept stream whose new limits are larger
    # 5 create every absent stream (stamped)
    # An update keeps the stream's epoch (EPOCH_KEY copied from the server) and first sequence and
    # writes the new TABLE_KEY. Two applies of one slice racing are both no-ops for the loser.
# deleted: buffer, bucket, ClassTable, declare_table
# kept: declare (WALL and the mirror), wall_config, wall_mirror_config, event_buffer, state_bucket, desired_bucket

# tests/integration/bus_servers.py
def line_slices() -> dict[str, Slice]: ...   # one slice per STORE_LINES entry at its full bytes and streams
```

Guard (`tests/test_node_bus_config.py`): scans `nodeapi/` only; stream creates and updates only in `nodeapi/buffers.py`; pulls and consumer creates only in `nodeapi/pull.py`; outside `nodeapi` the import contract already forbids nats (design §12.11). The evasion test is kept for `nodeapi` files.

**Tests.**
- Tracer **step 5:** the display session already holds the display line, full since S2, and the mirror; apply every other `line_slices()` entry in shuffled order (17 streams, 11.5 MiB in all, no 10027 or 10047); fill the health line's streams (fleet pipe, no session in the tracer: the test calls `apply` as the health component). Apply a health release that replaces `OBSERVATION_health` with `RECORD_health` at the same bytes, drops one desired key and grows the state table: no refusal; the kept streams' epochs and every listed key's value are unchanged; the dropped key is purged; the fleet NodeLink's next reconcile commits `Gap(epoch, seq, None)` for `OBSERVATION_health` and drains `RECORD_health`. The display session stays attached throughout.
- `test_node_bus_limits.py`: every-buffer-full-still-takes-a-write, each retention class keeps its promise (state now sticky: no key loses its only value), and two applies of the full store racing are no-ops — all on `line_slices()`.
- `tests/test_node_bus_config.py`: `line_slices()` at full bytes builds; one byte or one stream past a line is refused.

**Deleted, with surviving coverage.** `test_one_class_table_holds_the_whole_store_within_the_servers_stream_count` → the `line_slices` config test. `test_one_class_table_declares_in_any_order_on_a_wholly_reserved_store` → step 5 (shuffled order). The circular state-bucket retention leg → the sticky state leg. The harness `retention` key (Q2) → none needed.

**Acceptance.** G-static, G-unit, G-bus, G-fence green; step 5 passes; `rg "ClassTable|declare_table|node_split|def buffer\(|def bucket\(|resplit|apply_table|retention_refused" nodeapi tests` finds nothing.

**Mutation probe.** Reorder `apply` to grow and create before prune: step 5 must fail with 10027 or 10047 on the full store.

---

# S5 — attach waits for its declarer; the app line from held slices

**Goal.** A session whose line another component declares (the Player's line, declared by apps) attaches in any start order and writes `birth` in every epoch; apps applies the union of two held Player slices for a hot-swap and then the new slice alone; a union that cannot fit is refused and the running slice stays. Design: §7.2 node (revised), §7.3, §9.5, §9.7, §11 attach row, §16.1 "also in E3b".

**Files.** `nodeapi/node.py`, `nodeapi/buffers.py` (`Slice.union`), `tests/integration/test_node_bus_api_recovery.py` (new).

**Frozen signatures.**

```python
# nodeapi/buffers.py
class Slice:   # adds
    def union(self, other: Slice) -> Slice: ...
        # same line only; a stream both name: the larger cap and the union of their key tables (largest
        # size per key); any other difference refused.
        # ValueError: slice_union_lines, slice_union_conflict, slice_past_its_line

# nodeapi/node.py
ATTACH_BACKOFF_SECONDS: Final = (0.1, 2.0)   # first and largest wait while a declarer has not applied
class NodeSession:   # __init__ no longer raises session_line_declared_by_another: such a session waits
    def apply_line(self, slice_: Slice) -> None: ...
        # thread-safe; for a line whose declarer is this component and which is not its own slice's
        # line; applied now and at every later attach; ValueError("session_does_not_declare_line")
```

Attach for a non-declaring session: wait, with backoff, until every stream of its slice exists (STREAM.INFO per stream, no options), then re-put state and `birth`. Attach runs again on every reconnect and whenever the outbox or a state put meets `NoStreamResponseError`. Events emitted while waiting stay in the outbox (bounded) and publish after attach.

**Tests** (`test_node_bus_api_recovery.py`):
- **Attach before declare:** a `player` session starts and emits 10 events; 3 s later the `apps` session starts and `apply_line`s the player slice; the show `NodeLink` records the Player's `birth` in that epoch and all 10 events. Then crash and restart the bus with the start order reversed: `birth` is recorded again in the new epoch.
- **Hot-swap:** apps applies `old.union(new)`; two player sessions (old and new release) attach; then apps applies `new` alone; the old-only stream is pruned and Central records an unknown gap for it. A `new` release whose union with `old` needs a fourth stream raises `ValueError("slice_past_its_line")` and the applied streams are unchanged.

**Deleted.** None.

**Acceptance.** G-static, G-unit, G-bus green; both tests pass; no `NoStreamResponseError` escapes a session in either leg (the session's thread logs none).

**Mutation probe.** Remove the wait in attach (a non-declarer re-puts at once): the attach-before-declare test must fail with no `birth` recorded in the first epoch.

---

# S6 — every loss is one counted row; rollout skew and table changes

**Goal.** Each real loss reaches Central as exactly one counted row and nothing else does: an event buffer that overflows while Central is away, state written faster than it is drained, and an outbox that overflows while the bus is down. Central never sends a key the Node's table does not list, retries it after the next birth, and a changed key table re-puts every owned key of that bucket. Design: §9.2 steps 3–4, §9.3, §11 rows "event buffer full", "state written faster", "outbox full", "key not in the Node's table".

**Files.** `nodeapi/node.py` (outbox count), `nodeapi/hub.py` (skew, table change), `tests/integration/test_node_bus_api_recovery.py`, `tests/integration/test_node_bus_seam.py`, `tests/integration/bus_servers.py` (`Recorder` deleted).

**Frozen behaviour (no new public names beyond these).**

```python
# nodeapi/node.py: at every attach the session puts state OUTBOX_KEY = JSON {"dropped": <int>},
# the events its outbox dropped (oldest first) since the session started.

# nodeapi/hub.py, NodeLink.assert_documents:
# - a key the stream's KeyTable does not list: not sent; action("document_refused", {stream, key});
#   retried at the reconcile a new birth revision triggers
# - TABLE_KEY differs from the table Central last asserted against: every owned key of that bucket is
#   re-put, conditional on Central's token; a Conflict on a key the Node no longer holds re-puts with ABSENT
```

**Tests** (`test_node_bus_api_recovery.py`):
- **Event buffer overflow:** with the fleet NodeLink stopped, a display session emits past `RECORD_display`'s cap; restart the link: exactly one gap row with the exact count of dropped events, then every held event once.
- **State faster than the drain:** 500 puts over 5 keys while the link is stopped; restart: gap rows count the unseen revisions; every key's latest value is recorded.
- **Outbox overflow:** stop the bus; emit `OUTBOX_MESSAGES + 100` events; start the bus: state `outbox` = `{"dropped": 100}` is recorded and the newest `OUTBOX_MESSAGES` events are recorded once each.
- **Rollout skew:** Central's projection holds `layout`, which the display release's table lacks: not sent, `document_refused` logged; a release adding `layout` re-attaches (new birth); the next reconcile writes it.
- **Table change:** a release shrinks one document's size and adds a key (same epoch); Central re-puts every owned key; all are present with writer `central`.

**Deleted, with surviving coverage.** `test_node_bus_seam.py::test_a_buffer_that_overflows_while_central_is_away_reaches_central_as_a_counted_gap[stream|bucket]` and `_drain` (its last user) → the overflow and state tests above. `bus_servers.Recorder` (no user left) → `FileLinkStore`.

**Acceptance.** G-static, G-unit, G-bus green; all five tests pass; `rg "class Recorder|def _drain" tests` finds nothing; no `nodeapi` module creates a durable consumer.

**Mutation probe.** Count the gap as `s - c` instead of `s - c - 1`: the overflow test must fail on the exact count.

---

# D1 — docs: 0017 amendments and the code map

**Goal.** The docs say what E3b built. **Files:** `docs/decisions/0017-node-redesign-r3.md` (C4: birth is a state key, store lines in `contracts`, splits in releases; C5: memory store, every bus start empty, no boot header, cursors by epoch; C11: `retention` dropped as an example (Q2); C13: cursor readers with the consumer-sequence check replace durable consumers; C18: state is sticky, no manifest), `AGENTS.md` (code map row for `nodeapi`: `epoch.py`, `envelope.py`, `buffers.py`, `pull.py`, `documents.py`, `node.py`, `hub.py`; the import-layering sentence gains the `nodeapi` layers), and any architecture page `check_docs.py` binds to these names. No code changes. **Acceptance:** G-static green; every amended clause cites the design section and erratum it comes from. A docs finding never fails or reverts a code slice.

---

## Handoff

The run ends with: ledger rows filled (bead, outcome, sha); `wip/e3b-s<n>` branches for anything unlanded; a PR 49 comment per skill §5; and a one-paragraph "start here" naming the next bead. After D1: one CI run of PR 49 (the milestone gate), then the E3c brief (bus unit, nats-py at runtime, fence re-run with kill -9 and small messages).
