"""nano-banana and Chatterbox jobs with fal mocked: submit-once/resume, storage, spend, errors."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frootio import config, db, falapi, jobs, media, paidgen
from frootio.app import create_app
from frootio.library import add_image

from .libmedia import png, tone


class FakeFal:
    def __init__(self, result: dict[str, Any], error: str | None = None) -> None:
        self.result_payload = result
        self.error = error
        self.submitted: list[tuple[str, dict[str, Any]]] = []
        self.uploads: list[Path] = []
        self.polls = 0

    async def submit(self, endpoint: str, arguments: dict[str, Any]) -> str:
        self.submitted.append((endpoint, arguments))
        return "req-1"

    async def status(self, endpoint: str, request_id: str, **_: Any) -> falapi.FalStatus:
        self.polls += 1
        if self.polls == 1:
            return falapi.FalStatus("running")
        return falapi.FalStatus(
            "completed", error=self.error, error_type="content_policy" if self.error else None
        )

    async def result(self, endpoint: str, request_id: str) -> dict[str, Any]:
        return self.result_payload

    async def upload_file(self, path: Path) -> str:
        self.uploads.append(Path(path))
        return f"https://fal.example/{Path(path).name}"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def install(result: dict[str, Any], files: dict[str, bytes], error: str | None = None) -> FakeFal:
        f = FakeFal(result, error)
        for name in ("submit", "status", "result", "upload_file"):
            monkeypatch.setattr(falapi, name, getattr(f, name))

        async def download(url: str) -> bytes:
            return files[url]

        monkeypatch.setattr(paidgen, "download", download)
        monkeypatch.setattr(paidgen, "POLL_START_S", 0.0)
        return f

    return install


def _character(conn: sqlite3.Connection, name: str = "Kiwi") -> int:
    ts = db.now()
    cid = conn.execute(
        "INSERT INTO characters (name, slug, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (name, name.lower(), ts, ts),
    ).lastrowid
    conn.commit()
    return cid


def _spend(conn: sqlite3.Connection) -> list[tuple[str, float, int | None, int | None]]:
    return [tuple(r) for r in conn.execute("SELECT kind, amount_usd, character_id, location_id FROM spend")]


async def test_image_job_edit_mode_stores_candidates_and_logs_spend(conn, fake, tmp_path: Path) -> None:
    anchor = _character(conn, "Anchor")
    anchor_img = add_image(conn, "character", anchor, png(tmp_path / "a.png", "yellow"), "png")
    conn.commit()
    anchor_path = conn.execute("SELECT path FROM character_images WHERE id = ?", (anchor_img,)).fetchone()[0]
    kiwi = _character(conn)
    out1, out2 = png(tmp_path / "o1.png", "green"), png(tmp_path / "o2.png", "lime")
    f = fake(
        {"images": [{"url": "u1", "file_name": "a.png"}, {"url": "u2", "content_type": "image/png"}]},
        {"u1": out1, "u2": out2},
    )
    payload = {"owner": "character", "owner_id": kiwi, "prompt": "a kiwi", "num_images": 2,
               "input_paths": [anchor_path], "aspect_ratio": "9:16"}  # fmt: skip
    job_id = jobs.enqueue(conn, paidgen.IMAGE_JOB, payload)

    result = await paidgen.image_gen(job_id, payload)

    ((endpoint, args),) = f.submitted
    assert endpoint == config.NANO_BANANA_EDIT_ENDPOINT
    assert args == {"prompt": "a kiwi", "num_images": 2, "output_format": "png", "aspect_ratio": "9:16",
                    "image_urls": [f"https://fal.example/{Path(anchor_path).name}"]}  # fmt: skip
    assert jobs.get(job_id)["fal_request_id"] == "req-1"
    imgs = conn.execute(
        "SELECT * FROM character_images WHERE character_id = ? ORDER BY id", (kiwi,)
    ).fetchall()
    assert [i["id"] for i in imgs] == result["image_ids"]
    assert [media.abs_path(i["path"]).read_bytes() for i in imgs] == [out1, out2]
    assert all(i["source"] == "generated" and i["gen_request_id"] == "req-1" for i in imgs)
    assert db.loads(imgs[0]["gen_input_paths"]) == [anchor_path]
    # Candidates are extras: setting main is the user's choice.
    assert conn.execute("SELECT main_image_id FROM characters WHERE id = ?", (kiwi,)).fetchone()[0] is None
    assert _spend(conn) == [("image", pytest.approx(2 * config.NANO_BANANA_USD_PER_IMAGE), kiwi, None)]

    # Resume after a restart: polls the saved id, no new submit, no double spend.
    f.polls = 0
    await paidgen.image_gen(job_id, payload)
    assert len(f.submitted) == 1 and len(_spend(conn)) == 1


async def test_image_job_prompt_only_for_location_and_fal_error(conn, fake, tmp_path: Path) -> None:
    ts = db.now()
    loc = conn.execute(
        "INSERT INTO locations (name, slug, created_at, updated_at) VALUES ('Kitchen', 'kitchen', ?, ?)",
        (ts, ts),
    ).lastrowid
    conn.commit()
    f = fake({"images": [{"url": "u1"}]}, {"u1": png(tmp_path / "k.png", "white")})
    payload = {
        "owner": "location",
        "owner_id": loc,
        "prompt": "a kitchen",
        "num_images": 1,
        "input_paths": [],
    }
    await paidgen.image_gen(jobs.enqueue(conn, paidgen.IMAGE_JOB, payload), payload)
    assert f.submitted[0][0] == config.NANO_BANANA_ENDPOINT and "image_urls" not in f.submitted[0][1]
    assert _spend(conn) == [("image", pytest.approx(config.NANO_BANANA_USD_PER_IMAGE), None, loc)]
    path = conn.execute("SELECT path FROM location_images").fetchone()[0]
    assert path.startswith("locations/kitchen/images/")

    f2 = fake({}, {}, error="blocked by safety checker")
    with pytest.raises(paidgen.FalJobError, match="safety"):
        await paidgen.image_gen(jobs.enqueue(conn, paidgen.IMAGE_JOB, payload), payload)
    assert len(_spend(conn)) == 1 and f2.submitted


async def test_tts_job_short_saves_long_goes_to_clipper(conn, fake, tmp_path: Path) -> None:
    kiwi = _character(conn)
    short = tone(tmp_path / "s.wav", 3.0).read_bytes()
    f = fake({"audio": {"url": "a1"}}, {"a1": short})
    payload = {"owner": "character", "owner_id": kiwi, "voice": "Rico", "text": "Hello there, I am a kiwi."}
    out = await paidgen.tts_gen(jobs.enqueue(conn, paidgen.TTS_JOB, payload), payload)
    assert f.submitted == [(config.CHATTERBOX_ENDPOINT, {"text": payload["text"], "voice": "Rico"})]
    assert out["needs_clip"] is False
    v = conn.execute("SELECT * FROM character_voices WHERE id = ?", (out["voice_id"],)).fetchone()
    assert (v["source"], v["tts_voice"], v["tts_text"], v["gen_request_id"]) == (
        "chatterbox",
        "Rico",
        payload["text"],
        "req-1",
    )
    assert _spend(conn) == [("tts", pytest.approx(paidgen.tts_cost(payload["text"])), kiwi, None)]


def test_tts_long_result_clip_keeps_provenance(fake, tmp_path: Path) -> None:
    long = tone(tmp_path / "l.mp3", 7.0).read_bytes()
    fake({"audio": {"url": "a1"}}, {"a1": long})
    with TestClient(create_app(tmp_path / "no-dist")) as client:
        kiwi = client.post("/api/characters", json={"name": "Kiwi"}).json()["id"]
        bad = client.post(f"/api/characters/{kiwi}/voice/generate", json={"voice": "Nobody", "text": "hi"})
        assert bad.status_code == 422
        r = client.post(
            f"/api/characters/{kiwi}/voice/generate", json={"voice": "Vicky", "text": "A test line."}
        )
        assert r.status_code == 202
        job_id = r.json()["job_id"]
        for _ in range(200):
            job = jobs.get(job_id)
            if job["status"] in jobs.TERMINAL:
                break
            time.sleep(0.02)
        assert job["status"] == "done", job
        assert job["result"]["needs_clip"] is True
        src = job["result"]["source"]["path"]
        listed = client.get(f"/api/characters/{kiwi}/jobs").json()
        assert listed[0]["id"] == job_id
        clip = client.post(
            f"/api/characters/{kiwi}/voice/clip", json={"source_path": src, "start_s": 1, "end_s": 4}
        )
        v = clip.json()["character"]["voice"]
        assert (v["source"], v["tts_voice"], v["tts_text"]) == ("chatterbox", "Vicky", "A test line.")


def test_generate_endpoint_validation(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "no-dist")) as client:
        kiwi = client.post("/api/characters", json={"name": "Kiwi"}).json()["id"]
        url = f"/api/characters/{kiwi}/images/generate"
        assert client.post(url, json={"prompt": "x", "num_images": 5}).status_code == 422
        assert (
            client.post(
                url, json={"prompt": "x", "input_paths": ["characters/x/images/nope.png"]}
            ).status_code
            == 422
        )
        assert client.post(url, json={"prompt": "x", "aspect_ratio": "auto"}).status_code == 422
        assert client.post(url, json={"prompt": "x", "aspect_ratio": "7:3"}).status_code == 422
