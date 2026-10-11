"""What the operator API answers and accepts for roadmap 4 (run ledger .claude/runs/photo-4.md,
route table). The console codes against these shapes (JSON, snake_case); no view carries a key,
an Immich URL of an asset, or an Immich asset id.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import ConfigDict, Field, SecretStr

from central.photo_library.model import (
    AssetId,
    CheckState,
    ConnectionRef,
    Count,
    DraftRef,
    ImmichUser,
    Kind,
    LeftOutReason,
    LibraryCounts,
    LibraryHealth,
    SourceQuery,
    SourceRef,
)
from contracts.models import Instant, Model

Item = TypeVar("Item")


class ConnectionWrite(Model):
    """Settings › Photo library, Save (#8, #9). `key` is write-only: None keeps the stored key.
    Adding a connection without a key is refused (key_required)."""

    model_config = ConfigDict(hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2048)
    key: SecretStr | None = Field(default=None, repr=False)


class ConnectionSaved(Model):
    connection_ref: ConnectionRef


class LibraryConnectionView(Model):
    """One row of Settings › Photo library (#8-#12). A cut-over row (C12) has no url or key yet."""

    connection_ref: ConnectionRef
    name: str
    url: str | None
    key_hint: str | None              # "…a3F9": the key is never read back
    immich_user: ImmichUser | None    # set by the first passing check
    health: LibraryHealth
    counts: LibraryCounts | None      # the last passing check's
    required_permissions: tuple[str, ...]  # policy.REQUIRED_PERMISSIONS, for "Make a key with these"


class ListAnswer(Model, Generic[Item]):
    """A picker's answer from the stored list (the worker lists; Central never calls Immich).
    `status` is the list's last listing; a failed re-list serves the last list with its age."""

    connection_ref: ConnectionRef
    status: str = Field(pattern=r"^(ok|pending|unavailable|permission|incompatible)$")
    observed_at: Instant | None
    read_at: Instant
    total_matches: Count
    items: tuple[Item, ...]
    absent: tuple[str, ...] = ()      # by-id reads only: ids the current, complete list lacks


class DraftCreate(Model):
    """A draft is a Source not yet saved (C8); `editing` names the Source it would replace."""

    query: SourceQuery
    editing: SourceRef | None = None


class DraftCreated(Model):
    draft_ref: DraftRef


class DraftQueryWrite(Model):
    query: SourceQuery


class DraftSave(Model):
    name: str = Field(min_length=1, max_length=120)


class DraftSaved(Model):
    source_ref: SourceRef


class KindCounts(Model):
    photos: Count
    videos: Count


class LeftOutCount(Model):
    reason: LeftOutReason
    count: Count


class PoolItem(Model):
    """One photo of a pool page; `thumbnail` is Central's own same-origin URL."""

    asset_id: AssetId
    kind: Kind
    taken_at: Instant
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    thumbnail: str


class SourcePoolView(Model):
    """A draft's or a saved Source's pool, counts by outcome and one page (design `SourcePool &
    PoolPage`). `next` is an opaque cursor (PoolCursor.encode) or None at the end."""

    check: CheckState
    in_pool: KindCounts
    waiting: Count
    left_out: tuple[LeftOutCount, ...]
    immich_up_to: Count | None        # "up to N": Immich's statistics for the include query
    items: tuple[PoolItem, ...]
    next: str | None
    read_at: Instant
