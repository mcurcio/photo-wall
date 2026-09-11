# Errata

Append-only. Read with `grep -a`.

## m1-central-lifecycle (central unbind + pending queue)

- **"bumps generation (and whatever epoch `configuration_in` fences on)" conflates two
  distinct mechanisms.** `configuration_in` (central/registry.py:300) fences execution
  bindings on the *player's* `authority_epoch`, which only changes on retire/re-enroll —
  not on the Frame's `generation`, which is a separate optimistic-concurrency counter
  bumped by `bind`/`retire`/now `unbind`. Deleting the `bindings` row (frame_id is the
  bindings PK) is what withdraws the player's execution binding; the generation bump on
  the Frame is for `bind`/`calibrate` optimistic-concurrency parity with `bind`/`retire`,
  not part of `configuration_in`'s fencing. `unbind` does not touch `authority_epoch`, and
  should not — the player's token/session must remain valid after an unbind (only `retire`
  invalidates it). Implemented per the literal instruction (mirror bind/retire's generation
  bump) but flagging that the epoch clause, read literally, does not apply here.

- **Extending `InstallationInventory`/`PlayerInventory` with a required field breaks
  existing consumers.** Adding `PlayerInventory.is_bound: bool` (no default) broke
  `tests/test_installation_models.py`, `tests/test_appliance_media.py`, and
  `tests/test_vm_media_probe.py`, which construct/decode inventory-shaped payloads by
  hand (fixture `Operator.request` in tests/test_appliance_media.py:19, and
  `InstallationInventory.model_validate_json` in scripts/vm_inventory_probe.py) without
  the new field. Resolved by defaulting `is_bound = False` rather than requiring it. This
  is the pragmatic non-breaking choice per the bead's "do not break existing inventory
  consumers/tests" constraint, but it does mean a hand-built or older-schema inventory
  payload silently reads as "not bound" rather than raising — worth noting since it's a
  divergence from this codebase's general preference for `Field(strict=True)`/required
  fields over silent defaults on wire models.

## m3-hardware-serial (flashed device_id) — DESIGN FACT CORRECTION

- **The 0008 "Verified facts" table is wrong that serial enroll is standalone/already-built.**
  `Registry.enroll` (central/registry.py:134) unconditionally calls `bind_session_in`
  (central/releases.py:264) -> `_device()` (central/releases.py:168), which raises
  `device_not_found` (404) when there is no `appliance_devices` row. That row is created
  ONLY by the netboot boot-ticket path (`select_boot`). So the as-built enroll is COUPLED
  to netboot: a flashed (D0) player presenting a placeholder ticket_id/release_id is
  rejected before any player record is created. Reproduced against real Postgres by the
  m3-hardware-serial verifier.
- **Consequence:** the 0008 baseline "flash and go" tracer (line 146: "enrolls by serial ...
  with no pre-registration") is NOT deliverable with player-only changes. It requires a
  NET-NEW central bead -- `m3-central-d0-enroll` -- that lets a ticketless serial enrollment
  succeed and auto-creates an UNBOUND (pending) equipment record. Additive within the
  accepted three-ladder architecture (does not change the frame); the design INTENT
  ("no pre-registration") already dictates the behavior. Re-cut: added m3-central-d0-enroll
  before m3-flash-image.
- The player bead (serial->device_id derivation) is correct in isolation (risks A/B passed);
  its docstring claim that "central records but does not verify" is FALSE and was corrected.
