"""Generate takes for a snippet: cost estimate and the Generate click."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db, generation

router = APIRouter(prefix="/snippets", tags=["generate"])

MAX_TAKES_PER_CLICK = 20


class GenerateBody(BaseModel):
    n: int | None = Field(default=None, ge=1, le=MAX_TAKES_PER_CLICK)  # default: episode take_count
    seed: int | None = Field(default=None, ge=0)  # omitted: the snippet's pinned seed (NULL = random)


@router.get("/{snippet_id}/estimate")
def get_estimate(
    snippet_id: int, conn: db.Conn, n: int | None = Query(default=None, ge=1, le=MAX_TAKES_PER_CLICK)
) -> dict[str, Any]:
    try:
        return generation.estimate(conn, snippet_id, n)
    except generation.GenerateError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.post("/{snippet_id}/generate", status_code=201)
async def post_generate(snippet_id: int, body: GenerateBody, conn: db.Conn) -> dict[str, Any]:
    seed = body.seed
    if "seed" not in body.model_fields_set:
        r = conn.execute("SELECT pinned_seed FROM snippets WHERE id = ?", (snippet_id,)).fetchone()
        seed = r[0] if r is not None else None
    try:
        take_ids = await generation.generate(conn, snippet_id, body.n, seed)
    except generation.GenerateError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    selected = conn.execute("SELECT selected_take_id FROM snippets WHERE id = ?", (snippet_id,)).fetchone()[0]
    marks = ",".join("?" * len(take_ids))
    takes = conn.execute(f"SELECT * FROM takes WHERE id IN ({marks}) ORDER BY id", take_ids).fetchall()
    return {"takes": [generation.take_summary(t, selected) for t in takes]}
