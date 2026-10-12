"""Series/characters/locations/music API: aliases, casting, archive, images, episodes."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frootio import media
from frootio.app import create_app

from .libmedia import png, tone


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(tmp_path / "no-dist")) as c:
        yield c


def _char(client: TestClient, name: str, aliases: list[str] | None = None) -> int:
    r = client.post("/api/characters", json={"name": name, "aliases": aliases or []})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _series(client: TestClient, name: str) -> int:
    r = client.post("/api/series", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_alias_collision_on_casting_is_case_insensitive_and_per_series(client: TestClient) -> None:
    kiwi = _char(client, "Kiwi Kowalski", ["Kiwi"])
    other = _char(client, "Plantain", ["  KIWI "])
    s1, s2 = _series(client, "One"), _series(client, "Two")
    assert client.put(f"/api/series/{s1}/cast/{kiwi}").status_code == 200
    r = client.put(f"/api/series/{s1}/cast/{other}")
    assert r.status_code == 409
    assert "'KIWI' is already used by Kiwi Kowalski in series One" in r.json()["detail"]
    # A different series is fine: aliases only need to be unique within one series.
    assert client.put(f"/api/series/{s2}/cast/{other}").status_code == 200
    assert client.put(f"/api/series/{s2}/cast/{kiwi}").status_code == 409


def test_alias_collision_against_names_on_alias_edit_and_rename(client: TestClient) -> None:
    kiwi = _char(client, "Kiwi Kowalski", ["Kiwi"])
    lemon = _char(client, "Lemon Lee", ["Lemon"])
    s = _series(client, "Show")
    for c in (kiwi, lemon):
        assert client.put(f"/api/series/{s}/cast/{c}").status_code == 200

    # An alias equal to another cast member's name collides (names always match).
    r = client.put(f"/api/characters/{kiwi}/aliases", json={"aliases": ["Kiwi", "lemon lee"]})
    assert r.status_code == 409 and "Lemon Lee" in r.json()["detail"]
    assert client.get(f"/api/characters/{kiwi}").json()["aliases"] == ["Kiwi"]  # rolled back

    # Renaming into another character's alias collides too.
    r = client.patch(f"/api/characters/{lemon}", json={"name": "kiwi"})
    assert r.status_code == 409
    assert client.get(f"/api/characters/{lemon}").json()["name"] == "Lemon Lee"

    # Own name as an alias, duplicates and whitespace are fine and deduplicated.
    r = client.put(
        f"/api/characters/{kiwi}/aliases", json={"aliases": ["Kiwi Kowalski", "Kiwi", "kiwi", " Mr B "]}
    )
    assert r.status_code == 200
    assert r.json()["aliases"] == ["Kiwi Kowalski", "Kiwi", "Mr B"]
    assert client.put(f"/api/characters/{kiwi}/aliases", json={"aliases": ["[Kiwi]"]}).status_code == 422


def test_archive_hides_from_lists_and_blocks_casting(client: TestClient) -> None:
    kiwi = _char(client, "Kiwi")
    s = _series(client, "Show")
    assert client.patch(f"/api/characters/{kiwi}", json={"archived": True}).json()["archived"] is True
    assert [c["id"] for c in client.get("/api/characters").json()] == []
    assert [c["id"] for c in client.get("/api/characters?include_archived=true").json()] == [kiwi]
    r = client.put(f"/api/series/{s}/cast/{kiwi}")
    assert r.status_code == 422 and "archived" in r.json()["detail"]
    client.patch(f"/api/characters/{kiwi}", json={"archived": False})
    assert client.put(f"/api/series/{s}/cast/{kiwi}").status_code == 200

    loc = client.post("/api/locations", json={"name": "Beach Hut", "description": "sand"}).json()
    assert loc["slug"] == "beach-hut"
    client.patch(f"/api/locations/{loc['id']}", json={"archived": True})
    assert client.get("/api/locations").json() == []
    assert client.put(f"/api/series/{s}/locations/{loc['id']}").status_code == 422


def test_character_images_are_content_addressed_and_main_is_a_pointer(
    client: TestClient, tmp_path: Path
) -> None:
    kiwi = _char(client, "Kiwi")
    red, blue = png(tmp_path / "r.png", "red"), png(tmp_path / "b.png", "blue", "8x8")
    r = client.post(
        f"/api/characters/{kiwi}/images",
        files=[("files", ("r.png", red, "image/png")), ("files", ("b.png", blue, "image/png"))],
    )
    assert r.status_code == 201, r.text
    first, second = r.json()["image_ids"]
    c = r.json()["character"]
    assert c["main_image"]["id"] == first  # first upload becomes main
    img = next(i for i in c["images"] if i["id"] == second)
    assert img["path"] == f"characters/kiwi/images/{media.sha256_bytes(blue)[:12]}.png"
    assert (img["width"], img["height"]) == (8, 8)
    assert media.abs_path(img["path"]).read_bytes() == blue

    # Same bytes again: no new row.
    again = client.post(f"/api/characters/{kiwi}/images", files=[("files", ("x.png", red, "image/png"))])
    assert again.json()["image_ids"] == [first]

    assert (
        client.put(f"/api/characters/{kiwi}/main-image", json={"image_id": second}).json()["main_image"]["id"]
        == second
    )
    other = _char(client, "Lime")
    assert client.put(f"/api/characters/{other}/main-image", json={"image_id": second}).status_code == 404

    bad = client.post(
        f"/api/characters/{kiwi}/images", files=[("files", ("x.png", b"not an image", "image/png"))]
    )
    assert bad.status_code == 422
    assert len(list((media.root() / "characters/kiwi/images").iterdir())) == 2


def test_location_refs_at_most_three(client: TestClient, tmp_path: Path) -> None:
    loc = client.post("/api/locations", json={"name": "Kitchen"}).json()["id"]
    files = [
        ("files", (f"{c}.png", png(tmp_path / f"{c}.png", c), "image/png"))
        for c in ("red", "blue", "green", "white")
    ]
    ids = client.post(f"/api/locations/{loc}/images", files=files).json()["image_ids"]
    assert client.put(f"/api/locations/{loc}/refs", json={"image_ids": ids}).status_code == 422
    r = client.put(f"/api/locations/{loc}/refs", json={"image_ids": [ids[2], ids[0]]})
    assert [i["id"] for i in r.json()["ref_images"]] == [ids[2], ids[0]]
    r = client.put(f"/api/locations/{loc}/refs", json={"image_ids": []})
    assert r.json()["ref_images"] == []


def test_episodes_create_and_list(client: TestClient) -> None:
    s = _series(client, "Kiwi and Lemon")
    e1 = client.post(f"/api/series/{s}/episodes", json={}).json()
    assert (e1["number"], e1["take_count"], e1["spend_cap_usd"], e1["default_location_id"]) == (
        1,
        1,
        20.0,
        None,
    )
    assert client.post(f"/api/series/{s}/episodes", json={"number": 1}).status_code == 409
    loc = client.post("/api/locations", json={"name": "Kitchen"}).json()["id"]
    # The default location must be one of the series' locations.
    assert client.post(f"/api/series/{s}/episodes", json={"default_location_id": loc}).status_code == 422
    client.put(f"/api/series/{s}/locations/{loc}")
    e4 = client.post(
        f"/api/series/{s}/episodes", json={"number": 4, "default_location_id": loc, "take_count": 3}
    ).json()
    assert (e4["number"], e4["default_location_name"], e4["take_count"]) == (4, "Kitchen", 3)
    assert client.post(f"/api/series/{s}/episodes", json={}).json()["number"] == 5
    assert [e["number"] for e in client.get(f"/api/series/{s}/episodes").json()] == [1, 4, 5]
    detail = client.get(f"/api/series/{s}").json()
    assert detail["slug"] == "kiwi-and-lemon" and detail["episode_count"] == 3 and detail["spend_usd"] == 0


def test_music_upload_converts_dedupes_and_never_overwrites(client: TestClient, tmp_path: Path) -> None:
    s = _series(client, "Show")
    wav = tone(tmp_path / "Theme Song.wav", 1.0).read_bytes()
    r = client.post(f"/api/series/{s}/music", files=[("files", ("Theme Song.wav", wav, "audio/wav"))])
    assert r.status_code == 201, r.text
    (track,) = r.json()["music"]
    assert track["path"] == "series/show/music/theme-song.mp3" and track["duration_s"] > 0.9
    assert media.abs_path(track["path"]).read_bytes()[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3")
    # Identical audio returns the existing track.
    again = client.post(f"/api/series/{s}/music", files=[("files", ("Theme Song.wav", wav, "audio/wav"))])
    assert again.json()["track_ids"] == [track["id"]]
    # Different audio with the same name gets a suffix.
    mp3 = tone(tmp_path / "other.mp3", 1.0, 880).read_bytes()
    r = client.post(
        f"/api/series/{s}/music", data={"name": "theme song"}, files=[("files", ("x.mp3", mp3, "audio/mpeg"))]
    )
    assert [t["name"] for t in r.json()["music"]] == ["theme-song", "theme-song-2"]
    assert media.abs_path("series/show/music/theme-song-2.mp3").read_bytes() == mp3
    bad = client.post(f"/api/series/{s}/music", files=[("files", ("x.mp3", b"nope", "audio/mpeg"))])
    assert bad.status_code == 422
