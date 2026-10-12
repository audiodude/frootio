"""Snippet board and side panel: CRUD-ish edits, archive/restore/copy, order, prompt parts."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import config, db, openrouter, prompt, snippets, split

router = APIRouter(tags=["snippets"])


def _episode_or_404(conn: sqlite3.Connection, episode_id: int) -> sqlite3.Row:
    ep = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if ep is None:
        raise HTTPException(404, "episode not found")
    return ep


def _snippet_or_404(conn: sqlite3.Connection, snippet_id: int) -> sqlite3.Row:
    s = snippets.get(conn, snippet_id)
    if s is None:
        raise HTTPException(404, "snippet not found")
    return s


def _out(conn: sqlite3.Connection, snippet_id: int) -> dict[str, Any]:
    return snippets.serialize(conn, _snippet_or_404(conn, snippet_id))


def _changed(conn: sqlite3.Connection, s: sqlite3.Row) -> dict[str, Any]:
    conn.commit()
    snippets.publish(s["episode_id"], id=s["id"])
    return _out(conn, s["id"])


@router.get("/episodes/{episode_id}/snippets")
def get_board(episode_id: int, conn: db.Conn) -> dict[str, Any]:
    _episode_or_404(conn, episode_id)
    return snippets.board(conn, episode_id)


@router.post("/episodes/{episode_id}/snippets")
def add_snippet(episode_id: int, conn: db.Conn) -> dict[str, Any]:
    """Blank snippet at the end of the board (location = episode default)."""
    ep = _episode_or_404(conn, episode_id)
    new_id = snippets.insert(
        conn, episode_id, snippets.NewSnippet(location_id=ep["default_location_id"]), origin="manual"
    )
    return _changed(conn, _snippet_or_404(conn, new_id))


class Order(BaseModel):
    ids: list[int]


@router.put("/episodes/{episode_id}/snippets/order")
def set_order(episode_id: int, body: Order, conn: db.Conn) -> dict[str, Any]:
    _episode_or_404(conn, episode_id)
    try:
        snippets.reorder(conn, episode_id, body.ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    conn.commit()
    snippets.publish(episode_id)
    return snippets.board(conn, episode_id)


@router.get("/snippets/{snippet_id}")
def get_snippet(snippet_id: int, conn: db.Conn) -> dict[str, Any]:
    return _out(conn, snippet_id)


class SnippetPatch(BaseModel):
    cast_ids: list[int] | None = None
    location_id: int | None = None
    target_duration_s: int | None = Field(
        default=None, ge=config.SNIPPET_MIN_SECONDS, le=config.SNIPPET_MAX_SECONDS
    )
    continue_from_previous: bool | None = None
    script_slice: str | None = None
    body: str | None = None
    tail: str | None = None
    pinned_seed: int | None = None


@router.patch("/snippets/{snippet_id}")
def patch_snippet(snippet_id: int, body: SnippetPatch, conn: db.Conn) -> dict[str, Any]:
    s = _snippet_or_404(conn, snippet_id)
    fields = body.model_dump(include=body.model_fields_set)
    for key in ("cast_ids", "target_duration_s", "continue_from_previous", "script_slice", "body", "tail"):
        if key in fields and fields[key] is None:
            raise HTTPException(422, f"{key} cannot be null")
    if fields.get("continue_from_previous") and snippets.is_first(conn, s):
        raise HTTPException(400, "continue from previous is disabled on the first snippet")
    try:
        if "cast_ids" in fields:
            snippets.set_cast(conn, snippet_id, fields.pop("cast_ids"))
        if "location_id" in fields:
            snippets.check_location(conn, fields["location_id"])
    except ValueError as exc:
        conn.rollback()
        raise HTTPException(400, str(exc)) from exc
    if "continue_from_previous" in fields:
        fields["continue_from_previous"] = int(fields["continue_from_previous"])
    sets = [f"{k} = ?" for k in fields] + ["updated_at = ?"]
    conn.execute(
        f"UPDATE snippets SET {', '.join(sets)} WHERE id = ?", [*fields.values(), db.now(), snippet_id]
    )
    return _changed(conn, s)


@router.delete("/snippets/{snippet_id}")
@router.post("/snippets/{snippet_id}/archive")
def archive_snippet(snippet_id: int, conn: db.Conn) -> dict[str, Any]:
    """Delete means archive: the snippet keeps its takes, media and spend."""
    s = _snippet_or_404(conn, snippet_id)
    snippets.archive(conn, snippet_id)
    return _changed(conn, s)


@router.post("/snippets/{snippet_id}/restore")
def restore_snippet(snippet_id: int, conn: db.Conn) -> dict[str, Any]:
    s = _snippet_or_404(conn, snippet_id)
    snippets.restore(conn, snippet_id)
    return _changed(conn, s)


@router.post("/snippets/{snippet_id}/copy")
def copy_snippet(snippet_id: int, conn: db.Conn) -> dict[str, Any]:
    """Copy the snippet's prompt (parts, cast, location, slice, target) into a new snippet at the end."""
    _snippet_or_404(conn, snippet_id)
    new_id = snippets.copy(conn, snippet_id)
    return _changed(conn, _snippet_or_404(conn, new_id))


@router.get("/snippets/{snippet_id}/prompt")
def get_prompt(snippet_id: int, conn: db.Conn) -> dict[str, Any]:
    return prompt.build_from_row(conn, _snippet_or_404(conn, snippet_id)).as_dict()


class PartIn(BaseModel):
    text: str | None = None
    overridden: bool = True


class PartsIn(BaseModel):
    parts: dict[str, PartIn]


@router.put("/snippets/{snippet_id}/parts")
def put_parts(snippet_id: int, body: PartsIn, conn: db.Conn) -> dict[str, Any]:
    """Partial update; `overridden: false` on a derived part resets it to inherited."""
    s = _snippet_or_404(conn, snippet_id)
    try:
        prompt.update_parts(conn, snippet_id, {k: v.model_dump() for k, v in body.parts.items()})
    except ValueError as exc:
        conn.rollback()
        raise HTTPException(400, str(exc)) from exc
    conn.commit()
    snippets.publish(s["episode_id"], id=snippet_id)
    return prompt.build_prompt(conn, snippet_id).as_dict()


class RewriteIn(BaseModel):
    model: str | None = None
    instructions: str = ""


@router.post("/snippets/{snippet_id}/rewrite-body")
async def rewrite_body(snippet_id: int, body: RewriteIn) -> dict[str, Any]:
    """Ask the LLM for a new body (returned, not saved; the panel applies it via PUT parts)."""
    conn = db.connect()
    try:
        _snippet_or_404(conn, snippet_id)
        return await split.rewrite_body(conn, snippet_id, body.model, body.instructions)
    except (split.SplitError, openrouter.OpenRouterError) as exc:
        raise HTTPException(502, str(exc)) from exc
    finally:
        conn.close()
