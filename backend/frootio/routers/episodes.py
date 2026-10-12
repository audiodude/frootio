"""Episode settings used by the workspace: GET (with cast, locations, spend) and PATCH."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db, split
from ..spend import episode_summary

router = APIRouter(prefix="/episodes", tags=["episodes"])


def _episode_or_404(conn: sqlite3.Connection, episode_id: int) -> sqlite3.Row:
    ep = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if ep is None:
        raise HTTPException(404, "episode not found")
    return ep


def episode_out(conn: sqlite3.Connection, ep: sqlite3.Row) -> dict[str, Any]:
    out = dict(ep)
    out["archived"] = bool(out["archived"])
    out["captions_on"] = bool(out["captions_on"])
    out["series"] = db.row(
        conn.execute(
            "SELECT id, name, slug, style_preamble FROM series WHERE id = ?", (ep["series_id"],)
        ).fetchone()
    )
    out["cast"] = [
        {**dict(r), "archived": bool(r["archived"])}
        for r in conn.execute(
            "SELECT c.id, c.name, c.description, c.archived, ci.path AS image_path,"
            " c.voice_id IS NOT NULL AS has_voice"
            " FROM series_cast sc JOIN characters c ON c.id = sc.character_id"
            " LEFT JOIN character_images ci ON ci.id = c.main_image_id"
            " WHERE sc.series_id = ? ORDER BY c.name",
            (ep["series_id"],),
        )
    ]
    for c in out["cast"]:
        c["has_voice"] = bool(c["has_voice"])
    out["locations"] = [
        {**dict(r), "archived": bool(r["archived"])}
        for r in conn.execute(
            "SELECT l.id, l.name, l.description, l.archived,"
            " (SELECT COUNT(*) FROM location_images li WHERE li.location_id = l.id"
            "  AND li.ref_order IS NOT NULL AND li.hidden = 0) AS ref_image_count"
            " FROM locations l WHERE l.id IN"
            " (SELECT location_id FROM series_locations WHERE series_id = ?) OR l.id = ?"
            " ORDER BY l.name",
            (ep["series_id"], ep["default_location_id"]),
        )
    ]
    out["spend"] = episode_summary(conn, ep["id"])
    out["openrouter_model"] = split.get_model(conn)
    return out


@router.get("/{episode_id}")
def get_episode(episode_id: int, conn: db.Conn) -> dict[str, Any]:
    return episode_out(conn, _episode_or_404(conn, episode_id))


class EpisodePatch(BaseModel):
    title: str | None = None
    default_location_id: int | None = None
    take_count: int | None = Field(default=None, ge=1)
    spend_cap_usd: float | None = Field(default=None, gt=0)


@router.patch("/{episode_id}")
def patch_episode(episode_id: int, body: EpisodePatch, conn: db.Conn) -> dict[str, Any]:
    _episode_or_404(conn, episode_id)
    fields = body.model_dump(include=body.model_fields_set)
    for key in ("title", "take_count", "spend_cap_usd"):
        if key in fields and fields[key] is None:
            raise HTTPException(422, f"{key} cannot be null")
    loc = fields.get("default_location_id")
    if loc is not None and conn.execute("SELECT 1 FROM locations WHERE id = ?", (loc,)).fetchone() is None:
        raise HTTPException(400, f"location {loc} not found")
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE episodes SET {sets}, updated_at = ? WHERE id = ?",
            [*fields.values(), db.now(), episode_id],
        )
        conn.commit()
    return episode_out(conn, _episode_or_404(conn, episode_id))
