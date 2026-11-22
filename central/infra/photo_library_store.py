"""The photo library store over PostgreSQL (roadmap 4; run ledger .claude/runs/photo-4.md).

`PgPhotoLibrary` is the one home of the photo library's tables (library_connections with its
lists, photo_pools, photo_pool_rows, photo_check_pages, library_assets, and the draft rows of
media_sources; decision 0034, migration 124) and the only
code that reads `library_connections.api_key` (for the media worker: `begin_contact`,
`connection_secret`, `begin_check`). It implements central.photo_library.ports'
PhotoLibraryQueries and PhotoLibraryCommands (slice C1), ManagedConnectionCommands (the connection
the deployment sets), PhotoSourceQueries and the store half of
PhotoSourceCommands (slice C2), and LibraryWorkerStore (C1 contacts and lists, C2 checks).

Every time written or compared here is `times`' (the database's, G11). Fences are numbers the
database hands out (contact numbers, check numbers), never clock comparisons across processes.

A pool is **wanted** while a draft points at it, or while a Source revision Runtime or a name
needs (`media_sources.refresh_active`, computed by MediaRepository.reconcile_source_activity_in)
names it. A draft is a `media_sources` row of its own (decision 0034): its ref is the draft's, it
has no query and no name, its pool is its `pool_key`, and `draft` holds the name it edits. Only wanted pools are checked, served as thumbnails, or kept by maintenance.

Photos are normalized (follow-ups 2026-10-11, owner item 6; decision 0034): one `library_assets`
row per Immich asset holds its facts once: kind, taken time, the original file's facts, its
current revision (`asset_id`, upright size, duration, Immich's `updatedAt`), that revision's
measured Immich copies (`copies`, by copy name) and its key content (`key_*`,
central/infra/key_content_store.py). A new revision replaces the old one on the row, clearing its
copies and key content; the pool rows naming the old revision follow it (the foreign key's
`ON UPDATE CASCADE`). A pool row names a revision (`asset_id`) and copies none of its facts, so
every pool read joins the photo's row; `library_assets_in` is the one place a stored asset
becomes a LibraryAsset.

A connection's lists are one document per kind on its row (`library_connections.lists`:
{kind: {status, error, observed_at, checked_at, items: [{ref, search, item}]}}), written by the
list check alone (`record_list`).

A pool check writes what changed, plus one page per step for what it saw (decision 0033): a row
of `library_assets` or `photo_pool_rows` is written only when it is new or a fact it stores
differs (`record_step`), stamped `library-check`, its only writer; what a check
saw or marked is a `photo_check_pages` row per step and kind holding the ids, never a row per id.
"""

from __future__ import annotations

import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from typing import Any, Literal, NamedTuple

from psycopg.types.json import Jsonb
from pydantic import SecretStr, TypeAdapter, ValidationError

from central.actor import Actor
from central.db import Database, TransactionClock
from central.infra.display_store import frame_outputs_in
from central.infra.house_store import house_time_in
from central.installation_models import FrameOutput
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
    CopySize,
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
    MeasurableCopy,
    MeasuredCopy,
    NeverChecked,
    OriginalFile,
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
    LeanMember,
    LibraryRefused,
    Provision,
    RankedLine,
)
from central.photo_library.rendition import covers, frame_need
from central.photo_library.views import (
    ConnectionWrite,
    CopyView,
    FrameCopy,
    KindCounts,
    LeftOutCount,
    LibraryConnectionView,
    ListAnswer,
    PhotoFacts,
    PoolItem,
    SourcePoolView,
)
from central.wall_plan.order import (
    ORDERS,
    SHUFFLE_FIELDS,
    SHUFFLE_SEPARATOR,
    TIER_FRESH,
    TIER_PICKED,
    TIER_RECENT,
    LineQuery,
    ShapeBounds,
    SourceLine,
)
from central.wall_plan.selection import Order

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
# One connection's listed items of one kind, each `e` ({ref, search, item}); `%(ref)s` names the
# connection and `%(kind)s` the kind.
_ITEMS = ("library_connections c, jsonb_array_elements(COALESCE(c.lists->%(kind)s->'items',"
          "'[]'::jsonb)) AS e WHERE c.connection_ref=%(ref)s")
_ITEM_TYPES: dict[str, type] = {"albums": LibraryAlbum, "people": LibraryPerson, "places": Place,
                                "tags": LibraryTag}
# A pool is wanted (module docstring); `p` names the photo_pools row.
_WANTED = ("EXISTS (SELECT 1 FROM media_sources s WHERE s.pool_key=p.pool_key AND "
           "(s.refresh_active OR s.draft IS NOT NULL))")
# The audit stamp of a write: `changed_at` then `changed_by`.
_STAMP = "changed_at=%s,changed_by=%s"
# A Source revision's row, never a draft's (`s` names the media_sources row).
_REVISION_ROW = "s.draft IS NULL"
# The one writer of the pool check's tables and its pages (central/actor.py).
_CHECK_ACTOR: Actor = "library-check"
# The ids in the pages of pool `%(key)s`'s check `%(n)s`, as one set `upstream_id`: every read of
# pages joins this set (a hash join), never searches a page once per row (a page holds 1,000 ids).
_CHECK_IDS = ("(SELECT DISTINCT unnest(upstream_ids) AS upstream_id FROM photo_check_pages "
              "WHERE pool_key=%(key)s AND check_number=%(n)s{kinds})")
# Seen or marked by the check: what its Re-check phase need not ask about.
_SEEN_OR_MARKED = _CHECK_IDS.format(kinds="")
# Marked by the check (an excluded id or a stacked copy): the subtraction's one set, for the
# step's entries and for the finish.
_MARKED = _CHECK_IDS.format(kinds=" AND kind<>'seen'")
# An in-pool row `r` joined to its photo `l` (owner item 6: the facts live once, on the photo's
# row), and the columns of a PoolMember.
_REVISION_JOIN = "JOIN library_assets l ON l.asset_id=r.asset_id"
_MEMBER_COLUMNS = "r.asset_id,l.revision,l.kind,l.taken_at,l.width,l.height"

# A Scene line's rows (follow-up S3): one per asset id of the query's pools, as `m`.
_LINE_ROWS = ("SELECT l.asset_id,l.revision,l.kind,l.taken_at,l.width,l.height FROM "
              "(SELECT DISTINCT asset_id FROM photo_pool_rows WHERE pool_key=ANY(%(pools)s) "
              "AND outcome='in_pool') AS r JOIN library_assets l USING (asset_id)")
# On this day on the house calendar (roadmap 2b): `order.py` `on_day` reads the same zone.
_ON_THIS_DAY = "to_char(to_timestamp(m.taken_at) AT TIME ZONE %(zone)s,'MM-DD')=%(today)s"
_SHUFFLE_TEXT = {"scene_id": "%(scene)s::text", "pass_number": "%(pass)s::text",
                 "asset_id": "m.asset_id"}
_ORDER_FIELDS = {
    "shuffle": "sha256(" + " || %(sep)s::bytea || ".join(
        f"convert_to({_SHUFFLE_TEXT[name]},'UTF8')" for name in SHUFFLE_FIELDS) + ")",
    "wrapped": ("CASE WHEN %(mark_id)s::text IS NOT NULL AND (m.taken_at,m.asset_id COLLATE \"C\")"
                " <= (%(mark_at)s::float8,%(mark_id)s::text COLLATE \"C\") THEN 1 ELSE 0 END"),
    "captured_at": "m.taken_at",
    "asset_id": "m.asset_id COLLATE \"C\"",
    "off_this_day": f"CASE WHEN {_ON_THIS_DAY} THEN 0 ELSE 1 END",
}


def _line_order_sql(order: Order) -> str:
    """The SQL interpreter of central/wall_plan/order.py's tiers and `ORDERS[order]` (the Python
    one is `order_key`); tests/test_order_sql_conformance.py holds the two equal."""
    terms = ["CASE WHEN m.asset_id=ANY(%(picked)s::text[]) THEN %(tier_picked)s "
             "WHEN m.asset_id=ANY(%(recent)s::text[]) THEN %(tier_recent)s ELSE %(fresh)s END"]
    for term in ORDERS[order]:
        expression = _ORDER_FIELDS[term.field]
        if term.on_this_day_only:
            expression = f"CASE WHEN {_ON_THIS_DAY} THEN {expression} END"
        terms.append(expression + (" DESC" if term.descending else ""))
    return ",".join(terms)


def _shape_sql(shape: ShapeBounds, params: dict[str, Any]) -> str:
    """`ShapeBounds.admits` in SQL over the line's revision sizes; adds its values to `params`."""
    clauses = []
    for index, bounds in enumerate(shape.classes):
        params.update({f"low{index}": bounds.min_ratio, f"high{index}": bounds.max_ratio,
                       f"width{index}": bounds.min_width, f"height{index}": bounds.min_height})
        video = "m.kind='video'" if bounds.video else "FALSE"
        clauses.append(
            f"(m.width::float8/m.height BETWEEN %(low{index})s AND %(high{index})s AND ({video} "
            f"OR (m.kind='image' AND m.width>=%(width{index})s AND m.height>=%(height{index})s)))")
    return "(" + (" OR ".join(clauses) or "FALSE") + ")"


def _lean(row: dict, pool_keys: dict[str, list[str]]) -> LeanMember:
    return LeanMember(asset_id=row["asset_id"], revision=row["revision"], kind=row["kind"],
                      taken_at=row["taken_at"], width=row["width"], height=row["height"],
                      pool_keys=tuple(pool_keys.get(row["asset_id"], ())))


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


def library_assets_in(conn: Any, asset_ids: Iterable[str]) -> dict[str, LibraryAsset]:
    """Each named revision as a LibraryAsset, in the caller's transaction (follow-ups 2026-10-11,
    owner item 6; decision 0034): its photo's `library_assets` row (connection, Immich id, kind,
    taken time, the revision's width, height and duration, the original's facts as
    `OriginalFile`, None when every original column is NULL). An id that is no photo's current
    revision is absent. The one place a stored asset becomes a LibraryAsset: job leases
    (`central.media_repository`'s variant inputs) and the key content measurement
    (roadmap 5a, `central.infra.key_content_store`) all read here."""
    ids = list(set(asset_ids))
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT asset_id,revision,width,height,duration,connection_ref,"
        "upstream_id::text AS upstream_id,kind,taken_at,original_media_type,"
        "original_width,original_height,original_orientation,original_bytes "
        "FROM library_assets WHERE asset_id=ANY(%s)", (ids,)).fetchall()
    return {row["asset_id"]: LibraryAsset(
        connection_ref=row["connection_ref"], upstream_id=row["upstream_id"],
        revision=row["revision"], kind=row["kind"], taken_at=row["taken_at"],
        width=row["width"], height=row["height"], duration=row["duration"],
        original=_original_of(row)) for row in rows}


_ORIGINAL_COLUMNS = ("original_media_type", "original_width", "original_height",
                     "original_orientation", "original_bytes")


def _original_of(row: dict) -> OriginalFile | None:
    """A `library_assets` row's original file; None when Immich has said nothing of it."""
    if all(row[column] is None for column in _ORIGINAL_COLUMNS):
        return None
    return OriginalFile(media_type=row["original_media_type"], width=row["original_width"],
                        height=row["original_height"], orientation=row["original_orientation"],
                        size_bytes=row["original_bytes"])


def _copy_sizes_in(conn: Any, asset_id: str) -> dict[MeasurableCopy, CopySize]:
    """A revision's measured Immich copies (its photo row's `copies`)."""
    row = conn.execute("SELECT copies FROM library_assets WHERE asset_id=%s",
                       (asset_id,)).fetchone()
    copies = {} if row is None else row["copies"]
    return {name: CopySize(name=name, media_type=copy["media_type"], width=copy["width"],
                           height=copy["height"], measured_at=copy["measured_at"])
            for name, copy in copies.items()}


def _frame_copy(frame: FrameOutput, preview: CopySize | None) -> FrameCopy | None:
    """What `frame` asks of a photo's copy; None for a Frame without a Frame output (it asks
    none: `frame_need`)."""
    need = frame_need(frame)
    if need is None:
        return None
    return FrameCopy(frame_id=frame.frame_id, need_px=(need.width, need.height),
                     preview_holds=None if preview is None
                     else covers(preview.width, preview.height, (need,)))


def _original_values(original: OriginalFile | None) -> tuple:
    """The five `original_*` column values of `original`, in `_ORIGINAL_COLUMNS`' order (None:
    five NULLs)."""
    if original is None:
        return (None,) * 5
    return (original.media_type, original.width, original.height, original.orientation,
            original.size_bytes)


# The facts a pool check compares (decision 0033): a revision's, and a pool row's.
_REVISION_FACTS = ("asset_id", "width", "height", "duration", "revision_updated_at")
# A revision's columns on its photo's row (`asset_id` names the revision, so it carries it), and
# what is measured on it: key content's (central/infra/key_content_store.py) and the copies.
_REVISION_COLUMNS = ("asset_id", "revision", "width", "height", "duration", "revision_updated_at")
_KEY_COLUMNS = ("key_analysis_version", "key_status", "key", "key_failure", "key_measured_at",
                "key_retry_at")
_POOL_ROW_FACTS = ("outcome", "reason", "asset_id")


def _differs(stored: dict | None, facts: dict, columns: Sequence[str]) -> bool:
    """Whether a row read is missing or any compared fact differs from the step's."""
    return stored is None or any(stored[column] != facts[column] for column in columns)


def _asset_differs(stored: dict | None, facts: dict) -> bool:
    """A `library_assets` row: kind and taken time, and the original's five facts and the
    revision's when the step read them (an InPool)."""
    return _differs(stored, facts, ("kind", "taken_at", *(
        (*_ORIGINAL_COLUMNS, *_REVISION_FACTS) if facts["read"] else ())))


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
                "counts,managed FROM library_connections ORDER BY name, connection_ref").fetchall()
        return tuple(LibraryConnectionView(
            connection_ref=row["connection_ref"], name=row["name"], url=row["url"],
            key_hint=None if row["key_tail"] is None else policy.key_hint(row["key_tail"]),
            immich_user=row["immich_user"], health=row["health"], counts=row["counts"],
            required_permissions=policy.REQUIRED_PERMISSIONS, managed=row["managed"])
            for row in rows)

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

    def add_connection(self, write: ConnectionWrite, *, by: Actor) -> ConnectionRef:
        url, key = self._url(write.url), self._key(write)
        if key is None:
            raise LibraryRefused("key_required", 422)
        connection_ref = "library-" + uuid.uuid4().hex[:12]
        queue = self._queue()
        with self.db.transaction() as conn:
            self._name_taken(conn, write.name, None)
            now = self.times.now_in(conn)
            conn.execute("INSERT INTO library_connections(connection_ref,name,url,api_key,"
                         "created_at,created_by,changed_at,changed_by) "
                         "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                         (connection_ref, write.name, url, key, now, by, now, by))
            queue.enqueue_connection_check_in(conn, connection_ref, due_at=now)
        return connection_ref

    def change_connection(self, connection_ref: ConnectionRef, write: ConnectionWrite, *,
                          by: Actor) -> None:
        url, key = self._url(write.url), self._key(write)
        queue = self._queue()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT api_key IS NOT NULL AS keyed,managed FROM "
                               "library_connections WHERE connection_ref=%s FOR UPDATE",
                               (connection_ref,)).fetchone()
            if row is None:
                raise LibraryRefused("connection_unknown", 404)
            if row["managed"]:
                raise LibraryRefused("connection_managed", 409)
            if key is None and not row["keyed"]:
                raise LibraryRefused("key_required", 422)   # a cut-over row's first paste (C12)
            self._name_taken(conn, write.name, connection_ref)
            conn.execute("UPDATE library_connections SET name=%s,url=%s,"
                         "api_key=COALESCE(%s,api_key),changed_at=%s,changed_by=%s "
                         "WHERE connection_ref=%s",
                         (write.name, url, key, self.times.now_in(conn), by, connection_ref))
            queue.enqueue_connection_check_in(conn, connection_ref,
                                              due_at=self.times.now_in(conn))

    def remove_connection(self, connection_ref: ConnectionRef) -> None:
        """Refused while a saved Source (a named Source's current revision, or a revision Runtime
        needs) or a draft uses it. Older revisions nothing needs keep their query, lose their
        pool, and the connection's pools and lists go with it."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT managed FROM library_connections WHERE connection_ref=%s "
                               "FOR UPDATE", (connection_ref,)).fetchone()
            if row is None:
                raise LibraryRefused("connection_unknown", 404)
            if row["managed"]:
                raise LibraryRefused("connection_managed", 409)
            used = conn.execute(
                "SELECT 1 FROM media_sources s WHERE s.query->>'connection_ref'=%s AND "
                "(s.refresh_active OR (s.head AND NOT s.deleted)) "
                "UNION ALL SELECT 1 FROM media_sources d JOIN photo_pools p USING (pool_key) "
                "WHERE d.draft IS NOT NULL AND p.connection_ref=%s LIMIT 1",
                (connection_ref, connection_ref)).fetchone()
            if used is not None:
                raise LibraryRefused("sources_use_connection", 409)
            conn.execute("UPDATE media_sources SET pool_key=NULL WHERE pool_key IN "
                         "(SELECT pool_key FROM photo_pools WHERE connection_ref=%s)",
                         (connection_ref,))
            conn.execute("DELETE FROM photo_pools WHERE connection_ref=%s", (connection_ref,))
            conn.execute("DELETE FROM library_connections WHERE connection_ref=%s",
                         (connection_ref,))

    # --- The deployment's connection (provisioning.py; ManagedConnectionCommands) ---------------

    def provision_connection(self, url: str, key: SecretStr) -> Provision:
        url, secret = self._url(url), key.get_secret_value().strip()
        if not secret:
            raise LibraryRefused("key_required", 422)
        queue = self._queue()
        with self.db.transaction() as conn:
            # Two Centrals starting together take turns; the unique index keeps one managed row.
            conn.execute("LOCK TABLE library_connections IN SHARE ROW EXCLUSIVE MODE")
            managed = conn.execute("SELECT connection_ref,name,url,api_key FROM library_connections "
                                   "WHERE managed").fetchone()
            outcome: Literal["created", "adopted", "updated"]
            if managed is not None:
                ref = managed["connection_ref"]
                named = self._name_if_unnamed(conn, managed, self.times.now_in(conn))
                if (managed["url"], managed["api_key"]) == (url, secret):
                    return Provision(connection_ref=ref, outcome="renamed" if named else "unchanged")
                outcome = "updated"
            else:
                adopted = conn.execute(
                    "SELECT connection_ref,name FROM library_connections WHERE api_key=%(key)s OR "
                    "url=%(url)s OR url IS NULL ORDER BY api_key IS NOT DISTINCT FROM %(key)s DESC,"
                    " url IS NOT DISTINCT FROM %(url)s DESC, created_at, connection_ref LIMIT 1",
                    {"key": secret, "url": url}).fetchone()
                if adopted is not None:
                    ref, outcome = adopted["connection_ref"], "adopted"
                    self._name_if_unnamed(conn, adopted, self.times.now_in(conn))
                else:
                    ref, outcome = "library-" + uuid.uuid4().hex[:12], "created"
                    now = self.times.now_in(conn)
                    conn.execute("INSERT INTO library_connections(connection_ref,name,created_at,"
                                 "created_by,changed_at,changed_by) "
                                 "VALUES(%s,%s,%s,'central',%s,'central')",
                                 (ref, self._free_name(conn), now, now))
            conn.execute("UPDATE library_connections SET url=%s,api_key=%s,managed=TRUE,"
                         "changed_at=%s,changed_by='central' WHERE connection_ref=%s",
                         (url, secret, self.times.now_in(conn), ref))
            queue.enqueue_connection_check_in(conn, ref, due_at=self.times.now_in(conn))
        return Provision(connection_ref=ref, outcome=outcome)

    @classmethod
    def _name_if_unnamed(cls, conn: Any, row: Any, now: float) -> bool:
        """A managed connection whose name is still its ref (migration 080's cut-over row, e.g.
        `immich-main`) never had a name chosen, and the operator cannot rename a managed one: it
        takes `_free_name`'s. A name someone chose is kept. True when it was renamed."""
        if row["name"] != row["connection_ref"]:
            return False
        conn.execute("UPDATE library_connections SET name=%s,changed_at=%s,changed_by='central' "
                     "WHERE connection_ref=%s", (cls._free_name(conn), now, row["connection_ref"]))
        return True

    @staticmethod
    def _free_name(conn: Any) -> str:
        """"Immich", or "Immich 2", "Immich 3"… when an ordinary connection has the name."""
        taken = {row["name"] for row in conn.execute(
            "SELECT name FROM library_connections WHERE name ~ '^Immich( [0-9]+)?$'").fetchall()}
        return next(name for name in ("Immich", *(f"Immich {n}" for n in range(2, len(taken) + 3)))
                    if name not in taken)

    def release_managed_connection(self) -> ConnectionRef | None:
        with self.db.transaction() as conn:
            row = conn.execute("UPDATE library_connections SET managed=FALSE,changed_at=%s,"
                               "changed_by='central' WHERE managed RETURNING connection_ref",
                               (self.times.now_in(conn),)).fetchone()
        return None if row is None else row["connection_ref"]

    def check_connection(self, connection_ref: ConnectionRef) -> None:
        queue = self._queue()
        with self.db.transaction() as conn:
            self._known_connection(conn, connection_ref)
            queue.enqueue_connection_check_in(conn, connection_ref,
                                              due_at=self.times.now_in(conn))

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
        """The kind's list status on the connection's row ("pending": never listed)."""
        row = conn.execute("SELECT lists->%s->>'status' AS status,"
                           "(lists->%s->>'observed_at')::float8 AS observed_at "
                           "FROM library_connections WHERE connection_ref=%s",
                           (kind, kind, connection_ref)).fetchone()
        if row is None:
            raise LibraryRefused("connection_unknown", 404)
        listed = row if row["status"] is not None else {"status": "pending", "observed_at": None}
        return listed, self.times.now_in(conn)

    def _search(self, kind: str, connection_ref: str, q: str, limit: int) -> ListAnswer:
        """Items whose any field starts with `q` first, then those containing it, each group by
        search text then ref; `total_matches` counts every match before `limit` cuts."""
        needle = _fold(str(visible_text(q)).strip())
        with self.db.transaction() as conn:
            listed, read_at = self._list_row(conn, kind, connection_ref)
            rows = conn.execute(
                "SELECT item, count(*) OVER () AS total FROM ("
                " SELECT e->'item' AS item, e->>'ref' AS item_ref, e->>'search' AS search_text,"
                "  strpos(chr(31)||(e->>'search'), chr(31)||%(q)s)=0 AS later"
                f" FROM {_ITEMS} AND e->>'search'<>''"
                "  AND strpos(e->>'search', %(q)s)>0) matches "
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
                f"SELECT e->>'ref' AS item_ref,e->'item' AS item FROM {_ITEMS} "
                "AND e->>'ref'=ANY(%(ids)s)",
                {"ref": connection_ref, "kind": kind, "ids": list(ids)}).fetchall()}
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

    @staticmethod
    def _secret(row: dict) -> ConnectionSecret:
        user = row["immich_user"]
        return ConnectionSecret(connection_ref=row["connection_ref"], base_url=row["url"],
                                api_key=SecretStr(row["api_key"]),
                                bound_user=None if user is None else user["user_ref"])

    def begin_contact(self, connection_ref: ConnectionRef) -> ContactTicket | None:
        queue = self._queue()
        with self.db.transaction() as conn:
            now = self.times.now_in(conn)
            row = conn.execute(
                "UPDATE library_connections SET last_contact=last_contact+1,contact_started_at=%s "
                "WHERE connection_ref=%s AND api_key IS NOT NULL "
                "RETURNING connection_ref,url,api_key,immich_user,last_contact",
                (now, connection_ref)).fetchone()
            if row is None:
                return None
            # The next check, armed where this one starts (`contact_started_at`, the time the
            # check period counts from): a contact that raises still leaves its next one queued.
            queue.enqueue_connection_check_in(conn, connection_ref,
                                              due_at=policy.next_check_due(now, now))
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
            answered = isinstance(result, ContactAnswer)   # a refusal says nothing of copies
            conn.execute(
                "UPDATE library_connections SET applied_contact=%s,health=%s,immich_user=%s,"
                "counts=COALESCE(%s,counts),full_size_copies=CASE WHEN %s THEN %s "
                "ELSE full_size_copies END WHERE connection_ref=%s",
                (ticket.contact_number, Jsonb(_HEALTH.dump_python(health, mode="json")),
                 None if bound is None else Jsonb(bound.model_dump(mode="json")),
                 None if counts is None else Jsonb(counts.model_dump(mode="json")),
                 answered, result.full_size_copies if answered else None,
                 ticket.connection.connection_ref))
            return True

    def record_list(self, connection_ref: ConnectionRef, kind: ListKind,
                    items: Sequence[LibraryAlbum | LibraryPerson | Place | LibraryTag] | None,
                    error: str | None) -> None:
        if (items is None) == (error is None):
            raise ValueError("record either items or an error")
        with self.db.transaction() as conn:
            now = self.times.now_in(conn)
            if items is None:
                # A failed listing keeps what the last good one listed, and when.
                conn.execute(
                    "UPDATE library_connections SET lists=jsonb_set(lists,ARRAY[%(kind)s::text],"
                    "COALESCE(lists->%(kind)s,'{\"items\":[],\"observed_at\":null}'::jsonb) "
                    "|| %(listed)s) WHERE connection_ref=%(ref)s",
                    {"kind": kind, "ref": connection_ref, "listed": Jsonb({
                        "status": _LIST_STATUS.get(error, "unavailable"), "error": error,
                        "checked_at": now})})
                return
            # Keyed by item ref: an item listed twice is kept once, the later winning.
            listed = {_item_ref(kind, item): {"ref": _item_ref(kind, item),
                                              "search": _search_text(kind, item),
                                              "item": item.model_dump(mode="json")}
                      for item in items}
            # Nothing when removed while the worker listed: nothing to keep.
            conn.execute(
                "UPDATE library_connections SET lists=jsonb_set(lists,ARRAY[%s::text],%s) "
                "WHERE connection_ref=%s",
                (kind, Jsonb({"status": "ok", "error": None, "observed_at": now, "checked_at": now,
                              "items": [listed[ref] for ref in sorted(listed)]}),
                 connection_ref))

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
        row = conn.execute("SELECT query,pool_key FROM media_sources s WHERE source_ref=%s "
                           f"AND {_REVISION_ROW}", (source_ref,)).fetchone()  # noqa: S608
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
        for row in conn.execute("SELECT source_ref,query,pool_key FROM media_sources s WHERE "
                                f"source_ref=ANY(%s) AND {_REVISION_ROW}",  # noqa: S608
                                (list(source_refs),)).fetchall():
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
            f"SELECT r.pool_key,{_MEMBER_COLUMNS} FROM photo_pool_rows r {_REVISION_JOIN} "
            "WHERE r.pool_key=ANY(%s) AND r.outcome='in_pool' ORDER BY r.pool_key,"
            "l.taken_at DESC,r.asset_id DESC", (list(ready),))
        for row in rows:
            members[row["pool_key"]].append(PoolMember(
                asset_id=row["asset_id"], revision=row["revision"], kind=row["kind"],
                taken_at=row["taken_at"], width=row["width"], height=row["height"]))
        for ref, key in keys.items():
            if key in ready:
                answer[ref] = (tuple(members[key]), ready[key])
        return answer

    @staticmethod
    def connection_health_in(conn: Any) -> dict[str, LibraryHealth]:
        """connection ref -> that connection's health as its last check left it, for every
        connection. Read-only, in the caller's transaction (a Frame's status)."""
        return {row["connection_ref"]: _HEALTH.validate_python(row["health"]) for row in conn.execute(
            "SELECT connection_ref,health FROM library_connections").fetchall()}

    @staticmethod
    def source_connections_in(conn: Any, source_refs: set[str]) -> dict[str, str]:
        """Each named Source revision's connection ref (its query's own); a ref naming no revision,
        or one whose query names no connection, is left out. Read-only, in the caller's transaction."""
        return {row["source_ref"]: row["connection_ref"] for row in conn.execute(
            "SELECT source_ref,query->>'connection_ref' AS connection_ref FROM media_sources s WHERE "
            f"source_ref=ANY(%s) AND {_REVISION_ROW}",  # noqa: S608
            (list(source_refs),)).fetchall() if row["connection_ref"] is not None}

    @classmethod
    def source_lines_in(cls, conn: Any, source_refs: set[str]) -> dict[str, SourceLine]:
        """ScenePoolReader (follow-up S3): each named Source revision's pool key (None: no
        readable query, or no pool yet), whether its pool has a complete check (C7: an unchecked
        pool still plays what it has) and whether it holds no photo. A ref naming no revision is
        left out. Read-only, in the caller's transaction."""
        keys = {row["source_ref"]: cls._query_key(row["query"], row["pool_key"]) for row in
                conn.execute("SELECT source_ref,query,pool_key FROM media_sources s WHERE "
                             f"source_ref=ANY(%s) AND {_REVISION_ROW}",  # noqa: S608
                             (list(source_refs),)).fetchall()}
        pools = {row["pool_key"]: row for row in conn.execute(
            "SELECT p.pool_key,p.last_complete_at IS NOT NULL AS ready,EXISTS(SELECT 1 FROM "
            "photo_pool_rows r WHERE r.pool_key=p.pool_key AND r.outcome='in_pool') AS held "
            "FROM photo_pools p WHERE p.pool_key=ANY(%s)",
            (list({key for key in keys.values() if key is not None}),)).fetchall()}
        answer = {}
        for ref, key in keys.items():
            pool = pools.get(key) if key is not None else None
            answer[ref] = SourceLine(
                source_ref=ref, pool_key=key if pool is not None else None,
                ready=pool is not None and pool["ready"],
                empty=pool is None or not pool["held"])
        return answer

    @staticmethod
    def ranked_line_in(conn: Any, q: LineQuery) -> RankedLine:
        """ScenePoolReader (follow-up S3, design rule 3): the top `q.limit` of a Scene's line in
        SQL, the in-pool rows of `q.pool_keys` (one per asset id) that `q.shape` admits by
        revision size, ranked by tier then the pass order: central/wall_plan/order.py's `ORDERS`
        and tiers, rendered here (`_line_order_sql`). Capture time and size are the rows' now.
        Also the wrap mark's row (`q.after`, admitted or not). Read-only, in the caller's
        transaction."""
        params: dict[str, Any] = {
            "pools": list(q.pool_keys), "after": q.after, "scene": q.scene_id,
            "pass": q.pass_number, "sep": SHUFFLE_SEPARATOR, "today": q.today.strftime("%m-%d"), "zone": q.house.zone,
            "picked": sorted(q.picked), "recent": sorted(q.recent), "limit": q.limit,
            "fresh": TIER_FRESH, "tier_recent": TIER_RECENT, "tier_picked": TIER_PICKED}
        mark = None
        if q.after is not None:
            mark = conn.execute(f"SELECT * FROM ({_LINE_ROWS}) AS m WHERE m.asset_id=%(after)s",
                                params).fetchone()
        params["mark_at"] = None if mark is None else mark["taken_at"]
        params["mark_id"] = None if mark is None else mark["asset_id"]
        shape = _shape_sql(q.shape, params)
        rows = conn.execute(
            f"SELECT * FROM ({_LINE_ROWS}) AS m WHERE {shape} "
            f"ORDER BY {_line_order_sql(q.order)} LIMIT %(limit)s", params).fetchall()
        ids = [row["asset_id"] for row in rows] + ([] if mark is None else [mark["asset_id"]])
        pool_keys = {row["asset_id"]: row["keys"] for row in conn.execute(
            "SELECT asset_id,array_agg(pool_key ORDER BY pool_key) AS keys FROM photo_pool_rows "
            "WHERE asset_id=ANY(%s) AND pool_key=ANY(%s) AND outcome='in_pool' GROUP BY asset_id",
            (ids, list(q.pool_keys))).fetchall()}
        return RankedLine(members=tuple(_lean(row, pool_keys) for row in rows),
                          after=None if mark is None else _lean(mark, pool_keys))

    def _ensure_pool_in(self, conn: Any, query: SourceQuery, *, by: Actor) -> PoolKey:
        """The query's pool, created (check requested) when no pool has its key yet."""
        self._known_connection(conn, query.connection_ref)
        key = query.pool_key()
        now = self.times.now_in(conn)
        created = conn.execute(
            "INSERT INTO photo_pools(pool_key,connection_ref,query,created_at,"
            "created_by,changed_at,changed_by) VALUES(%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(pool_key) DO NOTHING RETURNING pool_key",
            (key, query.connection_ref, Jsonb(query.model_dump(mode="json")), now, by,
             now, by)).fetchone()
        if created is not None:   # a new pool's first check, at once
            self._queue().enqueue_pool_check_in(conn, key, due_at=policy.next_check_due(None, now))
        return key

    def _request_check_in(self, conn: Any, pool_key: str, *, by: Actor) -> None:
        conn.execute("UPDATE photo_pools SET check_requested=TRUE,changed_at=%s,changed_by=%s "
                     "WHERE pool_key=%s AND NOT check_requested",
                     (self.times.now_in(conn), by, pool_key))
        self._queue().enqueue_pool_check_in(conn, pool_key, due_at=self.times.now_in(conn))

    def assign_pools_in(self, conn: Any) -> None:
        """Give each wanted Source revision with a query and no pool its pool (C12's pre-4 rows,
        and a dormant revision whose pool `retire_unwanted` forgot, wanted again): a pool created
        here queues its first check at once. Called where a revision becomes wanted
        (MediaRepository.reconcile_source_activity_in), in that transaction."""
        for row in conn.execute("SELECT source_ref FROM media_sources WHERE refresh_active AND "
                                "query IS NOT NULL AND pool_key IS NULL "
                                "ORDER BY source_ref").fetchall():
            self.pool_key_of_source_in(conn, row["source_ref"])

    def pool_key_of_source_in(self, conn: Any, source_ref: str, *,
                              by: Actor = "library-check") -> PoolKey | None:
        """A Source revision's pool key; for a pre-4 row (query set, pool_key NULL) it assigns
        and stores the key, creating the pool (check requested). None: no such revision, no
        readable query, or its connection is gone."""
        row = conn.execute("SELECT query,pool_key FROM media_sources s WHERE source_ref=%s "
                           f"AND {_REVISION_ROW} FOR UPDATE",  # noqa: S608
                           (source_ref,)).fetchone()
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
        key = self._ensure_pool_in(conn, query, by=by)
        conn.execute("UPDATE media_sources SET pool_key=%s,changed_at=%s,changed_by=%s "
                     "WHERE source_ref=%s", (key, self.times.now_in(conn), by, source_ref))
        return key

    # --- Drafts and Check now: operator side (C2) -----------------------------------------------

    def create_draft(self, query: SourceQuery, editing: SourceRef | None, *,
                     by: Actor) -> DraftRef:
        """A draft: a `media_sources` row of its own (module docstring), keyed by its ref."""
        self._queue()
        draft = str(uuid.uuid4())
        with self.db.transaction() as conn:
            editing_name = None
            if editing is not None:
                # The edited revision's name, while that name stands.
                row = conn.execute(
                    "SELECT v.name FROM media_sources v JOIN media_sources n ON n.name=v.name "
                    "AND n.head WHERE v.source_ref=%s AND NOT n.deleted",
                    (editing,)).fetchone()
                if row is None:
                    raise LibraryRefused("source_unknown", 404)
                editing_name = row["name"]
            key = self._ensure_pool_in(conn, query, by=by)
            now = self.times.now_in(conn)
            conn.execute("INSERT INTO media_sources(source_ref,refresh_active,pool_key,draft,"
                         "created_at,created_by,changed_at,changed_by) "
                         "VALUES(%s,FALSE,%s,%s,%s,%s,%s,%s)",
                         (draft, key, Jsonb({"editing_name": editing_name}), now, by, now, by))
            self._request_check_in(conn, key, by=by)
        return draft

    def set_draft_query(self, draft: DraftRef, query: SourceQuery, *, by: Actor) -> None:
        self._queue()
        with self.db.transaction() as conn:
            if self._draft_row_in(conn, draft, lock=True) is None:
                raise LibraryRefused("draft_unknown", 404)
            key = self._ensure_pool_in(conn, query, by=by)
            conn.execute("UPDATE media_sources SET pool_key=%s,changed_at=%s,changed_by=%s "
                         "WHERE source_ref=%s AND draft IS NOT NULL",
                         (key, self.times.now_in(conn), by, draft))
            self._request_check_in(conn, key, by=by)

    def check_source(self, source_ref: SourceRef, *, by: Actor) -> None:
        self._queue()
        with self.db.transaction() as conn:
            key = self.pool_key_of_source_in(conn, source_ref, by=by)
            if key is None:
                raise LibraryRefused("source_unknown", 404)
            self._request_check_in(conn, key, by=by)

    @staticmethod
    def _draft_row_in(conn: Any, draft: DraftRef, *, lock: bool) -> dict | None:
        """A draft's row (its pool and the name it edits), or None; `lock`: FOR UPDATE."""
        try:
            uuid.UUID(draft)
        except ValueError:
            return None
        return conn.execute(
            "SELECT pool_key,draft->>'editing_name' AS editing_name FROM media_sources "
            "WHERE source_ref=%s AND draft IS NOT NULL" + (" FOR UPDATE" if lock else ""),
            (draft,)).fetchone()

    def draft_for_save_in(self, conn: Any, draft: DraftRef) -> DraftForSave | None:
        """The draft (locked until the caller's transaction ends) with its pool's query."""
        row = self._draft_row_in(conn, draft, lock=True)
        if row is None:
            return None
        query = conn.execute("SELECT query FROM photo_pools WHERE pool_key=%s",
                             (row["pool_key"],)).fetchone()["query"]
        return DraftForSave(row["pool_key"], SourceQuery.model_validate(query),
                            row["editing_name"])

    def pool_of_draft_in(self, conn: Any, draft: DraftRef) -> PoolKey | None:
        found = self.draft_for_save_in(conn, draft)
        return None if found is None else found.pool_key

    @staticmethod
    def remove_draft_in(conn: Any, draft: DraftRef) -> None:
        conn.execute("DELETE FROM media_sources WHERE source_ref=%s AND draft IS NOT NULL",
                     (draft,))

    # --- Pool pages: operator side (C2) ---------------------------------------------------------

    def draft_pool(self, draft: DraftRef, after: PoolCursor | None) -> SourcePoolView:
        with self.db.transaction() as conn:
            row = self._draft_row_in(conn, draft, lock=False)
            if row is None:
                raise LibraryRefused("draft_unknown", 404)
            return self._pool_view_in(conn, row["pool_key"], after)

    def source_pool(self, source_ref: SourceRef, after: PoolCursor | None) -> SourcePoolView:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT query,pool_key FROM media_sources s WHERE source_ref=%s "
                               f"AND {_REVISION_ROW}", (source_ref,)).fetchone()  # noqa: S608
            if row is None:
                raise LibraryRefused("source_unknown", 404)
            return self._pool_view_in(conn, self._query_key(row["query"], row["pool_key"]), after)

    def _pool_view_in(self, conn: Any, pool_key: str | None,
                      after: PoolCursor | None) -> SourcePoolView:
        pool = None if pool_key is None else conn.execute(
            "SELECT * FROM photo_pools WHERE pool_key=%s", (pool_key,)).fetchone()
        # Kind by the Immich asset's parent row, through the pool's connection: a Waiting or
        # LeftOut row has no revision, and a LeftOut row with no parent counts with kind unknown.
        counts = [] if pool is None else conn.execute(
            "SELECT r.outcome,r.reason,l.kind,count(*) AS n FROM photo_pool_rows r "
            "LEFT JOIN library_assets l ON l.connection_ref=%s AND l.upstream_id=r.upstream_id "
            "WHERE r.pool_key=%s GROUP BY r.outcome,r.reason,l.kind",
            (pool["connection_ref"], pool_key)).fetchall()
        rows = [] if pool is None else conn.execute(
            f"SELECT {_MEMBER_COLUMNS} FROM photo_pool_rows r {_REVISION_JOIN} "
            "WHERE r.pool_key=%s AND r.outcome='in_pool' AND (%s::double precision IS NULL OR "
            "(l.taken_at,r.asset_id) < (%s::double precision,%s::text)) "
            "ORDER BY l.taken_at DESC,r.asset_id DESC LIMIT %s",
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

    def begin_check(self, pool_key: PoolKey) -> CheckLease | None:
        queue = self._queue()
        with self.db.transaction() as conn:
            row = conn.execute(
                f"SELECT p.*,{_WANTED} AS wanted,c.url,c.api_key,c.immich_user FROM photo_pools p "
                "JOIN library_connections c USING (connection_ref) WHERE p.pool_key=%s "
                "FOR UPDATE OF p", (pool_key,)).fetchone()
            if row is None:
                return None
            now = self.times.now_in(conn)
            if not row["wanted"] or row["api_key"] is None:
                # Not checked now, but its checks go on while the pool stands: wanted again
                # before `retire_unwanted` removes it, or its connection's key pasted (C12), it
                # is checked at its next due time.
                queue.enqueue_pool_check_in(conn, pool_key, due_at=policy.next_check_due(now, now))
                return None
            if row["check_state"] != "checking":
                row = conn.execute(
                    "UPDATE photo_pools SET check_number=check_number+1,check_state='checking',"
                    "check_started_at=%s,step_at=%s,progress=NULL,seen_so_far=0,problem=NULL,"
                    "incomplete_reason=NULL,check_requested=FALSE,changed_at=%s,changed_by=%s "
                    "WHERE pool_key=%s RETURNING check_number,check_started_at,progress",
                    (now, now, now, _CHECK_ACTOR, pool_key)).fetchone() | {
                        key: row[key] for key in ("connection_ref", "url", "api_key",
                                                  "immich_user", "query")}
                # An earlier check that never finished left its pages: none outlives its check.
                conn.execute("DELETE FROM photo_check_pages WHERE pool_key=%s", (pool_key,))
            else:
                conn.execute("UPDATE photo_pools SET step_at=%s WHERE pool_key=%s",
                             (now, pool_key))
            zone = house_time_in(conn).zone
        return CheckLease(pool_key=pool_key, query=SourceQuery.model_validate(row["query"]),
                          connection=self._secret(row), check_number=row["check_number"],
                          started_at=row["check_started_at"], zone=zone, progress=row["progress"])

    def known_revisions(self, connection_ref: ConnectionRef,
                        upstream_ids: Sequence[LibraryRef]) -> dict[str, KnownRevision]:
        if not upstream_ids:
            return {}
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT upstream_id::text AS upstream_id,revision_updated_at,revision "
                "FROM library_assets WHERE connection_ref=%s AND upstream_id=ANY(%s::uuid[]) "
                "AND revision_updated_at IS NOT NULL",
                (connection_ref, list(upstream_ids))).fetchall()
        return {row["upstream_id"]: KnownRevision(updated_at=row["revision_updated_at"],
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
            marks: dict[str, set[str]] = {}
            for mark in step.marks:
                marks.setdefault(mark.mark, set()).add(mark.upstream_id)
            for kind, ids in sorted(marks.items()):
                self._page_in(conn, lease, kind, ids, now)
            marked = self._marked_in(conn, lease, (self._upstream(e) for e in step.entries))
            entries = [e for e in step.entries if self._upstream(e) not in marked]
            if entries:
                self._page_in(conn, lease, "seen", {self._upstream(e) for e in entries}, now)
                with conn.cursor() as cursor:
                    self._record_assets_in(conn, cursor, lease.query.connection_ref, entries, now)
                    self._record_rows_in(conn, cursor, lease.pool_key, entries, now)
            conn.execute("UPDATE photo_pools SET progress=%s,seen_so_far=%s,"
                         "immich_up_to=COALESCE(%s,immich_up_to),changed_at=%s,changed_by=%s "
                         "WHERE pool_key=%s",
                         (Jsonb(step.progress), step.seen_so_far, step.immich_up_to, now,
                          _CHECK_ACTOR, lease.pool_key))
            queue.enqueue_pool_check_in(conn, lease.pool_key, due_at=now)
            return True

    @staticmethod
    def _upstream(entry: InPool | Waiting | LeftOut) -> str:
        return entry.asset.upstream_id if isinstance(entry, InPool) else entry.upstream_id

    @staticmethod
    def _page_in(conn: Any, lease: CheckLease, kind: str, upstream_ids: Iterable[str],
                 now: float) -> None:
        """One page of this check: the ids it saw (`seen`) or marked with one kind."""
        conn.execute("INSERT INTO photo_check_pages(pool_key,check_number,kind,upstream_ids,"
                     "recorded_at,recorded_by) VALUES(%s,%s,%s,%s::uuid[],%s,%s)",
                     (lease.pool_key, lease.check_number, kind, sorted(set(upstream_ids)), now,
                      _CHECK_ACTOR))

    @staticmethod
    def _marked_in(conn: Any, lease: CheckLease, upstream_ids: Iterable[str]) -> set[str]:
        """Ids carrying a mark of this check (stored by this step or an earlier one): the
        subtraction's one home."""
        ids = list(set(upstream_ids))
        if not ids:
            return set()
        return {row["upstream_id"] for row in conn.execute(
            f"SELECT m.upstream_id::text AS upstream_id FROM {_MARKED} AS m "  # noqa: S608
            "WHERE m.upstream_id=ANY(%(ids)s::uuid[])",
            {"key": lease.pool_key, "n": lease.check_number, "ids": ids}).fetchall()}

    @staticmethod
    def _record_assets_in(conn: Any, cursor: Any, connection_ref: str,
                          entries: Sequence[InPool | Waiting | LeftOut], now: float) -> None:
        """The facts a step read, in their one home, written only where they differ from what
        is stored (decision 0033): every entry naming a kind and taken time is its Immich
        asset's `library_assets` row (kind and taken time; the original's facts and the
        revision's, `asset_id`, width, height, duration and `updatedAt`, too when an InPool read
        them, None meaning NULLs, while a Waiting or LeftOut keeps them). A new revision clears
        the copies and key content measured on the old one (module docstring). The stored facts
        are read without a lock and only the rows that differ are upserted, in Immich-id order,
        so two pools' steps lock shared rows in one order (a deadlock between two pools whose
        steps saw the same two photos as InPool and Waiting the other way round otherwise).
        Entries naming one photo twice fold in order, the later winning."""
        assets: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in entries:
            if isinstance(entry, InPool):
                asset = entry.asset
                key = (asset.connection_ref, asset.upstream_id)
                assets[key] = {**assets.get(key, {}), "kind": asset.kind,
                               "taken_at": asset.taken_at, "read": True,
                               **dict(zip(_ORIGINAL_COLUMNS, _original_values(asset.original),
                                          strict=True)),
                               "asset_id": asset.asset_id, "revision": asset.revision,
                               "width": asset.width, "height": asset.height,
                               "duration": asset.duration,
                               "revision_updated_at": entry.updated_at}
            elif entry.kind is not None and entry.taken_at is not None:
                key = (connection_ref, entry.upstream_id)
                assets[key] = {"read": False, **assets.get(key, {}), "kind": entry.kind,
                               "taken_at": entry.taken_at}
        if not assets:
            return
        stored = {(row["connection_ref"], row["upstream_id"]): row for row in conn.execute(
            "SELECT connection_ref,upstream_id::text AS upstream_id,kind,taken_at,"
            f"{','.join((*_ORIGINAL_COLUMNS, *_REVISION_FACTS))} FROM library_assets WHERE "  # noqa: S608
            "(connection_ref,upstream_id) IN (SELECT * FROM unnest(%s::text[],%s::uuid[]))",
            ([ref for ref, _ in assets], [upstream for _, upstream in assets])).fetchall()}
        changed = [{column: None for column in (*_ORIGINAL_COLUMNS, *_REVISION_COLUMNS)}
                   | dict(facts, connection_ref=key[0], upstream_id=key[1], now=now,
                          by=_CHECK_ACTOR)
                   for key, facts in sorted(assets.items())
                   if _asset_differs(stored.get(key), facts)]
        if not changed:
            return
        # The step's value where it read one (`read`), else the stored one.
        kept = ",".join(f"{column}=CASE WHEN %(read)s THEN EXCLUDED.{column} "
                        f"ELSE library_assets.{column} END"
                        for column in (*_ORIGINAL_COLUMNS, *_REVISION_COLUMNS))
        # Measured on the revision: a new one starts with none.
        same = "library_assets.asset_id IS NOT DISTINCT FROM EXCLUDED.asset_id OR NOT %(read)s"
        cleared = ",".join([f"copies=CASE WHEN {same} THEN library_assets.copies "
                            "ELSE '{}'::jsonb END"] + [
            f"{column}=CASE WHEN {same} THEN library_assets.{column} END"
            for column in _KEY_COLUMNS])
        cursor.executemany(
            "INSERT INTO library_assets(connection_ref,upstream_id,kind,taken_at,"
            f"{','.join((*_ORIGINAL_COLUMNS, *_REVISION_COLUMNS))},created_at,changed_at,"
            "created_by,changed_by) VALUES(%(connection_ref)s,%(upstream_id)s,%(kind)s,"
            "%(taken_at)s,"
            + ",".join(f"%({column})s" for column in (*_ORIGINAL_COLUMNS, *_REVISION_COLUMNS))
            + ",%(now)s,%(now)s,%(by)s,%(by)s) ON CONFLICT(connection_ref,upstream_id) DO UPDATE "
            f"SET kind=EXCLUDED.kind,taken_at=EXCLUDED.taken_at,{cleared},{kept},"  # noqa: S608
            "changed_at=EXCLUDED.changed_at,changed_by=EXCLUDED.changed_by", changed)

    @classmethod
    def _record_rows_in(cls, conn: Any, cursor: Any, pool_key: str,
                        entries: Sequence[InPool | Waiting | LeftOut], now: float) -> None:
        """Each entry's pool row, written only when new or its outcome, reason or revision
        differs (decision 0033); read without a lock, upserted in Immich-id order."""
        rows = {row["upstream_id"]: row for row in map(cls._row, entries)}
        stored = {row["upstream_id"]: row for row in conn.execute(
            "SELECT upstream_id::text AS upstream_id,outcome,reason,asset_id FROM photo_pool_rows "
            "WHERE pool_key=%s AND upstream_id=ANY(%s::uuid[])",
            (pool_key, list(rows))).fetchall()}
        changed = [dict(row, pool_key=pool_key, now=now, by=_CHECK_ACTOR)
                   for upstream, row in sorted(rows.items())
                   if _differs(stored.get(upstream), row, _POOL_ROW_FACTS)]
        if changed:
            cursor.executemany(
                "INSERT INTO photo_pool_rows(pool_key,upstream_id,outcome,reason,asset_id,"
                "created_at,changed_at,created_by,changed_by) VALUES(%(pool_key)s,"
                "%(upstream_id)s,%(outcome)s,%(reason)s,%(asset_id)s,%(now)s,%(now)s,%(by)s,"
                "%(by)s) ON CONFLICT(pool_key,upstream_id) DO UPDATE SET "
                "outcome=EXCLUDED.outcome,reason=EXCLUDED.reason,asset_id=EXCLUDED.asset_id,"
                "changed_at=EXCLUDED.changed_at,changed_by=EXCLUDED.changed_by", changed)

    @staticmethod
    def _row(entry: InPool | Waiting | LeftOut) -> dict[str, Any]:
        """A pool row's facts: it copies no fact of its photo."""
        if isinstance(entry, InPool):
            return dict(upstream_id=entry.asset.upstream_id, outcome="in_pool", reason=None,
                        asset_id=entry.asset.asset_id)
        if isinstance(entry, Waiting):
            return dict(upstream_id=entry.upstream_id, outcome="waiting", reason=None,
                        asset_id=None)
        return dict(upstream_id=entry.upstream_id, outcome="left_out", reason=entry.reason,
                    asset_id=None)

    def unseen_rows(self, lease: CheckLease, limit: int) -> tuple[LibraryRef, ...]:
        with self.db.transaction() as conn:
            if not self._fenced(conn, lease):
                return ()
            rows = conn.execute(
                "SELECT r.upstream_id::text AS upstream_id FROM photo_pool_rows r "
                f"LEFT JOIN {_SEEN_OR_MARKED} AS s USING (upstream_id) "  # noqa: S608
                "WHERE r.pool_key=%(key)s AND s.upstream_id IS NULL "
                "ORDER BY r.upstream_id LIMIT %(limit)s",
                {"key": lease.pool_key, "n": lease.check_number, "limit": limit}).fetchall()
        return tuple(row["upstream_id"] for row in rows)

    def record_recheck(self, lease: CheckLease, ref: LibraryRef, matches: bool) -> bool:
        queue = self._queue()
        with self.db.transaction() as conn:
            if not self._fenced(conn, lease):
                return False
            if matches:
                self._page_in(conn, lease, "seen", (ref,), self.times.now_in(conn))
            else:
                conn.execute("DELETE FROM photo_pool_rows WHERE pool_key=%s AND upstream_id=%s",
                             (lease.pool_key, ref))
            queue.enqueue_pool_check_in(conn, lease.pool_key, due_at=self.times.now_in(conn))
            return True

    def finish_check(self, lease: CheckLease, finish: CheckFinish) -> bool:
        queue = self._queue()
        with self.db.transaction() as conn:
            if not self._fenced(conn, lease):
                return False
            now = self.times.now_in(conn)
            requested = conn.execute("SELECT check_requested FROM photo_pools WHERE pool_key=%s",
                                     (lease.pool_key,)).fetchone()["check_requested"]
            # The next check: at once when a Check now came while this one ran, else at its
            # due time.
            queue.enqueue_pool_check_in(conn, lease.pool_key, due_at=now if requested
                                        else policy.next_check_due(now, now))
            if isinstance(finish, CheckComplete):
                # The subtraction (design rule 2): a complete check removes what it marked (an id
                # marked after an earlier step of it added a row). A row it did not see stays:
                # only `record_recheck` removes one, after asking Immich (follow-up S4).
                conn.execute(
                    f"DELETE FROM photo_pool_rows r USING {_MARKED} AS m "  # noqa: S608
                    "WHERE r.pool_key=%(key)s AND r.upstream_id=m.upstream_id",
                    {"key": lease.pool_key, "n": lease.check_number})
                conn.execute("UPDATE photo_pools SET check_state='complete',check_finished_at=%s,"
                             f"last_complete_at=%s,progress=NULL,{_STAMP} WHERE pool_key=%s",
                             (now, now, now, _CHECK_ACTOR, lease.pool_key))
            elif isinstance(finish, CheckIncomplete):
                conn.execute("UPDATE photo_pools SET check_state='incomplete',incomplete_reason=%s,"
                             f"check_finished_at=%s,progress=NULL,{_STAMP} WHERE pool_key=%s",
                             (finish.reason, now, now, _CHECK_ACTOR, lease.pool_key))
            else:
                conn.execute("UPDATE photo_pools SET check_state='failed',problem=%s,"
                             f"check_finished_at=%s,progress=NULL,{_STAMP} WHERE pool_key=%s",
                             (finish.problem, now, now, _CHECK_ACTOR, lease.pool_key))
            # However it finishes, no page outlives its check.
            conn.execute("DELETE FROM photo_check_pages WHERE pool_key=%s", (lease.pool_key,))
            return True

    def check_step_failed(self, pool_key: PoolKey) -> None:
        """A step that raised until its retries ran out: the pool's next check is queued at its
        due time, as a completion queues it (the check resumes from its stored progress then).
        Nothing for a pool that is gone."""
        queue = self._queue()
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM photo_pools WHERE pool_key=%s",
                            (pool_key,)).fetchone() is None:
                return
            now = self.times.now_in(conn)
            queue.enqueue_pool_check_in(conn, pool_key, due_at=policy.next_check_due(now, now))

    def frame_outputs(self) -> tuple[FrameOutput, ...]:
        with self.db.transaction() as conn:
            return frame_outputs_in(conn)

    def copy_sizes(self, asset_id: str) -> dict[MeasurableCopy, CopySize]:
        with self.db.transaction() as conn:
            return _copy_sizes_in(conn, asset_id)

    def record_copies(self, asset_id: str, measured: Sequence[MeasuredCopy]) -> None:
        if not measured:
            return
        with self.db.transaction() as conn:
            now = self.times.now_in(conn)
            latest = {copy.name: copy for copy in measured}   # the later measurement wins
            # On the revision's row alone: a revision replaced meanwhile writes none.
            conn.execute("UPDATE library_assets SET copies=copies || %s WHERE asset_id=%s",
                         (Jsonb({name: {"media_type": copy.media_type, "width": copy.width,
                                        "height": copy.height, "measured_at": now}
                                 for name, copy in latest.items()}), asset_id))

    def photo_facts(self, asset_id: str) -> PhotoFacts:
        with self.db.transaction() as conn:
            row = conn.execute(
                # The copy the wall's newest made variant of this revision was made from
                # (its `MediaReady.made_from`; a variant 087 migrated names none).
                "SELECT c.full_size_copies,(SELECT v.produced_detail->>'made_from' FROM assets v "
                "WHERE v.kind='media-variant' AND v.identity LIKE a.asset_id || '.%%' "
                "AND v.produced_sha256 IS NOT NULL "
                "AND v.produced_detail->>'made_from' <> 'legacy' "
                "ORDER BY v.created_at DESC, v.identity LIMIT 1) AS made_from "
                "FROM library_assets a "
                "LEFT JOIN library_connections c USING (connection_ref) WHERE a.asset_id=%s "
                "AND EXISTS (SELECT 1 FROM photo_pool_rows r JOIN photo_pools p USING (pool_key) "
                f"WHERE r.asset_id=a.asset_id AND r.outcome='in_pool' AND {_WANTED})",
                (asset_id,)).fetchone()
            if row is None:
                raise LibraryRefused("photo_not_found", 404)
            asset = library_assets_in(conn, (asset_id,))[asset_id]
            measured = _copy_sizes_in(conn, asset_id)
            outputs = frame_outputs_in(conn) if asset.kind == "image" else ()
        original = asset.original
        kept = policy.copies_in_immich(asset.kind, asset.edited,
                                       None if original is None else original.media_type,
                                       row["full_size_copies"])
        return PhotoFacts(
            asset_id=asset_id, kind=asset.kind, taken_at=asset.taken_at, width=asset.width,
            height=asset.height, edited=asset.edited, original=original,
            copies=tuple(CopyView(name=name, in_immich=in_immich, measured=measured.get(name))
                         for name, in_immich in kept.items()),
            prepared_from=row["made_from"],
            frames=tuple(copy for frame in outputs
                         if (copy := _frame_copy(frame, measured.get("preview"))) is not None))

    def pool_thumbnail(self, asset_id: str) -> PoolThumbnail | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT p.connection_ref,r.upstream_id::text AS upstream_id,a.revision "
                "FROM photo_pool_rows r JOIN photo_pools p USING (pool_key) "
                f"JOIN library_assets a USING (asset_id) WHERE r.asset_id=%s "
                f"AND r.outcome='in_pool' AND {_WANTED} LIMIT 1", (asset_id,)).fetchone()
        if row is None:
            return None
        return PoolThumbnail(connection_ref=row["connection_ref"], upstream_id=row["upstream_id"],
                             revision=row["revision"])

    def retire_unwanted(self) -> frozenset[str]:
        with self.db.transaction() as conn:
            now = self.times.now_in(conn)
            conn.execute("DELETE FROM media_sources WHERE draft IS NOT NULL AND created_at<=%s",
                         (now - policy.DRAFT_KEEP_SECONDS,))
            unwanted = [row["pool_key"] for row in conn.execute(
                f"SELECT p.pool_key FROM photo_pools p WHERE NOT {_WANTED} FOR UPDATE").fetchall()]
            if unwanted:
                # A dormant revision forgets its pool; should Runtime need it again, its pool is
                # made anew from its query (pool_key_of_source_in).
                conn.execute("UPDATE media_sources SET pool_key=NULL,changed_at=%s,"
                             "changed_by=%s WHERE pool_key=ANY(%s)",
                             (now, _CHECK_ACTOR, unwanted))
                conn.execute("DELETE FROM photo_pools WHERE pool_key=ANY(%s)", (unwanted,))
            conn.execute(
                "DELETE FROM assets a WHERE a.kind='library-thumbnail' AND NOT EXISTS "
                "(SELECT 1 FROM photo_pool_rows r JOIN photo_pools p USING (pool_key) WHERE "
                f"r.asset_id=a.identity AND r.outcome='in_pool' AND {_WANTED})")
            rows = conn.execute("SELECT identity FROM assets WHERE kind='library-thumbnail'")
            return frozenset(row["identity"] for row in rows.fetchall())
