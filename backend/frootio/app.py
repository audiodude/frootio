"""FastAPI app factory.

Lifespan: migrate the DB, write a startup backup, start the job worker and the
24h backup loop. API routers are mounted under `/api` (each router module
defines its own sub-prefix, e.g. `APIRouter(prefix="/series")`), media files
under `/media`, and everything else falls back to the built SPA.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import backup, config, db, events, jobs
from .routers import (
    assembly,
    characters,
    episodes,
    generate,
    health,
    locations,
    music,
    overdubs,
    scripts,
    series,
    snippets,
    spend,
    takes,
)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = config.settings()
    s.media_root.mkdir(parents=True, exist_ok=True)
    conn = db.connect()
    try:
        applied = db.migrate(conn)
    finally:
        conn.close()
    if applied:
        log.info("applied schema migrations %s", applied)
    try:
        path = await asyncio.to_thread(backup.backup_now)
        log.info("startup backup written: %s", path)
    except Exception:
        log.exception("startup backup failed")
    await jobs.start()
    backup_task = asyncio.create_task(backup.backup_loop(), name="frootio-backup")
    try:
        yield
    finally:
        backup_task.cancel()
        await asyncio.gather(backup_task, return_exceptions=True)
        await jobs.stop()


def create_app(frontend_dist: Path | None = None) -> FastAPI:
    s = config.settings()
    dist = (frontend_dist or s.frontend_dist).resolve()
    app = FastAPI(title="frootio", lifespan=lifespan)

    # API routers: one include line per area.
    app.include_router(health.router, prefix="/api")
    app.include_router(events.router, prefix="/api")
    app.include_router(spend.router, prefix="/api")
    app.include_router(series.router, prefix="/api")
    app.include_router(characters.router, prefix="/api")
    app.include_router(locations.router, prefix="/api")
    app.include_router(music.router, prefix="/api")
    app.include_router(assembly.router, prefix="/api")
    app.include_router(episodes.router, prefix="/api")
    app.include_router(scripts.router, prefix="/api")
    app.include_router(snippets.router, prefix="/api")
    app.include_router(generate.router, prefix="/api")
    app.include_router(takes.router, prefix="/api")
    app.include_router(overdubs.router, prefix="/api")

    s.media_root.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=s.media_root), name="media")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        if full_path == "api" or full_path.startswith(("api/", "media/")):
            raise HTTPException(404, "not found")
        index = dist / "index.html"
        if not index.is_file():
            raise HTTPException(503, "frontend not built: run `npm run build` in frontend/")
        if full_path:
            candidate = (dist / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(dist):
                return FileResponse(candidate)
        return FileResponse(index)

    return app
