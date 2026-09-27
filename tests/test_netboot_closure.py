"""The every-PR initramfs closure test (decision 0014 §1): stage 1's computed first-party
closure is complete and stdlib-only. It is the guarantee; the import-linter contract on
`uplink` is only an early warning (a denylist cannot catch an unlisted package)."""

import subprocess
import sys

import pytest

from scripts.module_closure import INITRD_FORBIDDEN, REPO, initrd_closure, stage


@pytest.fixture(scope="module")
def closure():
    return initrd_closure()


def test_stage_1_reaches_uplink_and_the_stdlib_only_contracts(closure):
    assert {"appliance.netboot_init", "appliance.bootstrap", "uplink.locate", "uplink.fetch",
            "uplink.clock", "uplink.trust", "contracts.central_identity",
            "contracts.clock_record"} <= set(closure.modules)


def test_stage_1_reaches_no_provisioner_and_no_forbidden_package(closure):
    assert "appliance.provision" not in closure.modules
    assert not [name for name in closure.modules if name.partition(".")[0] in INITRD_FORBIDDEN]
    assert not [name for name in closure.modules
                if name in ("contracts.models", "contracts.enrollment")]   # pydantic


def test_every_closure_module_imports_with_only_the_closure_and_the_stdlib(closure, tmp_path):
    stage(closure, repo=REPO, into=tmp_path)
    probe = (f"import sys; sys.path.insert(0, {str(tmp_path)!r})\n"
             + "".join(f"import {name}\n" for name in closure.modules)
             + "import importlib.util\n"
             + "assert importlib.util.find_spec('pydantic') is None, 'site-packages leaked'\n")
    result = subprocess.run([sys.executable, "-I", "-S", "-c", probe], cwd=tmp_path,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
