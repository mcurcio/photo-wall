# Acceptance evidence

Evidence classes are separate:

- **Simulated:** deterministic domain, recording Actuator, protocol and cache fault tests.
- **Integration:** actual database, HTTP service, packaged launch, real declared-version Immich and network-isolation tests when run.
- **Physical:** checksum-identified appliance boot, fresh Pi PXE, HDMI rendering/continuity and visible coordination on named equipment.

No physical or Immich integration evidence exists yet. Container images for central services are not the required Pi appliance artifact. Keep binary artifacts and private deployment data outside Git. Detailed dated records identify revisions, executed commands, failures and limits; the [delivery checklist](../implementation-checklist.md) tracks remaining acceptance.
