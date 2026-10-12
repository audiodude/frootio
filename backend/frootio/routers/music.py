"""Series music library: list and upload tracks (series/<slug>/music/<track>.mp3)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .. import db
from ..library import add_music_track
from .library_common import raise_http

router = APIRouter(prefix="/series", tags=["music"])


def _tracks(conn, series_id: int) -> list[dict[str, Any]]:
    return db.rows(
        conn.execute("SELECT * FROM music_tracks WHERE series_id = ? ORDER BY name", (series_id,)).fetchall()
    )


def _check(conn, series_id: int) -> None:
    if not conn.execute("SELECT 1 FROM series WHERE id = ?", (series_id,)).fetchone():
        raise HTTPException(404, "series not found")


@router.get("/{series_id}/music")
def list_music(series_id: int, conn: db.Conn) -> list[dict[str, Any]]:
    _check(conn, series_id)
    return _tracks(conn, series_id)


@router.post("/{series_id}/music", status_code=201)
def upload_music(
    series_id: int,
    conn: db.Conn,
    files: Annotated[list[UploadFile], File()],
    name: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Upload one or more audio files; `name` (single file only) overrides the track name."""
    _check(conn, series_id)
    if name and len(files) != 1:
        raise HTTPException(422, "name can only be given with a single file")
    ids = []
    with raise_http(conn), tempfile.TemporaryDirectory() as tmp:
        for i, f in enumerate(files):
            filename = Path(f.filename or f"track-{i + 1}")
            src = Path(tmp) / f"{i}{filename.suffix.lower()[:10]}"
            src.write_bytes(f.file.read())
            ids.append(add_music_track(conn, series_id, src, name or filename.stem))
    conn.commit()
    return {"track_ids": ids, "music": _tracks(conn, series_id)}
