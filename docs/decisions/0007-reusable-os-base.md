# 0007 — Reusable OS base and offline appliance assembly

Status: accepted design; qualification is recorded separately in the CI evidence.

## Problem

Application changes currently reconstruct the native Ubuntu package environment.
The optional archive cache cannot remove that dependency: package indexes must
be fetched before cached `.deb` files can be admitted. Snapshot HTTP failures can
therefore prevent unrelated Player changes from reaching boot tests.

## Decision

GitHub Actions produces three independent inputs: an image-builder container, a
prepared Ubuntu/Pi OS base, and the current Player wheelhouse. Final assembly
consumes these inputs without network access, adds current configuration and
boot logic, regenerates boot artifacts, signs the image, and runs the existing
generic-VM acceptance gate.

The prepared base stops immediately after authenticated native package
installation. It retains the matching Pi boot files, kernel modules, firmware,
package inventory, signed indexes, and package archives. It contains no Photo
Wall application, deployment configuration, fleet credential, or release key.
Filesystem ownership, modes, links, xattrs, ACLs, and required device nodes must
survive publication and restoration.

The builder and OS base are retained in GitHub Container Registry. Definition
tags are lookup keys; consumers resolve and record immutable digests. Ordinary
GHA caches may accelerate transport but are never the authoritative artifact.
Missing or mismatched prepared inputs fail explicitly instead of falling back
to APT during application assembly.

The OS definition owns the Ubuntu input, architecture, dated snapshot, native
package list, and OS preparation recipe. Its identity excludes application
source, Python dependency changes, deployment configuration, and final image
assembly. Builder tooling has an independent lock so an application lock edit
does not rebuild the builder. Changes to relevant preparation tooling remain
part of the dependency identity.

An always-running routing job determines which definitions changed. A changed
definition or explicit preparation request permits constructing a missing
dependency. App-only changes reuse the matching retained base. Registry
authorization and transport failures are errors, not evidence of a missing
artifact. New bases are candidates until the final appliance passes its gate;
publication must not replace another definition's selected content.

## Application packaging

The existing Player-only wheelhouse remains the package format. It is prepared
before offline assembly, then verified against the explicit Git revision and
its locked dependency hashes. Application bytes, requirements, wheel inventory,
and source identity must match; a supplied inventory cannot authorize different
code or dependencies. Installation retains system Python's compatible native
GI/GStreamer bindings and uses `pip --no-index --require-hashes`.

A future Debian package may wrap this same installation contract. It is not
required to separate OS preparation from application assembly and must not
introduce package-server calls into the offline phase.

## Boot and update boundaries

OS dependency identity is distinct from boot compatibility. Current bootstrap
code, release contracts, systemd units, and initramfs hooks are applied during
final assembly. Their changes regenerate and requalify boot artifacts even
when native packages remain unchanged. Deployment configuration and source
revision are bound to the final release. Players continue to receive signed
images through central PXE release selection and reboot; they do not perform
APT updates.

## Acceptance

- Unchanged definitions reuse the exact retained artifacts.
- Native-definition changes permit a candidate build; app edits do not.
- Missing artifacts, registry errors, wrong definitions, changed archives, and
  mismatched application packages fail without dependency-acquisition fallback.
- Prepared roots preserve Linux filesystem metadata and native package evidence.
- Final assembly runs in a network-disabled container.
- Every completed appliance still passes its exact-artifact generic boot gate;
  physical Pi/PXE/HDMI qualification remains separate.

See the [CI module](../module-appliance-ci.md),
[Player package](../module-player-package.md), and
[GitHub digest pulls](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry#pull-by-digest).
