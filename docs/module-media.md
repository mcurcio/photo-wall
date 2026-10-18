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

A **connection** is one Immich server and one key, added in Settings › Photo library or set by the deployment in Central's environment (`central/photo_library/provisioning.py`, read-only in Settings: the API refuses to change or remove it, 409 `connection_managed`; [runbook](runbook.md#the-deployments-immich)). Its row holds a name, the URL in normal form (`policy.normalise_url`: `http` or `https`, a host, no user, query or fragment; `/api` appended when missing, so the owner may paste `http://immich.local:2283`), the key, the Immich user it belongs to, its health and Immich's counts. The key is written through the API and never read back: answers carry only its last four characters ("…1234"). `PgPhotoLibrary` selects it in exactly three methods (`begin_contact`, `connection_secret`, `begin_check`), all on the worker side, and compares it, never returning it, in `provision_connection` at Central's start; no route answer, job argument, log line or console file carries it. Why the key lives in the database, and what that costs, is [decision 0020](decisions/0020-immich-key-in-central-database.md). Plain `http` is accepted; a private certificate authority must be in the worker's system trust store (the connections file's `ca_file` option is gone).

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
| `immich_too_old` | older than 3.1.0 | Update Immich, then Check again |
| `unreachable` | no connection or a timeout | Check again |
| `unexpected_answer` | an answer that did not parse (its code is the detail) | Check again; Details |
| `other_user` | the key belongs to another Immich user than the connection's | Paste a key for that user |

| Note | Meaning |
|---|---|
| `untested_version` | newer than 3.1.x, the tested release; it keeps working |
| `no_full_size_copies` | Immich keeps no full-size copies, so an unedited photo whose original Photo Wall cannot read (RAW) plays at Immich's preview size; every other photo is fetched as its original or its edited full-size copy ([the copy choice](#original-validation-and-preparation-recipe)) |
| `no_face_detection` | Immich's face detection is off, so crops ignore faces (5a; the console says "Immich's face detection is off, so crops ignore faces.") |

A connection belongs to the Immich user of its first passing contact (an Immich older than 3.1 does not bind it); a later key of another user is `other_user`. **While a connection needs a look, the last pool of every Source on it keeps playing** (C7): a rejected key or an outage never empties the wall, and removing the connection is the only way to stop it. Counts are those of the last passing contact, and they are Immich's own totals (`GET assets/statistics` with no filter), which include archived photos. A pool never includes archived photos, so a Source of "everything" can show fewer photos than the connection's count (on the fixture: 25 counted, 24 on the timeline).

The key needs exactly these permissions (one home: `policy.REQUIRED_PERMISSIONS`) and no upload, edit, delete, share or administration permission. What each is for, and what happens without it, is the runbook's [library key](runbook.md#the-library-key).

`user.read`, `asset.read`, `asset.view`, `asset.download`, `asset.statistics`, `album.read`, `album.statistics`, `person.read`, `tag.read`, `face.read` (the face boxes smart crop keeps whole, 5a), `asset.edit.get`.

**Versions.** Immich 3.1.x is tested (the CI fixture runs v3.1.0; follow-up to 5a, slice S4: production runs 3.1, and the adapter reads 3.x answers, such as durations in milliseconds and an album's owner among its `albumUsers`). An older Immich is Needs a look ("Update Immich"); a newer one keeps working with the `untested_version` note. A changed field in a newer Immich reads as `unexpected_answer` with the field's code, never as an empty pool. The offline answers in `tests/test_immich_library.py` are the record of what 3.1 sends.

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
3. **Include.** `POST search/metadata` with the query's filters, `visibility: timeline` (archived, hidden and locked never answer), `withStacked: true`, `withExif: true` (every search a check sends asks it, exclusions included), `order: desc` and size 1,000 (`LibraryLimits.page_size`), read by **keyset pages**: each page continues from the oldest capture time seen, inclusive (`takenBefore`), and drops the ids already seen at that instant. The adapter never asks for `page` 2: Immich pages by position, ordered only by capture time, and offset pages lose photos at equal times (the [fixture](module-immich-fixture.md#executed-evidence) proved it). A full page at one instant switches to upload time within that instant (`createdBefore`); more than 1,000 photos at one capture and one upload instant ends the check **incomplete**. Immich 2.5.6's search answer never says which photos are stacked copies (`withStacked` changes nothing), so for each page the step also reads the timeline months around any photo near a stacked month, and marks the photos the collapsed timeline does not show.
4. **Re-check** (follow-up S4). Each row the walk neither saw nor marked is asked about alone (`LibraryClient.still_matches`), one photo per step: it stays if Immich still answers it for the query and leaves if not. A photo whose capture time moved behind the keyset edge during the check is missed by the walk but never removed. When no such row is left, the check is complete.
5. **Done.** The next step finishes the check as incomplete (one instant held more than a page); nothing is re-checked or removed.

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

A bad answer affects only its photo. An in-pool photo is a `LibraryAsset`: connection, Immich id, revision, kind, capture time, upright size as Immich shows it (Immich's own `width` and `height`, already upright and with edits applied, so Photo Wall reads no orientation), a video's duration, and the **original file's facts** (`OriginalFile`, owner 2026-10-11: "Part of the immich discovery should include fetching (and caching?) metadata about the original photos"): its type (`originalMimeType`), its stored pixels (`exifInfo.exifImageWidth/Height`, before the EXIF orientation: proven on the fixture, so `OriginalFile.upright` swaps them for orientations 5 to 8), its orientation and its bytes, each None when Immich did not say. They come with the search answer, about 1 KB more per row and no extra request, and are kept on the photo's `library_assets` row: a cache of Immich rewritten by every check, never needed for correctness (a fetch measures what it downloads). Its **revision** is Immich's checksum, or the checksum plus the first 16 hex of the sha256 of its Immich edits (`GET assets/{id}/edits`) when it is edited; a revision already known is reused while Immich's `updatedAt` is unchanged. Photo Wall's `asset_id` is `asset_identity(connection, Immich id, revision)`, so an unedited photo keeps the id it had before delivery 4, and an edit makes a new asset.

**A check adds what it confirms and removes only when it completes.** Each step upserts its rows with the check's number in one transaction and queues the next step in the same transaction. A finished check:

| Finish | Pool |
|---|---|
| complete | rows this check marked are removed, and so are rows the Re-check found gone; zero rows here is truly empty |
| incomplete (more than 1,000 photos at one instant) | nothing removed |
| failed: `connection` (see the connection's health) | nothing removed |
| failed: `chosen_item_gone` (an album, person or tag deleted in Immich: Immich answers 400 for its id) | nothing removed |

A row the walk did not see is not removed by the walk: only the Re-check removes one (one request per unseen photo, so untagging 10,000 photos is 10,000 short steps). A failed, incomplete or interrupted check therefore removes nothing, and a newer check fences an older one's late steps by number. Every check re-reads every match (about 100 requests per 100,000 photos, plus exclusions, every 15 minutes). That is sound but not cheap; Immich's sync stream, which reports only changes, was rejected because Photo Wall would then compute membership from the whole library and would write acknowledgements to Immich.

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
| `GET /v1/operator/library/photos/{asset_id}` | one pool photo's facts (`PhotoFacts`): its size as Immich shows it, whether it is edited, its original file, each of Immich's copies in the order thumbnail, preview, full-size, original (whether Immich keeps it, `policy.copies_in_immich`, and the size Photo Wall measured when it downloaded it; never an assumed size), and the copy the current recipe's prepared copy was made from. 404 `photo_not_found` for an id no wanted pool holds. No Immich id or URL (R22) |

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
| Only Immich 3.1.0 is tested (the fixture since the Frame-sized media follow-up); 2.5.x is no longer run | An older Immich works with a note until a field changes, then reads as `unexpected_answer` |
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
| Storage | One cache budget for prepared copies: `CacheSettings.media_budget_bytes` (4 GiB, `PHOTO_WALL_MEDIA_CACHE_BYTES`) over `media/blobs`. Every file a live pin holds is kept; the others go least recently served first until the directory fits ([the media cache budget](module-media-worker.md#the-media-cache-budget)). A file is evictable because it can be made again from its name. Per-job staging stays bounded by `PreparationLimits` (256 MiB fetched, JPEG output at most 16 MiB, MP4 at most 256 MiB). |
| Delivery | Full-file requests only in v1; maximum active transfer count 8 globally. Each descriptor/response has a hard 120 s lifetime and a 150 s database reference for cleanup margin. Player enforces its independent cache/download bounds. |

Use `httpx` with `trust_env=False`, the connection's normalised base URL, explicit TLS verification, no source-supplied URL fragments, and `x-api-key` only toward that base. Metadata requests follow no redirect. A rendition fetch follows at most three redirects, and only to the same origin (scheme, host and port); a redirect elsewhere is `upstream_redirect`. Never forward upstream headers or bodies to a Player. A response `Content-Type` is a hint, not proof of safe media or matching bytes.

A connection-wide failure (401, unreachable, a malformed answer) becomes the connection's health and fails the running check with `connection`; a 403 on one statistics read means that permission is missing. A per-photo problem is that photo's outcome. Preparation failures keep their codes (`asset_missing`, `asset_permission`, `asset_changed`, `asset_integrity`, conversion and resource codes). Do not persist raw exception strings, URLs with credentials, request bodies, or upstream error bodies.

Retry transport failures with bounded backoff, initially 5/15/60 s; attempts are separately leased and never run concurrently for one job. Unsupported media and integrity failures require a new revision or an explicit retry after their recorded cooldown. Return failures to central coordination for Planner and Runtime ownership. During that cooldown, exclude the failed asset revision from **unsecured** dynamic/authored candidate inputs and report its code. Locked Layers are retained independently. No media worker retry authorizes late execution or rerolls a secured assignment.

## Original validation and preparation recipe

Preparation fetches **the photo as Immich shows it**, edits included (owner, 2026-10-09: "whichever one shows up in immich"), and only for planned photos within the preparation horizon. Checking 100,000 photos never means downloading 100,000 files.

- **Photos: one copy per photo, sized for the wall, from the smallest Immich copy that holds it** (owner, 2026-10-09: "use Immich downsampled copy only if it is at least the visible Frame size, else the original"; 2026-10-11: measured "against the Frame output and not the Display"; 2026-10-12: "i expect the final composition to be handled on the Player, via scaling and masking. it would be inefficient for Central to scale and crop every conceivable variation for every Frame"). [`central/photo_library/rendition.py`](../central/photo_library/rendition.py) is the one pure home of the rules; the media worker's fetch, its measurement, the fit rule, the fit analysis, the Planner's variant names and the photo sheet's "Which copy plays" all ask it:
  - **The ladder** (`ladder_of`, `copy_ladder`): the copies Photo Wall can read, smallest first. A video: the original. An edited photo: preview, then the edited full-size copy (Immich always keeps one). An unedited photo whose type preparation reads (`STILL_MEDIA_TYPES`): preview, then the original. One Immich serves as itself but preparation cannot read (a GIF, a BMP): the preview alone. Any other unedited photo (a RAW file, or a type Immich did not say): the preview alone when the connection's full-size copies are off, else preview and full-size. The thumbnail is never on it.
  - **The readable size** (`readable_size`): the size of the ladder's largest copy, or `None` when that size is a copy Photo Wall has not measured. Nothing guesses it: a photo whose readable size is `None` is not placed, and its preview is measured (a `MeasureRequest`, `copy_to_measure`). An unedited RAW photo with full-size copies off is therefore judged at its preview's size, never its original's: when that preview is too small for a Frame the photo is ineligible there (`below_frame_pixels`). This closes the open finding of deliveries 4 and 5a, where such a photo played at preview size on a larger Frame.
  - **The wall need** (`wall_need`): ONE copy per photo and recipe, never cropped and never per Frame. Its long edge is the smallest step of a coarse ladder (`FitSettings.copy_ladder_px`, default 1,920, 2,560 and 3,840, then `FitSettings.max_still_edge`) that holds the largest need among the Frames the photo could fill (`fitting_outputs`, with `copy_edge` of each Frame's need). A photo moving between Frames reuses its copy, and adding a smaller Frame prepares nothing again. The copy made is the need or the readable size, whichever is smaller: preparation never upscales (`Preparer._size`).
  - **The still limit** (`FitSettings.max_still_edge`, default 4,096, range 1,024 to 16,384, set on the photo fit settings page): the longest edge a Player shows a still at. The default is a placeholder, to be measured on the bench Pi 5 from the renderer's `GL_MAX_TEXTURE_SIZE`. A Frame whose need for a photo's copy is longer refuses the photo as `above_player_limit` ("needs more detail than this Pi can draw"), counted on the Frame page beside the other refusals; the Pi's limit in pixels is under that Frame's Details. A photo whose readable size is not measured yet is not a refusal: the Frame page says it is waiting to be measured, apart from the reasons. No copy is longer than the limit.
  - **The fetch** (`media/library_client.py` `fetch_plan`): the first copy on the ladder that holds the need, else the last. The full-resolution copy is the edited full-size copy for an edited photo (`size=fullsize&edited=true`), the original for an unedited photo whose type preparation reads (`assets/{id}/original?edited=false`), and otherwise `size=fullsize&edited=false`, which Immich answers with its full-size copy when it keeps one and redirects to the preview when it does not (a redirect to the preview already downloaded keeps it). A preview whose stored size (`asset_copies`) is known not to hold is not downloaded again. Every still copy a fetch downloads is measured from its header and recorded (`record_copies`); the original is never measured (Immich reports its size). The variant's record names the copy Immich served (`made_from`).
- **Where a photo can still play below its Frame output** (F-FU-4, narrowed by the Frame-sized media follow-up): a Frame added or enlarged after photos were prepared, until they are prepared again (its larger need names a new recipe, which the next plan prepares). A photo whose largest readable copy is too small for a Frame is not placed on it at all (above), and a Frame whose need is above the still limit refuses the photo.
- **Videos.** Fetch the original (`assets/{id}/original?edited=false`); Immich does not edit videos.

The recipes (`StillRecipe` version 3 for photos, `VideoRecipe` version 4 for videos, [preparation](module-media-preparation.md)) accept JPEG, PNG, WebP, HEIC/HEIF and AVIF stills and ordinary SDR MP4/MOV H.264/HEVC video; every video's copy is HEVC 1080p30 ([0029](decisions/0029-render-planes.md)), and the split into two recipes (render redesign) changed every recipe id, so every photo and video was prepared once more. A still whose RGB ICC profile is not sRGB (an iPhone's Display P3, which Immich's previews keep by default) is converted to sRGB by LittleCMS; HEIC is read by libheif (pillow-heif) and AVIF by Pillow's libavif ([preparation](module-media-preparation.md#checks-before-conversion)). Animated images, PQ and HLG HDR stills, unusual rotation matrices and unsupported containers are explicit preparation failures; a gain-map photo prepares from its base image. Untagged ordinary 8-bit JPEG is interpreted as sRGB. Recipe 3 and the copy choice meant every photo was fetched and prepared once more when they landed (F-FU-3), and the Frame-sized media follow-up does it once more, because a copy is now named by a new recipe id ([the asset tables](#the-asset-tables)): more Immich traffic for a while, from the original where the preview was too small, while Frames keep showing their prepared copies. The conversion is proven on synthetic Display P3, HEIC and AVIF files only, not yet on a real iPhone library (F-FU-1).

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

Still recipe: orient, downscale without upscaling to the variant's long edge (`StillRecipe.long_edge`, at most 16,384: the wall need's step, not a fixed 3,840 px), encode a single JPEG with fixed quality, strip inherited EXIF/location metadata, normalize the accepted SDR color profile. Video recipe: orient, downscale without upscaling to a configurable tested output bound, normalize to SDR BT.709/yuv420p, cap 30 fps, encode HEVC Main level 4.1 with `libx265` in an `hvc1` MP4 with `+faststart`, fixed CRF/preset/GOP, and no audio/subtitle/data streams (`-map 0:v:0 -an -sn -dn -map_metadata -1 -map_chapters -1`), at most 1,920 long-edge and 2,073,600 pixels (`VideoRecipe`, [preparation](module-media-preparation.md)). If its result falls below the Frame-required quality floor, the Planner's existing variant check rejects it; do not upscale to manufacture readiness.

Invoke FFmpeg/ffprobe as argument arrays with `-nostdin`, bounded logs, no shell, local staged inputs, explicit demuxer/protocol allowlists, and network protocols disabled. A URL in media must not become a nested network request. The conversion process gets only required read-only runtime files and its private staging directory; limit output file size, aggregate staging bytes, wall time, CPU, and memory independently. Kill/reap its process group on timeout/cancellation and remove partial output. FFmpeg's `-fs` is only a secondary stop condition; enforce the filesystem/process limit and postcheck too. [FFmpeg options](https://ffmpeg.org/ffmpeg.html), [filters](https://ffmpeg.org/ffmpeg-filters.html).

After conversion, probe and fully decode the output under the same limits; verify codec/container, stream count, SDR profile, dimensions, duration, frame-rate bound, orientation, and output byte cap. Hash final bytes, fsync the file, and publish to a canonical digest-derived path on the same filesystem. Only then is the file placed at `media/blobs/<sha256>` without overwriting a path (`CacheStore.install_by_digest`) and its facts recorded on the variant's record as `MediaReady`; the `Variant` a plan carries is `asset_id`, `recipe_id`, `sha256`, `size`, `media_type`, `width`, `height` and `duration`. JPEG has `duration=None`; MP4 has a measured positive duration. This establishes prepared **bytes**, not a Player's decoder readiness, aggregate capacity, or execution commitment.

## The asset tables

Owner, 2026-10-11: "I would build this as a normalized database, with each upstream immich asset being a primary resource and then have no join tables which hold different resolutions, crops, encodings of that asset." Built (migration 080, follow-ups slice A2) as one parent row per Immich asset, with every version of it in a child table keyed to it (one-to-many; no table joins two assets):

| Table | Key | Holds | Written by |
|---|---|---|---|
| `library_assets` (the parent) | (connection, Immich asset id) | Kind, taken time, the original file's type, stored width and height, EXIF orientation and bytes (NULL until a check reads them), first and last seen | Every check step that names the photo's kind and taken time. A photo read in full rewrites the original's facts; a waiting or left-out photo keeps them |
| `asset_revisions` | `asset_id` (connection, Immich id, revision) | One per edit Immich has made: the photo as Immich shows it, upright, its width, height and video duration, Immich's `updatedAt` | Each check step's in-pool photos |
| `asset_copies` | (`asset_id`, copy) | Each Immich copy of a revision Photo Wall downloaded (thumbnail, preview, full-size): its type and measured size. The original's size is the parent's (Immich reports it), never measured twice | The worker after a fetch (`record_copies`) |
| `assets` (kind `media-variant`) | `<asset_id>.<recipe_id>` | Each prepared copy: its digest and size, and in `produced_detail` the media type, size in pixels, duration and `made_from`, the Immich copy it was made from (`MediaReady`); `last_served_at`. The file is at `media/blobs/<sha256>` | `MediaMaker` (the `PrepareMedia` handler), through the job runtime |
| `media_recipes` | `recipe_id` | The recipe values and the wall need a recipe id hashes, so a variant can be made again from its name alone | `MediaRepository`, when the Planner names a variant |

A pool row (`photo_pool_rows`) names a revision by `asset_id` and copies none of its facts, so the pool page, its counts, the Planner's members and the thumbnail read join the revision and its parent. `central.infra.photo_library_store.library_assets_in` is the one place a stored photo becomes a `LibraryAsset`: the media maker and the Planner's hydration read it there. `library_connections.full_size_copies` keeps Immich's last answer on full-size copies.

No cropped file exists: the Player crops at draw time, and a crop is a fact of the plan's layer. A prepared copy is a **media variant**, named by its inputs and stored by its bytes: the name is `<asset_id>.<recipe_id>` (the photo revision and the recipe), the record maps the name to its current output digest, and the file lives at `media/blobs/<sha256>`. A path is never overwritten, so a body always matches the digest read with it, even while a re-make is in flight. `recipe_id` (`media/prepare.py`, the one home of what the name hashes) is the hash of the recipe values and the wall need's long edge. **The renderer build is not in it** (the follow-up's open owner question, default "drop"; keeping it is a change to that one function), so a worker upgrade does not re-name every variant. The cache is ephemeral: a purged or corrupt file is made again from its name, and the records are the truth. Migration 087 moved the old `media_jobs` and `media_blobs` records onto variant records (their recipes name no need, so they are served while their files last, under the owner `legacy-087`, and never made again; once evicted, the photo is prepared again under a current recipe); migration 088 dropped both tables.

Cost: every pool read is a join. With 50,000 photos in one pool (seeded through the store), the page with its counts reads in about 60 ms and the Planner's whole-pool read in about 260 ms on the development Mac; should it grow, the fix is an index, never copying the facts back onto pool rows. Migration 081 converts the pre-4 records a Scene still holds into revisions with a parent row (their originals unknown) and drops the old `metadata` JSON.

## The Frame output

Owner, 2026-10-11: compatibility must be "measuring against the Frame output and not the Display". The Frame output is the Frame's picture as drawn on its Output, in output pixels along the picture's own width and height (`contracts.frame_fit.picture_size`): in Cover, the cover rectangle (a mat opening needs fewer pixels than the Display; a picture turned off-axis needs more than its opening); in Corners, the longer of each pair of opposite edges, a quarter turn swapping them. `contracts.models.frame_output` computes it from the Frame's profile (its shape), its Position and the size its Output last reported; `central.infra.display_store.frame_outputs_in` is the one read of every Frame's (the worker's `frame_outputs`). A Frame whose Output never reported a size has none. "This photo, or this copy of it, is at least the Frame output" is one test, `contracts.frame_fit.window_holds`: the largest window of the Frame's shape cut from the photo holds the Frame output on both sides (owner, 2026-10-09: Immich's downsampled copy "only if it is at least the visible Frame size, else the original").

## PostgreSQL, file publication, and reference interface

The root-owned repositories are the only SQL owners: `PgPhotoLibrary` for connections, lists, pools, drafts, check marks and [the asset tables](#the-asset-tables) a check or fetch writes; `MediaRepository` for variant naming and hydration (`variant_inputs`, `media_recipes`) and `media_references`; the asset layer's `AssetRecords` for variant records. A pool row's in-pool photo is an `asset_revisions` row under its `library_assets` parent, which is what `PrepareMedia` reads. A pin is a `media_references` row, typed by its owner (an offer, a secured lock, a transfer) and keyed by the variant's name.

The worker side of the photo library store (`LibraryWorkerStore`):

* `due_contacts()`, `begin_contact(ref) -> ContactTicket`, `record_contact(ticket, result) -> bool` (applied only when newer), `record_list(ref, kind, …)`.
* `due_pools()`, `begin_check(pool_key) -> CheckLease | None` (None for an unwanted pool; resumes a running check with its progress), `known_revisions(…)`, `record_step(lease, step) -> bool` (fenced by check number; upserts rows, stores marks, queues the next step in the same transaction), `finish_check(lease, finish) -> bool` (removes only on complete, and only the rows the check marked), `unseen_rows(lease, limit)` and `record_recheck(lease, ref, matches)` (the Re-check: the one removal of a row the check did not see).
* `connection_secret(ref)`, `frame_outputs()` ([the Frame output](#the-frame-output)), `copy_sizes(asset_id)` and `record_copies(asset_id, measured)` (`asset_copies`), `pool_thumbnail(asset_id)`, `retire_unwanted()` (drafts older than a day, then unwanted pools, then thumbnail records outside wanted pools).

The Planner's read is `ScenePoolReader` (`central/photo_library/ports.py`): `source_lines_in` gives each Source revision's pool key, readiness and emptiness, and `ranked_line_in` the top of a Scene's line in SQL, hydrated apart by `MediaRepository.hydrate_in`. Nothing reads a whole pool ([Planner](module-planner.md#inputs-state-and-output)).

The preparation and delivery path (follow-up to 5a; the media store, its jobs and its blobs are gone):

* **Request.** The Planner names every variant a plan needs, and coordination publishes `PrepareMedia` (`central/kernel/job_types.py`, one field: the variant's name) for those with no usable file; a Player's miss publishes the same job (below). The kernel job runtime runs it, not the media queue, with its own rescue. The key is the variant's name, so a duplicate request is absorbed.
* **Make** (`MediaMaker`, `media/media_maker.py`): read the revision and the recipe and need the name stands for (`variant_inputs`), fetch the smallest copy that holds the need (`fetch_plan`), encode it (`Preparer`), install the file by digest and return `MediaReady`; the job runtime writes it on the record with the outcome. A variant whose file is on disk and whole is not made again; a legacy variant answers `legacy_variant`. A failure no retry can mend (the photo's type, colour or size: `PERMANENT` in `media_maker.py`) is terminal; any other is retried by the job's delivery. Nothing is upscaled.
* **Pins.** `FrameMedia.resolve` (`central/assets/frame_media.py`) decides in one transaction, in coordination's lock order, whether a Player's bearer may read a variant now: the session, its configuration at its epoch, the variant's record (none: 404 `media_unavailable`) and the execution's authorization (an offer or a lock that names the variant; refused: 403 `media_forbidden`). It then pins the variant for the transfer (`transfer:<hex>`, 150 s). Every pin is written under the media lock.
* **Release and eviction.** Coordination releases an owner's pins when its work ends or is superseded and expires timed ones. A pinned file is never evicted, and a photo leaving a pool, or a pool swept, never unlocks secured content. Eviction is [the media cache budget](module-media-worker.md#the-media-cache-budget). A lost file is made again from its name while the photo is still in Immich, instead of being reported lost.

## Immutable Photo Wall delivery

`GET /v1/media/{asset_id}/{recipe_id}` (`Variant.path`; the old `/v1/media/<sha256>` route is gone) takes the Player's bearer through `FrameMedia.resolve` and reads the file through the one `AssetReader`. On a hit it serves the file at `media/blobs/<sha256>`, hash-checked. On a miss (a purged, corrupt or never-made file) it publishes `PrepareMedia`, waits at most 30 s for it in one of eight waiter slots, then serves the result or answers 503. The route never reaches Immich; the media worker does, on the job. The response (`stream_frame_media`) has an exact `Content-Length`, the prepared media type, `Repr-Digest: sha-256=:<base64>:` (RFC 9530), a quoted digest ETag, `Cache-Control: private, no-store`, `Accept-Ranges: none` and no `Location`. Range requests are not supported. Unbound, retired or stale authority is denied on every request.

The `Variant` in a plan carries the `sha256` and `size` current when the plan was issued. A re-make can serve other bytes of the same content, so the Player checks the body against the `Repr-Digest` and `Content-Length` it is served, never the plan's, and rejects every 3xx (`player/cache.py`). A file descriptor and the transfer pin protect a stream from eviction until it ends or its hard lifetime; revalidation on each request rejects new retired authority and does not claim to revoke bytes already transferred. Because Players fetch by name, the `Variant` change is a contracts change: merging it cuts a Node package release, and a Pi gets nothing new until it reboots onto it, then downloads again from Central's warm cache (migration 087 re-keyed it).

## Disposable real-Immich integration fixture

No user library or credentials are needed. The [fixture module](module-immich-fixture.md) owns the harness: a dedicated Compose project from the **tagged** official Immich compose, its dependencies, no fixed container names, only newly created volumes, `ghcr.io/immich-app/immich-server:v3.1.0` pinned by digest, machine learning disabled (people come from manual faces). The isolation proof uses two internal networks:

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
| FFmpeg/ffprobe/x265 | Debian bookworm `ffmpeg` with matching libav packages; record `ffmpeg -version`, `-buildconf`, `-encoders`, `-filters`, and `dpkg-query` output in the built worker. | [Debian package](https://packages.debian.org/bookworm/ffmpeg), [FFmpeg licensing](https://ffmpeg.org/legal.html); `libx264` makes this an FFmpeg GPL build. |
| Immich | `ghcr.io/immich-app/immich-server:v3.1.0`, resolved by digest in the fixture Compose. | [AGPL-3.0 license](https://github.com/immich-app/immich/blob/main/LICENSE). |
| Immich queue | Tagged compose pins `docker.io/valkey/valkey:9@sha256:546304417feac0874c3dd576e0952c6bb8f06bb4093ea0c9ca303c73cf458f63`. This queue belongs to Immich; Photo Wall jobs use PostgreSQL. | [Valkey BSD license](https://valkey.io/topics/license/). |
| Immich DB/extensions | Tagged compose pins `ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0@sha256:bcf63357191b76a916ae5eb93464d65c07511da41e3bf7a8416db519b40b1c23`; an independent DB, not Photo Wall PostgreSQL 16. | [PostgreSQL](https://www.postgresql.org/about/licence/), [VectorChord 0.4.3 license](https://github.com/tensorchord/VectorChord/blob/0.4.3/LICENSE), [pgvecto.rs v0.2.0 license](https://github.com/tensorchord/pgvecto.rs/blob/v0.2.0/LICENSE). |

Before qualification: follow the 30-second acknowledgment diagnostic grace, lease-safe release policy and root repository ownership in [decision 0003](decisions/0003-coordination-and-player-execution.md); pin a reproducible FFmpeg package snapshot; and measure check time on a real library (the fixture proves behaviour, not the time a 100,000-photo first check takes). Package/image digests must be measured from the built artifacts, not invented. Project licensing and security contact remain the repository-owner decisions already recorded in [design decisions](design-decisions.md).
