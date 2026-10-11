"""The photo library store over PostgreSQL (roadmap 4; run ledger .claude/runs/photo-4.md).

`PgPhotoLibrary` is the one home of migration 080's tables (library_connections, library_lists,
library_list_items, photo_pools, photo_pool_rows, photo_check_marks, photo_drafts) and the only
code that reads `library_connections.api_key` (for the media worker: `begin_contact`,
`connection_secret`, `begin_check`). It implements central.photo_library.ports'
PhotoLibraryQueries and PhotoLibraryCommands (slice C1), PhotoSourceQueries and the store half of
PhotoSourceCommands (slice C2), and LibraryWorkerStore (C1 contacts and lists, C2 checks).

Every time written or compared here is `times`' (the database's, G11). Fences are numbers the
database hands out (contact numbers, check numbers), never clock comparisons across processes.

A pool is **wanted** while a draft points at it, or while a Source revision Runtime or a name
needs (`media_sources.refresh_active`, computed by MediaRepository.reconcile_source_activity_in)
names it. Only wanted pools are checked, served as thumbnails, or kept by maintenance.
"""

from __future__ import annotations

import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from typing import Any, NamedTuple

from psycopg.types.json import Jsonb
from pydantic import SecretStr, TypeAdapter, ValidationError

from central.db import Database, TransactionClock
from central.media_queue import MediaTaskQueue
from central.photo_library import policy
from central.photo_library.model import (
    AlbumRef,
    Checking,
    CheckState,
    Complete,
    ConnectionRef,
    ConnectionSecret,
    ContactAnswer,
    ContactResult,
    ContactTicket,
    DraftRef,
    Failed,
    HealthOk,
    ImmichUser,
    Incomplete,
    InPool,
    LeftOut,
    LibraryAlbum,
    LibraryAsset,
    LibraryHealth,
    LibraryPerson,
    LibraryRef,
    LibraryTag,
    ListKind,
    NeverChecked,
    PersonRef,
    Place,
    PoolCursor,
    PoolKey,
    PoolMember,
    PoolThumbnail,
    SourceQuery,
    SourceRef,
    TagRef,
    Waiting,
    visible_text,
)
from central.photo_library.ports import (
    CheckComplete,
    CheckFinish,
    CheckIncomplete,
    CheckLease,
    CheckStep,
    KnownRevision,
    LibraryRefused,
)
from central.photo_library.views import (
    ConnectionWrite,
    KindCounts,
    LeftOutCount,
    LibraryConnectionView,
    ListAnswer,
    PoolItem,
    SourcePoolView,
)

THUMBNAIL_PATH = "/v1/operator/library/thumbnails/"
# Searchable fields of one list item are joined by this separator (never visible text: the
# list's text is stripped of control characters), so "starts with" means any field starts so.
_FIELD = "\x1f"
_HEALTH = TypeAdapter(LibraryHealth)
_CHECK_STATE = TypeAdapter(CheckState)
# A failed listing's error code is a problem kind (LibraryProblem) or a code of the worker's own;
# the list's status says which family it is, as the pre-4 tag list did.
_LIST_STATUS = {"key_rejected": "permission", "missing_permissions": "permission",
                "immich_too_old": "incompatible", "unexpected_answer": "incompatible"}
_ITEM_TYPES: dict[str, type] = {"albums": LibraryAlbum, "people": LibraryPerson, "places": Place,
                                "tags": LibraryTag}
# A pool is wanted (module docstring); `p` names the photo_pools row.
_WANTED = ("(EXISTS (SELECT 1 FROM photo_drafts d WHERE d.pool_key=p.pool_key) OR EXISTS "
           "(SELECT 1 FROM media_sources s WHERE s.pool_key=p.pool_key AND s.refresh_active))")


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _search_text(kind: str, item: Any) -> str:
    """The folded fields a picker search matches; "" when there is nothing visible to pick it by
    (the item is stored, so a by-id read still names it, but a search never offers it)."""
    match kind:
        case "albums" | "people":
            fields = (item.name,)
        case "tags":
            fields = (item.path, item.name) if item.path and item.name else ()
        case _:
            fields = tuple(text for text in (item.city, item.state, item.country) if text)
    return _FIELD.join(_fold(text) for text in fields) if all(fields) else ""


def _item_ref(kind: str, item: Any) -> str:
    match kind:
        case "albums":
            return item.album_ref
        case "people":
            return item.person_ref
        case "tags":
            return item.tag_ref
        case _:
            return _FIELD.join((item.country, item.state or "", item.city or ""))


class DraftForSave(NamedTuple):
    pool_key: PoolKey
    query: SourceQuery
    editing_name: str | None


class PgPhotoLibrary:
    def __init__(self, db: Database, *, times: TransactionClock,
                 queue: MediaTaskQueue | None = None) -> None:
        """`times` stamps and compares every time on the transaction's own connection (G11);
        `queue` queues connection checks and pool check steps in the writing transaction (None:
        a read-only composition, whose queuing methods refuse with media_queue_unconfigured)."""
        self.db, self.times, self.queue = db, times, queue

    def _queue(self) -> MediaTaskQueue:
        if self.queue is None:
            raise LibraryRefused("media_queue_unconfigured", 503)
        return self.queue

    # --- Connections: operator side (C1) --------------------------------------------------------

    def connections(self) -> tuple[LibraryConnectionView, ...]:
        with self.db.transaction() as conn:
            # The key's last four characters only (policy.key_hint): never the key itself.
            rows = conn.execute(
                "SELECT connection_ref,name,url,right(api_key,4) AS key_tail,immich_user,health,"
                "counts FROM library_connections ORDER BY name, connection_ref").fetchall()
        return tuple(LibraryConnectionView(
            connection_ref=row["connection_ref"], name=row["name"], url=row["url"],
            key_hint=None if row["key_tail"] is None else policy.key_hint(row["key_tail"]),
            immich_user=row["immich_user"], health=row["health"], counts=row["counts"],
            required_permissions=policy.REQUIRED_PERMISSIONS) for row in rows)

    @staticmethod
    def _url(text: str) -> str:
        try:
            return policy.normalise_url(text)
        except ValueError:
            raise LibraryRefused("invalid_url", 422) from None

    @staticmethod
    def _key(write: ConnectionWrite) -> str | None:
        key = None if write.key is None else write.key.get_secret_value().strip()
        return key or None

    @staticmethod
    def _name_taken(conn: Any, name: str, connection_ref: str | None) -> None:
        row = conn.execute("SELECT 1 FROM library_connections WHERE name=%s AND "
                           "connection_ref IS DISTINCT FROM %s", (name, connection_ref)).fetchone()
        if row is not None:
            raise LibraryRefused("connection_name_exists", 409)

    def add_connection(self, write: ConnectionWrite) -> ConnectionRef:
        url, key = self._url(write.url), self._key(write)
        if key is None:
            raise LibraryRefused("key_required", 422)
        connection_ref = "library-" + uuid.uuid4().hex[:12]
        queue = self._queue()
        with self.db.transaction() as conn:
            self._name_taken(conn, write.name, None)
            conn.execute("INSERT INTO library_connections(connection_ref,name,url,api_key,"
                         "created_at) VALUES(%s,%s,%s,%s,%s)",
                         (connection_ref, write.name, url, key, self.times.now_in(conn)))
            queue.enqueue_connection_check_in(conn, connection_ref)
        return connection_ref

    def change_connection(self, connection_ref: ConnectionRef, write: ConnectionWrite) -> None:
        url, key = self._url(write.url), self._key(write)
        queue = self._queue()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT api_key IS NOT NULL AS keyed FROM library_connections "
                               "WHERE connection_ref=%s FOR UPDATE", (connection_ref,)).fetchone()
            if row is None:
                raise LibraryRefused("connection_unknown", 404)
            if key is None and not row["keyed"]:
                raise LibraryRefused("key_required", 422)   # a cut-over row's first paste (C12)
            self._name_taken(conn, write.name, connection_ref)
            conn.execute("UPDATE library_connections SET name=%s,url=%s,"
                         "api_key=COALESCE(%s,api_key) WHERE connection_ref=%s",
                         (write.name, url, key, connection_ref))
            queue.enqueue_connection_check_in(conn, connection_ref)

    def remove_connection(self, connection_ref: ConnectionRef) -> None:
        """Refused while a saved Source (a named Source's current revision, or a revision Runtime
        needs) or a draft uses it. Older revisions nothing needs keep their query, lose their
        pool, and the connection's pools and lists go with it."""
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM library_connections WHERE connection_ref=%s FOR UPDATE",
                            (connection_ref,)).fetchone() is None:
                raise LibraryRefused("connection_unknown", 404)
            used = conn.execute(
                "SELECT 1 FROM media_sources s WHERE s.query->>'connection_ref'=%s AND "
                "(s.refresh_active OR EXISTS (SELECT 1 FROM media_source_names n WHERE "
                "n.current_ref=s.source_ref AND NOT n.deleted)) "
                "UNION ALL SELECT 1 FROM photo_drafts d JOIN photo_pools p USING (pool_key) "
                "WHERE p.connection_ref=%s LIMIT 1", (connection_ref, connection_ref)).fetchone()
            if used is not None:
                raise LibraryRefused("sources_use_connection", 409)
            conn.execute("UPDATE media_sources SET pool_key=NULL WHERE pool_key IN "
                         "(SELECT pool_key FROM photo_pools WHERE connection_ref=%s)",
                         (connection_ref,))
            conn.execute("DELETE FROM photo_pools WHERE connection_ref=%s", (connection_ref,))
            conn.execute("DELETE FROM library_connections WHERE connection_ref=%s",
                         (connection_ref,))

    def check_connection(self, connection_ref: ConnectionRef) -> None:
        queue = self._queue()
        with self.db.transaction() as conn:
            self._known_connection(conn, connection_ref)
            queue.enqueue_connection_check_in(conn, connection_ref)

    @staticmethod
    def _known_connection(conn: Any, connection_ref: str) -> None:
        if conn.execute("SELECT 1 FROM library_connections WHERE connection_ref=%s",
                        (connection_ref,)).fetchone() is None:
            raise LibraryRefused("connection_unknown", 404)

    # --- Lists: operator side (C1) --------------------------------------------------------------

    def albums(self, connection_ref: ConnectionRef, q: str, limit: int) -> ListAnswer[LibraryAlbum]:
        return self._search("albums", connection_ref, q, limit)

    def albums_by_id(self, connection_ref: ConnectionRef,
                     ids: tuple[AlbumRef, ...]) -> ListAnswer[LibraryAlbum]:
        return self._by_id("albums", connection_ref, ids)

    def people(self, connection_ref: ConnectionRef, q: str, limit: int) -> ListAnswer[LibraryPerson]:
        return self._search("people", connection_ref, q, limit)

    def people_by_id(self, connection_ref: ConnectionRef,
                     ids: tuple[PersonRef, ...]) -> ListAnswer[LibraryPerson]:
        return self._by_id("people", connection_ref, ids)

    def places(self, connection_ref: ConnectionRef, q: str, limit: int) -> ListAnswer[Place]:
        return self._search("places", connection_ref, q, limit)

    def tags(self, connection_ref: ConnectionRef, q: str, limit: int) -> ListAnswer[LibraryTag]:
        return self._search("tags", connection_ref, q, limit)

    def tags_by_id(self, connection_ref: ConnectionRef,
                   ids: tuple[TagRef, ...]) -> ListAnswer[LibraryTag]:
        return self._by_id("tags", connection_ref, ids)

    def _list_row(self, conn: Any, kind: str, connection_ref: str) -> tuple[dict, float]:
        self._known_connection(conn, connection_ref)
        row = conn.execute("SELECT status,observed_at FROM library_lists WHERE connection_ref=%s "
                           "AND kind=%s", (connection_ref, kind)).fetchone()
        return row or {"status": "pending", "observed_at": None}, self.times.now_in(conn)

    def _search(self, kind: str, connection_ref: str, q: str, limit: int) -> ListAnswer:
        """Items whose any field starts with `q` first, then those containing it, each group by
        search text then ref; `total_matches` counts every match before `limit` cuts."""
        needle = _fold(str(visible_text(q)).strip())
        with self.db.transaction() as conn:
            listed, read_at = self._list_row(conn, kind, connection_ref)
            rows = conn.execute(
                "SELECT item, count(*) OVER () AS total FROM ("
                " SELECT item, item_ref, search_text,"
                "  strpos(chr(31)||search_text, chr(31)||%(q)s)=0 AS later"
                " FROM library_list_items WHERE connection_ref=%(ref)s AND kind=%(kind)s"
                "  AND search_text<>'' AND strpos(search_text, %(q)s)>0) matches "
                "ORDER BY later, search_text COLLATE \"C\", item_ref COLLATE \"C\" LIMIT %(limit)s",
                {"q": needle, "ref": connection_ref, "kind": kind, "limit": limit}).fetchall()
        return self._answer(kind, connection_ref, listed, read_at,
                            [row["item"] for row in rows], rows[0]["total"] if rows else 0, ())

    def _by_id(self, kind: str, connection_ref: str, ids: Sequence[str]) -> ListAnswer:
        """Exactly these items, in the asked order; `absent` only from an `ok` list (a pending or
        failed list proves nothing gone)."""
        with self.db.transaction() as conn:
            listed, read_at = self._list_row(conn, kind, connection_ref)
            stored = {row["item_ref"]: row["item"] for row in conn.execute(
                "SELECT item_ref,item FROM library_list_items WHERE connection_ref=%s AND kind=%s "
                "AND item_ref=ANY(%s)", (connection_ref, kind, list(ids))).fetchall()}
        found = [stored[ref] for ref in ids if ref in stored]
        absent = tuple(ref for ref in ids if ref not in stored) if listed["status"] == "ok" else ()
        return self._answer(kind, connection_ref, listed, read_at, found, len(found), absent)

    @staticmethod
    def _answer(kind: str, connection_ref: str, listed: dict, read_at: float, items: list,
                total: int, absent: tuple[str, ...]) -> ListAnswer:
        item_type = _ITEM_TYPES[kind]
        return ListAnswer[item_type](  # type: ignore[valid-type]
            connection_ref=connection_ref, status=listed["status"],
            observed_at=listed["observed_at"], read_at=read_at, total_matches=total,
            items=tuple(item_type.model_validate(item) for item in items), absent=absent)

    # --- Contacts and lists: worker side (C1) ---------------------------------------------------

    def due_contacts(self) -> tuple[ConnectionRef, ...]:
        with self.db.transaction() as conn:
            now = self.times.now_in(conn)
            rows = conn.execute(
                "SELECT connection_ref FROM library_connections WHERE api_key IS NOT NULL AND "
                "(contact_started_at IS NULL OR contact_started_at<=%s) ORDER BY connection_ref",
                (now - policy.CHECK_EVERY_SECONDS,)).fetchall()
        return tuple(row["connection_ref"] for row in rows)

    @staticmethod
    def _secret(row: dict) -> ConnectionSecret:
        user = row["immich_user"]
        return ConnectionSecret(connection_ref=row["connection_ref"], base_url=row["url"],
                                api_key=SecretStr(row["api_key"]),
                                bound_user=None if user is None else user["user_ref"])

    def begin_contact(self, connection_ref: ConnectionRef) -> ContactTicket | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "UPDATE library_connections SET last_contact=last_contact+1,contact_started_at=%s "
                "WHERE connection_ref=%s AND api_key IS NOT NULL "
                "RETURNING connection_ref,url,api_key,immich_user,last_contact",
                (self.times.now_in(conn), connection_ref)).fetchone()
        if row is None:
            return None
        return ContactTicket(contact_number=row["last_contact"], connection=self._secret(row))

    def connection_secret(self, connection_ref: ConnectionRef) -> ConnectionSecret | None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT connection_ref,url,api_key,immich_user FROM "
                               "library_connections WHERE connection_ref=%s AND api_key IS NOT NULL",
                               (connection_ref,)).fetchone()
        return None if row is None else self._secret(row)

    def record_contact(self, ticket: ContactTicket, result: ContactResult) -> bool:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT immich_user,health,applied_contact FROM library_connections "
                "WHERE connection_ref=%s FOR UPDATE",
                (ticket.connection.connection_ref,)).fetchone()
            if row is None or row["applied_contact"] >= ticket.contact_number:
                return False
            bound = None if row["immich_user"] is None else ImmichUser.model_validate(row["immich_user"])
            health = policy.contact_health(result, bound_user=bound,
                                           previous=_HEALTH.validate_python(row["health"]),
                                           checked_at=self.times.now_in(conn))
            if bound is None and isinstance(health, HealthOk) and isinstance(result, ContactAnswer):
                bound = result.user  # the first passing contact binds (C1)
            counts = (result.counts if isinstance(health, HealthOk) and isinstance(
                result, ContactAnswer) and result.counts is not None else None)
            conn.execute(
                "UPDATE library_connections SET applied_contact=%s,health=%s,immich_user=%s,"
                "counts=COALESCE(%s,counts) WHERE connection_ref=%s",
                (ticket.contact_number, Jsonb(_HEALTH.dump_python(health, mode="json")),
                 None if bound is None else Jsonb(bound.model_dump(mode="json")),
                 None if counts is None else Jsonb(counts.model_dump(mode="json")),
                 ticket.connection.connection_ref))
            return True

    def record_list(self, connection_ref: ConnectionRef, kind: ListKind,
                    items: Sequence[LibraryAlbum | LibraryPerson | Place | LibraryTag] | None,
                    error: str | None) -> None:
        if (items is None) == (error is None):
            raise ValueError("record either items or an error")
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM library_connections WHERE connection_ref=%s FOR SHARE",
                            (connection_ref,)).fetchone() is None:
                return   # removed while the worker listed: nothing to keep
            now = self.times.now_in(conn)
            if items is None:
                conn.execute(
                    "INSERT INTO library_lists(connection_ref,kind,status,error,checked_at) "
                    "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(connection_ref,kind) DO UPDATE SET "
                    "status=EXCLUDED.status,error=EXCLUDED.error,checked_at=EXCLUDED.checked_at",
                    (connection_ref, kind, _LIST_STATUS.get(error, "unavailable"), error, now))
                return
            conn.execute(
                "INSERT INTO library_lists(connection_ref,kind,status,error,observed_at,checked_at) "
                "VALUES(%s,%s,'ok',NULL,%s,%s) ON CONFLICT(connection_ref,kind) DO UPDATE SET "
                "status='ok',error=NULL,observed_at=EXCLUDED.observed_at,"
                "checked_at=EXCLUDED.checked_at", (connection_ref, kind, now, now))
            conn.execute("DELETE FROM library_list_items WHERE connection_ref=%s AND kind=%s",
                         (connection_ref, kind))
            rows = {_item_ref(kind, item): (connection_ref, kind, _item_ref(kind, item),
                                            _search_text(kind, item),
                                            Jsonb(item.model_dump(mode="json")))
                    for item in items}
            with conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO library_list_items(connection_ref,kind,item_ref,search_text,item) "
                    "VALUES(%s,%s,%s,%s,%s)", list(rows.values()))

    # --- Pools: shared reads (C2) ---------------------------------------------------------------

    @staticmethod
    def _query_key(query: object, pool_key: str | None) -> str | None:
        """A Source revision's pool key: stored, or (a pre-4 row not yet assigned) computed from
        its query by the one canonical form; None when it has no readable query."""
        if pool_key is not None:
            return pool_key
        if query is None:
            return None
        try:
            return SourceQuery.model_validate(query).pool_key()
        except ValidationError:
            return None

    @staticmethod
    def _state(row: dict | None) -> CheckState:
        if row is None:
            return NeverChecked()
        last = row["last_complete_at"]
        match row["check_state"]:
            case "checking":
                return Checking(seen_so_far=row["seen_so_far"], started_at=row["check_started_at"],
                                last_complete_at=last)
            case "complete":
                return Complete(at=last)
            case "incomplete":
                return Incomplete(reason=row["incomplete_reason"], last_complete_at=last)
            case "failed":
                return Failed(problem=row["problem"], last_complete_at=last)
            case _:
                return NeverChecked()

    @classmethod
    def check_state_in(cls, conn: Any, source_ref: str) -> CheckState:
        """The check state of a Source revision's pool (read-only: valid in a READ ONLY
        transaction, so the operator snapshot may call it)."""
        row = conn.execute("SELECT query,pool_key FROM media_sources WHERE source_ref=%s",
                           (source_ref,)).fetchone()
        key = None if row is None else cls._query_key(row["query"], row["pool_key"])
        if key is None:
            return NeverChecked()
        return cls._state(conn.execute("SELECT * FROM photo_pools WHERE pool_key=%s",
                                       (key,)).fetchone())

    @classmethod
    def pool_members_in(cls, conn: Any, source_refs: set[str]
                        ) -> dict[str, tuple[tuple[PoolMember, ...], bool]]:
        """Each Source revision's in-pool rows (newest first, no window, no limit) and whether its
        pool has a complete check. Read-only, in the caller's transaction (P1's read)."""
        answer: dict[str, tuple[tuple[PoolMember, ...], bool]] = {
            ref: ((), False) for ref in source_refs}
        keys: dict[str, str] = {}
        for row in conn.execute("SELECT source_ref,query,pool_key FROM media_sources WHERE "
                                "source_ref=ANY(%s)", (list(source_refs),)).fetchall():
            key = cls._query_key(row["query"], row["pool_key"])
            if key is not None:
                keys[row["source_ref"]] = key
        if not keys:
            return answer
        ready = {row["pool_key"]: row["last_complete_at"] is not None for row in conn.execute(
            "SELECT pool_key,last_complete_at FROM photo_pools WHERE pool_key=ANY(%s)",
            (list(set(keys.values())),)).fetchall()}
        members: dict[str, list[PoolMember]] = {key: [] for key in ready}
        rows = conn.execute(
            "SELECT pool_key,asset_id,revision,kind,taken_at,width,height FROM photo_pool_rows "
            "WHERE pool_key=ANY(%s) AND outcome='in_pool' ORDER BY pool_key,taken_at DESC,"
            "asset_id DESC", (list(ready),))
        for row in rows:
            members[row["pool_key"]].append(PoolMember(
                asset_id=row["asset_id"], revision=row["revision"], kind=row["kind"],
                taken_at=row["taken_at"], width=row["width"], height=row["height"]))
        for ref, key in keys.items():
            if key in ready:
                answer[ref] = (tuple(members[key]), ready[key])
        return answer

    def _ensure_pool_in(self, conn: Any, query: SourceQuery) -> PoolKey:
        """The query's pool, created (check requested) when no pool has its key yet."""
        self._known_connection(conn, query.connection_ref)
        key = query.pool_key()
        conn.execute("INSERT INTO photo_pools(pool_key,connection_ref,query,created_at) "
                     "VALUES(%s,%s,%s,%s) ON CONFLICT(pool_key) DO NOTHING",
                     (key, query.connection_ref, Jsonb(query.model_dump(mode="json")),
                      self.times.now_in(conn)))
        return key

    def _request_check_in(self, conn: Any, pool_key: str) -> None:
        conn.execute("UPDATE photo_pools SET check_requested=TRUE WHERE pool_key=%s", (pool_key,))
        self._queue().enqueue_pool_check_in(conn, pool_key)

    def pool_key_of_source_in(self, conn: Any, source_ref: str) -> PoolKey | None:
        """A Source revision's pool key; for a pre-4 row (query set, pool_key NULL) it assigns
        and stores the key, creating the pool (check requested). None: no such revision, no
        readable query, or its connection is gone."""
        row = conn.execute("SELECT query,pool_key FROM media_sources WHERE source_ref=%s "
                           "FOR UPDATE", (source_ref,)).fetchone()
        if row is None:
            return None
        if row["pool_key"] is not None:
            return row["pool_key"]
        try:
            query = SourceQuery.model_validate(row["query"])
        except ValidationError:
            return None
        if conn.execute("SELECT 1 FROM library_connections WHERE connection_ref=%s",
                        (query.connection_ref,)).fetchone() is None:
            return None
        key = self._ensure_pool_in(conn, query)
        conn.execute("UPDATE media_sources SET pool_key=%s WHERE source_ref=%s", (key, source_ref))
        return key

    # --- Drafts and Check now: operator side (C2) -----------------------------------------------

    def create_draft(self, query: SourceQuery, editing: SourceRef | None) -> DraftRef:
        self._queue()
        draft = str(uuid.uuid4())
        with self.db.transaction() as conn:
            editing_name = None
            if editing is not None:
                row = conn.execute(
                    "SELECT v.name FROM media_source_name_versions v JOIN media_source_names n "
                    "ON n.name=v.name WHERE v.source_ref=%s AND NOT n.deleted",
                    (editing,)).fetchone()
                if row is None:
                    raise LibraryRefused("source_unknown", 404)
                editing_name = row["name"]
            key = self._ensure_pool_in(conn, query)
            conn.execute("INSERT INTO photo_drafts(draft_ref,pool_key,editing_name,created_at) "
                         "VALUES(%s,%s,%s,%s)", (draft, key, editing_name, self.times.now_in(conn)))
            self._request_check_in(conn, key)
        return draft

    def set_draft_query(self, draft: DraftRef, query: SourceQuery) -> None:
        self._queue()
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM photo_drafts WHERE draft_ref=%s FOR UPDATE",
                            (draft,)).fetchone() is None:
                raise LibraryRefused("draft_unknown", 404)
            key = self._ensure_pool_in(conn, query)
            conn.execute("UPDATE photo_drafts SET pool_key=%s WHERE draft_ref=%s", (key, draft))
            self._request_check_in(conn, key)

    def check_source(self, source_ref: SourceRef) -> None:
        self._queue()
        with self.db.transaction() as conn:
            key = self.pool_key_of_source_in(conn, source_ref)
            if key is None:
                raise LibraryRefused("source_unknown", 404)
            self._request_check_in(conn, key)

    def draft_for_save_in(self, conn: Any, draft: DraftRef) -> DraftForSave | None:
        """The draft (locked until the caller's transaction ends) with its pool's query."""
        row = conn.execute(
            "SELECT d.pool_key,d.editing_name,p.query FROM photo_drafts d JOIN photo_pools p "
            "USING (pool_key) WHERE d.draft_ref=%s FOR UPDATE OF d", (draft,)).fetchone()
        if row is None:
            return None
        return DraftForSave(row["pool_key"], SourceQuery.model_validate(row["query"]),
                            row["editing_name"])

    def pool_of_draft_in(self, conn: Any, draft: DraftRef) -> PoolKey | None:
        found = self.draft_for_save_in(conn, draft)
        return None if found is None else found.pool_key

    @staticmethod
    def remove_draft_in(conn: Any, draft: DraftRef) -> None:
        conn.execute("DELETE FROM photo_drafts WHERE draft_ref=%s", (draft,))

    # --- Pool pages: operator side (C2) ---------------------------------------------------------

    def draft_pool(self, draft: DraftRef, after: PoolCursor | None) -> SourcePoolView:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT pool_key FROM photo_drafts WHERE draft_ref=%s",
                               (draft,)).fetchone()
            if row is None:
                raise LibraryRefused("draft_unknown", 404)
            return self._pool_view_in(conn, row["pool_key"], after)

    def source_pool(self, source_ref: SourceRef, after: PoolCursor | None) -> SourcePoolView:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT query,pool_key FROM media_sources WHERE source_ref=%s",
                               (source_ref,)).fetchone()
            if row is None:
                raise LibraryRefused("source_unknown", 404)
            return self._pool_view_in(conn, self._query_key(row["query"], row["pool_key"]), after)

    def _pool_view_in(self, conn: Any, pool_key: str | None,
                      after: PoolCursor | None) -> SourcePoolView:
        pool = None if pool_key is None else conn.execute(
            "SELECT * FROM photo_pools WHERE pool_key=%s", (pool_key,)).fetchone()
        counts = [] if pool is None else conn.execute(
            "SELECT outcome,reason,kind,count(*) AS n FROM photo_pool_rows WHERE pool_key=%s "
            "GROUP BY outcome,reason,kind", (pool_key,)).fetchall()
        rows = [] if pool is None else conn.execute(
            "SELECT asset_id,kind,taken_at,width,height FROM photo_pool_rows WHERE pool_key=%s "
            "AND outcome='in_pool' AND (%s::double precision IS NULL OR "
            "(taken_at,asset_id) < (%s::double precision,%s::text)) "
            "ORDER BY taken_at DESC,asset_id DESC LIMIT %s",
            (pool_key, None if after is None else after.taken_at,
             None if after is None else after.taken_at, None if after is None else after.asset_id,
             policy.POOL_PAGE_SIZE + 1)).fetchall()
        page = rows[:policy.POOL_PAGE_SIZE]
        more = len(rows) > policy.POOL_PAGE_SIZE
        in_pool = {kind: sum(c["n"] for c in counts if c["outcome"] == "in_pool" and c["kind"] == kind)
                   for kind in ("image", "video")}
        left_out: dict[str, int] = {}
        for count in counts:
            if count["outcome"] == "left_out":
                left_out[count["reason"]] = left_out.get(count["reason"], 0) + count["n"]
        return SourcePoolView(
            check=self._state(pool),
            in_pool=KindCounts(photos=in_pool["image"], videos=in_pool["video"]),
            waiting=sum(c["n"] for c in counts if c["outcome"] == "waiting"),
            left_out=tuple(LeftOutCount(reason=reason, count=n)
                           for reason, n in sorted(left_out.items())),
            immich_up_to=None if pool is None else pool["immich_up_to"],
            items=tuple(PoolItem(asset_id=row["asset_id"], kind=row["kind"],
                                 taken_at=row["taken_at"], width=row["width"],
                                 height=row["height"], thumbnail=THUMBNAIL_PATH + row["asset_id"])
                        for row in page),
            next=(PoolCursor(taken_at=page[-1]["taken_at"], asset_id=page[-1]["asset_id"]).encode()
                  if more else None),
            read_at=self.times.now_in(conn))

    # --- Checks: worker side (C2) ---------------------------------------------------------------

    def due_pools(self) -> tuple[PoolKey, ...]:
        """Also assigns the pool of each wanted pre-4 Source revision (C12's cut-over), so a
        converted Source is checked without anyone opening it."""
        with self.db.transaction() as conn:
            for row in conn.execute("SELECT source_ref FROM media_sources WHERE refresh_active AND "
                                    "query IS NOT NULL AND pool_key IS NULL "
                                    "ORDER BY source_ref").fetchall():
                self.pool_key_of_source_in(conn, row["source_ref"])
            now = self.times.now_in(conn)
            rows = conn.execute(
                "SELECT p.pool_key FROM photo_pools p JOIN library_connections c USING "
                f"(connection_ref) WHERE c.api_key IS NOT NULL AND {_WANTED} AND "
                "CASE WHEN p.check_state='checking' THEN p.step_at IS NULL OR p.step_at<=%s "
                "ELSE p.check_requested OR p.check_finished_at IS NULL OR p.check_finished_at<=%s "
                "END ORDER BY p.pool_key",
                (now - policy.STALE_CHECK_SECONDS, now - policy.CHECK_EVERY_SECONDS)).fetchall()
        return tuple(row["pool_key"] for row in rows)

    def begin_check(self, pool_key: PoolKey) -> CheckLease | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                f"SELECT p.*,{_WANTED} AS wanted,c.url,c.api_key,c.immich_user FROM photo_pools p "
                "JOIN library_connections c USING (connection_ref) WHERE p.pool_key=%s "
                "FOR UPDATE OF p", (pool_key,)).fetchone()
            if row is None or not row["wanted"] or row["api_key"] is None:
                return None
            now = self.times.now_in(conn)
            if row["check_state"] != "checking":
                row = conn.execute(
                    "UPDATE photo_pools SET check_number=check_number+1,check_state='checking',"
                    "check_started_at=%s,step_at=%s,progress=NULL,seen_so_far=0,problem=NULL,"
                    "incomplete_reason=NULL,check_requested=FALSE WHERE pool_key=%s "
                    "RETURNING check_number,check_started_at,progress",
                    (now, now, pool_key)).fetchone() | {
                        key: row[key] for key in ("connection_ref", "url", "api_key",
                                                  "immich_user", "query")}
            else:
                conn.execute("UPDATE photo_pools SET step_at=%s WHERE pool_key=%s",
                             (now, pool_key))
        return CheckLease(pool_key=pool_key, query=SourceQuery.model_validate(row["query"]),
                          connection=self._secret(row), check_number=row["check_number"],
                          started_at=row["check_started_at"], progress=row["progress"])

    def known_revisions(self, connection_ref: ConnectionRef,
                        upstream_ids: Sequence[LibraryRef]) -> dict[str, KnownRevision]:
        if not upstream_ids:
            return {}
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT DISTINCT ON (upstream_id) upstream_id::text AS upstream_id,updated_at,"
                "metadata->>'revision' AS revision FROM asset_revisions WHERE connection_ref=%s "
                "AND upstream_id=ANY(%s::uuid[]) AND updated_at IS NOT NULL "
                "ORDER BY upstream_id,last_seen DESC", (connection_ref, list(upstream_ids))).fetchall()
        return {row["upstream_id"]: KnownRevision(updated_at=row["updated_at"],
                                                  revision=row["revision"]) for row in rows}

    @staticmethod
    def _fenced(conn: Any, lease: CheckLease) -> bool:
        row = conn.execute("SELECT check_number,check_state FROM photo_pools WHERE pool_key=%s "
                           "FOR UPDATE", (lease.pool_key,)).fetchone()
        return (row is not None and row["check_number"] == lease.check_number
                and row["check_state"] == "checking")

    def record_step(self, lease: CheckLease, step: CheckStep) -> bool:
        queue = self._queue()
        with self.db.transaction() as conn:
            if not self._fenced(conn, lease):
                return False
            now = self.times.now_in(conn)
            with conn.cursor() as cursor:
                if step.marks:
                    cursor.executemany(
                        "INSERT INTO photo_check_marks(pool_key,check_number,upstream_id,mark) "
                        "VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        [(lease.pool_key, lease.check_number, mark.upstream_id, mark.mark)
                         for mark in step.marks])
                marked = self._marked_in(conn, lease, (self._upstream(e) for e in step.entries))
                entries = [e for e in step.entries if self._upstream(e) not in marked]
                assets = {e.asset.asset_id: e for e in entries if isinstance(e, InPool)}
                if assets:
                    cursor.executemany(
                        "INSERT INTO asset_revisions(asset_id,metadata,first_seen,last_seen,"
                        "connection_ref,upstream_id,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT(asset_id) DO UPDATE SET metadata=EXCLUDED.metadata,"
                        "last_seen=EXCLUDED.last_seen,connection_ref=EXCLUDED.connection_ref,"
                        "upstream_id=EXCLUDED.upstream_id,updated_at=EXCLUDED.updated_at",
                        [(entry.asset.asset_id, Jsonb(entry.asset.model_dump(mode="json")), now,
                          now, entry.asset.connection_ref, entry.asset.upstream_id,
                          entry.updated_at) for entry in assets.values()])
                if entries:
                    cursor.executemany(
                        "INSERT INTO photo_pool_rows(pool_key,upstream_id,outcome,reason,asset_id,"
                        "revision,kind,taken_at,width,height,seen_check) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(pool_key,upstream_id) "
                        "DO UPDATE SET outcome=EXCLUDED.outcome,reason=EXCLUDED.reason,"
                        "asset_id=EXCLUDED.asset_id,revision=EXCLUDED.revision,kind=EXCLUDED.kind,"
                        "taken_at=EXCLUDED.taken_at,width=EXCLUDED.width,height=EXCLUDED.height,"
                        "seen_check=EXCLUDED.seen_check",
                        [(lease.pool_key, *self._row(entry), lease.check_number)
                         for entry in entries])
            conn.execute("UPDATE photo_pools SET progress=%s,seen_so_far=%s,"
                         "immich_up_to=COALESCE(%s,immich_up_to) WHERE pool_key=%s",
                         (Jsonb(step.progress), step.seen_so_far, step.immich_up_to,
                          lease.pool_key))
            queue.enqueue_pool_check_in(conn, lease.pool_key)
            return True

    @staticmethod
    def _upstream(entry: InPool | Waiting | LeftOut) -> str:
        return entry.asset.upstream_id if isinstance(entry, InPool) else entry.upstream_id

    @staticmethod
    def _marked_in(conn: Any, lease: CheckLease, upstream_ids: Iterable[str]) -> set[str]:
        """Ids carrying a mark of this check (stored by this step or an earlier one): the
        subtraction's one home."""
        ids = list(set(upstream_ids))
        if not ids:
            return set()
        return {row["upstream_id"] for row in conn.execute(
            "SELECT DISTINCT upstream_id::text AS upstream_id FROM photo_check_marks WHERE "
            "pool_key=%s AND check_number=%s AND upstream_id=ANY(%s::uuid[])",
            (lease.pool_key, lease.check_number, ids)).fetchall()}

    @staticmethod
    def _row(entry: InPool | Waiting | LeftOut) -> tuple:
        """(upstream_id, outcome, reason, asset_id, revision, kind, taken_at, width, height)."""
        if isinstance(entry, InPool):
            asset: LibraryAsset = entry.asset
            return (asset.upstream_id, "in_pool", None, asset.asset_id, asset.revision, asset.kind,
                    asset.taken_at, asset.width, asset.height)
        if isinstance(entry, Waiting):
            return (entry.upstream_id, "waiting", None, None, None, entry.kind, entry.taken_at,
                    None, None)
        return (entry.upstream_id, "left_out", entry.reason, None, None, entry.kind,
                entry.taken_at, None, None)

    def finish_check(self, lease: CheckLease, finish: CheckFinish) -> bool:
        with self.db.transaction() as conn:
            if not self._fenced(conn, lease):
                return False
            now = self.times.now_in(conn)
            if isinstance(finish, CheckComplete):
                # The sweep (design rule 2): only a complete check removes what it did not see,
                # and what it subtracted (an id marked after an earlier step of it added a row).
                conn.execute(
                    "DELETE FROM photo_pool_rows r WHERE r.pool_key=%(key)s AND "
                    "(r.seen_check<%(n)s OR EXISTS (SELECT 1 FROM photo_check_marks m WHERE "
                    "m.pool_key=r.pool_key AND m.check_number=%(n)s AND "
                    "m.upstream_id=r.upstream_id))", {"key": lease.pool_key, "n": lease.check_number})
                conn.execute("DELETE FROM photo_check_marks WHERE pool_key=%s AND check_number<%s",
                             (lease.pool_key, lease.check_number))
                conn.execute("UPDATE photo_pools SET check_state='complete',check_finished_at=%s,"
                             "last_complete_at=%s,progress=NULL WHERE pool_key=%s",
                             (now, now, lease.pool_key))
            elif isinstance(finish, CheckIncomplete):
                conn.execute("UPDATE photo_pools SET check_state='incomplete',incomplete_reason=%s,"
                             "check_finished_at=%s,progress=NULL WHERE pool_key=%s",
                             (finish.reason, now, lease.pool_key))
            else:
                conn.execute("UPDATE photo_pools SET check_state='failed',problem=%s,"
                             "check_finished_at=%s,progress=NULL WHERE pool_key=%s",
                             (finish.problem, now, lease.pool_key))
            return True

    def frame_sizes(self) -> tuple[tuple[int, int], ...]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT DISTINCT (profile->>'width_px')::int AS width,"
                "(profile->>'height_px')::int AS height FROM frames "
                "ORDER BY width, height").fetchall()
        return tuple((row["width"], row["height"]) for row in rows)

    def pool_thumbnail(self, asset_id: str) -> PoolThumbnail | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT p.connection_ref,r.upstream_id::text AS upstream_id,r.revision "
                f"FROM photo_pool_rows r JOIN photo_pools p USING (pool_key) WHERE r.asset_id=%s "
                f"AND r.outcome='in_pool' AND {_WANTED} LIMIT 1", (asset_id,)).fetchone()
        if row is None:
            return None
        return PoolThumbnail(connection_ref=row["connection_ref"], upstream_id=row["upstream_id"],
                             revision=row["revision"])

    def retire_unwanted(self) -> frozenset[str]:
        with self.db.transaction() as conn:
            now = self.times.now_in(conn)
            conn.execute("DELETE FROM photo_drafts WHERE created_at<=%s",
                         (now - policy.DRAFT_KEEP_SECONDS,))
            unwanted = [row["pool_key"] for row in conn.execute(
                f"SELECT p.pool_key FROM photo_pools p WHERE NOT {_WANTED} FOR UPDATE").fetchall()]
            if unwanted:
                # A dormant revision forgets its pool; should Runtime need it again, its pool is
                # made anew from its query (pool_key_of_source_in).
                conn.execute("UPDATE media_sources SET pool_key=NULL WHERE pool_key=ANY(%s)",
                             (unwanted,))
                conn.execute("DELETE FROM photo_pools WHERE pool_key=ANY(%s)", (unwanted,))
            conn.execute(
                "DELETE FROM assets a WHERE a.kind='library-thumbnail' AND NOT EXISTS "
                "(SELECT 1 FROM photo_pool_rows r JOIN photo_pools p USING (pool_key) WHERE "
                f"r.asset_id=a.identity AND r.outcome='in_pool' AND {_WANTED})")
            rows = conn.execute("SELECT identity FROM assets WHERE kind='library-thumbnail'")
            return frozenset(row["identity"] for row in rows.fetchall())
