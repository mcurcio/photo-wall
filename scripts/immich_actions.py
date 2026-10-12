"""Container-side Immich fixture client and the synthetic content set.

This module deliberately has no Docker or host-process dependencies.  It is a
small, explicit dependency of helper images that need upstream authority, and
the named tests' accessor (tests/support/immich_library.py) reuses its client
against the published fixture.

The content set (run ledger .claude/runs/photo-4.md, slice FX1) is data: `CONTENT`
lists every asset by label, `ALBUMS`, `TAGS`, `STACK` and `EDIT` what joins them.
`UpstreamFixture.initialize` uploads it with the admin session and records it in
`/runtime/library.json`; tests/test_immich_fixture_content.py holds the expected pool
membership of every label for the named tests' queries.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from scripts.harness_failure import CodedFailure

# The runtime key's permissions: exactly central.photo_library.policy.REQUIRED_PERMISSIONS
# (tests/test_immich_fixture_content.py pins the equality; this bundle never imports central).
PERMISSIONS = [
    "album.read", "album.statistics", "asset.download", "asset.edit.get", "asset.read",
    "asset.statistics", "asset.view", "face.read", "person.read", "tag.read", "user.read",
]
UPSTREAM = "http://immich:2283/api"
MAX_DOCUMENT_BYTES = 10 * 1024**2
LIBRARY_SCHEMA = 2
OWNER_NAME = "Fixture Owner"
PARTNER_NAME = "Fixture Partner"
# The locked folder needs a PIN on the admin session; synthetic, never a real secret.
FIXTURE_PIN = "135790"
# Paris (48.8566 N, 2.3522 E) as EXIF degrees, minutes, seconds.
PARIS = ((48.0, 51.0, 23.76), (2.0, 21.0, 7.92))
SAME_INSTANT = "2024-03-03T03:03:03Z"
METADATA_TIMEOUT = 240


@dataclass(frozen=True)
class Item:
    """One asset of the content set, as uploaded."""

    label: str
    taken: str                      # ISO 8601 UTC
    people: tuple[str, ...] = ()    # manual faces, by person name
    kind: str = "image"             # "image" or "video"
    size: tuple[int, int] = (96, 64)
    png: bool = False
    filename: str | None = None     # the uploaded file name; default "<label>.jpg|.png|.mp4"
    favourite: bool = False
    visibility: str = "timeline"    # "timeline", "archive", "hidden" or "locked"
    owner: str = "owner"            # "owner" or "partner"
    paris: bool = False             # GPS at Paris, reverse geocoded by Immich

    @property
    def camera(self) -> bool:
        """A JPEG carries a camera maker and model; a PNG none (C4's screenshot rule)."""
        return self.kind == "image" and not self.png

    @property
    def upload_name(self) -> str:
        suffix = ".mp4" if self.kind == "video" else ".png" if self.png else ".jpg"
        return self.filename or self.label + suffix


AB = ("Anna", "Ben")
CONTENT: tuple[Item, ...] = (
    Item("anna-ben-1", "2025-06-01T10:00:00Z", AB, favourite=True),
    Item("anna-ben-2", "2025-01-15T10:00:00Z", AB),
    Item("anna-ben-3", "2024-08-20T10:00:00Z", AB),
    Item("anna-only", "2024-11-05T10:00:00Z", ("Anna",)),
    Item("ben-only", "2024-12-05T10:00:00Z", ("Ben",)),
    Item("anna-ben-old", "2019-07-04T10:00:00Z", AB),
    Item("screenshot-anna-ben", "2025-03-01T09:00:00Z", AB, png=True,
         filename="Screenshot 2025-03-01.png"),
    Item("scan", "2024-05-01T10:00:00Z", png=True, filename="scan-1.png"),
    Item("archived-anna-ben", "2025-02-01T10:00:00Z", AB, visibility="archive"),
    Item("hidden-anna-ben", "2025-02-02T10:00:00Z", AB, visibility="hidden"),
    Item("locked-anna-ben", "2025-02-03T10:00:00Z", AB, visibility="locked"),
    Item("stack-primary", "2025-04-01T10:00:00Z", ("Ben",)),
    Item("stack-copy", "2025-04-01T10:00:01Z", AB),
    Item("edited", "2025-05-01T10:00:00Z", size=(128, 96)),
    Item("partner-anna-ben", "2025-05-15T10:00:00Z", AB, owner="partner"),
    Item("paris-1", "2025-07-14T10:00:00Z", paris=True),
    Item("paris-2", "2025-07-15T10:00:00Z", paris=True),
    Item("garden-1", "2024-06-01T10:00:00Z", favourite=True),
    Item("garden-2", "2024-06-02T10:00:00Z"),
    Item("xmas-1", "2024-12-25T10:00:00Z"),
    *(Item(f"same-instant-{index}", SAME_INSTANT) for index in range(1, 9)),
    Item("video-1", "2025-08-01T12:00:00Z", kind="video", size=(160, 90)),
)
ALBUMS = {"Garden": ("garden-1", "garden-2"), "Holidays": ("paris-1", "anna-ben-1")}
# The partner's album, shared with the owner: never offered (Immich's search returns none of
# another user's album) and not among the owner's album count.
SHARED_ALBUM = ("Partner's trip", ("partner-anna-ben",))
# A person the owner hid, with one face (Immich counts only people with a face): never offered
# and not among the people count.
HIDDEN_PERSON = ("Hidden Hal", "scan")
# Each tag path and the labels it is on; "Family/Christmas" is a child of "Family".
TAGS = {"Family": ("anna-ben-2",), "Family/Christmas": ("xmas-1",)}
# The stack: its primary first (POST /stacks makes the first id the primary).
STACK = ("stack-primary", "stack-copy")
# The edit: a crop of `edited` (128 x 96) to its left half, so its upright size is 64 x 96.
EDIT = ("edited", {"x": 0, "y": 0, "width": 64, "height": 96})


def upright_size(item: Item) -> tuple[int, int]:
    """The asset's size as Immich shows it, after the fixture's edit."""
    if item.label == EDIT[0]:
        return EDIT[1]["width"], EDIT[1]["height"]
    return item.size


class ActionError(CodedFailure):
    """A bounded container-side failure code safe for public evidence."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ActionError(code)


def read_json(path: Path) -> dict:
    with path.open("rb") as stream:
        data = stream.read(MAX_DOCUMENT_BYTES + 1)
    require(len(data) <= MAX_DOCUMENT_BYTES, "fixture_document_bound")
    value = json.loads(data)
    require(isinstance(value, dict), "fixture_document_invalid")
    return value


def write_json(path: Path, value: object) -> None:
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    require(len(data) <= MAX_DOCUMENT_BYTES, "fixture_document_bound")
    with path.open("wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)


def instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def synthetic_image(label: str, size: tuple[int, int], taken: str, *, png: bool = False,
                    camera: bool = True, paris: bool = False, orientation: int = 1) -> bytes:
    """A small image unique to `label` (Immich refuses a second upload of the same bytes).

    A JPEG carries its capture time with a UTC offset, a camera maker and model, and GPS when
    `paris`; a PNG carries no EXIF, so Immich takes its time from the upload's fileCreatedAt.
    """
    from PIL import Image, ImageDraw

    digest = hashlib.sha256(label.encode()).digest()
    image = Image.new("RGB", size, tuple(digest[:3]))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, size[0] // 2, size[1] // 2), fill=tuple(digest[3:6]))
    draw.text((3, 3), label, fill="white")
    stream = io.BytesIO()
    if png:
        image.save(stream, format="PNG")
        return stream.getvalue()
    exif = Image.Exif()
    stamp = instant(taken).strftime("%Y:%m:%d %H:%M:%S")
    exif[0x0132] = stamp                      # DateTime
    if orientation != 1:
        exif[0x0112] = orientation            # Orientation
    if camera:
        exif[0x010F] = "PhotoWall"            # Make
        exif[0x0110] = "Synthetic Fixture"    # Model
    details = exif.get_ifd(0x8769)
    details[0x9003] = stamp                   # DateTimeOriginal
    details[0x9011] = "+00:00"                # OffsetTimeOriginal
    if paris:
        gps = exif.get_ifd(0x8825)
        gps[1], gps[2], gps[3], gps[4] = "N", PARIS[0], "E", PARIS[1]
    image.save(stream, format="JPEG", quality=95, exif=exif)
    return stream.getvalue()


def synthetic_video(path: Path, size: tuple[int, int], taken: str) -> bytes:
    """A 2 s H.264 MP4 made by the fixture image's ffmpeg."""
    import subprocess

    result = subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={size[0]}x{size[1]}:rate=12", "-t", "2", "-an",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-metadata", "creation_time=" + taken, "-movflags", "+faststart", str(path),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=False)
    require(result.returncode == 0, "synthetic_video_failed")
    return path.read_bytes()


class UpstreamFixture:
    """An Immich session with upstream authority (the admin's, or the partner's).

    In the fixture image it talks to `UPSTREAM` with the session in /setup; the named tests'
    accessor points it at the published URL with the exported admin session.
    """

    def __init__(self, base_url: str = UPSTREAM, token: str | None = None) -> None:
        import httpx

        self.client = httpx.Client(base_url=base_url, trust_env=False,
                                   follow_redirects=False, timeout=30)
        if token is not None:
            self.client.headers["Authorization"] = "Bearer " + token
        self.setup, self.runtime = Path("/setup"), Path("/runtime")

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, path: str, expected: tuple[int, ...] = (200,),
                **kwargs: object) -> dict | list:
        response = self.client.request(method, path, **kwargs)
        require(response.status_code in expected, "fixture_http_" + str(response.status_code))
        return response.json() if response.content else {}

    def authenticate(self) -> dict:
        state = read_json(self.setup / "session.json")
        self.client.headers["Authorization"] = "Bearer " + state["token"]
        return state

    def sign_in(self, email: str, password: str) -> dict:
        login = self.request("POST", "/auth/login", (201,),
                             json={"email": email, "password": password})
        self.client.headers["Authorization"] = "Bearer " + login["accessToken"]
        return login

    # --- uploads ------------------------------------------------------------------------------

    def upload_bytes(self, name: str, data: bytes, mime: str, taken: str, *,
                     favourite: bool = False) -> str:
        result = self.request("POST", "/assets", (201,), data={
            "fileCreatedAt": taken, "fileModifiedAt": taken,
            "isFavorite": str(favourite).lower(),
        }, files={"assetData": (name, data, mime)})
        return result["id"]

    def upload_item(self, item: Item, scratch: Path) -> dict:
        """Upload one content item; its record for library.json (without people or tags)."""
        if item.kind == "video":
            data = synthetic_video(scratch / item.upload_name, item.size, item.taken)
            mime = "video/mp4"
        else:
            data = synthetic_image(item.label, item.size, item.taken, png=item.png,
                                   camera=item.camera, paris=item.paris)
            mime = "image/png" if item.png else "image/jpeg"
        upstream_id = self.upload_bytes(item.upload_name, data, mime, item.taken,
                                        favourite=item.favourite)
        width, height = upright_size(item)
        return {"upstream_id": upstream_id, "kind": item.kind, "taken": item.taken,
                "width": width, "height": height, "sha1": hashlib.sha1(data).hexdigest(),
                "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                "filename": item.upload_name, "owner": item.owner,
                "favourite": item.favourite, "visibility": item.visibility,
                "people": list(item.people), "paris": item.paris}

    def upload(self, manifest: dict, label: str, size: tuple[int, int], orientation: int,
               captured: str, favorite: bool) -> None:
        """One JPEG with an EXIF orientation, recorded in `manifest` (the wall demo's uploads)."""
        data = synthetic_image(label, size, captured, orientation=orientation)
        upstream_id = self.upload_bytes(label + ".jpg", data, "image/jpeg", captured,
                                        favourite=favorite)
        manifest[label] = {
            "upstream_id": upstream_id, "sha1": hashlib.sha1(data).hexdigest(),
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "raw_width": size[0], "raw_height": size[1], "orientation": orientation,
            "captured": captured, "favorite": favorite, "deleted": False,
        }

    def tag(self, paths: list[str], asset_ids: list[str]) -> dict[str, str]:
        """Upsert the tag paths and put each on the assets; tag path -> tag id."""
        tags = self.request("PUT", "/tags", json={"tags": paths})
        by_path = {tag["value"]: tag["id"] for tag in tags}
        require(set(paths) <= set(by_path), "fixture_document_invalid")
        if asset_ids:
            self.request("PUT", "/tags/assets",
                         json={"tagIds": [by_path[path] for path in paths],
                               "assetIds": asset_ids})
        return by_path

    def add_face(self, asset_id: str, person_id: str, size: tuple[int, int],
                 slot: int) -> None:
        """A manual face (machine learning is off): one box per person, side by side."""
        width, height = size
        box = max(4, min(width // 3, height // 2))
        self.request("POST", "/faces", (201,), json={
            "assetId": asset_id, "personId": person_id, "imageWidth": width,
            "imageHeight": height, "x": 2 + slot * (box + 4), "y": 2,
            "width": box, "height": box,
        })

    def wait_until_read(self, upstream_id: str, taken: str, *, city: bool = False,
                        deadline: float) -> dict:
        """The asset once Immich has read its size and capture time (and its city)."""
        refreshed = False
        while True:
            asset = self.request("GET", "/assets/" + upstream_id)
            exif = asset.get("exifInfo") or {}
            created = asset.get("fileCreatedAt")
            done = (bool(asset.get("width")) and bool(asset.get("height"))
                    and created is not None and instant(created) == instant(taken)
                    and (not city or bool(exif.get("city"))))
            if done:
                return asset
            remaining = deadline - time.monotonic()
            require(remaining > 0, "metadata_convergence_timeout")
            if city and not refreshed and remaining < METADATA_TIMEOUT / 2:
                # Reverse geocoding may have run before Immich loaded its place data.
                self.request("POST", "/assets/jobs", (204,),
                             json={"assetIds": [upstream_id], "name": "refresh-metadata"})
                refreshed = True
            time.sleep(1)

    # --- the content set ----------------------------------------------------------------------

    def initialize(self) -> dict:
        password = secrets.token_urlsafe(32)
        deadline = time.monotonic() + 120
        while True:
            try:
                version = self.request("GET", "/server/version")
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise ActionError("upstream_startup_timeout") from None
                time.sleep(1)
        require({part: version.get(part) for part in ("major", "minor", "patch")}
                == {"major": 3, "minor": 1, "patch": 0} and not version.get("prerelease"),
                "upstream_version_mismatch")
        credentials = {"email": "fixture@example.invalid", "password": password}
        owner = self.request("POST", "/auth/admin-sign-up", (201,),
                             json={**credentials, "name": OWNER_NAME})
        login = self.sign_in(**credentials)
        config = self.request("GET", "/system-config")
        config["machineLearning"]["enabled"] = False
        config["newVersionCheck"]["enabled"] = False
        config["reverseGeocoding"]["enabled"] = True
        self.request("PUT", "/system-config", json=config)
        key = self.request("POST", "/api-keys", (201,),
                           json={"name": "Photo Wall fixture", "permissions": PERMISSIONS})
        require(sorted(key["apiKey"]["permissions"]) == sorted(PERMISSIONS),
                "key_scope_mismatch")
        write_json(self.setup / "session.json", {
            "token": login["accessToken"], "key_id": key["apiKey"]["id"],
        })

        partner_credentials = {"email": "partner@example.invalid",
                               "password": secrets.token_urlsafe(32)}
        partner_user = self.request("POST", "/admin/users", (201,),
                                    json={**partner_credentials, "name": PARTNER_NAME})
        partner = UpstreamFixture(str(self.client.base_url))
        try:
            partner.sign_in(**partner_credentials)
            partner_key = partner.request("POST", "/api-keys", (201,), json={
                "name": "Photo Wall fixture (partner)", "permissions": PERMISSIONS})
            partner.request("POST", "/partners", (201,), json={"sharedWithId": owner["id"]})
            self.request("PUT", "/partners/" + partner_user["id"], json={"inTimeline": True})
            library = self._content(partner, owner["id"])
        finally:
            partner.close()

        library.update(schema=LIBRARY_SCHEMA, internal_url=UPSTREAM,
                       api_key=key["secret"], permissions=sorted(PERMISSIONS),
                       partner_api_key=partner_key["secret"],
                       owner={"ref": owner["id"], "name": OWNER_NAME},
                       partner={"ref": partner_user["id"], "name": PARTNER_NAME})
        library["counts"] = self._counts(key["secret"])
        write_json(self.runtime / "library.json", library)
        return {"version": version, "uploaded": len(library["assets"]),
                "runtime_permissions": sorted(PERMISSIONS), "counts": library["counts"],
                "machine_learning_enabled": False, "reverse_geocoding_enabled": True,
                "edited_reported": library.pop("_edited_reported")}

    def _content(self, partner: UpstreamFixture, owner_ref: str) -> dict:
        sessions = {"owner": self, "partner": partner}
        people: dict[str, dict[str, str]] = {"owner": {}, "partner": {}}
        for owner, session in sessions.items():
            for name in AB:
                person = session.request("POST", "/people", (201,), json={"name": name})
                people[owner][name] = person["id"]
        scratch = self.setup
        assets = {}
        for item in CONTENT:
            session = sessions[item.owner]
            record = session.upload_item(item, scratch)
            for slot, name in enumerate(item.people):
                session.add_face(record["upstream_id"], people[item.owner][name], item.size,
                                 slot)
            assets[item.label] = record
        ids = {label: record["upstream_id"] for label, record in assets.items()}
        hidden_name, hidden_label = HIDDEN_PERSON
        hidden = self.request("POST", "/people", (201,), json={"name": hidden_name})
        self.add_face(ids[hidden_label], hidden["id"],
                      next(item.size for item in CONTENT if item.label == hidden_label), 0)
        self.request("PUT", "/people/" + hidden["id"], json={"isHidden": True})

        albums = {}
        for name, labels in ALBUMS.items():
            album = self.request("POST", "/albums", (201,), json={
                "albumName": name, "assetIds": [ids[label] for label in labels]})
            albums[name] = album["id"]
        shared_name, shared_labels = SHARED_ALBUM
        shared = partner.request("POST", "/albums", (201,), json={
            "albumName": shared_name, "assetIds": [ids[label] for label in shared_labels],
            "albumUsers": [{"userId": owner_ref, "role": "viewer"}]})
        tags = {}
        for path, labels in TAGS.items():
            tags.update(self.tag([path], [ids[label] for label in labels]))
        stack = self.request("POST", "/stacks", (201,),
                             json={"assetIds": [ids[label] for label in STACK]})
        require(stack["primaryAssetId"] == ids[STACK[0]], "fixture_document_invalid")
        self.request("PUT", f"/assets/{ids[EDIT[0]]}/edits",
                     json={"edits": [{"action": "crop", "parameters": EDIT[1]}]})

        # Visibility last: the faces and the stack are made on timeline assets.
        self.request("POST", "/auth/pin-code", (204,), json={"pinCode": FIXTURE_PIN})
        self.request("POST", "/auth/session/unlock", (204,), json={"pinCode": FIXTURE_PIN})
        for item in CONTENT:
            if item.visibility != "timeline":
                self.request("PUT", "/assets/" + ids[item.label],
                             json={"visibility": item.visibility})

        deadline = time.monotonic() + METADATA_TIMEOUT
        reported = {}
        for item in CONTENT:
            asset = sessions[item.owner].wait_until_read(ids[item.label], item.taken,
                                                         city=item.paris, deadline=deadline)
            if item.label == EDIT[0]:
                reported = {"width": asset["width"], "height": asset["height"],
                            "is_edited": asset.get("isEdited")}
        return {"assets": assets, "albums": albums, "tags": tags,
                "people": people["owner"], "partner_people": people["partner"],
                "shared_albums": {shared_name: shared["id"]},
                "hidden_people": {hidden_name: hidden["id"]},
                "stack": {"primary": STACK[0], "copies": list(STACK[1:])},
                "edit": {"label": EDIT[0], "crop": EDIT[1]},
                "_edited_reported": reported}

    def _counts(self, api_key: str) -> dict[str, int]:
        """The counts the owner should see: photos and videos as Immich's statistics give them
        (read through the runtime key), albums and people from the content set (the owner's own
        albums; the named people they did not hide), so the product's formulas over Immich's
        statistics are checked against the content, not against those statistics again."""
        import httpx

        with httpx.Client(base_url=str(self.client.base_url), trust_env=False,
                          follow_redirects=False, timeout=30,
                          headers={"x-api-key": api_key}) as client:
            def get(path: str) -> dict:
                response = client.get(path)
                require(response.status_code == 200, "fixture_http_" + str(response.status_code))
                return response.json()

            me = get("/api-keys/me")
            require(sorted(me["permissions"]) == sorted(PERMISSIONS), "key_scope_mismatch")
            assets = get("/assets/statistics")
            albums, people = get("/albums/statistics"), get("/people")
        # The content set is what Immich holds: a shared album and a hidden person are there.
        require(albums["shared"] >= 1 and people["hidden"] >= 1, "fixture_document_invalid")
        return {"photos": assets["images"], "videos": assets["videos"],
                "albums": len(ALBUMS), "people": len(AB)}


def run_action(action: str) -> dict:
    require(action == "initialize", "unknown_fixture_action")
    fixture = UpstreamFixture()
    try:
        return fixture.initialize()
    finally:
        fixture.close()
