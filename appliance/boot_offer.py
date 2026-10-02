"""Versioned, bounded PXE offer and volatile stage handoff values.

These values correlate an issued selection with an OS report. They never prove
physical device identity or authorize an OS command.
"""

from __future__ import annotations

import json
import math
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from contracts.player_payload import MAX_ARCHIVE_BYTES
from contracts.release import MAX_ROOTFS_BYTES
from contracts.strict_json import loads_object
from uplink.files import write_atomically

MAX_OFFER_BYTES = 4096
MAX_HANDOFF_BYTES = 4096
NONCE_NAME = "boot-nonce.json"
HANDOFF_RELATIVE = Path("etc/photo-wall/boot-handoff.json")
BOOT_HANDOFF = Path("/") / HANDOFF_RELATIVE
_SHA256 = re.compile(r"[0-9a-f]{64}")
_NONCE = re.compile(r"[0-9a-f]{32,64}")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_ABI = re.compile(r"sha256:[0-9a-f]{64}")
DATA_FORMAT = "pw-player-data-v1"
_APP_STATUSES = frozenset({"selected", "unconfigured", "unavailable",
                           "compatibility_unverified"})
_COMPATIBILITY = frozenset({"none", "co_release_unverified", "abi_match"})
_APP_SOURCES = frozenset({"override", "explicit", "legacy_promotion"})
_BASE_SOURCES = frozenset({"pin", "operator_baseline"})


class BootOfferError(ValueError):
    pass


def _hex(value: object, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise BootOfferError(code)
    return value


@dataclass(frozen=True, slots=True)
class BootAsset:
    sha256: str
    size: int
    tag: str | None
    format: str | None = None
    base_abi: str | None = None

    @classmethod
    def parse(cls, value: object, *, schema: int = 1, app: bool = False) -> BootAsset:
        if not isinstance(value, dict):
            raise BootOfferError("boot_offer_asset")
        size, tag = value.get("size"), value.get("tag")
        maximum = MAX_ARCHIVE_BYTES if app and schema == 2 else MAX_ROOTFS_BYTES
        if type(size) is not int or not 0 < size <= maximum:
            raise BootOfferError("boot_offer_size")
        if not isinstance(tag, str) or not 0 < len(tag) <= 128:
            raise BootOfferError("boot_offer_tag")
        if app and schema == 2:
            if value.get("format") != DATA_FORMAT:
                raise BootOfferError("boot_offer_format")
            abi = _hex(value.get("base_abi"), _ABI, "boot_offer_abi")
            return cls(_hex(value.get("sha256"), _SHA256, "boot_offer_digest"),
                       size, tag, DATA_FORMAT, abi)
        if "format" in value or "base_abi" in value:
            raise BootOfferError("boot_offer_format")
        return cls(_hex(value.get("sha256"), _SHA256, "boot_offer_digest"), size, tag)


@dataclass(frozen=True, slots=True)
class BootOffer:
    schema: int
    offer_id: str
    base: BootAsset
    initial_app: BootAsset | None
    initial_app_status: str
    compatibility_basis: str
    base_policy_source: str
    base_policy_revision: int
    app_policy_source: str
    app_policy_revision: int
    expires_at: float

    @classmethod
    def parse(cls, data: bytes) -> BootOffer:
        value = loads_object(data, max_bytes=MAX_OFFER_BYTES)
        if value is None or type(value.get("schema")) is not int or value["schema"] not in (1, 2):
            raise BootOfferError("boot_offer_schema")
        schema = value["schema"]
        offer_id = _hex(value.get("offer_id"), _UUID, "boot_offer_id")
        base_revision, app_revision = (value.get("base_policy_revision"),
                                       value.get("app_policy_revision"))
        if any(type(revision) is not int or revision < 0
               for revision in (base_revision, app_revision)):
            raise BootOfferError("boot_offer_revision")
        base_source, app_source = value.get("base_policy_source"), value.get("app_policy_source")
        status, basis = value.get("initial_app_status"), value.get("compatibility_basis")
        expiry = value.get("expires_at")
        if (not isinstance(base_source, str) or base_source not in _BASE_SOURCES
                or not isinstance(app_source, str) or app_source not in _APP_SOURCES
                or not isinstance(status, str) or status not in _APP_STATUSES
                or not isinstance(basis, str) or basis not in _COMPATIBILITY
                or type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0):
            raise BootOfferError("boot_offer_selection")
        initial = value.get("initial_app")
        if ((initial is None) != (status != "selected")
                or (basis == "none") != (initial is None)):
            raise BootOfferError("boot_offer_selection")
        return cls(schema, offer_id, BootAsset.parse(value.get("base"), schema=schema),
                   None if initial is None else BootAsset.parse(initial, schema=schema, app=True),
                   status, basis, base_source, base_revision, app_source, app_revision,
                   float(expiry))


def boot_nonce(run_root: Path, kernel_boot_id: str) -> str:
    """Persist one nonce per kernel boot; a damaged existing file cannot select anew."""
    _hex(kernel_boot_id, _UUID, "boot_id_invalid")
    path = run_root / NONCE_NAME
    try:
        with path.open("rb") as stream:
            raw = stream.read(257)
    except FileNotFoundError:
        if path.is_symlink():
            raise BootOfferError("boot_nonce_invalid") from None
        existing = None
    except OSError as exc:
        raise BootOfferError("boot_nonce_unavailable") from exc
    else:
        existing = loads_object(raw, max_bytes=256)
        if (existing is None or set(existing) != {"kernel_boot_id", "boot_nonce"}
                or not isinstance(existing["kernel_boot_id"], str)
                or _UUID.fullmatch(existing["kernel_boot_id"]) is None
                or not isinstance(existing["boot_nonce"], str)
                or _NONCE.fullmatch(existing["boot_nonce"]) is None):
            raise BootOfferError("boot_nonce_invalid")
        if existing["kernel_boot_id"] == kernel_boot_id:
            return existing["boot_nonce"]
    nonce = secrets.token_hex(16)
    write_atomically(path, json.dumps({"kernel_boot_id": kernel_boot_id,
                                       "boot_nonce": nonce}, sort_keys=True).encode(), mode=0o600)
    return nonce


def write_handoff(rootmnt: Path, *, kernel_boot_id: str, nonce: str,
                  base_digest: str, offer: BootOffer | None) -> Path:
    """Write into the mounted new root; do not assume initramfs /run crosses switch_root."""
    _hex(kernel_boot_id, _UUID, "boot_id_invalid")
    _hex(nonce, _NONCE, "boot_nonce_invalid")
    _hex(base_digest, _SHA256, "base_digest_invalid")
    if offer is not None and offer.base.sha256 != base_digest:
        raise BootOfferError("boot_handoff_base_mismatch")
    payload = {"schema": offer.schema if offer else 1,
               "kernel_boot_id": kernel_boot_id, "boot_nonce": nonce,
               "base_digest": base_digest,
               "base_tag": offer.base.tag if offer else None,
               "mode": "offer" if offer else "legacy_uncorrelated",
               "offer_id": offer.offer_id if offer else None,
               "initial_app_status": offer.initial_app_status if offer else None,
               "compatibility_basis": offer.compatibility_basis if offer else None,
               "app_policy_source": offer.app_policy_source if offer else None,
               "app_policy_revision": offer.app_policy_revision if offer else None,
               "initial_app": None if offer is None or offer.initial_app is None else {
                   "sha256": offer.initial_app.sha256, "size": offer.initial_app.size,
                   "tag": offer.initial_app.tag,
                   **({"format": offer.initial_app.format,
                       "base_abi": offer.initial_app.base_abi}
                      if offer.schema == 2 else {})}}
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if len(data) > MAX_HANDOFF_BYTES:
        raise BootOfferError("boot_handoff_limit")
    path = rootmnt / HANDOFF_RELATIVE
    write_atomically(path, data, mode=0o600)
    return path


def read_handoff(path: Path) -> dict[str, object] | None:
    """Read a syntactically valid correlation claim, without boot-age inference."""
    try:
        value = loads_object(path.read_bytes(), max_bytes=MAX_HANDOFF_BYTES)
    except OSError:
        return None
    if value is None or value.get("schema") not in (1, 2):
        return None
    try:
        _hex(value.get("kernel_boot_id"), _UUID, "boot_id_invalid")
        _hex(value.get("boot_nonce"), _NONCE, "boot_nonce_invalid")
        _hex(value.get("base_digest"), _SHA256, "base_digest_invalid")
        base_tag = value.get("base_tag")
        if (value.get("schema") == 2 and value.get("mode") == "offer"
                and (not isinstance(base_tag, str) or not 0 < len(base_tag) <= 128)):
            return None
        if base_tag is not None and (not isinstance(base_tag, str)
                                     or not 0 < len(base_tag) <= 128):
            return None
        if value.get("mode") == "offer":
            _hex(value.get("offer_id"), _UUID, "boot_offer_id")
            status, basis = value.get("initial_app_status"), value.get("compatibility_basis")
            if (not isinstance(status, str) or status not in _APP_STATUSES
                    or not isinstance(basis, str) or basis not in _COMPATIBILITY
                    or not isinstance(value.get("app_policy_source"), str)
                    or value["app_policy_source"] not in _APP_SOURCES
                    or type(value.get("app_policy_revision")) is not int
                    or value["app_policy_revision"] < 0
                    or (value.get("initial_app") is None) != (status != "selected")
                    or (basis == "none") != (value.get("initial_app") is None)):
                return None
            if value.get("initial_app") is not None:
                BootAsset.parse(value["initial_app"], schema=value["schema"], app=True)
        elif (value.get("schema") != 1 or value.get("mode") != "legacy_uncorrelated"
              or value.get("offer_id") is not None):
            return None
    except BootOfferError:
        return None
    return value


def read_current_handoff(path: Path, kernel_boot_id: str) -> dict[str, object] | None:
    """Missing means old stage one; a present bad or prior-boot file is never legacy."""
    _hex(kernel_boot_id, _UUID, "boot_id_invalid")
    handoff = read_handoff(path)
    if handoff is None:
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise BootOfferError("boot_handoff_invalid") from exc
        raise BootOfferError("boot_handoff_invalid")
    if handoff["kernel_boot_id"] != kernel_boot_id:
        raise BootOfferError("boot_handoff_stale")
    return handoff
