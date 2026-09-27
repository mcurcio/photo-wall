"""The clock gate (R6): before the first request, raise the clock to the build's floor, then take
at most one SNTP step from the first valid answer (option-42 servers, then the pool), within one
10 s budget, and record what happened for later stages. It never blocks TLS: no answer leaves
the clock at the floor, and the certificate check decides."""

import errno
import ipaddress
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol

from contracts.clock_record import (
    CLOCK_RECORD_PATH,
    MAX_RECORD_BYTES,
    MAX_TRIED,
    ClockRecord,
    ClockState,
    encode_clock_record,
    parse_clock_record,
)
from contracts.time import Clock
from uplink.causes import Cause, UplinkError
from uplink.files import write_atomically
from uplink.lookup import lookup
from uplink.sntp import NTP_PORT, Endpoint, TimeAnswer, TimeQuery, query_first

DHCP_NTP_SERVERS: Final = Path("/proc/net/ipconfig/ntp_servers")
POOL_HOST: Final = "debian.pool.ntp.org"    # Q2 = A: a public zone the product may use
FLOOR_FILE: Final = Path("/usr/lib/photo-wall/clock-floor")
GATE_BUDGET: Final = 10.0           # choice (0014)
DHCP_TIER_BUDGET: Final = 3.0       # the option-42 tier's share, used only when it lists servers
POOL_LOOKUP_TIMEOUT: Final = 2.0
STEP_THRESHOLD: Final = 0.5         # offsets at or below this count as "already right"
MAX_DHCP_SERVERS: Final = 3         # the kernel publishes up to three
MAX_POOL_ADDRESSES: Final = 4
_FLOOR = re.compile(r"[0-9]{1,12}")
_UNUSABLE = frozenset({ipaddress.IPv4Address("0.0.0.0"),
                       ipaddress.IPv4Address("255.255.255.255")})

ClockStep = Callable[[float], None]
"""Step the wall clock by `seconds`. Real: step_realtime. Tests: ManualClock.step_utc."""


def step_realtime(seconds: float) -> None:
    """clock_settime_ns(CLOCK_REALTIME, clock_gettime_ns(CLOCK_REALTIME) + seconds), computed
    immediately before the call. Needs CAP_SYS_TIME (root stages only). Does not mark the
    kernel clock synchronised, so the kernel never copies it to the RTC."""
    now = time.clock_gettime_ns(time.CLOCK_REALTIME)
    time.clock_settime_ns(time.CLOCK_REALTIME, now + round(seconds * 1_000_000_000))


def read_floor(path: Path = FLOOR_FILE) -> int:
    """Unix seconds; the file holds one decimal integer. Missing or malformed means a broken
    build: raises UplinkError(CONFIGURATION, "floor")."""
    try:
        text = path.read_bytes()[:64].decode("ascii").strip()
    except OSError as error:
        raise UplinkError(Cause.CONFIGURATION, "floor",
                          detail=errno.errorcode.get(error.errno, type(error).__name__)
                          ) from error
    except UnicodeDecodeError:
        text = ""
    if not _FLOOR.fullmatch(text):
        raise UplinkError(Cause.CONFIGURATION, "floor", detail="malformed")
    return int(text)


@dataclass(frozen=True, slots=True)
class TimeTier:
    name: Literal["dhcp", "pool"]
    servers: Callable[[float], Sequence[Endpoint]]   # given a timeout, the endpoints to ask


def dhcp_tier(path: Path = DHCP_NTP_SERVERS) -> TimeTier:
    """Up to 3 IPv4 literals, one per line, from the kernel's own DHCP (option 42), each on
    NTP_PORT. A missing or empty file, 0.0.0.0 or 255.255.255.255 means no servers."""
    def servers(timeout: float) -> list[Endpoint]:
        try:
            lines = path.read_bytes()[:4096].decode("ascii", "replace").split()
        except OSError:
            return []
        endpoints: list[Endpoint] = []
        for line in lines:
            try:
                address = ipaddress.IPv4Address(line)
            except ValueError:
                continue
            if address not in _UNUSABLE and (str(address), NTP_PORT) not in endpoints:
                endpoints.append((str(address), NTP_PORT))
        return endpoints[:MAX_DHCP_SERVERS]

    return TimeTier("dhcp", servers)


def pool_tier(host: str = POOL_HOST) -> TimeTier:
    """Up to 4 addresses of `host` on NTP_PORT, looked up with uplink.lookup(timeout=POOL_LOOKUP_TIMEOUT)
    or the tier's share, whichever is shorter. A failed lookup raises (the gate names it)."""
    def servers(timeout: float) -> list[Endpoint]:
        endpoints: list[Endpoint] = []
        for _, address in lookup(host, NTP_PORT, min(timeout, POOL_LOOKUP_TIMEOUT)):
            if (address, NTP_PORT) not in endpoints:
                endpoints.append((address, NTP_PORT))
        return endpoints[:MAX_POOL_ADDRESSES]

    return TimeTier("pool", servers)


class ClockRecordStore(Protocol):
    def write(self, record: ClockRecord) -> None: ...
    def read(self) -> ClockRecord | None: ...


class RunClockRecord:
    """The /run file: atomic replace, mode 0644 set explicitly (stage-2 units run UMask=0077).
    /run moves into the new root at switch_root, so later stages read what stage 1 wrote."""

    def __init__(self, path: Path = Path(CLOCK_RECORD_PATH)) -> None:
        self._path = path

    def write(self, record: ClockRecord) -> None:
        write_atomically(self._path, encode_clock_record(record), mode=0o644)

    def read(self) -> ClockRecord | None:
        try:
            with self._path.open("rb") as stream:
                return parse_clock_record(stream.read(MAX_RECORD_BYTES + 1))
        except OSError:
            return None


class ClockSettler(Protocol):
    """What a stage depends on (stage 1 now, provisioning in Project 2)."""
    def settle(self) -> ClockRecord: ...


def _outcome(error: BaseException) -> str:
    if isinstance(error, UplinkError):
        return f"{error.cause}_{error.reason}"
    if isinstance(error, OSError) and error.errno in errno.errorcode:
        return errno.errorcode[error.errno]
    return type(error).__name__


class ClockGate:
    """The real ClockSettler."""

    def __init__(self, *, floor: int, tiers: Sequence[TimeTier], clock: Clock, step: ClockStep,
                 store: ClockRecordStore, writer: str, query: TimeQuery = query_first,
                 budget: float = GATE_BUDGET) -> None:
        self._floor, self._tiers, self._clock, self._step = floor, tuple(tiers), clock, step
        self._store, self._writer, self._query, self._budget = store, writer, query, budget
        self._record: ClockRecord | None = None

    def settle(self) -> ClockRecord:
        """At most once per process; later calls return the first result. All budgets use
        clock.monotonic(), because the floor raise and the step both move wall time.
          1. clock.utc() < floor -> step forward to the floor.
          2. Tiers in order, while budget remains; each tier's endpoints go to `query`.
             The first valid answer ends the search:
             offset > +STEP_THRESHOLD      -> step forward: SYNCED, stepped
             |offset| <= STEP_THRESHOLD    -> SYNCED, not stepped
             offset < -STEP_THRESHOLD      -> step back (Q5 = A): SYNCED, stepped, a negative
                                              offset; AHEAD, not stepped, only where the step
                                              would take the clock below the floor
          3. No valid answer within budget -> UNSYNCED (clock stays at max(now, floor)).
          4. store.write(record); return it.
        Never raises for a network failure: a failed step does not block TLS (R6)."""
        if self._record is None:
            self._record = self._settle()
            self._store.write(self._record)
        return self._record

    def _settle(self) -> ClockRecord:
        clock, floor = self._clock, self._floor
        end = clock.monotonic() + self._budget
        tried: list[str] = []
        raised = False
        behind = floor - clock.utc()
        if behind > 0:
            raised = self._try_step(behind, "floor", tried)
        answer, tier = None, None
        for candidate in self._tiers:
            answer = self._ask(candidate, end, tried)
            if answer is not None:
                tier = candidate.name
                break
        state, stepped = ClockState.UNSYNCED, False
        if answer is not None:
            state, stepped = self._apply(answer.offset, tried)
        return ClockRecord(
            state=state, floor=floor, raised_to_floor=raised, tier=tier,
            source=None if answer is None else answer.server,
            offset=None if answer is None else answer.offset, stepped=stepped,
            tried=tuple(tried[:MAX_TRIED]), writer=self._writer, written_at=clock.utc())

    def _ask(self, tier: TimeTier, end: float, tried: list[str]) -> TimeAnswer | None:
        """One tier within its share: the dhcp tier gets up to DHCP_TIER_BUDGET, the pool the
        rest. An empty tier spends only its (lookup) time."""
        start = self._clock.monotonic()
        share = min(DHCP_TIER_BUDGET, end - start) if tier.name == "dhcp" else end - start
        if share <= 0:
            tried.append(f"{tier.name}:skipped")
            return None
        try:
            servers = list(tier.servers(share))
        except (OSError, UplinkError) as error:
            tried.append(f"{tier.name}:lookup:{_outcome(error)}")
            return None
        if not servers:
            tried.append(f"{tier.name}:none")
            return None
        answer, notes = self._query(servers, deadline=start + share, floor=self._floor)
        tried.extend(f"{tier.name}:{note}" for note in notes)
        return answer

    def _apply(self, offset: float, tried: list[str]) -> tuple[ClockState, bool]:
        if abs(offset) <= STEP_THRESHOLD:
            return ClockState.SYNCED, False
        if offset < 0 and self._clock.utc() + offset < self._floor:
            return ClockState.AHEAD, False
        if self._try_step(offset, "step", tried):
            return ClockState.SYNCED, True
        return ClockState.UNSYNCED, False

    def _try_step(self, seconds: float, what: str, tried: list[str]) -> bool:
        """A step the kernel refuses (no CAP_SYS_TIME) is recorded, never raised."""
        try:
            self._step(seconds)
        except OSError as error:
            tried.append(f"{what}:{_outcome(error)}")
            return False
        return True
