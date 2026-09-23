# Lane D — GitHub origin gateway + pod probe (`central.origins`, `central.health`)

Design: §3 ("Origin gateways are adapters"), §10.4 (`ReleaseOrigin`, `OriginUnavailable`/`OriginRejected`,
`PodProbe`), and §9 decision 5. Kernel: `P0-kernel.md`. This lane PORTS `central/github_releases.py`, which is
`httpx`-based and has an injectable transport. It keeps the discipline (identity encoding, bounded reads, exact
Content-Length, no partial files, bounded redirects) and changes only the shapes and the failure classification.
The legacy file stays until P2 deletes it.

## Owns (create)
- `central/origins/github.py`
- `central/health/probe.py`
- `tests/test_origins_github.py`, `tests/test_health_probe.py`

## Consumes
The kernel: `ReleaseOrigin`, `ReleaseListing`, `PublishedRelease`, `OriginLocator`, `OriginUnavailable`,
`OriginRejected`, `release_version`, and `require_sha256`. Also `httpx` and `contracts.release.MAX_ROOTFS_BYTES`.
It may NOT import `central.app_releases`, `central.app_packages` or `central.github_releases` (they reach `psycopg`).

## Must not touch
`pyproject.toml`, `tests/fakes/`, `central/infra/*`, `central/app.py`, `media/`, legacy `central/*.py`,
and migrations.

## Provides (frozen signatures)

```python
# central/origins/github.py
GITHUB_API_BASE: Final = "https://api.github.com"
MAX_DOWNLOAD_BYTES: Final = 1024**3
class GitHubReleaseOrigin:                 # implements kernel ReleaseOrigin
    def __init__(self, repo: str, *, token: str | None = None, include_prereleases: bool = False,
                 transport: httpx.AsyncBaseTransport | None = None, api_base: str = GITHUB_API_BASE,
                 timeout: timedelta = timedelta(seconds=30)) -> None: ...   # ValueError on a bad repo slug
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GitHubReleaseOrigin: ...
        # PHOTO_WALL_RELEASE_REPO (default "mcurcio/photo-wall"), PHOTO_WALL_RELEASE_TOKEN,
        # PHOTO_WALL_RELEASE_PRERELEASES truthy ∈ {"1","true","yes","on"} (the from_env rules at
        # app_release_service.py:95-127)
    async def list_releases(self, *, etag: str | None) -> ReleaseListing: ...
    async def download(self, locator: OriginLocator, into: Path, *, max_bytes: int) -> None: ...
```
Each call opens and closes its own `httpx.AsyncClient`, so there is no lifecycle to manage. One TLS handshake
per call is negligible at a 15-minute cadence.

```python
# central/health/probe.py
class PodProbe:                            # /livez and /readyz (decision 5): process + DB, never cache/origins
    def __init__(self, database_reachable: Callable[[], bool]) -> None: ...
    def live(self) -> bool: ...            # always True: the process answered
    def ready(self) -> bool: ...           # database_reachable(); any exception → False
```

## Behaviour (acceptance criteria)

**D1 Listing.** The parsing rules are those of `github_releases.py:190-356`:
- Drafts are skipped. Prereleases are skipped unless configured. Non-semver tags are skipped (via `release_version`).
- The manifest has schema 1. `player_deb` becomes `package`. `base_image` becomes `os_image`: the tarball
  locator with its url, sha256 and size.
- A release with no usable `.deb` gets `package=None` and `package_problem` set to one of
  `no_manifest`, `manifest_invalid`, `schema_mismatch` or `asset_missing`.
- A 304 on page 1 → `unchanged=True`.
- Pagination is capped as it is today.

**D2 Classification.** This is the only new logic. Each row gets a test with a fake transport.

| Condition | Raised |
| --- | --- |
| connect/read error, timeout, 5xx | `OriginUnavailable("origin_unreachable" \| "origin_error")` |
| 403/429 on list/manifest/download | `OriginUnavailable("rate_limited", retry_after=Retry-After)` |
| manifest asset 5xx/unreachable | `OriginUnavailable("manifest_unavailable")`: the whole listing aborts; there is never a partial list |
| download 404/410 | `OriginRejected("download_not_found")` |
| declared or streamed size > `max_bytes` | `OriginRejected("download_too_large")` |
| non-identity `Content-Encoding` | `OriginRejected("download_encoding")` |
| truncated stream / Content-Length mismatch | `OriginUnavailable("download_truncated")` |
| sha256 or size ≠ `locator` | `OriginUnavailable("download_corrupt")`: CDN corruption is retryable, and the retry tuple bounds it |
| list page not JSON / not a list | `OriginRejected("list_invalid")` |
| `into` already exists | `FileExistsError` (a caller bug; no classification) |
| local write error | `OSError` re-raised after removing `into` |

**D3 Download.**
- `into` is created `O_EXCL` with mode 0600, streamed, and fsynced.
- On every failure path `into` does not exist afterwards (mutation-probe: drop the unlink).
- `max_bytes` must be in `0 < max_bytes <= MAX_DOWNLOAD_BYTES`, else `ValueError`.
- `Authorization` never follows a cross-host redirect. That is httpx's behaviour; keep a test.

**D4 Probe.** `ready()` is False when the callable raises or returns False. `live()` needs no DB.

Port `tests/test_github_releases.py` wholesale into `tests/test_origins_github.py`, then add the D2 rows.

## Beads
| Bead | Scope |
| --- | --- |
| D-1 | `central/origins/github.py` + `tests/test_origins_github.py` |
| D-2 | `central/health/probe.py` + `tests/test_health_probe.py`. This is under an hour of work, so the lane runner may fold it into D-1 as one bead. |
