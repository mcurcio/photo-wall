#!/usr/bin/env python3
"""Sign/verify immutable exact-image CI evidence without changing release assets.

Private keys are explicit filesystem inputs from a separately provisioned CI
identity. No key generation, deployment mutation or positive unsigned fallback.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from central.fleet.node_ci_evidence import report_result, sign, verify  # noqa: E402,F401
from contracts.node_rollout import parse_qualification  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("sign", "verify"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    key_bytes = bytes.fromhex(args.key.read_text().strip())
    if args.operation == "sign":
        if args.output is None:
            parser.error("sign requires --output")
        payload = parse_qualification(args.input.read_bytes())
        args.output.write_bytes(sign(payload, Ed25519PrivateKey.from_private_bytes(key_bytes), args.reports))
    else:
        payload = verify(args.input.read_bytes(), Ed25519PublicKey.from_public_bytes(key_bytes), args.reports)
        print(json.dumps({"verified_images": [image["reference"] for image in payload["images"]]}))


if __name__ == "__main__":
    main()
