# 2026-09-30 local Player process proof

**Evidence class:** local software, packaged-source and PostgreSQL checks. **Status:** staged code, not a commissioned service or Player acceptance. No Kubernetes deployment, Pi boot, protected T1/T2 command session, physical Output or pixels were tested.

## Boundary exercised

The replaceable Player can use its process-local enrollment key to answer one root-owned Unix socket challenge after Central enrollment. The client checks the socket path ownership and connected root peer, then checks the challenge's device, kernel boot, Player ID, authority epoch, process PID and `t1`/`t2` mode before signing. A re-enrollment or service exit invalidates an in-flight response. A recorded proof is not repeated for that same enrollment; socket absence or failure does not interrupt app control or rendering.

The base-owned service accepts one bounded `SOCK_SEQPACKET` exchange per connection. Linux `SO_PASSCRED` credentials on **both** application packets must match the connection peer and a stable PID1 Player unit, `/proc` start-tick, cgroup and invocation sample. A passed socket cannot let a different process answer the second packet. The challenge includes the current protected command session and exact attempt. A one-use nonce expires after 15 seconds of `CLOCK_BOOTTIME`; a separately injected sink must compare the exact context atomically before storing only volatile local evidence. The loader OS observation channel stays independent.

Adversarial review found that rejected `SCM_RIGHTS` packets could leave received file descriptors open and that serial handling let an idle local client block every subsequent proof. Both paths now close returned descriptors before rejection, with `MSG_CMSG_CLOEXEC` where available. The service gates accepted sockets to the current Player MainPID and UID, admits at most four queue-free workers, and gives the first packet a two-second deadline. Every worker still rechecks the full process and attempt context before proof storage. The admission sample is a short-lived routing hint, never evidence of authenticity.

The bootstrapper source closure now includes the proof contract, verifier, Linux adapter and service with declared Debian `python3-cryptography` and `python3-pydantic` dependencies. Their bytes affect the sealed base ABI. The Player closure includes the separate client. No proof socket or service unit is installed or enabled: a protected T1/T2 context provider and atomic sink do not yet exist. The service has no runnable production composition, Central credential, command effect or acceptance writer. A locally signed public key must later be joined to the current authenticated Player Registry enrollment and authority epoch before any acceptance decision.

## Checks and limits

| Check | Result | Limit |
|---|---|---|
| Combined proof, Player service/link, closure and packaging tests on macOS | 290 passed, 6 skipped | Linux kernel credential and `dpkg-deb` cases are among the skips. This is not a resident base service test. |
| Full portable suite | 2,806 passed, 1,123 skipped, 1 failed at the old bootstrapper import-table expectation | The package declaration now correctly includes cryptography and pydantic; after updating that assertion, all 7 package-closure tests passed. The full suite was not repeated after that test correction. |
| PostgreSQL attempt-report tests with proof session/trust binding | 26 passed, 2 warnings | A report is stored only as a carrier claim; this does not certify the app process or accept an artifact. |
| Ruff, six import contracts, documentation links and diff check | Passed | Static/structural checks. |
| Adversarial socket regressions | 34 passed, 1 skipped | Fake-socket tests close returned descriptors and prove a second proof completes while one connection waits. The Linux credential test is skipped on macOS. |

The review found and corrected a final TTL timestamp split: the verifier now uses one sampled `CLOCK_BOOTTIME` value for both its expiry decision and the recorded proof time. The full Linux CI run exercised the kernel socket tests before failing on the unrelated proof-task lifecycle cases described below. Physical qualification must still exercise the actual installed base service, app process restart and protected context revocation. The [fleet design](../player-fleet-control-design.md), [red/blue safety contract](../player-fleet-red-blue-refinement.md) and [decision register](../design-decisions.md) own those remaining requirements.

The first hosted full PostgreSQL run for this slice passed 3,559 cases but exposed six Player lifecycle failures: cancellation of the new proof task could escape `run()` cleanup during pre-enrollment fault recovery. The Player now starts that task only after enrollment and waits for its cancellation under a shield during shutdown. The affected Player link, fault, service and proof suites then passed locally (161 passed, 1 skipped). A later hosted run must still verify the full revision.
