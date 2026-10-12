"""Spend logging and totals (per episode and per series).

Video takes are logged with kind `video` (only those count toward the episode
cap); nano-banana `image`, Chatterbox `tts`, OpenRouter `llm`.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from . import db, events

Kind = Literal["video", "image", "tts", "llm"]
KINDS: tuple[Kind, ...] = ("video", "image", "tts", "llm")


def log(
    conn: sqlite3.Connection,
    kind: Kind,
    amount_usd: float,
    *,
    episode_id: int | None = None,
    series_id: int | None = None,
    character_id: int | None = None,
    location_id: int | None = None,
    take_id: int | None = None,
    note: str = "",
) -> int:
    """Insert a spend row (commits `conn`) and publish a `spend` event.

    `series_id` is filled from the episode when only `episode_id` is given, so
    series totals are a plain sum over `series_id`.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown spend kind {kind!r}")
    if episode_id is not None and series_id is None:
        r = conn.execute("SELECT series_id FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        if r is None:
            raise ValueError(f"episode {episode_id} not found")
        series_id = r[0]
    cur = conn.execute(
        "INSERT INTO spend (kind, amount_usd, episode_id, series_id, character_id, location_id,"
        " take_id, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, amount_usd, episode_id, series_id, character_id, location_id, take_id, note, db.now()),
    )
    conn.commit()
    events.publish(
        {
            "type": "spend",
            "id": cur.lastrowid,
            "kind": kind,
            "amount_usd": amount_usd,
            "episode_id": episode_id,
            "series_id": series_id,
        }
    )
    return cur.lastrowid


def _by_kind(conn: sqlite3.Connection, where: str, params: tuple[Any, ...]) -> dict[str, float]:
    out = {k: 0.0 for k in KINDS}
    for r in conn.execute(
        f"SELECT kind, COALESCE(SUM(amount_usd), 0) FROM spend WHERE {where} GROUP BY kind", params
    ):
        out[r[0]] = r[1]
    return out


def episode_total(conn: sqlite3.Connection, episode_id: int, kind: Kind | None = None) -> float:
    sql = "SELECT COALESCE(SUM(amount_usd), 0) FROM spend WHERE episode_id = ?"
    params: tuple[Any, ...] = (episode_id,)
    if kind is not None:
        sql += " AND kind = ?"
        params += (kind,)
    return conn.execute(sql, params).fetchone()[0]


def series_total(conn: sqlite3.Connection, series_id: int) -> float:
    return conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0) FROM spend WHERE series_id = ?", (series_id,)
    ).fetchone()[0]


def episode_summary(conn: sqlite3.Connection, episode_id: int) -> dict[str, Any] | None:
    ep = conn.execute(
        "SELECT id, series_id, number, title, spend_cap_usd FROM episodes WHERE id = ?", (episode_id,)
    ).fetchone()
    if ep is None:
        return None
    by_kind = _by_kind(conn, "episode_id = ?", (episode_id,))
    return {
        "id": ep["id"],
        "series_id": ep["series_id"],
        "number": ep["number"],
        "title": ep["title"],
        "spend_cap_usd": ep["spend_cap_usd"],
        "total_usd": sum(by_kind.values()),
        "video_usd": by_kind["video"],
        "by_kind": by_kind,
    }


def series_summary(conn: sqlite3.Connection, series_id: int) -> dict[str, Any] | None:
    s = conn.execute("SELECT id, name, slug FROM series WHERE id = ?", (series_id,)).fetchone()
    if s is None:
        return None
    by_kind = _by_kind(conn, "series_id = ?", (series_id,))
    episode_ids = [
        r[0]
        for r in conn.execute("SELECT id FROM episodes WHERE series_id = ? ORDER BY number", (series_id,))
    ]
    return {
        "id": s["id"],
        "name": s["name"],
        "slug": s["slug"],
        "total_usd": sum(by_kind.values()),
        "by_kind": by_kind,
        "episodes": [episode_summary(conn, eid) for eid in episode_ids],
    }


def totals(conn: sqlite3.Connection) -> dict[str, Any]:
    by_kind = _by_kind(conn, "1", ())
    series_ids = [r[0] for r in conn.execute("SELECT id FROM series ORDER BY name")]
    unattributed = conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0) FROM spend WHERE series_id IS NULL"
    ).fetchone()[0]
    return {
        "total_usd": sum(by_kind.values()),
        "by_kind": by_kind,
        "unattributed_usd": unattributed,  # e.g. images for characters not tied to a series
        "series": [series_summary(conn, sid) for sid in series_ids],
    }
