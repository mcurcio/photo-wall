> Historical pre-repair audit/checkpoint. Its unapplied status and capability mask are superseded by the [stable Linux checkpoint](../2026-09-30-node-linux-integration.md#stable-pause-checkpoint-after-linux-unit-repairs) and [main handoff](../2026-09-30-player-node-handoff.md). This is retained for provenance, not current instructions.

# Linux lane audit — 2026-09-30

This is a read-only audit after Frame-v3 frozen artifact verification. No corrections
below have been applied. Current archives are pre-fix milestones, not release-ready.
No active build/probe processes remain.

## Source defects and bounded correction design

1. **Broker socket startup capability.** `BrokerLinkService.__init__` changes the socket
   group from root to10004, but `photo-wall-app-broker.service` bounds capabilities to
   SYS_PTRACE and DAC_READ_SEARCH. Root is not a member of group10004. Linux chown(2)
   requires CHOWN for this arbitrary group change; configured mask is0x200004. Add
   CHOWN only, and test the actual restricted service rather than unrestricted root.
   The live effective mask has not been sampled because the matching probe did not run.
   https://man7.org/linux/man-pages/man2/chown.2.html
   https://manpages.debian.org/trixie/systemd/systemd.exec.5.en.html#CAPABILITIES

2. **Retained app cannot reconnect after broker restart.** The app bind-mounts one
   socket inode. Restart unlinks/replaces it, so its bind retains the obsolete inode.
   Parent-approved design: root-owned0755 `/run/photo-wall-app-proof`, containing only
   root:10004 mode0660 app-link.sock, directory-bound read-only to the existing
   `/run/photo-wall-client`. Validate parent ownership/mode/nonlink and existing socket
   type/owner before replacing. Broker journals stay in their separate0700 directory.
   Test retained app across broker restart, changed socket inode with usable new
   listener, parent/symlink substitution rejection and unauthorized peers.

3. **XDG ownership/mode.** Current app XDG runtime points to root-owned0755 directory.
   Use private UID/GID10004 mode0700 tmpfs at `/run/photo-wall-wayland`, bounded1MiB,
   nosuid,nodev,noexec, with readonly Wayland socket bind inside. Point XDG_RUNTIME_DIR
   there. Proof directory remains separate. Narrow Weston runtime itself to0700.
   Display owner confirmed no fixed client socket path dependency; native ABI unchanged.
   Actual nested tmpfs/bind order and UID access need a real unit probe.
   https://specifications.freedesktop.org/basedir/latest/

4. **DRM seat composition omitted.** Node Weston unit does not yet reproduce the
   existing rootless legacy logind/tty1 setup. Display owner confirmed this is a code
   gap: add After/Wants logind, getty conflict, PAMName=login, tty1 and tty cleanup,
   LIBSEAT_BACKEND=logind and XDG_SESSION_TYPE=wayland, retaining UID10005 and private
   runtime. Verify PAM/logind with NoNewPrivileges/capability bounds under actual PID1;
   headless compositor tests do not qualify DRM seat startup. Do not add seatd.

5. **Base-owned supervisory telemetry incomplete.** HostCore currently samples only
   uptime/load/memory/runtime-space. Node model requires independent L1/Display state
   supervision. Manager's own preparation telemetry does not attest its supervisor.
   Add bounded read-only PID1 observations; manager exhausted-budget state also needs
   an explicit base-owned status observation because its supervisor currently remains
   active in a loop. Keep host import closure independent from manager/effect modules.

6. **Host command polling can be delayed by retained evidence.** HostRunner replays
   every retained reboot record (up to1024) before polling commands, including records
   already acknowledged. Each can issue two HTTP calls. Add a bounded durable outbox
   schedule/acknowledgment or cursor so command polling is not proportional to the
   full retained anti-replay journal; retain all effect fences. Test lost receipt and
   journal restart without permitting repeated reboot. Never execute real reboot.

Corrections1–4 change base package/import or unit identity; rebuild baseABI and reseal
app+manager environment references. Manager executable bytes may remain exact only
if its own computed closure is unchanged. Environment mount-parent changes alter
sealer provenance. Existing native Frame-v3 bytes can remain exact if display source
is unchanged. Corrections5–6 would further alter HostCore/baseABI and should be
included before the next coherent snapshot.

## Requirement/evidence matrix

| Concern | Current implementation/evidence | Outstanding |
|---|---|---|
| Debian environment | Real arm64 package/configured recursive snapshot closure, per-member integrity and exact source/native hashes; previous private-root execution | Post-fix coherent artifacts; current entire runtime launch still unexecuted |
| Host independence/reboot | Separate closure/session/journal/slice and actual PID1 request adapter; portable fences tested | Supervisory telemetry and bounded outbox fixes; supported pressure/network envelope; no real reboot test |
| Manager | Versioned sealed executable, optional exact fallback, own preparation credential/telemetry; actual older PID1 manager credential denial+session/idle fixture roundtrip | Matching final artifact runtime; full desired-policy download→broker preparation composed with actual Central; exact fallback under actual PID1 |
| Broker/worker | Durable one-writer journal, exact process/root identity, permit split, seal/no-effect, restart recovery; actual older worker import/no-duplicate recovery | Broker startup/reconnect fixes; real app unit cold launch+online stop/start/failed-target exact fallback+no-effect with retained app and real Central |
| Cold PXE | Explicit V2 flag, nonce/frozen offer/hash handoff, protected config/root preparation; immutable release manifest producer/verification and CI wiring | No current full production squashfs/kernel/initrd bundle built; no actual PXE boot; per-device cohort routing remains deployment-owned |
| Display | Real native and GTK/HTTP/DB headless evidence from display lane | Unit seat/XDG fixes; current packaged GTK follow-up; physical DRM two-Output continuity/pressure |
| Storage | Explicit bounded tmpfs, streaming publication, exact retained-root reuse, capacity refusal and malicious archive tests; prior real1GiB worker peak | Repeated updates eventually refuse with no approved root reclamation; combined8GiB physical load remains unqualified |
| Deployment | Existing publisher with exact schema2 assets/provenance and default-closed D17 gating | Actual release build/publish and deployment-owner cohort configuration, exact serving/rollback guard qualification |

## Distinct blocked actions

- Persistent privileged CI probe invocation: explicit authorization unanswered; not applied.
- Automatic unused-root reclamation: explicit authorization unanswered; not applied.
- Already-authorized standalone matching-artifact PID1 probe: automatic approval review
  unavailable due usage limit, not a safety refusal. No container was started. Do not
  bypass or repeatedly retry while the review service remains unavailable.

Current completed image: `sha256:590ceb64a06ae4d11580b0786c37933b79edd6bbaf1580326a9df8ffb4ce294a`.
Completed source/artifact evidence:
`/private/tmp/photo-wall-node-frame3-review/{source-inputs.json,runtime-inputs.json,build-provenance.json,artifact-verification.json}`.
Latest checkpoint: `/private/tmp/node-frame3-verification-checkpoint.md`.

## HostCore follow-up anchors and acceptance

Exact current path: `HostRunner.tick` (`appliance/node/host_runner.py:55`) saves local
sample, establishes session, POSTs observation at65, then loops every journal key
at69, sends response at73 and event at76, and only polls commands at77. Journal
capacity defaults1024 (`host_storage.py:31`), while acknowledgement is not retained;
`put` preserves only request/response/invocation/event/unknown. `LinuxHostSampler.sample`
(`host_linux.py:16–29`) reads only proc and filesystem, with no PID1 state port.
This misses `docs/player-node-domain-model.md:50`. The manager supervisor loops after
`recovery.recover()` (`manager_launcher.py:106–115`) even at exhausted budget, so an
active unit alone cannot prove manager availability.

Proposed bounded outbox correction: retain anti-replay journal without deletion;
store a separate bounded delivery ledger keyed by command ID and exact response/event
digest. Persist acknowledgements only after canonical route200. An absent event slot
must not mark a later-created event acknowledged. Poll/admit commands before evidence
backlog; durable accepted command execution must not depend on evidence transport.
Then send at most2 pending canonical messages per tick with a durable fair cursor
and per-request timeout. Retry the original encoded bytes/sample/sequence after a
lost response, with no age refresh. A failed evidence request must not suppress the
next tick's command poll. Use one sampled local observation rather than calling the
sampler twice; unit-query failure yields explicit unavailable state, not failure of
host observation or reboot polling.

Tests:1023 retained commands plus one fresh command, failed/slow evidence transport,
and a bounded transport call count;1024 full records still poll the new command but
refuse durable capacity without effect. Restart after lost200 retries exact bytes;
restart after acknowledgement avoids resending; event created after response ACK is
still pending; cursor reaches all pending entries despite one rejected record;
producer/session renewal obeys existing command fences; accepted reboot invoked at
most once. Drivers are recording fakes—never a real reboot. Supervisory tests cover
manager absent/starting/running/exhausted, Display failed/active, PID1 timeout and
malformed result, base-owned sample provenance, and no imports from app/effect code.
