"""HTTP API for generate/estimate/takes (no worker running: jobs stay queued)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frootio import db, generation, media, spend
from frootio.app import create_app

from .genkit import World, add_snippet, make_world, setup, take_row


@pytest.fixture
def world(conn: sqlite3.Connection, tmp_path: Path) -> World:
    return make_world(conn, tmp_path)


@pytest.fixture
def client(conn: sqlite3.Connection, tmp_path: Path) -> Iterator[TestClient]:
    # No `with`: lifespan (and the job worker) doesn't run, so generated takes stay queued.
    yield TestClient(create_app(tmp_path / "dist"))


def finished_take(conn: sqlite3.Connection, world: World, snippet_id: int, status: str = "done") -> int:
    ts = db.now()
    tid = conn.execute(
        "INSERT INTO takes (snippet_id, episode_id, status, requested_duration_s, created_at, updated_at)"
        " VALUES (?, ?, ?, 5, ?, ?)",
        (snippet_id, world.episode_id, status, ts, ts),
    ).lastrowid
    paths = media.take_paths("test-series", 1, snippet_id, tid)
    media.write_file(paths["video"], world.video_src.read_bytes())
    conn.execute(
        "UPDATE takes SET video_path = ?, video_duration_s = 2.0 WHERE id = ?", (paths["video"], tid)
    )
    conn.commit()
    return tid


def test_estimate_and_generate(
    client: TestClient,
    conn: sqlite3.Connection,
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fal = setup(monkeypatch, world, tmp_path)
    conn.execute("UPDATE episodes SET take_count = 2 WHERE id = ?", (world.episode_id,))
    conn.commit()
    sid = add_snippet(conn, world, 0, duration=10)
    est = client.get(f"/api/snippets/{sid}/estimate").json()
    assert est["n"] == 2 and est["default_n"] == 2 and est["duration_s"] == 10
    assert est["ref_tokens"] == 1224 and est["per_take_usd"] == pytest.approx(0.80)
    assert est["total_usd"] == pytest.approx(1.60) and est["blocked"] is None and est["cap_error"] is None
    assert client.get(f"/api/snippets/{sid}/estimate?n=5").json()["total_usd"] == pytest.approx(4.0)

    r = client.post(f"/api/snippets/{sid}/generate", json={})
    assert r.status_code == 201
    takes = r.json()["takes"]
    assert len(takes) == 2 and {t["status"] for t in takes} == {"queued"}
    assert all(t["can_cancel"] and not t["selected"] and not t["slow"] for t in takes)

    # Pinned seed on the snippet is used when the body omits `seed`; an explicit null means random.
    conn.execute("UPDATE snippets SET pinned_seed = 9 WHERE id = ?", (sid,))
    conn.commit()
    r = client.post(f"/api/snippets/{sid}/generate", json={"n": 2})
    assert [t["seed_requested"] for t in r.json()["takes"]] == [9, 10]
    r = client.post(f"/api/snippets/{sid}/generate", json={"n": 1, "seed": None})
    assert r.json()["takes"][0]["seed_requested"] is None
    assert client.post(f"/api/snippets/{sid}/generate", json={"n": 0}).status_code == 422
    assert fal.count("upload") == 6

    # In-flight estimates count toward the cap.
    est = client.get(f"/api/snippets/{sid}/estimate?n=1").json()
    assert est["cap"]["in_flight_usd"] == pytest.approx(0.8 * 5)


def test_generate_cap_409(
    client: TestClient,
    conn: sqlite3.Connection,
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup(monkeypatch, world, tmp_path)
    sid = add_snippet(conn, world, 0, duration=5)
    spend.log(conn, "video", 19.9, episode_id=world.episode_id)
    est = client.get(f"/api/snippets/{sid}/estimate").json()
    assert est["cap"]["exceeded"] and "spend cap" in est["cap_error"]
    r = client.post(f"/api/snippets/{sid}/generate", json={"n": 1})
    assert r.status_code == 409 and "Episode spend cap $20.00 would be exceeded" in r.json()["detail"]
    assert conn.execute("SELECT COUNT(*) FROM takes").fetchone()[0] == 0


def test_generate_blocked_by_continuity_409(
    client: TestClient,
    conn: sqlite3.Connection,
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup(monkeypatch, world, tmp_path)
    add_snippet(conn, world, 0)
    second = add_snippet(conn, world, 1, continue_prev=True)
    r = client.post(f"/api/snippets/{second}/generate", json={"n": 1})
    assert r.status_code == 409 and r.json()["detail"] == "Previous snippet has no selected take"
    assert client.post("/api/snippets/999/generate", json={}).status_code == 404


def test_list_select_hide_rules(
    client: TestClient,
    conn: sqlite3.Connection,
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup(monkeypatch, world, tmp_path)
    first = add_snippet(conn, world, 0)
    second = add_snippet(conn, world, 1, continue_prev=True)
    a = finished_take(conn, world, first)
    b = finished_take(conn, world, first)
    failed = finished_take(conn, world, first, status="failed")
    conn.execute("UPDATE takes SET video_path = NULL WHERE id = ?", (failed,))
    conn.commit()

    listing = client.get(f"/api/snippets/{first}/takes").json()
    assert [t["id"] for t in listing["takes"]] == [failed, b, a]  # newest first
    assert listing["takes"][1]["video_url"] == media.url(take_row(conn, b)["video_path"])

    assert client.post(f"/api/takes/{failed}/select").status_code == 409  # no video
    r = client.post(f"/api/takes/{a}/select")
    assert r.status_code == 200 and r.json()["take"]["selected"] is True
    assert r.json()["stale_snippet_ids"] == []  # second has no selected take yet: blocked, not stale
    assert conn.execute("SELECT selected_take_id FROM snippets WHERE id = ?", (first,)).fetchone()[0] == a

    # Selected take can't be hidden; others can, and show_hidden brings them back.
    r = client.post(f"/api/takes/{a}/hide")
    assert r.status_code == 409 and "selected" in r.json()["detail"]
    assert client.post(f"/api/takes/{b}/hide").json()["hidden"] is True
    listing = client.get(f"/api/snippets/{first}/takes").json()
    assert [t["id"] for t in listing["takes"]] == [failed, a] and listing["hidden_count"] == 1
    listing = client.get(f"/api/snippets/{first}/takes?show_hidden=true").json()
    assert [t["id"] for t in listing["takes"]] == [failed, b, a]
    assert client.post(f"/api/takes/{b}/unhide").json()["hidden"] is False

    # Second snippet's selected take used a's frame; selecting b makes it stale.
    s2 = finished_take(conn, world, second)
    conn.execute(
        "UPDATE takes SET start_frame_take_id = ?, start_frame_time_s = 2.0, start_frame_path = 'x.png'"
        " WHERE id = ?",
        (a, s2),
    )
    conn.execute("UPDATE snippets SET selected_take_id = ? WHERE id = ?", (s2, second))
    conn.commit()
    assert client.post(f"/api/takes/{a}/select").json()["stale_snippet_ids"] == []
    assert client.post(f"/api/takes/{b}/select").json()["stale_snippet_ids"] == [second]
    # The previously selected take can now be hidden.
    assert client.post(f"/api/takes/{a}/hide").status_code == 200


def test_slow_flag_in_listing(client: TestClient, conn: sqlite3.Connection, world: World) -> None:
    sid = add_snippet(conn, world, 0)
    old = (datetime.now(UTC) - timedelta(minutes=45)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO takes (snippet_id, episode_id, status, requested_duration_s, created_at, updated_at)"
        " VALUES (?, ?, 'running', 5, ?, ?)",
        (sid, world.episode_id, old, old),
    )
    conn.commit()
    [t] = client.get(f"/api/snippets/{sid}/takes").json()["takes"]
    assert t["slow"] is True and t["can_cancel"] is True


def test_take_prompt_and_cancel_retry_endpoints(
    client: TestClient,
    conn: sqlite3.Connection,
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fal = setup(monkeypatch, world, tmp_path)
    sid = add_snippet(conn, world, 0)
    [take] = client.post(f"/api/snippets/{sid}/generate", json={"n": 1, "seed": 5}).json()["takes"]
    p = client.get(f"/api/takes/{take['id']}/prompt").json()
    assert set(p["parts"]) == {"style", "legend", "location", "body", "tail"}
    assert p["parts"]["style"] == {"text": "Pixar style.", "overridden": True}
    assert p["parts"]["legend"]["overridden"] is False
    assert p["joined"].startswith("Pixar style.") and p["char_count"] == len(p["joined"])
    assert [r["kind"] for r in p["refs"]] == ["image", "audio"] and p["seed_requested"] == 5
    assert p["request_args"]["seed"] == 5

    # Not yet submitted (no worker): cancel needs no fal call.
    r = client.post(f"/api/takes/{take['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["cost_usd"] == 0
    assert fal.count("cancel") == 0
    assert client.post(f"/api/takes/{take['id']}/cancel").status_code == 409
    r = client.post(f"/api/takes/{take['id']}/retry")
    assert r.status_code == 409 and "can be retried" in r.json()["detail"]
    assert client.get("/api/takes/12345").status_code == 404
    assert generation.take_summary(take_row(conn, take["id"]), None)["can_retry"] is False
