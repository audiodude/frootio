"""Locations: CRUD, archive, images (upload/generate) and the 0-3 ref images."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .. import db, media
from ..library import add_image, image_ext, image_json, location_json
from ..paidgen import IMAGE_JOB
from .library_common import generate_images, owner_jobs, raise_http

router = APIRouter(prefix="/locations", tags=["locations"])

MAX_REF_IMAGES = 3


class LocationCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""


class LocationPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    archived: bool | None = None


class RefsIn(BaseModel):
    image_ids: list[int]  # in ref order; empty = no ref images


class GenerateImagesIn(BaseModel):
    prompt: str = Field(min_length=1)
    num_images: int = Field(default=2, ge=1, le=4)
    input_paths: list[str] = []
    aspect_ratio: str | None = None


def _get(conn, location_id: int):
    loc = conn.execute("SELECT * FROM locations WHERE id = ?", (location_id,)).fetchone()
    if loc is None:
        raise HTTPException(404, "location not found")
    return loc


def _detail(conn, location_id: int) -> dict[str, Any]:
    out = location_json(conn, _get(conn, location_id))
    out["images"] = [
        image_json(r)
        for r in conn.execute(
            "SELECT * FROM location_images WHERE location_id = ? ORDER BY id DESC", (location_id,)
        )
    ]
    out["spend_usd"] = conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0) FROM spend WHERE location_id = ?", (location_id,)
    ).fetchone()[0]
    return out


def _touch(conn, location_id: int) -> None:
    conn.execute("UPDATE locations SET updated_at = ? WHERE id = ?", (db.now(), location_id))


@router.get("")
def list_locations(conn: db.Conn, include_archived: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM locations" + ("" if include_archived else " WHERE archived = 0") + " ORDER BY name"
    return [location_json(conn, r) for r in conn.execute(sql).fetchall()]


@router.post("", status_code=201)
def create_location(body: LocationCreate, conn: db.Conn) -> dict[str, Any]:
    name = " ".join(body.name.split())
    ts = db.now()
    slug = db.unique_slug(conn, "locations", media.slugify(name))
    lid = conn.execute(
        "INSERT INTO locations (name, slug, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (name, slug, body.description, ts, ts),
    ).lastrowid
    conn.commit()
    return _detail(conn, lid)


@router.get("/{location_id}")
def get_location(location_id: int, conn: db.Conn) -> dict[str, Any]:
    return _detail(conn, location_id)


@router.patch("/{location_id}")
def patch_location(location_id: int, body: LocationPatch, conn: db.Conn) -> dict[str, Any]:
    _get(conn, location_id)
    if body.name is not None:
        name = " ".join(body.name.split())
        if not name:
            raise HTTPException(422, "name can't be empty")
        conn.execute("UPDATE locations SET name = ? WHERE id = ?", (name, location_id))
    if body.description is not None:
        conn.execute("UPDATE locations SET description = ? WHERE id = ?", (body.description, location_id))
    if body.archived is not None:
        conn.execute("UPDATE locations SET archived = ? WHERE id = ?", (int(body.archived), location_id))
    _touch(conn, location_id)
    conn.commit()
    return _detail(conn, location_id)


@router.post("/{location_id}/images", status_code=201)
def upload_images(
    location_id: int, conn: db.Conn, files: Annotated[list[UploadFile], File()]
) -> dict[str, Any]:
    _get(conn, location_id)
    with raise_http(conn):
        ids = [_add_upload(conn, location_id, f) for f in files]
        _touch(conn, location_id)
    conn.commit()
    return {"image_ids": ids, "location": _detail(conn, location_id)}


@router.put("/{location_id}/refs")
def set_refs(location_id: int, body: RefsIn, conn: db.Conn) -> dict[str, Any]:
    """Choose the location's ref images (0-3, in order); the rest stay library-only."""
    _get(conn, location_id)
    if len(body.image_ids) > MAX_REF_IMAGES:
        raise HTTPException(422, f"a location has at most {MAX_REF_IMAGES} ref images")
    if len(set(body.image_ids)) != len(body.image_ids):
        raise HTTPException(422, "duplicate image ids")
    for iid in body.image_ids:
        if not conn.execute(
            "SELECT 1 FROM location_images WHERE id = ? AND location_id = ?", (iid, location_id)
        ).fetchone():
            raise HTTPException(404, f"image {iid} not found for this location")
    conn.execute("UPDATE location_images SET ref_order = NULL WHERE location_id = ?", (location_id,))
    for order, iid in enumerate(body.image_ids, start=1):
        conn.execute("UPDATE location_images SET ref_order = ? WHERE id = ?", (order, iid))
    _touch(conn, location_id)
    conn.commit()
    return _detail(conn, location_id)


@router.post("/{location_id}/images/generate", status_code=202)
def generate(location_id: int, body: GenerateImagesIn, conn: db.Conn) -> dict[str, Any]:
    _get(conn, location_id)
    return generate_images(
        conn, "location", location_id, body.prompt, body.num_images, body.input_paths, body.aspect_ratio
    )


@router.get("/{location_id}/jobs")
def location_jobs(location_id: int, conn: db.Conn) -> list[dict[str, Any]]:
    _get(conn, location_id)
    return owner_jobs(conn, "location", location_id, (IMAGE_JOB,))


def _add_upload(conn, location_id: int, f: UploadFile) -> int:
    data = f.file.read()
    return add_image(conn, "location", location_id, data, image_ext(f.filename, f.content_type, data))
