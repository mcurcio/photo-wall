"""Fetch and authenticate the fixed appliance input into an external artifact cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = "https://cdimage.ubuntu.com/ubuntu/releases/24.04/release/"
IMAGE = "ubuntu-24.04.4-preinstalled-server-arm64+raspi.img.xz"
DIGEST = "790652faeb4f61ce7bb12f5cb61734595c61d3cd882915b8b5f9918106c80d37"
SIGNER = "843938DF228D22F7B3742BC0D94AA3F0EFE21092"
GPG_DIAGNOSTIC_LIMIT = 4096


def _gpg(keyhome: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run public-input verification without starting an unavailable agent."""
    command = ["gpg", "--batch", "--no-autostart", "--homedir", str(keyhome), *arguments]
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
    except subprocess.CalledProcessError as error:
        detail = error.stderr or ""
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        if detail:
            bounded = detail[-GPG_DIAGNOSTIC_LIMIT:]
            print(bounded, file=sys.stderr, end="" if bounded.endswith("\n") else "\n")
        raise ValueError("signature_tool_failed") from None
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("signature_tool_failed") from None


def fetch(url: str, path: Path, maximum: int):
    if path.exists():
        if not path.is_file() or path.is_symlink() or path.stat().st_size > maximum:
            raise ValueError("invalid_cached_input")
        return
    partial = path.with_suffix(path.suffix + ".partial")
    deadline, total = time.monotonic() + 3600, 0
    created = False
    try:
        with urllib.request.urlopen(url, timeout=60) as response, partial.open("xb") as output:
            created = True
            while block := response.read(1024 * 1024):
                total += len(block)
                if total > maximum or time.monotonic() > deadline:
                    raise ValueError("input_download_limit")
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        partial.rename(path)
    except BaseException:
        if created:
            partial.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory.absolute()
    root.mkdir(parents=True, exist_ok=True)
    for name in ("SHA256SUMS", "SHA256SUMS.gpg"):
        fetch(BASE + name, root / name, 1024 * 1024)
    key = root / "ubuntu-cdimage.asc"
    fetch("https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x" + SIGNER, key, 1024 * 1024)
    keyhome = root / "verification-keyring"
    if keyhome.is_symlink():
        raise ValueError("invalid_verification_keyring")
    keyhome.mkdir(mode=0o700, exist_ok=True)
    keyhome.chmod(0o700)
    _gpg(keyhome, "--import", str(key))
    result = _gpg(keyhome, "--status-fd", "1", "--verify",
                  str(root / "SHA256SUMS.gpg"), str(root / "SHA256SUMS"))
    if not any(line.startswith("[GNUPG:] VALIDSIG " + SIGNER + " ")
               for line in result.stdout.splitlines()):
        raise ValueError("unexpected_image_signer")
    expected = f"{DIGEST} *{IMAGE}"
    if expected not in (root / "SHA256SUMS").read_text().splitlines():
        raise ValueError("pinned_image_checksum_not_signed")
    print("Canonical signature verified; fetching pinned Raspberry Pi image.", flush=True)
    image = root / IMAGE
    fetch(BASE + IMAGE, image, 2 * 1024**3)
    with image.open("rb") as input_file:
        actual = hashlib.file_digest(input_file, "sha256").hexdigest()
    if actual != DIGEST:
        raise ValueError("image_checksum_mismatch")
    evidence = dict(image=IMAGE, sha256=actual, size=image.stat().st_size, signer=SIGNER,
                    checksums_sha256=hashlib.sha256((root / "SHA256SUMS").read_bytes()).hexdigest())
    (root / "verified-input.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence), flush=True)


if __name__ == "__main__":
    main()
