# Player cache module

Status: implementation slice; local persistence and fault behavior are tested, while
Player integration and physical qualification remain pending.

## Responsibility and boundary

`player/cache.py` owns the Player's bounded local byte store. It accepts an exact
`contracts.models.Variant` and a caller-supplied byte iterator, verifies the declared
length and SHA-256 digest, publishes the bytes atomically, and records readiness in a
SQLite index. It also owns explicit owner pins, release, LRU eviction of unpinned
entries, and restart repair. It has no source selection, HTTP, Immich, renderer, or
decoder policy.

The cache directory contains the SQLite database `cache.sqlite3`, canonical files named
`<sha256>.blob`, and transient files named `.partial-<random>.tmp`. A digest is already
validated by the shared `Variant` contract; the cache still checks it at every storage
boundary. Canonical names are derived only from that validated digest, so an indexed
filename can never redirect a lookup outside the cache directory.

## API and state

`Cache(directory: Path, max_bytes: int)` creates the directory and opens SQLite in WAL
mode. The public methods are:

* `secure(variant, chunks, pin) -> Path`: acquire and validate exact bytes, evicting
  unpinned least-recently-used files as needed, then atomically rename and index the
  file. The supplied owner is pinned in the same SQLite transaction as readiness.
* `path_for(variant) -> Path | None`: return a path only after rechecking the indexed
  size, file size, and SHA-256. An invalid entry is removed and treated as a miss.
* `pin(sha256, owner)` and `release(owner)`: add or remove owner pins explicitly;
  releasing an owner releases all of its entries. Expiry is never inferred by Cache.
* `stats() -> dict[str, int]`: report `bytes`, `entries`, `pinned_bytes`, `pinned_entries`,
  `temporary_bytes`, and `max_bytes`.
* `close()`: checkpoint/close the SQLite connection; the instance must not be reused.

Access is serialized by a re-entrant lock. This keeps concurrent background download
threads from exceeding the aggregate bound or racing publication, while allowing
internal repair and eviction calls. A temporary file is counted against the same
bound as indexed files; acquisition reserves the declared variant size before reading.
An invalid stream is discarded and does not publish readiness. If a stream is longer
than declared, shorter than declared, or hashes differently, `CacheError` is raised.

## Persistence and failure behavior

SQLite uses WAL and stores the digest, exact size, canonical filename, and access time
for every ready blob, plus an owner-to-digest pin table. A file is renamed into its
canonical name before its readiness row is committed. If the process dies between
those operations, startup removes the unindexed canonical orphan; an indexed row
whose file is missing or corrupt is removed and becomes a miss. Stale partial files
are removed on startup and before accounting. Database transactions protect a pin
created by `secure` from eviction before the caller receives the path.

Eviction considers only unpinned indexed entries and orders them by last access, then
digest for deterministic ties. If pinned bytes plus the requested declared size exceed
`max_bytes`, acquisition fails without evicting a pin. A valid pinned blob is retained
when an operator reopens the cache with a lower quota; `stats()` then exposes the
resulting pressure and new work is refused until pins are released. A variant larger
than the bound is rejected before reading its iterator. The aggregate on-disk cache is
bounded by `max_bytes` for new work, including a live temporary; failed filesystem
deletions fail closed and retain their index accounting rather than discounting bytes
that remain on disk.

## Acceptance checks

The focused tests cover valid acquisition and restart, corrupt/partial/overlong input,
corrupt indexed files, rename-before-index crash recovery, pin protection and release,
untrusted index filenames, two-file pressure, and concurrent acquisitions. They assert
observable paths, database-backed pins, and directory byte totals rather than mocking a
successful download.
