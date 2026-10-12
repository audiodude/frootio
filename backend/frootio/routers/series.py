"""Series: CRUD, casting of characters and locations, episode list/create.

Episode detail/edit (`/api/episodes/{id}`) lives in the episodes router (T3).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import config, db, media, spend
from ..library import cast_character, character_json, location_json
from .library_common import raise_http

router = APIRouter(prefix="/series", tags=["series"])


class SeriesCreate(BaseModel):
    name: str = Field(min_length=1)
    style_preamble: str = ""


class SeriesPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    style_preamble: str | None = None
    archived: bool | None = None


class EpisodeCreate(BaseModel):
    number: int | None = Field(default=None, ge=0)  # default: next number
    title: str = ""
    default_location_id: int | None = None
    take_count: int = Field(default=1, ge=1)
    spend_cap_usd: float = Field(default=config.DEFAULT_SPEND_CAP_USD, gt=0)


def _get(conn, series_id: int):
    s = conn.execute("SELECT * FROM series WHERE id = ?", (series_id,)).fetchone()
    if s is None:
        raise HTTPException(404, "series not found")
    return s


def episodes_json(conn, series_id: int) -> list[dict[str, Any]]:
    rs = conn.execute(
        "SELECT e.id, e.series_id, e.number, e.title, e.default_location_id, l.name AS default_location_name,"
        " e.take_count, e.spend_cap_usd, e.archived, e.created_at, e.updated_at,"
        " (SELECT COUNT(*) FROM script_versions v WHERE v.episode_id = e.id) AS script_versions,"
        " (SELECT COUNT(*) FROM snippets sn WHERE sn.episode_id = e.id AND sn.archived = 0) AS snippets,"
        " (SELECT COALESCE(SUM(amount_usd), 0) FROM spend sp WHERE sp.episode_id = e.id) AS spend_usd"
        " FROM episodes e LEFT JOIN locations l ON l.id = e.default_location_id"
        " WHERE e.series_id = ? ORDER BY e.number",
        (series_id,),
    ).fetchall()
    return [{**dict(r), "archived": bool(r["archived"])} for r in rs]


def _summary(conn, s) -> dict[str, Any]:
    counts = conn.execute(
        "SELECT (SELECT COUNT(*) FROM episodes WHERE series_id = :id),"
        " (SELECT COUNT(*) FROM series_cast WHERE series_id = :id),"
        " (SELECT COUNT(*) FROM series_locations WHERE series_id = :id)",
        {"id": s["id"]},
    ).fetchone()
    return {
        **dict(s),
        "archived": bool(s["archived"]),
        "episode_count": counts[0],
        "cast_count": counts[1],
        "location_count": counts[2],
        "spend_usd": spend.series_total(conn, s["id"]),
    }


def _detail(conn, series_id: int) -> dict[str, Any]:
    s = _get(conn, series_id)
    cast = [
        character_json(conn, c)
        for c in conn.execute(
            "SELECT c.* FROM series_cast sc JOIN characters c ON c.id = sc.character_id"
            " WHERE sc.series_id = ? ORDER BY c.name",
            (series_id,),
        ).fetchall()
    ]
    locations = [
        location_json(conn, loc)
        for loc in conn.execute(
            "SELECT l.* FROM series_locations sl JOIN locations l ON l.id = sl.location_id"
            " WHERE sl.series_id = ? ORDER BY l.name",
            (series_id,),
        ).fetchall()
    ]
    music = db.rows(
        conn.execute("SELECT * FROM music_tracks WHERE series_id = ? ORDER BY name", (series_id,)).fetchall()
    )
    return {
        **_summary(conn, s),
        "episodes": episodes_json(conn, series_id),
        "cast": cast,
        "locations": locations,
        "music": music,
        "spend": spend.series_summary(conn, series_id),
    }


@router.get("")
def list_series(conn: db.Conn, include_archived: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM series" + ("" if include_archived else " WHERE archived = 0") + " ORDER BY name"
    return [_summary(conn, s) for s in conn.execute(sql).fetchall()]


@router.post("", status_code=201)
def create_series(body: SeriesCreate, conn: db.Conn) -> dict[str, Any]:
    name = " ".join(body.name.split())
    ts = db.now()
    slug = db.unique_slug(conn, "series", media.slugify(name))
    sid = conn.execute(
        "INSERT INTO series (name, slug, style_preamble, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (name, slug, body.style_preamble, ts, ts),
    ).lastrowid
    conn.commit()
    return _detail(conn, sid)


@router.get("/{series_id}")
def get_series(series_id: int, conn: db.Conn) -> dict[str, Any]:
    return _detail(conn, series_id)


@router.patch("/{series_id}")
def patch_series(series_id: int, body: SeriesPatch, conn: db.Conn) -> dict[str, Any]:
    _get(conn, series_id)
    if body.name is not None:
        name = " ".join(body.name.split())
        if not name:
            raise HTTPException(422, "name can't be empty")
        conn.execute("UPDATE series SET name = ? WHERE id = ?", (name, series_id))
    if body.style_preamble is not None:
        conn.execute("UPDATE series SET style_preamble = ? WHERE id = ?", (body.style_preamble, series_id))
    if body.archived is not None:
        conn.execute("UPDATE series SET archived = ? WHERE id = ?", (int(body.archived), series_id))
    conn.execute("UPDATE series SET updated_at = ? WHERE id = ?", (db.now(), series_id))
    conn.commit()
    return _detail(conn, series_id)


# --- casting -------------------------------------------------------------------------


@router.put("/{series_id}/cast/{character_id}")
def add_cast(series_id: int, character_id: int, conn: db.Conn) -> dict[str, Any]:
    _get(conn, series_id)
    with raise_http(conn):
        cast_character(conn, series_id, character_id)
    conn.commit()
    return _detail(conn, series_id)


@router.delete("/{series_id}/cast/{character_id}")
def remove_cast(series_id: int, character_id: int, conn: db.Conn) -> dict[str, Any]:
    _get(conn, series_id)
    conn.execute(
        "DELETE FROM series_cast WHERE series_id = ? AND character_id = ?", (series_id, character_id)
    )
    conn.commit()
    return _detail(conn, series_id)


@router.put("/{series_id}/locations/{location_id}")
def add_location(series_id: int, location_id: int, conn: db.Conn) -> dict[str, Any]:
    _get(conn, series_id)
    loc = conn.execute("SELECT archived FROM locations WHERE id = ?", (location_id,)).fetchone()
    if loc is None:
        raise HTTPException(404, "location not found")
    if not conn.execute(
        "SELECT 1 FROM series_locations WHERE series_id = ? AND location_id = ?", (series_id, location_id)
    ).fetchone():
        if loc["archived"]:
            raise HTTPException(422, "archived locations can't be added to a series")
        conn.execute(
            "INSERT INTO series_locations (series_id, location_id, created_at) VALUES (?, ?, ?)",
            (series_id, location_id, db.now()),
        )
        conn.commit()
    return _detail(conn, series_id)


@router.delete("/{series_id}/locations/{location_id}")
def remove_location(series_id: int, location_id: int, conn: db.Conn) -> dict[str, Any]:
    _get(conn, series_id)
    conn.execute(
        "DELETE FROM series_locations WHERE series_id = ? AND location_id = ?", (series_id, location_id)
    )
    conn.commit()
    return _detail(conn, series_id)


# --- episodes ------------------------------------------------------------------------


@router.get("/{series_id}/episodes")
def list_episodes(series_id: int, conn: db.Conn) -> list[dict[str, Any]]:
    _get(conn, series_id)
    return episodes_json(conn, series_id)


@router.post("/{series_id}/episodes", status_code=201)
def create_episode(series_id: int, body: EpisodeCreate, conn: db.Conn) -> dict[str, Any]:
    _get(conn, series_id)
    number = body.number
    if number is None:
        number = conn.execute(
            "SELECT COALESCE(MAX(number), 0) + 1 FROM episodes WHERE series_id = ?", (series_id,)
        ).fetchone()[0]
    if conn.execute(
        "SELECT 1 FROM episodes WHERE series_id = ? AND number = ?", (series_id, number)
    ).fetchone():
        raise HTTPException(409, f"episode {number} already exists in this series")
    if body.default_location_id is not None:
        loc = conn.execute(
            "SELECT l.archived FROM series_locations sl JOIN locations l ON l.id = sl.location_id"
            " WHERE sl.series_id = ? AND sl.location_id = ?",
            (series_id, body.default_location_id),
        ).fetchone()
        if loc is None:
            raise HTTPException(422, "default location must be one of the series' locations")
        if loc["archived"]:
            raise HTTPException(422, "default location is archived")
    ts = db.now()
    eid = conn.execute(
        "INSERT INTO episodes (series_id, number, title, default_location_id, take_count, spend_cap_usd,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            series_id,
            number,
            body.title,
            body.default_location_id,
            body.take_count,
            body.spend_cap_usd,
            ts,
            ts,
        ),
    ).lastrowid
    conn.commit()
    return next(e for e in episodes_json(conn, series_id) if e["id"] == eid)
