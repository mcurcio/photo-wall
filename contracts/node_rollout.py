"""Canonical immutable CI qualification and live D17 evidence domains.

Hashes identify exact reports, not an operator's assertion of compatibility.
Signature verification and publication belong to infrastructure adapters.
"""
from __future__ import annotations

import json
import math
import re
from hashlib import sha256

from contracts.strict_json import loads_object

QUALIFICATION_DOMAIN = b"photo-wall-image-qualification-v1\0"
REVOCATION_DOMAIN = b"photo-wall-image-qualification-revocations-v1\0"
CI_LIVE_DOMAIN = b"photo-wall-rollout-ci-v1\0"
GUARD_LIVE_DOMAIN = b"photo-wall-rollout-guard-v1\0"
REPORT_CATEGORIES = ("compatibility_matrix", "fence_contract", "readiness_contract")
MAX_QUALIFICATION_BYTES = 131072
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"[a-z0-9][a-z0-9./:_-]+@sha256:[0-9a-f]{64}\Z")


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def document_hash(value) -> str:
    return sha256(canonical(value)).hexdigest()


def image_digest(reference: str) -> str:
    if type(reference) is not str or not _IMAGE.fullmatch(reference):
        raise ValueError("qualification_immutable_image_required")
    return reference.rsplit("@", 1)[1]


def _hex(value):
    if type(value) is not str or not _HEX.fullmatch(value):
        raise ValueError("qualification_hash_invalid")


def validate_qualification(value: dict) -> dict:
    if type(value) is not dict or set(value) != {"schema", "kind", "revision", "suite_sha256",
            "created_at", "expires_at", "images"} or value["schema"] != 1 or value["kind"] != "image_qualification":
        raise ValueError("qualification_shape_invalid")
    if type(value["revision"]) is not str or not re.fullmatch(r"[0-9a-f]{40}", value["revision"]):
        raise ValueError("qualification_revision_invalid")
    _hex(value["suite_sha256"])
    start, end = value["created_at"], value["expires_at"]
    if any(type(t) not in (int, float) or not math.isfinite(t) for t in (start, end)) or not 0 < end-start <= 90*86400:
        raise ValueError("qualification_validity_invalid")
    images = value["images"]
    if type(images) is not list or not 1 <= len(images) <= 128:
        raise ValueError("qualification_images_invalid")
    references = []
    for item in images:
        if type(item) is not dict or set(item) != {"reference", "image_config_id", "platform", "interpreter", "imports", "reports"}:
            raise ValueError("qualification_image_shape_invalid")
        image_digest(item["reference"])
        references.append(item["reference"])
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(item["image_config_id"])) or item["platform"] != "linux/amd64":
            raise ValueError("qualification_image_identity_invalid")
        if item["interpreter"] != "/app/.venv/bin/python":
            raise ValueError("qualification_interpreter_changed")
        imports = item["imports"]
        if type(imports) is not dict or set(imports) != {"central.app", "central.coordination", "central.fleet.node_lifecycle", "contracts.node_protocol"}:
            raise ValueError("qualification_imports_invalid")
        if any(path != "/app/"+name.replace(".", "/")+".py" for name, path in imports.items()):
            raise ValueError("qualification_source_shadowed")
        if type(item["reports"]) is not dict or set(item["reports"]) != set(REPORT_CATEGORIES):
            raise ValueError("qualification_reports_missing")
        for report in item["reports"].values():
            if type(report) is not dict or set(report) != {"sha256", "passed"}:
                raise ValueError("qualification_report_invalid")
            _hex(report["sha256"])
            if type(report["passed"]) is not int or report["passed"] < 1:
                raise ValueError("qualification_report_empty")
    if references != sorted(set(references)):
        raise ValueError("qualification_images_duplicate_or_unsorted")
    if len(canonical(value)) > MAX_QUALIFICATION_BYTES:
        raise ValueError("qualification_too_large")
    return value


def parse_qualification(raw: bytes) -> dict:
    value = loads_object(raw, max_bytes=MAX_QUALIFICATION_BYTES)
    return validate_qualification(value)
