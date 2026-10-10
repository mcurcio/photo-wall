# CI for the Node's images and packages

The signed appliance image, its VM boot gate, the reusable OS base (`scripts/os_base.py`, `scripts/ci_images.py`, `appliance/os_definition.json`), the Player wheelhouse and the Player unit preflight were retired with the V1 pipeline ([0019](decisions/0019-debian-packaging-with-debhelper.md)); [decision 0007](decisions/0007-reusable-os-base.md) and the [boot gate](module-appliance-e2e.md) are history. What builds and tests the Node now:

| Workflow (called by `pipeline.yml`) | Does | Native arm64 |
|---|---|---|
| [`node-components.yml`](../.github/workflows/node-components.yml), job `debs` | Builds the `photo-wall` source package in the pinned build container, twice (the two `Packages` indexes must agree), proves the local repo with `tests/debs`, uploads it | yes |
| `node-components.yml`, job `build` | Builds the app and manager release roots from the pin and the local repo, seals them, writes the component set (`scripts/node_release_writer.py`) | yes |
| `node-components.yml`, job `pid1-fixture` | Derives the PID1 scenarios' stage targets and fixture image from the repo and the set | yes |
| [`base-image.yml`](../.github/workflows/base-image.yml) | rpi-image-gen builds the base squashfs installing `photo-wall-node` from the local repo; builds the scratch root with `photo-wall-netboot-init` and assembles the netboot bundle | yes |
| [`node-pid1.yml`](../.github/workflows/node-pid1.yml) | The Node lifecycle under real systemd, one leg per scenario, plus the `display-harness` leg | yes |
| [`software-e2e.yml`](../.github/workflows/software-e2e.yml) | Central, worker and Players on the wall scenario; the Players run from the component set's app root | yes |

The build steps, the cache keys and the pins are owned by the [Debian packaging module](module-debian-packaging.md). `scripts/release_plan.py` decides which of these a change needs; `node-components` is a build, not a test, and its consumers are `release_plan.BUILD_JOBS['node-components']`. Releases come only from `pipeline.yml`'s automated flow after merge. CI proves packages, images and systemd behaviour on a generic arm64 runner; it does not qualify Pi firmware, PXE, HDMI or timing.

## Shared service and test dependencies

[`pipeline.yml`](../.github/workflows/pipeline.yml) is the one workflow for
pull requests and pushes to `main`; its `gate` job is the one required check.
It has no manual release path: a release that fails is recovered only by the next
push to `main`, which releases whatever changed since the last published tag.
Its `plan` job runs `scripts/release_plan.py`, whose package manifest decides
which test workflows run and whether a push releases.
Only `base-image.yml` builds the Pi base OS, the netboot bundle and both
`.deb`s: the pipeline calls it for the plan's revision. On a release, the
`images` job pushes the service images by digest, and the `seal` job
(`scripts/release_seal.py`), the only one that writes a version, packages the
uploaded build output and those digests (via `scripts/package_release_artifacts.py`)
into the release [`contracts/release.py`](../contracts/release.py) declares. It
publishes the GitHub Release only once every declared asset is attached to its
draft and every image is tagged with the version. The
[`service-base.yml`](../.github/workflows/service-base.yml) reusable workflow
provides a separate retained FFmpeg environment for `checks.yml`,
`software-e2e.yml` and the release's media worker image. Both image-building checks
jobs (`image-smoke`, `linux-media`) share the AMD64 result; every software E2E job
shares the ARM64 definition and builds its images through one shared setup action
([`software-e2e-setup`](../.github/actions/software-e2e-setup/action.yml)).

`scripts/service_base.py` reads the `media-os` recipe prefix ending at
`# END MEDIA OS DEFINITION` in the root Dockerfile. The recipe and architecture
determine the identity; application source, `pyproject.toml`, and `uv.lock` are
downstream inputs. The helper uses the same strict registry operations as
`scripts/ci_images.py`: resolve the definition tag, consume its digest, and
permit construction only when the definition changed or preparation was
explicitly requested. Registry errors do not authorize an APT fallback.
Consumers pass that digest as `MEDIA_BASE_IMAGE` when building `media-worker`
or `media-test`, so rebuilding application layers does not reconstruct FFmpeg.

The small reusable preparation job serializes all callers by architecture,
with `cancel-in-progress: false` and `queue: max`. It rechecks the registry
after entering the queue, allowing later callers to reuse the first successful
publication. [GitHub's extended concurrency queue](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#concurrency)
keeps waiting callers from replacing each other. Same-repository jobs may
publish; fork jobs require an already published definition because a local
Docker image cannot cross job runners. A missing fork definition fails clearly
until a trusted run prepares it.

Central's target contains no APT calls. The Immich and VM helpers inherit the
already selected service images, and simulated Player helpers install local
wheels; those derived builds do not rebuild an OS. The standalone
`tests/native/Dockerfile` remains an explicit local cold integration fixture,
not a GHA dependency or substitute for exact-appliance qualification. Browser
checks use a prebuilt Playwright environment as described in the
[runbook](runbook.md#tests-and-local-development).

The first introduction of a definition permits preparation. An unchanged
definition with no retained artifact fails until a manual dispatch supplies
`prepare_base=true`; that input is available on all three caller workflows.
These routes describe the implemented dependency contract, not measured hosted
warm-build performance or completed qualification.

The [initial OS publication failure](evidence/2026-09-08-os-base-publication.md)
records the packaged test-key fixture correction and distinguishes it from
the successful shared-media and browser workflow checks.

## Docker Hub pulls

Hosted runners share addresses, so anonymous Docker Hub pulls hit its per-address
limit (`toomanyrequests`). Every CI job that pulls, runs or builds a Docker Hub
image first runs the
[`docker-hub-mirror`](../.github/actions/docker-hub-mirror/action.yml) action,
the one owner of the route: it adds Google's pull-through mirror,
`mirror.gcr.io`, to the Docker daemon's `registry-mirrors` (pulls, `docker run`,
Compose and the default builder) and outputs the same mirror as BuildKit
configuration and BuildKit's own image (`moby/buildkit`) on the mirror, which
every `docker/setup-buildx-action` passes as `buildkitd-config-inline` and
`driver-opts: image=…`, since a docker-container builder ignores the daemon's
mirrors and its own image is pulled before that configuration applies. Because
the daemon falls back to Docker Hub silently, the action drops the runner's own
Docker Hub login from the Docker client configuration (the daemon presents it to
the mirror, which rejects it with `unauthorized: authentication failed`; the
mirror serves anonymous pulls), then proves the route:
it pulls one digest-pinned Docker Hub image with the daemon's debug log on and
fails unless that log shows the mirror served it. The mirror applies only to `docker.io` references, serves the pinned
digests unchanged and falls back to Docker Hub on a miss; Dockerfiles and
Compose files keep their Docker Hub references, so nothing outside CI depends on
it. Other registries (`ghcr.io`, `mcr.microsoft.com`) are pulled directly, and
scripts pull through [`registry_pull.py`](../scripts/registry_pull.py)'s retry.
`tests/test_service_workflows.py` fails any job with steps that does not run the
mirror before its first Docker step, unless the test lists it as pulling nothing
from Docker Hub, and any builder set up without the mirror or its BuildKit image.

## Service image builds

The root Dockerfile's application stages and the
[`service-image`](../.github/actions/service-image/action.yml) action are the
one policy for building `central`, `media-worker` and `media-test` in every
workflow.

**Layers.** Each target stacks, from least to most often changed: its base (the
pinned Python image for `central`, the retained media OS for the worker), the
locked third-party environment (`deps`: `pyproject.toml` and `uv.lock`, without
the project), the application source (`source`: the Python packages and the
console bundle from `console-builder`), and the project's own editable install,
a `.pth` naming `/app`. Every application layer is copied with
[`COPY --link`](https://docs.docker.com/reference/dockerfile/#copy---link) from
the one stage that owns it, so its cache key is its own content. A source edit
rebuilds only the source and project-install layers; the worker no longer
re-ships its third-party environment with each code change; a new
`MEDIA_BASE_IMAGE` rebases the worker's dependency and source layers rather than
rebuilding them, and only its project-install and cache-directory steps re-run.
`media-test` layers the `dev-deps` environment (the same lock with the test
group, a superset of `deps`) over the worker, so a code change does not
reinstall test dependencies. uv is mounted only while it runs, and its download
cache, like npm's, lives in a BuildKit cache mount, so neither ships in an
image. The project install stays after the source: hatchling writes the
editable path only when the packages exist, and making it source-independent
would change `pyproject.toml`, an input of every released package and of the
base squashfs cache key.

**Cache scopes.** The [`buildkit-cache`](../.github/actions/buildkit-cache/action.yml)
action is the one scope policy. Each target reads and writes (`mode=max`) one GHA scope per
architecture, its own: `photo-wall-<target>-<architecture>-v<epoch>`. An export
replaces its scope's index, so the former shared scopes
(`photo-wall-checks-amd64-v1`, `photo-wall-software-e2e-arm64-v1`) kept only
their last writer's layers, and AMD64 never exported `central`. A build does not
also import its siblings' scopes: with several imports, BuildKit matched the
shared `deps` parent in a sibling's index and then missed the target's own
`COPY --link` layers, depending on import order. Builds in one job still share
layers through the job's builder. Jobs of one run that build the same target
write identical content. Raising the action's `CACHE_EPOCH` discards every
service cache at once. `linux-media` reads its scope on every run and writes it
only from `main`, whose scope every pull request can read.

**Node components.** [`node-components.yml`](../.github/workflows/node-components.yml)
builds the node component set once per pipeline run, for `base-image`, the wall e2e and
every `node-pid1` leg, which download it (and the PID1 fixture image, `docker save`d).
The BuildKit scopes use the same policy with one scope per container (`photo-wall-node-<role>-arm64-v<epoch>`:
the build container and the roots container), written from `main` only. The cache changes no byte:
packages carry content-derived versions, and a release root's digest is the digest of an image
built from the pin and the repo's `Package=Version` lines.

**Release roots cache.** The roots are cached whole (`actions/cache`, exact key `node-roots-v1-<digest>`, written only
from `main`, like `linux-media`'s). The key is `scripts/node_release_writer.py key`: the recipe (the
build and seal scripts and the first-party modules they import), the ABI pair and, per root
package, the `Package=Version` lines `debian-packaging/build-root.sh --resolve` prints. A
miss builds each root twice and requires equal digests; the writer and its proofs run on every
run, so the set's `.deb`s and ABI are always that run's. The base squashfs cache is keyed on the
layers, the pin and the `Package=Version` lines of the packages `photo-wall-node` pulls from the
local repo, and the ABI check refuses a hit whose installed ABI differs from the set's.
After a build the set is stamped with its revision (`revision.json`); the seal
([`node_release_artifacts.py`](../scripts/node_release_artifacts.py)) refuses a set without
that stamp for its revision.

**Limits.** A workflow run restores only caches of its own ref, its pull
request's base branch and the default branch
([GitHub cache access](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#restrictions-for-accessing-a-cache)).
The push to `main` after a merge therefore cannot read the pull request's
caches and rebuilds every layer the pull request changed. The same rule keeps
pull request caches out of release builds; a registry cache shared with pull
requests would trade that boundary for merge-run hits. A fully cached `--load`
still transfers every layer of the image into the runner's Docker, so the
retained media OS dominates worker build time on any hit. The build summary's step-count
percentage reports a `--link` step's zero-cost merge as uncached; the
[service image cache evidence](evidence/2026-09-28-service-image-cache.md)
records durations and transferred bytes instead.
