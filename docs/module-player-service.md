# Single-process Player control service

Implementation contract, revised for stateless Players. The service composes neutral enrollment, the [Executor](module-player-execution.md), the [disposable cache](module-cache.md), and [native rendering](module-native-renderer.md). It owns networking, fresh session authority, current preparation, and worker scheduling. It never imports central or media packages, selects media, or owns durable state.

## Startup and enrollment

The common public configuration names an optional trusted central origin, an optional public CA file, an optional cache directory, the RAM boot-context file, a cache quota, and native resource limits. The Player reads `photowall.central` from the kernel command line; that root takes precedence. `central_origin` in public.json is the saved root, written by provisioning only when it found Central by mDNS, and used only when the command line names no Central. mDNS discovery (`_photowall._tcp`) runs only when neither the command line nor the saved configuration names a Central. Before the first request the Player locates Central with `GET /v1/locate` (the only request that follows redirects) and sends every subsequent request, including the WebSocket, to the located origin. It locates again after every failed cycle. Its registration is bound to the origin that issued it, via a `Session(central)` value in `player/central_link.py` that is the only source of authenticated headers: a registration joins a session only through `.enrolled()`, and `.relocated(new)` keeps it only when the new origin's scheme, host and port match the one that issued it. A newly located origin means silent re-enrollment by serial, which keeps the Frame binding ([decision 0014, U8](decisions/0014-reaching-central-from-every-boot-stage.md#requirements-hard-rules)). `CentralLink` also owns the httpx clients (a response hook refuses any 3xx before it can be followed), the websocket factory, and failure naming, so `service.py` never passes `follow_redirects`, `ssl`, headers or URLs itself. Strict bounded JSON rejects unknown fields, credentials in URLs, query fragments, and non-origin base paths. Output observations come from Linux DRM connectors, with at most two HDMI connectors.

Every process start generates a new Ed25519 key in memory. The key, bearer token, authority epoch, plans, commitments, observations, pins, and cache metadata never reach persistent storage. Enrollment proves the fresh key together with the central boot ticket, equipment observation ID, Linux boot ID, and current Output observations. The central registry recognizes returning equipment from the trusted provisioning observation, issues a new authority epoch, and returns current configuration and assignments. Unknown equipment remains unbound. An earlier process key, bearer token, epoch, plan, or commitment is rejected after the new session becomes current.

Cold boot requires reachable time, provisioning, release, enrollment, control, and media services. A running Player may preserve already authorized output while a connection is interrupted until its bounded authority lease expires. The Player makes no promise to reconstruct playback after a cold reboot without central connectivity.

Once running, the Player's own liveness deadline is owned by the systemd hardware watchdog (0014 M5): `appliance/systemd/player.service` sets `Type=notify`, `NotifyAccess=main`, `WatchdogSec=300`. `main()` sends `READY=1` (`uplink.watchdog.ready()`) once the service starts, and the run loop sends `WATCHDOG=1` (`uplink.watchdog.pet()`) at the end of every run cycle and after each completed control-loop exchange. The 300 s bound leaves margin over the worst healthy gap (about 213 s: one backoff plus a full reconnect and locate cycle), and never gates the pet on renderer health — a Player with no display would otherwise crash-loop.

The boot context binds an optional `ticket_id` (`None` means no boot ticket was ever issued for this boot), centrally recognized `device_id`, Linux `boot_id`, selected `release_id`, and trial status. On netboot (D1), the common bootstrap produces it at `/run/photo-wall/boot.json` with `persistence="volatile"` and a real `ticket_id`, describing the intentional stateless model rather than a storage fault. On a flashed (D0) Player, that file does not exist — there is no boot server to write it — so the service synthesizes an equivalent boot context itself from the Pi's hardware serial (0008 baseline), with `persistence="persistent"` and `ticket_id=None`; `device_id` is derived from the same serial-hashing scheme either way, so one Pi keeps one `device_id` across delivery tiers. Enrollment, `release_accepted`, and boot-health reporting are all keyed on whether `boot_context.ticket_id is None` — never on `persistence`, which only describes storage. A ticketless context sends `ticket_id=None` at enroll, which central's registry reads as the explicit signal to enroll the Player unbound by serial with no prior boot ticket required, and starts `release_accepted=True` so it never reports boot-health against a ticket that was never issued.

## Control, media, and authority

The GTK/GLib main thread owns Renderer and control-state application. An asyncio network thread owns authenticated bounded HTTP/WebSocket work, and one media thread serializes cache operations. Queues are bounded. Configuration/epoch changes and revocations cannot be lost through state coalescing. Apply configuration, the optional Plan, revocations, compatible commits, and the immediate prepare/tick in one GLib callback.

HTTP does not follow redirects or use ambient proxy settings. Metadata responses and WebSocket messages are limited to 1 MiB with bounded deadlines. Media requests use only `/v1/media/{exact SHA256}` on the located central origin. The service verifies status, media type, content length, and current authority at every awaited boundary, then streams bounded chunks into Executor/Cache. Cache verifies the complete digest and quota admission. No redirect is ever followed by httpx or the WebSocket, so the bearer token never leaves the located origin.

Before requesting media, the service asks Cache to validate a reusable content-addressed file. A valid candidate is pinned for the current assignment without a download. A missing or corrupt candidate is reacquired. Download and cache loss invalidate readiness but never change the exact centrally secured selection. Acquisition retries use bounded 1/5/15/60-second delays. Cache ownership is reconciled every two seconds and secured bytes are reverified at least every ten seconds.

The service sends readiness at least twice per second while a Plan is active. Observations retain their actual sample/draw times; download or preroll is never reported as presentation. A current-state response carries configuration, an optional Plan, Commit records, Revocations, and optional `identify_output` operational state. The identify cue carries a request ID, Output ID, authority epoch, and server-computed remaining seconds. The Player accepts it only for a connected Output that remains unbound in the current configuration and whose epoch matches. PlayerService converts the bounded remaining duration to a local monotonic deadline, so repeated state delivery cannot extend a request, and clears it from its existing 33 ms main-thread tick. NativeRenderer only shows or hides the selected Output's banner. The cue is not Scene content, execution authority, or evidence that a person saw the banner. Both `/v1/player/state` and the authenticated WebSocket carry it; a missing, stale, expired, bound, or disconnected request clears the banner. State delivery does not carry a clock sample. Invalid or stale messages cause a coded reconnect and cannot restore old authority.

## Independent clock probe

Coordinated scheduling uses the disciplined OS UTC clock and an authenticated `GET /v1/player/time` probe, independent of state delivery and using its own one-connection HTTP client. The response binds the server sample to the current `player_id` and authority epoch. The Player measures transport RTT through complete bounded-body receipt, estimates central offset at the local midpoint, and checks transport drift, time between receipt and application, post-receipt drift, mapping age, and clock steps.

The existing readiness thresholds remain: uncertainty is `RTT/2 + abs(offset)` and must be at most 100 ms; application age is at most one second; transport/application drift is at most 10 ms; mappings expire after 30 seconds; and a clock step above 250 ms invalidates the mapping. The Player currently probes once per second. Rejected samples withhold clock-dependent readiness and publish bounded diagnostics: status/rejection reason, RTT, central offset, application age, aggregate and component drift, mapping age, detected step, uncertainty, and accepted/rejected/sample counters. The endpoint samples time; it never sets the system clock. Chrony remains the appliance clock-discipline service.

## Health and release trial

After current configuration is reconciled, the service atomically writes `/run/photo-wall/player/service-health.json`. The public sample includes Linux boot ID, monotonic sample time, current player/session identity, a `persistence` field, health and reason, and clock diagnostics. It contains no key or token. As implemented, the health sample's `persistence` field is currently always published as `volatile`, independent of the boot context's own `persistence` (which is `persistent` for a flashed D0 Player — see [Startup and enrollment](#startup-and-enrollment)); this is a known discrepancy worth verifying against intent rather than a documented guarantee. Healthy requires an Executor, reconciled configuration, a safe current clock mapping, and available renderer capacity; an unbound Player with healthy connected Outputs may be healthy.

The service sends each accepted configuration's connected, unbound Output IDs to
the Renderer before the next Executor tick. The Renderer owns the local status
page and never treats it as Scene content or playback evidence. When the Pi
provisioner hands forward a base-running tag on the per-device `.deb` path, the
service posts base-health; a response with `accepted: false` does not count as
reported and a retry uses a higher sequence number. The default global `.deb`
path hands forward no tag and sends no base-health report.

The journal line for faults is `player fault: <code> <detail>`, logged once per change of code. Network failures use `<cause>_<reason>` codes (e.g. `tls_untrusted`, `time_not_yet_valid`, `redirect_unexpected`, `connect_refused`, `dns_failed`, `central_error`, `http_status`, `transfer_short`), and the detail is `cause=... reason=... host=... detail=...` plus the stage-1 clock record for time and untrusted-TLS failures. Service codes (e.g. `registration_required`, `media_download`, `clock_probe`) are unchanged.

The earlier signed-release boot-health trial has been retired: every boot is ticketless, so there is no per-boot signed release trial or trial watchdog to disarm. The optional base-health report above belongs to the later per-device base/package path. The Player also writes current process health locally to the health file. The hardware watchdog that stage 1 arms and hands to systemd ([0014](decisions/0014-reaching-central-from-every-boot-stage.md)) is systemd's to pet; the Player never touches it.

The Player .deb runs `/usr/bin/python3 -I -B /usr/lib/photo-wall-player --config /etc/photo-wall/public.json`, executing the package's private directory application. The public configuration shape is:

```json
{
  "schema": 1,
  "central_origin": "https://photo-wall.example",
  "ca_file": "/etc/photo-wall/ca.pem",
  "cache_dir": null,
  "boot_context_file": "/run/photo-wall/boot.json",
  "cache_bytes": 536870912,
  "allow_http": false,
  "decoder_limit": 4,
  "texture_budget": 536870912
}
```

`cache_dir=null` uses a process-owned temporary directory. A deployment may point it at a writable cache path to reuse surviving bytes, but availability and correctness never depend on that path. Bootstrap/release configuration is separate.

`ca_file` is an absolute path to a PEM bundle used **instead of** the Debian bundle (`/etc/ssl/certs/ca-certificates.crt`) for every Central request (locate, httpx, and WebSocket). One trust store per process.

`allow_http` is accepted but **ignored** (decision 0014 R2 made HTTP legal for command-line and discovered origins). It is kept because the configuration schema forbids unknown keys. In fixtures, set it explicitly; in production, omit it or set it to `false` without effect.

The systemd sandbox keeps the Wayland runtime visible and grants the Player write access only to its runtime health directory and any explicitly configured cache directory. The root-owned `/run/photo-wall` parent protects the bootstrap report. Native namespace boot and exact-image testing remain required to qualify the built unit.

## Acceptance boundary

Service tests cover fresh enrollment/session rotation, stale grants and revocations, current-state reconciliation, independent time-probe diagnostics, bounded bodies and redirects, cancellation during media transfer, valid surviving-cache reuse, corrupt-cache reacquisition, and real central HTTP integration. The complete exact-image native path, process restart/reboot on built artifacts, failed-candidate reboot, and physical Pi/PXE/dual-HDMI behavior remain acceptance work in [validation](validation.md).
