"""Helpers shared by the character/location/series/music routers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import HTTPException

from .. import db, jobs
from ..library import LibraryError, Owner, is_library_image
from ..paidgen import IMAGE_JOB, MAX_IMAGES_PER_CALL, NANO_BANANA_ASPECTS, image_cost


@contextmanager
def raise_http(conn: sqlite3.Connection | None = None) -> Iterator[None]:
    """Turn LibraryError into an HTTP error, rolling back uncommitted changes."""
    try:
        yield
    except LibraryError as exc:
        if conn is not None and conn.in_transaction:
            conn.rollback()
        raise HTTPException(exc.status, exc.detail) from exc


def generate_images(
    conn: sqlite3.Connection,
    owner: Owner,
    owner_id: int,
    prompt: str,
    num_images: int,
    input_paths: list[str],
    aspect_ratio: str | None,
) -> dict[str, Any]:
    """Queue a nano-banana job (edit mode when input images from the library are given)."""
    if not 1 <= num_images <= MAX_IMAGES_PER_CALL:
        raise HTTPException(422, f"num_images must be 1-{MAX_IMAGES_PER_CALL}")
    if aspect_ratio is not None and aspect_ratio not in NANO_BANANA_ASPECTS:
        raise HTTPException(422, f"aspect_ratio must be one of {', '.join(NANO_BANANA_ASPECTS)}")
    if aspect_ratio == "auto" and not input_paths:
        raise HTTPException(422, "aspect_ratio 'auto' needs input images")
    for p in input_paths:
        if not is_library_image(conn, p):
            raise HTTPException(422, f"input image {p!r} is not in the library")
    job_id = jobs.enqueue(
        conn,
        IMAGE_JOB,
        {
            "owner": owner,
            "owner_id": owner_id,
            "prompt": prompt,
            "num_images": num_images,
            "input_paths": input_paths,
            "aspect_ratio": aspect_ratio,
        },
    )
    return {"job_id": job_id, "estimated_cost_usd": image_cost(num_images)}


def owner_jobs(
    conn: sqlite3.Connection, owner: Owner, owner_id: int, kinds: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Recent generation jobs for a character/location (status, result, error)."""
    marks = ",".join("?" * len(kinds))
    rs = conn.execute(
        "SELECT id, kind, payload, status, error, result, created_at, updated_at FROM jobs"
        f" WHERE kind IN ({marks})"
        " AND json_extract(payload, '$.owner') = ? AND json_extract(payload, '$.owner_id') = ?"
        " ORDER BY id DESC LIMIT 20",
        (*kinds, owner, owner_id),
    ).fetchall()
    out = []
    for r in rs:
        d = dict(r)
        d["payload"] = db.loads(d["payload"], {})
        d["result"] = db.loads(d["result"])
        out.append(d)
    return out
