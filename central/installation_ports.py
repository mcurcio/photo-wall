"""Read-only transaction-bound Installation interfaces for central domains."""

from __future__ import annotations

from typing import Any, Protocol

from contracts.models import FrameProfile, OutputBinding


class InstallationSessions(Protocol):
    def authenticate_in(self, conn: Any, token: str) -> dict | None: ...

    def active_sessions_in(self, conn: Any) -> tuple[dict, ...]: ...

    def session_is_current_in(
        self, conn: Any, player_id: str, authority_epoch: int, *, lock: bool = False
    ) -> bool: ...

    def frame_profiles_in(self, conn: Any, frame_ids: set[str]) -> dict[str, FrameProfile]: ...

    def configuration_in(
        self, conn: Any, player_id: str, authority_epoch: int
    ) -> dict[str, list[OutputBinding]] | None: ...
