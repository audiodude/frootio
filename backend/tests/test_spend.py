from __future__ import annotations

import sqlite3

import pytest

from frootio import db, events, spend

from .conftest import make_episode


async def test_log_fills_series_and_publishes(conn: sqlite3.Connection) -> None:
    series_id, ep = make_episode(conn)
    sub = events.subscribe()
    try:
        spend.log(conn, "video", 1.2, episode_id=ep, note="take 1")
        ev = sub.queue.get_nowait()
    finally:
        events.unsubscribe(sub)
    assert ev["type"] == "spend" and ev["series_id"] == series_id and ev["episode_id"] == ep
    row = conn.execute("SELECT series_id, note FROM spend").fetchone()
    assert tuple(row) == (series_id, "take 1")


def test_totals_per_episode_and_series(conn: sqlite3.Connection) -> None:
    series_id, ep1 = make_episode(conn, 1)
    _, ep2 = make_episode(conn, 2)
    ts = db.now()
    char_id = conn.execute(
        "INSERT INTO characters (name, slug, created_at, updated_at) VALUES ('Kiwi', 'kiwi', ?, ?)", (ts, ts)
    ).lastrowid
    spend.log(conn, "video", 1.20, episode_id=ep1)
    spend.log(conn, "video", 0.40, episode_id=ep1)
    spend.log(conn, "llm", 0.01, episode_id=ep1)
    spend.log(conn, "video", 0.80, episode_id=ep2)
    spend.log(conn, "image", 0.039, character_id=char_id)  # not tied to a series
    spend.log(conn, "tts", 0.02, series_id=series_id, character_id=char_id)

    assert spend.episode_total(conn, ep1) == pytest.approx(1.61)
    assert spend.episode_total(conn, ep1, "video") == pytest.approx(1.60)
    assert spend.series_total(conn, series_id) == pytest.approx(2.43)

    t = spend.totals(conn)
    assert t["total_usd"] == pytest.approx(2.469)
    assert t["unattributed_usd"] == pytest.approx(0.039)
    (s,) = t["series"]
    assert s["total_usd"] == pytest.approx(2.43)
    assert s["by_kind"]["tts"] == pytest.approx(0.02)
    e1, e2 = s["episodes"]
    assert (e1["number"], e1["video_usd"], e1["spend_cap_usd"]) == (1, pytest.approx(1.6), 20.0)
    assert e2["total_usd"] == pytest.approx(0.8)


def test_log_rejects_unknown_kind(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        spend.log(conn, "bribe", 1.0)  # type: ignore[arg-type]
