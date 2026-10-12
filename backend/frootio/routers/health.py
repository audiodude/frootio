"""Health/status endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .. import config, db

router = APIRouter(tags=["health"])


@router.get("/health")
def health(conn: db.Conn) -> dict[str, Any]:
    s = config.settings()
    schema_version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    jobs = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")}
    return {
        "ok": True,
        "schema_version": schema_version,
        "media_root": str(s.media_root),
        "db_path": str(s.db_path),
        "fal_configured": s.fal_key is not None,
        "openrouter_configured": s.openrouter_api_key is not None,
        "openrouter_model": s.openrouter_model,
        "jobs": jobs,
    }
