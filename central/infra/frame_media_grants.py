"""The PostgreSQL `FrameMediaGrants` (central/assets/frame_media.py): sessions, the media lock, a
variant's pins (`media_references`, re-keyed by name in 087) and the execution's authorization."""

from __future__ import annotations

from central.assets.frame_media import Grantee
from central.db import MEDIA_LOCK
from central.execution_ports import ExecutionMediaAuthorization
from central.infra.transactions import pg_connection
from central.installation_ports import InstallationSessions
from central.kernel.assets import AssetKind
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

    def named(self, tx: Transaction, sha256: str) -> str | None:
        row = pg_connection(tx).execute(
            "SELECT identity FROM assets WHERE kind=%s AND produced_sha256=%s "
            "ORDER BY identity LIMIT 1", (AssetKind.MEDIA_VARIANT.value, sha256)).fetchone()
        return None if row is None else row["identity"]

    def live_references(self, tx: Transaction, key: str, now: float) -> set[str]:
        return {row["owner"] for row in pg_connection(tx).execute(
            "SELECT owner FROM media_references WHERE variant_key=%s AND expires_at>%s",
            (key, now)).fetchall()}

    def authorized(self, tx: Transaction, grantee: Grantee, key: str, references: set[str],
                   now: float) -> bool:
        return self._execution.media_authorized_in(
            pg_connection(tx), grantee.player_id, grantee.authority_epoch, grantee.configuration,
            key, references, now)

    def pin(self, tx: Transaction, owner: str, key: str, expires_at: float) -> None:
        pg_connection(tx).execute(
            "INSERT INTO media_references(owner,variant_key,expires_at) VALUES(%s,%s,%s)",
            (owner, key, expires_at))

    def release(self, tx: Transaction, owner: str) -> None:
        pg_connection(tx).execute("DELETE FROM media_references WHERE owner=%s", (owner,))
