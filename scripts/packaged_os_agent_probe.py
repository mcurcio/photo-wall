#!/usr/bin/env python3
"""One-shot packaged OS observation and package-refusal probe in the device root.

The netboot tracer runs this with Debian's isolated Python. Only the installed
bootstrapper closure is added to sys.path; the checkout mounted at /e2e is not
an import source. Fixed synthetic serial/boot values are tracer fixtures, not
claims about a Pi or systemd boot.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import sys
from pathlib import Path

PACKAGE = Path("/usr/lib/photo-wall-bootstrapper")


def _installed():
    sys.path.insert(0, str(PACKAGE))
    from appliance import os_agent, provision  # noqa: PLC0415
    for module in (os_agent, provision):
        if not Path(module.__file__).resolve().is_relative_to(PACKAGE):
            raise RuntimeError("probe_not_using_installed_bootstrapper")
    return os_agent, provision


async def _probe(args: argparse.Namespace) -> dict:
    os_agent, provision = _installed()
    from uplink.finder import find_central  # noqa: PLC0415
    from uplink.origin import Origin  # noqa: PLC0415
    from uplink.resolver import Configured  # noqa: PLC0415
    from uplink.transport import HttpTransport  # noqa: PLC0415
    from uplink.trust import Trust  # noqa: PLC0415

    args.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    boot_file = args.state / "boot-id"
    boot_file.write_text(args.boot_id + "\n")
    phase_file = args.state / "phase.json"
    transport = HttpTransport(trust=Trust.public())
    find = functools.partial(find_central, Configured(Origin.parse_root(args.root)),
                             transport=transport)
    if args.mode == "report":
        agent = os_agent.OsAgent(
            serial=args.serial, kernel_boot_id=args.boot_id, find=find,
            transport=transport,
            sequence=os_agent.ObservationSequence(args.state / "sequence.json"),
            phase_path=phase_file, handoff_path=args.state / "absent-handoff")
        receipt = await agent.report_once()
        if not receipt.accepted:
            raise RuntimeError("packaged_os_check_in_not_accepted")
        sequence = json.loads((args.state / "sequence.json").read_text())["sequence"]
        return {"mode": "report", "accepted": True, "sequence": sequence,
                "phase": os_agent.read_phase(args.boot_id, path=phase_file)[0]}

    seen = {"manifest": False, "package": False}

    def manifest(fetch, serial):
        result = provision.fetch_manifest(fetch, serial)
        seen["manifest"] = True
        return result

    def package(fetch, app_manifest):
        seen["package"] = True
        return provision.fetch_package(fetch, app_manifest)

    def forbidden(*_args, **_kwargs):
        raise RuntimeError("package_failure_reached_install_or_start")

    async def no_sleep(_seconds):
        return None

    bootstrapper = provision.Bootstrapper(
        find=find, transport=transport, clock=None,
        fetch_manifest=manifest, fetch_package=package,
        install=forbidden, write_handoff=forbidden, start_unit=forbidden,
        sleep=no_sleep, watchdog_extend=lambda _seconds: True,
        watchdog_ready=lambda: True,
        phase=lambda phase, digest, fault: os_agent.write_phase(
            phase, digest, fault, path=phase_file, boot_id_path=boot_file))
    outcome = await bootstrapper.run(max_attempts=1)
    phase, digest, fault = os_agent.read_phase(args.boot_id, path=phase_file)
    if outcome or not all(seen.values()) or (phase, fault) != ("retry_wait", "app_integrity"):
        raise RuntimeError("packaged_corrupt_package_not_refused")
    return {"mode": "refuse-package", "refused": True, "phase": phase,
            "fault": fault, "attempted_sha256": digest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("report", "refuse-package"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--boot-id", required=True)
    parser.add_argument("--state", type=Path, required=True)
    print(json.dumps(asyncio.run(_probe(parser.parse_args())), sort_keys=True))


if __name__ == "__main__":
    main()
