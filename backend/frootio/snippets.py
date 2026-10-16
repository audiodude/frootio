"""Snippet board operations: create, edit, archive/restore/copy, reorder, serialize.

Nothing is hard-deleted: deleting a snippet archives it and it keeps its takes.
Active snippets are ordered by `position` (then id). Functions here don't
commit unless noted; callers commit and publish `snippet` events.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import config, continuity, db, events, media, prompt, script


@dataclass
class NewSnippet:
    body: str = ""
    script_slice: str = ""
    cast_ids: list[int] = field(default_factory=list)
    location_id: int | None = None
    target_duration_s: int = config.SNIPPET_MAX_SECONDS
    warnings: list[str] = field(default_factory=list)


def clamp_duration(seconds: float) -> int:
    """Estimate rounded up to whole seconds, clamped to 5-15."""
    whole = math.ceil(round(seconds, 6))
    return max(config.SNIPPET_MIN_SECONDS, min(config.SNIPPET_MAX_SECONDS, whole))


def publish(episode_id: int, **extra: Any) -> None:
    events.publish({"type": "snippet", "episode_id": episode_id, **extra})


def get(conn: sqlite3.Connection, snippet_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM snippets WHERE id = ?", (snippet_id,)).fetchone()


def active(conn: sqlite3.Connection, episode_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM snippets WHERE episode_id = ? AND archived = 0 ORDER BY position, id", (episode_id,)
    ).fetchall()


def _next_position(conn: sqlite3.Connection, episode_id: int) -> int:
    r = conn.execute(
        "SELECT MAX(position) FROM snippets WHERE episode_id = ? AND archived = 0", (episode_id,)
    ).fetchone()
    return 0 if r[0] is None else r[0] + 1


def set_cast(conn: sqlite3.Connection, snippet_id: int, character_ids: list[int]) -> None:
    seen: list[int] = []
    for cid in character_ids:
        if cid not in seen:
            seen.append(cid)
    for cid in seen:
        if conn.execute("SELECT 1 FROM characters WHERE id = ?", (cid,)).fetchone() is None:
            raise ValueError(f"character {cid} not found")
    conn.execute("DELETE FROM snippet_cast WHERE snippet_id = ?", (snippet_id,))
    conn.executemany(
        "INSERT INTO snippet_cast (snippet_id, character_id, position) VALUES (?, ?, ?)",
        [(snippet_id, cid, i) for i, cid in enumerate(seen)],
    )


def check_location(conn: sqlite3.Connection, location_id: int | None) -> None:
    if (
        location_id is not None
        and conn.execute("SELECT 1 FROM locations WHERE id = ?", (location_id,)).fetchone() is None
    ):
        raise ValueError(f"location {location_id} not found")


def insert(
    conn: sqlite3.Connection,
    episode_id: int,
    new: NewSnippet,
    *,
    origin: str = "manual",
    position: int | None = None,
    split_proposal_id: int | None = None,
) -> int:
    check_location(conn, new.location_id)
    ts = db.now()
    cur = conn.execute(
        "INSERT INTO snippets (episode_id, position, origin, split_proposal_id, body, script_slice,"
        " location_id, target_duration_s, warnings, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            episode_id,
            _next_position(conn, episode_id) if position is None else position,
            origin,
            split_proposal_id,
            new.body,
            new.script_slice,
            new.location_id,
            new.target_duration_s,
            db.dumps(new.warnings),
            ts,
            ts,
        ),
    )
    set_cast(conn, cur.lastrowid, new.cast_ids)
    return cur.lastrowid


def replace_board(
    conn: sqlite3.Connection,
    episode_id: int,
    new: list[NewSnippet],
    *,
    origin: str,
    split_proposal_id: int | None,
) -> list[int]:
    """Archive the current active snippets and put `new` on the board (no commit)."""
    ts = db.now()
    conn.execute(
        "UPDATE snippets SET archived = 1, archived_at = ?, updated_at = ?"
        " WHERE episode_id = ? AND archived = 0",
        (ts, ts, episode_id),
    )
    return [
        insert(conn, episode_id, n, origin=origin, position=i, split_proposal_id=split_proposal_id)
        for i, n in enumerate(new)
    ]


def archive(conn: sqlite3.Connection, snippet_id: int) -> None:
    ts = db.now()
    conn.execute(
        "UPDATE snippets SET archived = 1, archived_at = ?, updated_at = ? WHERE id = ? AND archived = 0",
        (ts, ts, snippet_id),
    )


def restore(conn: sqlite3.Connection, snippet_id: int) -> None:
    """Unarchive to the end of the board."""
    s = get(conn, snippet_id)
    if s is None or not s["archived"]:
        return
    conn.execute(
        "UPDATE snippets SET archived = 0, archived_at = NULL, position = ?, updated_at = ? WHERE id = ?",
        (_next_position(conn, s["episode_id"]), db.now(), snippet_id),
    )


def copy(conn: sqlite3.Connection, snippet_id: int) -> int:
    """New active snippet (end of board) with the source's prompt parts, cast, location, slice, target."""
    s = get(conn, snippet_id)
    if s is None:
        raise LookupError(f"snippet {snippet_id} not found")
    cast_ids = [
        r[0]
        for r in conn.execute(
            "SELECT character_id FROM snippet_cast WHERE snippet_id = ? ORDER BY position, character_id",
            (snippet_id,),
        )
    ]
    new_id = insert(
        conn,
        s["episode_id"],
        NewSnippet(
            body=s["body"],
            script_slice=s["script_slice"],
            cast_ids=cast_ids,
            location_id=s["location_id"],
            target_duration_s=s["target_duration_s"],
        ),
        origin="copy",
    )
    cols = ["tail"] + [
        f"{p}_{suffix}" for p in prompt.DERIVED for suffix in ("overridden", "override", "source_snapshot")
    ]
    conn.execute(
        f"UPDATE snippets SET {', '.join(f'{c} = ?' for c in cols)}, copied_from_snippet_id = ? WHERE id = ?",
        [s[c] for c in cols] + [snippet_id, new_id],
    )
    return new_id


def reorder(conn: sqlite3.Connection, episode_id: int, ids: list[int]) -> None:
    current = {r["id"] for r in active(conn, episode_id)}
    if len(ids) != len(set(ids)) or set(ids) != current:
        raise ValueError("order must list every active snippet of the episode exactly once")
    ts = db.now()
    conn.executemany(
        "UPDATE snippets SET position = ?, updated_at = ? WHERE id = ?",
        [(i, ts, sid) for i, sid in enumerate(ids)],
    )


def is_first(conn: sqlite3.Connection, snippet: sqlite3.Row) -> bool:
    return not snippet["archived"] and continuity.predecessor(conn, snippet["id"]) is None


def serialize(conn: sqlite3.Connection, s: sqlite3.Row) -> dict[str, Any]:
    out = dict(s)
    for k in ("archived", "continue_from_previous", *(f"{p}_overridden" for p in prompt.DERIVED)):
        out[k] = bool(out[k])
    out["warnings"] = db.loads(s["warnings"], [])
    out["cast"] = [
        {"id": r["id"], "name": r["name"], "archived": bool(r["archived"])}
        for r in conn.execute(
            "SELECT c.id, c.name, c.archived FROM snippet_cast sc JOIN characters c ON c.id = sc.character_id"
            " WHERE sc.snippet_id = ? ORDER BY sc.position, c.id",
            (s["id"],),
        )
    ]
    loc = (
        conn.execute("SELECT name FROM locations WHERE id = ?", (s["location_id"],)).fetchone()
        if s["location_id"] is not None
        else None
    )
    out["location_name"] = loc["name"] if loc else None
    out["estimate_s"] = round(script.estimate(s["script_slice"]), 2) if s["script_slice"].strip() else None
    out["take_count"] = conn.execute(
        "SELECT COUNT(*) FROM takes WHERE snippet_id = ?", (s["id"],)
    ).fetchone()[0]
    sel_poster = (
        conn.execute("SELECT poster_path FROM takes WHERE id = ?", (s["selected_take_id"],)).fetchone()
        if s["selected_take_id"] is not None
        else None
    )
    out["selected_take_poster_url"] = (
        media.url(sel_poster["poster_path"]) if sel_poster and sel_poster["poster_path"] else None
    )
    out["is_first"] = is_first(conn, s)
    out["stale"] = continuity.is_stale(conn, s["id"])
    built = prompt.build_from_row(conn, s)
    out["prompt_errors"] = built.errors
    reasons = [r for r in [continuity.blocked_reason(conn, s["id"]), *built.errors] if r]
    out["blocked_reason"] = "; ".join(reasons) or None
    return out


def board(conn: sqlite3.Connection, episode_id: int) -> dict[str, Any]:
    act = [serialize(conn, r) for r in active(conn, episode_id)]
    archived = [
        serialize(conn, r)
        for r in conn.execute(
            "SELECT * FROM snippets WHERE episode_id = ? AND archived = 1 ORDER BY archived_at DESC, id DESC",
            (episode_id,),
        ).fetchall()
    ]
    total = sum(s["target_duration_s"] for s in act)
    return {
        "active": act,
        "archived": archived,
        **episode_length(total),
    }


def episode_length(total_s: float) -> dict[str, Any]:
    lo, hi = config.EPISODE_TARGET_MIN_SECONDS, config.EPISODE_TARGET_MAX_SECONDS
    warning = None
    if total_s and not lo <= total_s <= hi:
        warning = f"Estimated length {total_s:.0f}s is outside the {lo}-{hi}s target"
    return {"estimated_total_s": total_s, "length_warning": warning}
