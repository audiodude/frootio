from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frootio import config, db, media, spend
from frootio.app import create_app

from .conftest import make_episode


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><div id=root></div>")
    (d / "assets" / "app.js").write_text("console.log(1)")
    return d


def test_startup_migrates_backs_up_and_serves_health(dist: Path, env: Path) -> None:
    with TestClient(create_app(dist)) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        latest = max([1, *(int(p.name.split("_", 1)[0]) for p in db.MIGRATIONS_DIR.glob("[0-9]*_*.sql"))])
        assert body["ok"] is True and body["schema_version"] == latest
        assert body["fal_configured"] is False
        assert body["media_root"] == str(env / "media")
    backups = list((env / "media" / config.BACKUP_DIRNAME).glob("frootio-*.db"))
    assert len(backups) == 1


def test_spa_fallback_and_static(dist: Path) -> None:
    with TestClient(create_app(dist)) as client:
        assert "id=root" in client.get("/").text
        assert "id=root" in client.get("/episodes/3/assembly").text
        assert client.get("/assets/app.js").text == "console.log(1)"
        r = client.get("/api/nope")
        assert r.status_code == 404 and r.json() == {"detail": "not found"}


def test_spa_missing_build_is_503(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "missing")) as client:
        assert client.get("/").status_code == 503
        assert client.get("/api/health").status_code == 200


def test_media_mount(dist: Path) -> None:
    rel = media.store_bytes("characters/kiwi/images", b"\x89PNG", "png")
    with TestClient(create_app(dist)) as client:
        assert client.get(media.url(rel)).content == b"\x89PNG"
        assert client.get("/media/does/not/exist.png").status_code == 404


def test_spend_endpoints(dist: Path) -> None:
    with TestClient(create_app(dist)) as client:
        conn = db.connect()
        try:
            series_id, ep = make_episode(conn)
            spend.log(conn, "video", 2.5, episode_id=ep)
        finally:
            conn.close()
        t = client.get("/api/spend").json()
        assert t["total_usd"] == 2.5
        assert t["series"][0]["episodes"][0]["video_usd"] == 2.5
        assert client.get(f"/api/spend/series/{series_id}").json()["total_usd"] == 2.5
        assert client.get(f"/api/spend/episodes/{ep}").json()["video_usd"] == 2.5
        assert client.get("/api/spend/episodes/999").status_code == 404
