"""A Pi's reboots for tests (decision 0034 S4c): the operator's ask as Central's route makes it, and the
worker's run of the queued `RebootPi` through the real `RebootPiHandler` with a recording `PiReboot`.
Seeding goes through Central's own owner (central/fleet/pi_reboot.py), never raw rows."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

from runtime_fakes import apply_procrastinate_schema

from central.actor import Actor
from central.db import Database
from central.fleet.pi_reboot import OperatorReboot, OperatorReboots, RebootPiHandler
from central.infra.asset_records import PgAssetRecords
from central.infra.job_queue import pending_in
from central.infra.outcomes import JobOutcomes
from central.infra.publisher import ProcrastinatePublisher
from central.infra.transactions import PgTransactions
from central.kernel.job_types import RebootPi
from contracts.time import Clock


class RecordingReboot:
    """A PiReboot that records each ask and answers `delivered`."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []
        self.delivered = True

    async def reboot(self, device_id: str, reason: str) -> bool:
        self.asked.append((device_id, reason))
        return self.delivered


def reboot_queue(db: Database) -> None:
    """The job queue's tables, which the reboot route defers into."""
    apply_procrastinate_schema(db.dsn)


def ask_reboot(db: Database, clock: Clock, device_id: str, command_id: UUID, *,
               reason: str = "console/2026-11-18/other-tab", by: Actor = "console") -> dict:
    """An operator's reboot as the route asks it (another page's, say): its answer."""
    reboot_queue(db)
    transactions = PgTransactions(db)
    publisher = ProcrastinatePublisher(db.dsn, transactions=transactions, outcomes=JobOutcomes(),
                                       assets=PgAssetRecords(clock), clock=clock, feed=None)
    return OperatorReboots(db, publisher).request_operator_reboot(
        device_id, OperatorReboot(command_id, reason), by=by)


def run_queued(db: Database, device_id: str, *, delivered: bool = True,
               pi: RecordingReboot | None = None) -> RebootPi:
    """The worker runs the Pi's one queued reboot with the real handler (the Pi answers
    `delivered`), then the job is finished as the worker finishes it."""
    pi = pi if pi is not None else RecordingReboot()
    pi.delivered = delivered
    with db.transaction() as conn:
        [(job, status)] = pending_in(conn, RebootPi(request_id="x", device_id=device_id, reason="x", by="central"))
    assert status == "todo"
    # On its own thread, as the worker's loop is: a caller may already run a loop (Playwright's).
    with ThreadPoolExecutor(1) as worker:
        worker.submit(asyncio.run, RebootPiHandler(pi, PgTransactions(db)).handle(job)).result(30)
    with db.transaction() as conn:
        conn.execute("UPDATE procrastinate_jobs SET status='succeeded' WHERE status='todo' AND args->>'request_id'=%s",
                     (job.request_id,))
    return job
