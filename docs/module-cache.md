# Player cache module

Status: implemented disposable cache; native-image and physical qualification remain pending.

## Responsibility and boundary

`player/cache.py` owns a bounded, process-local index of exact media bytes. The Player has no database, durable cache index, execution journal, or persistent pins. A configured directory may happen to survive a process restart or reboot, but its contents are only cache candidates. Central assignments and authority remain the source of truth.

The cache accepts an exact `contracts.models.Variant` and a caller-supplied byte iterator, verifies the declared length and SHA-256 digest, and publishes a content-addressed file atomically. It owns process-local pins, release, and least-recently-used eviction of unpinned entries. It has no source selection, HTTP, Immich, renderer, decoder, enrollment, or assignment policy.

`Cache(directory: Path | None, max_bytes: int)` uses a process-owned temporary directory when `directory` is `None`. An explicit directory allows verified byte reuse, without creating a persistence requirement. The only recognized reusable names are `<sha256>.blob`; temporary downloads use `.partial-<random>.tmp`. Startup removes partial files, scans candidate blobs, validates their names and complete contents, deletes corrupt candidates, rebuilds an in-memory index, and starts with no pins.

The public operations are:

- `secure(variant, chunks, pin) -> Path`: verify exact bytes, evict unpinned entries as needed, atomically replace the canonical file, and add a process-local owner pin.
- `path_for(variant) -> Path | None`: recheck exact size and SHA-256 before returning a path. Missing or corrupt content is removed and reported as a miss.
- `pin(sha256, owner)` and `release(owner)`: protect or release verified bytes for the current process only.
- `stats() -> dict[str, int]`: report bytes, entries, pinned bytes/entries, temporary bytes, and the configured maximum.
- `close()`: release resources; a temporary cache directory is removed with its owner.

## Capacity and failure behavior

Access is serialized by a re-entrant lock. At most one acquisition writes at a time, and a live temporary counts against the same bound as reusable blobs. A variant larger than the quota is rejected before reading. A short, overlong, malformed, or digest-mismatched stream never publishes readiness. Eviction considers only unpinned entries, ordered by last access and then digest for deterministic ties. If current process pins leave insufficient room, acquisition reports cache pressure.

Cache loss or corruption invalidates local readiness. It does not change a centrally secured assignment, re-enroll equipment, or authorize a replacement selection. The Player first validates any reusable candidate and otherwise requests the exact centrally authorized bytes. This gives the desired optimization when files survive while keeping deletion of the entire cache safe.

## Acceptance boundary

Focused tests cover empty startup, complete acquisition, corrupt/partial/overlong input, candidate discovery and validation after restart, corrupt surviving files, ephemeral pins, bounded eviction, concurrent acquisition, and a process-owned temporary directory. Player-service tests also verify that valid surviving bytes avoid a media request and that damaged bytes are reacquired. These checks establish the cache contract; the complete native image, reboot reuse, physical Pi storage behavior, and rendered output still require the acceptance runs in [validation](validation.md).
