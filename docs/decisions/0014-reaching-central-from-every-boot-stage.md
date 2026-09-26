# 0014 — Every boot stage reaches the configured Central the same way

**Date:** 2026-09-25 · **Layer:** module contracts and data flow · **Status:** Owner-reviewed
at this layer. Feature-layer design (function names, algorithms) follows per project. Project 1's
feature layer was reviewed on 2026-09-26; Projects 2 and 3 have not been designed at that layer.

Briefings reviewed by the owner:

- Module layer: <https://claude.ai/artifact/WJggU5Wp2FwPNxHXFA9ivN>
- Project 1, feature layer: <https://claude.ai/artifact/C4wrY7wfDDybcYWTxwhwp3>

## Problem

A Pi 5 booted with `photowall.central=http://photo-wall.localdomain/` reboot-loops and never
reaches Central. The gateway answers with a 301 to https on another host, and the initramfs
treats any redirect as fatal. It also has no CA list and no clock for https. After switch_root,
provisioning ignores the command line and uses only mDNS, and it crashes because a hand-kept
file list left a module out of its package. Every failure is reported as a generic network
error.

## Requirements (hard rules)

| ID | Requirement | Source |
|---|---|---|
| P1 | A netbooted Player joins Central with no local setup. | [requirements](../requirements.md#player-provisioning) |
| R1 | When `photowall.central` is on the cmdline, every stage uses it. Other discovery applies only when it is absent. | Owner |
| R2 | http and https are both legal; https is better. LAN trust is not a main concern, so no host allowlist. | Owner |
| R3 | Redirects are followed across hosts, with a hop limit above 3. One redirect policy covers every Central fetch. | Owner |
| R5 | The initramfs trusts the Debian CA bundle, the same list the booted base of the same release uses. | Owner (amended 2026-09-26) |
| R6 | Clock: a build-time floor, then one SNTP step that never sets the clock below the floor (DHCP option 42, then a public pool zone the product may use). No new `photowall.*` cmdline parameter. A failed step does not block TLS; a certificate date failure is reported as `time`. | Owner (amended at this gate and on 2026-09-26) |
| R7 | No plain-HTTP workaround, and no sidestepping the ingress. | Owner |
| R8 | Never downgrade https to http while following redirects. | Owner (confirmed 2026-09-25) |
| R9 | Every failure names its real cause (TLS, time, redirect, DNS/connect). | Owner (confirmed 2026-09-25) |

R4 ("persist the final address") became a design choice, because the owner called it optional.

The 2026-09-26 amendments came from Project 1's feature layer. R5 is read per release, because
the initrd is staged by hand and can run with a newer base. R6 may step the clock back, because
the Pi 5 kernel loads its RTC at boot and an RTC set in the future would otherwise fail every
https boot. The NTP Pool's terms forbid shipping the default `pool.ntp.org` names in a product.
Liveness needs two kernel parameters that are not `photowall.*` parameters.

## Current design choices (revisable)

- **Locate once, then go direct.** A credential-free `GET /v1/locate` is the only request that
  follows redirects (up to 10, no sub-path moves). It must end at a server that identifies
  itself as Central. Real requests go straight to that origin and refuse redirects. A refused
  redirect triggers a new locate on the next cycle. The owner accepted this as R3's single
  policy.
- **One shared package** (`uplink`, working name). It holds the resolver, locator, trust, clock
  gate, direct fetch and named failure causes, uses only the standard library and `contracts`,
  and is shared by the initramfs, provisioning and the Player. Central's identity format and the
  clock-record format live in `contracts`.
- **Clock gate.** It gives NTP up to 10 seconds, so boots are conservative and would rather wait
  than fail. It is best-effort even for http boots. Root stages write a clock record to `/run`;
  the Player reads it and never sets the clock.
- **No credential binding.** The Player trusts whichever Central locate finds (owner: the Player
  keeps no state across boots). R2 accepts the LAN exposure.
- **Nothing persisted from locate.** Each stage locates again from the cmdline.
- **Computed module lists.** The build computes each artefact's import set, which replaces the
  hand-kept lists that caused the crash.
- **Time source.** The kernel's own `ip=dhcp` supplies option 42, because klibc `ipconfig`
  never requests it. The pool zone is `debian.pool.ntp.org` until a photo-wall vendor zone
  exists.
- **A failed or hung boot always comes back.** P1 requires it. Stage 1 arms the hardware
  watchdog first and hands it to systemd, and it leaves through one emergency restart rather
  than `reboot -f`. The bootloader settings ship in the TFTP bundle as a self-update. Hangs
  before stage 1 starts are only partly covered; an external power cycle covers the rest.
- **Hardware proof before merge.** A pre-release tag of the verified branch provides the
  Central image, because Central images come only from the release workflow.

## Delivery

| Project | Scope | Proven when |
|---|---|---|
| 1. Initramfs reaches Central | Liveness first, then the shared package core, the 10 s clock step, the CA bundle and clock floor in the boot data, the initramfs's computed module list, `/v1/locate` on Central | The loop stays alive for 24 hours, and the Pi fetches the base through the gateway's 301 and switch_roots |
| 2. Provisioning and Player adopt it | Resolver-first provisioning and Player, computed module lists for the packages | The Pi appears unbound in the console |
| 3. Docs | Reword [0008](0008-generic-image-and-serial-identity.md), [0009](0009-minimal-base-and-app-package.md), the README and the runbook to R1–R9 | Docs check passes |

## Deferred

Central-served time between option 42 and the pool; writing a stepped clock back to the Pi 5
RTC; a private CA in the initramfs.

## History

Rev 1 went through two review rounds. Rev 3 was compressed to this layer. Rev 4 separated
requirements from choices and recorded the owner's answers (2026-09-25). Rev 5 recorded the
owner's answers to Project 1's feature-layer briefing: R5 and R6 amended, liveness and the
initramfs's computed module list added to Project 1 (2026-09-26).
