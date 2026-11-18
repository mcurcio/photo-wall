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
        now: float,
    ) -> bool:
        """Whether a current offer or lock of this session (`valid_until` past `now`, Central's
        clock, which wrote it) names the variant `variant_key` (its name: a re-made variant with
        other bytes is still the same content) in a layer still running and bound to this
        configuration: the variant's pin, derived (decision 0034)."""
        offers = conn.execute(
            "SELECT manifest FROM plan_offers WHERE player_id=%s AND authority_epoch=%s "
            "AND valid_until>%s",
            (player_id, authority_epoch, now),
        ).fetchall()
        for row in offers:
            plan = Plan.model_validate(row["manifest"])
            if any(
                layer.variant is not None and layer.variant.key == variant_key
                and layer.end > now and configuration.authorizes(layer)
                for layer in plan.layers
            ):
                return True

        locks = conn.execute(
            "SELECT layer FROM assignment_locks WHERE player_id=%s "
            "AND authority_epoch=%s AND valid_until>%s",
            (player_id, authority_epoch, now),
        ).fetchall()
        for row in locks:
            layer = Layer.model_validate(row["layer"])
            if (
                layer.variant is not None
                and layer.variant.key == variant_key
                and layer.end > now
                and configuration.authorizes(layer)
            ):
                return True
        return False
