# Central media module

Status: accepted for adapter/worker implementation, 2026-09-05; the Immich half was rebuilt by roadmap delivery [4, real photo sources](roadmap.md#4-real-photo-sources) (pull request [#81](https://github.com/mcurcio/photo-wall/pull/81), 2026-10-11). Delivery 4 replaced the private connections file, the one-snapshot-per-Source refresh, the unsaved-Source preview and the 1,000-match ceiling with Immich connections kept in Central's database ([decision 0020](decisions/0020-immich-key-in-central-database.md)), Photo source queries and the photo pool each query becomes. This document records the upstream contract and the preparation design; it is **not** physical qualification. It narrows D06/D07 and the media portion of D11/D12 without changing [requirements](requirements.md). [Decision 0001](decisions/0001-mvp-time-recovery-and-module-contracts.md), [the execution contract](execution-contract.md), and [the Planner contract](module-planner.md) own time, recovery, selection, and content-lock policy.

## Responsibility and decomposition

The central application owns Immich connections and their health, Photo source queries and their pools, planning, authorization, and all PostgreSQL schema/repository methods. One separately supervised Python media worker is the only process that talks to Immich and the only reader of a connection's key: it checks connections, reads each wanted query into its pool in short steps, fetches the planned photos, prepares them, and publishes immutable ready variants through the repository. The authenticated Photo Wall gateway streams those local variants. No conversion runs in API requests, the scheduler, or the Player. No upstream URL, ID, query, credential, redirect, or SDK enters Player configuration or protocol.

| Part | Owns | Inputs and results |
|---|---|---|
| `central/photo_library/` (model, policy, views, ports) | The photo library's vocabulary and rules, each with one home: `SourceQuery` and its pool key, per-photo outcomes, check states, connection health and its judgement (`policy.contact_health`), the key's permissions (`policy.REQUIRED_PERMISSIONS`), the check cadence, the draft lifetime, the page size. Pure: import-linter "The photo library domain is pure" forbids I/O, the store and the worker. | Values in, values out. |
| `central/infra/photo_library_store.py` (`PgPhotoLibrary`) | The tables of migration 080 (connections, lists, pools, pool rows, check marks, drafts) and the only SQL that reads a key | The operator side (connections, lists, drafts, pool pages) for Central's routes; the worker side (`LibraryWorkerStore`: contacts, lists, check steps, thumbnails, retirement) for the media worker; `pool_members_in` for the Planner's read. |
| `central/photo_library_routes.py`, `central/library_routes.py` | The operator routes for connections, lists, drafts, pools and checks ([routes](#operator-routes)) | JSON in and out; the key goes in and never comes back. |
| `media/library_client.py` | What the worker needs from Immich (`LibraryClient`) | Protocol only; the checks and preparation depend on it, tests drive them with a fake. |
| `media/immich.py` (`ImmichClient`) | Everything Immich-shaped: request bodies, answer parsing, versions, redirects | A `ConnectionSecret` → contacts, lists, keyset pages, exclusion pages, edits fingerprints, thumbnails, fetched renditions. No SQL, Frame policy, or Player API. |
| `media/pool_check.py` | The pure check fold: phases, keyset advance, per-photo entries, stack marks, relative dates resolved in UTC | No I/O, no clock reads. |
| `media/library_checks.py` (`LibraryChecks`) | Connection checks and pool checks, one bounded step per task | Store + client factory + clock. |
| `media/key_content.py` | Measuring a photo's key content (5a): Immich's face boxes as padded fractions, and an interest grid from the small thumbnail | Face answers + thumbnail bytes → `KeyContent`, or `MeasureError`. Judges no Frame: [the photo fit rule](module-planner.md#the-photo-fit-rule) does. |
| `central/infra/key_content_store.py` (`PgKeyContent`) | Migration 082's `photo_key_content`: one row per photo, measured or failed, at an analysis version; the only reader and writer | Hydrates `Candidate.analysis` for the Planner; queues measurements the Planner asks for ([media worker](module-media-worker.md#key-content)). |
| `media/library_clients.py` (`LibraryClients`) | One open client per connection, reused while its address and key are unchanged | Used by the checks, preparation and the thumbnail origin. |
| `media/prepare.py` | Probe, rendition validation, conversion, and recipe identity | Local fetched file + expected metadata + recipe + resource limits → exact `Variant`, coded failure. No upstream transport or assignment authority. |
| `media/worker.py` | Queued task execution, repository leases, and cleanup orchestration | Repository and store interfaces + adapters + explicit clock → check, preparation and maintenance results. Procrastinate owns dispatch and retry timing. |
| Integration fixture | Disposable real Immich and synthetic media ([fixture module](module-immich-fixture.md)) | Its own owner, partner and runtime key; it never touches a user's Immich. |

`Candidate` and `SourcePool` are defined only in [central/catalog.py](../central/catalog.py); `Variant`, `Plan`, and `Layer` stay in [contracts/models.py](../contracts/models.py). A pool photo's `Candidate.original_width/height` are its upright size **as Immich shows it** (edits applied): the largest copy Photo Wall can fetch. The Planner remains the sole owner of Frame compatibility and ranking ([the photo fit rule](module-planner.md#the-photo-fit-rule)). The adapter and the worker measure a photo's key content but never judge a Frame.

## Connections and health

A **connection** is one Immich server and one key, added in Settings › Photo library ([runbook](runbook.md#connecting-immich)). Its row holds a name, the URL in normal form (`policy.normalise_url`: `http` or `https`, a host, no user, query or fragment; `/api` appended when missing, so the owner may paste `http://immich.local:2283`), the key, the Immich user it belongs to, its health and Immich's counts. The key is written through the API and never read back: answers carry only its last four characters ("…1234"). `PgPhotoLibrary` selects it in exactly three methods (`begin_contact`, `connection_secret`, `begin_check`), all on the worker side; no route answer, job argument, log line or console file carries it. Why the key lives in the database, and what that costs, is [decision 0020](decisions/0020-immich-key-in-central-database.md). Plain `http` is accepted; a private certificate authority must be in the worker's system trust store (the connections file's `ca_file` option is gone).

A **contact** is one connection check. The store hands out a contact number when it starts, and a result applies only if its number is newer than the last applied one, so the latest-started contact wins and clocks never order results. The worker contacts a connection every 15 minutes (`policy.CHECK_EVERY_SECONDS`, setting #12, fixed), on Save and on Check now. It reads:

| Request | Permission | Gives |
|---|---|---|
| `GET server/version` | none | the version |
| `GET users/me` | `user.read` | the Immich user |
| `GET api-keys/me` | none | the key's permissions; missing = `REQUIRED_PERMISSIONS` minus these |
| `GET assets/statistics` | `asset.statistics` | photos (`images`), videos (`videos`) |
| `GET albums/statistics` | `album.statistics` | albums (`owned`: the user's own, the ones a Source can read; `shared` counts albums shared with them, and their own shared ones a second time) |
| `GET people?size=1` | `person.read` | people (`total − hidden`) |
| `GET server/features` | none | whether Immich's face detection is on (`facialRecognition`) |
| `GET assets/{id}/thumbnail?size=fullsize` of one non-JPEG photo, not followed | `asset.view` | whether Immich keeps full-size copies: a redirect to the preview means off |

`policy.contact_health` judges the answer. **Health** is `not_checked` (a new or cut-over row), `ok` with notes, or `needs_a_look` with one problem and the time it began (`since` carries while the problem's kind stays the same). The precedence is: a refused contact's problem; then `other_user`; then `immich_too_old`; then `missing_permissions`.

| Problem | Meaning | The fix the console offers |
|---|---|---|
| `key_rejected` | Immich answered 401 | Paste a new key |
| `missing_permissions` | the key lacks some of `REQUIRED_PERMISSIONS` (listed) | Paste a new key (the card lists the permissions to give it) |
| `immich_too_old` | older than 2.5.0 | Update Immich, then Check again |
| `unreachable` | no connection or a timeout | Check again |
| `unexpected_answer` | an answer that did not parse (its code is the detail) | Check again; Details |
| `other_user` | the key belongs to another Immich user than the connection's | Paste a key for that user |

| Note | Meaning |
|---|---|
| `untested_version` | newer than 2.5.x, the tested release; it keeps working |
| `no_full_size_copies` | Immich keeps no full-size copies, so HEIC and RAW photos play at preview size |
| `no_face_detection` | Immich's face detection is off, so crops ignore faces (5a; the console says "Immich's face detection is off, so crops ignore faces.") |

A connection belongs to the Immich user of its first passing contact (an Immich older than 2.5 does not bind it); a later key of another user is `other_user`. **While a connection needs a look, the last pool of every Source on it keeps playing** (C7): a rejected key or an outage never empties the wall, and removing the connection is the only way to stop it. Counts are those of the last passing contact, and they are Immich's own totals (`GET assets/statistics` with no filter), which include archived photos. A pool never includes archived photos, so a Source of "everything" can show fewer photos than the connection's count (on the fixture: 25 counted, 24 on the timeline).

The key needs exactly these permissions (one home: `policy.REQUIRED_PERMISSIONS`) and no upload, edit, delete, share or administration permission. What each is for, and what happens without it, is the runbook's [library key](runbook.md#the-library-key).

`user.read`, `asset.read`, `asset.view`, `asset.download`, `asset.statistics`, `album.read`, `album.statistics`, `person.read`, `tag.read`, `face.read` (the face boxes smart crop keeps whole, 5a), `asset.edit.get`.

**Versions.** Immich 2.5.x is tested (the CI fixture runs v2.5.6). An older Immich is Needs a look ("Update Immich"); a newer one keeps working with the `untested_version` note. A changed field in a newer Immich reads as `unexpected_answer` with the field's code, never as an empty pool. The offline answers in `tests/test_immich_library.py` are the record of what 2.5.6 sends.

**Lists.** After each passing contact the worker lists the connection's albums, people, places and tags and stores them one row per item, with no cap (owner, 2026-10-09: "i dont want any limit on the count of a source"). A failed listing keeps the last list and records the failure beside it. A place is a city, state and country together (Immich's reverse geocoding). Albums are the user's own only: Immich 2.5.6's search returns only the user's and their partners' photos, so an album another user shares with them would give a Source none of its owner's photos (probed against the fixture, 2026-10-11). Central's list routes read only stored lists: up to 20 matches per answer, prefix matches before contains matches, names stripped of control, bidi and zero-width characters; a by-id read names an item `absent` only from a list that was read whole.

**The cut-over (C12).** Migration 080 gave each connection name the old file named an empty row (no URL, no key, `not_checked`), so existing Sources keep pointing at it. Until the owner pastes that Immich's URL and key once, nothing on it is checked, no new photos play, and Frames keep their last photo.

## Photo source queries

A Photo source is one Immich query plus exclusion queries (`SourceQuery`; settings #13–#33):

| Field | Setting | Meaning |
|---|---|---|
| `connection_ref` | #14 | which Immich |
| `albums` | #15 | in every chosen album |
| `people` | #16, #17 | every chosen person appears |
| `place` | #18 | city, state and country, matched together |
| `taken` | #19, #20 | between two instants, or the last N years and months (resolved in UTC at each check) |
| `favourites_only` | #22 | Immich favourites only |
| `tags` | #23 | every chosen tag, nested tags included |
| `media_types` | #25 | photos, live photos, videos (all three by default) |
| `partners` | #32 | partners' photos too (off) |
| `exclude.albums`, `.people`, `.tags` | #26–#28 | subtracted |
| `exclude.screenshots` | #31 | subtracted (on) |

Several albums, people or tags mean "all of them", Immich's own meaning; "any of them" is a second Source on the Scene (#36). Archived, hidden and locked photos are never included: one fixed line, not a switch (#29, #30). On this day (#21) is delivery 5a's Order; a minimum rating (#24) and skipping blurry photos and duplicates (#33) are deferred.

**Immich answers every match question; Photo Wall only subtracts.** The include query is one Immich search. Immich's search has no "not", so each exclusion is one more Immich query, read whole on every check, whose answers Photo Wall subtracts. A screenshot is a file whose name contains "screenshot" (any case), or a PNG with no camera model (Immich's search cannot ask for a missing maker, so the code asks for a missing `model`). This reading of "one Immich query per Photo source" is a choice of 2026-10-11 awaiting the owner's confirmation ([requirements](requirements.md#live-media-compatibility-and-preparation)). Photo Wall never computes a filter itself, with one exception recorded as a finding: Immich's search has no "is a live photo" filter, so a query that chooses photos but not live photos (or the reverse) keeps or drops each still by whether Immich's answer names a live-photo video.

**One pool per query.** A query's canonical form is its JSON with sorted keys and sorted lists; a relative date range stays relative, so "last 3 years" is one pool on every day. Its sha256 is the **pool key**. Equal queries share one pool: a draft and the Source saved from it, two Sources with the same query, and an edit put back. Editing a draft's query points it at another pool, which the console shows as starting again.

**Drafts and names.** A **draft** is a Source not yet saved: it has a real query, its check runs, its photos appear as they are found, and Save works at any time. Saving makes the next revision of the Source's name (`name:N+1`, or `name:1` for a new name), and Scenes using the old revision move to the new one in the same transaction, as any Source edit does; the pool carries over because the query, and so the pool key, is the same. An unsaved draft is removed a day after it was made (`policy.DRAFT_KEEP_SECONDS`).

## Live discovery, pagination, and metadata

A **check** is one complete read of a pool's queries from Immich, in short steps. Each step is one task doing one bounded request (`CHECK_POOL_TASK`, below), so no task holds a long upstream call and a big library is read page by page. A pool is checked only while it is **wanted**: a live draft points at it, or an active Source revision has its key. It is due 15 minutes after its last check finished, at once when Save, Check now or a new draft asks, and never while a check runs.

A check runs in phases:

1. **Up to.** `POST search/statistics` with the include filters gives "up to N": an upper bound shown at once, since Immich's statistics count partners' photos, stacked copies and exclusions. The same step reads which timeline months hold stacked copies: `GET timeline/buckets` with stacks shown and with stacks collapsed; a month whose two counts differ holds a stack.
2. **Exclusions.** Each exclusion query (an album, a person, a tag, screenshots by name, screenshots as camera-less PNGs) is read whole, with no visibility filter, and each id it returns is **marked** excluded for this check. Exclusions come first, so no photo joins the pool and then leaves it.
3. **Include.** `POST search/metadata` with the query's filters, `visibility: timeline` (archived, hidden and locked never answer), `withStacked: true`, `order: desc` and size 1,000 (`LibraryLimits.page_size`), read by **keyset pages**: each page continues from the oldest capture time seen, inclusive (`takenBefore`), and drops the ids already seen at that instant. The adapter never asks for `page` 2: Immich pages by position, ordered only by capture time, and offset pages lose photos at equal times (the [fixture](module-immich-fixture.md#executed-evidence) proved it). A full page at one instant switches to upload time within that instant (`createdBefore`); more than 1,000 photos at one capture and one upload instant ends the check **incomplete**. Immich 2.5.6's search answer never says which photos are stacked copies (`withStacked` changes nothing), so for each page the step also reads the timeline months around any photo near a stacked month, and marks the photos the collapsed timeline does not show.
4. **Done.**

Each search row becomes one **photo outcome**:

| Row | Outcome |
|---|---|
| a stacked copy: taken within a month of a month that holds a stack, and not shown by Immich's timeline with stacks collapsed (`GET timeline/bucket?withStacked=true` for the months around it) | marked `stacked_copy`; a stack joins only through its primary, and only if the primary itself matches (C9) |
| a partner's photo, unless `partners` | skipped |
| a still or live photo the query did not choose | skipped |
| marked in this check | not added |
| did not parse | left out, `unreadable_answer` |
| over 64 Mpx, 16,384 px on a side, or 300 s of video | left out, `over_item_limits` |
| no size yet | waiting (Immich has not read it) |
| otherwise | in the pool |

A bad answer affects only its photo. An in-pool photo is a `LibraryAsset`: connection, Immich id, revision, kind, capture time, upright size as Immich shows it (Immich's own `width` and `height`, already upright and with edits applied, so Photo Wall reads no orientation), and a video's duration. Its **revision** is Immich's checksum, or the checksum plus the first 16 hex of the sha256 of its Immich edits (`GET assets/{id}/edits`) when it is edited; a revision already known is reused while Immich's `updatedAt` is unchanged. Photo Wall's `asset_id` is `asset_identity(connection, Immich id, revision)`, so an unedited photo keeps the id it had before delivery 4, and an edit makes a new asset.

**A check adds what it confirms and removes only when it completes.** Each step upserts its rows with the check's number in one transaction and queues the next step in the same transaction. A finished check:

| Finish | Pool |
|---|---|
| complete | rows not seen by this check, and older checks' marks, are swept; zero rows here is truly empty |
| incomplete (more than 1,000 photos at one instant) | nothing removed |
| failed: `connection` (see the connection's health) | nothing removed |
| failed: `chosen_item_gone` (an album, person or tag deleted in Immich: Immich answers 400 for its id) | nothing removed |

A failed, incomplete or interrupted check therefore removes nothing, and a newer check fences an older one's late steps by number. Every check re-reads every match (about 100 requests per 100,000 photos, plus exclusions, every 15 minutes). That is sound but not cheap; Immich's sync stream, which reports only changes, was rejected because Photo Wall would then compute membership from the whole library and would write acknowledgements to Immich.

There is no membership ceiling: the 1,000-match refusal, the per-Source asset cap and the Planner's candidate caps are gone. Budgets bound one request or one step (`LibraryLimits`), never a count of matches.

## Operator routes

Every route takes the operator token. Refusals answer `{"error": code}`. Query strings under `/v1/operator/library/` stay out of the access log.

| Route | Does |
|---|---|
| `GET /v1/operator/library/connections` | each connection: name, URL, key hint, Immich user, health, counts, the required permissions |
| `POST` (`{name, url, key}`), `PUT …/{ref}` (`key` optional: keeps the old one), `DELETE …/{ref}` | add, change, remove (refused `sources_use_connection` while a Source or draft uses it); add and change queue a contact |
| `POST …/connections/{ref}/checks` | Check now (a queued duplicate coalesces) |
| `GET /v1/operator/library/{albums,people,places,tags}?connection=&q=&limit=` (or `&ids=`) | the stored lists |
| `POST /v1/operator/photo-drafts` (`{query, editing?}`), `PUT …/{draft}/query`, `POST …/{draft}/save` (`{name}`) | drafts and Save |
| `GET /v1/operator/photo-drafts/{draft}/pool?after=`, `GET /v1/operator/sources/{ref}/pool?after=` | 60 in-pool photos per page, newest first, after a keyset cursor (capture time, asset id) that stays valid while the pool changes; counts by outcome, "up to N" and the check state |
| `POST /v1/operator/sources/{ref}/checks` | Check now for a saved Source |
| `GET /v1/operator/library/thumbnails/{asset_id}` | a pool photo's thumbnail ([below](#lists-pools-and-thumbnails)) |

`GET /v1/operator/media` lists each Source with its `query` and its `check`.

<a id="tags-previews-and-thumbnails"></a>

## Lists, pools and thumbnails

These serve Settings › Photo library and the Photo source editor. Each data type keeps one path: lists and pools are database rows the worker writes; files (thumbnails) go through the one [asset layer](module-central-cache.md). Players have no route or job kind for any of them, and the browser never receives the library's URL, key, owner, or any photo's Immich id or checksum.

- **Thumbnails.** Asset kind `library-thumbnail` in the cache root's `previews/` subdirectory, fetched by `FetchLibraryThumbnail` jobs on the kernel FETCH queue and read through `AssetReader`. **Servable** means an in-pool row of a wanted pool (`pool_thumbnail`). Central's `FetchLibraryThumbnailHandler` owns production; only the library half, the `ThumbnailOrigin` port, is implemented in `media/library_thumbnails.py` and injected into the job runtime, and an import-linter contract forbids Central from importing a library client. The worker asks `GET assets/{id}/thumbnail?size=thumbnail&edited=true`, so a thumbnail shows Immich's edits, and re-encodes it without metadata; a revision that changed since the pool row was written is terminal for that thumbnail. `GET /v1/operator/library/thumbnails/{asset_id}` checks the operator session first, then refuses a sent `Sec-Fetch-Site` other than `same-origin` (403), then a non-servable id (404, nothing queued); it waits at most 2 s in four dedicated waiter slots and otherwise answers 503 with `Retry-After`; CORP is on every answer. Maintenance deletes the thumbnail records outside wanted pools.

Every time on these records follows the worker's [one media clock](module-media-worker.md#one-media-clock).

## Open gaps

The two [requirements](requirements.md#live-media-compatibility-and-preparation) this section used to list as not built (no membership ceiling; a paged view before and after saving) are met by delivery 4. What it leaves open:

| Gap | Cost |
|---|---|
| More than 1,000 photos at one capture and one upload instant | The check ends incomplete and removes nothing; the pool keeps what it found |
| One photo reached through two connections counts twice | Two Immich servers holding the same photo give two assets |
| The hand-placed photo picker reads a whole pool in one answer | A huge pool answers one large list (the 1,000 refusal there is gone; paging it is not in 4) |
| Only Immich 2.5.x is tested; no 3.x fixture | A newer Immich works with a note until a field changes, then reads as `unexpected_answer` |
| Live photos are kept or dropped per photo by Photo Wall | Immich has no live-photo filter; the alternative is one "Photos and live photos" choice |
| Settings #12 (check every 15 minutes), #24, #33 | Fixed cadence; minimum rating and blurry/duplicates are deferred (#21, On this day, is a Scene order since 5a) |

## Operational bounds and failure classes

Budgets bound work per request or per step; none counts matches.

| Budget | Value and behavior |
|---|---|
| Check step | One Immich request (a page, an exclusion page or "up to"); at most 60 s per step; a search page holds at most 1,000 rows |
| JSON transfer | 16 MiB per answer, read in bounded chunks |
| HTTP | 3 s connect, 10 s per read inactivity, 15 s metadata request wall time; a fetch 120 s total and 256 MiB |
| Acquisition | At most 256 queued distinct original/recipe jobs globally; one active conversion initially; reserve staging before opening a stream. Reject advertised oversize before reading and enforce actual received bytes even without `Content-Length`. |
| Preparation | At most 64 million pixels and 16,384 px on a side; video duration at most 300 s; one video stream; conversion wall time 180 s and process memory/CPU limits. |
| Storage | 4 GiB aggregate managed central media bytes, including active reservations, fetched files, partials, prepared blobs, and cleanup-pending files; per-job reserve 256 MiB fetched + 256 MiB derivative. JPEG output maximum 16 MiB; MP4 maximum 256 MiB. Reject work when pinned bytes/reservations cannot fit. |
| Delivery | Full-file requests only in v1; maximum active transfer count 8 globally. Each descriptor/response has a hard 120 s lifetime and a 150 s database reference for cleanup margin. Player enforces its independent cache/download bounds. |

Use `httpx` with `trust_env=False`, the connection's normalised base URL, explicit TLS verification, no source-supplied URL fragments, and `x-api-key` only toward that base. Metadata requests follow no redirect. A rendition fetch follows at most three redirects, and only to the same origin (scheme, host and port); a redirect elsewhere is `upstream_redirect`. Never forward upstream headers or bodies to a Player. A response `Content-Type` is a hint, not proof of safe media or matching bytes.

A connection-wide failure (401, unreachable, a malformed answer) becomes the connection's health and fails the running check with `connection`; a 403 on one statistics read means that permission is missing. A per-photo problem is that photo's outcome. Preparation failures keep their codes (`asset_missing`, `asset_permission`, `asset_changed`, `asset_integrity`, conversion and resource codes). Do not persist raw exception strings, URLs with credentials, request bodies, or upstream error bodies.

Retry transport failures with bounded backoff, initially 5/15/60 s; attempts are separately leased and never run concurrently for one job. Unsupported media and integrity failures require a new revision or an explicit retry after their recorded cooldown. Return failures to central coordination for Planner and Runtime ownership. During that cooldown, exclude the failed asset revision from **unsecured** dynamic/authored candidate inputs and report its code. Locked Layers are retained independently. No media worker retry authorizes late execution or rerolls a secured assignment.

## Original validation and preparation recipe

Preparation fetches **the photo as Immich shows it**, edits included (owner, 2026-10-09: "whichever one shows up in immich"), and only for planned photos within the preparation horizon. Checking 100,000 photos never means downloading 100,000 files.

- **Photos.** Fetch Immich's edited preview (`thumbnail?size=preview&edited=true`) and read its size from its header. When it covers every Frame as the Player shows it, keep it (owner, 2026-10-09: use Immich's downsampled copy only if it is at least the visible Frame size): from 5a the Player draws a window of the Frame's shape cut from the photo, so the window cut from the preview must hold each Frame's pixels (`media/library_client.py` `preview_covers`, over `frame_sizes()`; a 1152 x 1440 preview shows a 1280 x 800 Frame only a 1152 x 720 window, so the full-size copy is taken). Otherwise fetch Immich's edited full-size copy (`size=fullsize&edited=true`): the original for JPEG and PNG, Immich's conversion for HEIC and RAW. When Immich keeps no full-size copies the request redirects to the preview, and Photo Wall keeps the preview. The connection's `no_full_size_copies` note says HEIC and RAW then play at preview size; in fact, on the fixture's Immich 2.5.6, an unedited JPEG redirects to its preview too, so on a Frame larger than the preview any photo plays at preview size. That case is an open finding awaiting a decision (fetch the original of an unedited photo, or require full-size copies); its test is a strict expected failure (`tests/test_preparation_rendition.py`).
- **Videos.** Fetch the original (`assets/{id}/original`); Immich 2.5 does not edit videos.

The recipe (`PreparationRecipe.version` 2, so every photo prepared before delivery 4 is prepared once more) accepts JPEG, PNG and WEBP stills and ordinary SDR MP4/MOV H.264/HEVC video. Animated images, HDR/HLG/PQ, non-sRGB/wide-gamut still profiles, unusual rotation matrices, and unsupported containers are explicit preparation failures. Untagged ordinary 8-bit JPEG is interpreted as sRGB; reject a conflicting embedded profile.

Each fetch goes into a private, exclusively created staging file, is bounded by 256 MiB, and is fsynced. Integrity is the fetched file's own SHA-256 and size, never Immich's checksum of the original. No upstream path or filename is used locally. A still's orientation comes from the fetched file's EXIF (a preview is already upright; a JPEG full-size copy may carry EXIF); apply it exactly once with FFmpeg autorotation disabled:

| EXIF | Pixel transform after raw decode |
|---|---|
| 1 | Identity |
| 2 | `hflip` |
| 3 | `hflip,vflip` |
| 4 | `vflip` |
| 5 | `transpose=clock,hflip` |
| 6 | `transpose=clock` |
| 7 | `transpose=clock,vflip` |
| 8 | `transpose=cclock` |

For a video, normalize the original display matrix to this orthogonal representation and cross-check Immich's orientation; reject arbitrary matrix/shear rather than guess. Preserve the display aspect ratio, including sample-aspect ratio; normalize derivative pixels to SAR 1. Output probing must establish upright dimensions and absence of a residual rotation matrix. Portrait/quality eligibility is the Planner's, on the pool photo's size as Immich shows it, never on an upscaled derivative.

Still recipe: orient, downscale without upscaling to maximum long edge 3,840 px, encode a single JPEG with fixed quality, strip inherited EXIF/location metadata, normalize the accepted SDR color profile. Video recipe: orient, downscale without upscaling to a configurable tested output bound, normalize to SDR BT.709/yuv420p, cap 30 fps, encode H.264 with `libx264`, MP4 `+faststart`, fixed CRF/preset/GOP/profile, and no audio/subtitle/data streams (`-map 0:v:0 -an -sn -dn -map_metadata -1 -map_chapters -1`). Initial bench recipe targets 1,920 long-edge, at most 2,073,600 pixels, H.264 High level 4.2. If its result falls below the Frame-required quality floor, the Planner's existing variant check rejects it; do not upscale to manufacture readiness.

Invoke FFmpeg/ffprobe as argument arrays with `-nostdin`, bounded logs, no shell, local staged inputs, explicit demuxer/protocol allowlists, and network protocols disabled. A URL in media must not become a nested network request. The conversion process gets only required read-only runtime files and its private staging directory; limit output file size, aggregate staging bytes, wall time, CPU, and memory independently. Kill/reap its process group on timeout/cancellation and remove partial output. FFmpeg's `-fs` is only a secondary stop condition; enforce the filesystem/process limit and postcheck too. [FFmpeg options](https://ffmpeg.org/ffmpeg.html), [filters](https://ffmpeg.org/ffmpeg-filters.html).

After conversion, probe and fully decode the output under the same limits; verify codec/container, stream count, SDR profile, dimensions, duration, frame-rate bound, orientation, and output byte cap. Hash final bytes, fsync the file, and publish to a canonical digest-derived path on the same filesystem. Only then can the repository atomically expose `Variant(sha256,size,media_type,width,height,duration)`. JPEG has `duration=None`; MP4 has a measured positive duration. This establishes prepared **bytes**, not a Player's decoder readiness, aggregate capacity, or execution commitment.

## PostgreSQL, file publication, and reference interface

The root-owned repositories are the only SQL owners: `PgPhotoLibrary` for connections, lists, pools, drafts and check marks; `MediaRepository` for `asset_revisions`, `media_jobs`, `media_blobs` and `media_references`. A pool row's in-pool photo is also an `asset_revisions` row (with its connection, Immich id and `updatedAt`), which is what preparation leases. Blob identity/metadata are immutable. A job is unique for `(asset_revision, recipe_id)` and has `queued/running/retry/ready/failed` state, attempt generation, attempt expiry, bounded failure code, and reserved bytes. Procrastinate owns queue attempts and retry timing. References identify their typed owner and exact digest rather than only a source or Run.

The worker side of the photo library store (`LibraryWorkerStore`):

* `due_contacts()`, `begin_contact(ref) -> ContactTicket`, `record_contact(ticket, result) -> bool` (applied only when newer), `record_list(ref, kind, …)`.
* `due_pools()`, `begin_check(pool_key) -> CheckLease | None` (None for an unwanted pool; resumes a running check with its progress), `known_revisions(…)`, `record_step(lease, step) -> bool` (fenced by check number; upserts rows, stores marks, queues the next step in the same transaction), `finish_check(lease, finish) -> bool` (sweeps only on complete).
* `connection_secret(ref)`, `frame_sizes()`, `pool_thumbnail(asset_id)`, `retire_unwanted()` (drafts older than a day, then unwanted pools, then thumbnail records outside wanted pools).

The Planner's read is `MediaRepository.source_pools_in(conn, source_refs) -> {source_ref: SourcePool}` over `PgPhotoLibrary.pool_members_in` ([Planner](module-planner.md#inputs-state-and-output)).

The preparation and delivery operations stay as before:

* `request_acquisitions(requests)`: idempotently create selected asset/recipe jobs and defer their Procrastinate task through its external PostgreSQL connection in the same transaction. Reject the explicit job/count bound; never perform network or conversion inside the transaction.
* `claim_job(job_id) -> Lease | None`: lock only the exact job delivered by Procrastinate, increment its attempt token, and transactionally reserve the complete worst-case staging budget under the media quota lock. A pool photo's lease carries its `LibraryAsset`. No row lock survives the transaction during conversion.
* `publish(lease, result)` and `fail(lease, code, retry=...)`: compare-and-swap on live attempt identity. An expired/stale task may clean its own staging files but cannot publish over a replacement attempt. Publication verifies expected asset/recipe identity and transitions the blob/job atomically.
* `offer_plan_media(plan_identity, exact_layers, player_authority, now)`: authorize and pin every digest in the offered current plan **before** returning it to a Player. Limit offers to true projection intervals and record expiry; a digest alone is not authorization.
* `lock_secured(plan_identity, readiness, now)`: central coordination checks current epoch/revision/bindings and exact offered Layer under its transaction, inserts immutable assignment locks, and upgrades/reuses media references before dropping offer references. This happens on `secured` receipt, before formal commitment.
* `open_authorized_blob(player, digest, now) -> ReadLease`: current nonretired Player, current Frame generations, and its live offer/assignment references must authorize that exact digest. Acquire an in-flight reference before opening a nonsymlink canonical regular file. Return coded errors and a read-only file descriptor, never an upstream locator.
* `release_owner(owner, reason, now)` and `collect_unreferenced(now)`: lifecycle/coordination explicitly release completed/cancelled/superseded work; timed offers and crashed transfer leases can expire. Garbage collection selects only unreferenced blobs while holding the same quota/reference lock.

All operations creating/removing blob references or reserving/evicting bytes serialize on one PostgreSQL advisory lock for the initial single-installation media store. Multiple references to the same digest count its bytes once. A plan offer pins bytes before download; a secured acknowledgment pins/locks the exact assignment before commit; active transfers independently pin their source file. Supersession cannot destroy a lock already secured on the Player but racing its acknowledgment: keep an offered revision's exact manifest and pin through a bounded readiness reconciliation/grace interval, accept only still-valid authority, and release it only after explicit supersession acknowledgment or execution lease expiry. A photo leaving a pool, or a pool swept, never unlocks secured content.

Filesystem and SQL are not one transaction. Reserve before download; write/fsync/rename before committing readiness. Use a per-blob publication journal/record plus the quota lock to prevent GC from treating a live publication as an orphan. Worker restart reclaims expired attempts only after checking their authoritative lease; stale partial/orphan files remain byte-accounted until successfully deleted. Startup reconciliation verifies ready files against size/hash, invalidates corrupt ready rows, and reports failures to coordination. A rename-before-ready crash leaves an untrusted, accounted orphan, never a downloadable ready blob. A failed unlink retains accounting and refuses further acquisition if necessary.

GC has a durable `deleting` state: mark only an unreferenced blob while serialized with reference creation; new offers cannot reference it; unlink; then remove its accounting. A crash or unlink failure keeps it unavailable and accounted until reconciliation finishes. Quota reductions preserve all existing references and expose pressure rather than deleting pins. Fetched files may be discarded after verified variant publication when no job needs them; long Runs do not pin their history. Central cannot reconstruct a lost locked derivative from a changed upstream photo and must report `blob_lost` while a healthy Player retains its valid local copy/fallback.

## Immutable Photo Wall delivery

The gateway route is the exact relative path generated by the existing `Variant` contract, `/v1/media/<sha256>`. It returns local immutable bytes with exact content length/type, ETag equal to the digest, no content encoding, no upstream-derived headers, and no `Location`. Accept Player bearer authentication through the central authority check on every request; unbound/retired/stale authority is denied even if the caller knows a digest. Use private cache policy. GET/HEAD may be supported; in v1 reject range requests explicitly rather than implement incorrect partial hash semantics.

A file descriptor and fixed transfer lease protect a stream from GC until its hard lifetime; the server releases it on completion, disconnect, error, or timeout. Revalidation on each request rejects new retired authority; it does not claim to revoke bytes already transferred. The Player rejects every 3xx and verifies exact bytes against its Variant before reporting secured. A gateway miss never downloads from Immich on behalf of that HTTP request and never redirects there. It returns `media_unavailable` and schedules appropriate central repair through module ownership.

## Disposable real-Immich integration fixture

No user library or credentials are needed. The [fixture module](module-immich-fixture.md) owns the harness: a dedicated Compose project from the **tagged** official Immich compose, its dependencies, no fixed container names, only newly created volumes, `ghcr.io/immich-app/immich-server:v2.5.6` pinned by digest, machine learning disabled (people come from manual faces). The isolation proof uses two internal networks:

| Service | `wall_net` | `upstream_net` |
|---|---|---|
| Simulated Player test containers | Yes | No |
| Photo Wall central/gateway and media worker | Yes | Yes |
| Photo Wall PostgreSQL | Yes | No |
| Immich server, its PostgreSQL/Valkey | No | Yes |
| Fixture setup/fault driver | No | Yes |

Neither Immich nor its dependencies publishes a host port, except the one loopback port `--publish` opens for the named CI tests. Player containers have no host networking, host gateway mapping, Docker socket, proxy, or network-admin capability. From a Player namespace, both upstream DNS and a TCP request to Immich's numeric container address must fail while the Photo Wall gateway succeeds, before and after acquisition. The named tests that prove delivery 4 against it are listed in the [roadmap](roadmap.md#4-real-photo-sources).

This establishes integration evidence for central media and a recording Player. It does not establish physical Pi rendering, PXE, HDMI continuity, or visible coordination; those remain [physical validation](validation.md#physical-player-and-timing) gates.

## Runtime inventory and remaining decisions

All required deployed components are free/self-hosted. The development machine's Docker Desktop is a test host, not a production runtime dependency; clean self-hosted launch must also work on Linux Docker Engine with Compose. Photo Wall requires no hosted media/CDN service, paid queue, or SaaS account.

| Component | Exact starting pin / inventory requirement | Primary license/source |
|---|---|---|
| Central/worker Python | Existing `python:3.12.11-slim-bookworm`; existing `uv.lock` pins httpx, psycopg, Pydantic, Pillow and application dependencies. Record resolved image digest and installed packages. | [Python license](https://docs.python.org/3/license.html); package license files in the locked distribution. |
| Photo Wall DB | Existing `postgres:16.9-bookworm`, separate from Immich DB; record digest/packages. | [PostgreSQL license](https://www.postgresql.org/about/licence/). |
| FFmpeg/ffprobe/x264 | Debian bookworm `ffmpeg` with matching libav packages; record `ffmpeg -version`, `-buildconf`, `-encoders`, `-filters`, and `dpkg-query` output in the built worker. | [Debian package](https://packages.debian.org/bookworm/ffmpeg), [FFmpeg licensing](https://ffmpeg.org/legal.html); `libx264` makes this an FFmpeg GPL build. |
| Immich | `ghcr.io/immich-app/immich-server:v2.5.6`, resolved by digest in the fixture Compose. | [Tagged AGPL-3.0 license](https://github.com/immich-app/immich/blob/3be8e265cd5bd6ca921ff9e665e274c5de45caa0/LICENSE). |
| Immich queue | Tagged compose pins `docker.io/valkey/valkey:9@sha256:546304417feac0874c3dd576e0952c6bb8f06bb4093ea0c9ca303c73cf458f63`. This queue belongs to Immich; Photo Wall jobs use PostgreSQL. | [Valkey BSD license](https://valkey.io/topics/license/). |
| Immich DB/extensions | Tagged compose pins `ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0@sha256:bcf63357191b76a916ae5eb93464d65c07511da41e3bf7a8416db519b40b1c23`; an independent DB, not Photo Wall PostgreSQL 16. | [PostgreSQL](https://www.postgresql.org/about/licence/), [VectorChord 0.4.3 license](https://github.com/tensorchord/VectorChord/blob/0.4.3/LICENSE), [pgvecto.rs v0.2.0 license](https://github.com/tensorchord/pgvecto.rs/blob/v0.2.0/LICENSE). |

Before qualification: follow the 30-second acknowledgment diagnostic grace, lease-safe release policy and root repository ownership in [decision 0003](decisions/0003-coordination-and-player-execution.md); pin a reproducible FFmpeg package snapshot; and measure check time on a real library (the fixture proves behaviour, not the time a 100,000-photo first check takes). Package/image digests must be measured from the built artifacts, not invented. Project licensing and security contact remain the repository-owner decisions already recorded in [design decisions](design-decisions.md).
