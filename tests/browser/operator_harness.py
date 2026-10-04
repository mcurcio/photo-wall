"""Shared browser-test harness: run the production app on a loopback listener.

Relocated here at the Bead 17 cutover from the retired `test_operator_browser.py`
(the legacy flat-page test). These helpers are transport-level only — spin up the
real FastAPI app over real HTTP, and read the public Installation contract — so
they outlive the flat page and are imported by the `/console` browser tests.
"""

import itertools
import re
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import uvicorn
from playwright.sync_api import expect
from test_registry import ADMIN

from central.app import create_app
from central.coordination import Coordinator
from central.fleet.node_sessions import NodeControlConfig
from central.installation_models import InstallationInventory
from contracts.models import Readiness

# The console assumes node control (console DDD Part E, R20), so the harness runs it by
# default; `node_control=None` is the misconfigured Central without it.
NODE_CONTROL = NodeControlConfig("node-test")

# Readiness sequences only ever rise, as a live Player's do (player/executor.py).
_SEQUENCE = itertools.count(1)


@contextmanager
def operator_server(db, clock, *, media_root=None, media_queue=None, admin_token=ADMIN,
                    node_control=NODE_CONTROL, node_serving_verifier=None):
    """Run the production app on an ephemeral loopback listener with real lifespan.

    `node_control` (a NodeControlConfig, node-test by default) mounts node management as the
    supported composition does; `node_control=None` runs Central without node control, the
    misconfiguration the console shows as one banner.
    `node_serving_verifier` lets the effect gate admit node commands (reboots)."""
    app = create_app(db, clock, admin_token, run_scheduler=False,
                     media_root=media_root, media_queue=media_queue, node_control=node_control,
                     node_serving_verifier=node_serving_verifier)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started, "operator server did not start"
        yield origin
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive(), "operator server did not stop"


def sign_in(page, origin, token=ADMIN):
    """Open the console at `origin` and sign in through its sign-in screen (pass A).

    The suite's ONE sign-in step. Cookies ignore the port, so a session from an
    earlier server in the same browser context would already be sent here (reads
    work, writes are refused for their Origin); the helper clears them first so
    every sign-in binds this origin through the real screen.
    """
    page.context.clear_cookies()
    page.goto(origin + "/console")
    submit_sign_in(page, token)


def submit_sign_in(page, token=ADMIN):
    """Sign in through the sign-in screen already on the page, without loading it again:
    after a session ends mid-use the screen overlays the console, which keeps its state."""
    page.get_by_label("Operator token").fill(token)
    page.get_by_role("button", name="Sign in", exact=True).click()


def inventory(page, origin, token=ADMIN):
    """Read-only proof through the complete public Installation contract."""
    response = page.request.get(origin + "/v1/operator/inventory", headers={
        "Authorization": "Bearer " + token,
    })
    assert response.status == 200
    return InstallationInventory.model_validate_json(response.body())


def report_readiness(registry, player_id):
    """Have Central accept one readiness report from a Player at the current (controlled)
    clock, as a live Player's control loop does: advance() offers the Player a plan (an empty
    one when it is unbound or idle) and the Player reports on it. Liveness is exactly this."""
    coordinator = Coordinator(registry.db, registry.clock)
    coordinator.advance()
    epoch = next(p.authority_epoch for p in registry.inventory().players if p.id == player_id)
    plan = coordinator.delivery(player_id, epoch)["plan"]
    assert coordinator.readiness(player_id, Readiness(
        plan_id=plan.plan_id, revision=plan.revision, authority_epoch=epoch,
        sequence=next(_SEQUENCE), clock_uncertainty=.01, capacity_ok=True,
        observed_at=registry.clock.utc()))


def tile_status(page, frame_id):
    """A plan tile's status readout, located by frame identity (never by coordinates)."""
    return page.get_by_role("group", name=f"Frame {frame_id} status", exact=True)


def tile_health(page, frame_id):
    """A plan tile's health: its visible text is the short tile label, and its accessible
    name is the full label with the age (health.js `tileLabel` / `label`). The tile's planned
    fact is an image too (console DDD §35), so the health line is picked by its class."""
    return tile_status(page, frame_id).locator(".plan__health")


INVENTORY = "**/v1/operator/inventory"
SNAPSHOT = "**/v1/operator/snapshot"


def drive_poll(page):
    """Run the paused page clock one poll interval and wait until that poll has finished.

    The console's poller is single-flight: a tick that finds the previous aggregate
    read in flight, or its result not yet applied, is skipped. The
    snapshot status is `aria-busy` while any Plane A read is in flight and clears only
    once the read has been applied, which is when the poller's slot frees. Use this
    wherever a test drives polls back to back.
    """
    status = page.get_by_role("group", name="Snapshot status", exact=True)
    with page.expect_response(SNAPSHOT):
        page.clock.run_for(5000)
    expect(status).not_to_have_attribute("aria-busy", "true")


_OFFENDERS = """() => [...document.querySelectorAll("body *")]
    .filter((el) => el.getBoundingClientRect().right > window.innerWidth + 0.5)
    .map((el) => el.tagName + "." + [...el.classList].join("."))
    .slice(0, 12)"""


def assert_fits_width(page, where):
    """The page shown never scrolls sideways; otherwise name what sticks out, as
    `where: overflows at <width> px: [elements]`."""
    fits = page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
    width = page.evaluate("() => window.innerWidth")
    assert fits, f"{where}: overflows at {width} px: {page.evaluate(_OFFENDERS)}"


def pause_page_clock(page, at):
    """Install Playwright's fake clock at `at` (Unix seconds) and pause it, before navigation:
    the console's timers (the 5 s poll, the age ticker, the lease countdown) then fire only
    when the test runs the clock (page.clock.run_for, or `advance_clock` past one step)."""
    page.clock.install(time=at)
    page.clock.pause_at(at + 1)


def _console_fetch_timeout_ms():
    source = Path(__file__).resolve().parents[2] / "central" / "console" / "src" / "apiWrite.js"
    match = re.search(r"^export const TIMEOUT_MS = (\d+);$", source.read_text(), re.MULTILINE)
    assert match, f"{source}: no `export const TIMEOUT_MS = <ms>;` to bound the page clock by"
    return int(match.group(1))


# The console's fetch abort budget (apiWrite.js TIMEOUT_MS; the Plane A read shares it).
CONSOLE_FETCH_TIMEOUT_MS = _console_fetch_timeout_ms()
# How far `advance_clock` runs the page clock between settles: one poll interval.
CLOCK_STEP_MS = 5000
assert CLOCK_STEP_MS < CONSOLE_FETCH_TIMEOUT_MS
# How long (real time) a settle waits for the page's reads before it fails.
_SETTLE_TIMEOUT_S = 15.0
_TRACKERS = {}


class _ClockGuard:
    """The page clock of one browser context, guarded; see `advance_clock` for the rule.

    Wraps the context's Clock (every page of the context shares it, so `page.clock` is this
    clock) so that a single `run_for`/`fast_forward` of CONSOLE_FETCH_TIMEOUT_MS or more
    fails the test, and tracks the context's fetch/XHR requests awaiting their answer so
    that `advance_clock` can settle between steps. A request is answered at its response:
    the page's fetch has resolved, and a body the page never reads (a refused Plane A read)
    stays open until its abort without the page waiting on it. Requests a RequestGate holds
    on purpose are not waited for.
    """

    def __init__(self, context):
        self.in_flight = []
        self.held = []
        clock = context.clock
        self.run_for = clock.run_for
        self.fast_forward = clock.fast_forward
        clock.run_for = self._guarded(self.run_for, "run_for")
        clock.fast_forward = self._guarded(self.fast_forward, "fast_forward")
        context.on("request", self._started)
        context.on("response", lambda response: self._ended(response.request))
        context.on("requestfailed", self._ended)

    @staticmethod
    def _guarded(method, name):
        def guarded(ticks):
            assert isinstance(ticks, int | float), f"page.clock.{name}: pass milliseconds, not {ticks!r}"
            assert ticks < CONSOLE_FETCH_TIMEOUT_MS, (
                f"page.clock.{name}({ticks}) jumps the page clock past the console's "
                f"{CONSOLE_FETCH_TIMEOUT_MS} ms fetch abort (apiWrite.js TIMEOUT_MS) in one step, "
                "aborting any read in flight; use operator_harness.advance_clock")
            method(ticks)
        return guarded

    def _started(self, request):
        if request.resource_type in ("fetch", "xhr"):
            self.in_flight.append(request)

    def _ended(self, request):
        if request in self.in_flight:
            self.in_flight.remove(request)
        if request in self.held:
            self.held.remove(request)

    def settle(self, page):
        """Wait (real time) until every fetch the page sent, other than a held one, is answered."""
        deadline = time.monotonic() + _SETTLE_TIMEOUT_S
        while busy := [request.url for request in self.in_flight if request not in self.held]:
            assert time.monotonic() < deadline, f"reads unanswered after {_SETTLE_TIMEOUT_S} s: {busy}"
            page.wait_for_timeout(20)


@contextmanager
def guard_page_clock(context):
    """Guard `context`'s page clock for the test (conftest's autouse fixture)."""
    _TRACKERS[context] = _ClockGuard(context)
    try:
        yield
    finally:
        _TRACKERS.pop(context, None)


def advance_clock(page, ms, *, jump=False):
    """Advance the paused page clock by `ms`, never aborting a read the page has in flight.

    THE RULE. Playwright's page clock also fakes `AbortSignal.timeout`, and every console
    fetch carries one of CONSOLE_FETCH_TIMEOUT_MS (apiWrite.js TIMEOUT_MS, read from the
    source). A real network read does not answer while the clock runs, so one jump of that
    much or more aborts whatever read is in flight, or one the jump's own timers start, and
    the page shows a failed read the test never staged. So the page clock never runs that
    far in one step while a read may be in flight: the guarded clock fails any single
    `run_for`/`fast_forward` of CONSOLE_FETCH_TIMEOUT_MS or more, and longer spans come
    through here, in steps of CLOCK_STEP_MS with a settle (every fetch in flight answered;
    real time) before and after each step. A read a step starts is then younger than one
    step when the next settle waits for it.

    `jump` moves the clock with `fast_forward` instead (due timers fire at most once, at
    the end) after a settle, for spans where the steps between do not matter (ten minutes
    without a Player rejoining).
    """
    guard = _TRACKERS[page.context]
    guard.settle(page)
    if jump:
        guard.fast_forward(ms)
        guard.settle(page)
        return
    remaining = ms
    while remaining > 0:
        step = min(remaining, CLOCK_STEP_MS)
        page.clock.run_for(step)
        guard.settle(page)
        remaining -= step


class RequestGate:
    """Holds requests matching `pattern` while `holding` is set, until the test releases them
    -- a slow network response the test controls. Unheld requests pass straight through."""

    def __init__(self, page, pattern):
        self.page = page
        self.holding = False
        self.held = []
        self.seen = 0
        page.route(pattern, self._handle)

    def _handle(self, route):
        self.seen += 1
        if self.holding:
            self.held.append(route)
            guard = _TRACKERS.get(self.page.context)
            if guard is not None:
                guard.held.append(route.request)  # held on purpose: advance_clock does not wait for it
        else:
            route.continue_()

    def wait_held(self, count=1, timeout=5.0):
        """Wait until `count` requests are held (their fetches have been issued)."""
        deadline = time.monotonic() + timeout
        while len(self.held) < count:
            assert time.monotonic() < deadline, f"{count} request(s) were not held in time"
            self.page.wait_for_timeout(20)

    def release(self, index=0, **fulfill):
        """Let a held request through (to the server now), or answer it with `fulfill`."""
        route = self.held.pop(index)
        if fulfill:
            route.fulfill(**fulfill)
        else:
            route.continue_()


def answer_first(page, pattern, answer):
    """Answer the first request matching `pattern` with `answer(route)`; every later one
    passes through. Returns the list of answered URLs.

    Use this, never `page.route(..., times=1)`: when a `times` route runs out, Playwright
    turns request interception off asynchronously (`setNetworkInterceptionPatterns`),
    and a request the page sends at that moment -- the refresh `useMutate` starts right
    after a write's answer -- can stall until the console's 15 s fetch timeout. This
    route stays registered for the page's life, so interception never changes mid-test.
    """
    answered = []

    def handle(route):
        if answered:
            route.fallback()
            return
        answered.append(route.request.url)
        answer(route)

    page.route(pattern, handle)
    return answered
