"""Script versions, lint, and splitting (Level 0 / Level 2 proposals)."""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, script, split

router = APIRouter(tags=["scripts"])


def _episode_or_404(conn: sqlite3.Connection, episode_id: int) -> sqlite3.Row:
    ep = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if ep is None:
        raise HTTPException(404, "episode not found")
    return ep


def _lint(conn: sqlite3.Connection, ep: sqlite3.Row, text: str) -> dict[str, Any]:
    return script.lint_report(script.parse(text, script.speaker_map(conn, ep["series_id"])))


def _version_out(conn: sqlite3.Connection, ep: sqlite3.Row, row: sqlite3.Row | None) -> dict[str, Any]:
    text = row["text"] if row else ""
    meta = (
        {"id": row["id"], "version": row["version"], "source": row["source"], "created_at": row["created_at"]}
        if row
        else {"id": None, "version": None, "source": None, "created_at": None}
    )
    return {**meta, "text": text, **_lint(conn, ep, text)}


@router.get("/episodes/{episode_id}/script")
def get_script(episode_id: int, conn: db.Conn, version: int | None = None) -> dict[str, Any]:
    ep = _episode_or_404(conn, episode_id)
    if version is None:
        row = script.latest_version(conn, episode_id)
    else:
        row = conn.execute(
            "SELECT * FROM script_versions WHERE episode_id = ? AND version = ?", (episode_id, version)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"script version {version} not found")
    return _version_out(conn, ep, row)


@router.get("/episodes/{episode_id}/script/versions")
def list_versions(episode_id: int, conn: db.Conn) -> list[dict[str, Any]]:
    _episode_or_404(conn, episode_id)
    return db.rows(
        conn.execute(
            "SELECT id, version, source, created_at, length(text) AS chars FROM script_versions"
            " WHERE episode_id = ? ORDER BY version DESC",
            (episode_id,),
        ).fetchall()
    )


class ScriptText(BaseModel):
    text: str


@router.post("/episodes/{episode_id}/script")
def save_script(episode_id: int, body: ScriptText, conn: db.Conn) -> dict[str, Any]:
    """Explicit save: a new version only when the text changed."""
    ep = _episode_or_404(conn, episode_id)
    row = script.save_version(conn, episode_id, body.text)
    out = _version_out(conn, ep, row or script.latest_version(conn, episode_id))
    out["created"] = row is not None
    return out


@router.post("/episodes/{episode_id}/script/lint")
def lint_script(episode_id: int, body: ScriptText, conn: db.Conn) -> dict[str, Any]:
    """Lint unsaved editor text against the series cast."""
    return _lint(conn, _episode_or_404(conn, episode_id), body.text)


class SplitRequest(BaseModel):
    level: Literal[0, 2]
    model: str | None = None
    version: int | None = None  # script version; default latest


@router.post("/episodes/{episode_id}/split")
def run_split(episode_id: int, body: SplitRequest, conn: db.Conn) -> dict[str, Any]:
    """Level 0 replaces the board now; Level 2 starts a job and returns the pending proposal."""
    _episode_or_404(conn, episode_id)
    try:
        if body.level == 0:
            return {"level": 0, **split.run_level0(conn, episode_id, body.version)}
        proposal_id = split.start_level2(conn, episode_id, body.version, body.model)
    except split.SplitError as exc:
        raise HTTPException(400, str(exc)) from exc
    row = conn.execute("SELECT * FROM split_proposals WHERE id = ?", (proposal_id,)).fetchone()
    return {"level": 2, "proposal": split.proposal_dict(conn, row)}


@router.get("/episodes/{episode_id}/split-proposals")
def list_proposals(
    episode_id: int, conn: db.Conn, level: int | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    _episode_or_404(conn, episode_id)
    sql = "SELECT * FROM split_proposals WHERE episode_id = ?"
    params: list[Any] = [episode_id]
    if level is not None:
        sql += " AND level = ?"
        params.append(level)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [split.proposal_dict(conn, r) for r in conn.execute(sql, params).fetchall()]


def _proposal_or_404(conn: sqlite3.Connection, proposal_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM split_proposals WHERE id = ?", (proposal_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "split proposal not found")
    return row


@router.get("/split-proposals/{proposal_id}")
def get_proposal(proposal_id: int, conn: db.Conn) -> dict[str, Any]:
    return split.proposal_dict(conn, _proposal_or_404(conn, proposal_id))


@router.post("/split-proposals/{proposal_id}/accept")
def accept_proposal(proposal_id: int, conn: db.Conn) -> dict[str, Any]:
    _proposal_or_404(conn, proposal_id)
    try:
        ids = split.accept(conn, proposal_id)
    except split.SplitError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"snippet_ids": ids, "proposal": split.proposal_dict(conn, _proposal_or_404(conn, proposal_id))}


@router.post("/split-proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: int, conn: db.Conn) -> dict[str, Any]:
    _proposal_or_404(conn, proposal_id)
    try:
        split.reject(conn, proposal_id)
    except split.SplitError as exc:
        raise HTTPException(409, str(exc)) from exc
    return split.proposal_dict(conn, _proposal_or_404(conn, proposal_id))
