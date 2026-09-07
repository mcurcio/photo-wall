# Single-process Player control service

Implementation contract, revised for stateless Players. The service composes neutral enrollment, the [Executor](module-player-execution.md), the [disposable cache](module-cache.md), and [native rendering](module-native-renderer.md). It owns networking, fresh session authority, current preparation, and worker scheduling. It never imports central or media packages, selects media, or owns durable state.

## Startup and enrollment

The common public configuration names one trusted central HTTPS origin, an optional public CA file, an optional cache directory, the RAM boot-context file, a cache quota, and native resource limits. Explicit HTTP is limited to isolated fixture development. Strict bounded JSON rejects unknown fields, credentials in URLs, query fragments, and non-origin base paths. Output observations come from Linux DRM connectors, with at most two HDMI connectors.

Every process start generates a new Ed25519 key in memory. The key, bearer token, authority epoch, plans, commitments, observations, pins, and cache metadata never reach persistent storage. Enrollment proves the fresh key together with the central boot ticket, equipment observation ID, Linux boot ID, and current Output observations. The central registry recognizes returning equipment from the trusted provisioning observation, issues a new authority epoch, and returns current configuration and assignments. Unknown equipment remains unbound. An earlier process key, bearer token, epoch, plan, or commitment is rejected after the new session becomes current.

Cold boot requires reachable time, provisioning, release, enrollment, control, and media services. A running Player may preserve already authorized output while a connection is interrupted until its bounded authority lease expires. The Player makes no promise to reconstruct playback after a cold reboot without central connectivity.

The boot context is produced by the common bootstrap in `/run/photo-wall/boot.json`. It binds `ticket_id`, centrally recognized `device_id`, Linux `boot_id`, selected `release_id`, and trial status. Its `persistence` value is always `volatile`; it describes the intentional stateless model rather than a storage fault.

## Control, media, and authority

The GTK/GLib main thread owns Renderer and control-state application. An asyncio network thread owns authenticated bounded HTTP/WebSocket work, and one media thread serializes cache operations. Queues are bounded. Configuration/epoch changes and revocations cannot be lost through state coalescing. Apply configuration, the optional Plan, revocations, compatible commits, and the immediate prepare/tick in one GLib callback.

HTTP does not follow redirects or use ambient proxy settings. Metadata responses and WebSocket messages are limited to 1 MiB with bounded deadlines. Media requests use only `/v1/media/{exact SHA256}` on the configured central origin. The service verifies status, media type, content length, and current authority at every awaited boundary, then streams bounded chunks into Executor/Cache. Cache verifies the complete digest and quota admission.

Before requesting media, the service asks Cache to validate a reusable content-addressed file. A valid candidate is pinned for the current assignment without a download. A missing or corrupt candidate is reacquired. Download and cache loss invalidate readiness but never change the exact centrally secured selection. Acquisition retries use bounded 1/5/15/60-second delays. Cache ownership is reconciled every two seconds and secured bytes are reverified at least every ten seconds.

The service sends readiness at least twice per second while a Plan is active. Observations retain their actual sample/draw times; download or preroll is never reported as presentation. A current-state response carries configuration, an optional Plan, Commit records, and Revocations. It does not carry a clock sample. Invalid or stale messages cause a coded reconnect and cannot restore old authority.

## Independent clock probe

Coordinated scheduling uses the disciplined OS UTC clock and an authenticated `GET /v1/player/time` probe, independent of state delivery and using its own one-connection HTTP client. The response binds the server sample to the current `player_id` and authority epoch. The Player measures transport RTT through complete bounded-body receipt, estimates central offset at the local midpoint, and checks transport drift, time between receipt and application, post-receipt drift, mapping age, and clock steps.

The existing readiness thresholds remain: uncertainty is `RTT/2 + abs(offset)` and must be at most 100 ms; application age is at most one second; transport/application drift is at most 10 ms; mappings expire after 30 seconds; and a clock step above 250 ms invalidates the mapping. The Player currently probes once per second. Rejected samples withhold clock-dependent readiness and publish bounded diagnostics: status/rejection reason, RTT, central offset, application age, aggregate and component drift, mapping age, detected step, uncertainty, and accepted/rejected/sample counters. The endpoint samples time; it never sets the system clock. Chrony remains the appliance clock-discipline service.

## Health and release trial

After current configuration is reconciled, the service atomically writes `/run/photo-wall/player/service-health.json`. The public sample includes Linux boot ID, monotonic sample time, current player/session identity, `persistence=volatile`, health and reason, release-acceptance state, and clock diagnostics. It contains no key or token. Healthy requires an Executor, reconciled configuration, a safe current clock mapping, and available renderer capacity; an unbound Player with healthy connected Outputs may be healthy.

While the selected release is not accepted, the Player posts current health to `/v1/player/boot-health` with the boot ticket. Central accepts only fresh continuous health for the current boot attempt and current session. A local watchdog observes the central acknowledgment; a candidate that fails to obtain it within its bound requests a reboot. The next PXE boot asks central for a new ticket and therefore receives the centrally accepted release.

Implemented entry point: `python -m player.service --config /etc/photo-wall/public.json`. The current public shape is:

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

The systemd sandbox keeps the Wayland runtime visible and grants the Player write access only to its runtime health directory and any explicitly configured cache directory. The root-owned `/run/photo-wall` parent protects the bootstrap report. Native namespace boot and exact-image testing remain required to qualify the built unit.

## Acceptance boundary

Service tests cover fresh enrollment/session rotation, stale grants and revocations, current-state reconciliation, independent time-probe diagnostics, bounded bodies and redirects, cancellation during media transfer, valid surviving-cache reuse, corrupt-cache reacquisition, and real central HTTP integration. The complete exact-image native path, process restart/reboot on built artifacts, failed-candidate reboot, and physical Pi/PXE/dual-HDMI behavior remain acceptance work in [validation](validation.md).
