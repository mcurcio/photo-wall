"""Central-only source/transport models; never part of the Player protocol."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID

import httpx
from pydantic import (
    AfterValidator,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    SerializerFunctionWrapHandler,
    StrictBool,
    field_validator,
    model_serializer,
    model_validator,
)

from central.catalog import Candidate, CatalogSnapshot
from contracts.models import Digest, Identifier, Instant, Model, Positive

Status = Literal["unavailable", "permission", "incompatible"]
Kind = Literal["image", "video"]
Sha1 = Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
Count = Annotated[int, Field(ge=0, strict=True)]


def canonical_uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid UUID") from None


# A library tag's id; tag ids may reach the browser (R22, PR 37 Q4).
TagRef = Annotated[str, Field(max_length=36), AfterValidator(canonical_uuid)]
MAX_SOURCE_TAGS = 4
PREVIEW_SHOWN = 24


class SourceQuery(Model):
    """One library query. Media matches every tag in `tags` (and their nested tags).

    Empty `tags` are omitted when serialized, so an untagged query stores exactly as it
    did before tags existed, and an older worker (`extra='forbid'`) refuses a tagged one.
    """

    model_config = ConfigDict(populate_by_name=True)
    connection_ref: Identifier
    favorites: StrictBool | None = None
    captured_from: Instant | None = None
    captured_until: Instant | None = None
    media_types: tuple[Kind, ...] = ("image", "video")
    tags: tuple[TagRef, ...] = Field(default=(), max_length=MAX_SOURCE_TAGS)

    @field_validator("tags")
    @classmethod
    def unique_sorted_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("tags must be unique")
        return tuple(sorted(value))

    @model_serializer(mode="wrap")
    def omit_empty_tags(self, handler: SerializerFunctionWrapHandler) -> dict:
        data = handler(self)
        if not self.tags:
            data.pop("tags", None)
        return data

    def canonical(self) -> dict:
        """The query's meaning, for comparison only (never stored): kinds as a set, unset filters dropped."""
        data = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        data["media_types"] = sorted(data["media_types"])
        return data

    @field_validator("captured_from", "captured_until", mode="before")
    @classmethod
    def capture_instant(cls, value: object) -> object:
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("capture limit must be a UTC instant")
            try:
                datetime.fromtimestamp(value, timezone.utc)
            except (ValueError, OverflowError, OSError):
                raise ValueError("capture instant out of range") from None
        return value

    @model_validator(mode="after")
    def scope(self) -> Self:
        if not self.media_types or len(set(self.media_types)) != len(self.media_types):
            raise ValueError("media types must be a nonempty unique subset")
        if (self.captured_from is not None and self.captured_until is not None
                and self.captured_from >= self.captured_until):
            raise ValueError("capture interval must increase")
        return self


class SourceSpec(SourceQuery):
    """A persistent Source identity plus its validated live query."""

    schema_version: Literal[1] = Field(default=1, alias="schema")
    source_ref: Identifier


class SourcePreviewQuery(SourceQuery):
    """An unsaved query that may be evaluated without a persistent Source."""


# Control characters, bidi controls (LRM, RLM, ALM, embeddings, isolates) and zero-width
# characters never reach the console (PR 37 §9): a tag name may otherwise reorder the text
# around it, or read as empty while not being so. Stripped before any length check.
_UNSAFE_TEXT = re.compile(
    "[\x00-\x1f\x7f-\x9f\u061c\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
MAX_LIBRARY_TAGS = 5000
MAX_TAG_TEXT = 1024


def visible_text(value: object) -> object:
    return _UNSAFE_TEXT.sub("", value) if isinstance(value, str) else value


# Empty when nothing visible is left once stripped: the tag still exists in the library, so
# the stored list keeps its id (a by-id lookup must never call it gone); only the search
# hides it (`central/media_repository.py` `matching_tags`).
TagText = Annotated[str, BeforeValidator(visible_text), Field(max_length=MAX_TAG_TEXT)]


class LibraryTag(Model):
    """A tag as the browser may receive it (R22): ids, path and name only.

    The listed list is the library's whole list: every listed id is kept, even one whose
    path or name has nothing visible (`""`), which the search never offers.
    """

    tag_ref: TagRef
    path: TagText  # the full nested path, e.g. "Family/Christmas"
    name: TagText
    parent_ref: TagRef | None = None


class PreviewMember(Model):
    """A preview member as the browser may receive it (R22): no library id or checksum.

    Sizes and duration are absent when the library's metadata for the item is unusable.
    """

    asset_id: Identifier
    kind: Kind
    captured_at: Instant
    width: int | None = Field(default=None, gt=0, strict=True)
    height: int | None = Field(default=None, gt=0, strict=True)
    duration_seconds: Positive | None = None


class StoredPreviewMember(PreviewMember):
    """A served member plus the library identity Central keeps and never serves."""

    upstream_id: str
    checksum: Sha1

    @field_validator("upstream_id")
    @classmethod
    def upstream_uuid(cls, value: str) -> str:
        return canonical_uuid(value)

    def served(self) -> PreviewMember:
        return PreviewMember.model_validate(self.model_dump(include=set(PreviewMember.model_fields)))


class SourcePreviewResult(Model):
    """Counts of what a query selects, and its newest members.

    `limited`: the query selects more than the worker currently accepts (its 1,000
    ceiling); the counts are then lower bounds and `shown` is the newest observed.
    """

    count: Count
    image_count: Count
    video_count: Count
    shown: tuple[PreviewMember, ...] = Field(default=(), max_length=PREVIEW_SHOWN)
    limited: StrictBool = False

    @model_validator(mode="after")
    def totals_match(self) -> Self:
        if self.count != self.image_count + self.video_count:
            raise ValueError("preview counts do not add up")
        return self


class SourcePreview(Model):
    """A preview observation: the served result and its members' stored metadata."""

    result: SourcePreviewResult
    members: tuple[StoredPreviewMember, ...] = Field(default=(), max_length=PREVIEW_SHOWN)

    @model_validator(mode="after")
    def members_are_shown(self) -> Self:
        if tuple(member.served() for member in self.members) != self.result.shown:
            raise ValueError("stored members must be the shown members")
        return self


class ConnectionConfig(Model):
    model_config = ConfigDict(hide_input_in_errors=True)
    connection_id: Identifier
    base_url: str = Field(max_length=2048)
    owner_id: str
    api_key: SecretStr = Field(repr=False, exclude=True)
    allow_http: StrictBool = False
    ca_file: Path | None = None

    @field_validator("owner_id")
    @classmethod
    def owner_uuid(cls, value: str) -> str:
        return canonical_uuid(value)

    @field_validator("api_key")
    @classmethod
    def header_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if not 1 <= len(secret) <= 4096 or any(not 33 <= ord(c) <= 126 for c in secret):
            raise ValueError("invalid API key")
        return value

    @model_validator(mode="after")
    def origin(self) -> Self:
        try:
            url = httpx.URL(self.base_url)
        except httpx.InvalidURL:
            raise ValueError("invalid API base") from None
        if (any(ord(c) < 33 or ord(c) == 127 for c in self.base_url)
                or url.scheme not in ("https", "http") or not url.host
                or (url.scheme == "http" and not self.allow_http)
                or url.userinfo or "?" in self.base_url or "#" in self.base_url
                or url.raw_path not in (b"/api", b"/api/")):
            raise ValueError("API base requires a trusted HTTP(S) origin and /api path")
        return self


class MediaLimits(Model):
    page_size: int = Field(default=100, ge=1, le=1000, strict=True)
    max_candidates: int = Field(default=1000, ge=1, le=1000, strict=True)
    max_search_requests: int = Field(default=40, ge=1, strict=True)
    max_examined_rows: int = Field(default=4000, ge=1, strict=True)
    max_json_bytes: int = Field(default=2 * 1024**2, ge=1, strict=True)
    max_refresh_bytes: int = Field(default=32 * 1024**2, ge=1, strict=True)
    refresh_seconds: Positive = 60
    metadata_seconds: Positive = 15
    connect_seconds: Positive = 3
    read_seconds: Positive = 10
    original_seconds: Positive = 120
    max_original_bytes: int = Field(default=256 * 1024**2, ge=1, strict=True)
    max_pixels: int = Field(default=64_000_000, ge=1, strict=True)
    max_dimension: int = Field(default=16384, ge=1, strict=True)
    max_video_seconds: Positive = 300
    max_diagnostics: int = Field(default=128, ge=1, strict=True)


def asset_identity(connection_id: str, upstream_id: str, original_sha1: str) -> str:
    identity = json.dumps([connection_id, upstream_id, original_sha1], separators=(",", ":"))
    return "asset-" + hashlib.sha256(identity.encode()).hexdigest()


class OriginalAsset(Model):
    connection_id: Identifier
    upstream_id: str
    original_sha1: Sha1
    kind: Kind
    raw_width: int = Field(gt=0, strict=True)
    raw_height: int = Field(gt=0, strict=True)
    orientation: int = Field(ge=1, le=8, strict=True)
    captured_at: Instant
    file_size: int | None = Field(default=None, gt=0, strict=True)
    duration: Positive | None = None

    @field_validator("upstream_id")
    @classmethod
    def upstream_uuid(cls, value: str) -> str:
        return canonical_uuid(value)

    @model_validator(mode="after")
    def duration_matches_kind(self) -> Self:
        if (self.kind == "video") != (self.duration is not None):
            raise ValueError("only videos require duration")
        return self

    @property
    def asset_id(self) -> str:
        return asset_identity(self.connection_id, self.upstream_id, self.original_sha1)

    @property
    def original_width(self) -> int:
        return self.raw_height if self.orientation >= 5 else self.raw_width

    @property
    def original_height(self) -> int:
        return self.raw_width if self.orientation >= 5 else self.raw_height

    @property
    def candidate(self) -> Candidate:
        return Candidate(asset_id=self.asset_id, kind=self.kind, captured_at=self.captured_at,
                         original_width=self.original_width, original_height=self.original_height)


class Diagnostic(Model):
    code: str = Field(pattern=r"^[a-z_]{1,64}$")
    asset_id: Identifier | None = None


class RefreshCounts(Model):
    discovered: Count = 0
    valid: Count = 0
    pending: Count = 0
    rejected: Count = 0
    duplicates: Count = 0
    examined: Count = 0
    search_requests: Count = 0
    json_bytes: Count = 0


class RefreshResult(Model):
    snapshot: CatalogSnapshot
    assets: tuple[OriginalAsset, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    counts: RefreshCounts = RefreshCounts()


class DownloadedOriginal(Model):
    path: Path
    size: int = Field(gt=0, strict=True)
    sha1: Sha1
    sha256: Digest


class MediaError(Exception):
    """Only bounded codes cross the worker boundary; no chained transport exception."""

    def __init__(self, code: str, status: Status = "unavailable") -> None:
        if not re.fullmatch(r"[a-z_]{1,64}", code):
            raise ValueError("invalid media failure code")
        self.code, self.status = code, status
        super().__init__(code)
