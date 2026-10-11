# 0020 — The Immich key is kept in Central's database

**Date:** 2026-10-11 · **Layer:** module contracts and data flow (where an Immich connection and its key live, and who reads the key) · **Status:** built (pull request [#81](https://github.com/mcurcio/photo-wall/pull/81), roadmap delivery [4](../roadmap.md#4-real-photo-sources)). **Decision owner:** the owner. This is choice C1 of delivery 4's design, taken as recommended in an overnight build with no owner gate (owner, 2026-10-11: "That all sounds correct. I’m hoping for one clean overnight build —plan the whole batch"); it is a current choice he may reopen, not his ruling. Relates to D06 and D11 in [design decisions](../design-decisions.md).

## Problem

Before delivery 4 the media worker read Immich connections, keys included, from a private JSON file in its own volume. A new key meant writing that file by hand, with the right owner and mode, and restarting the worker. Roadmap 4 asks for the opposite: "connect Immich in Settings with no file and no restart" (step A3, settings #8–#11). The key has to get from the console to the one process that talks to Immich, and live somewhere both can reach, without ever reaching a Player or the browser again.

## Requirements (bind)

| Requirement | Source |
|---|---|
| Connect Immich in Settings with no file and no restart; the real counts; Immich health over time as Needs a look with the fix | [roadmap 4](../roadmap.md#4-real-photo-sources) |
| Players are Immich-unaware; Central owns every Immich interaction; only the media worker calls Immich | [requirements › central media boundary](../requirements.md#central-media-boundary); owner, 2026-09-28 |
| Maximum UX; a security finding matters only if it is normal-case or does not recover by itself | owner, 2026-10-09 |
| No backwards compatibility; breaking changes are "trivially cheap" | owner, 2026-10-11 |

## Decision

- A connection is a row in Central's PostgreSQL database (`library_connections`, migration 080): name, URL, key, the Immich user it belongs to, health, counts.
- **The key is write-only.** The operator API takes it on add and on change (`PUT` without a key keeps the old one); no answer, view, list or log carries it; the console shows only its last four characters ("Key ending …1234").
- **One reader.** Only the photo library store's worker side selects the key (`begin_contact`, `connection_secret`, `begin_check` in `central/infra/photo_library_store.py`), and only the media worker calls those. Jobs carry a connection ref or a pool key, never the key. The worker holds it in process memory and its client, and caches clients per connection and key, so a new key is used at the next task with no restart.
- **A connection belongs to the Immich user of its first passing check.** A later key of another user is a problem ("This key belongs to …"), not a silent switch of library.
- The connections file, its volume, `PHOTO_WALL_CONNECTIONS_FILE` and the file's private-CA option are deleted. Each connection name in use became an empty row (C12), so existing Sources keep pointing at it until the key is pasted once.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Keep the private file | A new key needs a file edit and a restart: against roadmap 4's "no file and no restart" |
| A secret store (1Password `op run`, Kubernetes Secrets) holding the key, the database holding a reference | Still needs a deploy step or a restart to add or change a key, and a second system for a single-house appliance; the console could not paste a key |
| Encrypt the key in the database with a key from the environment | Adds a second secret to provision and rotate, and protects only against a database copy taken without the environment; the worker must still decrypt it to use it. Recorded as a later hardening if the owner wants it |

Prior art: Grafana's data-source secure fields (written through the API, stored server-side, never returned) and Home Assistant's integration credentials (entered in the UI, kept in its own storage).

## Costs

- **A database backup holds the key.** Anyone with a backup or database access can read it. Backups must be treated as secret.
- **A revoked key does not stop the wall.** The last pool keeps playing while the connection needs a look (C7); removing the connection is what stops new photos from it.
- **The cut-over.** After deploy, no new photos play until the owner pastes each Immich's address and key once; Frames keep their last photo.
- **Plain `http` is accepted** (UX over security): the key crosses the network in clear text when the owner pastes an `http` address. A private certificate authority must be in the worker's system trust store.

## Evidence

The named tests of delivery 4: `tests/test_photo_library_routes.py` (the key in no route answer, no queued job's arguments, no captured log record; a change without a key keeps it), `tests/test_immich_connection.py` (against real Immich with a worker started before the connection exists: counts with no restart; the key in no answer, job row, worker log or console build file; the key problems in plain words), and `tests/browser/test_photo_library_settings_browser.py` (the key in neither the page nor any response the page received). How it works is the [media module](../module-media.md#connections-and-health) and the runbook's [connecting Immich](../runbook.md#connecting-immich).
