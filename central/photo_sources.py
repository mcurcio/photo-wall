"""Photo sources for the operator API (roadmap 4, slice C2; run ledger .claude/runs/photo-4.md).

`PhotoSources` is the routes' one face for drafts, pools and Check now: the photo library store
answers every question but Save, which belongs to Source names (a saved Source is the next
revision of a name, and Scenes move to it under Runtime's lock).
"""

from __future__ import annotations

from central.actor import Actor
from central.infra.photo_library_store import PgPhotoLibrary
from central.photo_library.model import DraftRef, PoolCursor, SourceQuery, SourceRef
from central.photo_library.views import SourcePoolView
from central.source_names import SourceNameService


class PhotoSources:
    """PhotoSourceCommands and PhotoSourceQueries, by delegation."""

    def __init__(self, store: PgPhotoLibrary, names: SourceNameService) -> None:
        self.store, self.names = store, names

    def create_draft(self, query: SourceQuery, editing: SourceRef | None) -> DraftRef:
        return self.store.create_draft(query, editing)

    def set_draft_query(self, draft: DraftRef, query: SourceQuery) -> None:
        self.store.set_draft_query(draft, query)

    def save_draft(self, draft: DraftRef, name: str, *, by: Actor) -> SourceRef:
        return self.names.save_draft(draft, name, by=by)["source_ref"]

    def check_source(self, source_ref: SourceRef) -> None:
        self.store.check_source(source_ref)

    def draft_pool(self, draft: DraftRef, after: PoolCursor | None) -> SourcePoolView:
        return self.store.draft_pool(draft, after)

    def source_pool(self, source_ref: SourceRef, after: PoolCursor | None) -> SourcePoolView:
        return self.store.source_pool(source_ref, after)
