"""Shared browser-test harness: run the production app on a loopback listener.

Relocated here at the Bead 17 cutover from the retired `test_operator_browser.py`
(the legacy flat-page test). These helpers are transport-level only — spin up the
real FastAPI app over real HTTP, and read the public Installation contract — so
they outlive the flat page and are imported by the `/console` browser tests.
"""

import itertools
import socket
import threading
import time
from contextlib import contextmanager

import uvicorn
from playwright.sync_api import expect
from test_registry import ADMIN

from central.app import create_app
from central.coordination import Coordinator
from central.installation_models import InstallationInventory
from contracts.models import Readiness

# Readiness sequences only ever rise, as a live Player's do (player/executor.py).
_SEQUENCE = itertools.count(1)


@contextmanager
def operator_server(db, clock, *, media_root=None, media_queue=None, admin_token=ADMIN):
    """Run the production app on an ephemeral loopback listener with real lifespan."""
    app = create_app(db, clock, admin_token, run_scheduler=False,
                     media_root=media_root, media_queue=media_queue)
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
    name is the full label with the age (health.js `tileLabel` / `label`)."""
    return tile_status(page, frame_id).get_by_role("img")


INVENTORY = "**/v1/operator/inventory"


def drive_poll(page):
    """Run the paused page clock one poll interval and wait until that poll has finished.

    The console's poller is single-flight: a tick that finds the previous poll still
    settling (its runtime or media read in flight, or its result not yet applied) is
    skipped. So waiting for the inventory response alone races the next tick. The
    snapshot status is `aria-busy` while any Plane A read is in flight and clears only
    once the read has been applied, which is when the poller's slot frees. Use this
    wherever a test drives polls back to back.
    """
    status = page.get_by_role("group", name="Snapshot status", exact=True)
    with page.expect_response(INVENTORY):
        page.clock.run_for(5000)
    expect(status).not_to_have_attribute("aria-busy", "true")


def pause_page_clock(page, at):
    """Install Playwright's fake clock at `at` (Unix seconds) and pause it, before navigation:
    the console's timers (the 5 s poll, the age ticker, the lease countdown) then fire only
    when the test runs the clock (page.clock.run_for)."""
    page.clock.install(time=at)
    page.clock.pause_at(at + 1)


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
