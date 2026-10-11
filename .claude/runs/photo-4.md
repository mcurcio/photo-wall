# Run ledger: roadmap 4, real photo sources from Immich

**Branch:** `feat/photo-sources` (from `origin/main` at 1b721dc), ONE pull request (owner, 2026-10-10: one PR per delivery, no split). Lane branches `lane/photo4-domain`, `lane/photo4-central`, `lane/photo4-planner`, `lane/photo4-media`, `lane/photo4-fixture`, `lane/photo4-console`, `lane/photo4-docs`, `lane/photo4-integrate` are created at the F0 commit; a lane that builds on another starts from that lane's tip (it is a fast-forward of its own branch, since F0 is the ancestor of every tip).
**Rules:** `~/.claude/skills/implementation-workflow/SKILL.md` (binding). An implementer reads **its slice page below, the owner answers, and the design sections the page names**, nothing else.
**Design (accepted overnight; recommended options adopted, the owner reviews it on the PR):** `/Volumes/Dock/tmp/photo-4/design-draft.md` (r1; its "For the code architects" and "Reconcile with existing code" sections are parked notes). Owner answers: `/Volumes/Dock/tmp/photo-4/answers.md` (binding). Review: `/Volumes/Dock/tmp/photo-4/review-r1.md`. Immich facts: the section below (checked against Immich v2.5.6's OpenAPI by the code architect).
**Errata:** `grep -a 'E-P4-' .claude/errata.md`. A page changes only through an erratum and a re-cut: an implementer who hits a wall appends `E-P4-<slice>-<n>` and STOPS.

## The problem in plain words

Today Photo Wall reads Immich through a private file that holds the key: a new key means editing the file and restarting the worker. A Photo source can ask only for tags, dates and favourites. Anything matching more than 1,000 photos is refused, against the owner's "no limit". One bad answer from Immich empties the Source on the wall. Edits made in Immich are ignored, and each copy in a stack plays as its own photo.

Delivery 4 is, in the owner's words, "create an immich query that becomes a photo pool":

- **Connect.** The owner pastes the Immich address and key in Settings › Photo library (no file, no restart) and sees Immich's real counts.
- **Build a Source.** A Photo source picks albums, people, a place, dates, favourites, tags and media types, minus excluded albums, people, tags and screenshots. Archived, hidden and locked photos are never included.
- **Watch it fill.** Photos join the pool page by page as Immich is read, before and after saving. Several Sources can go on one Scene.
- **Immich does the matching.** Photo Wall asks Immich every question and only subtracts the exclusions. A check adds what it confirms, and removes photos only once it has read everything.
- **Outages don't empty the wall.** While Immich is down or the key is rejected, the last pool keeps playing and the connection says Needs a look, with the fix.
- **Planning and preparation.** The Planner reads the whole pool, with no window and no limit. Preparation fetches the photo as Immich shows it, edits included. It takes Immich's preview when that covers the wall's largest Frame, and Immich's full-size copy otherwise.

## Owner answers (binding; copied verbatim from `/Volumes/Dock/tmp/photo-4/answers.md`)

> # Delivery 4: owner words and choices (2026-10-11)
>
> Owner request for tonight's run, verbatim: "That all sounds correct. I’m hoping for one clean overnight build —plan the whole batch". There is no owner gate tonight. Design: design-draft.md (r1) in this folder; its "For the code architects" section holds the parked notes. Review: review-r1.md.
>
> ## Owner words and settled choices for delivery 4 (verbatim, dated; requirements, not choices)
> - 2026-10-11: delivery 4 is "create an immich query that becomes a photo pool"; 5a is "how to apply that pool onto frames".
> - 2026-09-28: "immich should be the computation engine as much as possible"; each photo-source configuration maps to ONE Immich query; OR across tags via several Sources on one Scene was liked but deferred; previews go through the media worker (Central never calls Immich directly); Players are Immich-unaware.
> - 2026-10-09: "i dont want any limit on the count of a source. I never stated that requirement and I disagree with it." The 1,000-match refusal in media/immich.py must go (requirements.md:181 already says no ceiling); page large matches with bounded work per tick.
> - 2026-10-09: use Immich downsampled copy only if it is at least the visible Frame size, else the original; prepare lazily ahead of display.
> - 2026-10-11: breaking changes are "trivially cheap"; no backwards compatibility.
> - 2026-10-09 (UX over security): max UX; security findings matter only if normal-case or not self-recovering.
> - What 5a (built right after 4, design accepted at /Volumes/Dock/tmp/photo-5a/design-draft.md and answers /Volumes/Dock/tmp/photo-5a/answers.md) needs from 4: an Immich stack counts as one photo (the pool takes only the stack primary); the key needs face.read for GET /api/faces?id=<assetId>; the original size and EXIF orientation of each asset for ratio pre-filtering; the wall shows Immich edits, so preparation fetches the edited version Immich analysed.
> - Roadmap 4 (docs/roadmap.md on origin/main, section 4) is the scope and acceptance: connect Immich in Settings with no file and no restart, the real counts, filters (albums, people, places, dates, favourites), exclusions with three defaults (archived, hidden, screenshots), Immich health over time as Needs a look with the fix, several Sources per Scene, settings #8-#33 and #36 in docs/operator-console-design.md, and its named CI tests against tests/integration/compose.immich.yml.
> - One PR for delivery 4, no split (owner 2026-10-10). The roadmap split line is build order only.
>
> ## Choices made tonight (adopted recommended options; owner may reopen)
> - C1: The Immich key is kept in Central's database. It is written through the API and never read back. A connection belongs to the Immich user of its first check.
> - C2: Immich 2.5.x is tested. A newer version keeps working, with a note. A version older than 2.5 shows Needs a look ("Update Immich"). Checks run every 15 min (#12), on Save and on Check now.
> - C3: Several albums, people or tags in one Source mean "all of them". "Any of them" means a second Source on the Scene. #17 defaults to "all". A place is a city, state and country together.
> - C4 (also Question 1: Photo Wall subtracting Immich's answers stands in for the rule of one Immich query): each exclusion is its own Immich query, read whole on every check and subtracted. A screenshot is a file named "screenshot", or a PNG with no camera maker.
> - C5 (also Question 2): archived, hidden and locked photos are never included. This is one fixed line, not switches #29 and #30.
> - C6 (also Question 2): #21 On this day becomes 5a's Order. #24 minimum rating and #33 blurry/duplicates are deferred.
> - C7: The last pool keeps playing while Immich is down or the key is rejected.
> - C8: A draft is a Source not yet saved. Its check is real, photos appear as they are found, and Save works at any time. Unsaved drafts are removed after a day.
> - C9: A stack joins the pool only through its primary, and only if the primary itself matches. Immich's timeline decides which photos are stacked copies.
> - C10: Preparation fetches the edited photo. It takes the preview when the preview covers the wall's largest Frame, else Immich's full-size copy. If Immich's full-size copies are off, HEIC and RAW photos play at preview size, with a note.
> - C11: The Planner reads the Scene's whole pool on each projection. There is no window and no candidate cap.
> - C12: For the cut-over, each existing connection name gets an empty connection row, so existing Sources keep pointing at it. The owner pastes the URL and key once. Until then no new photos play, and Frames keep their last photo.

## Findings for the owner (open; each restated in the PR body)

- **F-1 (departure from the design: the Planner's read is per Source, not per Scene).** The design named `ScenePoolReader.scenePool(scene)`. In the code, though, each Frame of a Scene names its own Sources: Runtime's `Contribution.source_refs` is set per target, and the Planner already merges a contribution's Sources at `planner.py:250-266`. A per-Scene union would mix one Frame's Sources into another's. The read is therefore `ScenePoolReader.source_pools_in(conn, source_refs) -> {source_ref: SourcePool}`, and the Planner unions per contribution with one row per asset, newest first. The console writes the same Sources on every Frame of a Scene, so for every Scene the console makes, the result equals the design's Scene pool. 5a reads the same thing.
- **F-2 (refinement: one pool per distinct query).** A pool is keyed by its query's canonical form (`SourceQuery.pool_key`). A draft and the Source saved from it share one pool, as do two Sources with the same query. So Save carries the pool over by construction, and editing a query back reuses what was already found. `setDraftQuery` points the draft at the new query's pool, which is what the owner sees as "cleared". A pool nothing uses is retired by maintenance.
- **F-3 (departure: Save makes the Source's next revision).** The design said Save returns "`editing`'s ref". The code keeps today's named revisions (`name:N`, `central/source_names.py`). Saving an edit therefore makes `name:N+1`, and Scenes move to it in the same transaction, exactly as a Source edit does today. The pool carries over through F-2. The owner sees the same Source name.
- **F-4 (words).** The code keeps "image" and "video", the words the Planner and Player already use. The console says "photo". The design's TypeScript said "photo".
- **F-5 (live photos, #25; to verify in M1).** Immich v2.5.6's search has no filter for "is a live photo". A live photo is an IMAGE whose answer carries `livePhotoVideoId`. When a query chooses photos but not live photos (or the reverse), the adapter keeps or drops each row by that field: a per-photo kind, as Immich's own `type` is. If that reads as Photo Wall computing a filter, the alternative is one "Photos (and live photos)" choice.
- **F-6 (cut-over of existing Sources, C12).** Migration 080 rewrites each stored Source into the new query. A Source that asked for non-favourites only (`favorites: false`) becomes "all photos", because #22 is a yes/no. Existing Sources also gain the screenshot exclusion, the new default (C4), so their pools may shrink.
- **F-7 (roadmap Developed column).** The Developed column is not on `main`. It is on the unmerged branch `docs/roadmap-developed-column` (e8b61a4), so the docs lane adds no Developed row. Whichever of the two pull requests merges second adds "4 · #<this PR>".
- **F-8 (CI shape and cost).** The `immich-adapter` job is replaced by `photo-sources`, with one Immich start per leg (design parked note). Its old checks move into the named tests: live changes, deletion, permission loss and outage. Its "explicit source-limit failure" check is deleted (owner 2026-10-09: no limit). Cost: the leg installs ffmpeg on the runner (about 30 s) and likely runs about 5 minutes, over the about-4-minutes-per-leg budget (owner memory "No long CI tests").
- **F-9 (lists).** Albums, people, places and tags are re-listed with each connection check, every 15 minutes; before 4, tags were re-listed every 5. Lists have no count cap: the 5,000-tag refusal goes, and lists are stored one row per item.
- **F-10 (caps).** Besides the design's four caps (F1), `source_candidates`' 1,000 refusal also goes. That is the hand-placed photo picker's list, so a huge pool now answers one large list. Paging that picker is not in 4.
- **F-11 (URL and trust).** The owner may paste the server address without "/api" (it is appended), and plain http is accepted. The private CA file option (`ca_file`) goes with the connections file, so a private CA must be in the system trust store.
- **F-12 (Immich facts not yet proven on the fixture).** These come from the OpenAPI spec and are proven only by the CI `photo-sources` leg: `originalFileName` matching "contains, any case"; a stacked copy's search answer carrying `stack.primaryAssetId`; manual faces through `POST /faces` with machine learning off; the place from GPS with reverse geocoding on. M1 and FX1 settle each one on the fixture. A missing capability is an erratum and a stop.

## Immich facts (v2.5.6 OpenAPI, `open-api/immich-openapi-specs.json` at tag v2.5.6, checked 2026-10-11)

- `POST search/metadata` (permission `asset.read`) takes `albumIds`, `personIds`, `tagIds` (nullable), `city`, `state`, `country` (nullable), `takenAfter`/`takenBefore`, `createdAfter`/`createdBefore`, `isFavorite`, `type`, `visibility`, `originalFileName`, `make` (NOT nullable), `model` (nullable), `withStacked`, `withExif`, `size`, `page`, `order`. It has no owner filter and no "not".
- `AssetResponseDto` carries `width`/`height` (nullable until Immich reads the file), `isEdited`, `updatedAt`, `checksum`, `ownerId`, `livePhotoVideoId`, `fileCreatedAt`, `localDateTime`, `originalMimeType`, `type`, `visibility`, and an optional `stack {id, primaryAssetId, assetCount}`.
- `GET api-keys/me` needs no permission and answers the key's `permissions`; `GET users/me` needs `user.read`; `GET server/version` needs none.
- `GET assets/{id}/edits` (`asset.edit.get`) answers `{assetId, edits[]}`; the edit actions are crop, rotate and mirror; `PUT assets/{id}/edits` (`asset.edit.create`) is the fixture's.
- `GET assets/{id}/thumbnail?size=thumbnail|preview|fullsize|original&edited=` (`asset.view`); `GET assets/{id}/original?edited=` (`asset.download`).
- `GET timeline/buckets` and `GET timeline/bucket?timeBucket=` (`asset.read`) take `withStacked`, `withPartners`, `visibility`, `albumId`, `personId`, `tagId`, `userId`, `isFavorite`.
- `GET assets/statistics` → `images`, `videos`, `total` (`asset.statistics`); `GET albums/statistics` → `owned`, `shared`, `notShared` (`album.statistics`); `GET people` → `total`, `hidden`, `people[]`, paged (`person.read`); `POST search/statistics` (`asset.statistics`); `GET search/cities` (`asset.read`).
- `POST faces` (`face.create`) makes a manual face from `assetId`, `personId`, `x`, `y`, `width`, `height`, `imageWidth`, `imageHeight`; `GET faces?id=` needs `face.read` (5a).

## Lanes, slices and order

Tracer first, package depth first:

1. **T0** lands the photo library's vocabulary and rules (`central/photo_library`), which every other code lane imports.
2. **C1** (Central half of the tracer: paste a URL and a key, a check is queued, a contact's answer lands as health and counts) and **M2** (worker half: the check runs against Immich) run next.
3. **P1** and the **console** build on the central lane's routes and store.
4. **I1** retires the old shapes and runs the end-to-end tracer.

| Lane | Branch | Starts from | After | Slices | Expertise |
|---|---|---|---|---|---|
| domain | `lane/photo4-domain` | its own branch (F0) | none | T0 | Python domain modelling, pydantic |
| central | `lane/photo4-central` | `lane/photo4-domain` (its tip) | domain | C1, C2 | PostgreSQL, FastAPI, Central's store and source names |
| media | `lane/photo4-media` | `lane/photo4-domain` (its tip) | domain | M1, M2, M3 | async httpx, Immich API, media worker and preparation |
| fixture | `lane/photo4-fixture` | its own branch (F0) | none | FX1, FX2 | Docker Compose, GitHub Actions, the Immich fixture and wall demo |
| planner | `lane/photo4-planner` | `lane/photo4-central` (its tip) | central | P1 | the Planner, coordination, media repository |
| console | `lane/photo4-console` | `lane/photo4-central` (its tip) | central | K1, K2, K3 | React, the console design system, Playwright |
| docs | `lane/photo4-docs` | its own branch (F0) | none | X1 | owning documents, plain language |
| integrate | `lane/photo4-integrate` | `feat/photo-sources` once every lane above is merged into it | every lane | I1 | cross-package retirement, end-to-end tests |

| Slice | What it delivers | Owned paths (outputs its checks write included) | Depends on |
|---|---|---|---|
| **T0** (tracer, vocabulary) | Validators, canonical form and pool key, the pool cursor, the policy functions | `central/photo_library/model.py`, `central/photo_library/policy.py`, `tests/test_photo_library_model.py`, `tests/test_photo_library_policy.py` | F0 |
| **C1** (tracer, Central half) | Migration 080; connections with a write-only key; contacts fenced by number; health; lists; the connection and list routes | see its page | T0 |
| **C2** | Drafts; pools; the store half of a check (steps, marks, sweep); pool pages; draft, pool and check routes; Save through Source names | see its page | C1 |
| **P1** | The Planner, coordination, authoring and preparation leases read the pools; the caps go | see its page | C2 |
| **M1** | The Immich adapter implements `LibraryClient` | `media/immich.py`, `tests/test_immich_library.py`, `tests/test_immich.py` (only to keep it green) | T0 |
| **M2** (tracer, worker half) | The checks: connection contacts, lists, pool check steps; the tasks; the fake store; the old refresh, preview and tag loops go | see its page | M1 |
| **M3** | The preparation fetch (edited, preview or full-size), the inspector's WebP, edited thumbnails; the connections file goes | see its page | M2 |
| **FX1** | The content set, `--publish`, the fixture accessor, the `photo-sources` CI job, the `immich` marker | see its page | F0 |
| **FX2** | The wall e2e and the images move off the connections file: paste through the API, Sources through drafts | see its page | FX1 |
| **K1** | Settings › Photo library; Needs a look cards on Needs attention | see its page | central lane |
| **K2** | The Photo source editor: Include, Exclude, counts by outcome, pool pages before and after saving | see its page | K1 |
| **K3** | Scene editor › What: several Sources | see its page | K2 |
| **X1** | Owning documents updated to what landed | see its page | drafts from the pages at once; final pass after the code lanes |
| **I1** | Retire the old shapes (migration 081); the end-to-end tracer; the store conformance suite; seams | see its page | every lane |

**Integrate order** (the run merges each lane branch into `feat/photo-sources` with a merge commit, in this order): domain, central, planner, media, fixture, console, docs, then integrate's own slice on top. The lanes' owned files are disjoint. The planner and console lanes both start from the central lane's tip and touch different files (P1: planning and the media repository; the console lane: `central/console/` and `tests/browser/`).

**Seams named now (the end fix's if they open; I1 drives each):**
- (a) **The key.** C1 stores it. `begin_contact`, `connection_secret` and `begin_check` hand it to M2's and M3's clients. No view, job argument, log line or console file carries it.
- (b) **The check.** C2's `begin_check`, `record_step` and `finish_check` pair with M2's `LibraryChecks`. M2's fake store and C2's PostgreSQL store meet in I1's conformance suite.
- (c) **Asset rows.** C2's `record_step` writes `LibraryAsset` into `asset_revisions`. P1's `claim_job` leases it, and M3's `process_job` fetches and prepares it. I1's tracer prepares a pool photo.
- (d) **Thumbnails.** C2 rewires `create_app`'s servable check to `pool_thumbnail`. M3's `LibraryThumbnailOrigin` reads `pool_thumbnail` and `connection_secret`. K2 shows each `PoolItem.thumbnail`.
- (e) **Tasks.** C1 and C2 queue `CHECK_CONNECTION_TASK(connection_ref=)` and `CHECK_POOL_TASK(pool_key=)`. M2 registers both in `media/task_queue.py` under the same names and keyword arguments.
- (f) **The Planner's read.** P1's `MediaRepository.source_pools_in` reads C2's `photo_library_store.pool_members_in`.
- (g) **The wall e2e.** FX2's `demo_wall.py` uses C1's and C2's routes and M2's worker. CI's software e2e judges it at the end.
- (h) **Lists.** M2 lists after a passing contact, through C1's `record_list`. K2's pickers read C1's list routes.

**Land command (every slice).** Run it from the lane's own worktree. `<owned>` is the slice's owned paths that exist; `<lane>` is the lane's name.
```
git add -A -- <owned> && git commit -m '<msg>' -- <owned>
git push origin HEAD:lane/photo4-<lane>
```
A lane that starts from another lane's tip first runs `git fetch origin && git switch --detach origin/lane/photo4-<that lane>`, then pushes `HEAD:lane/photo4-<own lane>` (a fast-forward). A commit that meets a sibling's `index.lock` waits and retries. After a lane's last slice, `git status --porcelain` is empty; anything left is a write outside an owned list and stops the run. Slice agents do not edit this ledger; the run's closing step fills in the outcome table.

## Environment and gates

- Each lane runs in its own worktree (`isolation: "worktree"`). Shell setup for every command: `export UV_CACHE_DIR=/Volumes/Dock/tmp/uv-cache TMPDIR=/Volumes/Dock/tmp/pt PATH=$HOME/.nvm/versions/node/v22.23.2/bin:$PATH; mkdir -p $TMPDIR`. Use `.venv/bin/python` after `uv sync --frozen`, and revert `uv.lock` if anything rewrites it. No slice changes a dependency.
- pytest `--basetemp=/tmp/pw4/<slice>/<gate>` (short, for AF_UNIX socket paths). Every test, build or watch runs in the foreground, through `~/.claude/verify-quiet.sh` for long output; take the exit status from the command itself.
- `PHOTO_WALL_NATS_SERVER=/Volumes/Dock/tmp/nats/nats-server-v2.15.0-darwin-arm64/nats-server` for the DB tier's `test_central_*`.
- **Real Immich.** Tests that take `immich_library` need `PHOTO_WALL_IMMICH_FIXTURE=<state dir>`, from `.venv/bin/python -m scripts.immich_fixture run --keep --setup-only --publish --state-dir /Volumes/Dock/tmp/immich-p4-<lane>`. That command works only once FX1 is in the branch, and it builds the fixture image from the repo Dockerfile, which takes minutes. Without it these tests skip with "set PHOTO_WALL_IMMICH_FIXTURE"; the slice lists them as NOT RUN, and CI's `photo-sources` leg judges them. ffmpeg on this Mac: `/opt/homebrew/bin/ffmpeg`.
- **Test file ownership.** A test module never imports another lane's test module. Shared builders go to `tests/support/`. `tests/test_library_thumbnails.py` keeps importing `sample` and `stored_member` from `tests/test_media_repository.py`, and P1 keeps those two helpers' signatures until I1.
- **Gates.** Locally each slice runs its changed packages' tests, ruff, lint-imports and `check_docs`, plus the console typecheck, lint and build when it touches the console. CI (`pipeline.yml`) is the full gate.

| Gate | Command |
|---|---|
| static | `.venv/bin/python -m ruff check .` · `.venv/bin/lint-imports` · `python3 scripts/check_docs.py` |
| unit | `PHOTO_WALL_TEST_REQUIRE_DATABASE=1 .venv/bin/python -m pytest -q -m "not db and not browser" -n 4 --dist worksteal <paths>` |
| db | `docker compose -f tests/integration/compose.test-database.yml up -d --wait`, then `.venv/bin/python scripts/test_local.py -q -m db -n 4 --dist loadgroup <paths>` |
| immich (after FX1) | `PHOTO_WALL_IMMICH_FIXTURE=<dir> .venv/bin/python scripts/test_local.py -q -m immich -n 4 --dist loadgroup <paths>` |
| browser | `PHOTO_WALL_BROWSER_TESTS=1 .venv/bin/python scripts/test_local.py -q tests/browser/<file> -n 4 --browser chromium` (built console) |
| console | `npm --prefix central/console run typecheck`, `run lint`, `run build` |

**Preflight at F0 (2026-10-11, this branch after the freeze).**
- Static: ruff 0; lint-imports 23 kept, 0 broken (one new contract: "The photo library domain is pure"); `check_docs` 0.
- Unit tier on the touched modules: 216 passed (`test_immich`, `test_source_query`, `test_library_tags`, `test_library_thumbnails`, `test_planner`, `test_media_queue`, `test_source_names`, `test_media_worker`, `test_media_repository`, `test_authored_media`, `test_console_sources`).
- DB tier on `test_media_worker`, `test_media_repository`, `test_library_tags`, `test_library_thumbnails`, `test_source_names`, `test_authored_media`, `test_planner`, `test_console_sources`: 113 passed.
- After the second F0 commit (`JobLease.asset` widened, the worker port's `connection_secret`, the store's constructor), unit and DB tiers together on `test_media_worker`, `test_media_repository`, `test_media_store`, `test_library_thumbnails`, `test_immich`, `test_planner`, `test_coordination`, `test_media_queue`: 342 passed.
- NOT RUN at F0, because F0 changes no behaviour: the whole unit tier, browser, console, node-bus, the software e2e, image builds.

**Budget gates.**
- Per slice: 90 min wall-clock, 120 for C2, M1, M2, K2 and I1; at most 8 agents.
- Feature: 14 h wall-clock and 9 M tokens; warning at 80 %; stop after the current slice at 100 %.
- Critical path: T0 → C1 → C2 → K1 → K2 → K3 → I1, about 11 h.
- At most two fix cycles per gate, then a pushed `wip/photo4-<slice>` branch and a stop.
- A course-correction architect pass after the fifth implementer and before the end review.
- Models pinned by id: `claude-opus-5-5` for every code slice; `claude-sonnet-5-5` allowed for X1 (owner, 2026-10-09).

**End (once, over BASE..HEAD):**
- CI watcher on the PR.
- Verifier: each page's mutation probes, and conformance to the pages.
- Review lenses in parallel:
  - **security**: a key stored in the database and written through the API, new operator routes, migrations 080 and 081, deletion of Sources' old data;
  - **domain**: the pool rules (add then sweep, exclusions, stacks, the keyset walk);
  - **console UX** (`.claude/skills/console-ux/SKILL.md`).
- One fix agent.
- The PR goes ready with before and after screenshots, dark and light, of Settings › Photo library, the Photo source editor and Scene editor › What.

---

## F0: the freeze (this commit; code architect)

Interfaces as compiling code; new bodies `NotImplementedError`; additive, so every existing consumer stays green.

| File | What is frozen |
|---|---|
| `central/photo_library/__init__.py` (new) | The context's docstring: pure domain |
| `central/photo_library/model.py` (new) | `Count`, `Kind`, `LibraryRef`, `TagRef`, `AlbumRef`, `PersonRef`, `ConnectionRef`, `SourceRef`, `DraftRef`, `PoolKey`, `AssetRevision`, `AssetId`; moved verbatim from `media/models.py`: `canonical_uuid`, `visible_text`, `TagText`, `LibraryTag`, `MAX_LIBRARY_TAGS`, `MAX_TAG_TEXT`, `asset_identity` (third argument renamed `revision`; an unedited photo's revision is its checksum, so its asset id is unchanged); `ListKind`, `LibraryAlbum`, `LibraryPerson`, `Place`; `MediaType`, `MAX_CHOSEN`, `TakenBetween`, `TakenLast`, `TakenRange`, `Exclusions`, `SourceQuery` (+ `canonical`, `pool_key`); `ImmichVersion`, `ImmichUser`, `LibraryCounts`; the problems (`KeyRejected`, `MissingPermissions`, `ImmichTooOld`, `Unreachable`, `UnexpectedAnswer`, `OtherUser`, `LibraryProblem`), notes (`UntestedVersion`, `NoFullSizeCopies`, `LibraryNote`), health (`NotChecked`, `HealthOk`, `NeedsALook`, `LibraryHealth`); `ContactAnswer`, `ContactRefused`, `ContactResult`, `ConnectionSecret`, `ContactTicket`; `LeftOutReason`, `SourceProblem`, `LibraryAsset`, `PoolMember`, `InPool`, `Waiting`, `LeftOut`, `PoolEntry`, `CheckMark`; check states (`NeverChecked`, `Checking`, `Complete`, `Incomplete`, `Failed`, `CheckState`); `PoolThumbnail`; `PoolCursor` (+ `encode`, `decode`) |
| `central/photo_library/policy.py` (new) | Real constants `REQUIRED_PERMISSIONS`, `OLDEST_VERSION`, `TESTED_RELEASE`, `CHECK_EVERY_SECONDS` = 900, `DRAFT_KEEP_SECONDS` = 86400, `POOL_PAGE_SIZE` = 60; `normalise_url`, `key_hint`, `version_verdict`, `contact_health` |
| `central/photo_library/views.py` (new) | `ConnectionWrite`, `ConnectionSaved`, `LibraryConnectionView`, `ListAnswer[Item]`, `DraftCreate`, `DraftCreated`, `DraftQueryWrite`, `DraftSave`, `DraftSaved`, `KindCounts`, `LeftOutCount`, `PoolItem`, `SourcePoolView` |
| `central/photo_library/ports.py` (new) | `LibraryRefused`; `PhotoLibraryQueries`, `PhotoLibraryCommands`, `PhotoSourceCommands`, `PhotoSourceQueries`; `JsonObject`, `KnownRevision`, `CheckLease`, `CheckStep`, `CheckComplete`, `CheckIncomplete`, `CheckFailed`, `CheckFinish`, `LibraryWorkerStore`; `ScenePoolReader` |
| `central/photo_library_routes.py` (new) | The route table (below) and `mount_photo_library_routes`, `mount_photo_source_routes` (not mounted until C1, C2) |
| `central/infra/photo_library_store.py` (new) | `PgPhotoLibrary.__init__(db, *, times, queue=None)` only |
| `central/catalog.py` | `SourcePool(source_ref, ready, candidates)` |
| `central/media_ports.py` | `CoordinationMedia.source_pools_in` |
| `central/media_queue.py` | `CHECK_CONNECTION_TASK`, `CHECK_POOL_TASK`, `LIBRARY_CONNECTION_LOCK_PREFIX`, `LIBRARY_POOL_LOCK_PREFIX`; `MediaTaskQueue.enqueue_connection_check_in`, `.enqueue_pool_check_in`; `ProcrastinateMediaQueue`'s two (bodies C1, C2) |
| `central/media_repository.py` | `JobLease.asset: OriginalAsset \| LibraryAsset` (declaration only; `claim_job` unchanged until P1) |
| `media/models.py` | Re-exports the moved names (until I1) |
| `media/library_client.py` (new) | `LibraryUnavailable`, `ChosenItemGone`, `TakenWindow`, `KeysetPosition`, `SearchItem`, `SearchPage`, `IdsPage`, `ExclusionKind`, `ExclusionQuery`, `TimeBucket`, `FetchedRendition`, `LibraryClient` |
| `tests/support/immich_library.py` (new) | `FixtureAsset`, `ImmichLibrary` (+ `new_key`, `other_user_key`, `upload`, `delete`, `unreachable_url`), `load_immich_library` (bodies FX1) |
| `pyproject.toml` | import-linter: `central.photo_library` joins the pure domains; new contract "The photo library domain is pure" |

### Route table (the console lane codes against it)

| Method | Path | Body | Answer | Refusals | Mounted by |
|---|---|---|---|---|---|
| GET | `/v1/operator/library/connections` | | `tuple[LibraryConnectionView, ...]` | | C1 |
| POST | `/v1/operator/library/connections` | `ConnectionWrite {name, url, key}` | 201 `ConnectionSaved` | 422 `invalid_url`, `key_required` | C1 |
| PUT | `/v1/operator/library/connections/{connection_ref}` | `ConnectionWrite {name, url, key?}` | `ConnectionSaved` | 404 `connection_unknown`; 422 `invalid_url` | C1 |
| DELETE | `/v1/operator/library/connections/{connection_ref}` | | 204 | 404 `connection_unknown`; 409 `sources_use_connection` | C1 |
| POST | `/v1/operator/library/connections/{connection_ref}/checks` | | 202 | 404 `connection_unknown` | C1 |
| GET | `/v1/operator/library/albums?connection=&q=&limit=` or `&ids=` (1-32, repeated) | | `ListAnswer[LibraryAlbum]` | 404 `connection_unknown`; 422 `ids_with_query` | C1 |
| GET | `/v1/operator/library/people?…` (as albums) | | `ListAnswer[LibraryPerson]` | as albums | C1 |
| GET | `/v1/operator/library/places?connection=&q=&limit=` | | `ListAnswer[Place]` | 404 `connection_unknown` | C1 |
| GET | `/v1/operator/library/tags?…` (`library_routes.py`, same shape as today) | | `ListAnswer[LibraryTag]` | as albums | C1 |
| POST | `/v1/operator/photo-drafts` | `DraftCreate {query, editing?}` | 201 `DraftCreated` | 404 `connection_unknown`, `source_unknown`; 422 | C2 |
| PUT | `/v1/operator/photo-drafts/{draft_ref}/query` | `DraftQueryWrite {query}` | 204 | 404 `draft_unknown`, `connection_unknown` | C2 |
| POST | `/v1/operator/photo-drafts/{draft_ref}/save` | `DraftSave {name}` | `DraftSaved` | 404 `draft_unknown`; 409 `source_name_exists`, `source_revision_conflict`; 422 `invalid_source_name` | C2 |
| GET | `/v1/operator/photo-drafts/{draft_ref}/pool?after=` | | `SourcePoolView` | 404 `draft_unknown`; 422 `cursor_invalid` | C2 |
| GET | `/v1/operator/sources/{source_ref}/pool?after=` | | `SourcePoolView` | 404 `source_unknown`; 422 `cursor_invalid` | C2 |
| POST | `/v1/operator/sources/{source_ref}/checks` | | 202 | 404 `source_unknown` | C2 |
| GET | `/v1/operator/library/thumbnails/{asset_id}` | | image (unchanged route) | servable = a row of a wanted pool | C2 |
| GET | `/v1/operator/media` | | each `sources[]` row gains `query` (the `SourceQuery`, or null for a pre-4 row not yet converted) and `check` (`CheckState`) | | C2 |
| POST | `/v1/operator/source-previews`, GET `/v1/operator/source-previews/{id}`, PUT `/v1/operator/sources/{ref}`, POST `/v1/operator/sources/{ref}/refresh`, PUT `/v1/operator/source-names/{name}` | | removed | | I1 |

Every route takes the operator `admin` dependency. Refusals answer `{"error": code}` at the status, as `apiWrite.js` reads. Query strings under `/v1/operator/library/` stay out of the access log (`LibraryQueryStrings`).

## Shared primitives (each with one responsibility; placed by the altitude tree)

| Primitive | Responsibility | Consumers now | Scheduled |
|---|---|---|---|
| `central/photo_library/model.py` | The context's vocabulary: values and their invariants | the store (C1, C2), the routes, the worker (M1-M3), the fixture accessor, P1 (via `SourcePool`) | 5a reads `PoolMember` facts through `Candidate` |
| `central/photo_library/policy.py` | Every rule with one home: required permissions, versions, check cadence, draft lifetime, URL normal form, key hint, health judgement | the store (`contact_health`), routes (`normalise_url`, `key_hint`), the adapter (`REQUIRED_PERMISSIONS`), the fixture (pinned equal by a test), the fake store | Settings #12 when it becomes editable |
| `central/photo_library/ports.py` | What depends on the store: one protocol per side | routes (C1, C2), the worker (M2, M3), coordination (P1) | 5a's Planner work |
| `media/library_client.py` | What the worker needs from Immich | `LibraryChecks` (M2), preparation and thumbnails (M3) | a second library kind, if ever: a second implementation |
| `media/pool_check.py` (M2) | The pure check fold: phases, keyset advance, per-photo entries, stack marks, relative dates in UTC | `LibraryChecks` | none |
| `central/infra/photo_library_store.py` | The one home of migration 080's tables and the only reader of the key | create_app (C1, C2), the worker (M2, M3), P1's `source_pools_in` (`pool_members_in`) | none |
| `tests/support/library_store_fake.py` (M2) | An in-memory `LibraryWorkerStore` | M2, M3 tests; I1's conformance suite holds it equal to the PostgreSQL store | none |
| `tests/support/immich_library.py` (FX1) | The real fixture as tests see it | M2, M3 named tests; I1 | none |

## Test architecture

- **Pure domain (T0, unit leg).** `tests/test_photo_library_model.py` and `tests/test_photo_library_policy.py` check every invariant, the canonical form, the pool key, the cursor and the health judgement through the public functions.
- **Store (C1, C2, db leg).** Driven through the protocols only, with the worker side fed by hand-made `ContactResult` and `CheckStep` values; no Immich. `tests/test_photo_library_store.py`, `tests/test_photo_pools.py`.
- **Routes (C1, C2, db leg).** FastAPI `TestClient` over `create_app` with a real database: `tests/test_photo_library_routes.py`, `tests/test_photo_source_routes.py`.
- **Adapter (M1, unit leg).** `httpx.MockTransport` serving answers shaped exactly as the v2.5.6 OpenAPI, including an in-memory Immich ordering model (newest first, any order between equal times) for the keyset walk: `tests/test_immich_library.py`.
- **Checks (M2, unit leg).** The pure fold alone (`tests/test_pool_check.py`), then `LibraryChecks` over a fake `LibraryClient` and the fake store (`tests/test_library_checks.py`).
- **Against real Immich (`immich` marker, CI `photo-sources` leg).**
  - M2 (real Immich, fake store): `tests/test_source_filters.py`, `tests/test_source_no_ceiling.py`.
  - M3 (real Immich): `tests/test_preparation_rendition.py`.
  - I1 (real Immich, real store, real worker process): `tests/test_immich_connection.py`, `tests/test_source_preview_matches_plan.py`.
- **Conformance boundaries.**
  - `LibraryWorkerStore`: I1's `tests/test_library_store_conformance.py` runs one behaviour suite against the fake and the PostgreSQL store.
  - `LibraryClient`: the fake in `tests/test_library_checks.py` follows M1's offline answers, and the real `ImmichClient` is proven on the fixture.
- **Console.**
  - Browser tests drive the real app and database, seeding pools through the store's worker side (no worker, no Immich): `test_photo_library_settings_browser.py` (K1), `test_source_flow_browser.py` (K2), `test_scene_flow_browser.py` (K3).
  - Stories go through the catalog leg (render, axe, screenshots).

## Maintenance risks

- **Immich drift.** Everything Immich-shaped lives in `media/immich.py`. A newer Immich that changes a field reads as `unexpected_answer` with the field's code, which the owner sees as Needs a look ("Check again; Details"), never as an empty pool (C7). The offline answers in `tests/test_immich_library.py` are the record of what 2.5.6 sends.
- **Full re-reads.** Every check re-reads every match (design Costs: about 100 requests per 100,000 photos every 15 minutes). This is acceptable now; the sync stream was rejected, and the reason is recorded in the design.
- **Pool sharing (F-2).** Retirement needs a correct "wanted" rule: a live draft, or a Source revision that Runtime or a name needs (`refresh_active`, already computed by `reconcile_source_activity_in`). I1's conformance suite pins it.
- **Old shapes in the meantime.** Old shapes live on between the lanes and I1, as additive paths: `OriginalAsset` leases, `catalog_snapshots`, `source_previews`, the old routes. I1 retires every one; the grep list is on its page.
- **The key in the database (C1).** A database backup holds the key (design Costs). No route reads it back, and the security lens checks every select of `api_key`.

---

## T0 (tracer, vocabulary): the photo library's rules (lane domain)

**Owned:** `central/photo_library/model.py`, `central/photo_library/policy.py`, `tests/test_photo_library_model.py`, `tests/test_photo_library_policy.py`.
**Design:** "TypeScript sketches" (the shapes), "Choices made tonight" C2, C3, C5, the parked note on health ("a contact number taken at start"), this ledger's F-2, F-11.

**Frozen page.** Every F0 signature in these two files exactly as frozen. Bodies are as their docstrings say, plus these validators (pydantic `field_validator` and `model_validator`; no new public name):
- `SourceQuery`:
  - `albums`, `people` and `tags` are refused when duplicated and stored sorted;
  - `media_types` is non-empty, refused when duplicated, and stored sorted;
  - `canonical()` = `json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False)`;
  - `pool_key()` = sha256 hex of `canonical().encode()`.
- `Exclusions`: lists unique and sorted, likewise.
- `TakenBetween`: `start < end` when both are set.
- `TakenLast`: `years + months > 0`.
- `Place`: `country` is non-empty after `visible_text`; an empty `city` or `state` becomes None.
- `LibraryAsset`: `duration` is set exactly for videos.
- `PoolCursor.encode()` = urlsafe base64 (no padding) of the canonical JSON `[taken_at, asset_id]`. `decode()` refuses with ValueError: bad base64, bad JSON, a non-finite or non-number time, or an asset id not matching `AssetId`.
- `policy.normalise_url`, `key_hint`, `version_verdict`, `contact_health` as their docstrings say. In `contact_health`, `checked_at` is the store's database time; the function never reads a clock.

**Acceptance (unit leg):**
- `tests/test_photo_library_model.py`:
  1. Two queries equal up to list order have one `pool_key`; a query differing in any one field (each field in turn) has another.
  2. `TakenLast(years=3)` keeps one key across days (no resolved date in the canonical form).
  3. Duplicated album, person or tag refs are refused; an empty `media_types` is refused.
  4. `TakenBetween(start=5, end=5)` is refused, and `TakenLast()` is refused.
  5. A `Place` whose country is only a bidi control is refused.
  6. A video `LibraryAsset` without `duration` is refused, and so is an image with one.
  7. A `PoolCursor` round-trips. "", "x", a cursor whose time is "NaN", and one with an asset id "asset-1" each raise ValueError.
  8. `asset_identity(c, u, sha1)` equals the pre-4 `media.models.asset_identity` value for the same three strings (a pinned hex).
- `tests/test_photo_library_policy.py`:
  1. `normalise_url`:
     - "http://immich.local:2283" → ".../api";
     - "https://h/api/" → "https://h/api";
     - "https://h/immich/" → "https://h/immich/api";
     - "ftp://h", "https://u:p@h", "https://h?x", "h", "" raise.
  2. `key_hint("abcd1234")` == "…1234".
  3. `version_verdict`:
     - 2.4.9 → `ImmichTooOld(found="2.4.9", oldest="2.5.0")`;
     - 2.5.0 and 2.5.6 → None;
     - 2.6.0 and 3.1.0 → `UntestedVersion`.
  4. `contact_health` precedence, case by case:
     - a refused contact is `needs_a_look`, whatever `bound_user` is;
     - `other_user` beats `immich_too_old`, which beats `missing_permissions`;
     - otherwise `ok`, with notes in the order untested version, then no full-size copies.
  5. `since` carries while the problem kind stays the same: two contacts `key_rejected` at 10 then 20 give since 10. A kind change (`key_rejected` at 10, then `unreachable` at 20) gives since 20. `ok` clears it.

**Mutation probes:**
- Drop the list sort → case 1 of the model test fails.
- Resolve `TakenLast` into dates in `canonical()` → case 2 fails.
- `since = checked_at` always → policy case 5 fails.
- Check `missing_permissions` before `other_user` → policy case 4 fails.

**Land:** `feat(photos): the photo library's query, pool and health rules (4 T0)`.

## C1 (tracer, Central half): connections, health and lists (lane central)

**Owned:**
- `central/migrations/080_photo_library.sql`
- `central/infra/photo_library_store.py`
- `central/photo_library_routes.py`
- `central/library_routes.py`
- `central/media_queue.py`
- `central/app.py`
- `tests/test_photo_library_store.py`
- `tests/test_photo_library_routes.py`
- `tests/test_library_tags.py` (only its route cases, moved onto the new store)
- `tests/test_media_queue.py`

**Design:** "Contracts" (Console, Operator API; Connection check rows), "Data flow" (first two rows), design rule 3, "TypeScript sketches" (`LibraryConnection`, `LibraryHealth`, `PhotoLibraryApi`), C1, C2, C7, C12, this ledger's F-9, F-11.

**Frozen page.**

Migration `080_photo_library.sql` (forward-only; no trigger, no stored procedure, no function). The columns are exactly these; CHECK constraints may be added, not removed:
```sql
CREATE TABLE library_connections (
  connection_ref TEXT PRIMARY KEY CHECK (connection_ref ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
  name TEXT NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 120),
  url TEXT NULL,                    -- policy.normalise_url's form; NULL: a cut-over row (C12)
  api_key TEXT NULL,                -- written by the API, read only by PgPhotoLibrary's worker side
  CHECK ((url IS NULL) = (api_key IS NULL)),
  immich_user JSONB NULL,           -- ImmichUser, bound by the first passing contact
  health JSONB NOT NULL DEFAULT '{"state":"not_checked"}',   -- LibraryHealth
  counts JSONB NULL,                -- LibraryCounts of the last passing contact
  last_contact BIGINT NOT NULL DEFAULT 0,      -- the last contact number handed out
  applied_contact BIGINT NOT NULL DEFAULT 0,   -- the last contact number applied (newest wins)
  contact_started_at DOUBLE PRECISION NULL,    -- database time of the last begin_contact
  created_at DOUBLE PRECISION NOT NULL);
CREATE TABLE library_lists (
  connection_ref TEXT NOT NULL REFERENCES library_connections ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('albums','people','places','tags')),
  status TEXT NOT NULL CHECK (status IN ('ok','unavailable','permission','incompatible')),
  error TEXT NULL CHECK (error IS NULL OR error ~ '^[a-z_]{1,64}$'),
  observed_at DOUBLE PRECISION NULL, checked_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (connection_ref, kind));
CREATE TABLE library_list_items (
  connection_ref TEXT NOT NULL, kind TEXT NOT NULL,
  item_ref TEXT NOT NULL,           -- album, person or tag id; a place: "country\u001fstate\u001fcity"
  search_text TEXT NOT NULL,        -- NFKC-casefolded name, path or place words
  item JSONB NOT NULL,              -- the served LibraryAlbum, LibraryPerson, Place or LibraryTag
  PRIMARY KEY (connection_ref, kind, item_ref),
  FOREIGN KEY (connection_ref, kind) REFERENCES library_lists ON DELETE CASCADE);
CREATE TABLE photo_pools (
  pool_key TEXT PRIMARY KEY CHECK (pool_key ~ '^[a-f0-9]{64}$'),
  connection_ref TEXT NOT NULL REFERENCES library_connections,
  query JSONB NOT NULL,             -- SourceQuery, canonical
  check_number BIGINT NOT NULL DEFAULT 0,
  check_state TEXT NOT NULL DEFAULT 'never_checked'
    CHECK (check_state IN ('never_checked','checking','complete','incomplete','failed')),
  check_started_at DOUBLE PRECISION NULL, check_finished_at DOUBLE PRECISION NULL,
  progress JSONB NULL, seen_so_far BIGINT NOT NULL DEFAULT 0,
  problem TEXT NULL CHECK (problem IN ('connection','chosen_item_gone')),
  incomplete_reason TEXT NULL CHECK (incomplete_reason IN ('over_1000_at_one_instant')),
  last_complete_at DOUBLE PRECISION NULL, immich_up_to BIGINT NULL,
  check_requested BOOLEAN NOT NULL DEFAULT TRUE,
  created_at DOUBLE PRECISION NOT NULL);
CREATE TABLE photo_pool_rows (
  pool_key TEXT NOT NULL REFERENCES photo_pools ON DELETE CASCADE,
  upstream_id UUID NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('in_pool','waiting','left_out')),
  reason TEXT NULL CHECK (reason IN ('unreadable_answer','over_item_limits')),
  asset_id TEXT NULL REFERENCES asset_revisions(asset_id),
  revision TEXT NULL, kind TEXT NULL CHECK (kind IN ('image','video')),
  taken_at DOUBLE PRECISION NULL, width INTEGER NULL, height INTEGER NULL,
  seen_check BIGINT NOT NULL,
  PRIMARY KEY (pool_key, upstream_id),
  CHECK ((outcome = 'in_pool') = (asset_id IS NOT NULL AND width IS NOT NULL AND height IS NOT NULL
                                  AND taken_at IS NOT NULL AND kind IS NOT NULL AND revision IS NOT NULL)),
  CHECK ((outcome = 'left_out') = (reason IS NOT NULL)));
CREATE INDEX photo_pool_rows_page ON photo_pool_rows (pool_key, taken_at DESC, asset_id DESC)
  WHERE outcome = 'in_pool';
CREATE TABLE photo_check_marks (
  pool_key TEXT NOT NULL REFERENCES photo_pools ON DELETE CASCADE,
  check_number BIGINT NOT NULL, upstream_id UUID NOT NULL,
  mark TEXT NOT NULL CHECK (mark IN ('excluded','stacked_copy')),
  PRIMARY KEY (pool_key, check_number, upstream_id, mark));
CREATE TABLE photo_drafts (
  draft_ref UUID PRIMARY KEY,
  pool_key TEXT NOT NULL REFERENCES photo_pools,
  editing_name TEXT NULL REFERENCES media_source_names(name),
  created_at DOUBLE PRECISION NOT NULL);
ALTER TABLE asset_revisions ADD COLUMN connection_ref TEXT NULL, ADD COLUMN upstream_id UUID NULL,
  ADD COLUMN updated_at TEXT NULL;   -- KnownRevision's source (NULL on pre-4 rows)
ALTER TABLE media_sources ALTER COLUMN spec DROP NOT NULL,
  ADD COLUMN query JSONB NULL, ADD COLUMN pool_key TEXT NULL REFERENCES photo_pools;
-- C12: one empty connection row per connection name in use (Sources' specs, the worker's last
-- reported ids, stored tag lists), name = ref. Then each spec becomes a `query`:
-- favorites true -> favourites_only true (false/null -> false: F-6); captured_from/until ->
-- {"kind":"between","start":..,"end":..} (both null -> null); media_types image -> photo +
-- live_photo, video -> video, sorted; tags kept; exclude {"albums":[],"people":[],"tags":[],
-- "screenshots":true}; albums, people [] ; place null; partners false. pool_key stays NULL:
-- C2's store assigns it in Python on first read (one canonical form: SourceQuery.pool_key).
```

`PgPhotoLibrary`, C1's half. These are the F0 protocol methods, bodies as their docstrings say:
- **Operator side.** `connections`, `albums`, `albums_by_id`, `people`, `people_by_id`, `places`, `tags`, `tags_by_id`; `add_connection`, `change_connection`, `remove_connection`, `check_connection`.
- **Worker side.** `due_contacts`, `begin_contact`, `connection_secret`, `record_contact`, `record_list`.

Rules for these methods:
- **Connection refs.** A new connection's ref is `"library-"` plus 12 lowercase hex (uuid4).
- **Matching.** `matching` (prefix first, then contains, over `search_text`) is done in SQL, ordered by `search_text, item_ref`; the 20-item limit is the route's (`limit` ≤ 20).
- **`remove_connection`** refuses while any `media_sources.query->>'connection_ref'` or a draft's pool names it.
- **`begin_contact`** sets `last_contact = last_contact + 1` and `contact_started_at = now` in one `UPDATE … RETURNING`.
- **`record_contact`** applies only `WHERE applied_contact < ticket.contact_number`; it binds `immich_user` only when it is NULL and the result is a `ContactAnswer` with no `missing_permissions`.
- **The key** is selected in exactly three methods: `begin_contact`, `connection_secret`, and C2's `begin_check`.

Queue and composition:
- `ProcrastinateMediaQueue.enqueue_connection_check_in`: as its docstring says, on `MEDIA_QUEUE`. The queueing lock coalesces a queued duplicate (`AlreadyEnqueued` → `QueueReceipt(coalesced=True)`), as `enqueue_refresh_in` does.
- `mount_photo_library_routes` as the route table says. `create_app` builds one `PgPhotoLibrary(db, times=DatabaseTransactionClock(), queue=ProcrastinateMediaQueue(dsn))`, mounts the routes, and answers `LibraryRefused` like `DisplayRefused`.
- `library_routes.library_tags` answers from `PgPhotoLibrary.tags`/`tags_by_id` in the `ListAnswer` shape (`items`, one shape for all four lists), plus a `tags` key carrying the same list, so today's console tag picker keeps working until K2 replaces it. I1 removes the `tags` key.

**Acceptance (db leg):**
- `tests/test_photo_library_routes.py`:
  1. POST a connection with url "http://immich.invalid:2283" and key "PUBLIC-FIXTURE-KEY-1234" → 201 with a `library-…` ref. GET lists it with url ".../api", `key_hint` "…1234", health `not_checked`, `required_permissions` == `REQUIRED_PERMISSIONS`.
  2. The key string appears in no route answer, no `procrastinate_jobs.args` row, and no log record captured during the requests. The queued job's args are exactly `{"connection_ref": ref}`.
  3. Refusals: POST without a key → 422 `key_required`; url "ftp://x" → 422 `invalid_url`; PUT and DELETE of an unknown ref → 404.
  4. PUT without a key keeps the old key (`connection_secret` reads it) and renames. PUT with a new key replaces it, and a check is queued each time. POST `/checks` queues one, and a second POST before it runs coalesces.
  5. DELETE refused 409 `sources_use_connection` while a `media_sources` row's query names it.
- `tests/test_photo_library_store.py`:
  6. Newest wins: tickets 1 and 2 are taken; 2's `ContactAnswer` is applied; then 1's `ContactRefused(KeyRejected)` → False and the health stays ok.
  7. The first passing contact binds `immich_user`. A later answer from another user → `needs_a_look` `other_user` (expected and found are the users' names), and counts are kept from the last passing contact.
  8. Needs a look appears with `key_rejected` (since t1), stays with since t1 at t2, and clears to ok at t3.
  9. `due_contacts` uses the database clock: a connection contacted 899 s ago is not due, 900 s is; a cut-over row (no key) is never due.
  10. `record_list`: a listing of 6,000 tags is stored and searchable, with no cap (owner 2026-10-09); a failure keeps the list and its `observed_at` and sets status and error. By-id reads name `absent` only from an `ok` list. Prefix matches come before contains matches. An item whose text has nothing visible is stored but never offered by a search.
  11. Migration 080 on a database seeded with three pre-4 Sources (connection "family-library"; favorites true; a captured window; `media_types` ["image"]):
     - one cut-over row "family-library" (url and key NULL, health `not_checked`);
     - each Source's `query` as the migration comment says;
     - `pool_key` NULL;
     - `spec` kept.
- `tests/test_media_queue.py`: `enqueue_connection_check_in` defers `CHECK_CONNECTION_TASK` with the lock and the queueing lock, and coalesces a duplicate.

**Mutation probes:**
- Order contacts by `contact_started_at` instead of the number → case 6 fails.
- Return `api_key` in `LibraryConnectionView` → case 2 fails.
- Bind the user on every passing contact → case 7 fails.
- Cap the stored list at 5,000 → case 10 fails.
- Map `favorites: false` to `favourites_only: true` → case 11 fails.

**Land:** `feat(photos): Immich connections in Central's database with a write-only key, health and lists (4 C1)`.

## C2: drafts, pools and the store half of a check (lane central)

**Owned:**
- `central/infra/photo_library_store.py`
- `central/photo_library_routes.py`
- `central/source_names.py`
- `central/media_queue.py`
- `central/app.py`
- `central/media_repository.py` (only `sources_in` and `media_read_in`)
- `tests/test_photo_pools.py`
- `tests/test_photo_source_routes.py`
- `tests/test_source_names.py`
- `tests/test_media_queue.py`

**Design:** design rules 1 and 2, "Contracts" (Source check, Planner rows), "Data flow" third table, C8, C9, C11, "TypeScript sketches" (`SourcePool`, `PoolPage`, `PhotoSourceApi`), this ledger's F-1, F-2, F-3.

**Frozen page.**

`PgPhotoLibrary`, C2's half. These are the F0 methods `due_pools`, `begin_check`, `known_revisions`, `record_step`, `finish_check`, `largest_frame_edge`, `pool_thumbnail`, `retire_unwanted`, `draft_pool`, `source_pool`, plus these additions:
- `create_draft(query, editing) -> DraftRef`, `set_draft_query(draft, query) -> None`, `check_source(source_ref) -> None` (`PhotoSourceCommands` minus `save_draft`).
- `pool_of_draft_in(conn, draft: DraftRef) -> PoolKey | None` (the read `SourceNameService.save_draft` uses).
- `pool_key_of_source_in(conn, source_ref: str) -> PoolKey | None`. It assigns and stores the pool key (creating the pool, check requested) for a pre-4 row whose `query` is set and `pool_key` NULL.
- `@staticmethod pool_members_in(conn, source_refs: set[str]) -> dict[str, tuple[tuple[PoolMember, ...], bool]]`. It reads each Source revision's in-pool rows (newest first) and whether its pool has a complete check (`last_complete_at` set). A ref with no query or no pool maps to `((), False)`. This is P1's read.
- `check_state_in(conn, source_ref: str) -> CheckState`, for `sources_in`.

`central/source_names.py`:
- `SourceNameService.__init__(self, db, repository, runtime, library: PgPhotoLibrary)` (the library store is injected) and `SourceNameService.save_draft(self, draft: DraftRef, name: str) -> dict`, which makes the next revision of the draft's `editing_name` (renamed to `name` when it differs), or revision 1 of `name`. It writes the new `media_sources` row with `query` and the draft's `pool_key` (`spec` NULL), revises Runtime's refs and reconciles activity as `put` does, deletes the draft, and answers `{"name", "revision", "source_ref", "created"}`. Refusals are `put`'s codes.
- `PhotoSources(store, names)` (new, in `central/photo_sources.py`, owned by C2) implements `PhotoSourceCommands` and `PhotoSourceQueries` by delegation. `create_app` mounts `mount_photo_source_routes(app, admin=…, queries=…, commands=…)`.

Rules:
- **Wanted.** A pool is wanted while a draft points at it, or while a `media_sources` row with `refresh_active` has its `pool_key`.
- **Checks only on wanted pools.** `begin_check` returns None for an unwanted pool. It starts `check_number + 1` with state `checking` when no check runs, and otherwise resumes the running one with its `progress`.
- **The sweep.** `finish_check(complete)` sets `last_complete_at` to now and deletes `photo_pool_rows WHERE seen_check < lease.check_number` and this pool's marks of older checks. `incomplete` and `failed` delete nothing and keep `last_complete_at`.
- **Queueing.** `record_step` queues the next step through `queue.enqueue_pool_check_in` in its own transaction. `create_draft`, `set_draft_query` and `check_source` set `check_requested` and queue one.
- **Retirement.** `retire_unwanted` deletes drafts older than `DRAFT_KEEP_SECONDS`, then unwanted pools, then library-thumbnail asset records whose identity is not an `in_pool` asset id of a wanted pool. It returns the thumbnail identities still recorded.
- **Pool pages.** `draft_pool` and `source_pool` answer `POOL_PAGE_SIZE` in-pool rows ordered `(taken_at DESC, asset_id DESC)` strictly after the cursor, with counts by outcome, `immich_up_to` and `check`. Each `thumbnail` is `/v1/operator/library/thumbnails/{asset_id}`.
- **Thumbnails.** `create_app`'s `servable_thumbnail` becomes `lambda asset_id: store.pool_thumbnail(asset_id) is not None` (seam d).
- **Sources list.** `MediaRepository.sources_in` rows gain `query` and `check` (through `check_state_in`); nothing else in `media_repository.py` changes in C2.

**Acceptance (db leg):**
- `tests/test_photo_pools.py`:
  1. Two drafts with equal queries share one pool. `set_draft_query` moves a draft to another pool and queues its check.
  2. `record_step` is fenced: a lease of check 2 after check 3 began → False, and nothing is written.
  3. A step's entries are upserted with `seen_check`, and an InPool's asset lands in `asset_revisions` with `connection_ref`, `upstream_id`, `updated_at`. Marks are stored, and an entry marked in this check (in the same step or an earlier one) is not added. The next step is queued in the same transaction (a failing enqueue rolls the rows back).
  4. A complete finish sweeps the rows not seen this check. An incomplete or failed finish sweeps nothing, and `last_complete_at` stays the last complete one.
  5. Pages:
     - 150 rows page 60, 60, 30, newest first;
     - a cursor taken on page 1 still continues after a newer row is added and an older one swept, with nothing duplicated or lost among the unchanged rows;
     - "garbage" → `ValueError` → 422 at the route.
  6. Counts by outcome. `waiting` counts Waiting rows; `left_out` groups by reason; `in_pool` splits images and videos.
  7. No ceiling: 25,000 in-pool rows written through 25 steps of 1,000 page and count, and `pool_members_in` returns all 25,000.
  8. `retire_unwanted`:
     - a draft created a day ago (database clock) and its pool go;
     - a pool shared with an active Source stays;
     - thumbnail records outside wanted pools go.
  9. `due_pools`:
     - a wanted pool finished 900 s ago is due, one finished 899 s ago is not;
     - a checking pool is never due;
     - `check_requested` makes a pool due at once.
  10. `pool_thumbnail` answers for a row of a wanted pool only.
  11. `pool_key_of_source_in` assigns the pre-4 row's key once, as `SourceQuery.pool_key` computes it.
- `tests/test_photo_source_routes.py`:
  12. POST a draft → 201, and the pool view shows `checking` once a lease begins.
  13. Save a new draft as "family" → `family:1`, carrying the draft's pool key.
  14. Save a draft editing `family:1` → `family:2`, and a Scene using `family:1` now names `family:2`.
  15. Save into an existing other name → 409 `source_name_exists`; an unknown draft → 404.
  16. GET `/sources/{ref}/pool` and POST `/checks` answer, with 404 for unknown refs.
  17. `/v1/operator/media` rows carry `query` and `check`.
  18. A thumbnail of a pool row is servable; one of no wanted pool is 404 `thumbnail_unknown`.

**Mutation probes:**
- Sweep on an incomplete finish → case 4 fails.
- Drop the check-number fence → case 2 fails.
- Let a marked entry in → case 3 fails.
- Order pages by `taken_at` alone → case 5 fails.
- Treat a shared pool as unwanted → case 8 fails.

**Land:** `feat(photos): drafts, pools keyed by their query, and the store half of a check (4 C2)`.

## P1: the Planner and preparation read the pools (lane planner)

**Owned:**
- `central/planner.py`
- `central/coordination.py`
- `central/media_repository.py`
- `central/media_ports.py`
- `tests/test_planner.py`
- `tests/test_coordination.py`
- `tests/test_authored_media.py`
- `tests/test_media_repository.py` (keeps `sample` and `stored_member` as they are)

**Design:** "Contracts" (Planner row), C7, C11, F1's caps, design rule 3; this ledger's F-1, F-10.

**Frozen page.**
- **`planner.project`.** Its signature becomes `project(runtime, now, *, bindings_by_player, source_pools: Mapping[str, SourcePool], authored_candidates, locked_assignments, horizon_seconds=300, limits=PlannerLimits())`, and `catalog_snapshots` is gone.
- **The builder's pools.** `_ProjectionBuilder` takes `source_pools`. `_pool` unions the contribution's Sources' candidates with one per `asset_id` (the first seen, Sources in the contribution's order), newest first, whether or not a pool is `ready` (C7).
- **Diagnostics.**
  - `source_missing` for a ref with no pool;
  - `source_not_ready` for a ref whose pool is not ready and has no candidates;
  - `source_empty` for a ready pool with no candidates;
  - `asset_preparation_failed` as today.
- **Limits.** `PlannerLimits.max_candidates` is deleted, and so is `_bound`'s candidate-count call. The authored asset reference count keeps its own bound (`max_acquisitions`-sized; rename it `max_authored_refs` = 10000).
- **`MediaRepository.source_pools_in(conn, source_refs)`** (`ScenePoolReader`) reads `PgPhotoLibrary.pool_members_in`, builds `Candidate(asset_id, kind, original_width=width, original_height=height, captured_at=taken_at)` per member, hydrates through `_hydrate_candidates`, and answers `SourcePool(source_ref, ready, candidates)`.
- **`catalog_in`** keeps only its authored half, renamed `authored_in(conn) -> dict[str, Candidate]`. The snapshot half is deleted, along with `_candidate_capacity`'s snapshot sum. `StoreLimits.max_authored_candidates` and its refusals (`authored_candidate_limit`, `metadata_capacity` for snapshots) go.
- **Coordination.** `advance` calls `source_pools_in(conn, runtime.planning_source_refs())` and `authored_in(conn)`. `CoordinationMedia` loses `catalog_in` and gains `authored_in`.
- **Authoring.** `author_candidates_in` reads the Source's in-pool rows through `pool_members_in` instead of `source_members`. The `source_not_fresh` status gate goes: pool rows are confirmed photos, C7.
- **`source_candidates`** reads the pool through `pool_members_in` (hydrated), and its 1,000 refusal goes (F-10).
- **`claim_job`** validates `asset_revisions.metadata` as `LibraryAsset` when the row has `upstream_id` (a 4 row), else as `OriginalAsset`. The `reserve` uses `asset.kind` either way.

**Acceptance:**
- `tests/test_planner.py` (unit):
  1. Two Sources sharing one asset give one candidate. The order is newest first, across Sources.
  2. A not-ready pool with candidates still assigns (C7). A not-ready empty pool diagnoses `source_not_ready`, and a ready empty pool `source_empty`.
  3. 50,000 candidates project, with no budget error.
  4. An asset only in the pool of a Source that the contribution does not name is never assigned.
- `tests/test_coordination.py` (db): a Scene whose contributions name two saved Sources (pools seeded through the store's worker side) assigns only assets of their pools, and requests acquisitions for them.
- `tests/test_authored_media.py` (db): authoring takes a pool member, and refuses a non-member 409 `authored_asset_not_member`.
- `tests/test_media_repository.py` (db):
  - `claim_job` for a pool asset yields a `JobLease` whose asset is a `LibraryAsset`;
  - for a pre-4 row, an `OriginalAsset`;
  - `source_pools_in` hydrates a ready variant.

**Mutation probes:**
- Skip pools that are not ready → case 2's C7 assertion fails.
- Keep `max_candidates` → case 3 fails.
- Union without dedup → case 1 fails.

**Land:** `feat(planner): the Planner reads each Source's whole pool; no candidate caps (4 P1)`.

## M1: the Immich adapter (lane media)

**Owned:**
- `media/immich.py`
- `tests/test_immich_library.py`
- `tests/test_immich.py` (only to keep its old-path cases green until I1)

**Design:** design rule 1 and 3, "Paging: designed twice" (chosen row), C2, C3, C4, C5, C9, C10; the parked notes "Keyset", "Stacks", "Revision", "Health", "Permissions"; this ledger's "Immich facts", F-5, F-12.

**Frozen page.**
- **Construction.** `ImmichClient` implements `media.library_client.LibraryClient` exactly as frozen. It is built by `ImmichClient.for_connection(secret: ConnectionSecret, *, limits: LibraryLimits = LibraryLimits(), clock: Clock | None = None, transport: httpx.AsyncBaseTransport | None = None) -> ImmichClient`.
- **`LibraryLimits(Model)`.** `page_size: int` (1-1000, default 1000), `step_seconds` = 60, `metadata_seconds` = 15, `connect_seconds` = 3, `read_seconds` = 10, `max_answer_bytes` = 16 MiB, `fetch_seconds` = 120, `max_fetch_bytes` = 256 MiB, `max_pixels` = 64_000_000, `max_dimension` = 16384, `max_video_seconds` = 300. These are per-request and per-step budgets, never a count of matches (F1).
- **The old constructor and methods stay until I1.** The old constructor (`ConnectionConfig`) and methods (`refresh`, `preview`, `list_tags`, `check_connection`, `download_original`) stay for their current callers. `thumbnail(upstream_id, revision)` changes in place to the frozen behaviour (edited, compared by revision). An unedited photo's revision is its checksum, so today's caller, which passes the checksum, keeps working.
- **`contact`.** It reads `server/version`, `users/me`, `api-keys/me` (missing = `REQUIRED_PERMISSIONS` − the key's permissions, in that order), then `assets/statistics` (photos = `images`, videos = `videos`), `albums/statistics` (albums = `owned + shared`; the fixture's count pins it) and `people?size=1` (people = `total` − `hidden`).
  - **Full-size copies.** Ask `assets/{id}/thumbnail?size=fullsize` of one non-JPEG asset, without following. A redirect to `size=preview`, or to a preview path, means off. No non-JPEG asset means None.
  - **Failures.** A 401 is `KeyRejected`; a connect or timeout error is `Unreachable`; a malformed answer is `UnexpectedAnswer(detail=<code>)`. A 403 on a statistics read means that permission is missing, and counts are None.
- **`search_page`.** One `search/metadata` body:
  - the query's albumIds, personIds, tagIds, city, state and country;
  - `takenAfter`/`takenBefore` from the window and the position;
  - `isFavorite: true` only when favourites only;
  - `type` per media type (a query of only videos → VIDEO; only images or live photos → IMAGE; both → no type);
  - `visibility: "timeline"`, `withStacked: true`, `withExif: false`, `size`, `order: "desc"`.

  Keyset as `KeysetPosition`'s docstring says. **Never `page` beyond 1.**
- **`SearchItem` parsing.** One row parsed per item:
  - `taken_at` from `fileCreatedAt`;
  - `width`/`height` top-level, swapped when the EXIF orientation is 5-8 (read `exifInfo` only through `withExif` on a re-read: if the search answer lacks orientation, `revision_now` supplies it before an InPool is formed; record which in E-P4-M1);
  - `live_photo` = `livePhotoVideoId` is set;
  - `stack_primary` from `stack.primaryAssetId`.

  A row that fails parsing → `problem="unreadable_answer"`; over `max_pixels`, `max_dimension` or `max_video_seconds` → `"over_item_limits"`.
- **`exclusion_page`.** Album, person or tag: one id each. `screenshot_name`: `originalFileName: "screenshot"`. `screenshot_png`: `type: IMAGE`, `originalFileName: ".png"`, `model: null`. No visibility filter.
- **Missing chosen items.** A 400 on a body naming album, person or tag ids raises `ChosenItemGone`.
- **`edits_fingerprint`.** The first 16 hex of the sha256 of the canonical JSON of `edits`. The revision is `checksum` or `checksum-<fingerprint>` (for `isEdited`).
- **`thumbnail`.** `size=thumbnail&edited=true`, re-encoded by `reencode_thumbnail`. It raises `MediaError("asset_changed")` when `revision_now` differs.
- **`fetch_rendition`.**
  - **Photos.** Fetch `thumbnail?size=preview&edited=true` and measure it with Pillow (header only). When its long edge ≥ `frame_long_edge`, keep it. Otherwise fetch `size=fullsize&edited=true`, following at most 3 redirects, same origin only (scheme, host, port); a cross-origin redirect is `MediaError("upstream_redirect")`. When full-size copies are off (the redirect lands on the preview), keep the preview.
  - **Videos.** `original` (unedited; Immich 2.5 does not edit videos).
  - Every fetch is bounded by `max_fetch_bytes`, exclusively created, and fsynced.
- **`up_to`.** `search/statistics` with the include body's filters → `total`.
- **Unchanged.** The thumbnail re-encode and the JSON streaming bounds stay as they are; the 1,000 ceiling exists only in the old methods.

**Acceptance (unit leg, `tests/test_immich_library.py`; MockTransport answers shaped as the v2.5.6 OpenAPI; the in-memory Immich model orders by `fileCreatedAt` desc and shuffles ties on every request):**
1. `contact`:
   - exact counts from the three statistics answers;
   - a key lacking `face.read` → missing `("face.read",)`;
   - 401 → `KeyRejected`; a refused connection → `Unreachable`; a body that is not JSON → `UnexpectedAnswer`;
   - the full-size probe both ways.
2. The search body for a query with every filter set equals a pinned JSON. A video-only query sends `type: VIDEO`; a query with photos and videos sends no type.
3. Page size 3, eight assets at one instant plus four at others: walking `search_page` to the end yields each id exactly once, with ties shuffled per request.
4. 1,001 assets at one instant with page size 1000 switch to `createdBefore`. With 1,001 at one taken and one created instant, `over_one_instant` is True.
5. One malformed row among ten → that row `unreadable_answer`, nine parsed. `width: null` → width None. 70 Mpx → `over_item_limits`.
6. Exclusion bodies:
   - each of the five kinds pins its body;
   - a screenshot named "Screenshot 2025-01-01.png" is in `screenshot_name`;
   - the PNG check sends `model: null`.
7. A 400 for an album id → `ChosenItemGone`.
8. `edits_fingerprint` is stable across key order, and differs for another crop.
9. `fetch_rendition`:
   - a 2048 px preview for a 1920 Frame → preview;
   - a 1440 px preview → full-size;
   - a same-origin redirect is followed, and a cross-origin one refused;
   - full-size off → preview;
   - a video → original;
   - an existing destination is refused.
10. No method of the new surface sends `page` > 1 or refuses for a count of matches.

**Mutation probes:**
- Use offset `page: 2` instead of keyset → case 3 loses a tie under the shuffle.
- Drop `edge_ids` dropping → case 3 duplicates.
- Follow a cross-origin redirect → case 9 fails.
- Treat a malformed row as a page error → case 5 fails.

**Land:** `feat(media): the Immich adapter reads one query by keyset pages, exclusions, stacks and edits (4 M1)`.

## M2 (tracer, worker half): the checks (lane media)

**Owned:**
- `media/pool_check.py` (new)
- `media/library_checks.py` (new)
- `media/task_queue.py`
- `media/worker.py`
- `tests/support/library_store_fake.py` (new)
- `tests/test_pool_check.py`
- `tests/test_library_checks.py`
- `tests/test_media_worker.py`
- `tests/test_source_filters.py`
- `tests/test_source_no_ceiling.py`

**Design:** design rules 1 and 2, "Contracts" (Connection check, Source check), "Data flow", C3, C4, C5, C7, C9; the parked notes "Keyset" ("Exclusions first, so nothing joins then leaves"), "Stacks", "Health"; this ledger's F-5.

**Frozen page.**

`media/pool_check.py` (pure; no I/O, no clock reads):
- `class CheckProgress(Model)` with these fields:
  - `phase: Literal["up_to", "exclusions", "include", "done"]`
  - `window: TakenWindow`
  - `exclusion_index: int`
  - `position: KeysetPosition | None`
  - `seen_so_far: int`
  - `incomplete: bool`

  It is stored as the lease's `progress` JSON.
- `def start(query: SourceQuery, now_utc: float) -> CheckProgress`: resolves `TakenLast` to a UTC window (calendar months back from `now_utc`).
- `def exclusions_of(query: SourceQuery) -> tuple[ExclusionQuery, ...]`: one per excluded album, person and tag; plus `screenshot_name` and `screenshot_png` when `exclude.screenshots`.
- `def entries_of(items: Sequence[SearchItem], *, query: SourceQuery, bound_user: str | None, connection_ref: str, known: Mapping[str, KnownRevision], fingerprints: Mapping[str, str]) -> tuple[tuple[PoolEntry, ...], tuple[CheckMark, ...]]`. Per item:
  - a stacked copy (`stack_primary` set and ≠ its id) → `CheckMark(stacked_copy)`;
  - a partner's photo when not `partners` → skipped;
  - a live photo or still not chosen (F-5) → skipped;
  - `problem` → `LeftOut`; no width → `Waiting`;
  - else `InPool(LibraryAsset)`. Its revision is from `known` when `updated_at` matches, else `checksum` plus `fingerprints[id]` for an edited one.
- `def advance(progress, page_or_ids_next, …) -> CheckProgress`: the next phase or position.

`media/library_checks.py`:
- `class LibraryChecks` with:
  - `__init__(self, store: LibraryWorkerStore, client_for: Callable[[ConnectionSecret], LibraryClient], clock: Clock)`
  - `async def contact(self, connection_ref: str) -> None`: `begin_contact` → `client.contact()` → `record_contact`; after a passing contact, list albums, people, places and tags → `record_list` each, an error per kind kept.
  - `async def check_pool(self, pool_key: str) -> None`: `begin_check`, then ONE bounded step (one Immich page or `up_to`), then `record_step` or `finish_check`. `LibraryUnavailable` → `finish_check(CheckFailed("connection"))`; `ChosenItemGone` → `CheckFailed("chosen_item_gone")`; an `over_one_instant` end → `CheckIncomplete`.
  - `async def tick(self) -> None`: queues `due_contacts` and `due_pools` through the media queue.
  - `async def close(self) -> None`
- **Clients.** They are cached per `(connection_ref, sha256(key))`, so a new key gets a new client.

`media/task_queue.py`:
- Tasks `CHECK_CONNECTION_TASK(connection_ref)` and `CHECK_POOL_TASK(pool_key)` on `MEDIA_QUEUE`, with `lock` = the prefix plus the ref or key (one step per pool at a time).
- The 30-second periodic task calls `LibraryChecks.tick` instead of `refresh_once` and `list_tags`.
- `refresh_media_source` and `preview_source` are removed.

`media/worker.py`:
- **Removed.** `MediaWorker.refresh_once`, `refresh_source`, `_refresh`, `preview_source`, `_prefetch`, `list_tags` and the tag boot step are removed; `_media_writer` no longer refreshes or lists.
- **Composition.** `_entry` builds `PgPhotoLibrary(db, times=DatabaseTransactionClock(), queue=ProcrastinateMediaQueue(dsn))` and `LibraryChecks(store, lambda secret: ImmichClient.for_connection(secret, clock=clock), clock)`, and puts it in the task context as `"library_checks"`.
- **The file loader stays until M3**, for preparation and thumbnails only.

`tests/support/library_store_fake.py`: `FakeLibraryStore` implements `LibraryWorkerStore` in memory. Its contact judgement is `policy.contact_health`, it fences on numbers, and it subtracts marks as `record_step` says. It also has seeding helpers `add_connection(ref, url, key)` and `add_pool(query) -> PoolKey`, and the reads `rows(pool_key)` and `marks(pool_key)`.

**Acceptance:**
- `tests/test_pool_check.py` (unit):
  1. "last 3 years" on 2026-10-11T00:00Z gives `after` = 2023-10-11T00:00Z.
  2. A query excluding two people and screenshots yields four `ExclusionQuery` values.
  3. `entries_of`:
     - a copy whose primary ≠ itself is marked;
     - a partner's photo is dropped when `partners` is False;
     - a known revision is reused when `updated_at` is equal;
     - an edited item gets `checksum-<fp>`;
     - a null-width item is Waiting.
- `tests/test_library_checks.py` (unit; fake client, fake store):
  4. A check visits up_to, the exclusion pages, then the include pages, one per call, and finishes complete. An excluded id seen in the include pages never becomes a row.
  5. `LibraryUnavailable` mid-check → failed `connection`, no row removed.
  6. A second complete check that misses a photo removes it at the finish, never before.
  7. A contact's answer lands, and the lists follow. A key change makes a new client.
- `tests/test_media_worker.py`: the refresh, preview and tag tests are deleted; the boot path runs maintenance and checks the worker in.
- Real Immich (`immich` marker; fake store; `LibraryChecks` with the real `ImmichClient`):
  - `tests/test_source_filters.py`:
    8. Each filter gives exactly the fixture's expected labels: album "Garden"; people "Anna" and "Ben" both; place Paris; dates; favourites; tag "Family" with nested tags; videos only.
    9. Each exclusion removes its labels: an album, a person, a tag, and screenshots (the named PNG and the camera-less PNG).
    10. Archived, hidden and locked labels are never in a pool.
    11. Partners' photos are left out, and appear with `partners`.
    12. The stack: the matching copy is not in the pool, and the primary (which does not match) is not either (C9).
  - `tests/test_source_no_ceiling.py`:
    13. At page size 3, the eight equal-time photos are all in the pool, exactly once.
    14. The pool = the include matches − exclusions − stacked copies − partners' photos.
    15. The test uploads a photo with its own tag and checks it joins. It then deletes the photo, and a complete check removes it.
    16. A check interrupted after two steps (stop calling) has removed nothing.

**Mutation probes:**
- Run exclusions after the include pages → case 4's "never becomes a row" fails.
- Sweep on failure → case 5 fails.
- Drop the partner filter → case 11 fails.
- Keep stacked copies → case 12 fails.

**Land:** `feat(media): connection checks and pool checks in bounded steps; the refresh and preview loops go (4 M2)`.

## M3: preparation from the photo Immich shows, edited thumbnails; the connections file goes (lane media)

**Owned:**
- `media/prepare.py`
- `media/worker.py`
- `media/library_thumbnails.py`
- `tests/test_prepare.py`
- `tests/test_library_thumbnails.py`
- `tests/test_media_worker.py`
- `tests/test_preparation_rendition.py`

**Design:** C10, R4, R5 (edits), design rule 3; the parked note "Revision … Size … Full-size copies … WebP previews".

**Frozen page.**

`media/prepare.py`:
- `PreparationRecipe.version: Literal[2] = 2` (every preparation is redone once).
- `async def prepare_rendition(self, asset: LibraryAsset, rendition: FetchedRendition, destination: Path) -> PreparedMedia`: it inspects the fetched file and accepts JPEG, PNG and WEBP. Orientation comes from the file's EXIF (a preview is upright; a JPEG full-size copy may carry EXIF). Size comes from the file, and integrity is `rendition.sha256` and `size`, never the original's SHA-1. It scales to the recipe's long edge as `prepare` does; videos go as today. `prepare` (old) stays until I1.

`media/worker.py`:
- **The connections file goes.** `load_connections`, `_ConnectionFile` and the `PHOTO_WALL_CONNECTIONS_FILE` read go, and so does `worker_status`'s `connection_ids` (it passes None).
- **`MediaWorker.__init__`.** It becomes `(self, repository, store, library: LibraryWorkerStore, *, preparer=None, client_factory: Callable[[ConnectionSecret], LibraryClient] | None = None, limits=WorkerLimits(), thumbnails=None, previews=None)`.
- **`process_job`.** A `LibraryAsset` lease: `library.connection_secret` → client → `fetch_rendition(asset, frame_long_edge=library.largest_frame_edge(), destination=paths.original)` → `preparer.prepare_rendition` → `store.publish`. An `OriginalAsset` lease fails permanently with `MediaTaskFailed("asset_superseded")` (a refusing default arm until I1 removes the type).
- **`maintain`** calls `library.retire_unwanted()` instead of `maintain_source_previews`.
- **`_entry`** wires `LibraryThumbnailOrigin(library.pool_thumbnail, library.connection_secret, lambda secret: ImmichClient.for_connection(secret, clock=clock))`.

`media/library_thumbnails.py`: `LibraryThumbnailOrigin.__init__(self, servable: Callable[[str], PoolThumbnail | None], secret_of: Callable[[str], ConnectionSecret | None], client_factory: Callable[[ConnectionSecret], ThumbnailClient])`. `ThumbnailClient.thumbnail(upstream_id, revision)`. A new key gives a new client.

**Acceptance:**
- `tests/test_prepare.py`:
  1. A WEBP preview prepares to a JPEG of the recipe's size.
  2. A JPEG full-size copy carrying EXIF orientation 6 prepares upright.
  3. A size mismatch with `rendition.size` → `asset_integrity`.
- `tests/test_media_worker.py`:
  4. A `LibraryAsset` lease fetches with the largest Frame's edge and publishes.
  5. An `OriginalAsset` lease → `asset_superseded`, not retried.
  6. No environment variable names a connections file.
- `tests/test_library_thumbnails.py`: the origin refuses an id `pool_thumbnail` does not answer, without calling Immich. It writes Photo Wall's own re-encoding, and a changed revision is terminal.
- `tests/test_preparation_rendition.py` (`immich` marker, real Immich):
  7. The fixture's edited photo is fetched edited (its upright size equals the crop), thumbnailed edited, and its revision carries the edit fingerprint.
  8. A Frame edge at or below the preview's long edge takes the preview. A larger edge takes the full-size copy, whose long edge is the original's.
  9. The fixture's video is fetched as the original and prepares to MP4 (ffmpeg).

**Mutation probes:**
- Fetch `edited=false` → case 7 fails.
- Always take the preview → case 8's full-size half fails.
- Check integrity by the original's SHA-1 → case 1 fails.

**Land:** `feat(media): preparation fetches the edited photo at preview or full size; edited thumbnails; no connections file (4 M3)`.

## FX1: the content set, the published fixture and the `photo-sources` CI leg (lane fixture)

**Owned:**
- `scripts/immich_actions.py`, `scripts/immich_runtime.py`, `scripts/immich_fixture.py`
- `tests/integration/compose.immich.yml`, `tests/integration/compose.immich-published.yml` (new)
- `tests/support/immich_library.py`, `tests/conftest.py`
- `.github/workflows/software-e2e.yml`, `.github/workflows/checks.yml`
- `tests/test_service_workflows.py`, `tests/test_appliance_workflow.py`, `tests/test_wall_demo.py` (its provenance test only), `tests/test_immich_fixture_retry.py`
- `tests/test_immich_fixture_content.py` (new)

**Design:** "What happens next" 4.1 (the fixture gains …), the named-test table, the parked note "One Immich start per CI leg"; this ledger's F-8, F-12, "Immich facts".

**Frozen page.**
- **The content set.** `immich_actions.initialize` uploads it with the admin session and records it in `/runtime/library.json`. Each item is listed as label: content; **(P)** marks a PNG.
  - **Owner and keys.** The owner is "Fixture Owner". The runtime key has exactly `central.photo_library.policy.REQUIRED_PERMISSIONS`; a test pins the equality without importing `central` into the harness bundle. The partner user "Fixture Partner" shares with the owner through `/partners`.
  - **Anna and Ben.** `anna-ben-1` .. `anna-ben-3` have faces of both "Anna" and "Ben" (manual faces through `POST /faces`, machine learning off), taken 2024–2025. `anna-only` and `ben-only`. `anna-ben-old` was taken 2019.
  - **Screenshots and scans.** `screenshot-anna-ben` **(P)**: named "Screenshot 2025-03-01.png", with Anna and Ben. `scan` **(P)**: "scan-1.png", no camera model.
  - **Hidden from the timeline.** `archived-anna-ben` (archive), `hidden-anna-ben` (hidden), `locked-anna-ben` (locked).
  - **The stack.** `stack-primary` (Ben only) and `stack-copy` (Anna and Ben), stacked through `POST /stacks` with `stack-primary` as primary.
  - **The edit.** `edited` has a crop through `PUT /assets/{id}/edits`, so its upright size differs from the original's.
  - **Partner and place.** `partner-anna-ben`, owned by the partner, with Anna and Ben. `paris-1` and `paris-2` have GPS 48.8566, 2.3522 (reverse geocoding on).
  - **Albums.** "Garden" holds `garden-1` and `garden-2`; "Holidays" holds `paris-1` and `anna-ben-1`.
  - **Tags and favourites.** Tags "Family" and "Family/Christmas" are on `anna-ben-2` and `xmas-1`. Favourites: `anna-ben-1` and `garden-1`.
  - **Equal times.** `same-instant-1` .. `-8` share one capture instant.
  - **Video.** `video-1` is a 2 s MP4, made by ffmpeg in the fixture image.

  `counts` are Immich's own statistics after setup, read through the runtime key, and recorded. Every label's expected pool membership per named-test query is a table in `tests/test_immich_fixture_content.py`, a unit test over `library.json`'s schema only (no Immich).
- **`--publish`** adds `compose.immich-published.yml`, which puts Immich on a non-internal network `published_net` with `ports: ["127.0.0.1::2283"]`. The topology check allows exactly that one binding on 127.0.0.1, and only under `--publish`. The run writes `<state>/published.json {"base_url": "http://127.0.0.1:<port>/api"}` and copies the admin session to `<state>/runtime/admin.json`. The state directory is 0700 and private, and holds no real secret.
- **The adapter checks are deleted.** `ADAPTER_ROLES` and `immich_runtime.verify_adapter` go (`verify` is removed from the role choices); their checks live in the named tests (F-8). The Player-probe boundary checks stay, in every run.
- **`tests/support/immich_library.py`** is implemented as frozen.
- **`tests/conftest.py`:**
  - a session fixture `immich_library` (`load_immich_library()`, which skips with "set PHOTO_WALL_IMMICH_FIXTURE");
  - the `immich` marker, derived from the fixture closure as `db` is;
  - `CI_SKIP_ALLOWLIST` gains "set PHOTO_WALL_IMMICH_FIXTURE", with the comment "software-e2e.yml photo-sources runs the named Immich tests".
- **`software-e2e.yml`.** The `photo-sources` job replaces `immich-adapter` (same `needs`, `ubuntu-24.04-arm`, timeout 20). Its steps:
  1. setup action;
  2. `sudo apt-get install -y --no-install-recommends ffmpeg`;
  3. the test database (`compose.test-database.yml`);
  4. the fixture: `run --keep --setup-only --publish --base-image "$WORKER_IMAGE"`;
  5. `PHOTO_WALL_IMMICH_FIXTURE=$RUNNER_TEMP/immich-fixture PHOTO_WALL_TEST_REQUIRE_DATABASE=1 .venv/bin/python scripts/test_local.py -q -m immich -n 4 --dist loadgroup`;
  6. sanitized evidence uploaded;
  7. cleanup always.
- **`checks.yml`.** The db leg runs `-m "db and not immich"`.

**Acceptance:**
- `tests/test_service_workflows.py` and `tests/test_appliance_workflow.py` pin the new job:
  - it exists;
  - it starts one fixture with `--publish`;
  - it runs `-m immich`;
  - the db leg excludes `immich`;
  - no job is named `immich-adapter`.
- `tests/test_immich_fixture_content.py` (unit): the expected-membership table covers every label, and `REQUIRED_PERMISSIONS` equals the fixture's key permissions constant.
- `tests/test_immich_fixture_retry.py` stays green.
- In CI, the `photo-sources` leg collects the named tests (they fail on the fixture lane's branch until M2, M3 and I1 land: judged after the merge).
- Local: one `run --keep --setup-only --publish` on this Mac, if Docker builds the image. Otherwise NOT RUN.

**Mutation probes:**
- Leave `--publish` off in the workflow → the workflow test fails.
- Give the key 3 permissions → the content test fails.

**Land:** `test(fixture): the photo-source content set, a published fixture and the photo-sources CI leg (4 FX1)`.

## FX2: the wall e2e and the images move off the connections file (lane fixture)

**Owned:**
- `scripts/demo_wall.py`, `scripts/service_base.py`
- `Dockerfile`, `compose.yaml`
- `tests/support/workers.py`, `tests/test_wall_demo.py`, `tests/test_service_base.py` (if it pins the file)

**Design:** "Reconcile with existing code" row 1; C7, C12; this ledger's seam (g).

**Frozen page.**
- **`Dockerfile`** loses the `connections.json` creation (lines 36-38) and `ENV PHOTO_WALL_CONNECTIONS_FILE` (line 130). `compose.yaml` loses the `connections` volume and its mount. `service_base.py` loses the file's assertion. `tests/support/workers.py` writes no connections file and sets no such variable.
- **`demo_wall.py`'s setup:**
  - It POSTs `/v1/operator/library/connections` with the fixture's internal URL and key, then waits until GET shows health `ok`.
  - It creates the demo Source with POST `/v1/operator/photo-drafts` (query: favourites only, images), then saves it as "demo" → `demo:1`.
  - The Scene's contributions name `demo:1`.
  - `PERMISSIONS` = the fixture's runtime permissions (`REQUIRED_PERMISSIONS`).
- **`demo_wall.py`'s upstream-faults segment:**
  - Every `refresh_status(x, "permission" | "unavailable" | "ok")` becomes the connection's health (`missing_permissions` | `unreachable` | `ok`) plus the Source's `check` (`failed` with `connection` | `complete`), through `/v1/operator/library/connections` and `/v1/operator/sources/demo:1/pool`.
  - The "refresh" role becomes POST `/sources/demo:1/checks`.
  - The segment asserts that every Player keeps presenting through the permission loss and the outage (C7).

**Acceptance:**
- `tests/test_wall_demo.py` (unit): the demo's setup requests are the API's (no file write) and the fault expectations use the new words.
- In CI, the software e2e's two segments pass after the merge (judged at the end).

**Mutation probe:** expect the pool to empty on permission loss → the e2e's C7 assertion fails.

**Land:** `test(e2e): the wall demo pastes its connection and builds its Source through the API (4 FX2)`.

## K1: Settings › Photo library and Needs a look (lane console)

**Owned:**
- `central/console/src/libraryApi.js`
- `central/console/src/settingsRoutes.jsx`
- `central/console/src/Shell.jsx`, `central/console/src/routes.js`, `central/console/src/routeSamples.json`
- `central/console/src/pages/photo-library-page.tsx`
- `central/console/src/domain/library-connection.tsx` (+ `.stories.tsx`), `central/console/src/domain/library-health-card.tsx` (+ `.stories.tsx`)
- `central/console/src/AttentionPage.jsx`, `central/console/src/AttentionList.jsx`
- `tests/browser/test_photo_library_settings_browser.py`, `tests/browser/test_console_shell_browser.py` (only the new section)
- `tests/browser/catalog-baselines/` (new stories)
- `central/console/dist/` (build output, not committed)

**Design:** `.claude/skills/console-ux/SKILL.md` (procedure); design language T8 Settings area and row A3; operator console design §11 #8-#12; design-draft "TypeScript sketches" (`LibraryConnection`, `LibraryHealth`, `LibraryProblem` with each problem's fix).

**Frozen page.**
- **`libraryApi.js`.** JSDoc types from `central/photo_library/views.py`; each call returns `{ok: true, …} | {ok: false, error}` through `apiWrite`, as `powerApi.js` does:
  - `listConnections()`, `addConnection({name, url, key})`, `changeConnection(ref, {name, url, key?})`, `removeConnection(ref)`, `checkConnection(ref)`;
  - `searchAlbums(ref, q)`, `albumsById(ref, ids)`, `searchPeople(ref, q)`, `peopleById(ref, ids)`, `searchPlaces(ref, q)`, `searchTags(ref, q)`, `tagsById(ref, ids)`.
- **The Settings section.** `settingsRoutes.jsx` is a new route table with one section, `photo-library` ("Photo library"). `Shell.jsx` GROUPS gains `{ name: "Settings", table: settingsRoutes }` after Fleet.
- **`photo-library-page.tsx`** (`PhotoLibraryPage(): JSX.Element`). It shows one `LibraryConnection` per connection and an "Add a connection" form (name, Immich address, API key). The key field is a password input. After Save it shows only "Key ending …1234" and offers "Paste a new key". The page also shows:
  - Immich's counts: "41,203 photos · 2,110 videos · 212 albums · 96 people";
  - the bound user;
  - each note: "Immich 2.7.0 is newer than Photo Wall has been tested with; it keeps working" and "HEIC and RAW photos play at preview size: turn on full-size copies in Immich";
  - a cut-over row (no address): "Paste the address and key of this Immich to start new photos again";
  - "Check now", and Remove (refused in plain words while Sources use it).
- **`library-health-card.tsx`** (`LibraryHealthCard(props: {connection: LibraryConnectionView}): JSX.Element`). It is a `ProblemCard`, for Needs attention and the page, with one fix per problem:
  - `key_rejected` "Immich rejected the API key" → Paste a new key;
  - `missing_permissions` "The key cannot …" → "Make a key with these permissions" (lists `missing`);
  - `immich_too_old` "Immich 2.4.1 is older than 2.5" → Update Immich;
  - `unreachable` "Photo Wall cannot reach Immich at <url>" → Check again;
  - `unexpected_answer` → Check again, with Details › (the code);
  - `other_user` "This key belongs to <found>; the connection belongs to <expected>" → Paste a key for <expected>.

  Every card says "Photos keep playing". `AttentionPage` lists one card per connection that needs a look.
- **Words.** The console never shows the words Immich API key value, connection_ref, contact, check number, pool key.

**Acceptance (CI browser; the real app and database; health seeded through `PgPhotoLibrary.record_contact`):**
- Add a connection, and the row shows "Key ending …1234". The key is not in the page's DOM or in any response the page received (Playwright response bodies).
- The seeded counts show, in the house's number words.
- Each of the six problems shows its sentence and its fix, and one card appears on Needs attention. A passing contact removes it.
- Remove is refused while a Source uses the connection.
- A cut-over row asks for the address and key.
- Typecheck, lint, and the catalog leg for the new stories.

**Mutation probes:**
- Echo the key in the row → the DOM assertion fails.
- Drop the Needs attention card → its case fails.

**Land:** `feat(console): Settings › Photo library: connect Immich with no file and no restart; Needs a look with the fix (4 K1)`.

## K2: the Photo source editor (lane console)

**Owned:**
- `central/console/src/photoSourcesApi.js`
- `central/console/src/useDraftPool.js`
- `central/console/src/pages/photo-source-page.tsx`
- `central/console/src/domain/source-query-editor.tsx` (+ `.stories.tsx`), `central/console/src/domain/source-pool-view.tsx` (+ `.stories.tsx`)
- `central/console/src/patterns/item-picker.tsx` (+ `.stories.tsx`)
- `central/console/src/SourcesRegion.jsx`, `central/console/src/SourcePicker.jsx`, `central/console/src/sourceWords.js`, `central/console/src/mediaHealth.js`
- `central/console/src/showRoutes.jsx` (only the sources section's render)
- Deleted when no console module imports them any more: `SourceFlow.jsx`, `SourceSteps.jsx`, `sourceFlowModel.js`, `sourcePreview.js`, `usePreview.js`, `TagCombobox.jsx`, `libraryTags.js`
- `tests/browser/test_source_flow_browser.py`, `tests/test_console_sources.py`, `tests/test_console_flow.py` (only source cases)
- `tests/browser/catalog-baselines/`
- `central/console/dist/`

**Design:** console-ux skill; operator console design §11 #13-#33 with C3 (#17 "all"), C5 (one fixed line for #29 and #30), C6 (#21, #24, #33 not shown); design-draft "TypeScript sketches" (`SourceQuery`, `SourcePool`, `CheckState`, `PoolPage`), C8.

**Frozen page.**
- **`photoSourcesApi.js`:** `createDraft(query, editing?)`, `setDraftQuery(draftRef, query)`, `saveDraft(draftRef, name)`, `draftPool(draftRef, after?)`, `sourcePool(sourceRef, after?)`, `checkSource(sourceRef)`.
- **`useDraftPool.js`** (`useDraftPool({draftRef, sourceRef}): {view, pages, loadMore(), restart(), error}`) polls the first page every 2 s while `check.state` is `checking`, then every 30 s. On `cursor_invalid` it restarts from the top.
- **`patterns/item-picker.tsx`** (`ItemPicker<T>(props: {label: string, search: (q: string) => Promise<…>, byId?: …, value: string[], onChange, render: (item: T) => string})`) is one combobox for albums, people, tags and places, replacing `TagCombobox`.
- **`domain/source-query-editor.tsx`** (`SourceQueryEditor(props: {connections, value: SourceQuery, onChange})`) has these parts:
  - **Connection (#14):** hidden when there is only one.
  - **Include:**
    - albums (#15) and people (#16), with "All of these must be in the photo" fixed (C3), plus "For any of them, add another Source to the Scene";
    - place (#18): city, state and country chosen together;
    - dates (#19) "Between" and "In the last" years and months (#20);
    - favourites only (#22), tags (#23), media types (#25);
    - partners' photos (#32, off).
  - **Exclude:** albums (#26), people (#27), tags (#28), screenshots (#31, on).
  - **The fixed line:** "Archived, hidden and locked photos are never included" (#29, #30, C5).
- **`domain/source-pool-view.tsx`** (`SourcePoolView(props: {view, pages, onLoadMore})`) shows:
  - "12,400 in the pool so far · up to 41,203 in Immich" while checking;
  - "2,301 photos · 14 videos · checked 21:04" when complete;
  - "Checked part of it: more than 1,000 photos share one moment" when incomplete;
  - "Immich could not be read: see Photo library" or "An album, person or tag in this Source was deleted in Immich" when failed;
  - Waiting: "Immich is still reading 37 photos"; left out by reason: "2 could not be read", "1 is too large";
  - the thumbnails grid, newest first, with "Show more".
- **`pages/photo-source-page.tsx`.** New Source and Edit both open a draft. Every change PUTs the query, and the pool view restarts. Name and Save work at any time. Saving an edit keeps the name (F-3). "Check now" is shown on a saved Source.
- **Sources list.** `SourcesRegion.jsx` lists Sources with their `check` (from `/v1/operator/media` rows) and opens the page.

**Acceptance (CI browser; the real app and database; pools seeded through `PgPhotoLibrary.begin_check`/`record_step`/`finish_check`):**
- Build "Anna and Ben, last 3 years, no screenshots": the draft POST carries both people, `taken {"kind":"last","years":3}` and `exclude.screenshots` true.
- While seeded steps land, the count rises without a reload.
- Save works mid-check, and the saved Source shows the same pool.
- Pages: 130 seeded rows show 60, then 120, then 130 with "Show more".
- Each check state's sentence shows.
- The fixed line is present, and no archived or hidden switch exists.
- Picking two albums shows "All of these must be in the photo".
- A people search finds a seeded person.
- Typecheck, lint, catalog.

**Mutation probes:**
- Send `taken` resolved to dates → the POST assertion fails.
- Show an Archived switch → its case fails.
- Page without the cursor → the 130 case duplicates.

**Land:** `feat(console): the Photo source editor: include, exclude, the pool as it fills, before and after saving (4 K2)`.

## K3: Scene editor › What: several Sources (lane console)

**Owned:**
- `central/console/src/SceneSteps.jsx`, `central/console/src/sceneFlowModel.js`, `central/console/src/SceneFlow.jsx`, `central/console/src/authoring.js` (only `source_refs` writing)
- `tests/browser/test_scene_flow_browser.py`, `tests/test_console_flow.py` (only scene cases)
- `central/console/dist/`

**Design:** operator console design §11 #36; C3 ("Any of them" is a second Source); design-draft "Data flow" (the Scene's pool is the union); this ledger's F-1.

**Frozen page.**
- The draft's `sourceRef: string` becomes `sourceRefs: string[]` (at least one).
- `PhotosStep` lists the chosen Sources with Remove and "Add another Source" (`SourcePicker` excluding the chosen ones), plus "New Source…", which opens K2's page and returns.
- Every media contribution the Scene writes carries `source_refs` = the chosen list, in order.
- The answer label reads "Photos from: Anna and Ben, Garden".
- Each chosen Source's readiness shows: not ready ("Still being read: what is found so far plays"), or empty ("This Source has no photos").

**Acceptance (CI browser):**
- A Scene with two Sources saves with both refs on every Frame's contribution, and reopens with both.
- Removing one leaves one.
- An empty Source's line shows.

**Mutation probe:** write only the first ref → the save case fails.

**Land:** `feat(console): Scene editor › What takes several Photo sources (4 K3)`.

## X1: docs (lane docs)

**Owned:**
- `docs/module-media.md`, `docs/module-media-worker.md`, `docs/module-immich-fixture.md`, `docs/module-planner.md`
- `docs/runbook.md` (connecting Immich in Settings; the connections file and its volume gone; the iac change listed as a follow-up PR the owner merges)
- `docs/operator-console-design.md`: rows #8-#33 and #36 statuses; #17 default "all"; #21 moves to 5a's Order; #24 and #33 deferred; #29 and #30 become one fixed line; #31 on; §11's Photo library and Photo source text
- `docs/roadmap.md`:
  - 4's status and Built and Not built;
  - A3, D1, D2 "Now";
  - the settings counts;
  - acceptance corrected: the test names landed, F-8's CI leg, and `test_source_preview_matches_plan`'s wording "every photo the draft shows prepares and is a Planner candidate";
  - no Developed row (F-7).
- `docs/requirements.md`: only Question 1's reading (exclusions are Immich queries subtracted by Photo Wall, C4), saying it is the owner's choice of 2026-10-11 to confirm.
- `docs/central-system-architecture.md` and `docs/architecture.md`: the photo library context, its store, and the worker's checks.
- `docs/decisions/0020-immich-key-in-central-database.md` (new: C1, with its costs), `docs/design-decisions.md` (index row)
- `docs/design-language.md` (row A3 only if words changed)
- `AGENTS.md` code map: `central/photo_library/`, `central/infra/photo_library_store.py`, `central/photo_library_routes.py`, `media/library_checks.py`, `media/pool_check.py`, `media/library_client.py`

Every rule has one home, and the others link to it. `python3 scripts/check_docs.py` must be green. The lane drafts from these pages at once, and its final pass after the code lanes reads what landed.

**Land:** `docs: real photo sources from Immich (4 X1)`.

## I1: integrate (lane integrate, last)

**Owned:** every file naming a retiring shape (the deletion's scope is every file naming the deleted symbol); `central/migrations/081_retire_snapshots.sql`; `tests/test_immich_connection.py`; `tests/test_source_preview_matches_plan.py`; `tests/test_library_store_conformance.py`.

**Frozen page (the tripwire):**
- **`media/models.py`:**
  - `SourceQuery`, `SourceSpec`, `SourcePreviewQuery`, `PreviewMember`, `StoredPreviewMember`, `SourcePreviewResult`, `SourcePreview`, `ConnectionConfig`, `MediaLimits`, `OriginalAsset`, `Diagnostic`, `RefreshCounts`, `RefreshResult`, `MAX_SOURCE_TAGS` and `PREVIEW_SHOWN` are deleted;
  - the re-exports go, and importers use `central.photo_library.model`;
  - what is left (`MediaError`, `DownloadedOriginal` if still used, `Kind`, `Status`) stays.
- **`media/immich.py`:** the old constructor and the old methods, `_walk`, `_head`, `_original` and `_SUPPORTED_VERSIONS` are deleted.
- **`central/media_repository.py`:**
  - `configure_source`, `request_refresh`, the source-preview methods, `servable_thumbnail`/`ServableThumbnail`, `begin_*_refresh`, `publish_refresh`, `refresh_catalog_in`, `_current_candidates`, the library-tag methods, `refresh_revisions`, `max_assets`/`metadata_capacity`, `worker_status`'s `connection_ids` are deleted;
  - `JobLease.asset: LibraryAsset`.
- **`central/catalog.py`:** `CatalogSnapshot` is deleted.
- **`central/app.py` and `central/source_names.py`:** the routes the route table marks "removed" go, and so do `NamedSourceWrite` and `SourceNameService.put` (a rename is a draft saved under a new name). `SourceNameService.delete` and DELETE `/v1/operator/source-names/{name}` stay.
- **`media/worker.py`:** the `OriginalAsset` arm is deleted.
- **Migration 081:**
  - DROP `catalog_snapshots`, `source_members`, `source_previews`, `library_tags`;
  - `ALTER TABLE media_sources DROP COLUMN spec, DROP COLUMN status, DROP COLUMN diagnostics, DROP COLUMN counts, DROP COLUMN next_refresh, DROP COLUMN last_success, DROP COLUMN generation, DROP COLUMN refresh_started, DROP COLUMN refresh_lease_until, DROP COLUMN refresh_requested_revision, DROP COLUMN refresh_completed_revision` (keep `source_ref`, `query`, `pool_key`, `refresh_active`);
  - `ALTER TABLE media_settings DROP COLUMN connection_ids`;
  - `DELETE FROM media_jobs` and `asset_revisions` rows whose metadata lacks `upstream_id` and that no `media_references` pin (pre-4 rows nobody plays); keep pinned ones.
- **Console:** nothing reads `spec`, `connection_ids`, `source-previews` or `limited`, and a grep shows it. The `tags` key of the tag answer goes (`items` only).
- **Grep proof.** The grep for each deleted symbol over `central media scripts tests contracts` is empty, except the migrations.

**Acceptance (the tracer and the seams):**
- `tests/test_library_store_conformance.py` (db): one parametrised suite over `FakeLibraryStore` and `PgPhotoLibrary`:
  - the contact fence, health, binding, `due_contacts`;
  - `begin_check`, `record_step` (fence, marks, subtraction), `finish_check` (sweep or not), `due_pools`, `pool_thumbnail`, `retire_unwanted`.
- `tests/test_immich_connection.py` (`immich` + db; a real `python -m media.worker` started BEFORE the connection exists, via `tests/support/workers.py`):
  1. POST the fixture's URL and key. Within 60 s, GET shows health ok and exactly `immich_library.counts` (no file, no restart).
  2. The key appears in no route answer, no job row, no worker log line, and no file under `central/console/dist`.
  3. Key cases:
     - a key without `face.read` (`new_key`) → Needs a look `missing_permissions ["face.read"]`;
     - a bad key → `key_rejected`;
     - the partner's key → `other_user`;
     - `unreachable_url()` → `unreachable`;
     - restoring the good key clears it.
  4. Newest wins under two Check now requests.
- `tests/test_source_preview_matches_plan.py` (`immich` + db; the real store, the real `LibraryChecks` and `ImmichClient`, the real coordination and preparation with ffmpeg):
  1. A draft "Anna and Ben, last 3 years, no screenshots" is checked to complete at page size 3. Its pool equals the fixture's expected labels.
  2. It is saved, and a Scene puts it beside a second Source ("Garden") on two Frames.
  3. `Coordination.advance` assigns only assets in the union of the two pools, and every pool photo eligible for a Frame's profile is a candidate.
  4. Each assigned asset's job claims, fetches, prepares and publishes.
  5. An edited photo's variant comes from the edited rendition.
- Seams (a) through (h) each have a named assertion in one of these three files or in a lane's tests. The PR body lists where.
- The whole unit, DB, node-bus, browser and photo-sources tiers, and the software e2e, are green in CI with the old shapes gone.

**Mutation probes:**
- Keep `catalog_snapshots` read in coordination → the grep proof fails.
- Restart the worker in case 1 → it is no longer the "no restart" proof (asserted by the worker's pid unchanged).
- Assign from a stale pool after a sweep → tracer case 3 fails.

**Land:** `refactor(photos): retire the snapshot, preview and connections-file shapes; the end-to-end photo pool tracer (4 I1)`.

## Outcome (filled by the run's closing step)

| Slice | Outcome | sha |
|---|---|---|
| F0 | landed | (the freeze commit and this ledger's commit) |
