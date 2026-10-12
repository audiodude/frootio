"""Takes: list per snippet, detail/prompt, select, hide/unhide, cancel, retry."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException

from .. import continuity, db, events, generation

router = APIRouter(tags=["takes"])


def _take(conn: sqlite3.Connection, take_id: int) -> sqlite3.Row:
    t = conn.execute("SELECT * FROM takes WHERE id = ?", (take_id,)).fetchone()
    if t is None:
        raise HTTPException(404, "take not found")
    return t


def _selected(conn: sqlite3.Connection, snippet_id: int) -> int | None:
    return conn.execute("SELECT selected_take_id FROM snippets WHERE id = ?", (snippet_id,)).fetchone()[0]


def _summary(conn: sqlite3.Connection, take_id: int) -> dict[str, Any]:
    t = _take(conn, take_id)
    return generation.take_summary(t, _selected(conn, t["snippet_id"]))


@router.get("/snippets/{snippet_id}/takes")
def list_takes(snippet_id: int, conn: db.Conn, show_hidden: bool = False) -> dict[str, Any]:
    snippet = conn.execute("SELECT id, selected_take_id FROM snippets WHERE id = ?", (snippet_id,)).fetchone()
    if snippet is None:
        raise HTTPException(404, "snippet not found")
    takes = conn.execute(
        "SELECT * FROM takes WHERE snippet_id = ? ORDER BY id DESC", (snippet_id,)
    ).fetchall()
    selected = snippet["selected_take_id"]
    visible = [t for t in takes if show_hidden or not t["hidden"] or t["id"] == selected]
    return {
        "snippet_id": snippet_id,
        "selected_take_id": selected,
        "hidden_count": sum(1 for t in takes if t["hidden"]),
        "takes": [generation.take_summary(t, selected) for t in visible],
    }


@router.get("/takes/{take_id}")
def get_take(take_id: int, conn: db.Conn) -> dict[str, Any]:
    return _summary(conn, take_id)


@router.get("/takes/{take_id}/prompt")
def get_take_prompt(take_id: int, conn: db.Conn) -> dict[str, Any]:
    return generation.take_prompt(_take(conn, take_id))


@router.post("/takes/{take_id}/select")
def select_take(take_id: int, conn: db.Conn) -> dict[str, Any]:
    take = _take(conn, take_id)
    if take["video_path"] is None or take["status"] not in generation.HAS_VIDEO:
        raise HTTPException(409, f"Only a take with a video can be selected (this one is {take['status']})")
    ts = db.now()
    conn.execute(
        "UPDATE snippets SET selected_take_id = ?, updated_at = ? WHERE id = ?",
        (take_id, ts, take["snippet_id"]),
    )
    conn.execute("UPDATE takes SET hidden = 0, updated_at = ? WHERE id = ?", (ts, take_id))
    conn.commit()
    # The next snippet's expected start frame comes from this selection; report which
    # snippets are now stale.
    stale: list[int] = []
    for s in conn.execute(
        "SELECT id FROM snippets WHERE episode_id = ? AND archived = 0 ORDER BY position, id",
        (take["episode_id"],),
    ).fetchall():
        pred = continuity.predecessor(conn, s["id"])
        if pred is not None and pred["id"] == take["snippet_id"]:
            if continuity.is_stale(conn, s["id"]):
                stale.append(s["id"])
            events.publish({"type": "snippet", "id": s["id"], "episode_id": take["episode_id"]})
    events.publish({"type": "snippet", "id": take["snippet_id"], "episode_id": take["episode_id"]})
    generation.publish_take(conn, take_id)
    return {"take": _summary(conn, take_id), "stale_snippet_ids": stale}


def _set_hidden(conn: sqlite3.Connection, take_id: int, hidden: bool) -> dict[str, Any]:
    take = _take(conn, take_id)
    if hidden and _selected(conn, take["snippet_id"]) == take_id:
        raise HTTPException(409, "The selected take can't be hidden")
    conn.execute("UPDATE takes SET hidden = ?, updated_at = ? WHERE id = ?", (int(hidden), db.now(), take_id))
    conn.commit()
    generation.publish_take(conn, take_id)
    return _summary(conn, take_id)


@router.post("/takes/{take_id}/hide")
def hide_take(take_id: int, conn: db.Conn) -> dict[str, Any]:
    return _set_hidden(conn, take_id, True)


@router.post("/takes/{take_id}/unhide")
def unhide_take(take_id: int, conn: db.Conn) -> dict[str, Any]:
    return _set_hidden(conn, take_id, False)


@router.post("/takes/{take_id}/cancel")
async def cancel_take(take_id: int, conn: db.Conn) -> dict[str, Any]:
    try:
        await generation.cancel(conn, take_id)
    except generation.GenerateError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    return _summary(conn, take_id)


@router.post("/takes/{take_id}/retry")
async def retry_take(take_id: int, conn: db.Conn) -> dict[str, Any]:
    try:
        new_id = await generation.retry(conn, take_id)
    except generation.GenerateError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    return _summary(conn, new_id)
