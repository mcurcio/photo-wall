"""PostgreSQL adapter for execution-owned media authorization."""

from __future__ import annotations

from contracts.models import Layer, Plan, PlayerConfiguration


class PostgresExecutionRepository:
    @staticmethod
    def media_authorized_in(
        conn,
        player_id: str,
        authority_epoch: int,
        configuration: PlayerConfiguration,
        variant_key: str,
        reference_owners: set[str],
        now: float,
    ) -> bool:
        """Whether an offer or a lock of this session names the variant `variant_key` (its
        name: a re-made variant with other bytes is still the same content), is still running,
        is bound to this configuration, and holds a live pin of its own."""
        offers = conn.execute(
            "SELECT manifest FROM plan_offers WHERE player_id=%s AND authority_epoch=%s "
            "AND valid_until>%s",
            (player_id, authority_epoch, now),
        ).fetchall()
        for row in offers:
            plan = Plan.model_validate(row["manifest"])
            reference = f"offer:{plan.player_id}:{plan.authority_epoch}:{plan.revision}"
            if reference in reference_owners and any(
                layer.variant is not None and layer.variant.key == variant_key
                and layer.end > now and configuration.authorizes(layer)
                for layer in plan.layers
            ):
                return True

        locks = conn.execute(
            "SELECT assignment_id,layer FROM assignment_locks WHERE player_id=%s "
            "AND authority_epoch=%s AND valid_until>%s",
            (player_id, authority_epoch, now),
        ).fetchall()
        for row in locks:
            reference = f"secured:{player_id}:{authority_epoch}:{row['assignment_id']}"
            layer = Layer.model_validate(row["layer"])
            if (
                reference in reference_owners
                and layer.variant is not None
                and layer.variant.key == variant_key
                and layer.end > now
                and configuration.authorizes(layer)
            ):
                return True
        return False
