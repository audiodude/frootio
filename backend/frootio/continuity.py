"""Continue-from-previous: predecessor, expected start frame, staleness.

The start frame of a snippet with "continue from previous" on is the frame at
the previous active snippet's selected take's trim-out point (trim_out_s, or
the end of the video when no trim-out is set). A snippet is stale when its
selected take's recorded start frame (source take + time) differs from the one
it would get now. The toggle has no effect on the first snippet.
"""

from __future__ import annotations

import sqlite3

TIME_TOLERANCE_S = 1e-3
NO_SELECTED_TAKE = "Previous snippet has no selected take"

StartFrame = tuple[int, float | None]


def predecessor(conn: sqlite3.Connection, snippet_id: int) -> sqlite3.Row | None:
    """Previous active snippet on the board (by position, then id), or None if first/archived."""
    s = conn.execute(
        "SELECT id, episode_id, position, archived FROM snippets WHERE id = ?", (snippet_id,)
    ).fetchone()
    if s is None or s["archived"]:
        return None
    return conn.execute(
        "SELECT * FROM snippets WHERE episode_id = ? AND archived = 0 AND id != ?"
        " AND (position < ? OR (position = ? AND id < ?))"
        " ORDER BY position DESC, id DESC LIMIT 1",
        (s["episode_id"], s["id"], s["position"], s["position"], s["id"]),
    ).fetchone()


def effective_trim_out(take: sqlite3.Row) -> float | None:
    return take["trim_out_s"] if take["trim_out_s"] is not None else take["video_duration_s"]


def _continues(conn: sqlite3.Connection, snippet_id: int) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    """(snippet, predecessor) when continuation is in effect, else (snippet, None)."""
    s = conn.execute("SELECT * FROM snippets WHERE id = ?", (snippet_id,)).fetchone()
    if s is None or not s["continue_from_previous"] or s["archived"]:
        return s, None
    return s, predecessor(conn, snippet_id)


def expected_start(conn: sqlite3.Connection, snippet_id: int) -> StartFrame | None:
    """(source take id, trim-out time) the snippet would start from now, or None."""
    _, pred = _continues(conn, snippet_id)
    if pred is None or pred["selected_take_id"] is None:
        return None
    take = conn.execute("SELECT * FROM takes WHERE id = ?", (pred["selected_take_id"],)).fetchone()
    if take is None:
        return None
    return take["id"], effective_trim_out(take)


def blocked_reason(conn: sqlite3.Connection, snippet_id: int) -> str | None:
    """Why Generate is blocked by continuity, or None."""
    _, pred = _continues(conn, snippet_id)
    if pred is not None and pred["selected_take_id"] is None:
        return NO_SELECTED_TAKE
    return None


def _same(a: StartFrame | None, b: StartFrame | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if a[0] != b[0]:
        return False
    if a[1] is None or b[1] is None:
        return a[1] is None and b[1] is None
    return abs(a[1] - b[1]) <= TIME_TOLERANCE_S


def is_stale(conn: sqlite3.Connection, snippet_id: int) -> bool:
    s, pred = _continues(conn, snippet_id)
    if s is None or pred is None or s["selected_take_id"] is None:
        return False
    take = conn.execute(
        "SELECT start_frame_take_id, start_frame_time_s FROM takes WHERE id = ?", (s["selected_take_id"],)
    ).fetchone()
    if take is None:
        return False
    used: StartFrame | None = (
        (take["start_frame_take_id"], take["start_frame_time_s"])
        if take["start_frame_take_id"] is not None
        else None
    )
    return not _same(used, expected_start(conn, snippet_id))
