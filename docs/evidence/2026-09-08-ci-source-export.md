# CI source export and diagnostic ownership

The [ARM build-and-boot job](https://github.com/mcurcio/photo-wall/actions/runs/34283078855/job/102252245502)
checked out merge revision `6e00998f102250467104875daaf69480dc28c437`
(PR head `019f72dd2b79e0a9ee8ceddc53ff39c9f2345a23`). OS base restoration,
Player package restoration, and source export passed. `prepare_image` failed
with `executing_source_mismatch` before signed image assembly or VM boot.

`execution_inventory()` required `scripts/ci_apt_cache.py`, while
`export_source()` exported only the appliance tree and two contract files.
Consequently, the exported inventory could never satisfy the executing-source
check. Export now derives its required files from the executing inventory,
retains the complete tracked appliance tree, and reads every byte from the
requested Git revision. Exact hash verification remains enforced.

A real temporary Git repository regression reproduced the missing helper before
the fix and passed afterward, exercising export and verification together.
The existing mutation test continues to reject changed hashes for every
executing input. Independent review found no material issue with the fix.

The same hosted job also failed to collect public diagnostics: unprivileged
`install -d` could not change permissions on the root-owned assembly diagnostics
directory. The workflow now runs that operation with `sudo` before the existing
copy and ownership restoration. An isolated offline Ubuntu 24.04 reproduction
confirmed the original permission failure and verified that the corrected
sequence preserves both assembly and base logs and makes them readable by an
unprivileged user. Nine existing workflow/service tests passed.

The sandboxed portable suite recorded 1,308 passed, 203 skipped, and one
failure: `test_loopback_exchange_and_sigterm` could not bind a local UDP socket
(`PermissionError`). This is an execution-environment restriction, separate
from source verification. Ruff and relative documentation links passed.

The full suite then ran outside the restricted sandbox against the existing
local Compose PostgreSQL database using `.venv/bin/python scripts/test_local.py -q`:
**1,488 passed, 24 skipped, four dependency deprecation warnings** in 373.88
seconds. This includes the previously blocked loopback test. Skips require
explicit browser/container fixtures or Linux, GNU tar, and OpenSSL 3 tooling.

These are local working-tree regression results. The hosted signed-image build,
generic VM boot, and physical Pi/PXE/HDMI qualification have not been rerun by
this debugging task. The [CI module](../module-appliance-ci.md) owns the build
contract and its qualification limits.
