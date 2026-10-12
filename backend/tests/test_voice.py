"""Voice samples: 2-5s enforcement, >5s goes to the clipper, take-vocals clipping, pointer history."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frootio import db, media, voice
from frootio.app import create_app

from .conftest import make_episode
from .libmedia import tone


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(tmp_path / "no-dist")) as c:
        yield c


@pytest.fixture
def kiwi(client: TestClient) -> int:
    return client.post("/api/characters", json={"name": "Kiwi"}).json()["id"]


def _upload(client: TestClient, cid: int, path: Path):
    return client.post(f"/api/characters/{cid}/voice/upload", files={"file": (path.name, path.read_bytes())})


@pytest.mark.parametrize("seconds", [2.0, 3.5, 5.0])
def test_upload_within_bounds_is_saved_as_wav(
    client: TestClient, kiwi: int, tmp_path: Path, seconds: float
) -> None:
    r = _upload(client, kiwi, tone(tmp_path / "v.mp3", seconds))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["needs_clip"] is False
    v = body["character"]["voice"]
    assert v["id"] == body["voice_id"] and v["source"] == "upload"
    assert v["path"].startswith("characters/kiwi/voice/") and v["path"].endswith(".wav")
    assert abs(v["duration_s"] - seconds) < 0.06
    assert voice.duration(media.abs_path(v["path"])) == pytest.approx(v["duration_s"], abs=0.01)


def test_upload_too_short_is_rejected(client: TestClient, kiwi: int, tmp_path: Path) -> None:
    r = _upload(client, kiwi, tone(tmp_path / "v.wav", 1.5))
    assert r.status_code == 422 and "2-5s" in r.json()["detail"]
    assert client.get(f"/api/characters/{kiwi}").json()["voice"] is None


def test_long_upload_returns_full_audio_for_the_clipper(
    client: TestClient, kiwi: int, tmp_path: Path
) -> None:
    r = _upload(client, kiwi, tone(tmp_path / "long.m4a", 8.0))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["needs_clip"] is True and body["character"]["voice"] is None
    src = body["source"]
    assert src["path"].startswith("characters/kiwi/voice/sources/") and src["duration_s"] == pytest.approx(
        8.0, abs=0.06
    )

    peaks = client.get("/api/characters/voice/peaks", params={"path": src["path"]}).json()
    assert peaks["duration_s"] == pytest.approx(8.0, abs=0.06)
    assert len(peaks["peaks"]) == voice.PEAKS_BUCKETS and 0.1 < max(peaks["peaks"]) <= 1.0

    # Out-of-range clips are refused, nothing is saved.
    for start, end in [(0, 5.5), (1, 2.5), (6.0, 9.0), (3, 2)]:
        bad = client.post(
            f"/api/characters/{kiwi}/voice/clip",
            json={"source_path": src["path"], "start_s": start, "end_s": end},
        )
        assert bad.status_code == 422, (start, end)
    ok = client.post(
        f"/api/characters/{kiwi}/voice/clip",
        json={"source_path": src["path"], "start_s": 1.25, "end_s": 5.25},
    )
    assert ok.status_code == 200, ok.text
    v = ok.json()["character"]["voice"]
    assert (v["source"], v["clip_start_s"], v["clip_end_s"]) == ("upload", 1.25, 5.25)
    assert v["duration_s"] == pytest.approx(4.0, abs=0.01)


def test_peaks_and_clip_refuse_arbitrary_paths(client: TestClient, kiwi: int) -> None:
    rel = media.store_bytes("series/x/music", b"abc", "mp3")
    assert client.get("/api/characters/voice/peaks", params={"path": rel}).status_code == 404
    assert client.get("/api/characters/voice/peaks", params={"path": "../etc/passwd"}).status_code == 422
    r = client.post(f"/api/characters/{kiwi}/voice/clip", json={"source_path": rel, "start_s": 0, "end_s": 3})
    assert r.status_code == 422


def test_clip_from_take_vocals_and_pointer_history(client: TestClient, kiwi: int, tmp_path: Path) -> None:
    conn = db.connect()
    try:
        _, episode_id = make_episode(conn)
        ts = db.now()
        snippet_id = conn.execute(
            "INSERT INTO snippets (episode_id, created_at, updated_at) VALUES (?, ?, ?)", (episode_id, ts, ts)
        ).lastrowid
        take_id = conn.execute(
            "INSERT INTO takes (snippet_id, episode_id, requested_duration_s, status, created_at, updated_at)"
            " VALUES (?, ?, 10, 'done', ?, ?)",
            (snippet_id, episode_id, ts, ts),
        ).lastrowid
        rel = media.take_paths("test-series", 1, snippet_id, take_id)["vocals"]
        tone(tmp_path / "vocals.wav", 10.0)
        media.write_file(rel, (tmp_path / "vocals.wav").read_bytes())
        conn.execute("UPDATE takes SET vocals_path = ? WHERE id = ?", (rel, take_id))
        conn.commit()
    finally:
        conn.close()

    assert [t["id"] for t in client.get("/api/characters/voice/takes").json()] == [take_id]
    first = client.post(
        f"/api/characters/{kiwi}/voice/clip", json={"take_id": take_id, "start_s": 0.5, "end_s": 3.0}
    )
    assert first.status_code == 200, first.text
    v1 = first.json()["character"]["voice"]
    assert (v1["source"], v1["source_take_id"]) == ("take_clip", take_id)
    second = client.post(
        f"/api/characters/{kiwi}/voice/clip", json={"take_id": take_id, "start_s": 4, "end_s": 8}
    )
    v2 = second.json()["character"]["voice"]
    assert v2["id"] != v1["id"] and v2["path"] != v1["path"]
    # Older samples stay on disk and in history; the pointer can move back or be cleared.
    detail = client.put(f"/api/characters/{kiwi}/voice", json={"voice_id": v1["id"]}).json()
    assert detail["voice"]["id"] == v1["id"] and len(detail["voices"]) == 2
    assert media.abs_path(v2["path"]).is_file()
    assert client.put(f"/api/characters/{kiwi}/voice", json={"voice_id": None}).json()["voice"] is None
    assert (
        client.post(
            f"/api/characters/{kiwi}/voice/clip", json={"take_id": 999, "start_s": 0, "end_s": 3}
        ).status_code
        == 404
    )
