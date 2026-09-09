# Media publication and worker recovery

Status: accepted implementation contract, 2026-09-05; not yet fault-qualified.
Extends [the media module](../module-media.md) and
[persistent coordination](../module-coordination.md). Owner: orchestrator.

The MVP runs one supervised media worker. It takes an exclusive, nonblocking
filesystem lock in the shared media volume for its entire lifetime. A second worker
refuses startup. Central/gateway may read the volume but do not acquire originals
or convert files. The lock is released by the OS when the worker dies; startup
reconciliation under the new exclusive lock therefore cannot race another writer.
An expired database lease alone is not evidence that a process has stopped writing
or released its open file descriptors.

PostgreSQL job reservations account the complete original and derivative staging
budget before a job directory is created. The job records immutable original and
recipe identities, an attempt token, lease deadline and reserved bytes. Network and
conversion run outside transactions and use their own hard monotonic deadlines.
Publication checks the attempt token/lease under the media quota lock. A stale
attempt cannot publish. Cleanup does not release reservation accounting until its
staging files were successfully removed. Failed unlink and reduced quota remain
visible pressure; they do not silently authorize more bytes.

Before renaming a completed verified derivative to its canonical hash path, the
worker inserts a `publishing` blob record under the quota lock. Its byte accounting
replaces the corresponding portion of the job reservation, preventing both an
unaccounted rename and double-counted bytes. The worker fsyncs the file and parent
directory, then transactionally marks the immutable blob and job ready. Only ready
blobs can be offered or downloaded. A restart verifies publishing files against
the stored exact Variant before finishing publication; an absent/corrupt file is
unavailable and cleaned with its reservation still accounted.

Ready blobs and active references use the same PostgreSQL quota lock as offer,
secured-assignment and transfer reference creation. GC first marks only an
unreferenced blob `deleting`, making new grants impossible, then unlinks it and
removes accounting. A crash/failure leaves the deleting row accounted until startup
or a later collection completes. Size/hash verification invalidates corrupt files
without releasing the logical assignment's adopted content. A healthy Player may
continue its independently verified secured bytes; central cannot replace that
assignment with a new upstream revision.

Gateway reads use canonical nonsymlink regular files and a dedicated transfer
reference. A bounded response lifetime closes the file descriptor before the
transfer lease may expire, with a safety margin. Disconnect/completion releases
the reference in `finally`. An expired offer does not redirect or re-acquire
upstream media. Every new request must independently pass current Player epoch,
Frame generation and exact digest authorization.

Source definitions are immutable versions. A complete refresh replaces membership
atomically; latest refresh status/diagnostics remain distinct from last successful
membership. Selected acquisition failures get bounded cooldown before another
unsecured slot can select that impossible original. Secured/possibly secured
assignments retain their adopted Variant through upstream deletion or query change.
Only ready jobs for the configured preparation recipe attach a Variant to new
catalog candidates. Deployment connection secrets remain in private files outside
Git and never appear in source definitions, diagnostics or Player payloads.

Multiple concurrent workers, distributed blob storage and general distributed
filesystem semantics are deferred. This exclusive-worker design relies on a local
Linux filesystem volume with working POSIX locking/rename/fsync semantics; tests on
the developer host do not qualify an arbitrary network filesystem.
