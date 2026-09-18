# 0011 — Publishing the central and media-worker images to GHCR

**Date:** 2026-09-17
**Status:** **Accepted** — the release workflow publishes the `central` and
`media-worker` container images to GHCR at each release tag so an external
Kubernetes control plane can pull them. This directory (`docs/decisions/`) holds
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
[`.github/workflows/release.yml`](../../.github/workflows/release.yml) as two new
jobs gated on `needs.plan.outputs.should_release == 'true'` and keyed to
`needs.plan.outputs.tag`:

1. a `service-base` job that calls the existing
   [`service-base.yml`](../../.github/workflows/service-base.yml) reusable workflow
   (`architecture: amd64`) to get the media OS base image `media-worker` builds
   `FROM` — exactly how `checks.yml` consumes it;
2. a `service-images` job that checks out the released revision, logs into GHCR,
   sets up buildx, and pushes both targets with the **same pinned action SHAs and
   the same gha cache scope** `checks.yml` already uses, turning its `load: true`
   builds into `push: true` builds with the GHCR tags above.

Because both jobs depend only on the `plan` job's outputs, they fire on **both**
triggers `release.yml` already supports: a push to `main` that cuts a release, and
a `workflow_dispatch` of an existing tag. The dispatch path is how the owner
backfills images for the existing `v0.2.0` tag.

## Rationale

- **External hosts can't build from compose.** A pull-only consumer needs a
  published image; there is no lighter mechanism that satisfies it.
- **Reuse, not reinvention.** The `central`/`media-worker` targets, the media OS
  base image (`service-base.yml`), the GHCR login idiom, the pinned buildx /
  build-push action SHAs, and the amd64 build cache scope all already exist in
  `checks.yml`. This change only re-points the existing builds at `push: true`
  with release tags — no new or unpinned actions, matching the repo's
  pin-everything philosophy.
- **Immutable tags only.** Images are tagged `:vX.Y.Z` and nothing else — no
  `:latest` or other mutable tag. A given tag always resolves to the same bytes,
  matching the release cadence of the Player `.deb` (decision
  [0010](0010-github-release-sourcing.md)) and the same corruption-vs-authenticity
  posture settled in [0009](0009-minimal-base-and-app-package.md): a home LAN, no
  threat model, nothing signed.

## Scope and non-goals

- **amd64 only.** The control plane runs on x86 cluster nodes, not the arm Pis
  that netboot the Player, so only `linux/amd64` is published. Multi-arch
  (`arm64`) is **deferred** — `service-base.yml` already parameterizes
  architecture, so adding it later is a config change, not a redesign.
- **No mutable tags, no rollups.** Only the exact release tag is published.
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
