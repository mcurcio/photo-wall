"""The PostgreSQL `FrameMediaGrants` (central/assets/frame_media.py): sessions, the media lock and
the execution's authorization. A variant's pin is derived (decision 0034): a current offer or
secured lock naming it (`PostgresExecutionRepository.media_authorized_in`); nothing is stored."""

from __future__ import annotations

from central.assets.frame_media import Grantee
from central.db import MEDIA_LOCK
from central.execution_ports import ExecutionMediaAuthorization
from central.infra.transactions import pg_connection
from central.installation_ports import InstallationSessions
from central.kernel.transactions import Transaction
from central.transaction_locks import COORDINATION_LOCK
from contracts.models import PlayerConfiguration


class PgFrameMediaGrants:
    """Implements `central.assets.frame_media.FrameMediaGrants`."""

    def __init__(self, *, installation: InstallationSessions,
                 execution: ExecutionMediaAuthorization) -> None:
        self._installation = installation
        self._execution = execution

    def serialize(self, tx: Transaction) -> None:
        conn = pg_connection(tx)
        conn.execute("SET LOCAL lock_timeout='5s'")
        conn.execute("SET LOCAL statement_timeout='10s'")
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (COORDINATION_LOCK,))

    def grantee(self, tx: Transaction, token: str) -> Grantee | None:
        conn = pg_connection(tx)
        identity = self._installation.authenticate_in(conn, token)
        if identity is None:
            return None
        observed = self._installation.configuration_in(
            conn, identity["id"], identity["authority_epoch"])
        if observed is None:
            return None
        return Grantee(
            player_id=identity["id"], authority_epoch=identity["authority_epoch"],
            configuration=PlayerConfiguration(
                player_id=identity["id"], authority_epoch=identity["authority_epoch"],
                configuration_revision=1, bindings=tuple(observed["bindings"]),
                enabled_outputs=tuple(b.output_id for b in observed["execution_bindings"])))

    def lock_media(self, tx: Transaction) -> None:
        pg_connection(tx).execute("SELECT pg_advisory_xact_lock(%s)", (MEDIA_LOCK,))

    def authorized(self, tx: Transaction, grantee: Grantee, key: str, now: float) -> bool:
        return self._execution.media_authorized_in(
            pg_connection(tx), grantee.player_id, grantee.authority_epoch, grantee.configuration,
            key, now)
