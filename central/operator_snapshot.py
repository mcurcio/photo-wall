"""Consistent read model for the operator console's shared Plane A state."""

from typing import Any

from central.coordination import Coordinator
from central.db import Database
from central.installation_models import InstallationInventory
from central.media_repository import MediaRepository
from central.readiness_diagnostics import ReadinessDiagnostic, project_readiness_diagnostics
from central.registry import Registry
from central.runtime import Runtime
from central.runtime_store import RuntimeStore
from contracts.models import Instant, Model
from contracts.time import Clock


class OperatorSnapshot(Model):
    """Database-backed operator state read from one PostgreSQL snapshot.

    ``read_at`` is the common Runtime projection/application-clock time for all
    database rows in this response. ``inventory.read_at`` and
    ``player_reports_read_at`` timestamp the Player liveness sample separately;
    they are not intended to imply a visible/rendered-output observation.
    """

    read_at: Instant
    player_reports_read_at: Instant
    inventory: InstallationInventory
    runtime: dict[str, Any]
    media: dict[str, Any]
    readiness_diagnostics: tuple[ReadinessDiagnostic, ...] = ()


def runtime_document(runtime: Runtime, read_at: float) -> dict[str, Any]:
    state = runtime.export_state()
    projection = runtime.operator_projection(read_at)
    return {
        "definitions": state["scenes"],
        "programs": state["programs"],
        "current": projection.current,
        "protected_frames": projection.protected_frames,
        "program_outcomes": projection.program_outcomes,
    }


class OperatorSnapshotReader:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        registry: Registry,
        runtime_store: RuntimeStore,
        media: MediaRepository,
        coordinator: Coordinator,
    ):
        self.db, self.clock = db, clock
        self.registry, self.runtime_store = registry, runtime_store
        self.media, self.coordinator = media, coordinator

    def read(self) -> OperatorSnapshot:
        """Read related DB domains under one short, read-only MVCC snapshot."""
        with self.db.transaction() as conn:
            # Must precede the first data query. Database.transaction's SET LOCAL
            # timeout statements do not establish the MVCC snapshot.
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            read_at = self.clock.utc()
            inventory = self.registry.inventory_in(conn, read_at)
            reports = self.coordinator.player_reports_in(conn, read_at)
            readiness_diagnostics = project_readiness_diagnostics(
                conn, read_at=read_at, frames=inventory.frames
            )
            runtime = self.runtime_store.read_in(conn)
            sources = self.media.sources_in(conn)
            health = self.media.health_in(conn)
            return OperatorSnapshot(
                read_at=read_at,
                player_reports_read_at=reports.read_at,
                inventory=inventory.with_liveness(reports),
                runtime=runtime_document(runtime, read_at),
                media={"sources": sources, "health": health},
                readiness_diagnostics=readiness_diagnostics,
            )
