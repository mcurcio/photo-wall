"""Fetch and authenticate the fixed appliance input into an external artifact cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

BASE = "https://cdimage.ubuntu.com/ubuntu/releases/24.04/release/"
IMAGE = "ubuntu-24.04.4-preinstalled-server-arm64+raspi.img.xz"
DIGEST = "790652faeb4f61ce7bb12f5cb61734595c61d3cd882915b8b5f9918106c80d37"
SIGNER = "843938DF228D22F7B3742BC0D94AA3F0EFE21092"


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
    keyhome.mkdir(mode=0o700, exist_ok=True)
    gpg = ["gpg", "--batch", "--homedir", str(keyhome)]
    subprocess.run([*gpg, "--import", str(key)], check=True, capture_output=True, timeout=30)
    result = subprocess.run([*gpg, "--status-fd", "1", "--verify",
                             str(root / "SHA256SUMS.gpg"), str(root / "SHA256SUMS")],
                            check=True, capture_output=True, text=True, timeout=30)
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
