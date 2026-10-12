"""Overdub lines on takes: generate (Chatterbox, cloned voice), place, archive."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db, overdub

router = APIRouter()


class CreateBody(BaseModel):
    character_id: int
    text: str
    exaggeration: float = 0.5
    offset_s: float = Field(default=0.0, ge=0)


class PatchBody(BaseModel):
    offset_s: float | None = Field(default=None, ge=0)
    volume: float | None = Field(default=None, ge=0)
    replace_voice: bool | None = None


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except overdub.OverdubError as exc:
        raise HTTPException(exc.status_code, exc.detail) from None


@router.get("/takes/{take_id}/overdubs")
def list_overdubs(take_id: int, conn: db.Conn) -> list[dict[str, Any]]:
    return [overdub.serialize(o) for o in overdub.list_for_take(conn, take_id)]


@router.post("/takes/{take_id}/overdubs", status_code=201)
def create_overdub(take_id: int, body: CreateBody, conn: db.Conn) -> dict[str, Any]:
    return _call(
        overdub.create,
        conn,
        take_id,
        character_id=body.character_id,
        text=body.text,
        exaggeration=body.exaggeration,
        offset_s=body.offset_s,
    )


@router.patch("/overdubs/{overdub_id}")
def patch_overdub(overdub_id: int, body: PatchBody, conn: db.Conn) -> dict[str, Any]:
    return _call(
        overdub.update,
        conn,
        overdub_id,
        offset_s=body.offset_s,
        volume=body.volume,
        replace_voice=body.replace_voice,
    )


@router.post("/overdubs/{overdub_id}/archive", status_code=204)
def archive_overdub(overdub_id: int, conn: db.Conn) -> None:
    _call(overdub.archive, conn, overdub_id)
