# 0011 — Publishing the central and media-worker images to GHCR

**Date:** 2026-09-17
**Status:** **Accepted** — the release workflow publishes the `central` and
`media-worker` container images to GHCR at each release tag so an external
Kubernetes control plane can pull them. *Amended 2026-09-27: pushed by digest,
tagged with the version by the seal (see below).* This directory (`docs/decisions/`) holds
accepted architecture decisions; this file is the single gate artifact for the
change.

## The problem in plain words

- Photo Wall's [`Dockerfile`](../../Dockerfile) already defines a `central`
  target (`FROM runtime`) and a `media-worker` target (`FROM ${MEDIA_BASE_IMAGE}`).
- Today those targets are only ever **built for local/CI compose**:
  [`.github/workflows/checks.yml`](../../.github/workflows/checks.yml) builds both
  with `load: true` and never pushes them anywhere.
- A separate deployment — our home Kubernetes cluster (the `iac` repo) — wants to
  run `central` and `media-worker` as pods. A Kubernetes node cannot `build:` a
  compose target the way a developer laptop or the CI runner does; it can only
  **pull a prebuilt image** from a registry.
- So the two targets must be published to a registry, under a stable, immutable
  identifier the cluster can pin.

## The decision

Publish `central` and `media-worker` to GHCR at every semver release, tagged with
the release tag:

- `ghcr.io/mcurcio/photo-wall/central:vX.Y.Z`
- `ghcr.io/mcurcio/photo-wall/media-worker:vX.Y.Z`

The publishing lives in
`.github/workflows/release.yml` (since renamed
[`pipeline.yml`](../../.github/workflows/pipeline.yml)), in three jobs that run on
the one release trigger, a push to `main` that cuts a release:

1. a `service-base` job that calls the existing
   [`service-base.yml`](../../.github/workflows/service-base.yml) reusable workflow
   (`architecture: amd64`) to get the media OS base image `media-worker` builds
   `FROM` — exactly how `checks.yml` consumes it;
2. an `images` job that checks out the released revision, logs into GHCR, sets up
   buildx, and pushes both targets **by digest** with the **same pinned action SHAs
   and the same gha cache scope** `checks.yml` already uses. Its only tag is
   `:sha-<revision>`, which claims no version; it reads the plan's revision, never
   its tag, and outputs each image as `<repository>@<digest>`;
3. the `seal` job ([`scripts/release_seal.py`](../../scripts/release_seal.py)), the
   only job that writes a version. It records both digests in the release's
   `manifest.json` (`images`), checks that every declared release asset is packaged
   and every digest resolves, and only then tags `:vX.Y.Z` from each digest with
   `docker buildx imagetools create` (a copy, no rebuild), reads the tag back, and
   publishes the GitHub Release.

So a published release always has its images, and `:vX.Y.Z` names exactly the
digests its release's manifest records: the seal re-reads both tags just before it
publishes, and refuses otherwise. `:vX.Y.Z` appears before its release by the time
the seal takes to attach the release assets.

Until the release is published, `:vX.Y.Z` belongs to no release, and **the newest
commit wins it**. If a seal fails after tagging, the next push to `main` recovers
with no hand step. Its seal runs at `main`'s tip, moves `:vX.Y.Z` to its own
digests, and publishes its own release from its own draft. The failed run's draft
is left in place, untouched and never deleted; drafts are invisible to Central. A
later re-run of the older seal refuses: its revision is no longer the tip, or the
version is already published at another commit. A seal also refuses when another
draft of the version is at a later commit than its own.

Once a release is published, its tags never move and its images are never
overwritten. A re-run of the seal that published it writes nothing and passes
when the published release and its tags are exactly that build; any other
published release is refused.

*Amended:* this decision first also published on a `workflow_dispatch` of an
existing tag, which backfilled images for `v0.2.0`. The pipeline has since removed
dispatch. There is no manual release or rebuild path: a release that fails is
recovered only by the normal flow, where the next push to `main` releases whatever
changed since the last published tag.

## Rationale

- **External hosts can't build from compose.** A pull-only consumer needs a
  published image; there is no lighter mechanism that satisfies it.
- **Reuse, not reinvention.** The `central`/`media-worker` targets, the media OS
  base image (`service-base.yml`), the GHCR login idiom, the pinned buildx /
  build-push action SHAs, and the amd64 build cache scope all already exist in
  `checks.yml`. This change only re-points the existing builds at `push: true`
  with release tags — no new or unpinned actions, matching the repo's
  pin-everything philosophy.
- **Immutable version tags only.** Images are tagged `:vX.Y.Z` by the seal and
  `:sha-<revision>` by the build — no `:latest`. A given version tag always
  resolves to the same bytes,
  matching the release cadence of the Player `.deb` (decision
  [0010](0010-github-release-sourcing.md)) and the same corruption-vs-authenticity
  posture settled in [0009](0009-minimal-base-and-app-package.md): a home LAN, no
  threat model, nothing signed.

## Scope and non-goals

- **amd64 only.** The control plane runs on x86 cluster nodes, not the arm Pis
  that netboot the Player, so only `linux/amd64` is published. Multi-arch
  (`arm64`) is **deferred** — `service-base.yml` already parameterizes
  architecture, so adding it later is a config change, not a redesign.
- **No mutable version tags, no rollups.** Only the exact release tag names a
  version. `:sha-<revision>` names the newest build of a revision; nothing reads it.
- **No image cleanup.** The `images` job runs only for a push to `main` that
  releases (never for a pull request), so `:sha-<revision>` accrues one manifest per
  image per release *attempt*. A published release's `:sha-<revision>` and
  `:vX.Y.Z` are the same manifest, so they cost nothing extra; an attempt that
  failed before publishing leaves one unreferenced manifest per image. Storage and
  transfer are free for public GHCR packages, and a cleanup could only delete
  package versions, which deletes every tag on a version, `:vX.Y.Z` included. If
  the packages ever go private (billed storage), clean only versions whose every
  tag is `sha-*`.
- **No signing / provenance / SBOM.** Consistent with 0009's home-LAN ruling.
- **This change does not deploy anything.** It only publishes images; the `iac`
  repo owns how the cluster consumes them.

## One-time operator step — make the packages public

GHCR creates a package **private by default** on first publish. After the first
release that publishes these images, the repository owner must set the visibility
of both GHCR packages — `central` and `media-worker` — to **public**, or every
external puller (the cluster nodes) needs an image pull secret with a token that
can read the package.

Do this once, per package, from each package's GitHub page
(**Package settings → Danger Zone → Change visibility → Public**). Subsequent
releases publish new tags into the already-public package and need no repeat step.
Until then, a cluster pull of `ghcr.io/mcurcio/photo-wall/central:vX.Y.Z` fails
with an unauthorized/not-found error unless a pull secret is configured.
