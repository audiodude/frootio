"""Spend totals API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .. import db
from ..spend import episode_summary, series_summary, totals

router = APIRouter(prefix="/spend", tags=["spend"])


@router.get("")
def get_totals(conn: db.Conn) -> dict[str, Any]:
    return totals(conn)


@router.get("/series/{series_id}")
def get_series(series_id: int, conn: db.Conn) -> dict[str, Any]:
    out = series_summary(conn, series_id)
    if out is None:
        raise HTTPException(404, "series not found")
    return out


@router.get("/episodes/{episode_id}")
def get_episode(episode_id: int, conn: db.Conn) -> dict[str, Any]:
    out = episode_summary(conn, episode_id)
    if out is None:
        raise HTTPException(404, "episode not found")
    return out
