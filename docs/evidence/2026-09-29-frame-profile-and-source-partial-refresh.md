# 2026-09-29 Frame profile serialization and partial Source refresh

## Frame profile replacement

Profile replacement now checks for active Runtime references and updates the
Frame inside the same serialized coordination/Runtime transaction. This closes
the race where an activation could begin after an unlocked in-use check but
before the profile update. The registry requires the caller's connection to
hold both coordination and Runtime advisory locks before it changes the Frame.

Focused PostgreSQL-backed verification: `tests/test_operator_frames.py` — **18
passed**. This is local software/database evidence; no physical display or
hardware behavior was exercised.

## Partial Source refresh

A Source whose refresh succeeded remains healthy when some discovered items are
pending or rejected. The console retains the successful refresh status and
shows the affected item count and bounded diagnostic categories. When valid
items remain, the message says so. Clean successful refreshes keep their
existing wording, and upstream asset identifiers are not shown.

Focused verification: **5** console checks and **1** Chromium browser check
passed. These checks use local software and synthetic Source records; they do
not qualify a live Immich connection or media-worker deployment.

## Qualification

These results establish the described local transaction and operator wording
only. They make no claim about physical equipment, live service deployment,
real Immich behavior, or visible playback.
