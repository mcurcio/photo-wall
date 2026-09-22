# Photo Wall — Owner-stated requirements

Scope: the requirements the owner stated directly in conversation about how the
system should work. This captures those statements only — not proposed design,
sizing, sequencing, or analysis.

**This list is not comprehensive.** It records the requirements surfaced in this
conversation and is expected to grow; absence of a requirement here does not mean
it does not exist.

## Storage & cache structure

1. The app assumes **one core cache directory** inside the container (e.g.
   `/var/cache/photo-wall`), with **one subdirectory per data domain** (e.g.
   `os-images`). It is a single cache mount, not one per domain. This refers only
   to **the cache directory**; it does not preclude other, non-cache volumes in
   the future. Today everything the app stores is cache, so there is just the one.
2. The data stored there are **cached binary assets** kept for faster serving —
   not primary storage.
3. The cache-root path has its **default defined by a `VOLUME` in the Dockerfile**.
   `PHOTO_WALL_CACHE_ROOT` is **not a required environment variable** — with none
   set, the container falls back to that baked default and works.
4. **Storage placement is Kubernetes' responsibility, not the app's.** Moving a
   subdirectory (e.g. `os-images`) onto a different storage medium is done in
   Kubernetes via overlays / subdirectory mounts. The app only ever assumes the
   one core cache directory.
5. The container image provides **UID and GID configuration overrides**.

## Cache semantics

6. **Cache posture:** any cached file may disappear at any time, and the
   functionality must still work.
7. **Miss handling:** when the database references a file that is not on disk, the
   operation fetches and reprocesses that file from its source before continuing.
   Operations that previously assumed "the filesystem matches the database" become
   **asynchronous** to handle this.
8. **Cache vs. dedicated storage:** if files can be cheaply evicted, it is a cache;
   if the files are sticky (must persist), it is not a cache but a dedicated
   storage volume.
9. **Every layer that stores cached files must be quota-aware and must have GC
   policies**, including removing orphans — files present on the filesystem but
   not in the database (which are otherwise inaccessible). The cache must not have
   free rein to pull down and re-serve everything.

## Observability

10. Cache operations — **eviction, garbage collection, orphan removal, and
    miss-driven fetch/reprocess** — must have **good logging and OTEL metrics**.

## Sources of truth & release syncing

11. **All Player `.deb` files come from GitHub; GitHub is the source of truth.**
12. `.deb` releases are **periodically scanned to keep the database's list of
    releases in sync with the real GitHub release list** — operating the same way
    the base OS images do.
13. The **GitHub release has distinct artifacts, each named precisely** for what
    it is, with names reflecting **hardware-specificity**. Decided terminology and
    asset names:
    - **Boot payload** — the kernel + initramfs + DTBs + `config.txt`/`cmdline.txt`
      served over TFTP; Raspberry-Pi-specific → `raspberry-pi-boot_<ver>.tar.gz`.
      (Note: this is *not* the "bootloader" — that term is reserved for the Pi's
      on-device firmware/EEPROM, `bootcode.bin`, which is not a release asset.)
    - **OS** — the squashfs root filesystem served by Central over HTTP;
      Raspberry-Pi-specific → `raspberry-pi-os_<ver>.squashfs`.
    - **Application** — the Player app `.deb`; portable (`arch: all`), so it gets a
      generic, non-hardware name → `player-app_<ver>.deb`.
    The `raspberry-pi-` prefix marks hardware-bound assets; the application, being
    portable, deliberately does not carry it.
14. **Release assets are grouped semantically, not out of packaging convenience.**
    Concretely: today the **boot payload** (served over **TFTP**) and the **OS**
    squashfs (served by **Central over HTTP**) are bundled in the same `.tar.gz`,
    but they are distinct things served by distinct mechanisms to distinct
    consumers — they **must be two separate release assets**.

## Netboot base serving

15. **Base (OS image) serving is on by default.**

## Delivery constraints

16. **No stopgaps and no migrations** — implement the correct solution directly.
