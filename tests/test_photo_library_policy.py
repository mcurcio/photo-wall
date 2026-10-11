"""The photo library's rules: connection URLs, key hints, Immich versions and a connection's
health after a contact (roadmap 4 T0; run ledger .claude/runs/photo-4.md)."""

import pytest

from central.photo_library.model import (
    ContactAnswer,
    ContactRefused,
    HealthOk,
    ImmichTooOld,
    ImmichUser,
    ImmichVersion,
    KeyRejected,
    MissingPermissions,
    NeedsALook,
    NoFaceDetection,
    NoFullSizeCopies,
    NotChecked,
    OtherUser,
    UnexpectedAnswer,
    Unreachable,
    UntestedVersion,
)
from central.photo_library.policy import (
    contact_health,
    key_hint,
    normalise_url,
    version_verdict,
)


@pytest.mark.parametrize(("text", "stored"), [
    ("http://immich.local:2283", "http://immich.local:2283/api"),
    ("https://h/api/", "https://h/api"),
    ("https://h/immich/", "https://h/immich/api"),
    ("https://h/immich/api", "https://h/immich/api"),
    ("  HTTPS://photos.example.org//  ", "https://photos.example.org/api"),
    ("http://[::1]:2283/", "http://[::1]:2283/api"),
])
def test_a_connection_url_is_stored_ending_in_api(text: str, stored: str) -> None:
    assert normalise_url(text) == stored


@pytest.mark.parametrize("text", [
    "ftp://h", "https://u:p@h", "https://u@h", "https://h?x", "https://h/#top", "h", "",
    "https://", "https://h:99999", "https://h x", "https://h/" + "a" * 2100,
])
def test_a_connection_url_that_cannot_be_stored_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        normalise_url(text)


def test_a_key_shows_only_its_last_four_characters() -> None:
    assert key_hint("abcd1234") == "…1234"


@pytest.mark.parametrize(("found", "verdict"), [
    ((2, 4, 9), ImmichTooOld(found="2.4.9", oldest="2.5.0")),
    ((1, 9, 0), ImmichTooOld(found="1.9.0", oldest="2.5.0")),
    ((2, 5, 0), None),
    ((2, 5, 6), None),
    ((2, 6, 0), UntestedVersion(found="2.6.0")),
    ((3, 1, 0), UntestedVersion(found="3.1.0")),
])
def test_versions_older_than_tested_need_a_look_and_newer_get_a_note(
        found: tuple[int, int, int], verdict: object) -> None:
    major, minor, patch = found
    assert version_verdict(ImmichVersion(major=major, minor=minor, patch=patch)) == verdict


OWNER = ImmichUser(user_ref="40000000-0000-4000-8000-000000000001", name="Matt",
                   email="matt@example.org")
STRANGER = ImmichUser(user_ref="40000000-0000-4000-8000-000000000002", name="",
                      email="guest@example.org")
TESTED = ImmichVersion(major=2, minor=5, patch=6)
OLD = ImmichVersion(major=2, minor=4, patch=9)
NEWER = ImmichVersion(major=2, minor=6, patch=0)


def answer(*, user: ImmichUser = OWNER, version: ImmichVersion = TESTED,
           missing: tuple[str, ...] = (), full_size: bool | None = True,
           faces: bool | None = True) -> ContactAnswer:
    return ContactAnswer(version=version, user=user, missing_permissions=missing,
                         full_size_copies=full_size, face_detection=faces)


def health(result: ContactAnswer | ContactRefused, *, bound: ImmichUser | None = OWNER,
           previous: object = NotChecked(), at: float = 100.0) -> object:
    return contact_health(result, bound_user=bound, previous=previous, checked_at=at)


@pytest.mark.parametrize("problem", [KeyRejected(), Unreachable(),
                                     UnexpectedAnswer(detail="users_me_shape")])
@pytest.mark.parametrize("bound", [None, OWNER, STRANGER])
def test_a_refused_contact_needs_a_look_whoever_the_connection_belongs_to(
        problem: object, bound: ImmichUser | None) -> None:
    assert health(ContactRefused(problem=problem), bound=bound) == NeedsALook(
        problem=problem, since=100.0, checked_at=100.0)


def test_another_user_beats_an_old_immich_which_beats_missing_permissions() -> None:
    everything_wrong = answer(user=STRANGER, version=OLD, missing=("face.read",))
    assert health(everything_wrong).problem == OtherUser(expected="Matt",
                                                         found="guest@example.org")
    old_and_missing = answer(version=OLD, missing=("face.read",))
    assert health(old_and_missing).problem == ImmichTooOld(found="2.4.9", oldest="2.5.0")
    missing = answer(missing=("user.read", "face.read", "face.read"))
    assert health(missing).problem == MissingPermissions(missing=("face.read", "user.read"))


def test_the_first_check_binds_no_user_so_any_user_passes() -> None:
    assert health(answer(user=STRANGER), bound=None) == HealthOk(checked_at=100.0)


def test_a_passing_contact_is_ok_with_its_notes_in_order() -> None:
    assert health(answer()) == HealthOk(checked_at=100.0, notes=())
    assert health(answer(full_size=None)) == HealthOk(checked_at=100.0, notes=())
    assert health(answer(version=NEWER, full_size=False)) == HealthOk(
        checked_at=100.0, notes=(UntestedVersion(found="2.6.0"), NoFullSizeCopies()))


@pytest.mark.parametrize(("faces", "notes"), [
    (False, (UntestedVersion(found="2.6.0"), NoFullSizeCopies(), NoFaceDetection())),
    (True, (UntestedVersion(found="2.6.0"), NoFullSizeCopies())),
    (None, (UntestedVersion(found="2.6.0"), NoFullSizeCopies())),
])
def test_face_detection_off_is_a_note_after_the_others(faces: bool | None,
                                                       notes: tuple[object, ...]) -> None:
    """Roadmap 5a: with Immich's face detection off, crops ignore faces (owner, 2026-10-11)."""
    assert health(answer(version=NEWER, full_size=False, faces=faces)) == HealthOk(
        checked_at=100.0, notes=notes)


def test_since_carries_while_the_problem_kind_stays_the_same() -> None:
    rejected = ContactRefused(problem=KeyRejected())
    first = health(rejected, at=10.0)
    assert health(rejected, previous=first, at=20.0) == NeedsALook(
        problem=KeyRejected(), since=10.0, checked_at=20.0)
    changed = health(ContactRefused(problem=Unreachable()), previous=first, at=20.0)
    assert (changed.since, changed.checked_at) == (20.0, 20.0)
    cleared = health(answer(), previous=first, at=30.0)
    assert cleared == HealthOk(checked_at=30.0)
    again = health(rejected, previous=cleared, at=40.0)
    assert again.since == 40.0
    # The kind decides, not the detail: a different missing list keeps `since`.
    missing = health(answer(missing=("face.read",)), at=50.0)
    assert health(answer(missing=("tag.read",)), previous=missing, at=60.0).since == 50.0
