# 2026-09-29 Output identification request

Central can queue a short request for an active Player to identify one known,
connected, unbound Output. The Equipment roster exposes **Identify display** on
Pending Players, and the Player renders a high-contrast banner on the requested
Output. Central persists one request per Player for at most 15 seconds; a new
request replaces it. REST and WebSocket state delivery include the request only
while its authority epoch is current and its Output remains connected and
unbound. The Player applies the server-provided remaining duration against its
local monotonic clock in PlayerService; its existing 33 ms main-thread tick
owns expiry, and NativeRenderer only shows or hides the banner. Console success
wording reports Central acceptance only.

## Local verification

On the dirty working tree at base revision `b308838`, this focused command used
the local Compose PostgreSQL database:

```sh
.venv/bin/python scripts/test_local.py -q tests/test_output_identification.py
```

Result: **3 passed**, one Starlette/AnyIO deprecation warning. The cases cover
REST and WebSocket delivery, server-computed countdown and expiry,
single-request supersession, admin authentication, unknown/bound/disconnected
Output refusals, removal after binding or disconnection, and authority-epoch
fencing after re-enrollment. This is Central HTTP/PostgreSQL integration
evidence on a dirty checkout, not acceptance of a committed revision.

## Qualification limits

The tests do not run a Player process or verify the banner on a physical panel.
No Pi, HDMI output, Kubernetes deployment, or end-to-end operator-to-screen
observation was used. The request receipt does not prove that the Player
received or rendered the banner. Physical Pi/HDMI visibility and continuity
remain unqualified.
