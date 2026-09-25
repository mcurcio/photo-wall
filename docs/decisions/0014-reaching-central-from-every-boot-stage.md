# 0014 — Every boot stage reaches the configured Central the same way

**Date:** 2026-09-25 · **Layer:** module contracts and data flow · **Status:** Owner-reviewed
at this layer. Feature-layer design (function names, algorithms) follows per project. Nothing
is implemented yet.

Briefing reviewed by the owner: <https://claude.ai/artifact/WJggU5Wp2FwPNxHXFA9ivN>

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
| R5 | The initramfs trusts the Debian CA bundle, the same list the booted base uses. | Owner |
| R6 | Clock: a build-time floor, then one forward-only SNTP step (DHCP option 42, then `pool.ntp.org`). No new cmdline parameter. A failed step does not block TLS; a certificate date failure is reported as `time`. | Owner (amended at this gate) |
| R7 | No plain-HTTP workaround, and no sidestepping the ingress. | Owner |
| R8 | Never downgrade https to http while following redirects. | Owner (confirmed 2026-09-25) |
| R9 | Every failure names its real cause (TLS, time, redirect, DNS/connect). | Owner (confirmed 2026-09-25) |

R4 ("persist the final address") became a design choice, because the owner called it optional.

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

## Delivery

| Project | Scope | Proven when |
|---|---|---|
| 1. Initramfs reaches Central | Shared package core, the 10 s clock step, the CA bundle in the boot data, `/v1/locate` on Central | The Pi fetches the base through the gateway's 301 and switch_roots |
| 2. Provisioning and Player adopt it | Resolver-first provisioning and Player, computed module lists | The Pi appears unbound in the console |
| 3. Docs | Reword [0008](0008-generic-image-and-serial-identity.md), [0009](0009-minimal-base-and-app-package.md), the README and the runbook to R1–R9 | Docs check passes |

## Deferred

Central-served time between option 42 and the pool; writing a stepped clock back to the Pi 5
RTC; a private CA in the initramfs.

## History

Rev 1 went through two review rounds. Rev 3 was compressed to this layer. Rev 4 separated
requirements from choices and recorded the owner's answers (2026-09-25).
