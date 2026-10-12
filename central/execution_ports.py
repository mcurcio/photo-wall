"""Transaction-bound execution authority exposed to media delivery."""

from __future__ import annotations

from typing import Any, Protocol

from contracts.models import PlayerConfiguration


class ExecutionMediaAuthorization(Protocol):
    def media_authorized_in(
        self,
        conn: Any,
        player_id: str,
        authority_epoch: int,
        configuration: PlayerConfiguration,
        variant_key: str,
        reference_owners: set[str],
        now: float,
    ) -> bool: ...
