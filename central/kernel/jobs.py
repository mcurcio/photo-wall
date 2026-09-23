"""Job types: one pydantic model per real piece of work, checked when the class is defined.

A job type declares its name, delivery, subject and (for asset jobs) asset kind as class
keywords. Every rule a declaration can break raises `TypeError` at class definition, and a type
is registered only after all rules pass. Keys are derived here and never written by callers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum, StrEnum
from typing import Any, ClassVar, Final, Generic, TypeVar

from pydantic import BaseModel, ConfigDict

from central.kernel.assets import AssetKey, AssetKind, AssetReady


class QueueName(StrEnum):
    FETCH = "photo-wall-fetch"
    UPKEEP = "photo-wall-upkeep"  # TRANSCODE arrives with media


# Exactly the cadences a seconds-last croniter expression can say (infra maps them to cron).
PERIODIC_CADENCES: Final[frozenset[timedelta]] = frozenset(
    [timedelta(minutes=m) for m in (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30)]
    + [timedelta(hours=h) for h in (1, 2, 3, 4, 6, 8, 12, 24)]
)
_MAX_RETRY_DELAY = timedelta(days=1)
_MAX_PRIORITY = 100


@dataclass(frozen=True, slots=True)
class Delivery:
    queue: QueueName
    retry: tuple[timedelta, ...] = ()  # backoff delays; () = no retry; each 0 < d <= 1 day
    priority: int = 0  # -100..100
    every: timedelta | None = None  # periodic; must be in PERIODIC_CADENCES

    def __post_init__(self) -> None:
        if not isinstance(self.queue, QueueName):
            raise ValueError("invalid_queue")
        if not isinstance(self.retry, tuple) or not all(
            isinstance(delay, timedelta) and timedelta(0) < delay <= _MAX_RETRY_DELAY
            for delay in self.retry
        ):
            raise ValueError("invalid_retry")
        if type(self.priority) is not int or not -_MAX_PRIORITY <= self.priority <= _MAX_PRIORITY:
            raise ValueError("invalid_priority")
        if self.every is not None and self.every not in PERIODIC_CADENCES:
            raise ValueError("invalid_every")


R = TypeVar("R")

_NAME = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
_MAX_NAME_LENGTH = 64
_RESERVED_MEMBERS: Final = frozenset({
    "job_name", "delivery", "subject", "asset_kind", "result_type",
    "model_config", "__init_subclass__", "__pydantic_init_subclass__",
})
_RESERVED_FIELDS: Final = frozenset({"timestamp"})  # the procrastinate mapping reserves it
_KEYABLE_SCALARS: Final = (str, int, bool)

_REGISTRY: dict[str, type[Job[Any]]] = {}
_ASSET_CLAIMS: dict[AssetKind, type[Job[Any]]] = {}


class Job(BaseModel, Generic[R]):
    """Base of every job type; `R` is the handler's result type."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_name: ClassVar[str]
    delivery: ClassVar[Delivery]
    subject: ClassVar[tuple[str, ...]]
    asset_kind: ClassVar[AssetKind | None]
    result_type: ClassVar[type[Any] | None]  # None <=> Job[None]

    def __init_subclass__(
        cls, *, name: str | None = None, delivery: Delivery | None = None,
        subject: tuple[str, ...] | None = None, asset: AssetKind | None = None, **kwargs: Any,
    ) -> None:
        # Swallow the job keywords so object.__init_subclass__ does not reject them; pydantic
        # passes them again to __pydantic_init_subclass__ once the fields are built.
        super().__init_subclass__(**kwargs)

    @classmethod
    def __pydantic_init_subclass__(
        cls, *, name: str | None = None, delivery: Delivery | None = None,
        subject: tuple[str, ...] | None = None, asset: AssetKind | None = None, **kwargs: Any,
    ) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        if cls.__pydantic_generic_metadata__["origin"] is not None:
            return  # a parametrized intermediate such as Job[AssetReady]; not a job type
        result_type = _result_type(cls)
        _check_name(name)
        if not isinstance(delivery, Delivery):
            raise TypeError(f"{cls.__name__}: delivery must be a Delivery")
        _check_members(cls)
        fields = tuple(cls.model_fields)
        for field in fields:
            _check_keyable(cls, field)
        resolved_subject = _check_subject(cls, subject, fields)
        if delivery.every is not None and fields:
            raise TypeError(f"{cls.__name__}: a periodic job type has no fields")
        _check_asset(cls, asset, result_type, resolved_subject, delivery)

        cls.job_name = name  # type: ignore[assignment]
        cls.delivery = delivery
        cls.subject = resolved_subject
        cls.asset_kind = asset
        cls.result_type = result_type
        _REGISTRY[name] = cls  # type: ignore[index]
        if asset is not None:
            _ASSET_CLAIMS[asset] = cls


def _result_type(cls: type[Job[Any]]) -> type[Any] | None:
    """Check 1: the single base is a parametrized Job[...]; return its R."""
    bases = cls.__bases__
    if len(bases) != 1:
        raise TypeError(f"{cls.__name__}: a job type has exactly one base, Job[R]")
    metadata = getattr(bases[0], "__pydantic_generic_metadata__", None)
    if metadata is None or metadata["origin"] is not Job or len(metadata["args"]) != 1:
        raise TypeError(f"{cls.__name__}: subclass a parametrized Job[R], not {bases[0].__name__}")
    (arg,) = metadata["args"]
    if arg is None or arg is type(None):
        return None
    if isinstance(arg, TypeVar):
        raise TypeError(f"{cls.__name__}: Job[R] needs a concrete result type")
    return arg


def _check_name(name: object) -> None:
    """Check 2."""
    if not isinstance(name, str) or _NAME.fullmatch(name) is None or len(name) > _MAX_NAME_LENGTH:
        raise TypeError(f"invalid job name {name!r}")
    if name in _REGISTRY:
        raise TypeError(f"job name {name!r} is already registered")


def _check_members(cls: type[Job[Any]]) -> None:
    """Check 4. Pydantic always stores the merged `model_config` on the class, so a redefinition
    is detected as a config that differs from the base's."""
    own = set(vars(cls)) | set(vars(cls).get("__annotations__", {})) | set(cls.model_fields)
    own.discard("model_config")
    clash = sorted(own & _RESERVED_MEMBERS)
    if clash:
        raise TypeError(f"{cls.__name__}: redefines Job members {clash}")
    if cls.model_config != cls.__bases__[0].model_config:  # type: ignore[attr-defined]
        raise TypeError(f"{cls.__name__}: redefines Job members ['model_config']")
    reserved = sorted(set(cls.model_fields) & _RESERVED_FIELDS)
    if reserved:
        raise TypeError(f"{cls.__name__}: reserved field names {reserved}")


def _check_keyable(cls: type[Job[Any]], field: str) -> None:
    """Check 5: str, int, bool, or an Enum whose values are all str or all int."""
    annotation = cls.model_fields[field].annotation
    if annotation in _KEYABLE_SCALARS:
        return
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        values = [member.value for member in annotation]
        if values and (all(isinstance(v, str) for v in values)
                       or all(type(v) is int for v in values)):
            return
    raise TypeError(f"{cls.__name__}: field {field!r} has an unkeyable type {annotation!r}")


def _check_subject(
    cls: type[Job[Any]], subject: object, fields: tuple[str, ...],
) -> tuple[str, ...]:
    """Check 6. `None` means every field, in declaration order."""
    if subject is None:
        return fields
    if not isinstance(subject, tuple) or not all(isinstance(f, str) for f in subject):
        raise TypeError(f"{cls.__name__}: subject must be a tuple of field names")
    unknown = [f for f in subject if f not in fields]
    if unknown:
        raise TypeError(f"{cls.__name__}: subject names unknown fields {unknown}")
    if len(set(subject)) != len(subject):
        raise TypeError(f"{cls.__name__}: subject repeats a field")
    return subject


def _check_asset(
    cls: type[Job[Any]], asset: object, result_type: type[Any] | None,
    subject: tuple[str, ...], delivery: Delivery,
) -> None:
    """Check 8: the result type and the asset kind agree."""
    if asset is None:
        if result_type is not None:
            raise TypeError(f"{cls.__name__}: a job with a result must declare asset=")
        return
    if not isinstance(asset, AssetKind):
        raise TypeError(f"{cls.__name__}: asset must be an AssetKind")
    if not (isinstance(result_type, type) and issubclass(result_type, AssetReady)):
        raise TypeError(f"{cls.__name__}: an asset job's result must be AssetReady")
    if delivery.every is not None:
        raise TypeError(f"{cls.__name__}: an asset job is not periodic")
    if len(subject) != 1:
        raise TypeError(f"{cls.__name__}: an asset job's subject is exactly one field")
    if asset in _ASSET_CLAIMS:
        raise TypeError(f"{cls.__name__}: asset kind {asset.value!r} is already claimed")


@dataclass(frozen=True, slots=True)
class JobKeys:
    lock: str  # one RUNNING copy fleet-wide; also the job_outcomes key + NOTIFY payload
    queueing_lock: str  # one PENDING copy


def _registered(job: object) -> type[Job[Any]]:
    job_type = type(job)
    name = getattr(job_type, "job_name", None)
    if not isinstance(job, Job) or name is None or _REGISTRY.get(name) is not job_type:
        raise TypeError(f"{job_type.__name__} is not a registered job type")
    return job_type


def _encode(values: list[Any]) -> str:
    return json.dumps(values, separators=(",", ":"), ensure_ascii=False)


def job_keys(job: Job[Any]) -> JobKeys:
    """Job name + a JSON array of values; a name cannot contain `[`, so both are injective."""
    job_type = _registered(job)
    payload = job.model_dump(mode="json")
    return JobKeys(
        lock=job_type.job_name + _encode([payload[f] for f in job_type.subject]),
        queueing_lock=job_type.job_name + _encode([payload[f] for f in job_type.model_fields]),
    )


def asset_key(job: Job[Any]) -> AssetKey:
    """The disk key of an asset job; it names the same thing as the job's lock."""
    job_type = _registered(job)
    if job_type.asset_kind is None:
        raise TypeError(f"{job_type.__name__} is not an asset job type")
    payload = job.model_dump(mode="json")
    return AssetKey(job_type.asset_kind, str(payload[job_type.subject[0]]))


def registered_job_type(name: str) -> type[Job[Any]]:
    return _REGISTRY[name]
