"""`MediaMaker`: the `PrepareMedia` handler (follow-up to 5a, slice T). It makes, or re-makes, one
media variant from its name alone (`<asset_id>.<recipe_id>`): the photo revision and the recipe
and need that name stands for (`MediaRepository.variant_inputs`), the smallest of the library's
copies that holds the need (`fetch_plan`), encoded by the recipe (`Preparer`), and placed at
`media/blobs/<sha256>` without ever overwriting a path (`CacheStore.install_by_digest`). It
returns the facts; the job runtime writes them onto the variant's record with the outcome, so a
re-make's facts replace the record's (`AssetKind.MEDIA_VARIANT.keyed_by_content` is False).

A variant whose file is on disk and whole is not made again. One whose only reference is the
legacy owner (migration 087) has no recipe to make it from: it is served while its file lasts and
otherwise answers `legacy_variant`. Nothing is upscaled (`Preparer._size`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final, Protocol

from central.assets.frame_media import LEGACY_OWNER
from central.assets.store import CacheStore
from central.kernel.assets import Asset, MediaReady
from central.kernel.handling import TerminalFailure, TransientFailure
from central.kernel.job_types import PrepareMedia
from central.kernel.jobs import asset_key
from central.kernel.ports import AssetRecords
from central.kernel.publishing import ASSET_NOT_RECORDED
from central.kernel.transactions import Transactions
from central.kernel.types import split_media_variant_key
from central.media_repository import VariantInputs
from central.photo_library.model import ConnectionSecret, LibraryAsset
from central.photo_library.ports import LibraryWorkerStore
from media.library_client import FetchedRendition, LibraryClient, LibraryUnavailable, fetch_plan
from media.models import MediaError
from media.prepare import STILL_MEDIA_TYPES, PreparationRecipe, PreparedMedia

LEGACY_VARIANT: Final = "legacy_variant"
VARIANT_INPUTS_UNKNOWN: Final = "variant_inputs_unknown"
# Failures no retry can mend: the photo itself (its type, its colour, its size) or what Photo
# Wall made of it. Any other failure is retried by the job's delivery.
PERMANENT: Final = frozenset({
    "asset_integrity", "asset_oversize", "unsupported_media", "unsupported_color",
    "metadata_invalid", "item_over_limits", "metadata_mismatch", "preparation_limit",
    "preparation_invalid",
})


class PreparationClient(Protocol):
    async def prepare_rendition(self, asset: LibraryAsset, rendition: FetchedRendition,
                                destination: Path, *,
                                recipe: PreparationRecipe | None = None) -> PreparedMedia: ...


async def _blocking(function, *args):
    return await asyncio.to_thread(function, *args)


class MediaMaker:
    """Implements `Handler[PrepareMedia, MediaReady]`."""

    def __init__(self, *, store: CacheStore, records: AssetRecords, transactions: Transactions,
                 inputs: Callable[[str], VariantInputs | None], library: LibraryWorkerStore,
                 client: Callable[[ConnectionSecret], Awaitable[LibraryClient]],
                 preparer: PreparationClient, job_seconds: float = 330) -> None:
        self._store = store
        self._records = records
        self._transactions = transactions
        self._inputs = inputs  # blocking
        self._library = library
        self._client = client
        self._preparer = preparer
        self._job_seconds = job_seconds

    async def handle(self, job: PrepareMedia) -> MediaReady:
        key = asset_key(job)
        asset = await _blocking(self._get, job)
        if asset is None:
            # Transient: the record is catalog state a later plan re-creates (PB7).
            raise TransientFailure(ASSET_NOT_RECORDED)
        produced = asset.produced
        if (isinstance(produced, MediaReady)
                and await _blocking(self._store.verified, key, produced)):
            return produced
        if all(reference.owner == LEGACY_OWNER for reference in asset.references):
            raise TerminalFailure(LEGACY_VARIANT)
        inputs = await _blocking(self._inputs, job.key)
        if inputs is None:
            raise TerminalFailure(VARIANT_INPUTS_UNKNOWN)
        try:
            async with asyncio.timeout(self._job_seconds):
                return await self._make(job, inputs)
        except TimeoutError:
            raise TransientFailure("worker_timeout") from None
        except LibraryUnavailable as error:
            raise TransientFailure(error.problem.kind) from None
        except MediaError as error:
            if error.code in PERMANENT or error.status == "incompatible":
                raise TerminalFailure(error.code) from None
            raise TransientFailure(error.code) from None

    async def _make(self, job: PrepareMedia, inputs: VariantInputs) -> MediaReady:
        key = asset_key(job)
        photo = inputs.asset
        asset_id, _ = split_media_variant_key(job.key)
        secret = await _blocking(self._library.connection_secret, photo.connection_ref)
        if secret is None:
            raise MediaError("connection_unknown")
        client = await self._client(secret)
        needs = (inputs.need,)
        known = await _blocking(self._library.copy_sizes, asset_id)
        plan = fetch_plan(photo, needs, known, STILL_MEDIA_TYPES)
        variant = await _blocking(self._store.temp_path, key)
        original = variant.with_name(variant.name + ".original")
        try:
            fetched = await client.fetch_rendition(photo, plan=plan, needs=needs,
                                                   destination=original)
            await _blocking(self._library.record_copies, asset_id,
                            fetched.measured)
            if fetched.path.absolute() != original.absolute():
                raise MediaError("asset_integrity")
            prepared = await self._preparer.prepare_rendition(photo, fetched, variant,
                                                              recipe=inputs.recipe)
            if prepared.original_sha256 != fetched.sha256:
                raise MediaError("asset_integrity")
            facts = await _blocking(self._store.measure, variant)
            made = prepared.variant
            if (facts.sha256, facts.size) != (made.sha256, made.size):
                raise MediaError("asset_integrity")
            ready = MediaReady(size=facts.size, sha256=facts.sha256, media_type=made.media_type,
                               width=made.width, height=made.height, duration=made.duration,
                               made_from=prepared.made_from)
            await _blocking(self._store.install_by_digest, variant, key, ready)
            return ready
        finally:
            self._store.discard(variant)
            self._store.discard(original)

    def _get(self, job: PrepareMedia) -> Asset | None:
        with self._transactions.begin() as tx:
            return self._records.get(tx, asset_key(job))
