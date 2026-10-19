"""Seeding roadmap 4's photo library for the console's browser tests (lane console, K1-K4).

What the media worker would write, written through the store's own worker side
(`PgPhotoLibrary.begin_contact`/`record_contact`, `record_list`, `begin_check`/`record_step`/
`finish_check`), with no worker and no Immich. The store queues its follow-up work on a recording
queue here, so nothing runs it.
"""

import struct
import zlib
from uuid import UUID

from psycopg.types.json import Jsonb
from runtime_fakes import apply_procrastinate_schema
from support.frames import frame_create

from central.db import DatabaseTransactionClock
from central.infra.photo_library_store import PgPhotoLibrary
from central.media_queue import QueueReceipt
from central.photo_library.model import (
    CheckMark,
    ContactAnswer,
    ImmichUser,
    ImmichVersion,
    InPool,
    LibraryAlbum,
    LibraryAsset,
    LibraryCounts,
    LibraryPerson,
    SourceQuery,
    Waiting,
)
from central.photo_library.ports import CheckComplete, CheckStep
from central.photo_library.views import ConnectionWrite

KEY = "PUBLIC-FIXTURE-KEY-1234"
ANNA = ImmichUser(user_ref="5b0f3c0e-3d1c-4a8a-9f6e-2a8c1f0f7a11", name="Anna", email="anna@example.com")
COUNTS = LibraryCounts(photos=41203, videos=2110, albums=212, people=96)
TESTED = ImmichVersion(major=3, minor=1, patch=0)

__all__ = ["ANNA", "COUNTS", "KEY", "CheckMark", "TESTED", "Waiting"]


class NoQueue:
    """The store's queue in a browser test: what it would queue is recorded, never run."""

    def __init__(self):
        self.connections, self.pools = [], []

    def enqueue_connection_check_in(self, conn, connection_ref):
        self.connections.append(connection_ref)
        return QueueReceipt(coalesced=False)

    def enqueue_pool_check_in(self, conn, pool_key):
        self.pools.append(pool_key)
        return QueueReceipt(coalesced=False)


def library_of(registry) -> PgPhotoLibrary:
    """The store, as the worker sees it, over the test's database (Procrastinate's schema too,
    for the app's own queue)."""
    apply_procrastinate_schema(registry.db.dsn)
    return PgPhotoLibrary(registry.db, times=DatabaseTransactionClock(), queue=NoQueue())


def connected(library: PgPhotoLibrary, name="Family", answer: ContactAnswer | None = None) -> str:
    """A connection whose first check passed (Anna's key, Immich's counts)."""
    ref = library.add_connection(ConnectionWrite(name=name, url="http://immich.invalid:2283", key=KEY))
    library.record_contact(library.begin_contact(ref), answer or ContactAnswer(
        version=TESTED, user=ANNA, counts=COUNTS, full_size_copies=True))
    return ref


def person(number: int, name: str) -> LibraryPerson:
    return LibraryPerson(person_ref=str(UUID(int=0x1000 + number)), name=name)


def album(number: int, name: str, photos=10) -> LibraryAlbum:
    return LibraryAlbum(album_ref=str(UUID(int=0x2000 + number)), name=name, photos=photos)


def listed(library: PgPhotoLibrary, ref: str, *, people=(), albums=()) -> None:
    """The connection's lists as the worker records them (empty lists for the rest)."""
    library.record_list(ref, "people", list(people), None)
    library.record_list(ref, "albums", list(albums), None)
    library.record_list(ref, "places", [], None)
    library.record_list(ref, "tags", [], None)


def asset(ref: str, number: int, taken_at: float, kind="image") -> LibraryAsset:
    return LibraryAsset(connection_ref=ref, upstream_id=str(UUID(int=number)), revision=f"{number:040x}",
                        kind=kind, taken_at=taken_at, width=4000, height=3000,
                        duration=5.0 if kind == "video" else None)


def in_pool(ref: str, numbers, kind="image", newest=1_790_000_000.0):
    """Pool entries for `numbers`, one day apart, the lowest number newest."""
    return [InPool(asset=asset(ref, n, newest - n * 86400, kind)) for n in numbers]


# --- The varied library (follow-ups D2): every shape a Frame judges, drawn at its true ratio ------

# (what it is, width, height, where its subject sits as a share of each axis): 3:2, 4:3, 16:9,
# 9:16, 1:1, a 4:1 panorama, an 8 MP 4:3 photo (smaller than a 4K Frame) and a 4:3 photo whose
# face sits at its left edge.
VARIED = (
    ("3:2", 6000, 4000, (0.5, 0.5)),
    ("4:3", 4032, 3024, (0.5, 0.45)),
    ("16:9", 3840, 2160, (0.5, 0.5)),
    ("9:16", 2160, 3840, (0.5, 0.35)),
    ("1:1", 3000, 3000, (0.5, 0.5)),
    ("4:1 panorama", 12000, 3000, (0.5, 0.5)),
    ("8 MP 4:3", 3264, 2448, (0.5, 0.5)),
    ("4:3, face at the edge", 4032, 3024, (0.08, 0.4)),
)


def shaped(ref: str, number: int, width: int, height: int, taken_at: float, kind="image") -> LibraryAsset:
    """One Immich asset of `width` x `height` (upright pixels)."""
    return LibraryAsset(connection_ref=ref, upstream_id=str(UUID(int=number)), revision=f"{number:040x}",
                        kind=kind, taken_at=taken_at, width=width, height=height,
                        duration=5.0 if kind == "video" else None)


def varied_assets(ref: str, *, first=0x500, newest=1_790_000_000.0) -> list[LibraryAsset]:
    """The varied library's photos (VARIED), one hour apart, in VARIED's order (newest first)."""
    return [shaped(ref, first + index, width, height, newest - index * 3600)
            for index, (_name, width, height, _subject) in enumerate(VARIED)]


def seed_varied_library(store: PgPhotoLibrary, connection_ref: str, *, pool_key: str | None = None) -> tuple[str, ...]:
    """The varied library in one complete check of `pool_key` (by default a new unsaved Source
    of every photo of `connection_ref`), seeded through the store's worker side, no Immich.
    Returns the seeded asset ids, in VARIED's order."""
    if pool_key is None:
        draft = store.create_draft(SourceQuery(connection_ref=connection_ref), None)
        with store.db.transaction() as conn:
            pool_key = store.pool_of_draft_in(conn, draft)
    assets = varied_assets(connection_ref)
    filled(store, pool_key, [InPool(asset=a) for a in assets])
    return tuple(a.asset_id for a in assets)


def subject_grid(width: int, height: int, subject=(0.5, 0.5)):
    """An interest grid (16 columns, rows by the photo's shape) whose weight sits around
    `subject` (fractions of the width and height), the rest spread thinly: where the eye goes."""
    import math

    from central.wall_plan.key_content import INTEREST_COLUMNS, InterestGrid

    columns = INTEREST_COLUMNS
    rows = max(1, min(64, round(columns * height / width)))
    weights = []
    for row in range(rows):
        for column in range(columns):
            dx = (column + 0.5) / columns - subject[0]
            dy = (row + 0.5) / rows - subject[1]
            weights.append(0.02 + math.exp(-(dx * dx + dy * dy) / 0.01))
    total = sum(weights)
    return InterestGrid(columns=columns, rows=rows, weights=tuple(w / total for w in weights))


def measure_varied(registry, assets) -> None:
    """Key content for the varied library's photos (follow-ups C2), as the media worker writes it:
    each photo's interest around its VARIED subject; the "face at the edge" photo also has a face
    there (Immich's box, unpadded). `assets` in VARIED's order (varied_assets)."""
    from support.key_content_builders import face
    from support.measured_photos import measure

    for asset, (name, _width, _height, (x, y)) in zip(assets, VARIED, strict=False):
        faces = (face(max(0.0, x - 0.06), max(0.0, y - 0.1), min(1.0, x + 0.06), min(1.0, y + 0.1)),) \
            if "face" in name else ()
        measure(registry, asset, faces=faces, grid=subject_grid(asset.width, asset.height, (x, y)))


TALL_FRAME, WIDE_FRAME = "hall", "living-room"


def seed_varied_frames(registry) -> tuple[str, str]:
    """Two Frames beside the test's own, so a screenshot shows both shapes: "hall", 9:16 on a 32"
    display, and "living-room", 16:9 on a 55" one, each bound full screen to its own Pi's Output
    of its pixels (frame box run fb-8: a Frame with no known screen has no shape and no photo is
    judged for it). No Display is seen, so neither states a size."""
    from test_registry import enroll


    registry.create_frame(frame_create(TALL_FRAME, surface_id="hall", x_mm=100, y_mm=100))
    registry.create_frame(frame_create(WIDE_FRAME, surface_id="living", x_mm=100, y_mm=100))
    for frame_id, screen in ((TALL_FRAME, (1080, 1920)), (WIDE_FRAME, (3840, 2160))):
        player, _, _ = enroll(registry, count=1, size=screen)
        registry.bind(frame_id, player["player_id"], "HDMI-A-1", expected_generation=0)
    return TALL_FRAME, WIDE_FRAME


def thumbnail_png(width: int, height: int, subject=(0.5, 0.5), long_edge=120) -> bytes:
    """A thumbnail at a photo's true ratio: a gradient with a bright disc where its subject sits,
    so a ratio, a crop and an off-centre subject all read on a screenshot."""
    scale = long_edge / max(width, height)
    w, h = max(1, round(width * scale)), max(1, round(height * scale))
    cx, cy, r = subject[0] * w, subject[1] * h, min(w, h) / 4

    def pixel(x, y):
        if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
            return (238, 214, 160)
        return (40 + x * 150 // w, 70 + y * 120 // h, 150 - x * 60 // w)

    rows = b"".join(b"\x00" + bytes(c for x in range(w) for c in pixel(x, y)) for y in range(h))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


def route_thumbnails(page, assets) -> None:
    """Answer Central's thumbnail route in the browser with each asset's `thumbnail_png` (a
    VARIED subject where the asset is one), 4:3 for any other id."""
    subjects = {a.asset_id: VARIED[i][3] for i, a in enumerate(varied_assets(assets[0].connection_ref))} \
        if assets else {}
    sizes = {a.asset_id: (a.width, a.height) for a in assets}
    cache: dict[str, bytes] = {}

    def answer(route):
        asset_id = route.request.url.split("?", 1)[0].rsplit("/", 1)[-1]
        if asset_id not in cache:
            width, height = sizes.get(asset_id, (4000, 3000))
            cache[asset_id] = thumbnail_png(width, height, subjects.get(asset_id, (0.5, 0.5)))
        route.fulfill(status=200, content_type="image/png", body=cache[asset_id])

    page.route("**/v1/operator/library/thumbnails/**", answer)


def draft_pool_key(registry, draft_ref: str | None = None) -> str:
    """The pool of the newest draft (or of `draft_ref`)."""
    with registry.db.transaction() as conn:
        row = conn.execute(
            "SELECT pool_key FROM photo_drafts WHERE (%s::uuid IS NULL OR draft_ref=%s::uuid) "
            "ORDER BY created_at DESC LIMIT 1", (draft_ref, draft_ref)).fetchone()
    assert row is not None, "no draft yet"
    return row["pool_key"]


def drafts(registry) -> list[dict]:
    with registry.db.transaction() as conn:
        return conn.execute("SELECT d.draft_ref,d.pool_key,p.query FROM photo_drafts d "
                            "JOIN photo_pools p USING (pool_key) ORDER BY d.created_at").fetchall()


def begin(library: PgPhotoLibrary, pool_key: str):
    lease = library.begin_check(pool_key)
    assert lease is not None, "the pool is not wanted, or its connection has no key"
    return lease


def step(library: PgPhotoLibrary, lease, entries, *, seen: int, up_to: int | None = None) -> None:
    assert library.record_step(lease, CheckStep(progress={"seen": seen}, entries=tuple(entries),
                                                seen_so_far=seen, immich_up_to=up_to))


def finish(library: PgPhotoLibrary, lease, finish=None) -> None:
    assert library.finish_check(lease, finish or CheckComplete())


def filled(library: PgPhotoLibrary, pool_key: str, entries, finish_with=None) -> None:
    """One whole check: every entry in one step, then its finish (complete by default). The
    entries are all Immich holds, so a complete check's Re-check phase (follow-up S4) finds
    every earlier row it did not see gone."""
    lease = begin(library, pool_key)
    step(library, lease, entries, seen=len(entries), up_to=len(entries))
    if finish_with is None or isinstance(finish_with, CheckComplete):
        while unseen := library.unseen_rows(lease, 100):
            for ref in unseen:
                assert library.record_recheck(lease, ref, False)
    finish(library, lease, finish_with)


def saved_source(registry, library: PgPhotoLibrary, name: str, query: SourceQuery) -> str:
    """A saved Photo source (`name:1`) of `query` and its pool, wanted as Runtime's are."""
    draft = library.create_draft(query, None)
    with registry.db.transaction() as conn:
        pool_key = library.pool_of_draft_in(conn, draft)
        conn.execute("INSERT INTO media_sources(source_ref,query,pool_key,refresh_active) "
                     "VALUES(%s,%s,%s,TRUE)",
                     (f"{name}:1", Jsonb(query.model_dump(mode="json")), pool_key))
        conn.execute("INSERT INTO media_source_names(name,current_ref,revision) VALUES(%s,%s,1)",
                     (name, f"{name}:1"))
        conn.execute("INSERT INTO media_source_name_versions(source_ref,name,revision) VALUES(%s,%s,1)",
                     (f"{name}:1", name))
        library.remove_draft_in(conn, draft)
    return f"{name}:1"
