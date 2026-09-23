from __future__ import annotations

from datetime import timedelta
from enum import Enum, StrEnum
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ConfigDict, ValidationError

from central.kernel import jobs as jobs_module
from central.kernel.assets import AssetKey, AssetKind, AssetReady
from central.kernel.job_types import CATALOG, FetchOsImage, FetchPackage, SyncReleases
from central.kernel.jobs import (
    Delivery,
    Job,
    QueueName,
    asset_key,
    job_keys,
    registered_job_type,
)

FETCH = Delivery(queue=QueueName.FETCH)


class Colour(StrEnum):
    RED = "red"
    BLUE = "blue"


class TwoFields(Job[None], name="test.two_fields", delivery=FETCH):
    a: str
    b: str


class Keyable(Job[None], name="test.keyable", delivery=FETCH):
    s: str
    i: int
    flag: bool
    colour: Colour


class NoFields(Job[None], name="test.no_fields", delivery=FETCH):
    pass


# -- class-definition checks: each raises TypeError --------------------------------------------


def test_check_1_bare_job_and_subclassing_a_job_type_are_refused():
    with pytest.raises(TypeError, match="parametrized"):
        class Bare(Job, name="test.bare", delivery=FETCH):  # type: ignore[type-arg]
            pass
    with pytest.raises(TypeError, match="parametrized"):
        class Sub(TwoFields, name="test.sub", delivery=FETCH):
            pass
    assert "test.bare" not in jobs_module._REGISTRY
    assert "test.sub" not in jobs_module._REGISTRY


@pytest.mark.parametrize("name", [None, "nodot", "Upper.case", "a.b[", "a." + "b" * 63, "a..b"])
def test_check_2_bad_name(name):
    with pytest.raises(TypeError, match="job name"):
        class Bad(Job[None], name=name, delivery=FETCH):
            pass


def test_check_2_duplicate_name():
    with pytest.raises(TypeError, match="already registered"):
        class Dup(Job[None], name="test.two_fields", delivery=FETCH):
            pass
    assert registered_job_type("test.two_fields") is TwoFields


def test_check_3_missing_or_wrong_delivery():
    with pytest.raises(TypeError, match="delivery"):
        class Missing(Job[None], name="test.no_delivery"):
            pass
    with pytest.raises(TypeError, match="delivery"):
        class Wrong(Job[None], name="test.wrong_delivery", delivery="fetch"):  # type: ignore
            pass


def test_check_4_redefining_members_or_timestamp_field():
    with pytest.raises(TypeError, match="redefines"):
        class A(Job[None], name="test.redef_a", delivery=FETCH):
            subject = ("x",)
    with pytest.raises(TypeError, match="redefines"):
        class B(Job[None], name="test.redef_b", delivery=FETCH):
            job_name: str
    with pytest.raises(TypeError, match="redefines"):
        class C(Job[None], name="test.redef_c", delivery=FETCH):
            model_config = ConfigDict(frozen=False)
    with pytest.raises(TypeError, match="redefines"):
        class D(Job[None], name="test.redef_d", delivery=FETCH):
            @classmethod
            def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
                pass
    with pytest.raises(TypeError, match="reserved field"):
        class E(Job[None], name="test.redef_e", delivery=FETCH):
            timestamp: int


def test_check_5_unkeyable_field():
    class IntStrEnum(Enum):
        A = 1
        B = "b"

    with pytest.raises(TypeError, match="unkeyable"):
        class A(Job[None], name="test.unkeyable", delivery=FETCH):
            x: float
    with pytest.raises(TypeError, match="unkeyable"):
        class B(Job[None], name="test.unkeyable", delivery=FETCH):
            x: str | None
    with pytest.raises(TypeError, match="unkeyable"):
        class C(Job[None], name="test.unkeyable", delivery=FETCH):
            x: list[str]
    with pytest.raises(TypeError, match="unkeyable"):
        class D(Job[None], name="test.unkeyable", delivery=FETCH):
            x: IntStrEnum


@pytest.mark.parametrize("keyword", ["subject", "lock", "queueing_lock"])
def test_check_6_a_declaration_cannot_choose_its_keys(keyword):
    # The keys are derived from the fields (`job_keys`); no class keyword can narrow them.
    with pytest.raises(TypeError):
        class A(Job[None], name=f"test.keys_{keyword}", delivery=FETCH, **{keyword: ("a",)}):
            a: str
    assert f"test.keys_{keyword}" not in jobs_module._REGISTRY


def test_check_7_periodic_with_fields():
    with pytest.raises(TypeError, match="periodic"):
        class A(Job[None], name="test.periodic_fields",
                delivery=Delivery(queue=QueueName.UPKEEP, every=timedelta(minutes=5))):
            a: str


def test_check_8_result_needs_asset():
    with pytest.raises(TypeError, match="declare asset"):
        class A(Job[AssetReady], name="test.result_no_asset", delivery=FETCH):
            a: str


def test_check_8_asset_needs_asset_ready_result():
    with pytest.raises(TypeError, match="AssetReady"):
        class A(Job[None], name="test.asset_none", asset=AssetKind.OS_IMAGE, delivery=FETCH):
            a: str
    with pytest.raises(TypeError, match="AssetReady"):
        class B(Job[int], name="test.asset_int", asset=AssetKind.OS_IMAGE, delivery=FETCH):
            a: str


def test_check_8_an_asset_job_has_one_field():
    with pytest.raises(TypeError, match="exactly one field"):
        class A(Job[AssetReady], name="test.asset_two", asset=AssetKind.OS_IMAGE, delivery=FETCH):
            a: str
            b: str


def test_check_8_asset_is_not_periodic():
    with pytest.raises(TypeError, match="not periodic"):
        class A(Job[AssetReady], name="test.asset_periodic", asset=AssetKind.OS_IMAGE,
                delivery=Delivery(queue=QueueName.FETCH, every=timedelta(minutes=5))):
            pass


def test_check_8_asset_already_claimed():
    with pytest.raises(TypeError, match="already claimed"):
        class A(Job[AssetReady], name="test.asset_claimed", asset=AssetKind.OS_IMAGE,
                delivery=FETCH):
            tag: str
    assert jobs_module._ASSET_CLAIMS[AssetKind.OS_IMAGE] is FetchOsImage


def test_failed_definition_leaves_registry_unchanged():
    before = dict(jobs_module._REGISTRY)
    with pytest.raises(TypeError):
        class Broken(Job[None], name="test.corrected", delivery=FETCH):
            x: float
    assert jobs_module._REGISTRY == before

    class Corrected(Job[None], name="test.corrected", delivery=FETCH):
        x: int

    assert registered_job_type("test.corrected") is Corrected


def test_registered_job_type_unknown_raises_key_error():
    with pytest.raises(KeyError):
        registered_job_type("test.never_defined")


def test_catalog_types_are_registered_with_their_declarations():
    assert [registered_job_type(t.job_name) for t in CATALOG] == list(CATALOG)
    assert FetchOsImage.subject == ("tag",)
    assert FetchOsImage.result_type is AssetReady
    assert SyncReleases.result_type is None and SyncReleases.subject == ()
    assert Keyable.subject == ("s", "i", "flag", "colour")


# -- keys ----------------------------------------------------------------------------------------


def test_both_locks_are_every_field_in_declaration_order():
    keys = job_keys(TwoFields(a="x", b="y"))
    assert keys.lock == keys.queueing_lock == 'test.two_fields["x","y"]'
    assert job_keys(TwoFields(a="x", b="z")).lock != keys.lock


@given(st.tuples(st.text(), st.text()), st.tuples(st.text(), st.text()))
def test_keys_are_injective(p, q):
    left, right = TwoFields(a=p[0], b=p[1]), TwoFields(a=q[0], b=q[1])
    same_lock = job_keys(left).lock == job_keys(right).lock
    same_queueing = job_keys(left).queueing_lock == job_keys(right).queueing_lock
    assert same_lock == same_queueing == (p == q)


@pytest.mark.parametrize("p, q", [
    (('a","b', "c"), ("a", 'b","c')),
    (("a]", ""), ("a", "]")),
    (("a\\", '"'), ('a\\"', "")),
    ((",", ","), (",,", "")),
])
def test_keys_do_not_collide_on_separator_characters(p, q):
    left, right = TwoFields(a=p[0], b=p[1]), TwoFields(a=q[0], b=q[1])
    assert job_keys(left).queueing_lock != job_keys(right).queueing_lock
    assert job_keys(left).lock != job_keys(right).lock


def test_field_less_keys_are_constant():
    assert job_keys(NoFields()) == job_keys(NoFields())
    assert job_keys(NoFields()).lock == "test.no_fields[]"
    assert job_keys(NoFields()).queueing_lock == "test.no_fields[]"


def test_asset_key_matches_the_lock():
    tag = "v1.2.3-rc.1"
    job = FetchOsImage(tag=tag)
    assert asset_key(job) == AssetKey(AssetKind.OS_IMAGE, tag)
    assert job_keys(job).lock == f'os_image.fetch["{tag}"]'
    sha = "ab" * 32
    assert asset_key(FetchPackage(sha256=sha)) == AssetKey(AssetKind.PLAYER_DEB, sha)


def test_asset_key_refuses_non_asset_jobs_and_keys_refuse_unregistered():
    with pytest.raises(TypeError):
        asset_key(SyncReleases())
    with pytest.raises(TypeError):
        job_keys(object())  # type: ignore[arg-type]


def test_keyable_enum_and_scalars_encode_as_json():
    keys = job_keys(Keyable(s="s", i=3, flag=True, colour=Colour.RED))
    assert keys.lock == 'test.keyable["s",3,true,"red"]'


# -- validation ---------------------------------------------------------------------------------


def test_catalog_jobs_validate_their_fields():
    with pytest.raises(ValidationError):
        FetchOsImage(tag="1.0")
    with pytest.raises(ValidationError):
        FetchPackage(sha256="XYZ")


def test_jobs_are_frozen_and_reject_extra_fields():
    job = FetchOsImage(tag="v1.0.0")
    with pytest.raises(ValidationError):
        job.tag = "v2.0.0"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        FetchOsImage(tag="v1.0.0", extra="x")  # type: ignore[call-arg]
    assert hash(job) == hash(FetchOsImage(tag="v1.0.0"))


@pytest.mark.parametrize("kwargs", [
    {"every": timedelta(minutes=7)},
    {"retry": (timedelta(0),)},
    {"retry": (timedelta(days=1, seconds=1),)},
    {"priority": 101},
    {"priority": -101},
    {"priority": True},
])
def test_delivery_refuses_invalid_values(kwargs):
    with pytest.raises(ValueError):
        Delivery(queue=QueueName.FETCH, **kwargs)


def test_delivery_accepts_bounds():
    Delivery(queue=QueueName.UPKEEP, retry=(timedelta(days=1),), priority=-100,
             every=timedelta(hours=24))
