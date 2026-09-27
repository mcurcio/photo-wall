# 0014 — Every boot stage reaches the configured Central the same way

**Date:** 2026-09-25 · **Layer:** module contracts and data flow · **Status:** Owner-reviewed
at this layer. Feature-layer design (function names, algorithms) follows per project. Project 1's
feature layer was reviewed on 2026-09-26. Project 2's feature layer was designed on 2026-09-26;
its choices (Q1-Q4 below) were adopted as recommended without an owner gate (owner: "one big
implementation"), and they stay revisable. Project 3 is docs only.

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
| R1 | When `photowall.central` is on the cmdline, every stage uses it directly for app config (provisioning and the Player), skips mDNS, and consults no saved configuration. Other discovery applies only when it is absent. | Owner (amended 2026-09-27, U3) |
| R2 | http and https are both legal; https is better. LAN trust is not a main concern, so no host allowlist. | Owner |
| R3 | Redirects are followed across hosts, with a hop limit above 3. One redirect policy covers every Central fetch. | Owner |
| R5 | The initramfs trusts the Debian CA bundle, the same list the booted base of the same release uses. | Owner (amended 2026-09-26) |
| R6 | Clock: a build-time floor, then one SNTP step that never sets the clock below the floor (DHCP option 42, then a public pool zone the product may use). No new `photowall.*` cmdline parameter. A failed step does not block TLS; a certificate date failure is reported as `time`. | Owner (amended at this gate and on 2026-09-26) |
| R7 | No plain-HTTP workaround, and no sidestepping the ingress. | Owner |
| R8 | Never downgrade https to http while following redirects. | Owner (confirmed 2026-09-25) |
| R9 | Every failure names its real cause (TLS, time, redirect, DNS/connect). | Owner (confirmed 2026-09-25) |
| U7 | On failure, stage 1's boot screen prints the named cause and the redirect hops only. It never prints a suggested value to pin, because the located origin can come from an unauthenticated first hop. | Owner (2026-09-27) |
| U8 | Registration is bound to the origin that issued it (the bearer-leak fix). When Central's located origin changes, re-enrollment is silent: the Player re-enrolls automatically by serial and keeps its Frame binding, as [returning equipment](../requirements.md#central-authority-and-stateless-players) does. | Owner (2026-09-27) |

R4 ("persist the final address") became a design choice, because the owner called it optional.

The 2026-09-26 amendments came from Project 1's feature layer. R5 is read per release, because
the initrd is staged by hand and can run with a newer base. R6 may step the clock back, because
the Pi 5 kernel loads its RTC at boot and an RTC set in the future would otherwise fail every
https boot. The NTP Pool's terms forbid shipping the default `pool.ntp.org` names in a product.
Liveness needs two kernel parameters that are not `photowall.*` parameters.

The 2026-09-27 rows come from the owner's answers (U1-U8) to an adversarial review of
[PR #28](https://github.com/mcurcio/photo-wall/pull/28). U1, U2, U4 and U6 are product behaviour
and live in [requirements](../requirements.md#failure-visibility-and-recovery). U5 is
[deferred](#deferred). The owner has not ruled on the case where the cmdline names no Central.

Known defects against these rules, to be fixed in PR #28 (this revision changes no code):

- **R1 (U3).** The Player loads and validates the saved `central_origin` even when the cmdline
  names Central, and logs it as ignored (`PlayerConfig` and `central_finder` in
  `player/service.py`). A malformed saved value stops the Player before it reads the cmdline.
- **U7.** When the configured root is http, stage 1 prints
  `note: … set photowall.central=<located origin>/` (`appliance/netboot_init.py`, phase 5). The
  [runbook](../runbook.md)'s cmdline step repeats that advice.
- **U8.** The Player keeps its registration across a new locate, so a newly located origin
  receives the old bearer before it answers 401 and the Player re-enrolls.

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
  than fail. It is best-effort even for http boots. Stage 1 writes a clock record to `/run`;
  provisioning and the Player read it and never set the clock. A `time` failure in provisioning
  exits to the unit's reboot path, where stage 1 steps again.
- **No pinned Central.** The Player trusts whichever Central locate finds (owner: the Player
  keeps no state across boots). R2 accepts the LAN exposure. Its registration is the exception:
  U8 binds it to the origin that issued it.
- **The located origin is never saved.** Each stage locates again from its root, and so does
  every failed cycle.
- **The saved root, only when the cmdline names no Central.** When provisioning finds Central by
  mDNS, it writes that root to the handoff as `central_origin`, and the Player uses it before
  browsing mDNS itself. This is the current code, not an owner ruling. The "cmdline > saved root >
  mDNS" order in [0008](0008-generic-image-and-serial-identity.md) describes it.
- **Computed module lists.** The build computes each artefact's import set, which replaces the
  hand-kept lists that caused the crash, for the initramfs and both packages.
- **One Debian declaration; nothing on the device resolves packages.** `scripts/debian_packages.py`
  names every Debian package and the one snapshot.debian.org pin. The rpi-image-gen base, the
  initrd build root and the CI device root are built from it, and each `.deb`'s Depends is
  rendered from it. The base carries the device set (the bootstrapper's and the Player's
  packages), and provisioning installs the Player with `dpkg --install` alone (owner steer,
  2026-09-26).
- **Private package directories (Q1).** Each `.deb` ships its computed closure under
  `/usr/lib/<package>/`, run as `python3 -I -B <dir>`. Nothing goes to dist-packages, so the two
  packages share no file.
- **A Player the base cannot satisfy (Q2).** dpkg refuses it, and the unit's start limit reboots
  the Pi. The build checks one revision's base against its Player's Depends. Operator rule: stage
  the base first.
- **Stage-2 name resolution (Q3).** Stage 1 copies its resolver to the new root's
  `/etc/resolv.conf`, and the base carries none. Lease renewal is deferred.
- **The Player's link.** httpx and websockets are built from the one Trust. Neither follows
  redirects: the websocket's own redirect following is refused, so the bearer never leaves the
  located origin. Every failed cycle locates again, and a new locate can change the origin (the
  U8 defect above).
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
| 2. Provisioning and Player adopt it | Resolver-first provisioning and Player, computed module lists for the packages, the handoff readable by the Player (0644), private package directories, stage 1's resolver hand-over, device-root checks, and one Debian declaration for the base, the initrd root, CI and both packages | The Pi appears unbound in the console |
| 3. Docs | Reword [0008](0008-generic-image-and-serial-identity.md), [0009](0009-minimal-base-and-app-package.md), the README, the runbook, and the module docs (Player service, Player package, appliance builder) to R1–R9 | Docs check passes |
| 4. Owner UX fixes (in PR #28; fixes only) | R1 as amended (U3, cmdline first), U7's boot-screen text, the bearer leak (U8), and the media and time loops naming their cause and locating again | Not yet defined |
| 5. Follow-up PR (not PR #28; everything new) | U1's error page, independent of the Player package; the Player's debug overlay (U4; none exists yet); Central staleness detection for every enrolled Player (U4); console fault detail (U6). Also a pre-existing defect: the console's "Player connected" dot comes from the Player-reported HDMI flag (`central/console/src/join.js`), and `players.last_seen` is set only at enrollment (`central/registry.py`), so a dead Pi shows as connected | Not yet defined |

## Deferred

An internet (WAN) outage, and a stale clock floor against a renewed certificate (U5: deferred by
the owner on 2026-09-27, no requirement); Central-served time between option 42 and the pool;
writing a stepped clock back to the Pi 5 RTC; a private CA in the initramfs; DHCP lease renewal
in stage 2; a `time` failure in a running Player (persists until reboot); pinning the Raspberry
Pi archive packages.

## History

Rev 1 went through two review rounds. Rev 3 was compressed to this layer. Rev 4 separated
requirements from choices and recorded the owner's answers (2026-09-25). Rev 5 recorded the
owner's answers to Project 1's feature-layer briefing: R5 and R6 amended, liveness and the
initramfs's computed module list added to Project 1 (2026-09-26). Rev 6 recorded Project 2's
feature layer (2026-09-26): the choices above adopted without a gate, and the owner's steer to
unify the Debian package sources and lists.
Rev 7 recorded the owner's UX answers to the adversarial review of PR #28 (2026-09-27): R1 amended (U3), U7 and U8 added, U5 deferred, the saved root and the known defects stated, and the fixes and the follow-up placed in Delivery; the owner's rulings on the flagged conflicts then moved all new work to the follow-up and widened U4's staleness to every enrolled Player.
